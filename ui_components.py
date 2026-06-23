"""
ui_components.py — единая точка входа для повторно используемых UI-компонентов
бота (roadmap 1.3).

Цель: убрать разнобой в кнопках «Назад», календарях и постраничной навигации.
Все хендлеры должны брать эти примитивы отсюда, а не объявлять свои варианты
(◀ / ← / 🔙 / кастомные nav-строки).

Модуль НЕ содержит собственной реализации — он реэкспортирует канонические
функции из keyboards.py и pagination_utils.py, чтобы существующий код
(`from keyboards import back_button`) продолжал работать без изменений.
"""
from aiogram.types import InlineKeyboardButton

# Канонические клавиатурные примитивы
from keyboards import (
    back_button,
    home_button,
    generate_calendar,
    safe_cb,
    resolve_cb_name,
)

# Постраничная навигация
from pagination_utils import (
    paginate,
    page_nav_row,
    PAGE_SIZE_DEFAULT,
    PAGE_SIZE_USERS,
    PAGE_SIZE_ORGS,
    PAGE_SIZE_SALES,
    PAGE_SIZE_BTN,
)


def back_row(callback_data: str, text: str = "⬅️ Назад") -> list[InlineKeyboardButton]:
    """Строка-обёртка из одной кнопки «Назад» (для inline_keyboard списков)."""
    return [back_button(callback_data, text)]


__all__ = [
    "back_button",
    "back_row",
    "home_button",
    "generate_calendar",
    "safe_cb",
    "resolve_cb_name",
    "paginate",
    "page_nav_row",
    "PAGE_SIZE_DEFAULT",
    "PAGE_SIZE_USERS",
    "PAGE_SIZE_ORGS",
    "PAGE_SIZE_SALES",
    "PAGE_SIZE_BTN",
]
