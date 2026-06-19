"""
Обработчики для системы планов продаж
"""
import asyncio
import json as _json
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram.types import InlineKeyboardButton
from keyboards import InlineKeyboardBuilder, safe_cb, resolve_cb_name, back_button, home_button
from env_manager import env_manager
from utils import format_price, he
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit, safe_edit_message
from hints import hint_suffix
from states import SearchStates
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_BTN
from timezone_utils import get_current_user_time

sales_plans_router = Router()


class SalesPlanStates(StatesGroup):
    entering_target_value = State()
    selecting_products = State()
    selecting_categories = State()
    editing_target = State()


# ── Вспомогательные функции ───────────────────────────────────────────────────

_PERIOD_LABELS = {'weekly': 'Неделя', 'monthly': 'Месяц'}
_METRIC_LABELS = {'turnover': 'Оборот (₽)', 'quantity': 'Количество (шт)'}
_TARGET_LABELS = {'seller': 'Продавец', 'shop': 'Магазин'}
_FILTER_LABELS = {'all': 'Все товары', 'category': 'По категории', 'product': 'По товарам'}


def _progress_bar(percent: float, width: int = 8) -> str:
    percent = max(0.0, min(float(percent), 100.0))
    filled = round(percent / 100 * width)
    if percent < 30:
        fill_char = '🟥'
    elif percent < 50:
        fill_char = '🟧'
    elif percent < 70:
        fill_char = '🟨'
    else:
        fill_char = '🟩'
    return fill_char * filled + '⬜' * (width - filled)


def _plan_summary_line(plan, actual, percent) -> str:
    """Краткое описание плана с прогрессом для списков"""
    period = _PERIOD_LABELS.get(plan[1], plan[1])
    metric = _METRIC_LABELS.get(plan[2], plan[2])
    target = plan[3]
    target_type = plan[4]
    filter_type = plan[7]
    filter_val = plan[8]

    if target_type == 'seller':
        fn = plan[12] or ""
        ln = plan[13] or ""
        who = f"{fn} {ln}".strip() or f"id={plan[5]}"
    else:
        who = plan[6] or "Все"

    if filter_type == 'category':
        try:
            _cats = _json.loads(filter_val) if filter_val else []
            if isinstance(_cats, list) and _cats:
                scope = " · кат. «" + he(", ".join(_cats[:2])) + ("…" if len(_cats) > 2 else "") + "»"
            else:
                scope = f" · кат. «{he(str(filter_val))}»"
        except (ValueError, TypeError):
            scope = f" · кат. «{he(str(filter_val))}»"
    elif filter_type == 'product':
        scope = " · отд. товары"
    else:
        scope = ""

    if plan[2] == 'turnover':
        actual_str = f"{format_price(actual)}₽"
        target_str = f"{format_price(target)}₽"
    else:
        actual_str = f"{int(actual)} шт"
        target_str = f"{int(target)} шт"

    bar = _progress_bar(percent)
    return (
        f"📋 <b>{he(who)}</b> · {period} · {metric}{scope}\n"
        f"{bar} {percent}%\n"
        f"Факт: {actual_str} / Цель: {target_str}"
    )


def _plan_label_short(plan) -> str:
    """Короткий лейбл для кнопок (удаление)"""
    period = _PERIOD_LABELS.get(plan[1], plan[1])
    metric = _METRIC_LABELS.get(plan[2], plan[2])
    target_type = plan[4]
    if target_type == 'seller':
        fn = plan[12] or ""
        ln = plan[13] or ""
        who = f"{fn} {ln}".strip() or f"id={plan[5]}"
    else:
        who = plan[6] or "Все"
    return f"{who} · {period} · {metric}"


# ── Главное меню планов ───────────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data == "admin_sales_plans")
async def sales_plans_menu(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not is_any_admin(uid):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    if not env_manager.is_super_admin(uid):
        from subscription_utils import check_plans_motivation_permission
        if not check_plans_motivation_permission(uid):
            await callback.message.edit_text(
                "🔒 <b>Модуль «Планы и мотивация» не подключён</b>\n\n"
                "Создание планов продаж доступно при активном модуле <b>Планы и мотивация</b>.\n\n"
                "Подключите модуль в веб-кабинете:\n"
                "<b>Подписка → Модули → 📈 Планы и мотивация</b>",
                parse_mode="HTML"
            )
            await callback.answer()
            return

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать план", callback_data="plnwiz_start")
    builder.button(text="📊 Прогресс планов", callback_data="plans_progress")
    builder.button(text="✏️ Редактировать план", callback_data="editpln_start")
    builder.button(text="🗑 Удалить план", callback_data="delpln_start")
    builder.button(text="⬅️ Назад", callback_data="motivation_hub")
    builder.add(home_button())
    builder.adjust(1)

    _sp_db = await get_db(callback.from_user.id, state)
    _sp_user = await _sp_db.get_user(callback.from_user.id)
    _sp_hint = hint_suffix(_sp_db, _sp_user[0], 'first_plans') if _sp_user else ""

    await callback.message.edit_text(
        "📋 <b>Планы продаж</b>\n\n"
        "Задавайте цели по обороту или количеству — на неделю или месяц, "
        "для конкретного продавца или магазина, по всем или только по выбранным товарам.\n\n"
        f"Выполнение планов отображается в реальном времени.{_sp_hint}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Визард создания плана — Шаг 1: цель (продавец / магазин) ─────────────────

@sales_plans_router.callback_query(F.data == "plnwiz_start")
async def plnwiz_step1(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    await state.update_data(
        pln_target_type=None, pln_user_id=None, pln_user_name=None,
        pln_shop_name=None, pln_period=None, pln_metric=None,
        pln_filter_type=None, pln_filter_value=None, pln_filter_label=None,
        pln_products=[], anchor_msg_id=callback.message.message_id
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Конкретный продавец", callback_data="plntgt_seller")
    builder.button(text="🏪 Магазин", callback_data="plntgt_shop")
    builder.button(text="❌ Отмена", callback_data="admin_sales_plans")
    builder.adjust(1)

    await callback.message.edit_text(
        "📋 <b>Новый план — шаг 1/5</b>\n\n"
        "Для кого устанавливается план?",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Визард Шаг 2: выбор конкретного продавца или магазина ────────────────────

@sales_plans_router.callback_query(F.data.startswith("plntgt_"))
async def plnwiz_step2(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    target_type = callback.data[len("plntgt_"):]
    await state.update_data(pln_target_type=target_type)

    current_db = await get_db(callback.from_user.id, state)

    if target_type == 'seller':
        all_users = await current_db.get_all_users()
        sellers = [u for u in all_users
                   if not env_manager.is_super_admin(u[1])]
        if not sellers:
            await callback.answer("❌ Нет продавцов в системе", show_alert=True)
            return
        await state.update_data(anchor_msg_id=callback.message.message_id)
        await _show_plan_user_list(callback, sellers)
    else:
        shops = await current_db.get_all_shops()
        if not shops:
            await callback.answer("❌ Нет магазинов в системе", show_alert=True)
            return
        await state.update_data(anchor_msg_id=callback.message.message_id)
        await _show_plan_shop_list(callback, shops)
    await callback.answer()


async def _show_plan_shop_list(callback, shops, query=""):
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти магазин", callback_data="plnwiz_srch_shop_start")
    for shop in filtered:
        builder.button(text=f"🏪 {shop}", callback_data=safe_cb("plnshp_", shop))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="plnwiz_srch_shop_cancel")
    builder.button(text="⬅️ Назад", callback_data="plnwiz_start")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await callback.message.edit_text(
        f"📋 <b>Новый план — шаг 2/5</b>\n\nВыберите магазин:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


async def _show_plan_user_list(callback, sellers, query=""):
    filtered = sellers
    if query:
        q = query.lower()
        filtered = [u for u in sellers
                    if q in f"{u[2]} {u[3]}".lower() or q in (u[8] or "").lower()]
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти продавца", callback_data="plnwiz_srch_user_start")
    for u in filtered:
        uid, fname, lname = u[0], u[2], u[3]
        shop = u[8] or ""
        label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
        builder.button(text=label, callback_data=f"plnusr_{uid}")
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="plnwiz_srch_user_cancel")
    builder.button(text="⬅️ Назад", callback_data="plnwiz_start")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await callback.message.edit_text(
        f"📋 <b>Новый план — шаг 2/5</b>\n\nВыберите продавца:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_shop_start")
async def plnwiz_srch_shop_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.shop_plans)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="plnwiz_srch_shop_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск магазина</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_shop_cancel")
async def plnwiz_srch_shop_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    await _show_plan_shop_list(callback, shops)


@sales_plans_router.message(SearchStates.shop_plans)
async def plnwiz_srch_shop_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    shops = await current_db.get_all_shops()
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти магазин", callback_data="plnwiz_srch_shop_start")
    for shop in filtered:
        builder.button(text=f"🏪 {shop}", callback_data=safe_cb("plnshp_", shop))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="plnwiz_srch_shop_cancel")
    builder.button(text="⬅️ Назад", callback_data="plnwiz_start")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"📋 <b>Новый план — шаг 2/5</b>\n\nВыберите магазин:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_user_start")
async def plnwiz_srch_user_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.user_plans)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="plnwiz_srch_user_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск продавца</b>\n\nВведите имя или магазин:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_user_cancel")
async def plnwiz_srch_user_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    all_users = await current_db.get_all_users()
    sellers = [u for u in all_users if not env_manager.is_super_admin(u[1])]
    await _show_plan_user_list(callback, sellers)


