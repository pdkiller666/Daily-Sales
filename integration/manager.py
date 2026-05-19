"""Integration manager: triggers and schedules exports + OAuth token refresh."""
import asyncio
import json
import logging
import time
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger

from integration.providers.google_sheets import GoogleSheetsProvider

logger = logging.getLogger(__name__)

AVAILABLE_FIELDS = {
    'sales':     ['date', 'product_name', 'shop_name', 'quantity', 'price', 'total',
                  'seller_name', 'category'],
    'inventory': ['shop_name', 'product_name', 'category', 'quantity', 'last_updated'],
    'products':  ['name', 'category', 'price', 'description'],
    'staff':     ['name', 'shop_name', 'role', 'phone'],
    'plans':     ['type', 'metric', 'target', 'period', 'shop_name', 'seller_name'],
}

FIELD_LABELS = {
    'date': 'Дата продажи',
    'product_name': 'Название товара',
    'shop_name': 'Магазин',
    'quantity': 'Количество',
    'price': 'Цена',
    'total': 'Сумма',
    'seller_name': 'Продавец',
    'category': 'Категория',
    'last_updated': 'Обновлено',
    'name': 'Название',
    'description': 'Описание',
    'role': 'Роль',
    'phone': 'Телефон',
    'metric': 'Метрика',
    'target': 'Цель',
    'period': 'Период',
}


