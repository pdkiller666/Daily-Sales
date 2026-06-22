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
        self._token_refresh_locks: dict[int, asyncio.Lock] = {}

    @staticmethod
    def _unwrap(db):
        """Возвращает синхронный Database из AsyncDatabase (или сам db если уже sync)."""
        try:
            return object.__getattribute__(db, '_db')
        except AttributeError:
            return db

    # ───────────────────────────────────────────────────────
    #  OAuth token management
    # ───────────────────────────────────────────────────────

    async def _ensure_valid_token(self, db, conn_id: int, conn_config: dict) -> dict:
        """
        Check OAuth token expiry. Refresh if needed and save new token to DB.
        Returns potentially-updated config dict.
        Uses per-connection lock to prevent concurrent refresh races.
        """
        db = self._unwrap(db)
        if conn_config.get("auth_type") != "oauth":
            return conn_config

        if time.time() < conn_config.get("tokens", {}).get("expiry", 0) - 300:
            return conn_config

        if conn_id not in self._token_refresh_locks:
            self._token_refresh_locks[conn_id] = asyncio.Lock()

        async with self._token_refresh_locks[conn_id]:
            tokens = conn_config.get("tokens", {})
            if time.time() < tokens.get("expiry", 0) - 300:
                return conn_config

            refresh_token = tokens.get("refresh_token", "")
            if not refresh_token:
                raise ValueError("OAuth refresh_token отсутствует — переподключите Google аккаунт")

            logger.info(f"Refreshing OAuth token for connection {conn_id}")
            from integration.auth.google_oauth import refresh_access_token, OAuthTokenRevokedException
            try:
                new_tokens = await refresh_access_token(refresh_token)
            except OAuthTokenRevokedException as revoked_exc:
                logger.warning(f"OAuth token revoked for conn {conn_id}: {revoked_exc}")
                try:
                    db.update_integration_connection(conn_id, enabled=0)
                    db.add_integration_log(conn_id, None, 'error',
                                          'Токен отозван — подключение отключено автоматически')
                except Exception:
                    pass
                raise ValueError(str(revoked_exc)) from revoked_exc

            tokens.update(new_tokens)
            updated_config = dict(conn_config)
            updated_config["tokens"] = tokens

            db.update_integration_connection(conn_id, config=json.dumps(updated_config))
            return updated_config

    def save_oauth_tokens(self, db, conn_id: int, token_data: dict):
        """Merge new OAuth tokens into connection config and save."""
        db = self._unwrap(db)
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
    #  Motivation sync + config (config in connection config['motiv_config'])
    # ───────────────────────────────────────────────────────

    def get_motiv_config(self, db, conn_id: int) -> dict | None:
        """Return the saved motivation-import config for a connection, or None."""
        db = self._unwrap(db)
        conn = db.get_integration_connection(conn_id)
        if not conn:
            return None
        try:
            cfg = json.loads(conn[3] or '{}')
        except Exception:
            return None
        return cfg.get('motiv_config')

    def save_motiv_config(self, db, conn_id: int, motiv_config: dict) -> None:
        """Persist the motivation-import config inside the connection config JSON."""
        db = self._unwrap(db)
        conn = db.get_integration_connection(conn_id)
        if not conn:
            raise ValueError("Подключение не найдено")
        try:
            cfg = json.loads(conn[3] or '{}')
        except Exception:
            cfg = {}
        cfg['motiv_config'] = motiv_config
        db.update_integration_connection(conn_id, config=json.dumps(cfg, ensure_ascii=False))

    def get_import_config(self, db, conn_id: int, import_type: str = None):
        """Return saved import config(s) for a connection.
        With import_type → the config dict for that type (or None);
        without → the whole {import_type: cfg} map ({} if none)."""
        db = self._unwrap(db)
        conn = db.get_integration_connection(conn_id)
        if not conn:
            return None if import_type else {}
        try:
            cfg = json.loads(conn[3] or '{}')
        except Exception:
            return None if import_type else {}
        configs = cfg.get('import_configs', {}) or {}
        if import_type:
            return configs.get(import_type)
        return configs

    def save_import_config(self, db, conn_id: int, import_type: str,
                           import_cfg: dict) -> None:
        """Persist an import mapping (per import_type) inside the connection config."""
        db = self._unwrap(db)
        conn = db.get_integration_connection(conn_id)
        if not conn:
            raise ValueError("Подключение не найдено")
        try:
            cfg = json.loads(conn[3] or '{}')
        except Exception:
            cfg = {}
        configs = cfg.setdefault('import_configs', {})
        configs[import_type] = import_cfg
        db.update_integration_connection(conn_id, config=json.dumps(cfg, ensure_ascii=False))

    async def run_import_from_config(self, db, conn_id: int, import_type: str) -> dict:
        """Re-run an import using the saved config for this connection/type."""
        cfg = self.get_import_config(db, conn_id, import_type)
        if not cfg:
            raise ValueError("Конфигурация импорта не настроена. "
                             "Запустите мастер импорта.")
        return await self.run_import(
            db, conn_id, import_type,
            sheet_name=cfg.get('sheet_name', 'Sheet1'),
            header_row=int(cfg.get('header_row', 1)),
            col_mapping=cfg.get('col_mapping') or {},
        )

    async def sync_motivation_from_sheet(
        self, db, conn_id: int,
        sheet_name: str,
        header_row: int,
        model_col: int,
        bonus_col_map: dict,
        rrp_col: int = None,
        aliases: dict = None,
    ) -> dict:
        """
        Read bonus rates from a sheet (rows = models, columns = chains) and
        cache them in gs_bonus_cache.
        `aliases` maps a sheet model name → the system model name; applied
        (case-insensitively, trimmed) before writing to the cache.
        Returns {'synced': N, 'models': [list], 'sheet': sheet_name, 'chains': [list]}.
        """
        db = self._unwrap(db)
        conn = db.get_integration_connection(conn_id)
        if not conn:
            raise ValueError("Подключение не найдено")

        conn_config = json.loads(conn[3] or '{}')
        conn_config = await self._ensure_valid_token(db, conn_id, conn_config)

        provider = self.providers.get("google_sheets")
        rendered = self._render_sheet_name(sheet_name, {})

        rows = await provider.read_motivation_table(
            conn_config, rendered,
            header_row=header_row,
            model_col=model_col,
            bonus_col_map=bonus_col_map,
            rrp_col=rrp_col,
        )

        # Replace cache for this connection so removed chains/models don't linger
        db.clear_bonus_cache(conn_id)

        # Normalize aliases for case-insensitive, trimmed lookup
        alias_lookup = {}
        for k, v in (aliases or {}).items():
            ks = str(k).strip().lower()
            vs = str(v).strip()
            if ks and vs:
                alias_lookup[ks] = vs

        # Build a normalized product-name lookup ONCE (case-insensitive, trimmed).
        # get_product_by_name() does an exact `name = ?` match — sheet model names
        # rarely match byte-for-byte, so without this fallback rules_written stays 0
        # while the user sees a "success" message. Python .lower() folds Cyrillic too
        # (unlike SQLite LOWER which is ASCII-only).
        prod_by_norm = {}
        try:
            for p in db.get_all_products():
                key = str(p[1]).strip().lower()
                if key and key not in prod_by_norm:
                    prod_by_norm[key] = p
        except Exception:
            prod_by_norm = {}

        # Known trade networks from employee profiles (normalized). The sheet column
        # headers (e.g. "DNS") must match the trade_network stored in profiles
        # (e.g. "Днс") or resolve_motivation won't find the rule. We warn the user
        # about any sheet network that matches no profile so they can add an alias.
        known_networks_norm = set()
        try:
            for nw in (db.get_all_trade_networks() or []):
                ns = str(nw).strip().lower()
                if ns:
                    known_networks_norm.add(ns)
        except Exception:
            known_networks_norm = set()

        synced = 0
        models = []
        chains = set()
        rules_written = 0
        matched_models = 0
        unmatched = []
        unmatched_chains = set()
        for entry in rows:
            model_name = entry['model']
            if alias_lookup:
                model_name = alias_lookup.get(str(model_name).strip().lower(), model_name)
            rrp = entry.get('rrp', 0.0)
            bonuses = entry.get('bonuses', {})
            if not bonuses:
                continue
            # Find matching product once per model for targeted (trade_network) rules:
            # exact match first, then normalized (case/whitespace-insensitive) fallback.
            product = None
            try:
                product = db.get_product_by_name(model_name)
            except Exception:
                product = None
            if not product:
                product = prod_by_norm.get(str(model_name).strip().lower())
            if product:
                matched_models += 1
            else:
                unmatched.append(model_name)
            for chain, bonus in bonuses.items():
                # Resolve the sheet column header (network) through the SAME alias
                # map as models, so "DNS" → "Днс" matches the trade_network stored
                # in profiles. Without this the rule scope_value never matches and
                # resolve_motivation falls back to the wrong / global rate.
                chain_resolved = chain
                if alias_lookup:
                    chain_resolved = alias_lookup.get(str(chain).strip().lower(), chain)
                chain_norm = str(chain_resolved).strip().lower()
                db.upsert_bonus_cache(conn_id, model_name, chain_resolved, bonus, rrp)
                chains.add(chain_resolved)
                if known_networks_norm and chain_norm and chain_norm not in known_networks_norm:
                    unmatched_chains.add(str(chain_resolved))
                # Write a trade_network targeted motivation rule (task #49).
                # Skip blank/zero cells: read_motivation_table maps empty/"-" cells
                # to 0.0, and writing a 0 rule would silently OVERWRITE a previously
                # set rate with zero. Treat blank/0 as "no change" for that chain.
                try:
                    _bonus_val = float(bonus) if bonus is not None else 0.0
                except (TypeError, ValueError):
                    _bonus_val = 0.0
                if product and chain_resolved and _bonus_val > 0:
                    try:
                        ok = db.set_product_motivation(
                            product[0], 'fixed', float(bonus), None,
                            scope_type='trade_network', scope_value=str(chain_resolved),
                            recalculate=False,
                        )
                        if ok:
                            rules_written += 1
                    except Exception:
                        pass
            synced += 1
            models.append(model_name)

        db.add_integration_log(conn_id, None, 'success',
                               f'motivation sync: {synced} моделей из "{rendered}" '
                               f'({matched_models} сопоставлено, {rules_written} таргет-правил, '
                               f'{len(unmatched)} не найдено, '
                               f'{len(unmatched_chains)} сетей без профиля)')
        return {'synced': synced, 'models': models, 'sheet': rendered,
                'chains': sorted(chains), 'rules_written': rules_written,
                'matched_models': matched_models, 'unmatched': unmatched,
                'unmatched_chains': sorted(unmatched_chains)}

    async def run_motiv_sync_from_config(self, db, conn_id: int) -> dict:
        """Re-run a motivation sync using the saved config for this connection."""
        cfg = self.get_motiv_config(db, conn_id)
        if not cfg:
            raise ValueError("Конфигурация мотивации не настроена. "
                             "Запустите мастер «Синхронизировать мотивацию».")
        return await self.sync_motivation_from_sheet(
            db, conn_id,
            sheet_name=cfg.get('sheet_name', 'w{week}'),
            header_row=int(cfg.get('header_row', 1)),
            model_col=int(cfg.get('model_col', 1)),
            bonus_col_map=cfg.get('bonus_col_map', {}),
            rrp_col=cfg.get('rrp_col'),
            aliases=cfg.get('aliases'),
        )


    # ───────────────────────────────────────────────────────
    #  Export trigger
    # ───────────────────────────────────────────────────────

    async def trigger_export(self, db, export_type: str, event_data: dict):
        """Called after a business event. Runs immediate exports in background."""
        db = self._unwrap(db)
        try:
            exports = db.get_enabled_exports_by_type(export_type, schedule='immediate')
            logger.info(f"trigger_export: type={export_type} found={len(exports)} db={db.db_file}")
            for exp in exports:
                asyncio.create_task(self._run_export(db, exp, event_data))
        except Exception as e:
            logger.error(f"trigger_export error ({export_type}): {e}")

    async def trigger_export_with_result(
            self, db, export_type: str, event_data: dict, timeout: float = 60.0
    ) -> list:
        """
        Run immediate exports and return results synchronously (with timeout).
        Returns list of {'success': bool, 'error': str|None}.
        Returns [] if no exports are configured — caller should not add any status line.
        """
        db = self._unwrap(db)
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
        db = self._unwrap(db)
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
        db = self._unwrap(db)
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
        except Exception as raw_e:
            e = self._normalize_gs_error(raw_e, db, conn_id)
            msg = str(e)
            logger.error(f"_run_export_with_result id={export_id} error: {msg}")
            try:
                db.add_integration_log(conn_id, export_id, 'error', msg)
            except Exception:
                pass
            return {'success': False, 'error': msg}

    async def _run_export(self, db, export_row, event_data: dict):
        """Execute a single export configuration."""
        db = self._unwrap(db)
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
        except Exception as raw_e:
            e = self._normalize_gs_error(raw_e, db, conn_id)
            msg = str(e)
            logger.error(f"_run_export id={export_id} error: {msg}")
            try:
                db.add_integration_log(conn_id, export_id, 'error', msg)
            except Exception:
                pass
            if 'отозвана' in msg or 'Переподключите' in msg:
                await self._notify_admins(
                    db,
                    "🔑 <b>Google Sheets: требуется переподключение</b>\n\n"
                    "Авторизация отозвана. Зайдите в Управление орг. → Интеграции и переавторизуйте аккаунт."
                )
            else:
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
        db = self._unwrap(db)
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
            elif export_type == 'products':
                rows_raw = db.get_all_products_for_export()
                headers = ['Название', 'Категория', 'Цена', 'Описание']
                rows = [[str(r[i]) for i in range(min(len(r), 4))] for r in rows_raw]
                return {'headers': headers, 'rows': rows}
            elif export_type == 'staff':
                rows_raw = db.get_all_staff_for_export()
                headers = ['Сотрудник', 'Магазин', 'Город', 'Телефон']
                rows = [[str(r[i]) for i in range(min(len(r), 4))] for r in rows_raw]
                return {'headers': headers, 'rows': rows}
            elif export_type == 'plans':
                rows_raw = db.get_all_plans_for_export()
                headers = ['Тип плана', 'Метрика', 'Цель', 'Магазин', 'Продавец']
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

    def _normalize_gs_error(self, e: Exception, db=None, conn_id: int = None) -> Exception:
        """Convert google.auth RefreshError with invalid_grant → friendly ValueError.
        Also auto-disables the connection in DB."""
        err_str = str(e)
        if 'invalid_grant' in err_str.lower():
            from integration.auth.google_oauth import OAuthTokenRevokedException
            if db is not None and conn_id is not None:
                try:
                    db.update_integration_connection(conn_id, enabled=0)
                    db.add_integration_log(
                        conn_id, None, 'error',
                        'Токен отозван — подключение отключено автоматически'
                    )
                except Exception:
                    pass
            return ValueError(
                "Авторизация Google отозвана или истекла. "
                "Переподключите аккаунт в настройках интеграции."
            )
        return e

    async def sync_matrix_for_period(
            self, db, export_id: int, date_from: str, date_to: str
    ) -> dict:
        """
        Read all sales for the given period, aggregate by (row_field × col_field),
        and write values to the sheet using SET operation (idempotent — safe to repeat).
        Returns {'cells_updated': N, 'cells_skipped': N, 'errors': [...],
                 'sheet': sheet_name, 'total_sales': N}.
        """
        from collections import defaultdict
        db = self._unwrap(db)

        exp = db.get_integration_export(export_id)
        if not exp:
            raise ValueError("Экспорт не найден")

        export_type = exp[0]
        conn_id     = exp[1]
        sheet_tpl   = exp[4]
        operation   = exp[5]
        lookup      = json.loads(exp[7] or '{}')

        if operation != 'update_cell':
            raise ValueError(
                "Синхронизация поддерживается только для операции «Обновить ячейку (матрица)»"
            )

        conn = db.get_integration_connection(conn_id)
        if not conn:
            raise ValueError("Подключение не найдено")
        conn_config = json.loads(conn[3] or '{}')
        conn_config = await self._ensure_valid_token(db, conn_id, conn_config)

        sheet_name = self._render_sheet_name(sheet_tpl, {})

        sales_raw = db.get_sales_for_matrix_sync(date_from, date_to)

        row_field       = lookup.get('row_search_field', 'shop_name')
        col_field       = lookup.get('col_search_field', 'product_name')
        value_field     = lookup.get('value_field', 'quantity')
        row_search_col  = int(lookup.get('row_search_col', 1))
        col_search_row  = int(lookup.get('col_search_row', 1))
        data_start_row  = int(lookup.get('data_start_row', col_search_row + 1))
        data_start_col  = int(lookup.get('data_start_col', 1))
        aliases         = lookup.get('aliases', {})

        # Tuple indices from get_sales_for_matrix_sync:
        # 0=product_name  1=category  2=shop_name  3=quantity_sold
        # 4=sale_price    5=total     6=seller_name
        FIELD_IDX = {
            'product_name': 0,
            'category':     1,
            'shop_name':    2,
            'quantity':     3,
            'price':        4,
            'total':        5,
            'seller_name':  6,
        }
        row_fi = FIELD_IDX.get(row_field, 2)
        col_fi = FIELD_IDX.get(col_field, 0)
        val_fi = FIELD_IDX.get(value_field, 3)

        aggregated: dict = defaultdict(float)
        for row in sales_raw:
            r_val = str(row[row_fi]).strip()
            c_val = str(row[col_fi]).strip()
            v_val = float(row[val_fi]) if row[val_fi] else 0.0
            aggregated[(r_val, c_val)] += v_val

        if not aggregated:
            return {
                'cells_updated': 0, 'cells_skipped': 0,
                'errors': [], 'sheet': sheet_name, 'total_sales': len(sales_raw),
            }

        provider = self.providers.get('google_sheets')
        cells_updated = 0
        cells_skipped = 0
        errors: list = []

        for (row_val, col_val), agg_val in aggregated.items():
            try:
                write_val = (int(agg_val) if value_field == 'quantity'
                             else round(agg_val, 2))
                await provider.update_cell_matrix(
                    conn_config, sheet_name,
                    row_col=row_search_col,
                    row_value=aliases.get(row_val, row_val),
                    col_row=col_search_row,
                    col_value=aliases.get(col_val, col_val),
                    upd_op='set',
                    new_value=write_val,
                    start_row=data_start_row,
                    start_col=data_start_col,
                    row_raw=row_val,
                    col_raw=col_val,
                )
                cells_updated += 1
            except ValueError as e:
                cells_skipped += 1
                errors.append(str(e))
            except Exception as e:
                cells_skipped += 1
                errors.append(f"{row_val} × {col_val}: {e}")

        status = 'success' if cells_skipped == 0 else 'warning'
        db.add_integration_log(
            conn_id, export_id, status,
            f'matrix sync {date_from}–{date_to}: '
            f'{cells_updated} ячеек обновлено, {cells_skipped} пропущено'
        )

        return {
            'cells_updated': cells_updated,
            'cells_skipped': cells_skipped,
            'errors':        errors[:5],
            'sheet':         sheet_name,
            'total_sales':   len(sales_raw),
        }

    async def _notify_admins(self, db, text: str):
        try:
            from bot_holder import get_bot
            from notif_utils import add_read_btn
            bot = get_bot()
            if not bot:
                return
            db = self._unwrap(db)
            admins = db.get_all_admins_telegram_ids()
            for tg_id in admins:
                try:
                    await bot.send_message(int(tg_id), text, parse_mode="HTML",
                                           reply_markup=add_read_btn())
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"_notify_admins error: {e}")

    async def run_import(self, db, conn_id: int, import_type: str,
                          sheet_name: str, header_row: int = 1,
                          col_mapping: dict = None) -> dict:
        """Read data from Google Sheet and import into the bot database."""
        db = self._unwrap(db)
        conn = db.get_integration_connection(conn_id)
        if not conn:
            raise ValueError("Подключение не найдено")
        conn_config = json.loads(conn[3] or '{}')
        conn_config = await self._ensure_valid_token(db, conn_id, conn_config)
        provider = self.providers.get('google_sheets')
        data = await provider.read_all_data(conn_config, sheet_name, header_row)
        headers = data['headers']
        rows = data['rows']
        if not rows:
            return {'imported': 0, 'skipped': 0, 'errors': [], 'headers': headers, 'total': 0}
        if col_mapping is None:
            col_mapping = {}
        imported = 0
        skipped = 0
        errors = []

        def _clean_num(s: str) -> str:
            return s.replace(',', '.').replace('\xa0', '').replace(' ', '').strip()

        if import_type == 'products':
            name_col  = col_mapping.get('name', 0)
            cat_col   = col_mapping.get('category', 1)
            price_col = col_mapping.get('price', 2)
            items = []
            for row in rows:
                try:
                    name = str(row[name_col]).strip() if name_col < len(row) else ''
                    if not name:
                        skipped += 1
                        continue
                    cat = str(row[cat_col]).strip() if cat_col < len(row) else ''
                    if not cat:
                        cat = 'Без категории'
                    ps = str(row[price_col]).strip() if price_col < len(row) else '0'
                    price = float(_clean_num(ps)) if ps else 0.0
                    items.append({'name': name, 'category': cat, 'price': price})
                except Exception as e:
                    errors.append(f"Строка: {e}")
            if items:
                added, skipped_names = db.add_products_bulk(items)
                imported += added
                skipped += len(skipped_names)

        elif import_type == 'inventory':
            shop_col    = col_mapping.get('shop', 0)
            product_col = col_mapping.get('product', 1)
            qty_col     = col_mapping.get('quantity', 2)
            db_conn = db.get_connection()
            cur = db_conn.cursor()
            for row in rows:
                try:
                    shop  = str(row[shop_col]).strip() if shop_col < len(row) else ''
                    pname = str(row[product_col]).strip() if product_col < len(row) else ''
                    if not shop or not pname:
                        skipped += 1
                        continue
                    qs  = str(row[qty_col]).strip() if qty_col < len(row) else '0'
                    qty = int(float(_clean_num(qs))) if qs else 0
                    cur.execute("SELECT id FROM products WHERE LOWER(name) = LOWER(?)", (pname,))
                    prod_row = cur.fetchone()
                    if not prod_row:
                        skipped += 1
                        continue
                    pid = prod_row[0]
                    cur.execute(
                        "UPDATE inventory SET quantity = ?, last_updated = datetime('now') "
                        "WHERE shop_name = ? AND product_id = ?",
                        (qty, shop, pid)
                    )
                    if cur.rowcount == 0:
                        cur.execute(
                            "INSERT OR IGNORE INTO inventory (shop_name, product_id, quantity) VALUES (?, ?, ?)",
                            (shop, pid, qty)
                        )
                    imported += 1
                except Exception as e:
                    errors.append(f"Строка: {e}")
            db_conn.commit()
            db_conn.close()

        elif import_type == 'sales':
            def _parse_date(s: str) -> str:
                s = s.strip()
                for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y',
                            '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
                            '%d.%m.%Y %H:%M', '%d.%m.%Y %H:%M:%S'):
                    try:
                        return datetime.strptime(s, fmt).isoformat()
                    except ValueError:
                        pass
                return ''

            date_col    = col_mapping.get('date', 0)
            product_col = col_mapping.get('product', 1)
            shop_col    = col_mapping.get('shop', 2)
            qty_col     = col_mapping.get('quantity', 3)
            price_col   = col_mapping.get('price', 4)
            db_conn = db.get_connection()
            cur = db_conn.cursor()
            for row in rows:
                try:
                    date_s = str(row[date_col]).strip() if date_col < len(row) else ''
                    pname  = str(row[product_col]).strip() if product_col < len(row) else ''
                    shop   = str(row[shop_col]).strip() if shop_col < len(row) else ''
                    if not pname or not shop:
                        skipped += 1
                        continue
                    qs    = str(row[qty_col]).strip() if qty_col < len(row) else '1'
                    ps    = str(row[price_col]).strip() if price_col < len(row) else ''
                    qty   = int(float(_clean_num(qs))) if qs else 1
                    price = float(_clean_num(ps)) if ps else None
                    cur.execute("SELECT id, price FROM products WHERE LOWER(name) = LOWER(?)", (pname,))
                    prod = cur.fetchone()
                    if not prod:
                        skipped += 1
                        continue
                    pid = prod[0]
                    if price is None:
                        price = float(prod[1] or 0)
                    sale_date = _parse_date(date_s) or datetime.now().isoformat()
                    cur.execute(
                        "INSERT INTO sales (product_id, shop_name, quantity_sold, sale_date, user_id, sale_price)"
                        " VALUES (?, ?, ?, ?, NULL, ?)",
                        (pid, shop, qty, sale_date, price)
                    )
                    imported += 1
                except Exception as e:
                    errors.append(f"Строка: {e}")
            db_conn.commit()
            db_conn.close()

        elif import_type == 'staff':
            name_col  = col_mapping.get('name', 0)
            shop_col  = col_mapping.get('shop', 1)
            phone_col = col_mapping.get('phone', 2)
            db_conn = db.get_connection()
            cur = db_conn.cursor()
            for row in rows:
                try:
                    full_name = str(row[name_col]).strip() if name_col < len(row) else ''
                    shop      = str(row[shop_col]).strip() if shop_col < len(row) else ''
                    phone     = str(row[phone_col]).strip() if phone_col < len(row) else ''
                    if not full_name:
                        skipped += 1
                        continue
                    parts = full_name.split(None, 1)
                    fname = parts[0]
                    lname = parts[1] if len(parts) > 1 else ''
                    cur.execute(
                        "SELECT id FROM users WHERE LOWER(first_name) = LOWER(?)"
                        " AND (? = '' OR LOWER(COALESCE(last_name,'')) = LOWER(?))",
                        (fname, lname, lname)
                    )
                    urow = cur.fetchone()
                    if not urow:
                        skipped += 1
                        continue
                    uid = urow[0]
                    updates, params = [], []
                    if shop:
                        updates.append("shop_name = ?"); params.append(shop)
                    if phone:
                        updates.append("phone = ?"); params.append(phone)
                    if not updates:
                        skipped += 1
                        continue
                    params.append(uid)
                    cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
                    imported += 1
                except Exception as e:
                    errors.append(f"Строка: {e}")
            db_conn.commit()
            db_conn.close()

        elif import_type == 'plans':
            _TYPE_MAP   = {'seller': 'seller', 'продавец': 'seller', 'сотрудник': 'seller',
                           'shop': 'shop', 'магазин': 'shop'}
            _METRIC_MAP = {'turnover': 'turnover', 'оборот': 'turnover', 'сумма': 'turnover',
                           'выручка': 'turnover', 'quantity': 'quantity', 'кол-во': 'quantity',
                           'количество': 'quantity', 'штук': 'quantity'}
            _PERIOD_MAP = {'weekly': 'weekly', 'неделя': 'weekly', 'week': 'weekly',
                           'еженедельно': 'weekly', 'monthly': 'monthly', 'месяц': 'monthly',
                           'month': 'monthly', 'ежемесячно': 'monthly'}
            shop_col   = col_mapping.get('shop', 4)
            seller_col = col_mapping.get('seller', 5)
            type_col   = col_mapping.get('type', 0)
            metric_col = col_mapping.get('metric', 1)
            target_col = col_mapping.get('target', 2)
            period_col = col_mapping.get('period', 3)
            db_conn = db.get_connection()
            cur = db_conn.cursor()
            for row in rows:
                try:
                    plan_type   = _TYPE_MAP.get(
                        str(row[type_col]).strip().lower() if type_col < len(row) else '', '')
                    metric_type = _METRIC_MAP.get(
                        str(row[metric_col]).strip().lower() if metric_col < len(row) else '', '')
                    target_s    = str(row[target_col]).strip() if target_col < len(row) else ''
                    target_type = _PERIOD_MAP.get(
                        str(row[period_col]).strip().lower() if period_col < len(row) else '', '')
                    shop_name   = str(row[shop_col]).strip() if shop_col < len(row) else ''
                    seller_name = str(row[seller_col]).strip() if seller_col < len(row) else ''
                    if not plan_type or not metric_type or not target_s or not target_type:
                        skipped += 1
                        continue
                    target_value = float(_clean_num(target_s))
                    user_id = None
                    if plan_type == 'seller':
                        if not seller_name:
                            skipped += 1
                            continue
                        parts = seller_name.split(None, 1)
                        fname = parts[0]
                        lname = parts[1] if len(parts) > 1 else ''
                        cur.execute(
                            "SELECT id FROM users WHERE LOWER(first_name) = LOWER(?)"
                            " AND (? = '' OR LOWER(COALESCE(last_name,'')) = LOWER(?))",
                            (fname, lname, lname)
                        )
                        urow = cur.fetchone()
                        if not urow:
                            skipped += 1
                            continue
                        user_id = urow[0]
                    cur.execute(
                        "INSERT INTO sales_plans"
                        " (plan_type, metric_type, target_value, target_type, user_id, shop_name, filter_type)"
                        " VALUES (?, ?, ?, ?, ?, ?, 'all')",
                        (plan_type, metric_type, target_value, target_type,
                         user_id, shop_name or None)
                    )
                    imported += 1
                except Exception as e:
                    errors.append(f"Строка: {e}")
            db_conn.commit()
            db_conn.close()

        try:
            db.add_integration_log(conn_id, None, 'success',
                                   f'import {import_type}: {imported} записей')
        except Exception:
            pass
        return {
            'imported': imported,
            'skipped': skipped,
            'errors': errors[:10],
            'headers': headers,
            'total': len(rows),
        }

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