@sales_plans_router.message(SearchStates.user_plans)
async def plnwiz_srch_user_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    all_users = await current_db.get_all_users()
    sellers = [u for u in all_users if not env_manager.is_super_admin(u[1])]
    q = query.lower()
    filtered = [u for u in sellers
                if q in f"{u[2]} {u[3]}".lower() or q in (u[8] or "").lower()] if query else sellers
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти продавца", callback_data="plnwiz_srch_user_start")
    for u in filtered:
        uid, fname, lname = u[0], u[2], u[3]
        shop = u[8] or ""
        label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
        builder.button(text=label, callback_data=f"plnusr_{uid}")
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="plnwiz_srch_user_cancel")
    builder.button(text="⬅️ Назад", callback_data="plnwiz_start")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"📋 <b>Новый план — шаг 2/5</b>\n\nВыберите продавца:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data.startswith("plnusr_"))
async def plnwiz_seller_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    user_id = int(callback.data[len("plnusr_"):])
    current_db = await get_db(callback.from_user.id, state)
    all_users = await current_db.get_all_users()
    row = next((u for u in all_users if u[0] == user_id), None)
    if not row:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    uname = f"{row[2]} {row[3]}"
    await state.update_data(pln_user_id=user_id, pln_user_name=uname)
    await _show_period_step(callback)


@sales_plans_router.callback_query(F.data.startswith("plnshp_"))
async def plnwiz_shop_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    raw = callback.data[len("plnshp_"):]
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    shop_name = resolve_cb_name(raw, shops)
    await state.update_data(pln_shop_name=shop_name)
    await _show_period_step(callback)


