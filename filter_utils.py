"""Общие утилиты для фильтров по магазину/городу/торговой сети.

Фильтр — ручное сужение внутри scope роли (scope — потолок, фильтр — пол).
Используется в reports, rankings, admin_users, plans.

FSM-ключ фильтра: ADMIN_FILTER_KEY — хранится в data пользователя между экранами.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

ADMIN_FILTER_KEY = "admin_filter"


def empty_filter() -> dict:
    return {"shops": [], "cities": [], "networks": []}


def get_available_filter_values(current_db, scope_type: str, scope_values: list) -> dict:
    """Возвращает доступные значения фильтров в рамках scope роли."""
    try:
        all_shops    = current_db.get_all_shops()
    except Exception:
        all_shops = []
    try:
        all_cities   = current_db.get_all_cities()
    except Exception:
        all_cities = []
    try:
        all_networks = current_db.get_all_trade_networks()
    except Exception:
        all_networks = []

    if not scope_type or scope_type == 'all':
        return {"shops": all_shops, "cities": all_cities, "networks": all_networks}
    elif scope_type == 'shop':
        return {"shops": [s for s in all_shops if s in scope_values], "cities": [], "networks": []}
    elif scope_type == 'city':
        return {"shops": [], "cities": [c for c in all_cities if c in scope_values], "networks": []}
    elif scope_type == 'network':
        return {"shops": [], "cities": [], "networks": [n for n in all_networks if n in scope_values]}
    return {"shops": all_shops, "cities": all_cities, "networks": all_networks}


def filter_to_scope_kwargs(active_filter: dict) -> dict:
    """Конвертирует фильтр в kwargs для методов Database."""
    if active_filter.get("shops"):
        return {"shop_names": active_filter["shops"]}
    if active_filter.get("cities"):
        return {"cities": active_filter["cities"]}
    if active_filter.get("networks"):
        return {"trade_networks": active_filter["networks"]}
    return {}


def merge_scope_with_filter(scope_type: str, scope_values: list, active_filter: dict) -> dict:
    """Объединяет scope роли (потолок) с ручным фильтром (пол).
    Если фильтр пуст — возвращает scope-ограничение."""
    manual = filter_to_scope_kwargs(active_filter)
    if manual:
        return manual
    from dashboard_handlers import _scope_filter_kwargs
    return _scope_filter_kwargs(scope_type, scope_values)


def filter_active_text(active_filter: dict) -> str:
    """Короткое описание активного фильтра."""
    parts = []
    shops    = active_filter.get("shops", [])
    cities   = active_filter.get("cities", [])
    networks = active_filter.get("networks", [])
    if shops:
        parts.append(f"🏪 {shops[0]}" if len(shops) == 1 else f"🏪 {len(shops)} магаз.")
    if cities:
        parts.append(f"🏙 {cities[0]}" if len(cities) == 1 else f"🏙 {len(cities)} город.")
    if networks:
        parts.append(f"🔗 {networks[0]}" if len(networks) == 1 else f"🔗 {len(networks)} сетей")
    return " · ".join(parts)


def is_filter_active(active_filter: dict) -> bool:
    return bool(
        active_filter.get("shops") or
        active_filter.get("cities") or
        active_filter.get("networks")
    )


def has_anything_to_filter(available: dict) -> bool:
    return bool(
        available.get("shops") or
        available.get("cities") or
        available.get("networks")
    )


def filter_button_text(active_filter: dict) -> str:
    """Текст для кнопки открытия фильтра."""
    if is_filter_active(active_filter):
        ft = filter_active_text(active_filter)
        return f"🔍 Фильтр: {ft}"
    return "🔍 Фильтр"


def build_filter_keyboard(
    available: dict,
    active: dict,
    back_cb: str,
) -> InlineKeyboardMarkup:
    """Строит клавиатуру фильтра с чекбоксами (toggle).
    Кнопка '✅ Применить' = callback_data = back_cb (возвращает в модуль).
    """
    from keyboards import safe_cb
    builder = InlineKeyboardBuilder()

    shops    = available.get("shops", [])
    cities   = available.get("cities", [])
    networks = available.get("networks", [])
    active_shops    = active.get("shops", [])
    active_cities   = active.get("cities", [])
    active_networks = active.get("networks", [])

    if shops:
        builder.row(InlineKeyboardButton(text="🏪 Магазины:", callback_data="pg_noop"))
        for s in shops:
            icon = "✅" if s in active_shops else "◻️"
            builder.row(InlineKeyboardButton(
                text=f"{icon} {s}", callback_data=safe_cb("ftog_s_", s)
            ))

    if cities:
        builder.row(InlineKeyboardButton(text="🏙 Города:", callback_data="pg_noop"))
        for c in cities:
            icon = "✅" if c in active_cities else "◻️"
            builder.row(InlineKeyboardButton(
                text=f"{icon} {c}", callback_data=safe_cb("ftog_c_", c)
            ))

    if networks:
        builder.row(InlineKeyboardButton(text="🔗 Торговые сети:", callback_data="pg_noop"))
        for n in networks:
            icon = "✅" if n in active_networks else "◻️"
            builder.row(InlineKeyboardButton(
                text=f"{icon} {n}", callback_data=safe_cb("ftog_n_", n)
            ))

    row = []
    if is_filter_active(active):
        row.append(InlineKeyboardButton(text="🗑 Сбросить", callback_data="flt_reset"))
    row.append(InlineKeyboardButton(text="✅ Применить", callback_data=back_cb))
    builder.row(*row)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    return builder.as_markup()
