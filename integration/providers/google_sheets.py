"""Google Sheets provider — supports OAuth Device Flow and Service Account."""
import asyncio
import json
import logging
import os
import time

import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from tenacity import (retry, stop_after_attempt, wait_exponential,
                      retry_if_exception_type, retry_if_not_exception_type)

from integration.base import BaseProvider

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

TOKEN_URL = "https://oauth2.googleapis.com/token"


# ─────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────

def _make_gc_sync(config: dict) -> gspread.Client:
    """
    Create a synchronous gspread client.
    auth_type='oauth' → uses OAuth tokens from config['tokens'].
    auth_type='service_account' → uses GOOGLE_SERVICE_ACCOUNT_JSON env var.
    """
    auth_type = config.get("auth_type", "service_account")

    if auth_type == "oauth":
        tokens = config.get("tokens", {})
        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
        creds = OAuthCredentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=TOKEN_URL,
            client_id=client_id,
            client_secret=client_secret,
            scopes=_SCOPES,
        )
        return gspread.authorize(creds)

    # service_account
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON не задан в переменных окружения")
    sa_info = json.loads(sa_json)
    creds = SACredentials.from_service_account_info(sa_info, scopes=_SCOPES)
    return gspread.authorize(creds)


async def _get_ws(config: dict, sheet_name: str) -> gspread.Worksheet:
    """Async wrapper: open worksheet by name."""
    def _sync():
        gc = _make_gc_sync(config)
        ss = gc.open_by_key(config["spreadsheet_id"])
        return ss.worksheet(sheet_name)
    return await asyncio.to_thread(_sync)


async def _get_ss(config: dict) -> gspread.Spreadsheet:
    """Async wrapper: open spreadsheet."""
    def _sync():
        gc = _make_gc_sync(config)
        return gc.open_by_key(config["spreadsheet_id"])
    return await asyncio.to_thread(_sync)


def _col_to_letter(n: int) -> str:
    """Convert 1-based column number to A1-notation letter(s). E.g. 1→A, 27→AA."""
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


# ─────────────────────────────────────────────────────────────
#  Provider
# ─────────────────────────────────────────────────────────────

