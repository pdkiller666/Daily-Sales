"""Abstract base class for integration providers."""
from abc import ABC, abstractmethod


class BaseProvider(ABC):

    @abstractmethod
    async def test_connection(self, config: dict) -> tuple:
        """Test connectivity. Returns (ok: bool, message: str)."""

    @abstractmethod
    async def get_headers(self, config: dict, sheet_name: str) -> list:
        """Return list of header values from row 1 of the sheet."""

    @abstractmethod
    async def append_row(self, config: dict, sheet_name: str, row: list) -> bool:
        """Append a row to the sheet. Returns True on success."""

    @abstractmethod
    async def update_cell(self, config: dict, sheet_name: str,
                          row_idx: int, col_idx: int, value) -> bool:
        """Update a single cell (1-based indices). Returns True on success."""

    @abstractmethod
    async def find_row_by_value(self, config: dict, sheet_name: str,
                                col_idx: int, value: str):
        """Search column col_idx for value. Returns 1-based row index or None."""

    @abstractmethod
    async def find_col_by_value(self, config: dict, sheet_name: str,
                                row_idx: int, value: str):
        """Search row row_idx for value. Returns 1-based col index or None."""

    @abstractmethod
    async def get_cell(self, config: dict, sheet_name: str,
                       row_idx: int, col_idx: int):
        """Read a cell value (1-based). Returns string or None."""

    @abstractmethod
    async def replace_sheet(self, config: dict, sheet_name: str,
                            headers: list, rows: list) -> bool:
        """Clear sheet and write headers + rows. Returns True on success."""
