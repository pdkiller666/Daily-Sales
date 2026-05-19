"""Общие утилиты для фильтров по магазину/городу/торговой сети.

Фильтр — ручное сужение внутри scope роли (scope — потолок, фильтр — пол).
Используется в reports, rankings, admin_users, plans.

FSM-ключ фильтра: ADMIN_FILTER_KEY — хранится в data пользователя между экранами.
"""
import sqlite3 as _sqlite3
import time as _time
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

ADMIN_FILTER_KEY = "admin_filter"

# TTL-кеш справочных данных (магазины/города/сети) — меняются редко.
# Ключ: db_file path. Значение: (shops, cities, networks, timestamp).
_FILTER_VALUES_CACHE: dict = {}
_FILTER_VALUES_TTL = 60


def _get_db_path(current_db) -> str:
    return getattr(current_db, 'db_file', '') or getattr(current_db, 'db_path', '') or str(id(current_db))


def invalidate_filter_values_cache(db_path: str = None) -> None:
    """Инвалидировать кеш при изменении состава магазинов/городов/сетей."""
    if db_path:
        _FILTER_VALUES_CACHE.pop(db_path, None)
    else:
        _FILTER_VALUES_CACHE.clear()


def empty_filter() -> dict:
    return {"shops": [], "cities": [], "networks": []}


def get_available_filter_values(current_db, scope_type: str, scope_values: list) -> dict:
    """Возвращает доступные значения фильтров в рамках scope роли.

    Справочные данные кешируются на _FILTER_VALUES_TTL секунд по пути DB-файла —
    магазины/города/сети меняются редко, поэтому повторных запросов к SQLite нет.
    """
    db_path = _get_db_path(current_db)
    now = _time.time()
    cached = _FILTER_VALUES_CACHE.get(db_path)
    if cached and now - cached[3] < _FILTER_VALUES_TTL:
        all_shops, all_cities, all_networks = cached[0], cached[1], cached[2]
    else:
        all_shops, all_cities, all_networks = [], [], []
        if db_path and db_path != str(id(current_db)):
            try:
                _conn = _sqlite3.connect(db_path)
                all_shops = [r[0] for r in _conn.execute(
                    "SELECT DISTINCT shop_name FROM users "
                    "WHERE shop_name IS NOT NULL AND shop_name != '' "
                    "AND shop_name != 'Системный' AND shop_name != 'System'"
                ).fetchall()]
                all_cities = [r[0] for r in _conn.execute(
                    "SELECT DISTINCT city FROM users "
                    "WHERE city IS NOT NULL AND city != '' AND city != 'System'"
                ).fetchall()]
                all_networks = [r[0] for r in _conn.execute(
                    "SELECT DISTINCT trade_network FROM users "
                    "WHERE trade_network IS NOT NULL AND trade_network != '' "
                    "AND trade_network != 'System'"
                ).fetchall()]
                _conn.close()
            except Exception:
                pass
        _FILTER_VALUES_CACHE[db_path] = (all_shops, all_cities, all_networks, now)

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