async def _show_period_step(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Неделя (пн–вс)", callback_data="plnper_weekly")
    builder.button(text="🗓 Месяц", callback_data="plnper_monthly")
    builder.button(text="⬅️ Назад", callback_data="plnwiz_start")
    builder.adjust(1)
    await callback.message.edit_text(
        "📋 <b>Новый план — шаг 3/5</b>\n\nВыберите период:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Визард Шаг 3: период ──────────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data.startswith("plnper_"))
async def plnwiz_period_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    period = callback.data[len("plnper_"):]
    await state.update_data(pln_period=period)

    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Оборот (сумма продаж, ₽)", callback_data="plnmet_turnover")
    builder.button(text="📦 Количество (штуки)", callback_data="plnmet_quantity")
    builder.button(text="⬅️ Назад", callback_data="plnwiz_start")
    builder.adjust(1)

    await callback.message.edit_text(
        "📋 <b>Новый план — шаг 4/5</b>\n\nВыберите метрику:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Визард Шаг 4: метрика ─────────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data.startswith("plnmet_"))
async def plnwiz_metric_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    metric = callback.data[len("plnmet_"):]
    await state.update_data(pln_metric=metric)

    current_db = await get_db(callback.from_user.id, state)
    categories, products = await asyncio.gather(
        current_db.get_all_categories(),
        current_db.get_all_products()
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Все товары", callback_data="plnflt_all")
    if categories:
        builder.button(text="📂 По категории", callback_data="plnflt_cat")
    if products:
        builder.button(text="📦 По конкретным товарам", callback_data="plnflt_prod")
    builder.button(text="⬅️ Назад", callback_data="plnwiz_start")
    builder.adjust(1)

    await callback.message.edit_text(
        "📋 <b>Новый план — шаг 5/5</b>\n\nФильтр по товарам:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Визард Шаг 5a: все товары — сразу к вводу цели ───────────────────────────

@sales_plans_router.callback_query(F.data == "plnflt_all")
async def plnwiz_filter_all(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    await state.update_data(pln_filter_type='all', pln_filter_value=None,
                             pln_filter_label='Все товары')
    await _show_target_input(callback, state)


# ── Визард Шаг 5b: по категориям (мультивыбор) ───────────────────────────────

@sales_plans_router.callback_query(F.data == "plnflt_cat")
async def plnwiz_filter_cat(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    await state.update_data(pln_categories=[], pln_filter_type='category')
    await state.set_state(SalesPlanStates.selecting_categories)
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
    await _render_category_selection(callback.message, categories, [])
    await callback.answer()


async def _render_category_selection(message: Message, categories: list, selected: list,
                                      back_cb: str = "plnwiz_start", query: str = ""):
    filtered = categories
    if query:
        q = query.lower()
        filtered = [c for c in categories if q in c.lower()]
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти категорию", callback_data="plnwiz_srch_cat_start")
    for cat in filtered:
        icon = "✅" if cat in selected else "◻️"
        builder.button(text=f"{icon} {cat}", callback_data=safe_cb("plncat_", cat))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="plnwiz_srch_cat_cancel")
    builder.button(text="💾 Подтвердить выбор", callback_data="plncatok")
    builder.button(text="⬅️ Назад", callback_data=back_cb)
    builder.adjust(1)
    sel_text = f"Выбрано: {len(selected)}" if selected else "Ничего не выбрано"
    suffix = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await message.edit_text(
        f"📋 <b>Выбор категорий</b>\n\n{sel_text}{suffix}\n\nОтметьте нужные категории:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_cat_start")
async def plnwiz_srch_cat_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.category_plans)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="plnwiz_srch_cat_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск категории</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_cat_cancel")
async def plnwiz_srch_cat_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    edit_id = data.get('editpln_id')
    back_cb = f"editpln_{edit_id}" if edit_id else "plnwiz_start"
    selected = list(data.get('pln_categories', []))
    await state.set_state(SalesPlanStates.selecting_categories)
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
    await _render_category_selection(callback.message, categories, selected, back_cb=back_cb)


@sales_plans_router.message(SearchStates.category_plans)
async def plnwiz_srch_cat_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    data = await state.get_data()
    edit_id = data.get('editpln_id')
    back_cb = f"editpln_{edit_id}" if edit_id else "plnwiz_start"
    selected = list(data.get('pln_categories', []))
    current_db = await get_db(message.from_user.id, state)
    categories = await current_db.get_all_categories()
    q = query.lower()
    filtered = [c for c in categories if q in c.lower()] if query else categories
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти категорию", callback_data="plnwiz_srch_cat_start")
    for cat in filtered:
        icon = "✅" if cat in selected else "◻️"
        builder.button(text=f"{icon} {cat}", callback_data=safe_cb("plncat_", cat))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="plnwiz_srch_cat_cancel")
    builder.button(text="💾 Подтвердить выбор", callback_data="plncatok")
    builder.button(text="⬅️ Назад", callback_data=back_cb)
    builder.adjust(1)
    sel_text = f"Выбрано: {len(selected)}" if selected else "Ничего не выбрано"
    suffix = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"📋 <b>Выбор категорий</b>\n\n{sel_text}{suffix}\n\nОтметьте нужные категории:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await state.set_state(SalesPlanStates.selecting_categories)


@sales_plans_router.callback_query(SalesPlanStates.selecting_categories, F.data.startswith("plncat_"))
async def plnwiz_toggle_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    raw = callback.data[len("plncat_"):]
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
    cat = resolve_cb_name(raw, categories)
    if not cat:
        return
    data = await state.get_data()
    selected = list(data.get('pln_categories', []))
    if cat in selected:
        selected.remove(cat)
    else:
        selected.append(cat)
    await state.update_data(pln_categories=selected)
    edit_id = data.get('editpln_id')
    back_cb = f"editpln_{edit_id}" if edit_id else "plnwiz_start"
    await _render_category_selection(callback.message, categories, selected, back_cb=back_cb)


@sales_plans_router.callback_query(SalesPlanStates.selecting_categories, F.data == "plncatok")
async def plnwiz_categories_confirmed(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get('pln_categories', [])
    if not selected:
        await callback.answer("⚠️ Выберите хотя бы одну категорию", show_alert=True)
        return
    edit_id = data.get('editpln_id')
    if edit_id:
        current_db = await get_db(callback.from_user.id, state)
        await current_db.update_sales_plan(
            edit_id,
            filter_type='category',
            filter_value=_json.dumps(selected, ensure_ascii=False)
        )
        await state.set_state(None)
        await state.update_data(pln_categories=[])
        await callback.answer("✅ Фильтр обновлён")
        callback.data = f"editpln_{edit_id}"
        await editpln_plan_selected(callback, state)
        return
    label = ", ".join(selected[:3]) + ("..." if len(selected) > 3 else "")
    await state.update_data(
        pln_filter_value=_json.dumps(selected, ensure_ascii=False),
        pln_filter_label=f"Категории: {label}"
    )
    await state.set_state(None)
    await _show_target_input(callback, state)


# ── Визард Шаг 5c: по конкретным товарам (мультивыбор) ───────────────────────

@sales_plans_router.callback_query(F.data == "plnflt_prod")
async def plnwiz_filter_prod(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    await state.update_data(pln_products=[], pln_filter_type='product')
    await state.set_state(SalesPlanStates.selecting_products)
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    await _render_product_selection(callback.message, products, [])
    await callback.answer()


async def _render_product_selection(message: Message, products, selected_ids: list,
                                     back_cb: str = "plnwiz_start", query: str = "", page: int = 0):
    filtered = [p for p in products if query.lower() in p[1].lower()] if query else list(products)
    page_items, has_prev, has_next, total_pages, page = paginate(filtered, page, PAGE_SIZE_BTN)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔍 Найти товар", callback_data="plnwiz_srch_prd_start"))
    for p in page_items:
        icon = "✅" if p[0] in selected_ids else "◻️"
        builder.row(InlineKeyboardButton(text=f"{icon} {p[1]}", callback_data=f"plnprd_{p[0]}"))
    nav = page_nav_row("plnprd_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    if query:
        builder.row(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="plnwiz_srch_prd_cancel"))
    builder.row(InlineKeyboardButton(text="💾 Подтвердить выбор", callback_data="plnprdok"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))

    sel_text = f"Выбрано: {len(selected_ids)} тов." if selected_ids else "Ничего не выбрано"
    pg_line  = f"\n<i>Стр. {page+1}/{total_pages} · всего: {len(filtered)}</i>" if total_pages > 1 else ""
    suffix   = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered
                else f"\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await message.edit_text(
        f"📋 <b>Выбор товаров</b>{pg_line}\n\n{sel_text}{suffix}\n\nОтметьте нужные товары:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_prd_start")
async def plnwiz_srch_prd_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.product_plans)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="plnwiz_srch_prd_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск товара</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@sales_plans_router.callback_query(F.data == "plnwiz_srch_prd_cancel")
async def plnwiz_srch_prd_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    edit_id = data.get('editpln_id')
    back_cb = f"editpln_{edit_id}" if edit_id else "plnwiz_start"
    selected = list(data.get('pln_products', []))
    await state.update_data(pln_query='', pln_page=0)
    await state.set_state(SalesPlanStates.selecting_products)
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    await _render_product_selection(callback.message, products, selected, back_cb=back_cb)


@sales_plans_router.message(SearchStates.product_plans)
async def plnwiz_srch_prd_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    data = await state.get_data()
    edit_id = data.get('editpln_id')
    back_cb = f"editpln_{edit_id}" if edit_id else "plnwiz_start"
    selected = list(data.get('pln_products', []))
    await state.update_data(pln_query=query, pln_page=0)
    current_db = await get_db(message.from_user.id, state)
    products = await current_db.get_all_products()
    await state.set_state(SalesPlanStates.selecting_products)
    anchor = data.get('anchor_msg_id')

    class _Proxy:
        def __init__(self, bot, chat_id, msg_id):
            self._bot = bot; self._chat = chat_id; self._mid = msg_id
        async def edit_text(self, text, reply_markup=None, parse_mode=None):
            await self._bot.edit_message_text(text, self._chat, self._mid,
                                              reply_markup=reply_markup, parse_mode=parse_mode)

    target = _Proxy(message.bot, message.chat.id, anchor) if anchor else message
    await _render_product_selection(target, products, selected, back_cb=back_cb, query=query)
    try:
        await message.delete()
    except Exception:
        pass


# Page navigation for product selection (must be before plnprd_ toggle handler)
@sales_plans_router.callback_query(SalesPlanStates.selecting_products, F.data.startswith("plnprd_pg_"))
async def plnwiz_products_page(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    page = int(callback.data.removeprefix("plnprd_pg_"))
    data = await state.get_data()
    selected = list(data.get('pln_products', []))
    query    = data.get('pln_query', '')
    edit_id  = data.get('editpln_id')
    back_cb  = f"editpln_{edit_id}" if edit_id else "plnwiz_start"
    await state.update_data(pln_page=page)
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    await _render_product_selection(callback.message, products, selected, back_cb=back_cb,
                                    query=query, page=page)


@sales_plans_router.callback_query(SalesPlanStates.selecting_products, F.data.startswith("plnprd_"))
async def plnwiz_toggle_product(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("plnprd_"):]
    if not raw.isdigit():   # guard against plnprd_pg_ leaking here
        await callback.answer()
        return
    prod_id = int(raw)
    data = await state.get_data()
    selected = list(data.get('pln_products', []))
    page     = data.get('pln_page', 0)
    query    = data.get('pln_query', '')

    if prod_id in selected:
        selected.remove(prod_id)
    else:
        selected.append(prod_id)

    await state.update_data(pln_products=selected)
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    edit_id = data.get('editpln_id')
    back_cb = f"editpln_{edit_id}" if edit_id else "plnwiz_start"
    await _render_product_selection(callback.message, products, selected, back_cb=back_cb,
                                    query=query, page=page)
    await callback.answer()


@sales_plans_router.callback_query(SalesPlanStates.selecting_products, F.data == "plnprdok")
async def plnwiz_products_confirmed(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    data = await state.get_data()
    selected = data.get('pln_products', [])
    if not selected:
        await callback.answer("⚠️ Выберите хотя бы один товар", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    edit_id = data.get('editpln_id')
    if edit_id:
        await current_db.update_sales_plan(
            edit_id,
            filter_type='product',
            filter_value=_json.dumps(selected)
        )
        await state.set_state(None)
        await state.update_data(pln_products=[])
        await callback.answer("✅ Фильтр обновлён")
        callback.data = f"editpln_{edit_id}"
        await editpln_plan_selected(callback, state)
        return

    names = [p[1] for p in products if p[0] in selected]
    label = ", ".join(names[:3]) + ("..." if len(names) > 3 else "")

    await state.update_data(
        pln_filter_value=_json.dumps(selected),
        pln_filter_label=f"Товары: {label}"
    )
    await state.set_state(None)
    await _show_target_input(callback, state)


# ── Ввод целевого значения ────────────────────────────────────────────────────

async def _show_target_input(callback: CallbackQuery, state: FSMContext, hint_text: str = ""):
    data = await state.get_data()
    metric = data.get('pln_metric', 'turnover')
    period = _PERIOD_LABELS.get(data.get('pln_period', 'monthly'), '?')
    filter_label = data.get('pln_filter_label', 'Все товары')
    target_type = data.get('pln_target_type', '?')

    if target_type == 'seller':
        who = data.get('pln_user_name', '?')
    else:
        who = data.get('pln_shop_name', '?')

    if metric == 'turnover':
        unit = "₽ (рублей)"
        example = "500000"
    else:
        unit = "шт (штук)"
        example = "100"

    await state.set_state(SalesPlanStates.entering_target_value)

    builder = InlineKeyboardBuilder()
    _ai_ok = False
    try:
        from billing_utils import has_module as _hm
        _ai_ok = _hm(callback.from_user.id, "ai_assistant")
    except Exception:
        pass
    if _ai_ok:
        builder.button(text="🤖 AI-подсказка цели", callback_data="plnai_hint")
    builder.button(text="❌ Отмена", callback_data="admin_sales_plans")
    builder.adjust(1)

    hint_block = ""
    if hint_text:
        hint_block = f"\n\n💡 <b>AI-подсказка:</b>\n{he(hint_text)}"

    await callback.message.edit_text(
        f"📋 <b>Новый план — введите цель</b>\n\n"
        f"👤/🏪 Кому: <b>{he(who)}</b>\n"
        f"📅 Период: {period}\n"
        f"📊 Метрика: {_METRIC_LABELS.get(metric, metric)}\n"
        f"🔍 Фильтр: {filter_label}\n\n"
        f"Введите целевое значение в {unit}:\n"
        f"Пример: <code>{example}</code>"
        f"{hint_block}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data == "plnai_hint")
async def plnai_hint_callback(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    await callback.answer("🤖 Анализирую историю продаж…")

    try:
        from billing_utils import has_module as _hm
        if not _hm(callback.from_user.id, "ai_assistant"):
            await callback.message.edit_text(
                "🔒 <b>AI-помощник не подключён</b>\n\nПодключите модуль <b>AI-помощник</b> в веб-кабинете: Подписка → Модули.",
                parse_mode="HTML"
            )
            return
    except Exception:
        pass

    try:
        from web.ai_utils import ask_llm, is_configured, build_plan_target_hint_prompt
        if not is_configured():
            await _show_target_input(callback, state)
            return
    except Exception:
        await _show_target_input(callback, state)
        return

    try:
        from web.rate_store import check_and_increment_ai, get_ai_rate_limits
        base_limit, _ = get_ai_rate_limits()
        try:
            from billing_utils import has_extension as _hex
            from web.rate_store import get_custom_ai_limit
            custom = get_custom_ai_limit(callback.from_user.id)
            limit = custom if custom is not None else (
                base_limit * 3 if _hex(callback.from_user.id, "ai_high_limit") else base_limit
            )
        except Exception:
            limit = base_limit
        if not check_and_increment_ai(callback.from_user.id, limit):
            await _show_target_input(callback, state, hint_text="⚠️ Превышен дневной лимит AI-запросов.")
            return
    except Exception:
        pass

    try:
        data = await state.get_data()
        target_type = data.get('pln_target_type', 'shop')
        seller_id = data.get('pln_user_id')
        shop_name = data.get('pln_shop_name')
        plan_type = data.get('pln_period', 'monthly')
        metric_type = data.get('pln_metric', 'turnover')
        who = data.get('pln_user_name') if target_type == 'seller' else shop_name

        current_db = await get_db(callback.from_user.id, state)

        # Query historical data synchronously from the thread pool
        import asyncio
        history = []
        try:
            _MONTH_RU = {
                "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель",
                "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август",
                "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь",
            }
            metric_expr = (
                "COALESCE(SUM(s.sale_price * s.quantity_sold), 0)"
                if metric_type == "turnover"
                else "COALESCE(SUM(s.quantity_sold), 0)"
            )
            if plan_type == "monthly":
                period_fmt = "strftime('%Y-%m', s.sale_date)"
                lookback = "-4 months"
                now_period = __import__('datetime').datetime.utcnow().strftime("%Y-%m")
                def _fmt(raw):
                    try:
                        y, m = raw.split("-")
                        return f"{_MONTH_RU.get(m, m)} {y}"
                    except Exception:
                        return raw
            else:
                period_fmt = "strftime('%Y-%W', s.sale_date)"
                lookback = "-8 weeks"
                now_period = __import__('datetime').datetime.utcnow().strftime("%Y-%W")
                def _fmt(raw):
                    return f"Неделя {raw}"

            if target_type == "seller" and seller_id:
                where_extra = "AND s.user_id = ?"
                params = [lookback, int(seller_id), 4]
            elif target_type == "shop" and shop_name:
                where_extra = "AND s.shop_name = ?"
                params = [lookback, shop_name, 4]
            else:
                params = None

            if params:
                def _query():
                    conn = current_db.get_connection()
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            f"""SELECT {period_fmt} AS period, {metric_expr} AS value
                                FROM sales s
                                WHERE date(s.sale_date) >= date('now', ?)
                                  {where_extra}
                                GROUP BY period ORDER BY period DESC LIMIT ?""",
                            params
                        )
                        rows = cur.fetchall()
                        return rows
                    finally:
                        conn.close()
                rows = await asyncio.get_event_loop().run_in_executor(None, _query)
                for r in rows:
                    period_raw = r[0] or ""
                    if period_raw == now_period:
                        continue
                    history.append({"period": _fmt(period_raw), "value": float(r[1] or 0)})
                history = history[:4]
        except Exception as _he:
            pass

        avg = sum(h["value"] for h in history) / len(history) if history else 0.0
        suggestion = round(avg * 1.1)
        if metric_type == "turnover" and suggestion > 1000:
            suggestion = round(suggestion / 1000) * 1000

        prompt = build_plan_target_hint_prompt(
            target_type=target_type,
            who=who or "?",
            plan_type=plan_type,
            metric_type=metric_type,
            history=history,
            suggestion=float(suggestion),
        )
        _HINT_SYSTEM = (
            "Ты — аналитик продаж розничного магазина. "
            "Используй только предоставленные исторические данные. "
            "Давай конкретный числовой диапазон цели. "
            "Пиши по-русски. Без markdown. Без заголовков. Ровно 2 предложения."
        )
        hint_result = await ask_llm(prompt, system=_HINT_SYSTEM, max_tokens=200, temperature=0.1, feature="planhint")

        if not hint_result and avg > 0:
            unit_str = "₽" if metric_type == "turnover" else "шт"
            hint_result = (
                f"Средний показатель за последние периоды — {avg:,.0f} {unit_str}. "
                f"Рекомендуется поставить цель {suggestion:,.0f} {unit_str} (+10% к среднему)."
            )

        await _show_target_input(callback, state, hint_text=hint_result or "")
    except Exception as exc:
        import logging
        logging.error(f"plnai_hint_callback error: {exc}")
        await _show_target_input(callback, state)


@sales_plans_router.message(SalesPlanStates.entering_target_value)
async def plnwiz_target_entered(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="admin_sales_plans"
    ).as_markup()

    try:
        value = float(message.text.replace(',', '.').replace(' ', ''))
        if value <= 0:
            await fsm_edit(state, message,
                           "❌ <b>Значение должно быть больше 0</b>",
                           reply_markup=cancel_kb)
            return
    except ValueError:
        await fsm_edit(state, message,
                       "❌ <b>Неверный формат</b>\n\nВведите число (например: <code>500000</code>)",
                       reply_markup=cancel_kb)
        return

    data = await state.get_data()
    current_db = await get_db(message.from_user.id, state)

    plan_id = await current_db.add_sales_plan(
        plan_type=data.get('pln_period', 'monthly'),
        metric_type=data.get('pln_metric', 'turnover'),
        target_value=value,
        target_type=data.get('pln_target_type', 'shop'),
        user_id=data.get('pln_user_id'),
        shop_name=data.get('pln_shop_name'),
        filter_type=data.get('pln_filter_type', 'all'),
        filter_value=data.get('pln_filter_value'),
        created_by=data.get('pln_user_id') or 0
    )

    metric = data.get('pln_metric', 'turnover')
    period = _PERIOD_LABELS.get(data.get('pln_period', 'monthly'), '?')
    filter_label = data.get('pln_filter_label', 'Все товары')
    target_type = data.get('pln_target_type', 'shop')
    who = data.get('pln_user_name') if target_type == 'seller' else data.get('pln_shop_name', '?')

    if metric == 'turnover':
        value_str = f"{format_price(value)}₽"
    else:
        value_str = f"{int(value)} шт"

    if plan_id:
        _gs_sfx = ""
        try:
            from integration.manager import integration_manager as _int_mgr
            _gs_sfx = await _int_mgr.try_export_line(current_db, 'plans', {
                'type': target_type, 'metric': metric, 'target': str(value),
                'period': data.get('pln_period', ''),
                'shop_name': data.get('pln_shop_name', ''),
                'seller_name': data.get('pln_user_name', ''),
            })
        except Exception:
            pass

        await fsm_edit(
            state, message,
            f"✅ <b>План создан!</b>\n\n"
            f"👤/🏪 Кому: <b>{he(who)}</b>\n"
            f"📅 Период: {period}\n"
            f"📊 Метрика: {_METRIC_LABELS.get(metric, metric)}\n"
            f"🔍 Фильтр: {filter_label}\n"
            f"🎯 Цель: <b>{value_str}</b>{_gs_sfx}",
            reply_markup=InlineKeyboardBuilder().button(
                text="📊 Прогресс планов", callback_data="plans_progress"
            ).button(
                text="⬅️ К планам", callback_data="admin_sales_plans"
            ).adjust(1).as_markup()
        )
    else:
        await fsm_edit(state, message, "❌ Ошибка при сохранении плана",
                       reply_markup=cancel_kb)
    await clear_state_keep_org(state)


# ── Редактирование: получатель ────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data.regexp(r'^epwho_\d+$'))
async def editpln_who_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    plan_id = int(callback.data[len("epwho_"):])
    await state.update_data(editpln_id=plan_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Конкретный продавец", callback_data="epwhotgt_seller")
    builder.button(text="🏪 Магазин", callback_data="epwhotgt_shop")
    builder.button(text="⬅️ Назад", callback_data=f"editpln_{plan_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        "✏️ <b>Изменить получателя</b>\n\nДля кого устанавливается план?",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data.in_({"epwhotgt_seller", "epwhotgt_shop"}))
async def editpln_who_target(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    plan_id = data.get('editpln_id')
    target_type = "seller" if callback.data == "epwhotgt_seller" else "shop"
    current_db = await get_db(callback.from_user.id, state)
    builder = InlineKeyboardBuilder()
    if target_type == 'seller':
        all_users = await current_db.get_all_users()
        sellers = [u for u in all_users if not env_manager.is_super_admin(u[1])]
        if not sellers:
            await callback.answer("❌ Нет продавцов в системе", show_alert=True)
            return
        for u in sellers:
            uid, fname, lname = u[0], u[2], u[3]
            shop = u[8] or ""
            label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
            builder.button(text=label, callback_data=f"epwhousr_{uid}")
        prompt = "✏️ <b>Выберите продавца:</b>"
    else:
        shops = await current_db.get_all_shops()
        if not shops:
            await callback.answer("❌ Нет магазинов в системе", show_alert=True)
            return
        for shop in shops:
            builder.button(text=f"🏪 {shop}", callback_data=safe_cb("epwhoshp_", shop))
        prompt = "✏️ <b>Выберите магазин:</b>"
    builder.button(text="⬅️ Назад", callback_data=f"epwho_{plan_id}")
    builder.adjust(1)
    await callback.message.edit_text(prompt, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@sales_plans_router.callback_query(F.data.regexp(r'^epwhousr_\d+$'))
async def editpln_who_user_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    user_id = int(callback.data[len("epwhousr_"):])
    data = await state.get_data()
    plan_id = data.get('editpln_id')
    current_db = await get_db(callback.from_user.id, state)
    await current_db.update_sales_plan(plan_id, target_type='seller', user_id=user_id, shop_name=None)
    await callback.answer("✅ Получатель обновлён")
    callback.data = f"editpln_{plan_id}"
    await editpln_plan_selected(callback, state)


@sales_plans_router.callback_query(F.data.startswith("epwhoshp_"))
async def editpln_who_shop_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    raw = callback.data[len("epwhoshp_"):]
    data = await state.get_data()
    plan_id = data.get('editpln_id')
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    shop_name = resolve_cb_name(raw, shops)
    await current_db.update_sales_plan(plan_id, target_type='shop', user_id=None, shop_name=shop_name)
    await callback.answer("✅ Получатель обновлён")
    callback.data = f"editpln_{plan_id}"
    await editpln_plan_selected(callback, state)


# ── Редактирование: период ────────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data.regexp(r'^epperiod_\d+$'))
async def editpln_period_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    plan_id = int(callback.data[len("epperiod_"):])
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Неделя (пн–вс)", callback_data=f"epperset_w_{plan_id}")
    builder.button(text="🗓 Месяц", callback_data=f"epperset_m_{plan_id}")
    builder.button(text="⬅️ Назад", callback_data=f"editpln_{plan_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        "📅 <b>Выберите новый период:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data.regexp(r'^epperset_(w|m)_\d+$'))
async def editpln_period_set(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split('_')
    period_val = 'weekly' if parts[1] == 'w' else 'monthly'
    plan_id = int(parts[2])
    current_db = await get_db(callback.from_user.id, state)
    await current_db.update_sales_plan(plan_id, plan_type=period_val)
    await callback.answer("✅ Период обновлён")
    callback.data = f"editpln_{plan_id}"
    await editpln_plan_selected(callback, state)


# ── Редактирование: метрика ───────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data.regexp(r'^epmetric_\d+$'))
async def editpln_metric_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    plan_id = int(callback.data[len("epmetric_"):])
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Оборот (₽)", callback_data=f"epmset_t_{plan_id}")
    builder.button(text="📦 Количество (шт)", callback_data=f"epmset_q_{plan_id}")
    builder.button(text="⬅️ Назад", callback_data=f"editpln_{plan_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        "📊 <b>Выберите новую метрику:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data.regexp(r'^epmset_(t|q)_\d+$'))
async def editpln_metric_set(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split('_')
    metric_val = 'turnover' if parts[1] == 't' else 'quantity'
    plan_id = int(parts[2])
    current_db = await get_db(callback.from_user.id, state)
    await current_db.update_sales_plan(plan_id, metric_type=metric_val)
    await callback.answer("✅ Метрика обновлена")
    callback.data = f"editpln_{plan_id}"
    await editpln_plan_selected(callback, state)


# ── Редактирование: фильтр ────────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data.regexp(r'^epfilter_\d+$'))
async def editpln_filter_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    plan_id = int(callback.data[len("epfilter_"):])
    await state.update_data(editpln_id=plan_id)
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
    products = await current_db.get_all_products()
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Все товары", callback_data="epflt_all")
    if categories:
        builder.button(text="📂 По категории", callback_data="epflt_cat")
    if products:
        builder.button(text="📦 По конкретным товарам", callback_data="epflt_prod")
    builder.button(text="⬅️ Назад", callback_data=f"editpln_{plan_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        "🔍 <b>Выберите новый фильтр по товарам:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data == "epflt_all")
async def editpln_filter_all(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    plan_id = data.get('editpln_id')
    current_db = await get_db(callback.from_user.id, state)
    await current_db.update_sales_plan(plan_id, filter_type='all', filter_value=None)
    await callback.answer("✅ Фильтр обновлён")
    callback.data = f"editpln_{plan_id}"
    await editpln_plan_selected(callback, state)


@sales_plans_router.callback_query(F.data == "epflt_cat")
async def editpln_filter_cat(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    plan_id = data.get('editpln_id')
    current_db = await get_db(callback.from_user.id, state)
    plans = await current_db.get_sales_plans()
    plan = next((p for p in plans if p[0] == plan_id), None)
    pre_selected = []
    if plan and plan[7] == 'category' and plan[8]:
        try:
            pre_selected = _json.loads(plan[8])
        except (ValueError, TypeError):
            pre_selected = []
    await state.update_data(pln_categories=pre_selected)
    await state.set_state(SalesPlanStates.selecting_categories)
    categories = await current_db.get_all_categories()
    await _render_category_selection(
        callback.message, categories, pre_selected, back_cb=f"editpln_{plan_id}"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data == "epflt_prod")
async def editpln_filter_prod(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    plan_id = data.get('editpln_id')
    current_db = await get_db(callback.from_user.id, state)
    plans = await current_db.get_sales_plans()
    plan = next((p for p in plans if p[0] == plan_id), None)
    pre_selected = []
    if plan and plan[7] == 'product' and plan[8]:
        try:
            pre_selected = _json.loads(plan[8])
        except (ValueError, TypeError):
            pre_selected = []
    await state.update_data(pln_products=pre_selected)
    await state.set_state(SalesPlanStates.selecting_products)
    products = await current_db.get_all_products()
    await _render_product_selection(
        callback.message, products, pre_selected, back_cb=f"editpln_{plan_id}"
    )
    await callback.answer()


# ── Просмотр прогресса (admin) ────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data == "plans_progress")
async def show_plans_progress(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)

    # Применяем фильтр по магазину если установлен
    from filter_utils import ADMIN_FILTER_KEY, empty_filter, is_filter_active, filter_active_text, get_available_filter_values, has_anything_to_filter
    from db_utils import get_user_org_scope
    _data_p = await state.get_data()
    _af_p = _data_p.get(ADMIN_FILTER_KEY, empty_filter())
    _sc_p, _sv_p = get_user_org_scope(callback.from_user.id)

    _tz_sp = await current_db.get_user_timezone(callback.from_user.id)
    _now_sp = get_current_user_time(_tz_sp)
    plans_data_all = await current_db.get_plans_progress(_now_sp.date())

    # Фильтруем планы по магазину/городу/сети
    if _af_p.get("shops"):
        # Внутренние user_id пользователей в выбранных магазинах
        shop_user_ids = set()
        for sh in _af_p["shops"]:
            for u in await current_db.get_users_by_shop(sh):
                shop_user_ids.add(u[0])  # u[0] = users.id (internal)
        plans_data = [
            (p, a, pct) for p, a, pct in plans_data_all
            if (p[4] == 'shop' and p[6] in _af_p["shops"])
            or (p[4] == 'seller' and p[5] in shop_user_ids)
        ]
    elif _af_p.get("cities"):
        # Внутренние user_id пользователей в выбранных городах
        city_user_ids = set()
        for u in await current_db.get_users_by_city(_af_p["cities"][0]):
            city_user_ids.add(u[0])
        if len(_af_p["cities"]) > 1:
            for city in _af_p["cities"][1:]:
                for u in await current_db.get_users_by_city(city):
                    city_user_ids.add(u[0])
        plans_data = [
            (p, a, pct) for p, a, pct in plans_data_all
            if p[4] == 'seller' and p[5] in city_user_ids
        ]
    elif _af_p.get("networks"):
        # Для сети: фильтруем только seller-планы по пользователям сети
        all_u = await current_db.get_all_users(trade_networks=_af_p["networks"])
        net_user_ids = {u[0] for u in all_u}
        plans_data = [
            (p, a, pct) for p, a, pct in plans_data_all
            if p[4] == 'seller' and p[5] in net_user_ids
        ]
    else:
        plans_data = plans_data_all

    if not plans_data:
        builder_empty = InlineKeyboardBuilder()
        if plans_data_all and is_filter_active(_af_p):
            ft = filter_active_text(_af_p)
            empty_text = (
                f"📊 <b>Прогресс планов</b>\n\n"
                f"🔍 Фильтр: <b>{he(ft)}</b>\n\n"
                f"По выбранному фильтру активных планов нет.\n"
                f"Сбросьте фильтр, чтобы увидеть все планы."
            )
            builder_empty.button(text="🗑 Сбросить фильтр", callback_data="plnprog_reset_filter")
        else:
            empty_text = "📊 <b>Прогресс планов</b>\n\n❌ Нет активных планов"
            builder_empty.button(text="➕ Создать план", callback_data="plnwiz_start")
        builder_empty.button(text="⬅️ Назад", callback_data="admin_sales_plans")
        builder_empty.adjust(1)
        await callback.message.edit_text(
            empty_text,
            reply_markup=builder_empty.as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    text = "📊 <b>Прогресс планов продаж</b>\n\n"

    now = _now_sp
    weekly_start = (now - timedelta(days=now.weekday())).strftime('%d.%m')
    monthly_start = now.replace(day=1).strftime('%d.%m')

    weekly = [(p, a, pct) for p, a, pct in plans_data if p[1] == 'weekly']
    monthly = [(p, a, pct) for p, a, pct in plans_data if p[1] == 'monthly']

    if weekly:
        text += f"📅 <b>Неделя</b> (с {weekly_start}):\n\n"
        for plan, actual, percent in weekly:
            text += _plan_summary_line(plan, actual, percent) + "\n\n"

    if monthly:
        text += f"🗓 <b>Месяц</b> (с {monthly_start}):\n\n"
        for plan, actual, percent in monthly:
            text += _plan_summary_line(plan, actual, percent) + "\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="plans_progress")

    # Кнопка фильтра если доступен
    try:
        _avail_p = get_available_filter_values(current_db, _sc_p, _sv_p)
        if has_anything_to_filter(_avail_p):
            from filter_utils import filter_button_text
            builder.button(text=filter_button_text(_af_p), callback_data="flt_open_plans_progress")
    except Exception:
        pass

    builder.button(text="⬅️ Назад", callback_data="admin_sales_plans")
    builder.adjust(1)

    await safe_edit_message(callback, text, builder.as_markup())


# ── Сброс фильтра в прогрессе планов ─────────────────────────────────────────

@sales_plans_router.callback_query(F.data == "plnprog_reset_filter")
async def plnprog_reset_filter(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    from filter_utils import ADMIN_FILTER_KEY, empty_filter
    await state.update_data(**{ADMIN_FILTER_KEY: empty_filter()})
    await callback.answer("🗑 Фильтр сброшен")
    await show_plans_progress(callback, state)


# ── Удаление плана ────────────────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data == "delpln_start")
async def delpln_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    plans = await current_db.get_sales_plans()

    if not plans:
        await callback.message.edit_text(
            "🗑 <b>Удаление плана</b>\n\n❌ Нет активных планов",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_sales_plans"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for plan in plans:
        label = _plan_label_short(plan)
        builder.button(text=f"🗑 {label}", callback_data=f"delpln_{plan[0]}")
    builder.button(text="⬅️ Назад", callback_data="admin_sales_plans")
    builder.adjust(1)

    await callback.message.edit_text(
        "🗑 <b>Выберите план для удаления:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data.regexp(r'^delpln_\d+$'))
async def delpln_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    plan_id = int(callback.data[len("delpln_"):])
    current_db = await get_db(callback.from_user.id, state)
    plans = await current_db.get_sales_plans()
    plan = next((p for p in plans if p[0] == plan_id), None)

    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return

    label = _plan_label_short(plan)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"delpln_ok_{plan_id}")
    builder.button(text="❌ Отмена", callback_data="delpln_start")
    builder.adjust(1)

    await callback.message.edit_text(
        f"🗑 <b>Удаление плана</b>\n\n{label}\n\n⚠️ Это действие необратимо.",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data.startswith("delpln_ok_"))
async def delpln_execute(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    plan_id = int(callback.data[len("delpln_ok_"):])
    current_db = await get_db(callback.from_user.id, state)
    success = await current_db.delete_sales_plan(plan_id)

    if success:
        _gs_sfx = ""
        try:
            from integration.manager import integration_manager as _int_mgr
            _gs_sfx = await _int_mgr.try_export_line(current_db, 'plans', {
                'type': '', 'metric': '', 'target': '',
                'period': '', 'shop_name': '', 'seller_name': '',
            })
        except Exception:
            pass

        await callback.message.edit_text(
            f"✅ <b>План удалён</b>{_gs_sfx}",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ К планам", callback_data="admin_sales_plans"
            ).as_markup(), parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка при удалении",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_sales_plans"
            ).as_markup(), parse_mode="HTML"
        )
    await callback.answer()


# ── Мои планы (для всех пользователей) ───────────────────────────────────────

@sales_plans_router.callback_query(F.data == "my_plans")
async def my_plans(callback: CallbackQuery, state: FSMContext):
    await callback.answer("⏳ Загрузка...")
    current_db = await get_db(callback.from_user.id, state)
    _tz_mp = await current_db.get_user_timezone(callback.from_user.id)
    _now_mp = get_current_user_time(_tz_mp)
    plans_data = await current_db.get_user_plans_progress(callback.from_user.id, _now_mp.date())

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="my_plans")
    builder.button(text="⬅️ Назад", callback_data="reports")
    builder.adjust(1)

    if not plans_data:
        await callback.message.edit_text(
            "📋 <b>Мои планы продаж</b>\n\n"
            "Планы не назначены. Обратитесь к администратору.",
            reply_markup=builder.as_markup(), parse_mode="HTML"
        )
        return

    now = _now_mp
    weekly_start = (now - timedelta(days=now.weekday())).strftime('%d.%m')
    monthly_start = now.replace(day=1).strftime('%d.%m')

    weekly = [(p, a, pct) for p, a, pct in plans_data if p[1] == 'weekly']
    monthly = [(p, a, pct) for p, a, pct in plans_data if p[1] == 'monthly']

    text = "📋 <b>Мои планы продаж</b>\n\n"

    if weekly:
        text += f"📅 <b>Неделя</b> (с {weekly_start}):\n\n"
        for plan, actual, percent in weekly:
            metric = plan[2]
            target = plan[3]
            filter_type = plan[7]
            filter_val = plan[8]

            if filter_type == 'category':
                try:
                    _cats = _json.loads(filter_val) if filter_val else []
                    if isinstance(_cats, list) and _cats:
                        scope = "Категории: «" + he(", ".join(_cats[:2])) + ("…" if len(_cats) > 2 else "") + "»"
                    else:
                        scope = f"Категория: «{he(str(filter_val))}»"
                except (ValueError, TypeError):
                    scope = f"Категория: «{he(str(filter_val))}»"
            elif filter_type == 'product':
                scope = "Отдельные товары"
            else:
                scope = "Все товары"

            if metric == 'turnover':
                actual_str = f"{format_price(actual)}₽"
                target_str = f"{format_price(target)}₽"
            else:
                actual_str = f"{int(actual)} шт"
                target_str = f"{int(target)} шт"

            bar = _progress_bar(percent)
            status = "🎉 Выполнен!" if percent >= 100 else ("⚡ Почти!" if percent >= 80 else "")
            text += (
                f"🔹 {scope}\n"
                f"{bar} {percent}% {status}\n"
                f"Факт: {actual_str} / Цель: {target_str}\n\n"
            )

    if monthly:
        text += f"🗓 <b>Месяц</b> (с {monthly_start}):\n\n"
        for plan, actual, percent in monthly:
            metric = plan[2]
            target = plan[3]
            filter_type = plan[7]
            filter_val = plan[8]

            if filter_type == 'category':
                try:
                    _cats = _json.loads(filter_val) if filter_val else []
                    if isinstance(_cats, list) and _cats:
                        scope = "Категории: «" + he(", ".join(_cats[:2])) + ("…" if len(_cats) > 2 else "") + "»"
                    else:
                        scope = f"Категория: «{he(str(filter_val))}»"
                except (ValueError, TypeError):
                    scope = f"Категория: «{he(str(filter_val))}»"
            elif filter_type == 'product':
                scope = "Отдельные товары"
            else:
                scope = "Все товары"

            if metric == 'turnover':
                actual_str = f"{format_price(actual)}₽"
                target_str = f"{format_price(target)}₽"
            else:
                actual_str = f"{int(actual)} шт"
                target_str = f"{int(target)} шт"

            bar = _progress_bar(percent)
            status = "🎉 Выполнен!" if percent >= 100 else ("⚡ Почти!" if percent >= 80 else "")
            text += (
                f"🔹 {scope}\n"
                f"{bar} {percent}% {status}\n"
                f"Факт: {actual_str} / Цель: {target_str}\n\n"
            )

    await safe_edit_message(callback, text, builder.as_markup())


# ── Редактирование плана ──────────────────────────────────────────────────────

@sales_plans_router.callback_query(F.data == "editpln_start")
async def editpln_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    plans = await current_db.get_sales_plans()

    if not plans:
        await callback.message.edit_text(
            "✏️ <b>Редактирование плана</b>\n\n❌ Нет активных планов",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_sales_plans"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for plan in plans:
        label = _plan_label_short(plan)
        builder.button(text=f"✏️ {label}", callback_data=f"editpln_{plan[0]}")
    builder.button(text="⬅️ Назад", callback_data="admin_sales_plans")
    builder.adjust(1)

    await callback.message.edit_text(
        "✏️ <b>Выберите план для редактирования:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data.regexp(r'^editpln_\d+$'))
async def editpln_plan_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    plan_id = int(callback.data[len("editpln_"):])
    current_db = await get_db(callback.from_user.id, state)
    plans = await current_db.get_sales_plans()
    plan = next((p for p in plans if p[0] == plan_id), None)

    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return

    period = _PERIOD_LABELS.get(plan[1], plan[1])
    metric = _METRIC_LABELS.get(plan[2], plan[2])
    target = plan[3]
    target_type = plan[4]
    filter_type = plan[7]
    filter_val = plan[8]

    if target_type == 'seller':
        fn = plan[12] or ""
        ln = plan[13] or ""
        who = f"{fn} {ln}".strip() or f"id={plan[5]}"
    else:
        who = plan[6] or "Все"

    if filter_type == 'category':
        try:
            cats = _json.loads(filter_val) if filter_val else []
            scope = ", ".join(cats) if isinstance(cats, list) and cats else str(filter_val)
        except (ValueError, TypeError):
            scope = str(filter_val)
    elif filter_type == 'product':
        scope = "Отдельные товары"
    else:
        scope = "Все товары"

    target_str = f"{format_price(target)}₽" if plan[2] == 'turnover' else f"{int(target)} шт"

    await state.update_data(editpln_id=plan_id, anchor_msg_id=callback.message.message_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Изменить получателя", callback_data=f"epwho_{plan_id}")
    builder.button(text="📅 Изменить период", callback_data=f"epperiod_{plan_id}")
    builder.button(text="📊 Изменить метрику", callback_data=f"epmetric_{plan_id}")
    builder.button(text="🔍 Изменить фильтр", callback_data=f"epfilter_{plan_id}")
    builder.button(text="🎯 Изменить цель", callback_data=f"editpln_target_{plan_id}")
    builder.button(text="⬅️ Назад", callback_data="editpln_start")
    builder.adjust(1)

    await callback.message.edit_text(
        f"✏️ <b>Редактирование плана</b>\n\n"
        f"👤/🏪 Кому: <b>{he(who)}</b>\n"
        f"📅 Период: {period}\n"
        f"📊 Метрика: {metric}\n"
        f"🔍 Фильтр: {he(scope)}\n"
        f"🎯 Цель: <b>{target_str}</b>\n\n"
        f"Что изменить?",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.callback_query(F.data.startswith("editpln_target_"))
async def editpln_field_target(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    plan_id = int(callback.data[len("editpln_target_"):])
    current_db = await get_db(callback.from_user.id, state)
    plans = await current_db.get_sales_plans()
    plan = next((p for p in plans if p[0] == plan_id), None)

    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return

    metric = plan[2]
    unit = "₽ (рублей)" if metric == 'turnover' else "шт (штук)"
    example = "500000" if metric == 'turnover' else "100"

    await state.update_data(editpln_id=plan_id, editpln_metric=metric)
    await state.set_state(SalesPlanStates.editing_target)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="editpln_start")

    await callback.message.edit_text(
        f"✏️ <b>Новое целевое значение</b>\n\n"
        f"Введите новое значение в {unit}:\n"
        f"Пример: <code>{example}</code>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@sales_plans_router.message(SalesPlanStates.editing_target)
async def editpln_target_entered(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="editpln_start"
    ).as_markup()

    try:
        value = float(message.text.replace(',', '.').replace(' ', ''))
        if value <= 0:
            await fsm_edit(state, message,
                           "❌ <b>Значение должно быть больше 0</b>",
                           reply_markup=cancel_kb)
            return
    except ValueError:
        await fsm_edit(state, message,
                       "❌ <b>Неверный формат</b>\n\nВведите число (например: <code>500000</code>)",
                       reply_markup=cancel_kb)
        return

    data = await state.get_data()
    plan_id = data.get('editpln_id')
    metric = data.get('editpln_metric', 'turnover')

    if not plan_id:
        await fsm_edit(state, message, "❌ Ошибка: план не найден", reply_markup=cancel_kb)
        return

    current_db = await get_db(message.from_user.id, state)
    ok = await current_db.update_sales_plan(plan_id, target_value=value)

    value_str = f"{format_price(value)}₽" if metric == 'turnover' else f"{int(value)} шт"

    if ok:
        _gs_sfx = ""
        try:
            from integration.manager import integration_manager as _int_mgr
            _gs_sfx = await _int_mgr.try_export_line(current_db, 'plans', {
                'type': '', 'metric': metric, 'target': str(value),
                'period': '', 'shop_name': '', 'seller_name': '',
            })
        except Exception:
            pass

        await fsm_edit(
            state, message,
            f"✅ <b>План обновлён!</b>\n\nНовая цель: <b>{value_str}</b>{_gs_sfx}",
            reply_markup=InlineKeyboardBuilder().button(
                text="📊 Прогресс планов", callback_data="plans_progress"
            ).button(
                text="⬅️ К планам", callback_data="admin_sales_plans"
            ).adjust(1).as_markup()
        )
    else:
        await fsm_edit(state, message, "❌ Ошибка при обновлении плана",
                       reply_markup=cancel_kb)
    await clear_state_keep_org(state)