class GoogleSheetsProvider(BaseProvider):

    async def test_connection(self, config: dict) -> tuple[bool, str]:
        try:
            spreadsheet_id = config.get("spreadsheet_id", "")
            if not spreadsheet_id:
                return False, "spreadsheet_id не задан"
            ss = await _get_ss(config)
            sheets = await asyncio.to_thread(ss.worksheets)
            names = [s.title for s in sheets]
            auth_label = "OAuth" if config.get("auth_type") == "oauth" else "сервисный аккаунт"
            return True, f"✅ Подключено ({auth_label}). Листы: {', '.join(names[:8])}"
        except Exception as e:
            return False, str(e)

    async def get_sheets_list(self, config: dict) -> list[str]:
        """Return all worksheet titles."""
        try:
            ss = await _get_ss(config)
            sheets = await asyncio.to_thread(ss.worksheets)
            return [s.title for s in sheets]
        except Exception as e:
            logger.error(f"get_sheets_list error: {e}")
            return []

    async def get_first_rows(self, config: dict, sheet_name: str,
                              max_rows: int = 15) -> dict:
        """Return first max_rows non-empty rows as {row_num: [values]} in ONE API call."""
        try:
            ws = await _get_ws(config, sheet_name)
            all_vals = await asyncio.to_thread(ws.get_all_values)
            result = {}
            for i, row in enumerate(all_vals[:max_rows], start=1):
                if any(str(v).strip() for v in row):
                    result[i] = row
            return result
        except Exception as e:
            logger.error(f"get_first_rows error: {e}")
            return {}

    async def get_headers(self, config: dict, sheet_name: str) -> list:
        try:
            ws = await _get_ws(config, sheet_name)
            return await asyncio.to_thread(ws.row_values, 1)
        except Exception as e:
            logger.error(f"get_headers error: {e}")
            return []

    async def read_row(self, config: dict, sheet_name: str, row_idx: int) -> list:
        """Read all values in a row."""
        try:
            ws = await _get_ws(config, sheet_name)
            return await asyncio.to_thread(ws.row_values, row_idx)
        except Exception as e:
            logger.error(f"read_row error: {e}")
            return []

    async def read_col(self, config: dict, sheet_name: str, col_idx: int) -> list:
        """Read all values in a column."""
        try:
            ws = await _get_ws(config, sheet_name)
            return await asyncio.to_thread(ws.col_values, col_idx)
        except Exception as e:
            logger.error(f"read_col error: {e}")
            return []

    async def read_all_data(self, config: dict, sheet_name: str,
                             header_row: int = 1) -> dict:
        """Read all data from a sheet. Returns {'headers': [...], 'rows': [[...], ...]}."""
        try:
            ws = await _get_ws(config, sheet_name)
            all_vals = await asyncio.to_thread(ws.get_all_values)
            if not all_vals or len(all_vals) < header_row:
                return {'headers': [], 'rows': []}
            headers = all_vals[header_row - 1]
            rows = [r for r in all_vals[header_row:] if any(str(v).strip() for v in r)]
            return {'headers': headers, 'rows': rows}
        except Exception as e:
            logger.error(f"read_all_data error: {e}")
            return {'headers': [], 'rows': []}

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=1, max=5),
           retry=retry_if_exception_type(Exception),
           reraise=True)
    async def append_row(self, config: dict, sheet_name: str, row: list) -> bool:
        ws = await _get_ws(config, sheet_name)
        await asyncio.to_thread(
            ws.append_row, row, value_input_option="USER_ENTERED"
        )
        return True

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=1, max=5),
           retry=retry_if_exception_type(Exception),
           reraise=True)
    async def update_cell(self, config: dict, sheet_name: str,
                          row_idx: int, col_idx: int, value) -> bool:
        ws = await _get_ws(config, sheet_name)
        await asyncio.to_thread(ws.update_cell, row_idx, col_idx, value)
        return True

    async def find_row_by_value(self, config: dict, sheet_name: str,
                                col_idx: int, value: str,
                                start_row: int = 1) -> int | None:
        """Find first row where col_idx == value (case-insensitive strip)."""
        try:
            ws = await _get_ws(config, sheet_name)
            col_vals = await asyncio.to_thread(ws.col_values, col_idx)
            target = str(value).strip().lower()
            for i, v in enumerate(col_vals, start=1):
                if i < start_row:
                    continue
                if str(v).strip().lower() == target:
                    return i
            return None
        except Exception as e:
            logger.error(f"find_row_by_value error: {e}")
            return None

    async def find_col_by_value(self, config: dict, sheet_name: str,
                                row_idx: int, value: str,
                                start_col: int = 1) -> int | None:
        """Find first column where row_idx == value (case-insensitive strip)."""
        try:
            ws = await _get_ws(config, sheet_name)
            row_vals = await asyncio.to_thread(ws.row_values, row_idx)
            target = str(value).strip().lower()
            for i, v in enumerate(row_vals, start=1):
                if i < start_col:
                    continue
                if str(v).strip().lower() == target:
                    return i
            return None
        except Exception as e:
            logger.error(f"find_col_by_value error: {e}")
            return None

    async def get_cell(self, config: dict, sheet_name: str,
                       row_idx: int, col_idx: int):
        try:
            ws = await _get_ws(config, sheet_name)
            cell = await asyncio.to_thread(ws.cell, row_idx, col_idx)
            return cell.value
        except Exception as e:
            logger.error(f"get_cell error: {e}")
            return None

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=1, max=5),
           retry=retry_if_not_exception_type(ValueError),
           reraise=True)
    async def update_cell_matrix(
            self, config: dict, sheet_name: str,
            row_col: int, row_value: str,
            col_row: int, col_value: str,
            upd_op: str, new_value,
            start_row: int = 1, start_col: int = 1,
            row_raw: str = '', col_raw: str = ''
    ) -> None:
        """
        Find row by value in col, find col by value in row, then set/increment/decrement cell.
        Opens the worksheet ONCE and uses batch_get to read col+row in a single
        HTTP request instead of two separate calls — reduces API round-trips from
        5-7 down to 3 (open, batch_get, update_cell).
        Raises ValueError with a descriptive message if row or col is not found.
        ValueError is not retried (config error); other exceptions retry up to 3 times.
        """
        def _sync():
            gc = _make_gc_sync(config)
            ss = gc.open_by_key(config["spreadsheet_id"])
            ws = ss.worksheet(sheet_name)

            # Single batch_get reads column data + row data in ONE HTTP call
            col_letter = _col_to_letter(row_col)
            batch = ws.batch_get([
                f"{col_letter}:{col_letter}",  # entire column for row search
                f"{col_row}:{col_row}",         # entire header row for col search
            ])

            col_vals_raw = batch[0] if batch else []
            col_vals = [r[0] if r else "" for r in col_vals_raw]

            row_vals_raw = batch[1] if len(batch) > 1 and batch[1] else []
            row_vals = list(row_vals_raw[0]) if row_vals_raw else []

            # Find row index by matching column values
            target_row = str(row_value).strip().lower()
            row_idx = None
            for i, v in enumerate(col_vals, start=1):
                if i < start_row:
                    continue
                if str(v).strip().lower() == target_row:
                    row_idx = i
                    break

            if row_idx is None:
                avail = [str(v) for v in col_vals[start_row - 1:] if v][:8]
                alias_note = (f" (псевдоним: «{row_raw}»→«{row_value}»)"
                              if row_raw and row_raw != row_value else "")
                raise ValueError(
                    f"⚠️ Строка не найдена в листе «{sheet_name}»\n"
                    f"Искал: «{row_value}»{alias_note} (колонка {row_col})\n"
                    f"Значения в таблице: {', '.join(avail) or '(пусто)'}\n\n"
                    f"💡 Добавь псевдоним: Интеграции → Экспорт → 📝 Псевдонимы"
                )

            # Find col index by matching row values
            target_col = str(col_value).strip().lower()
            col_idx = None
            for i, v in enumerate(row_vals, start=1):
                if i < start_col:
                    continue
                if str(v).strip().lower() == target_col:
                    col_idx = i
                    break

            if col_idx is None:
                avail = [str(v) for v in row_vals if v][:8]
                alias_note = (f" (псевдоним: «{col_raw}»→«{col_value}»)"
                              if col_raw and col_raw != col_value else "")
                raise ValueError(
                    f"⚠️ Столбец не найден в листе «{sheet_name}»\n"
                    f"Искал: «{col_value}»{alias_note} (строка {col_row})\n"
                    f"Заголовки в таблице: {', '.join(avail) or '(пусто)'}\n\n"
                    f"💡 Добавь псевдоним: Интеграции → Экспорт → 📝 Псевдонимы"
                )

            val = new_value
            if upd_op in ('increment', 'decrement'):
                current = ws.cell(row_idx, col_idx).value
                try:
                    current_num = float(current) if current else 0.0
                except (ValueError, TypeError):
                    current_num = 0.0
                delta = float(new_value)
                val = (current_num + delta if upd_op == 'increment'
                       else current_num - delta)

            ws.update_cell(row_idx, col_idx, val)

        await asyncio.to_thread(_sync)

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=1, max=5),
           retry=retry_if_exception_type(Exception),
           reraise=True)
    async def replace_sheet(self, config: dict, sheet_name: str,
                            headers: list, rows: list) -> bool:
        ws = await _get_ws(config, sheet_name)
        await asyncio.to_thread(ws.clear)
        all_rows = [headers] + rows
        await asyncio.to_thread(
            ws.update, all_rows, "A1", value_input_option="USER_ENTERED"
        )
        return True

    async def read_motivation_rows(self, config: dict, sheet_name: str,
                                   header_row: int, dns_row: int, mvm_row: int,
                                   rrp_row: int, model_start_col: int) -> dict:
        """
        Read motivation bonus rates from a weekly sheet.
        Returns {model_name: {dns: X, mvm: Y, rrp: Z}}.
        """
        try:
            ws = await _get_ws(config, sheet_name)

            def _read():
                headers = ws.row_values(header_row)
                dns_vals = ws.row_values(dns_row)
                mvm_vals = ws.row_values(mvm_row)
                rrp_vals = ws.row_values(rrp_row)
                return headers, dns_vals, mvm_vals, rrp_vals

            headers, dns_vals, mvm_vals, rrp_vals = await asyncio.to_thread(_read)

            result = {}
            for col_i in range(model_start_col - 1, len(headers)):
                model = str(headers[col_i]).strip() if col_i < len(headers) else ""
                if not model:
                    continue
                def _safe_float(lst, i):
                    try:
                        v = lst[i] if i < len(lst) else ""
                        return float(v) if str(v).strip() not in ("", "0") else 0.0
                    except (ValueError, TypeError):
                        return 0.0

                result[model] = {
                    "dns": _safe_float(dns_vals, col_i),
                    "mvm": _safe_float(mvm_vals, col_i),
                    "rrp": _safe_float(rrp_vals, col_i),
                }
            return result
        except Exception as e:
            logger.error(f"read_motivation_rows error: {e}")
            raise
