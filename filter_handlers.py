"""Обработчики для системы ручных фильтров (магазин/город/сеть).

Поток:
  1. flt_open_{back_cb}  — открыть панель фильтра
  2. ftog_s_* / ftog_c_* / ftog_n_*  — переключить значение (toggle)
  3. ✅ Применить  — кнопка с callback_data = back_cb → возврат в модуль
  4. flt_reset  — сброс фильтра + перерисовка панели
"""
import asyncio

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from db_utils import get_db, get_user_org_scope
from keyboards import resolve_cb_name
from filter_utils import (
    ADMIN_FILTER_KEY,
    empty_filter,
    get_available_filter_values,
    build_filter_keyboard,
    filter_active_text,
    is_filter_active,
)

filter_router = Router()


async def _rerender_filter_panel(callback: CallbackQuery, state: FSMContext):
    """Перерисовывает панель фильтра с текущим состоянием."""
    data      = await state.get_data()
    active    = data.get(ADMIN_FILTER_KEY, empty_filter())
    available = data.get("filter_available", {"shops": [], "cities": [], "networks": []})
    back_cb   = data.get("filter_back_cb", "reports")

    ft = filter_active_text(active)
    header = "🔍 <b>Фильтр по подразделению</b>"
    if ft:
        header += f"\n<i>Активен: {ft}</i>"
    header += "\n\nВыберите значения для фильтрации (множественный выбор):"

    kb = build_filter_keyboard(available, active, back_cb)
    await callback.message.edit_text(header, reply_markup=kb, parse_mode="HTML")


@filter_router.callback_query(F.data.startswith("flt_open_"))
async def filter_open(callback: CallbackQuery, state: FSMContext):
    """Открывает панель фильтра. callback_data: flt_open_{back_cb}"""
    await callback.answer()
    back_cb    = callback.data[len("flt_open_"):]
    current_db = await get_db(callback.from_user.id, state)
    scope_type, scope_values = get_user_org_scope(callback.from_user.id)
    available  = await asyncio.to_thread(get_available_filter_values, current_db, scope_type, scope_values)

    data   = await state.get_data()
    active = data.get(ADMIN_FILTER_KEY, empty_filter())

    await state.update_data(
        filter_back_cb=back_cb,
        filter_available=available,
    )

    ft = filter_active_text(active)
    header = "🔍 <b>Фильтр по подразделению</b>"
    if ft:
        header += f"\n<i>Активен: {ft}</i>"
    header += "\n\nВыберите значения для фильтрации (множественный выбор):"

    kb = build_filter_keyboard(available, active, back_cb)
    await callback.message.edit_text(header, reply_markup=kb, parse_mode="HTML")


@filter_router.callback_query(F.data.startswith("ftog_s_"))
async def filter_toggle_shop(callback: CallbackQuery, state: FSMContext):
    """Переключает магазин в фильтре."""
    await callback.answer()
    data      = await state.get_data()
    available = data.get("filter_available", {"shops": [], "cities": [], "networks": []})
    partial   = callback.data[len("ftog_s_"):]
    shop      = resolve_cb_name(partial, available.get("shops", []))

    active = dict(data.get(ADMIN_FILTER_KEY, empty_filter()))
    shops  = list(active.get("shops", []))
    if shop in shops:
        shops.remove(shop)
    else:
        shops.append(shop)
    active.update({"shops": shops, "cities": [], "networks": []})
    await state.update_data(**{ADMIN_FILTER_KEY: active})
    await _rerender_filter_panel(callback, state)


@filter_router.callback_query(F.data.startswith("ftog_c_"))
async def filter_toggle_city(callback: CallbackQuery, state: FSMContext):
    """Переключает город в фильтре."""
    await callback.answer()
    data      = await state.get_data()
    available = data.get("filter_available", {"shops": [], "cities": [], "networks": []})
    partial   = callback.data[len("ftog_c_"):]
    city      = resolve_cb_name(partial, available.get("cities", []))

    active  = dict(data.get(ADMIN_FILTER_KEY, empty_filter()))
    cities  = list(active.get("cities", []))
    if city in cities:
        cities.remove(city)
    else:
        cities.append(city)
    active.update({"shops": [], "cities": cities, "networks": []})
    await state.update_data(**{ADMIN_FILTER_KEY: active})
    await _rerender_filter_panel(callback, state)


@filter_router.callback_query(F.data.startswith("ftog_n_"))
async def filter_toggle_network(callback: CallbackQuery, state: FSMContext):
    """Переключает торговую сеть в фильтре."""
    await callback.answer()
    data      = await state.get_data()
    available = data.get("filter_available", {"shops": [], "cities": [], "networks": []})
    partial   = callback.data[len("ftog_n_"):]
    network   = resolve_cb_name(partial, available.get("networks", []))

    active   = dict(data.get(ADMIN_FILTER_KEY, empty_filter()))
    networks = list(active.get("networks", []))
    if network in networks:
        networks.remove(network)
    else:
        networks.append(network)
    active.update({"shops": [], "cities": [], "networks": networks})
    await state.update_data(**{ADMIN_FILTER_KEY: active})
    await _rerender_filter_panel(callback, state)


@filter_router.callback_query(F.data == "flt_reset")
async def filter_reset(callback: CallbackQuery, state: FSMContext):
    """Сбрасывает все фильтры и перерисовывает панель."""
    await callback.answer("🗑 Фильтр сброшен")
    await state.update_data(**{ADMIN_FILTER_KEY: empty_filter()})
    await _rerender_filter_panel(callback, state)
