"""Универсальные утилиты пагинации для Telegram inline-клавиатур."""  # noqa: E501
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE_DEFAULT = 8
PAGE_SIZE_USERS   = 10
PAGE_SIZE_ORGS    = 8
PAGE_SIZE_SALES   = 8
PAGE_SIZE_BTN     = 5   # inline-button lists (shops, products, users)


def paginate(items: list, page: int = 0, per_page: int = PAGE_SIZE_DEFAULT):
    """Возвращает (items_on_page, has_prev, has_next, total_pages, clamped_page)."""
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end   = start + per_page
    return items[start:end], page > 0, end < total, total_pages, page


def page_nav_row(
    callback_prefix: str,
    page: int,
    has_prev: bool,
    has_next: bool,
    total_pages: int = None,
) -> list[InlineKeyboardButton]:
    """Строка навигации ◀ N/M ▶ (только нужные кнопки)."""
    buttons: list[InlineKeyboardButton] = []
    if has_prev:
        buttons.append(InlineKeyboardButton(
            text="◀️", callback_data=f"{callback_prefix}{page - 1}"
        ))
    if total_pages and total_pages > 1:
        buttons.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="pg_noop"
        ))
    if has_next:
        buttons.append(InlineKeyboardButton(
            text="▶️", callback_data=f"{callback_prefix}{page + 1}"
        ))
    return buttons