class IntegrationManager:

    def __init__(self):
        self.providers = {
            'google_sheets': GoogleSheetsProvider(),
        }

    # ───────────────────────────────────────────────────────
    #  OAuth token management
    # ───────────────────────────────────────────────────────

    async def _ensure_valid_token(self, db, conn_id: int, conn_config: dict) -> dict:
        """
        Check OAuth token expiry. Refresh if needed and save new token to DB.
        Returns potentially-updated config dict.
        """
        if conn_config.get("auth_type") != "oauth":
            return conn_config

        tokens = conn_config.get("tokens", {})
        expiry = tokens.get("expiry", 0)

        if time.time() < expiry - 300:
            return conn_config

        refresh_token = tokens.get("refresh_token", "")
        if not refresh_token:
            raise ValueError("OAuth refresh_token отсутствует — переподключите Google аккаунт")

        logger.info(f"Refreshing OAuth token for connection {conn_id}")
        from integration.auth.google_oauth import refresh_access_token
        new_tokens = await refresh_access_token(refresh_token)

        tokens.update(new_tokens)
        updated_config = dict(conn_config)
        updated_config["tokens"] = tokens

        db.update_integration_connection(conn_id, config=json.dumps(updated_config))
        return updated_config

    def save_oauth_tokens(self, db, conn_id: int, token_data: dict):
        """Merge new OAuth tokens into connection config and save."""
        conn = db.get_integration_connection(conn_id)
        if not conn:
            return
        existing = json.loads(conn[3] or '{}')
        existing["tokens"] = {
            "access_token":  token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "expiry":        token_data.get("expiry", time.time() + 3600),
        }
        db.update_integration_connection(conn_id, config=json.dumps(existing))

    # ───────────────────────────────────────────────────────
    #  Motivation sync
    # ───────────────────────────────────────────────────────

    async def sync_motivation_from_sheet(
        self, db, conn_id: int,
        sheet_name: str,
        header_row: int = 6,
        dns_row: int = 2,
        mvm_row: int = 3,
        rrp_row: int = 4,
        model_start_col: int = 14,
    ) -> dict:
        """
        Read bonus rates from a weekly sheet and cache them in gs_bonus_cache.
        Returns {'synced': N, 'models': [list], 'sheet': sheet_name}.
        """
        conn = db.get_integration_connection(conn_id)
        if not conn:
            raise ValueError("Подключение не найдено")

        conn_config = json.loads(conn[3] or '{}')
        conn_config = await self._ensure_valid_token(db, conn_id, conn_config)

        provider = self.providers.get("google_sheets")
        sheet_name = self._render_sheet_name(sheet_name, {})

        bonuses = await provider.read_motivation_rows(
            conn_config, sheet_name,
            header_row=header_row,
            dns_row=dns_row,
            mvm_row=mvm_row,
            rrp_row=rrp_row,
            model_start_col=model_start_col,
        )

        synced = 0
        models = []
        for model_name, rates in bonuses.items():
            for chain in ("dns", "mvm"):
                bonus = rates.get(chain, 0.0)
                rrp = rates.get("rrp", 0.0)
                db.upsert_bonus_cache(conn_id, model_name, chain, bonus, rrp)
            synced += 1
            models.append(model_name)

        db.add_integration_log(conn_id, None, 'success',
                               f'motivation sync: {synced} моделей из "{sheet_name}"')
        return {'synced': synced, 'models': models, 'sheet': sheet_name}

    # ───────────────────────────────────────────────────────
    #  Export trigger
    # ───────────────────────────────────────────────────────

    async def trigger_export(self, db, export_type: str, event_data: dict):
        """Called after a business event. Runs immediate exports in background."""
        try:
            exports = db.get_enabled_exports_by_type(export_type, schedule='immediate')
            logger.info(f"trigger_export: type={export_type} found={len(exports)} db={db.db_file}")
            for exp in exports:
                asyncio.create_task(self._run_export(db, exp, event_data))
        except Exception as e:
            logger.error(f"trigger_export error ({export_type}): {e}")

    async def trigger_export_with_result(
            self, db, export_type: str, event_data: dict, timeout: float = 10.0
    ) -> list:
        """
        Run immediate exports and return results synchronously (with timeout).
        Returns list of {'success': bool, 'error': str|None}.
        Returns [] if no exports are configured — caller should not add any status line.
        """
        try:
            exports = db.get_enabled_exports_by_type(export_type, schedule='immediate')
            if not exports:
                return []
            tasks = [self._run_export_with_result(db, exp, event_data) for exp in exports]
            raw = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
            out = []
            for r in raw:
                if isinstance(r, Exception):
                    out.append({'success': False, 'error': str(r)})
                else:
                    out.append(r)
            return out
        except asyncio.TimeoutError:
            return [{'success': False, 'error': f'Таймаут (>{int(timeout)}с)'}]
        except Exception as e:
            logger.warning(f"trigger_export_with_result error: {e}")
            return [{'success': False, 'error': str(e)}]

    async def try_export_line(self, db, export_type: str, event_data: dict) -> str:
        """
        Convenience wrapper: run export and return a ready-to-append status line.
        Returns '' if no exports configured (no line added to message).
        Returns '\\n📋 Google Таблицы: ✅ Записано' on success.
        Returns '\\n📋 Google Таблицы: ⚠️ Ошибка записи' on failure.
        """
        try:
            results = await self.trigger_export_with_result(db, export_type, event_data)
            if not results:
                return ""
            if all(r['success'] for r in results):
                return "\n📋 Google Таблицы: ✅ Записано"
            return "\n📋 Google Таблицы: ⚠️ Ошибка записи"
        except Exception as e:
            logger.warning(f"try_export_line {export_type}: {e}")
            return ""

    async def _run_export_with_result(self, db, export_row, event_data: dict) -> dict:
        """Like _run_export but returns {success, error} instead of notifying admins."""
        (export_id, conn_id, export_type, schedule, target_sheet,
         operation, mapping_json, lookup_json, conn_config_json) = export_row[:9]
        try:
            conn_config = json.loads(conn_config_json or '{}')
            provider_name = conn_config.get('provider', 'google_sheets')
            provider = self.providers.get(provider_name)
            if not provider:
                raise ValueError(f"Провайдер не найден: {provider_name}")
            conn_config = await self._ensure_valid_token(db, conn_id, conn_config)
            sheet_name = self._render_sheet_name(target_sheet or 'Sheet1', event_data)
            cfg = dict(conn_config)
            if operation == 'append_row':
                await self._do_append_row(provider, cfg, sheet_name, mapping_json, event_data)
            elif operation == 'update_cell':
                await self._do_update_cell(provider, cfg, sheet_name, lookup_json, event_data)
            elif operation == 'replace_sheet':
                await self._do_replace_sheet(provider, cfg, sheet_name, db, export_type)
            db.add_integration_log(conn_id, export_id, 'success',
                                   f'{operation} on "{sheet_name}" OK')
            db.update_integration_export_last_run(export_id)
            return {'success': True, 'error': None}
        except asyncio.CancelledError:
            try:
                db.add_integration_log(conn_id, export_id, 'error', 'Таймаут — операция отменена')
            except Exception:
                pass
            raise
        except Exception as e:
            msg = str(e)
            logger.error(f"_run_export_with_result id={export_id} error: {msg}")
            try:
                db.add_integration_log(conn_id, export_id, 'error', msg)
            except Exception:
                pass
            return {'success': False, 'error': msg}

    async def _run_export(self, db, export_row, event_data: dict):
        """Execute a single export configuration."""
        (export_id, conn_id, export_type, schedule, target_sheet,
         operation, mapping_json, lookup_json, conn_config_json) = export_row[:9]

        try:
            conn_config = json.loads(conn_config_json or '{}')
            provider_name = conn_config.get('provider', 'google_sheets')
            provider = self.providers.get(provider_name)
            if not provider:
                raise ValueError(f"Провайдер не найден: {provider_name}")

            conn_config = await self._ensure_valid_token(db, conn_id, conn_config)

            sheet_name = self._render_sheet_name(target_sheet or 'Sheet1', event_data)

            cfg = dict(conn_config)

            if operation == 'append_row':
                await self._do_append_row(provider, cfg, sheet_name,
                                          mapping_json, event_data)

            elif operation == 'update_cell':
                await self._do_update_cell(provider, cfg, sheet_name,
                                           lookup_json, event_data)

            elif operation == 'replace_sheet':
                await self._do_replace_sheet(provider, cfg, sheet_name,
                                             db, export_type)

            db.add_integration_log(conn_id, export_id, 'success',
                                   f'{operation} on "{sheet_name}" OK')
            db.update_integration_export_last_run(export_id)

        except asyncio.CancelledError:
            try:
                db.add_integration_log(conn_id, export_id, 'error', 'Задача отменена (shutdown)')
            except Exception:
                pass
            raise
        except Exception as e:
            msg = str(e)
            logger.error(f"_run_export id={export_id} error: {msg}")
            try:
                db.add_integration_log(conn_id, export_id, 'error', msg)
            except Exception:
                pass
            await self._notify_admins(db, f"⚠️ Ошибка экспорта Google Sheets\n\n{msg}")

    async def _do_append_row(self, provider, cfg, sheet_name,
                              mapping_json, event_data):
        mapping = json.loads(mapping_json or '{}')
        row = [str(event_data.get(f, '')) for f in mapping.keys()]
        logger.info(f"_do_append_row: sheet={sheet_name} row={row}")
        await provider.append_row(cfg, sheet_name, row)

    async def _do_update_cell(self, provider, cfg, sheet_name,
                               lookup_json, event_data):
        lookup = json.loads(lookup_json or '{}')
        row_col   = int(lookup.get('row_search_col', 1))
        row_field = lookup.get('row_search_field', '')
        col_row   = int(lookup.get('col_search_row', 1))
        col_field = lookup.get('col_search_field', '')
        upd_op    = lookup.get('operation', 'set')
        val_field = lookup.get('value_field', '')
        start_row = int(lookup.get('data_start_row', col_row + 1))
        start_col = int(lookup.get('data_start_col', 1))
        aliases   = lookup.get('aliases', {})

        row_raw = str(event_data.get(row_field, ''))
        col_raw = str(event_data.get(col_field, ''))
        row_value = aliases.get(row_raw, row_raw)
        col_value = aliases.get(col_raw, col_raw)
        new_value = event_data.get(val_field, 0)

        await provider.update_cell_matrix(
            cfg, sheet_name,
            row_col=row_col, row_value=row_value,
            col_row=col_row, col_value=col_value,
            upd_op=upd_op, new_value=new_value,
            start_row=start_row, start_col=start_col,
            row_raw=row_raw, col_raw=col_raw,
        )

    async def _do_replace_sheet(self, provider, cfg, sheet_name, db, export_type):
        data = self._get_replace_data(db, export_type)
        if data:
            await provider.replace_sheet(cfg, sheet_name,
                                         data['headers'], data['rows'])

    def _get_replace_data(self, db, export_type):
        try:
            if export_type == 'sales':
                rows_raw = db.get_all_sales_for_export()
                headers = ['Дата', 'Товар', 'Категория', 'Магазин',
                           'Количество', 'Цена', 'Сумма', 'Продавец']
                rows = [[str(r[i]) for i in range(min(len(r), 8))] for r in rows_raw]
                return {'headers': headers, 'rows': rows}
            elif export_type == 'inventory':
                rows_raw = db.get_all_inventory_for_export()
                headers = ['Магазин', 'Товар', 'Категория', 'Количество', 'Обновлено']
                rows = [[str(r[i]) for i in range(min(len(r), 5))] for r in rows_raw]
                return {'headers': headers, 'rows': rows}
        except Exception as e:
            logger.error(f"_get_replace_data error ({export_type}): {e}")
        return None

    def _render_sheet_name(self, template: str, event_data: dict) -> str:
        now = datetime.now()
        macros = {
            '{year}':  str(now.year),
            '{month}': f"{now.month:02d}",
            '{day}':   f"{now.day:02d}",
            '{week}':  str(now.isocalendar()[1]),
        }
        for macro, value in macros.items():
            template = template.replace(macro, value)
        return template

    async def _notify_admins(self, db, text: str):
        try:
            from main import bot
            admins = db.get_all_admins_telegram_ids()
            for tg_id in admins:
                try:
                    await bot.send_message(int(tg_id), text)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"_notify_admins error: {e}")

    async def schedule_exports(self, scheduler, get_db_paths_fn):
        """Register cron-scheduled export jobs at bot startup."""
        try:
            db_paths = get_db_paths_fn()
            for path in db_paths:
                from database import Database
                db = Database(path)
                db.create_tables()
                try:
                    exports = db.get_all_enabled_cron_exports()
                    for exp in exports:
                        export_id = exp[0]
                        cron_str  = exp[3]
                        if not cron_str or cron_str == 'immediate':
                            continue
                        job_id = f'gs_export_{path}_{export_id}'
                        scheduler.add_job(
                            self._scheduled_wrapper,
                            CronTrigger.from_crontab(cron_str),
                            args=[path, exp],
                            id=job_id,
                            replace_existing=True,
                            misfire_grace_time=300,
                        )
                        logger.info(f"Scheduled export job: {job_id} cron={cron_str}")
                except Exception as e:
                    logger.error(f"schedule_exports DB {path}: {e}")
        except Exception as e:
            logger.error(f"schedule_exports error: {e}")

    async def _scheduled_wrapper(self, db_path: str, export_row):
        from database import Database
        db = Database(db_path)
        db.create_tables()
        await self._run_export(db, export_row, {})


integration_manager = IntegrationManager()
