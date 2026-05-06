"""Утилиты для push-уведомлений бота.

Содержит вспомогательную функцию add_read_btn(), которая добавляет кнопку
«✅ Прочитано» к клавиатуре любого уведомления. При нажатии — сообщение
удаляется из чата (уведомление уже сохранено в notification_history).
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

_READ_BTN = [InlineKeyboardButton(text="✅ Прочитано", callback_data="notif_read")]


def add_read_btn(existing_markup: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    """Добавляет строку с кнопкой «✅ Прочитано» к клавиатуре уведомления.

    Если existing_markup равен None — создаёт клавиатуру с единственной кнопкой.
    Иначе добавляет кнопку как новую строку в конце существующей клавиатуры.
    """
    if existing_markup is None:
        return InlineKeyboardMarkup(inline_keyboard=[_READ_BTN])
    rows = [list(row) for row in existing_markup.inline_keyboard]
    rows.append(_READ_BTN)
    return InlineKeyboardMarkup(inline_keyboard=rows)
