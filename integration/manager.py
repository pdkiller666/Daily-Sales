"""Integration manager: triggers and schedules exports."""
import asyncio
import json
import logging
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger

from integration.providers.google_sheets import GoogleSheetsProvider

logger = logging.getLogger(__name__)

AVAILABLE_FIELDS = {
    'sales': ['date', 'product_name', 'shop_name', 'quantity', 'price', 'total',
              'seller_name', 'category'],
    'inventory': ['shop_name', 'product_name', 'category', 'quantity', 'last_updated'],
    'products': ['name', 'category', 'price', 'description'],
    'staff': ['name', 'shop_name', 'role', 'phone'],
    'plans': ['type', 'metric', 'target', 'period', 'shop_name', 'seller_name'],
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

    async def trigger_export(self, db, export_type: str, event_data: dict):
        """Called after a business event. Runs immediate exports in background."""
        try:
            exports = db.get_enabled_exports_by_type(export_type, schedule='immediate')
            for exp in exports:
                asyncio.create_task(self._run_export(db, exp, event_data))
        except Exception as e:
            logger.error(f"trigger_export error ({export_type}): {e}")

    async def _run_export(self, db, export_row, event_data: dict):
        """Execute a single export configuration."""
        (export_id, conn_id, export_type, schedule, target_sheet,
         operation, mapping_json, lookup_json, conn_config_json) = export_row[:9]

        try:
            conn_config = json.loads(conn_config_json or '{}')
            spreadsheet_id = conn_config.get('spreadsheet_id', '')
            provider_name = conn_config.get('provider', 'google_sheets')
            provider = self.providers.get(provider_name)
            if not provider:
                raise ValueError(f"Провайдер не найден: {provider_name}")

            sheet_name = self._render_sheet_name(target_sheet or 'Sheet1', event_data)
            cfg = {'spreadsheet_id': spreadsheet_id}

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
        headers = await provider.get_headers(cfg, sheet_name)
        if not headers:
            row = [str(event_data.get(f, '')) for f in mapping.keys()]
        else:
            row = []
            for header in headers:
                field = next((k for k, v in mapping.items() if v == header), None)
                row.append(str(event_data.get(field, '')) if field else '')
        await provider.append_row(cfg, sheet_name, row)

    async def _do_update_cell(self, provider, cfg, sheet_name,
                               lookup_json, event_data):
        lookup = json.loads(lookup_json or '{}')
        row_col = int(lookup.get('row_search_col', 1))
        row_field = lookup.get('row_search_field', '')
        col_row = int(lookup.get('col_search_row', 1))
        col_field = lookup.get('col_search_field', '')
        upd_op = lookup.get('operation', 'set')
        val_field = lookup.get('value_field', '')

        row_value = str(event_data.get(row_field, ''))
        col_value = str(event_data.get(col_field, ''))
        new_value = event_data.get(val_field, 0)

        row_idx = await provider.find_row_by_value(cfg, sheet_name, row_col, row_value)
        col_idx = await provider.find_col_by_value(cfg, sheet_name, col_row, col_value)

        if row_idx is None:
            raise ValueError(
                f"Строка не найдена: «{row_value}» в колонке {row_col} листа «{sheet_name}»"
            )
        if col_idx is None:
            raise ValueError(
                f"Столбец не найден: «{col_value}» в строке {col_row} листа «{sheet_name}»"
            )

        if upd_op in ('increment', 'decrement'):
            current = await provider.get_cell(cfg, sheet_name, row_idx, col_idx)
            try:
                current_num = float(current) if current else 0.0
            except (ValueError, TypeError):
                current_num = 0.0
            delta = float(new_value)
            new_value = current_num + delta if upd_op == 'increment' else current_num - delta

        await provider.update_cell(cfg, sheet_name, row_idx, col_idx, new_value)

    async def _do_replace_sheet(self, provider, cfg, sheet_name, db, export_type):
        data = self._get_replace_data(db, export_type)
        if data:
            await provider.replace_sheet(cfg, sheet_name, data['headers'], data['rows'])

    def _get_replace_data(self, db, export_type):
        """Get data for replace_sheet operation."""
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
            '{year}': str(now.year),
            '{month}': f"{now.month:02d}",
            '{day}': f"{now.day:02d}",
            '{week}': str(now.isocalendar()[1]),
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
        """Called at bot startup to register cron-scheduled export jobs."""
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
                        cron_str = exp[3]
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
                        logger.info(f"Scheduled integration export job: {job_id} cron={cron_str}")
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
