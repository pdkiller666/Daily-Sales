"""Google Sheets provider using gspread_asyncio."""
import json
import logging
import os

import gspread_asyncio
from google.oauth2.service_account import Credentials
from tenacity import (retry, stop_after_attempt, wait_exponential,
                      retry_if_exception_type)

from integration.base import BaseProvider

logger = logging.getLogger(__name__)

_SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]


def _make_credentials():
    sa_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip()
    if not sa_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON не задан в переменных окружения")
    sa_info = json.loads(sa_json)
    return Credentials.from_service_account_info(sa_info, scopes=_SCOPES)


_client_manager = gspread_asyncio.AsyncioGspreadClientManager(_make_credentials)


async def _get_worksheet(spreadsheet_id: str, sheet_name: str):
    agc = await _client_manager.authorize()
    ss = await agc.open_by_key(spreadsheet_id)
    return await ss.worksheet(sheet_name)


class GoogleSheetsProvider(BaseProvider):

    async def test_connection(self, config: dict) -> tuple:
        try:
            spreadsheet_id = config.get('spreadsheet_id', '')
            if not spreadsheet_id:
                return False, "spreadsheet_id не задан"
            agc = await _client_manager.authorize()
            ss = await agc.open_by_key(spreadsheet_id)
            sheets = await ss.worksheets()
            names = [s.title for s in sheets]
            return True, f"Подключено. Листы: {', '.join(names[:5])}"
        except Exception as e:
            return False, str(e)

    async def get_headers(self, config: dict, sheet_name: str) -> list:
        try:
            ws = await _get_worksheet(config['spreadsheet_id'], sheet_name)
            return await ws.row_values(1)
        except Exception as e:
            logger.error(f"get_headers error: {e}")
            return []

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type(Exception),
           reraise=True)
    async def append_row(self, config: dict, sheet_name: str, row: list) -> bool:
        ws = await _get_worksheet(config['spreadsheet_id'], sheet_name)
        await ws.append_row(row, value_input_option='USER_ENTERED')
        return True

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type(Exception),
           reraise=True)
    async def update_cell(self, config: dict, sheet_name: str,
                          row_idx: int, col_idx: int, value) -> bool:
        ws = await _get_worksheet(config['spreadsheet_id'], sheet_name)
        await ws.update_cell(row_idx, col_idx, value)
        return True

    async def find_row_by_value(self, config: dict, sheet_name: str,
                                col_idx: int, value: str):
        try:
            ws = await _get_worksheet(config['spreadsheet_id'], sheet_name)
            col_values = await ws.col_values(col_idx)
            for i, v in enumerate(col_values, start=1):
                if str(v).strip() == str(value).strip():
                    return i
            return None
        except Exception as e:
            logger.error(f"find_row_by_value error: {e}")
            return None

    async def find_col_by_value(self, config: dict, sheet_name: str,
                                row_idx: int, value: str):
        try:
            ws = await _get_worksheet(config['spreadsheet_id'], sheet_name)
            row_values = await ws.row_values(row_idx)
            for i, v in enumerate(row_values, start=1):
                if str(v).strip() == str(value).strip():
                    return i
            return None
        except Exception as e:
            logger.error(f"find_col_by_value error: {e}")
            return None

    async def get_cell(self, config: dict, sheet_name: str,
                       row_idx: int, col_idx: int):
        try:
            ws = await _get_worksheet(config['spreadsheet_id'], sheet_name)
            cell = await ws.cell(row_idx, col_idx)
            return cell.value
        except Exception as e:
            logger.error(f"get_cell error: {e}")
            return None

    async def replace_sheet(self, config: dict, sheet_name: str,
                            headers: list, rows: list) -> bool:
        ws = await _get_worksheet(config['spreadsheet_id'], sheet_name)
        await ws.clear()
        all_rows = [headers] + rows
        await ws.update('A1', all_rows, value_input_option='USER_ENTERED')
        return True
