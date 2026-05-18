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
                      retry_if_exception_type)

from integration.base import BaseProvider

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
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

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type(Exception),
           reraise=True)
    async def append_row(self, config: dict, sheet_name: str, row: list) -> bool:
        ws = await _get_ws(config, sheet_name)
        await asyncio.to_thread(
            ws.append_row, row, value_input_option="USER_ENTERED"
        )
        return True

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10),
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
           wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type(Exception),
           reraise=True)
    async def replace_sheet(self, config: dict, sheet_name: str,
                            headers: list, rows: list) -> bool:
        ws = await _get_ws(config, sheet_name)
        await asyncio.to_thread(ws.clear)
        all_rows = [headers] + rows
        await asyncio.to_thread(
            ws.update, "A1", all_rows, value_input_option="USER_ENTERED"
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
