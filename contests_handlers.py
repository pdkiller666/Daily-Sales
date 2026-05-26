"""
Обработчики модуля «Конкурсы»
"""
import asyncio
import json as _json
import logging
from datetime import datetime, timedelta
import calendar as _calendar
from hints import hint_suffix, maybe_send_welcome
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from keyboards import InlineKeyboardBuilder, safe_cb, resolve_cb_name, back_button, home_button
from env_manager import env_manager
from utils import format_price, he
from db_utils import get_db, clear_state_keep_org, is_any_admin
from states import SearchStates
from message_utils import fsm_edit, safe_edit_message
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_DEFAULT
from notif_utils import add_read_btn

contests_router = Router()
logger = logging.getLogger(__name__)


async def _safe_tier_edit(callback, state: FSMContext, text: str, markup, parse_mode: str = "HTML") -> None:
    """Редактирует якорное сообщение — работает и с реальным CallbackQuery, и с FakeCallback."""
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except Exception:
        # FakeCallback: callback.message — сообщение пользователя, редактировать нельзя.
        # Ищем якорное сообщение бота в FSM state.
        data = await state.get_data()
        anchor_id = data.get("anchor_msg_id")
        if anchor_id:
            try:
                await callback.message.bot.edit_message_text(
                    chat_id=callback.message.chat.id,
                    message_id=anchor_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode=parse_mode,
                )
            except Exception as e:
                logger.error(f"_safe_tier_edit: не удалось отредактировать якорь #{anchor_id}: {e}")
    await callback.answer()

MONTH_NAMES_CAL = [
    'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
]


def _fmt_date(d: str) -> str:
    """YYYY-MM-DD → DD.MM.YYYY для показа пользователю."""
    try:
        return datetime.strptime(d, '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        return d


def _build_calendar_keyboard(year: int, month: int,
                              selected_start: str = None) -> InlineKeyboardMarkup:
    """Inline-клавиатура-календарь для выбора даты конкурса."""
    def btn(text: str, cb: str = 'ctcal_noop') -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    prev_m, prev_y = (month - 1, year) if month > 1 else (12, year - 1)
    next_m, next_y = (month + 1, year) if month < 12 else (1, year + 1)

    rows = [
        [
            btn('◀', f'ctcal_nav_{prev_y}_{prev_m:02d}'),
            btn(f'{MONTH_NAMES_CAL[month - 1]} {year}'),
            btn('▶', f'ctcal_nav_{next_y}_{next_m:02d}'),
        ],
        [btn(d) for d in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']],
    ]
    today_str = datetime.now().strftime('%Y-%m-%d')
    for week in _calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(btn('  '))
            else:
                date_str = f'{year}-{month:02d}-{day:02d}'
                label = str(day)
                if date_str == today_str:
                    label = f'·{day}·'
                if selected_start and date_str == selected_start:
                    label = f'✓{day}'
                row.append(btn(label, f'ctcal_day_{date_str}'))
        rows.append(row)
    rows.append([btn('❌ Отмена', 'contests_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class ContestStates(StatesGroup):
    entering_title = State()
    entering_description = State()
    entering_target_value = State()
    entering_reward_value = State()
    entering_start_date = State()
    entering_end_date = State()
    editing_contest_target = State()
    editing_contest_reward = State()
    # per_sale: ввод бонуса за единицу (текущий товар текущего тира)
    configuring_tier_bonus = State()
    # total: ввод индивидуального порога для конкретного магазина
    entering_individual_target = State()
    # ручной ввод результата магазина
    entering_manual_result = State()


# ── Главное меню конкурсов ─────────────────────────────────────────────────────

@contests_router.callback_query(F.data == "contests_menu")
async def contests_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать конкурс", callback_data="contest_create")
    builder.button(text="🏆 Активные конкурсы", callback_data="contest_list_active")
    builder.button(text="📋 Архив конкурсов", callback_data="contest_list_archive")
    builder.button(text="⬅️ Назад", callback_data="admin_management")
    builder.add(home_button())
    builder.adjust(1)

    _ct_db = await get_db(callback.from_user.id, state)
    _ct_user = await _ct_db.get_user(callback.from_user.id)
    _ct_hint = hint_suffix(_ct_db, _ct_user[0], 'first_contests') if _ct_user else ""

    await safe_edit_message(
        callback,
        "🏆 <b>Конкурсы</b>\n\n"
        "Создавайте мотивационные конкурсы для продавцов: по товарам, "
        "категориям, магазинам.\n\n"
        f"Победители получают фиксированную надбавку или % от оборота за период.{_ct_hint}",
        builder.as_markup()
    )


# ── Визард создания конкурса ───────────────────────────────────────────────────

@contests_router.callback_query(F.data == "contest_create")
async def contest_create_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    await state.update_data(
        ct_title=None, ct_description=None,
        ct_contest_type='any', ct_metric='turnover', ct_target=None,
        ct_reward_type='fixed', ct_reward=None,
        ct_start=None, ct_end=None,
        ct_shops=None, ct_users=None,
        ct_products=None, ct_categories=None,
        ct_notify=0,
        anchor_msg_id=callback.message.message_id,
        # Новые поля
        ct_reward_mode='total',        # 'total' или 'per_sale'
        ct_tiers=[],                   # тиры per_sale: [{min_plan_pct, bonuses:[{product_id,product_name,bonus_per_unit}]}]
        ct_tier_idx=0,                 # индекс текущего тира
        ct_tier_count=1,               # сколько тиров
        ct_product_idx=0,              # индекс текущего товара в тире
        ct_individual_targets={},      # {shop_name: target_value} для total-режима
        ct_ind_shops=[],               # список магазинов для индивидуальных порогов
        ct_ind_idx=0,                  # текущий магазин
    )
    await state.set_state(ContestStates.entering_title)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="contests_menu")

    await callback.message.edit_text(
        "🏆 <b>Новый конкурс — шаг 1/8</b>\n\n"
        "Введите название конкурса:\n"
        "<i>Например: «Июньский спринт», «Лучший продавец месяца»</i>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.message(ContestStates.entering_title)
async def contest_title_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    title = message.text.strip()[:100]
    if not title:
        await fsm_edit(
            state, message, "❌ Название не может быть пустым. Введите название:",
            reply_markup=InlineKeyboardBuilder().button(
                text="❌ Отмена", callback_data="contests_menu"
            ).as_markup()
        )
        return

    await state.update_data(ct_title=title)
    await state.set_state(ContestStates.entering_description)

    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Пропустить", callback_data="contest_skip_desc")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    await fsm_edit(
        state, message,
        f"🏆 <b>Новый конкурс — шаг 2/8</b>\n\n"
        f"Название: <b>{he(title)}</b>\n\n"
        "Введите описание условий конкурса (или пропустите):",
        reply_markup=builder.as_markup()
    )


@contests_router.callback_query(ContestStates.entering_description, F.data == "contest_skip_desc")
async def contest_skip_desc(callback: CallbackQuery, state: FSMContext):
    await state.update_data(ct_description=None)
    await state.set_state(None)
    await _show_scope_step(callback, state)


@contests_router.message(ContestStates.entering_description)
async def contest_desc_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    desc = message.text.strip()[:500]
    await state.update_data(ct_description=desc)
    await state.set_state(None)
    await fsm_edit(
        state, message,
        "🏆 <b>Новый конкурс — шаг 3/8</b>\n\nЧто считается для конкурса?",
        reply_markup=_scope_kb()
    )


def _scope_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📦 Конкретные товары", callback_data="ctscp_product")
    b.button(text="📂 По категории", callback_data="ctscp_category")
    b.button(text="🌐 Все товары", callback_data="ctscp_any")
    b.button(text="❌ Отмена", callback_data="contests_menu")
    b.adjust(1)
    return b.as_markup()


async def _show_scope_step(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🏆 <b>Новый конкурс — шаг 3/8</b>\n\nЧто считается для конкурса?",
        reply_markup=_scope_kb(), parse_mode="HTML"
    )
    await callback.answer()


async def _show_contest_shop_list(message, shops, selected, query=""):
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти магазин", callback_data="ct_srch_shop_start")
    for shop in filtered:
        icon = "✅" if shop in selected else "◻️"
        builder.button(text=f"{icon} {shop}", callback_data=safe_cb("ctshopchk_", shop))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="ct_srch_shop_cancel")
    builder.button(text="💾 Применить", callback_data="ctshop_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    cnt = len(selected)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await message.edit_text(
        f"🏪 <b>Выберите магазины для конкурса</b> (выбрано: {cnt}):{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


async def _show_contest_product_list(message, products, selected, query=""):
    filtered = products
    if query:
        q = query.lower()
        filtered = [p for p in products if q in p[1].lower()]
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти товар", callback_data="ct_srch_prd_start")
    for p in filtered:
        icon = "✅" if p[0] in selected else "◻️"
        builder.button(text=f"{icon} {p[1]}", callback_data=f"ctprd_{p[0]}")
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="ct_srch_prd_cancel")
    builder.button(text="💾 Далее", callback_data="ctprd_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    cnt = len(selected)
    suffix = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await message.edit_text(
        f"📦 <b>Выберите товары для конкурса</b> (выбрано: {cnt}):{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


async def _show_contest_category_list(message, categories, selected, query=""):
    filtered = categories
    if query:
        q = query.lower()
        filtered = [c for c in categories if q in c.lower()]
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти категорию", callback_data="ct_srch_cat_start")
    for c in filtered:
        icon = "✅" if c in selected else "◻️"
        builder.button(text=f"{icon} {c}", callback_data=safe_cb("ctcat_", c))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="ct_srch_cat_cancel")
    builder.button(text="💾 Далее", callback_data="ctcat_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    cnt = len(selected)
    suffix = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await message.edit_text(
        f"📂 <b>Выберите категории для конкурса</b> (выбрано: {cnt}):{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@contests_router.callback_query(F.data.startswith("ctscp_"))
async def contest_scope_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    scope = callback.data[len("ctscp_"):]
    await state.update_data(ct_contest_type=scope, ct_products=None, ct_categories=None)
    current_db = await get_db(callback.from_user.id, state)

    if scope == 'product':
        products = await current_db.get_all_products()
        if not products:
            await callback.answer("❌ Нет товаров в системе", show_alert=True)
            return
        await state.update_data(anchor_msg_id=callback.message.message_id)
        selected = (await state.get_data()).get('ct_products') or []
        await _show_contest_product_list(callback.message, products, selected)
        await callback.answer()

    elif scope == 'category':
        categories = await current_db.get_all_categories()
        if not categories:
            await callback.answer("❌ Нет категорий в системе", show_alert=True)
            return
        await state.update_data(anchor_msg_id=callback.message.message_id)
        selected = (await state.get_data()).get('ct_categories') or []
        await _show_contest_category_list(callback.message, categories, selected)
        await callback.answer()

    else:
        await _show_metric_step(callback, state)


@contests_router.callback_query(F.data == "ctprd_done")
async def contest_products_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('ct_products'):
        await callback.answer("⚠️ Выберите хотя бы один товар", show_alert=True)
        return
    await _show_metric_step(callback, state)


@contests_router.callback_query(F.data.startswith("ctprd_"))
async def contest_toggle_product(callback: CallbackQuery, state: FSMContext):
    try:
        prod_id = int(callback.data[len("ctprd_"):])
    except ValueError:
        await callback.answer()
        return

    data = await state.get_data()
    selected = list(data.get('ct_products') or [])
    if prod_id in selected:
        selected.remove(prod_id)
    else:
        selected.append(prod_id)
    await state.update_data(ct_products=selected)

    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    await _show_contest_product_list(callback.message, products, selected)
    await callback.answer()


@contests_router.callback_query(F.data == "ctcat_done")
async def contest_categories_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('ct_categories'):
        await callback.answer("⚠️ Выберите хотя бы одну категорию", show_alert=True)
        return
    await _show_metric_step(callback, state)


@contests_router.callback_query(F.data.startswith("ctcat_"))
async def contest_toggle_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    raw = callback.data[len("ctcat_"):]
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
    cat = resolve_cb_name(raw, categories)
    if not cat:
        return

    data = await state.get_data()
    selected = list(data.get('ct_categories') or [])
    if cat in selected:
        selected.remove(cat)
    else:
        selected.append(cat)
    await state.update_data(ct_categories=selected)

    await _show_contest_category_list(callback.message, categories, selected)


@contests_router.callback_query(F.data == "ct_srch_prd_start")
async def ct_srch_prd_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.product_contests)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="ct_srch_prd_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск товара</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@contests_router.callback_query(F.data == "ct_srch_prd_cancel")
async def ct_srch_prd_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    data = await state.get_data()
    selected = list(data.get('ct_products') or [])
    await _show_contest_product_list(callback.message, products, selected)


@contests_router.message(SearchStates.product_contests)
async def ct_srch_prd_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    products = await current_db.get_all_products()
    data = await state.get_data()
    selected = list(data.get('ct_products') or [])
    q = query.lower()
    filtered = [p for p in products if q in p[1].lower()] if query else products
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти товар", callback_data="ct_srch_prd_start")
    for p in filtered:
        icon = "✅" if p[0] in selected else "◻️"
        builder.button(text=f"{icon} {p[1]}", callback_data=f"ctprd_{p[0]}")
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="ct_srch_prd_cancel")
    builder.button(text="💾 Далее", callback_data="ctprd_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    cnt = len(selected)
    suffix = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"📦 <b>Выберите товары для конкурса</b> (выбрано: {cnt}):{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@contests_router.callback_query(F.data == "ct_srch_cat_start")
async def ct_srch_cat_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.category_contests)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="ct_srch_cat_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск категории</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@contests_router.callback_query(F.data == "ct_srch_cat_cancel")
async def ct_srch_cat_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
    data = await state.get_data()
    selected = list(data.get('ct_categories') or [])
    await _show_contest_category_list(callback.message, categories, selected)


@contests_router.message(SearchStates.category_contests)
async def ct_srch_cat_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    categories = await current_db.get_all_categories()
    data = await state.get_data()
    selected = list(data.get('ct_categories') or [])
    q = query.lower()
    filtered = [c for c in categories if q in c.lower()] if query else categories
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти категорию", callback_data="ct_srch_cat_start")
    for c in filtered:
        icon = "✅" if c in selected else "◻️"
        builder.button(text=f"{icon} {c}", callback_data=safe_cb("ctcat_", c))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="ct_srch_cat_cancel")
    builder.button(text="💾 Далее", callback_data="ctcat_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    cnt = len(selected)
    suffix = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"📂 <b>Выберите категории для конкурса</b> (выбрано: {cnt}):{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


async def _show_metric_step(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Оборот (сумма продаж, ₽)", callback_data="ctmet_turnover")
    builder.button(text="📦 Количество (штуки)", callback_data="ctmet_quantity")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "🏆 <b>Новый конкурс — шаг 4/8</b>\n\nЧто измеряется в конкурсе?",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ctmet_"))
async def contest_metric_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    metric = callback.data[len("ctmet_"):]
    await state.update_data(ct_metric=metric, anchor_msg_id=callback.message.message_id)
    await _show_reward_mode_step(callback, state)


async def _show_reward_mode_step(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    scope = data.get('ct_contest_type', 'any')

    builder = InlineKeyboardBuilder()
    builder.button(text="🏅 Итоговый приз победителям", callback_data="ctrm_total")
    builder.button(text="💵 Бонус за каждую продажу (с тирами по % плана)", callback_data="ctrm_per_sale")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    note = ""
    if scope != 'product':
        note = "\n\n<i>Режим «Бонус за продажу» доступен для любого охвата, но позволяет задать единый flat-бонус за шт.</i>"

    await callback.message.edit_text(
        f"🏆 <b>Новый конкурс — шаг 5</b>\n\n"
        f"<b>Тип вознаграждения:</b>\n\n"
        f"• <b>Итоговый приз</b> — победитель получает фиксированную сумму или % от оборота, если достиг порога.\n"
        f"• <b>Бонус за каждую продажу</b> — бонус начисляется за каждую проданную единицу; "
        f"размер зависит от % выполнения плана (тиры).{note}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ctrm_"))
async def contest_reward_mode_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mode = callback.data[len("ctrm_"):]
    await state.update_data(ct_reward_mode=mode, anchor_msg_id=callback.message.message_id)

    if mode == 'per_sale':
        await _show_tier_count_step(callback, state)
    else:
        # total — идём к вводу глобального порога
        await _show_target_step(callback, state)


async def _show_target_step(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    metric = data.get('ct_metric', 'turnover')
    unit = "₽ (минимальный оборот для победы)" if metric == 'turnover' else "шт (минимальное кол-во для победы)"
    example = "100000" if metric == 'turnover' else "50"

    await state.set_state(ContestStates.entering_target_value)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="contests_menu")

    await callback.message.edit_text(
        f"🏆 <b>Новый конкурс — шаг 6 (Итоговый приз)</b>\n\n"
        f"Введите глобальный порог победы ({unit}):\n"
        f"Пример: <code>{example}</code>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── per_sale: настройка тиров ─────────────────────────────────────────────────

async def _show_tier_count_step(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="1 тир (единый бонус)", callback_data="cttc_1")
    builder.button(text="2 тира (например: 100% / 130%+)", callback_data="cttc_2")
    builder.button(text="3 тира (например: 80% / 100% / 130%)", callback_data="cttc_3")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "🏆 <b>Бонус за продажу — шаг 6</b>\n\n"
        "<b>Количество тиров бонуса:</b>\n\n"
        "Тир определяет размер бонуса в зависимости от % выполнения плана продавца.\n"
        "Например: тир 1 = при 100% плана, тир 2 = при 130%+.",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("cttc_"))
async def contest_tier_count_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    count = int(callback.data[len("cttc_"):])
    await state.update_data(ct_tier_count=count, ct_tier_idx=0, ct_tiers=[])
    await _show_tier_pct_step(callback, state)


async def _show_tier_pct_step(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tier_idx = data.get('ct_tier_idx', 0)
    tier_count = data.get('ct_tier_count', 1)

    builder = InlineKeyboardBuilder()
    if tier_idx == 0:
        builder.button(text="0% — всегда (без привязки к плану)", callback_data="ctpct_0")
        builder.button(text="50%", callback_data="ctpct_50")
        builder.button(text="80%", callback_data="ctpct_80")
        builder.button(text="100%", callback_data="ctpct_100")
    else:
        builder.button(text="80%", callback_data="ctpct_80")
        builder.button(text="100%", callback_data="ctpct_100")
        builder.button(text="120%", callback_data="ctpct_120")
        builder.button(text="130%", callback_data="ctpct_130")
        builder.button(text="150%", callback_data="ctpct_150")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(2)

    await _safe_tier_edit(
        callback, state,
        f"🏆 <b>Тир {tier_idx + 1} из {tier_count}</b>\n\n"
        f"Минимальный % выполнения плана для этого тира:\n"
        f"<i>(если у продавца нет плана, применяется тир с 0%)</i>",
        builder.as_markup(),
    )


@contests_router.callback_query(F.data.startswith("ctpct_"))
async def contest_tier_pct_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    pct = float(callback.data[len("ctpct_"):])
    data = await state.get_data()
    tier_idx = data.get('ct_tier_idx', 0)
    tiers = list(data.get('ct_tiers') or [])

    # Начинаем новый тир
    tiers.append({'min_plan_pct': pct, 'bonuses': []})
    await state.update_data(ct_tiers=tiers, ct_product_idx=0)
    await _show_tier_bonus_step(callback, state, tiers)


async def _show_tier_bonus_step(callback: CallbackQuery, state: FSMContext, tiers: list):
    """Запрашивает бонус за единицу для текущего товара текущего тира."""
    data = await state.get_data()
    tier_idx = data.get('ct_tier_idx', 0)
    tier_count = data.get('ct_tier_count', 1)
    prod_idx = data.get('ct_product_idx', 0)
    scope = data.get('ct_contest_type', 'any')

    current_tier = tiers[tier_idx]
    pct = current_tier['min_plan_pct']

    if scope == 'product':
        products = data.get('ct_products') or []
        current_db = await get_db(callback.from_user.id, state)
        all_products = await current_db.get_all_products()
        prod_map = {p[0]: p[1] for p in all_products}

        if prod_idx >= len(products):
            # Все товары текущего тира заполнены → следующий тир или период
            await _advance_tier_or_finish(callback, state, tiers)
            return

        pid = products[prod_idx]
        pname = prod_map.get(pid, f"Товар #{pid}")
        await state.set_state(ContestStates.configuring_tier_bonus)

        filled = len(current_tier['bonuses'])
        total = len(products)
        progress = f"({filled}/{total} товаров)"

        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data="contests_menu")

        await _safe_tier_edit(
            callback, state,
            f"🏆 <b>Тир {tier_idx + 1}, %≥{pct:.0f}%</b> {progress}\n\n"
            f"Бонус за 1 шт. «<b>{he(pname)}</b>» (₽):\n"
            f"<i>Пример: 500</i>",
            builder.as_markup(),
        )
    else:
        # any/category — единый flat-бонус за шт
        await state.set_state(ContestStates.configuring_tier_bonus)
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data="contests_menu")
        await _safe_tier_edit(
            callback, state,
            f"🏆 <b>Тир {tier_idx + 1}/{tier_count}, %≥{pct:.0f}%</b>\n\n"
            f"Единый бонус за 1 проданную единицу (₽):\n"
            f"<i>Пример: 500</i>",
            builder.as_markup(),
        )


@contests_router.message(ContestStates.configuring_tier_bonus)
async def contest_tier_bonus_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass

    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="contests_menu"
    ).as_markup()

    raw = message.text.strip().replace(',', '.').replace(' ', '')
    try:
        bonus = float(raw)
        if bonus < 0:
            raise ValueError
    except ValueError:
        await fsm_edit(state, message,
                       "❌ Введите положительное число (бонус в ₽):",
                       reply_markup=cancel_kb)
        return

    data = await state.get_data()
    tier_idx = data.get('ct_tier_idx', 0)
    prod_idx = data.get('ct_product_idx', 0)
    scope = data.get('ct_contest_type', 'any')
    tiers = list(data.get('ct_tiers') or [])
    await state.set_state(None)

    class _FakeCallback:
        def __init__(self, msg): self.message = msg; self.from_user = msg.from_user
        async def answer(self): pass

    fc = _FakeCallback(message)

    if scope == 'product':
        products = data.get('ct_products') or []
        current_db = await get_db(message.from_user.id, state)
        all_products = await current_db.get_all_products()
        prod_map = {p[0]: p[1] for p in all_products}

        pid = products[prod_idx]
        pname = prod_map.get(pid, f"Товар #{pid}")
        tiers[tier_idx]['bonuses'].append({
            'product_id': pid,
            'product_name': pname,
            'bonus_per_unit': bonus,
        })
        next_prod_idx = prod_idx + 1
        await state.update_data(ct_tiers=tiers, ct_product_idx=next_prod_idx)

        if next_prod_idx >= len(products):
            # Все товары тира заполнены
            await _advance_tier_or_finish(fc, state, tiers)
        else:
            # Следующий товар того же тира
            await _show_tier_bonus_step(fc, state, tiers)
    else:
        # flat-бонус
        tiers[tier_idx]['bonuses'].append({
            'product_id': None,
            'product_name': None,
            'bonus_per_unit': bonus,
        })
        await state.update_data(ct_tiers=tiers)
        await _advance_tier_or_finish(fc, state, tiers)


async def _advance_tier_or_finish(callback, state: FSMContext, tiers: list):
    """После заполнения тира: переходим к следующему или к шагу периода."""
    data = await state.get_data()
    tier_idx = data.get('ct_tier_idx', 0)
    tier_count = data.get('ct_tier_count', 1)
    next_idx = tier_idx + 1

    if next_idx < tier_count:
        await state.update_data(ct_tier_idx=next_idx, ct_product_idx=0)
        await _show_tier_pct_step(callback, state)
    else:
        # Все тиры заполнены → к периоду
        await _show_period_step(callback, state)


@contests_router.message(ContestStates.entering_target_value)
async def contest_target_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="contests_menu"
    ).as_markup()
    try:
        value = float(message.text.replace(',', '.').replace(' ', ''))
        if value <= 0:
            await fsm_edit(state, message, "❌ Значение должно быть больше 0", reply_markup=cancel_kb)
            return
    except ValueError:
        await fsm_edit(state, message,
                       "❌ Введите число, например: <code>50000</code>", reply_markup=cancel_kb)
        return

    await state.update_data(ct_target=value)
    await state.set_state(None)

    # Предлагаем индивидуальные пороги по магазинам
    builder = InlineKeyboardBuilder()
    builder.button(text="🏪 Да, задать индивидуальные пороги по магазинам", callback_data="ctind_yes")
    builder.button(text="⏭ Нет, единый порог для всех", callback_data="ctind_no")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    metric = (await state.get_data()).get('ct_metric', 'turnover')
    unit = '₽' if metric == 'turnover' else ' шт'

    await fsm_edit(
        state, message,
        f"✅ Глобальный порог: <b>{format_price(value)}{unit}</b>\n\n"
        f"🏆 <b>Индивидуальные пороги по магазинам:</b>\n"
        f"Хотите задать разные пороги для каждого магазина?\n"
        f"<i>(например: Магазин А → 100 000₽, Магазин Б → 80 000₽)</i>",
        reply_markup=builder.as_markup()
    )


@contests_router.callback_query(F.data == "ctind_no")
async def contest_individual_targets_skip(callback: CallbackQuery, state: FSMContext):
    await state.update_data(ct_individual_targets={})
    await _show_reward_type_step(callback, state)


@contests_router.callback_query(F.data == "ctind_yes")
async def contest_individual_targets_start(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    ct_shops = data.get('ct_shops') or []
    if ct_shops:
        shops = ct_shops
    else:
        shops = await current_db.get_all_shops() or []
    if not shops:
        await callback.answer("❌ Нет магазинов в системе", show_alert=True)
        return
    await state.update_data(ct_ind_shops=shops, ct_ind_idx=0, ct_individual_targets={})
    await _show_individual_target_prompt(callback, state)


async def _show_individual_target_prompt(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    shops = data.get('ct_ind_shops', [])
    idx = data.get('ct_ind_idx', 0)
    done = data.get('ct_individual_targets', {})
    metric = data.get('ct_metric', 'turnover')
    global_target = data.get('ct_target', 0)
    unit = '₽' if metric == 'turnover' else ' шт'

    if idx >= len(shops):
        await _show_reward_type_step(callback, state)
        return

    shop = shops[idx]
    progress = f"({idx + 1}/{len(shops)})"
    await state.set_state(ContestStates.entering_individual_target)

    builder = InlineKeyboardBuilder()
    builder.button(text=f"⏭ Использовать глобальный ({format_price(global_target)}{unit})", callback_data="ctindval_skip")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    done_lines = "\n".join(f"  • {he(s)}: {format_price(v)}{unit}" for s, v in done.items())
    done_block = f"\n\n<b>Уже задано:</b>\n{done_lines}" if done_lines else ""

    await callback.message.edit_text(
        f"🏪 <b>Индивидуальный порог {progress}</b>\n\n"
        f"Магазин: <b>{he(shop)}</b>\n"
        f"Введите порог ({unit}) или пропустите:{done_block}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data == "ctindval_skip")
async def contest_individual_target_skip_shop(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    data = await state.get_data()
    idx = data.get('ct_ind_idx', 0)
    await state.update_data(ct_ind_idx=idx + 1)
    await _show_individual_target_prompt(callback, state)


@contests_router.message(ContestStates.entering_individual_target)
async def contest_individual_target_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="contests_menu"
    ).as_markup()
    raw = message.text.strip().replace(',', '.').replace(' ', '')
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        await fsm_edit(state, message, "❌ Введите положительное число:", reply_markup=cancel_kb)
        return

    data = await state.get_data()
    shops = data.get('ct_ind_shops', [])
    idx = data.get('ct_ind_idx', 0)
    done = dict(data.get('ct_individual_targets') or {})

    if idx < len(shops):
        done[shops[idx]] = value

    await state.update_data(ct_individual_targets=done, ct_ind_idx=idx + 1)
    await state.set_state(None)

    class _FakeCallback:
        def __init__(self, msg): self.message = msg; self.from_user = msg.from_user
        async def answer(self): pass
    await _show_individual_target_prompt(_FakeCallback(message), state)


async def _show_reward_type_step(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Фиксированная надбавка (₽)", callback_data="ctrwd_fixed")
    builder.button(text="📊 Процент от оборота (%)", callback_data="ctrwd_percent")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "🏆 <b>Новый конкурс — Тип награды победителям:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ctrwd_"))
async def contest_reward_type_selected(callback: CallbackQuery, state: FSMContext):
    reward_type = callback.data[len("ctrwd_"):]
    await state.update_data(ct_reward_type=reward_type, anchor_msg_id=callback.message.message_id)
    await state.set_state(ContestStates.entering_reward_value)

    if reward_type == 'fixed':
        prompt = "Введите фиксированную сумму надбавки в ₽:\nПример: <code>5000</code>"
    else:
        prompt = "Введите процент от оборота победителя (0.1–50):\nПример: <code>3</code>"

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="contests_menu")

    await callback.message.edit_text(
        f"🏆 <b>Новый конкурс — шаг 6/8 (продолжение)</b>\n\n{prompt}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.message(ContestStates.entering_reward_value)
async def contest_reward_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="contests_menu"
    ).as_markup()
    data = await state.get_data()
    try:
        value = float(message.text.replace(',', '.').replace(' ', ''))
        if value <= 0:
            await fsm_edit(state, message, "❌ Значение должно быть больше 0", reply_markup=cancel_kb)
            return
        if data.get('ct_reward_type') == 'percent' and value > 50:
            await fsm_edit(state, message, "❌ Процент должен быть от 0.1 до 50", reply_markup=cancel_kb)
            return
    except ValueError:
        await fsm_edit(state, message, "❌ Введите число", reply_markup=cancel_kb)
        return

    await state.update_data(ct_reward=value)
    await state.set_state(None)
    await _show_period_step_from_message(message, state)


async def _show_period_step_from_message(message: Message, state: FSMContext):
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    week_end = (now - timedelta(days=now.weekday()) + timedelta(days=6)).strftime('%Y-%m-%d')
    month_start = now.replace(day=1).strftime('%Y-%m-%d')
    month_end = now.replace(day=_calendar.monthrange(now.year, now.month)[1]).strftime('%Y-%m-%d')
    next30_start = now.strftime('%Y-%m-%d')
    next30_end = (now + timedelta(days=29)).strftime('%Y-%m-%d')

    builder = InlineKeyboardBuilder()
    builder.button(text=f"📅 Текущая неделя ({_fmt_date(week_start)} – {_fmt_date(week_end)})",
                   callback_data=f"ctper_{week_start}_{week_end}")
    builder.button(text=f"🗓 Текущий месяц ({_fmt_date(month_start)} – {_fmt_date(month_end)})",
                   callback_data=f"ctper_{month_start}_{month_end}")
    builder.button(text=f"📆 30 дней ({_fmt_date(next30_start)} – {_fmt_date(next30_end)})",
                   callback_data=f"ctper_{next30_start}_{next30_end}")
    builder.button(text="📅 Выбрать любые даты", callback_data="ctper_manual")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    await fsm_edit(
        state, message,
        "🏆 <b>Новый конкурс — шаг 7/8</b>\n\nВыберите период проведения конкурса:",
        reply_markup=builder.as_markup()
    )


async def _show_period_step(callback: CallbackQuery, state: FSMContext):
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    week_end = (now - timedelta(days=now.weekday()) + timedelta(days=6)).strftime('%Y-%m-%d')
    month_start = now.replace(day=1).strftime('%Y-%m-%d')
    month_end = now.replace(day=_calendar.monthrange(now.year, now.month)[1]).strftime('%Y-%m-%d')
    next30_start = now.strftime('%Y-%m-%d')
    next30_end = (now + timedelta(days=29)).strftime('%Y-%m-%d')

    builder = InlineKeyboardBuilder()
    builder.button(text=f"📅 Текущая неделя ({_fmt_date(week_start)} – {_fmt_date(week_end)})",
                   callback_data=f"ctper_{week_start}_{week_end}")
    builder.button(text=f"🗓 Текущий месяц ({_fmt_date(month_start)} – {_fmt_date(month_end)})",
                   callback_data=f"ctper_{month_start}_{month_end}")
    builder.button(text=f"📆 30 дней ({_fmt_date(next30_start)} – {_fmt_date(next30_end)})",
                   callback_data=f"ctper_{next30_start}_{next30_end}")
    builder.button(text="📅 Выбрать любые даты", callback_data="ctper_manual")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    await _safe_tier_edit(
        callback, state,
        "🏆 <b>Новый конкурс — шаг 7/8</b>\n\nВыберите период проведения конкурса:",
        builder.as_markup(),
    )


@contests_router.callback_query(F.data == "ctper_manual")
async def contest_period_calendar_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    now = datetime.now()
    await state.update_data(ct_cal_picking='start', ct_cal_year=now.year, ct_cal_month=now.month)
    kb = _build_calendar_keyboard(now.year, now.month)
    await callback.message.edit_text(
        "📅 <b>Шаг 7/8 — Дата начала конкурса</b>\n\nВыберите дату начала:",
        reply_markup=kb, parse_mode="HTML"
    )


@contests_router.callback_query(F.data.startswith("ctper_"))
async def contest_period_selected(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("ctper_"):]
    parts = raw.split("_")
    if len(parts) != 2:
        await callback.answer("❌ Неверный формат", show_alert=True)
        return
    start_date, end_date = parts
    await state.update_data(ct_start=start_date, ct_end=end_date)
    await _show_shop_filter(callback, state)


# ── Календарь — навигация и выбор дня ─────────────────────────────────────────

@contests_router.callback_query(F.data == "ctcal_noop")
async def contest_calendar_noop(callback: CallbackQuery):
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ctcal_nav_"))
async def contest_calendar_nav(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data[len("ctcal_nav_"):].split("_")
    year, month = int(parts[0]), int(parts[1])
    data = await state.get_data()
    picking = data.get('ct_cal_picking', 'start')
    await state.update_data(ct_cal_year=year, ct_cal_month=month)
    edit_cid = data.get('ct_edit_cid')
    is_edit = bool(edit_cid)
    if picking == 'start':
        kb = _build_calendar_keyboard(year, month)
        header = (
            "✏️ <b>Редактирование — Дата начала</b>\n\nВыберите новую дату начала:"
            if is_edit else
            "📅 <b>Шаг 7/8 — Дата начала конкурса</b>\n\nВыберите дату начала:"
        )
    else:
        selected_start = data.get('ct_start', '')
        kb = _build_calendar_keyboard(year, month, selected_start)
        header = (
            f"✏️ <b>Редактирование — Дата окончания</b>\n\n"
            f"Начало: <b>{_fmt_date(selected_start)}</b>\n\n"
            f"Выберите новую дату окончания:"
            if is_edit else
            f"📅 <b>Шаг 7/8 — Дата окончания конкурса</b>\n\n"
            f"Начало: <b>{_fmt_date(selected_start)}</b>\n\n"
            f"Выберите дату окончания:"
        )
    await callback.message.edit_text(header, reply_markup=kb, parse_mode="HTML")


@contests_router.callback_query(F.data.startswith("ctcal_day_"))
async def contest_calendar_day(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data[len("ctcal_day_"):]
    data = await state.get_data()
    picking = data.get('ct_cal_picking', 'start')
    year = data.get('ct_cal_year', datetime.now().year)
    month = data.get('ct_cal_month', datetime.now().month)

    edit_cid = data.get('ct_edit_cid')

    if picking == 'start':
        await callback.answer()
        await state.update_data(ct_start=date_str, ct_cal_picking='end')
        kb = _build_calendar_keyboard(year, month, date_str)
        hdr = (
            f"✏️ <b>Редактирование — Дата окончания</b>\n\n"
            f"Начало: <b>{_fmt_date(date_str)}</b>\n\n"
            f"Теперь выберите дату окончания:"
            if edit_cid else
            f"📅 <b>Шаг 7/8 — Дата окончания конкурса</b>\n\n"
            f"Начало: <b>{_fmt_date(date_str)}</b>\n\n"
            f"Теперь выберите дату окончания:"
        )
        await callback.message.edit_text(hdr, reply_markup=kb, parse_mode="HTML")
    else:
        start_str = data.get('ct_start', '')
        try:
            end_dt = datetime.strptime(date_str, '%Y-%m-%d')
            start_dt = datetime.strptime(start_str, '%Y-%m-%d')
            if end_dt < start_dt:
                await callback.answer(
                    "❌ Дата окончания не может быть раньше начала!", show_alert=True
                )
                return
        except ValueError:
            await callback.answer("❌ Ошибка даты", show_alert=True)
            return
        await state.update_data(ct_end=date_str)
        if edit_cid:
            current_db = await get_db(callback.from_user.id, state)
            ok = await current_db.update_contest(edit_cid, start_date=start_str, end_date=date_str)
            await state.update_data(ct_edit_cid=None)
            back_kb = InlineKeyboardBuilder()
            back_kb.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{edit_cid}")
            back_kb.adjust(1)
            msg = (
                f"✅ <b>Период обновлён</b>\n\n📅 {_fmt_date(start_str)} — {_fmt_date(date_str)}"
                if ok else "❌ Ошибка при обновлении периода"
            )
            await callback.message.edit_text(msg, reply_markup=back_kb.as_markup(), parse_mode="HTML")
            await callback.answer()
        else:
            await _show_shop_filter(callback, state)


async def _show_shop_filter_from_message(message: Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Все магазины", callback_data="ctshop_all")
    builder.button(text="🏪 Конкретные магазины", callback_data="ctshop_select")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await fsm_edit(
        state, message,
        "🏆 <b>Новый конкурс — шаг 8/8</b>\n\n<b>Фильтр по магазинам:</b>",
        reply_markup=builder.as_markup()
    )


async def _show_shop_filter(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Все магазины", callback_data="ctshop_all")
    builder.button(text="🏪 Конкретные магазины", callback_data="ctshop_select")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "🏆 <b>Новый конкурс — шаг 8/8</b>\n\n<b>Фильтр по магазинам:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data == "ctshop_all")
async def contest_shop_all(callback: CallbackQuery, state: FSMContext):
    await state.update_data(ct_shops=None)
    await _show_user_filter(callback, state)


@contests_router.callback_query(F.data == "ctshop_select")
async def contest_shop_select(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    if not shops:
        await callback.answer("❌ Нет магазинов в системе", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get('ct_shops') or []
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await _show_contest_shop_list(callback.message, shops, selected)
    await callback.answer()


@contests_router.callback_query(F.data == "ct_srch_shop_start")
async def ct_srch_shop_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.shop_contests)
    await fsm_edit(state, callback.message,
                   "🔍 <b>Поиск магазина</b>\n\nВведите название магазина (или его часть):",
                   InlineKeyboardMarkup(inline_keyboard=[[
                       InlineKeyboardButton(text="✖️ Отмена", callback_data="ct_srch_shop_cancel")
                   ]]))
    await callback.answer()


@contests_router.callback_query(F.data == "ct_srch_shop_cancel")
async def ct_srch_shop_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    data = await state.get_data()
    selected = data.get('ct_shops') or []
    await _show_contest_shop_list(callback.message, shops, selected)


@contests_router.message(SearchStates.shop_contests)
async def ct_srch_shop_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    shops = await current_db.get_all_shops()
    data = await state.get_data()
    selected = data.get('ct_shops') or []
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти магазин", callback_data="ct_srch_shop_start")
    for shop in filtered:
        icon = "✅" if shop in selected else "◻️"
        builder.button(text=f"{icon} {shop}", callback_data=safe_cb("ctshopchk_", shop))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="ct_srch_shop_cancel")
    builder.button(text="💾 Применить", callback_data="ctshop_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    cnt = len(selected)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"🏪 <b>Выберите магазины для конкурса</b> (выбрано: {cnt}):{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@contests_router.callback_query(F.data.startswith("ctshopchk_"))
async def contest_toggle_shop(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    raw = callback.data[len("ctshopchk_"):]
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    shop = resolve_cb_name(raw, shops)
    data = await state.get_data()
    selected = list(data.get('ct_shops') or [])
    if shop in selected:
        selected.remove(shop)
    else:
        selected.append(shop)
    await state.update_data(ct_shops=selected if selected else None)
    await _show_contest_shop_list(callback.message, shops, selected)


@contests_router.callback_query(F.data == "ctshop_done")
async def contest_shop_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('ct_shops'):
        await callback.answer("⚠️ Выберите хотя бы один магазин", show_alert=True)
        return
    await _show_user_filter(callback, state)


async def _show_user_filter(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Все сотрудники", callback_data="ctusr_all")
    builder.button(text="👤 Конкретные сотрудники", callback_data="ctusr_select")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "🏆 <b>Участники конкурса:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data == "ctusr_all")
async def contest_user_all(callback: CallbackQuery, state: FSMContext):
    await state.update_data(ct_users=None)
    await _show_notify_confirm(callback, state)


@contests_router.callback_query(F.data == "ctusr_select")
async def contest_user_select(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    all_users = await current_db.get_all_users()
    sellers = [u for u in all_users if not env_manager.is_super_admin(u[1])]
    if not sellers:
        await callback.answer("❌ Нет сотрудников в системе", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get('ct_users') or []
    builder = InlineKeyboardBuilder()
    for u in sellers:
        uid, tg_id, fname, lname = u[0], u[1], u[2], u[3]
        shop = u[8] if len(u) > 8 else ""
        label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
        icon = "✅" if uid in selected else "◻️"
        builder.button(text=f"{icon} {label}", callback_data=f"ctusrchk_{uid}")
    builder.button(text="💾 Применить", callback_data="ctusr_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "👤 Выберите участников конкурса:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ctusrchk_"))
async def contest_toggle_user(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data[len("ctusrchk_"):])
    data = await state.get_data()
    selected = list(data.get('ct_users') or [])
    if user_id in selected:
        selected.remove(user_id)
    else:
        selected.append(user_id)
    await state.update_data(ct_users=selected if selected else None)

    current_db = await get_db(callback.from_user.id, state)
    all_users = await current_db.get_all_users()
    sellers = [u for u in all_users if not env_manager.is_super_admin(u[1])]
    builder = InlineKeyboardBuilder()
    for u in sellers:
        uid, tg_id, fname, lname = u[0], u[1], u[2], u[3]
        shop = u[8] if len(u) > 8 else ""
        label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
        icon = "✅" if uid in selected else "◻️"
        builder.button(text=f"{icon} {label}", callback_data=f"ctusrchk_{uid}")
    builder.button(text="💾 Применить", callback_data="ctusr_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        f"👤 Участники (выбрано: {len(selected)}):",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data == "ctusr_done")
async def contest_user_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('ct_users'):
        await callback.answer("⚠️ Выберите хотя бы одного участника", show_alert=True)
        return
    await _show_notify_confirm(callback, state)


async def _show_notify_confirm(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Уведомить участников о старте", callback_data="ctnotify_1")
    builder.button(text="🔕 Не уведомлять", callback_data="ctnotify_0")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "📢 <b>Уведомление участников</b>\n\n"
        "Отправить сообщение о старте конкурса всем участникам?",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ctnotify_"))
async def contest_notify_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    notify = int(callback.data[len("ctnotify_"):])
    await state.update_data(ct_notify=notify)
    await _show_contest_confirm(callback, state)


async def _show_contest_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    title = data.get('ct_title', '?')
    desc = data.get('ct_description', '')
    scope_labels = {'product': '📦 Конкретные товары', 'category': '📂 По категории', 'any': '🌐 Все товары'}
    scope = scope_labels.get(data.get('ct_contest_type', 'any'), '?')
    metric_labels = {'turnover': '💰 Оборот', 'quantity': '📦 Количество'}
    metric = metric_labels.get(data.get('ct_metric', 'turnover'), '?')
    metric_unit = '₽' if data.get('ct_metric') == 'turnover' else ' шт'
    target = data.get('ct_target', 0)
    reward_type = data.get('ct_reward_type', 'fixed')
    reward = data.get('ct_reward', 0)
    start = data.get('ct_start', '?')
    end = data.get('ct_end', '?')
    shops = data.get('ct_shops')
    users = data.get('ct_users')
    notify = data.get('ct_notify', 0)
    reward_mode = data.get('ct_reward_mode', 'total')
    tiers = data.get('ct_tiers') or []
    ind_targets = data.get('ct_individual_targets') or {}

    shops_str = ", ".join(shops) if shops else "Все магазины"
    users_str = f"{len(users)} чел." if users else "Все сотрудники"
    mode_label = '💵 Бонус за каждую продажу' if reward_mode == 'per_sale' else '🏅 Итоговый приз'

    text = (
        f"✅ <b>Подтверждение создания конкурса</b>\n\n"
        f"🏆 <b>{he(title)}</b>\n"
        + (f"📝 {he(desc)}\n" if desc else "")
        + f"\n🎯 Охват: {scope}\n"
        f"📊 Метрика: {metric}\n"
        f"💡 Режим: {mode_label}\n"
    )

    if reward_mode == 'per_sale':
        if tiers:
            text += "📋 <b>Тиры бонусов:</b>\n"
            for t in tiers:
                pct = t.get('min_plan_pct', 0)
                pct_str = "всегда" if pct == 0 else f"≥{pct:.0f}% плана"
                bonuses = t.get('bonuses', [])
                bonus_lines = []
                for b in bonuses:
                    bname = b.get('product_name') or 'все товары'
                    bonus_lines.append(f"{he(bname)}: {format_price(b['bonus_per_unit'])}₽/шт")
                text += f"  • {pct_str}: " + ", ".join(bonus_lines) + "\n"
    else:
        reward_str = f"{format_price(reward)}₽" if reward_type == 'fixed' else f"{reward}% от оборота"
        text += f"🎯 Порог победы: {format_price(target)}{metric_unit}\n"
        text += f"🏅 Награда: {reward_str}\n"
        if ind_targets:
            text += "🏪 <b>Индивидуальные пороги:</b>\n"
            for s, v in ind_targets.items():
                text += f"  • {he(s)}: {format_price(v)}{metric_unit}\n"

    text += (
        f"📅 Период: {_fmt_date(start)} — {_fmt_date(end)}\n"
        f"🏪 Магазины: {he(shops_str)}\n"
        f"👥 Участники: {users_str}\n"
        f"📢 Уведомление о старте: {'✅ Да' if notify else '❌ Нет'}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Создать конкурс", callback_data="ct_confirm_create")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@contests_router.callback_query(F.data == "ct_confirm_create")
async def contest_confirm_create(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    data = await state.get_data()
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    created_by = user[0] if user else None

    reward_mode = data.get('ct_reward_mode', 'total')
    ind_targets = data.get('ct_individual_targets') or {}
    ind_targets_json = _json.dumps({'by_shop': ind_targets}, ensure_ascii=False) if ind_targets else None

    contest_id = await current_db.create_contest(
        title=data.get('ct_title'),
        description=data.get('ct_description'),
        contest_type=data.get('ct_contest_type', 'any'),
        metric_type=data.get('ct_metric', 'turnover'),
        target_value=data.get('ct_target', 0) or 0,
        reward_type=data.get('ct_reward_type', 'fixed'),
        reward_value=data.get('ct_reward', 0) or 0,
        start_date=data.get('ct_start'),
        end_date=data.get('ct_end'),
        shop_filter=_json.dumps(data.get('ct_shops'), ensure_ascii=False) if data.get('ct_shops') else None,
        user_filter=_json.dumps(data.get('ct_users')) if data.get('ct_users') else None,
        product_filter=_json.dumps(data.get('ct_products')) if data.get('ct_products') else None,
        category_filter=_json.dumps(data.get('ct_categories'), ensure_ascii=False) if data.get('ct_categories') else None,
        notify_on_start=data.get('ct_notify', 0),
        created_by=created_by,
        reward_mode=reward_mode,
        individual_targets=ind_targets_json,
    )

    # Сохраняем тиры per_sale
    if contest_id and reward_mode == 'per_sale':
        tiers = data.get('ct_tiers') or []
        if tiers:
            await current_db.save_contest_product_bonuses(contest_id, tiers)

    await clear_state_keep_org(state)

    if not contest_id:
        await callback.message.edit_text(
            "❌ Ошибка при создании конкурса",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="contests_menu"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    notify_info = ""
    if data.get('ct_notify'):
        sent = 0
        all_users = await current_db.get_all_users()
        user_filter = data.get('ct_users')
        shop_filter = data.get('ct_shops')
        notif_text = (
            f"🏆 <b>Новый конкурс!</b>\n\n"
            f"<b>{he(data.get('ct_title', ''))}</b>\n\n"
            f"📅 Период: {_fmt_date(data.get('ct_start', ''))} — {_fmt_date(data.get('ct_end', ''))}\n"
            f"🎯 Порог победы: {format_price(data.get('ct_target', 0))}"
            f"{'₽' if data.get('ct_metric') == 'turnover' else ' шт'}\n"
            f"🏅 Награда: "
            f"{format_price(data.get('ct_reward', 0))}₽" if data.get('ct_reward_type') == 'fixed'
            else f"{data.get('ct_reward', 0)}% от оборота"
        )
        for u in all_users:
            uid_i, tg_id = u[0], u[1]
            if env_manager.is_super_admin(tg_id):
                continue
            if user_filter and uid_i not in user_filter:
                continue
            shop_name = u[8] if len(u) > 8 else None
            if shop_filter and shop_name not in shop_filter:
                continue
            try:
                await callback.bot.send_message(tg_id, notif_text, parse_mode="HTML", reply_markup=add_read_btn())
                sent += 1
            except Exception as e:
                logger.error(f"Ошибка уведомления о конкурсе {tg_id}: {e}")
        notify_info = f"\n📢 Уведомлено участников: {sent}"

    builder = InlineKeyboardBuilder()
    builder.button(text="🏆 Активные конкурсы", callback_data="contest_list_active")
    builder.button(text="⬅️ К конкурсам", callback_data="contests_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>Конкурс создан!</b>\n\n"
        f"🏆 {he(data.get('ct_title', ''))}\n"
        f"📅 {_fmt_date(data.get('ct_start', ''))} — {_fmt_date(data.get('ct_end', ''))}"
        + notify_info,
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Список конкурсов ───────────────────────────────────────────────────────────

async def _render_active_contests(callback: CallbackQuery, state: FSMContext, page: int = 0):
    current_db = await get_db(callback.from_user.id, state)
    contests = await current_db.get_contests(status='active')
    await callback.answer()

    builder = InlineKeyboardBuilder()
    if not contests:
        builder.button(text="➕ Создать конкурс", callback_data="contest_create")
        builder.button(text="🔄 Обновить", callback_data="contest_list_active")
        builder.button(text="⬅️ Назад", callback_data="contests_menu")
        builder.adjust(1)
        await safe_edit_message(callback,
                                "🏆 <b>Активные конкурсы</b>\n\n❌ Нет активных конкурсов",
                                builder.as_markup())
        return

    page_items, has_prev, has_next, total_pages, page = paginate(contests, page, PAGE_SIZE_DEFAULT)

    text = "🏆 <b>Активные конкурсы</b>"
    if total_pages > 1:
        text += f" · стр. {page + 1}/{total_pages}"
    text += f"\n\n"

    for c in page_items:
        cid = c[0]
        title = c[1]
        metric = c[4]
        target = c[5]
        rtype = c[6]
        rval = c[7]
        start = c[8]
        end = c[9]
        metric_unit = '₽' if metric == 'turnover' else ' шт'
        reward_str = f"{format_price(rval)}₽" if rtype == 'fixed' else f"{rval}%"
        text += (f"🏆 <b>{he(title)}</b>\n"
                 f"📅 {_fmt_date(start)} — {_fmt_date(end)}\n"
                 f"🎯 {format_price(target)}{metric_unit} · 🏅 {reward_str}\n\n")
        builder.button(text=f"📊 {title[:35]}", callback_data=f"ct_view_{cid}")

    nav = page_nav_row("cal_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.button(text="🔄 Обновить", callback_data="contest_list_active")
    builder.button(text="➕ Создать", callback_data="contest_create")
    builder.button(text="⬅️ Назад", callback_data="contests_menu")
    builder.adjust(1)

    await safe_edit_message(callback, text, builder.as_markup())


@contests_router.callback_query(F.data == "contest_list_active")
async def contest_list_active(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await _render_active_contests(callback, state, page=0)


@contests_router.callback_query(F.data.startswith("cal_pg_"))
async def contest_active_page(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    try:
        page = int(callback.data.replace("cal_pg_", ""))
    except ValueError:
        page = 0
    await _render_active_contests(callback, state, page=page)


async def _render_archive_contests(callback: CallbackQuery, state: FSMContext, page: int = 0):
    current_db = await get_db(callback.from_user.id, state)
    finished = await current_db.get_contests(status='finished')
    cancelled = await current_db.get_contests(status='cancelled')
    contests = finished + cancelled
    await callback.answer()

    builder = InlineKeyboardBuilder()
    if not contests:
        builder.button(text="⬅️ Назад", callback_data="contests_menu")
        await safe_edit_message(callback,
                                "📋 <b>Архив конкурсов</b>\n\n❌ Архив пуст",
                                builder.as_markup())
        return

    page_items, has_prev, has_next, total_pages, page = paginate(contests, page, PAGE_SIZE_DEFAULT)

    text = "📋 <b>Архив конкурсов</b>"
    if total_pages > 1:
        text += f" · стр. {page + 1}/{total_pages}"
    text += f"\n\n"

    for c in page_items:
        cid = c[0]
        title = c[1]
        start = c[8]
        end = c[9]
        status = c[16]
        icon = "✅" if status == 'finished' else "❌"
        text += f"{icon} <b>{he(title)}</b> ({_fmt_date(start)}–{_fmt_date(end)})\n"
        builder.button(text=f"📊 {title[:35]}", callback_data=f"ct_view_{cid}")

    nav = page_nav_row("car_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.button(text="🗑️ Очистить архив", callback_data="contest_archive_clear_confirm")
    builder.button(text="⬅️ Назад", callback_data="contests_menu")
    builder.adjust(1)

    await safe_edit_message(callback, text, builder.as_markup())


@contests_router.callback_query(F.data == "contest_list_archive")
async def contest_list_archive(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await _render_archive_contests(callback, state, page=0)


@contests_router.callback_query(F.data.startswith("car_pg_"))
async def contest_archive_page(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    try:
        page = int(callback.data.replace("car_pg_", ""))
    except ValueError:
        page = 0
    await _render_archive_contests(callback, state, page=page)


@contests_router.callback_query(F.data == "contest_archive_clear_confirm")
async def contest_archive_clear_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    finished = await current_db.get_contests(status='finished')
    cancelled = await current_db.get_contests(status='cancelled')
    total = len(finished) + len(cancelled)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить всё", callback_data="contest_archive_clear_do")
    builder.button(text="⬅️ Отмена", callback_data="contest_list_archive")
    builder.adjust(1)

    await safe_edit_message(
        callback,
        f"🗑️ <b>Очистить архив конкурсов?</b>\n\n"
        f"Будет удалено <b>{total}</b> конкурс(ов).\n"
        f"⚠️ Действие необратимо.",
        builder.as_markup()
    )


@contests_router.callback_query(F.data == "contest_archive_clear_do")
async def contest_archive_clear_do(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    deleted = await current_db.clear_contests_archive()

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К конкурсам", callback_data="contests_menu")
    builder.adjust(1)

    await safe_edit_message(
        callback,
        f"✅ Архив очищен. Удалено конкурсов: <b>{deleted}</b>.",
        builder.as_markup()
    )


# ── Просмотр конкурса ──────────────────────────────────────────────────────────

@contests_router.callback_query(F.data.startswith("ct_view_"))
async def contest_view(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    contest_id = int(callback.data[len("ct_view_"):])
    current_db = await get_db(callback.from_user.id, state)
    contest = await current_db.get_contest(contest_id)

    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return

    cid = contest[0]
    title = contest[1]
    desc = contest[2]
    ctype = contest[3]
    metric = contest[4]
    target = contest[5]
    rtype = contest[6]
    rval = contest[7]
    start = contest[8]
    end = contest[9]
    shop_f = contest[10]
    cat_f = contest[14]
    status = contest[16]
    reward_mode = contest[22] if len(contest) > 22 else 'total'
    ind_tgt_raw = contest[23] if len(contest) > 23 else None

    ctype_labels = {'product': 'Товары', 'category': 'Категории', 'any': 'Все товары'}
    metric_label = {'turnover': 'Оборот', 'quantity': 'Количество'}.get(metric, metric)
    metric_unit = '₽' if metric == 'turnover' else ' шт'
    status_labels = {'active': '🟢 Активен', 'finished': '✅ Завершён', 'cancelled': '❌ Отменён'}
    shops_str = ", ".join(_json.loads(shop_f)) if shop_f else "Все"
    cats_str = ", ".join(_json.loads(cat_f)) if cat_f else None
    mode_label = '💵 Бонус за продажу' if reward_mode == 'per_sale' else '🏅 Итоговый приз'

    text = (
        f"🏆 <b>{he(title)}</b>\n"
        + (f"📝 {he(desc)}\n" if desc else "")
        + f"\n📊 Статус: {status_labels.get(status, status)}\n"
        f"📅 Период: {_fmt_date(start)} — {_fmt_date(end)}\n"
        f"🎯 Охват: {ctype_labels.get(ctype, ctype)}\n"
        f"📈 Метрика: {metric_label}\n"
        f"💡 Режим: {mode_label}\n"
    )

    if reward_mode == 'per_sale':
        tier_bonuses = await current_db.get_contest_product_bonuses(cid)
        if tier_bonuses:
            # Группируем по min_plan_pct
            from collections import defaultdict
            tiers_grouped = defaultdict(list)
            for b in tier_bonuses:
                tiers_grouped[b['min_plan_pct']].append(b)
            text += "📋 <b>Тиры бонусов:</b>\n"
            for pct in sorted(tiers_grouped.keys()):
                pct_str = "всегда" if pct == 0 else f"≥{pct:.0f}% плана"
                lines = []
                for b in tiers_grouped[pct]:
                    bname = b.get('product_name') or 'все товары'
                    lines.append(f"{he(bname)}: {format_price(b['bonus_per_unit'])}₽/шт")
                text += f"  • {pct_str}: " + ", ".join(lines) + "\n"
    else:
        reward_str = f"{format_price(rval)}₽ (фиксированно)" if rtype == 'fixed' else f"{rval}% от оборота"
        text += f"🎯 Порог: {format_price(target)}{metric_unit}\n"
        text += f"🏅 Награда: {reward_str}\n"
        # Индивидуальные пороги
        if ind_tgt_raw:
            try:
                ind = _json.loads(ind_tgt_raw)
                by_shop = ind.get('by_shop', {})
                if by_shop:
                    text += "🏪 <b>Индивидуальные пороги:</b>\n"
                    for sn, tv in by_shop.items():
                        text += f"  • {he(sn)}: {format_price(tv)}{metric_unit}\n"
            except Exception:
                pass

    text += (
        f"🏪 Магазины: {he(shops_str)}\n"
        + (f"📂 Категории: {he(cats_str)}\n" if cats_str else "")
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Текущие результаты", callback_data=f"ct_results_{cid}")
    builder.button(text="✏️ Скорректировать показатели", callback_data=f"ct_manual_{cid}")
    if status == 'active':
        builder.button(text="✏️ Редактировать", callback_data=f"ct_edit_{cid}")
        builder.button(text="✅ Завершить конкурс", callback_data=f"ct_finish_{cid}")
        builder.button(text="❌ Отменить конкурс", callback_data=f"ct_cancel_{cid}")
    builder.button(text="🔄 Обновить", callback_data=f"ct_view_{cid}")
    back_cb = "contest_list_active" if status == 'active' else "contest_list_archive"
    builder.button(text="⬅️ Назад", callback_data=back_cb)
    builder.adjust(1)

    await safe_edit_message(callback, text, builder.as_markup())


# ── Результаты конкурса ────────────────────────────────────────────────────────

@contests_router.callback_query(F.data.startswith("ct_results_"))
async def contest_results(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    contest_id = int(callback.data[len("ct_results_"):])
    current_db = await get_db(callback.from_user.id, state)
    contest = await current_db.get_contest(contest_id)

    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return

    cid = contest[0]
    title = contest[1]
    metric = contest[4]
    start = contest[8]
    end = contest[9]
    status = contest[16]
    reward_mode = contest[22] if len(contest) > 22 else 'total'

    results = await current_db.compute_contest_results(contest_id)
    metric_unit = '₽' if metric == 'turnover' else ' шт'

    text = f"📊 <b>Результаты: {he(title)}</b>\n📅 {_fmt_date(start)} — {_fmt_date(end)}\n\n"

    if not results:
        text += "❌ Нет данных о продажах за указанный период"
    elif reward_mode == 'per_sale':
        # ── per_sale: показываем накопленный бонус ───────────────────────────
        earners = [r for r in results if r.get('reward', 0) > 0]
        no_bonus = [r for r in results if r.get('reward', 0) == 0]

        if earners:
            text += "💵 <b>Бонусы за продажи:</b>\n"
            for i, r in enumerate(earners, 1):
                name = he(f"{r['first_name']} {r['last_name']}".strip())
                uname = r.get('username')
                uname_str = f" <a href='tg://resolve?domain={he(uname)}'>@{he(uname)}</a>" if uname else ""
                qty = int(r.get('actual', 0))
                bonus = format_price(r.get('reward', 0))
                plan_pct = r.get('plan_pct', 0)
                tier_note = f" (план {plan_pct:.0f}%)" if plan_pct > 0 else ""
                text += f"  {i}. {name}{uname_str} — {qty} шт. → 💵 +{bonus}₽{tier_note}\n"
            text += "\n"

        if no_bonus:
            text += "📉 <b>Продаж по конкурсу нет:</b>\n"
            for r in no_bonus[:5]:
                name = he(f"{r['first_name']} {r['last_name']}".strip())
                uname = r.get('username')
                uname_str = f" <a href='tg://resolve?domain={he(uname)}'>@{he(uname)}</a>" if uname else ""
                text += f"  • {name}{uname_str}\n"
            if len(no_bonus) > 5:
                text += f"  … и ещё {len(no_bonus) - 5}\n"
    else:
        # ── total: победители и не достигшие порога ──────────────────────────
        winners = [r for r in results if r['is_winner']]
        others = [r for r in results if not r['is_winner']]

        if winners:
            text += "🏆 <b>Победители:</b>\n"
            for i, r in enumerate(winners, 1):
                name = he(f"{r['first_name']} {r['last_name']}".strip())
                uname = r.get('username')
                uname_str = f" <a href='tg://resolve?domain={he(uname)}'>@{he(uname)}</a>" if uname else ""
                actual = format_price(r['actual']) if metric == 'turnover' else str(int(r['actual']))
                reward = format_price(r['reward'])
                ind_tgt = r.get('individual_target')
                tgt_note = f" (порог: {format_price(ind_tgt)}{metric_unit})" if ind_tgt else ""
                manual_note = " ✏️" if r.get('is_manual') else ""
                text += f"  {i}. {name}{uname_str} — {actual}{metric_unit}{tgt_note}{manual_note} → 🏅 +{reward}₽\n"
            text += "\n"

        if others:
            text += "📉 <b>Не достигли порога:</b>\n"
            for r in others[:10]:
                name = he(f"{r['first_name']} {r['last_name']}".strip())
                uname = r.get('username')
                uname_str = f" <a href='tg://resolve?domain={he(uname)}'>@{he(uname)}</a>" if uname else ""
                actual = format_price(r['actual']) if metric == 'turnover' else str(int(r['actual']))
                ind_tgt = r.get('individual_target')
                tgt_note = f" / нужно {format_price(ind_tgt)}{metric_unit}" if ind_tgt else ""
                manual_note = " ✏️" if r.get('is_manual') else ""
                text += f"  • {name}{uname_str} — {actual}{metric_unit}{tgt_note}{manual_note}\n"
            if len(others) > 10:
                text += f"  … и ещё {len(others) - 10}\n"

    winners_list = [r for r in results if r['is_winner']] if results else []

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить результаты", callback_data=f"ct_results_{cid}")
    builder.button(text="✏️ Скорректировать показатели", callback_data=f"ct_manual_{cid}")
    if winners_list and status == 'finished':
        builder.button(text="📢 Разослать итоги победителям", callback_data=f"ct_notify_win_{cid}")
    builder.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{cid}")
    builder.adjust(1)

    await safe_edit_message(callback, text, builder.as_markup())


@contests_router.callback_query(F.data.startswith("ct_notify_win_"))
async def contest_notify_winners(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    contest_id = int(callback.data[len("ct_notify_win_"):])
    current_db = await get_db(callback.from_user.id, state)
    contest = await current_db.get_contest(contest_id)
    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return

    title = contest[1]
    metric = contest[4]
    reward_mode = contest[22] if len(contest) > 22 else 'total'
    results = await current_db.compute_contest_results(contest_id)
    winners = [r for r in results if r['is_winner']]
    sent = 0
    for r in winners:
        try:
            if reward_mode == 'per_sale':
                qty = int(r.get('actual', 0))
                bonus = format_price(r.get('reward', 0))
                plan_pct = r.get('plan_pct', 0)
                plan_note = f"\n📊 Выполнение плана: {plan_pct:.0f}%" if plan_pct > 0 else ""
                msg_text = (
                    f"🏆 <b>Итоги конкурса «{he(title)}»</b>\n\n"
                    f"Продано единиц: <b>{qty} шт.</b>\n"
                    f"💵 Ваш бонус за продажи: <b>+{bonus}₽</b>{plan_note}\n\n"
                    f"Отличная работа! 🎉"
                )
            else:
                actual_str = f"{format_price(r['actual'])}₽" if metric == 'turnover' else f"{int(r['actual'])} шт"
                msg_text = (
                    f"🏆 <b>Поздравляем с победой!</b>\n\n"
                    f"Вы победили в конкурсе «{he(title)}»!\n\n"
                    f"📊 Ваш результат: {actual_str}\n"
                    f"🏅 Ваша награда: {format_price(r['reward'])}₽\n\n"
                    f"Отличная работа! 🎉"
                )
            await callback.bot.send_message(
                r['telegram_id'], msg_text,
                parse_mode="HTML", reply_markup=add_read_btn()
            )
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка уведомления победителя {r['telegram_id']}: {e}")

    await callback.answer(f"✅ Отправлено уведомлений: {sent}", show_alert=True)


# ── Завершение / отмена конкурса ───────────────────────────────────────────────

@contests_router.callback_query(F.data.startswith("ct_finish_ok_"))
async def contest_finish_execute(callback: CallbackQuery, state: FSMContext):
    contest_id = int(callback.data[len("ct_finish_ok_"):])
    current_db = await get_db(callback.from_user.id, state)
    success = await current_db.update_contest_status(contest_id, 'finished')

    if success:
        results = await current_db.compute_contest_results(contest_id)
        winners = [r for r in results if r['is_winner']]
        builder = InlineKeyboardBuilder()
        builder.button(text="📊 Результаты", callback_data=f"ct_results_{contest_id}")
        builder.button(text="⬅️ Архив", callback_data="contest_list_archive")
        builder.adjust(1)
        await callback.message.edit_text(
            f"✅ <b>Конкурс завершён!</b>\n\n🏆 Победителей: {len(winners)}\n\n"
            "Результаты доступны в карточке конкурса.",
            reply_markup=builder.as_markup(), parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка при завершении конкурса",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data=f"ct_view_{contest_id}"
            ).as_markup(), parse_mode="HTML"
        )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ct_finish_"))
async def contest_finish_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    contest_id = int(callback.data[len("ct_finish_"):])
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Завершить и подвести итоги",
                   callback_data=f"ct_finish_ok_{contest_id}")
    builder.button(text="❌ Отмена", callback_data=f"ct_view_{contest_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        "✅ <b>Завершить конкурс?</b>\n\n"
        "Конкурс будет отмечен как завершённый. Итоги будут рассчитаны.",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ct_cancel_ok_"))
async def contest_cancel_execute(callback: CallbackQuery, state: FSMContext):
    contest_id = int(callback.data[len("ct_cancel_ok_"):])
    current_db = await get_db(callback.from_user.id, state)
    success = await current_db.update_contest_status(contest_id, 'cancelled')
    result_text = "✅ <b>Конкурс отменён</b>" if success else "❌ Ошибка при отмене"
    await callback.message.edit_text(
        result_text,
        reply_markup=InlineKeyboardBuilder().button(
            text="⬅️ К конкурсам", callback_data="contests_menu"
        ).as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ct_cancel_"))
async def contest_cancel_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    contest_id = int(callback.data[len("ct_cancel_"):])
    builder = InlineKeyboardBuilder()
    builder.button(text="⚠️ Да, отменить", callback_data=f"ct_cancel_ok_{contest_id}")
    builder.button(text="❌ Нет", callback_data=f"ct_view_{contest_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        "⚠️ <b>Отменить конкурс?</b>\n\nЭто действие необратимо.",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Редактирование конкурса ────────────────────────────────────────────────────

@contests_router.callback_query(F.data.startswith("ct_ep_"))
async def contest_edit_period(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    contest_id = int(callback.data[len("ct_ep_"):])
    now = datetime.now()
    await state.update_data(
        ct_cal_picking='start',
        ct_cal_year=now.year,
        ct_cal_month=now.month,
        ct_edit_cid=contest_id,
    )
    kb = _build_calendar_keyboard(now.year, now.month)
    await callback.message.edit_text(
        "✏️ <b>Редактирование — Дата начала</b>\n\nВыберите новую дату начала:",
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ct_et_"))
async def contest_edit_target(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    contest_id = int(callback.data[len("ct_et_"):])
    current_db = await get_db(callback.from_user.id, state)
    contest = await current_db.get_contest(contest_id)
    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return
    metric_unit = '₽' if contest[4] == 'turnover' else ' шт'
    await state.update_data(ct_edit_cid=contest_id)
    await state.set_state(ContestStates.editing_contest_target)
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Отмена", callback_data=f"ct_view_{contest_id}")
    cancel_kb.adjust(1)
    await callback.message.edit_text(
        f"✏️ <b>Редактирование — Порог победы</b>\n\n"
        f"Текущий порог: <b>{format_price(contest[5])}{metric_unit}</b>\n\n"
        f"Введите новый порог победы (только число):",
        reply_markup=cancel_kb.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ct_er_"))
async def contest_edit_reward(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    contest_id = int(callback.data[len("ct_er_"):])
    current_db = await get_db(callback.from_user.id, state)
    contest = await current_db.get_contest(contest_id)
    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return
    rtype = contest[6]
    rval = contest[7]
    reward_str = f"{format_price(rval)}₽" if rtype == 'fixed' else f"{rval}% от оборота"
    await state.update_data(ct_edit_cid=contest_id)
    await state.set_state(ContestStates.editing_contest_reward)
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Отмена", callback_data=f"ct_view_{contest_id}")
    cancel_kb.adjust(1)
    await callback.message.edit_text(
        f"✏️ <b>Редактирование — Награда</b>\n\n"
        f"Текущая награда: <b>{reward_str}</b>\n\n"
        f"Введите новое значение (только число):",
        reply_markup=cancel_kb.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ct_edit_"))
async def contest_edit_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    contest_id = int(callback.data[len("ct_edit_"):])
    current_db = await get_db(callback.from_user.id, state)
    contest = await current_db.get_contest(contest_id)
    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return
    title = contest[1]
    target = contest[5]
    metric_unit = '₽' if contest[4] == 'turnover' else ' шт'
    rtype = contest[6]
    rval = contest[7]
    start = contest[8]
    end = contest[9]
    reward_str = f"{format_price(rval)}₽" if rtype == 'fixed' else f"{rval}%"

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"📅 Период ({_fmt_date(start)} – {_fmt_date(end)})",
        callback_data=f"ct_ep_{contest_id}"
    )
    builder.button(
        text=f"🎯 Порог победы ({format_price(target)}{metric_unit})",
        callback_data=f"ct_et_{contest_id}"
    )
    builder.button(
        text=f"🏅 Награда ({reward_str})",
        callback_data=f"ct_er_{contest_id}"
    )
    builder.button(text="⬅️ Назад", callback_data=f"ct_view_{contest_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        f"✏️ <b>Редактировать конкурс</b>\n\n"
        f"🏆 {he(title)}\n\n"
        f"Выберите что изменить:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.message(ContestStates.editing_contest_target)
async def contest_edit_target_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    contest_id = data.get('ct_edit_cid')
    raw = message.text.strip().replace(' ', '').replace(',', '.')
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        cancel_kb = InlineKeyboardBuilder()
        cancel_kb.button(text="❌ Отмена", callback_data=f"ct_view_{contest_id}")
        await fsm_edit(state, message,
                       "❌ Введите положительное число:",
                       reply_markup=cancel_kb.as_markup())
        return
    current_db = await get_db(message.from_user.id, state)
    ok = await current_db.update_contest(contest_id, target_value=value)
    await state.set_state(None)
    await state.update_data(ct_edit_cid=None)
    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{contest_id}")
    back_kb.adjust(1)
    msg = (
        f"✅ <b>Порог победы обновлён</b>\n\n🎯 {format_price(value)}"
        if ok else "❌ Ошибка при обновлении порога"
    )
    await fsm_edit(state, message, msg, reply_markup=back_kb.as_markup())


@contests_router.message(ContestStates.editing_contest_reward)
async def contest_edit_reward_entered(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    contest_id = data.get('ct_edit_cid')
    raw = message.text.strip().replace(' ', '').replace(',', '.')
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        cancel_kb = InlineKeyboardBuilder()
        cancel_kb.button(text="❌ Отмена", callback_data=f"ct_view_{contest_id}")
        await fsm_edit(state, message,
                       "❌ Введите положительное число:",
                       reply_markup=cancel_kb.as_markup())
        return
    current_db = await get_db(message.from_user.id, state)
    ok = await current_db.update_contest(contest_id, reward_value=value)
    await state.set_state(None)
    await state.update_data(ct_edit_cid=None)
    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{contest_id}")
    back_kb.adjust(1)
    msg = (
        f"✅ <b>Награда обновлена</b>\n\n🏅 {format_price(value)}"
        if ok else "❌ Ошибка при обновлении награды"
    )
    await fsm_edit(state, message, msg, reply_markup=back_kb.as_markup())


# ── Ручной ввод результатов конкурса по магазинам ─────────────────────────────

@contests_router.callback_query(F.data.startswith("ct_manual_"))
async def contest_manual_list(callback: CallbackQuery, state: FSMContext):
    """Корректировка фактических показателей конкурса по магазинам"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    contest_id = int(callback.data[len("ct_manual_"):])
    current_db = await get_db(callback.from_user.id, state)
    contest = await current_db.get_contest(contest_id)
    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return
    await callback.answer()

    import json as _json2
    shop_f = contest[10]
    metric = contest[4]
    metric_unit = '₽' if metric == 'turnover' else ' шт'
    start_date = contest[8]
    end_date = contest[9]
    status = contest[16]
    reward_mode = contest[22] if len(contest) > 22 else 'total'

    # Определяем список магазинов конкурса
    if shop_f:
        shops = _json2.loads(shop_f)
    else:
        shops = await current_db.get_all_shops() or []

    builder = InlineKeyboardBuilder()
    status_emoji = "🟢 Идёт" if status == 'active' else "🏁 Завершён"

    # ── Конкурс с тирами (per_sale): корректировка не применима ─────────────
    if reward_mode == 'per_sale':
        builder.button(text="📊 Результаты", callback_data=f"ct_results_{contest_id}")
        builder.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{contest_id}")
        builder.adjust(1)
        await safe_edit_message(
            callback,
            f"✏️ <b>Корректировка показателей</b>\n\n"
            f"🏆 {he(contest[1])}\n"
            f"📅 {_fmt_date(start_date)} — {_fmt_date(end_date)} · {status_emoji}\n\n"
            "ℹ️ <b>Корректировка недоступна для конкурсов с тирами бонусов.</b>\n\n"
            "В таком конкурсе результат рассчитывается по каждой продаже отдельно "
            "с учётом тира (бонус за единицу × количество). Заменить это единым числом "
            "по магазину невозможно без искажения логики тиров.\n\n"
            "Для корректировки внесите нужные продажи вручную через раздел «Продажи».",
            builder.as_markup()
        )
        return

    # ── Конкурс total: авто-итоги с полными фильтрами конкурса ──────────────
    # compute_contest_shop_auto_totals применяет те же фильтры (товар/категория/город/пользователь)
    auto_totals = await current_db.compute_contest_shop_auto_totals(contest_id)
    manual_results = await current_db.get_contest_manual_results(contest_id)

    for shop in shops:
        auto_val = auto_totals.get(shop, 0)
        manual_info = manual_results.get(shop)
        if manual_info:
            corr_str = format_price(manual_info['value']) if metric == 'turnover' else str(int(manual_info['value']))
            label = f"✏️ {shop}: {corr_str}{metric_unit}"
        else:
            auto_str = format_price(auto_val) if metric == 'turnover' else str(int(auto_val))
            label = f"🏪 {shop}: {auto_str}{metric_unit}"
        builder.button(text=label, callback_data=safe_cb("ct_manset_", f"{contest_id}|{shop}"))

    if manual_results:
        builder.button(text="🗑 Сбросить все корректировки",
                       callback_data=f"ct_manreset_{contest_id}")
    builder.button(text="📊 Результаты", callback_data=f"ct_results_{contest_id}")
    builder.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{contest_id}")
    builder.adjust(1)

    text = (
        f"✏️ <b>Корректировка показателей</b>\n\n"
        f"🏆 {he(contest[1])}\n"
        f"📅 {_fmt_date(start_date)} — {_fmt_date(end_date)} · {status_emoji}\n"
        f"📊 Метрика: {'Оборот' if metric == 'turnover' else 'Количество'} ({metric_unit})\n\n"
        "Нажмите на магазин, чтобы скорректировать его показатель.\n"
        "Авто-значение считается по фактическим продажам <b>с учётом всех фильтров конкурса</b> "
        "(товар, категория, город). Корректировка применяется сразу.\n\n"
        "<b>Список магазинов</b> (🏪 авто · ✏️ скорректировано):\n"
    )
    if manual_results:
        text += "\n<b>Активные корректировки:</b>\n"
        for sn, info in manual_results.items():
            auto_val = auto_totals.get(sn, 0)
            auto_str = format_price(auto_val) if metric == 'turnover' else str(int(auto_val))
            corr_str = format_price(info['value']) if metric == 'turnover' else str(int(info['value']))
            editor = he(info['editor']) if info['editor'] else '—'
            text += (
                f"  • {he(sn)}: авто {auto_str} → ✏️ {corr_str}{metric_unit}"
                f" (👤 {editor} · {info['edited_at'][:10]})\n"
            )

    await safe_edit_message(callback, text, builder.as_markup())


@contests_router.callback_query(F.data.startswith("ct_manset_"))
async def contest_manual_set_shop(callback: CallbackQuery, state: FSMContext):
    """Выбран магазин — запрашиваем скорректированное значение"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    raw = callback.data[len("ct_manset_"):]
    current_db = await get_db(callback.from_user.id, state)

    # Восстанавливаем contest_id|shop_name через resolve_cb_name
    all_shops = await current_db.get_all_shops() or []
    all_contests = []
    try:
        def _get_contest_ids():
            import sqlite3 as _sqlite3
            conn_tmp = _sqlite3.connect(current_db.db_file)
            result = [str(r[0]) for r in conn_tmp.execute("SELECT id FROM contests").fetchall()]
            conn_tmp.close()
            return result
        all_contests = await asyncio.to_thread(_get_contest_ids)
    except Exception:
        pass

    candidates = [f"{cid}|{shop}" for cid in all_contests for shop in all_shops]
    resolved = resolve_cb_name(raw, candidates)

    if '|' not in resolved:
        await callback.answer("❌ Ошибка разбора магазина", show_alert=True)
        return

    contest_id_str, shop_name = resolved.split('|', 1)
    contest_id = int(contest_id_str)
    contest = await current_db.get_contest(contest_id)
    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return

    metric = contest[4]
    metric_unit = '₽' if metric == 'turnover' else ' шт'
    start_date = contest[8]
    end_date = contest[9]
    reward_mode = contest[22] if len(contest) > 22 else 'total'

    # per_sale (тиры): корректировка не применима — перенаправляем обратно
    if reward_mode == 'per_sale':
        await callback.answer(
            "Корректировка недоступна для конкурсов с тирами бонусов.",
            show_alert=True
        )
        return

    # Авто-значение магазина с полными фильтрами конкурса (товар/категория/город)
    auto_totals = await current_db.compute_contest_shop_auto_totals(contest_id)
    auto_val = auto_totals.get(shop_name, 0.0)

    # Текущая корректировка (если есть)
    manual_results = await current_db.get_contest_manual_results(contest_id)
    current_manual = manual_results.get(shop_name)

    auto_str = format_price(auto_val) if metric == 'turnover' else str(int(auto_val))

    info_lines = f"\n\n📊 <b>Авто (по продажам конкурса):</b> {auto_str}{metric_unit}"
    if current_manual:
        corr_str = format_price(current_manual['value']) if metric == 'turnover' else str(int(current_manual['value']))
        editor = he(current_manual['editor']) if current_manual['editor'] else '—'
        info_lines += (
            f"\n✏️ <b>Текущая корректировка:</b> {corr_str}{metric_unit}"
            f"\n   👤 {editor} · {current_manual['edited_at'][:10]}"
        )

    await state.update_data(
        ct_manual_cid=contest_id,
        ct_manual_shop=shop_name,
        anchor_msg_id=callback.message.message_id
    )
    await state.set_state(ContestStates.entering_manual_result)

    cancel_kb = InlineKeyboardBuilder()
    if current_manual:
        cancel_kb.button(text="🗑 Снять корректировку (вернуть авто)",
                         callback_data=f"ct_mandel_{contest_id}")
    cancel_kb.button(text="⬅️ Назад", callback_data=f"ct_manual_{contest_id}")
    cancel_kb.adjust(1)

    await callback.answer()
    await callback.message.edit_text(
        f"✏️ <b>Корректировка показателя</b>\n\n"
        f"🏪 <b>{he(shop_name)}</b>{info_lines}\n\n"
        f"Введите скорректированное значение ({metric_unit.strip()}):\n"
        f"<i>Оно сразу заменит авто-подсчёт в результатах конкурса.</i>",
        reply_markup=cancel_kb.as_markup(), parse_mode="HTML"
    )


@contests_router.message(ContestStates.entering_manual_result)
async def contest_manual_value_entered(message: Message, state: FSMContext):
    """Обработка введённого ручного результата"""
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    contest_id = data.get('ct_manual_cid')
    shop_name = data.get('ct_manual_shop')

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Отмена", callback_data=f"ct_manual_{contest_id}")

    try:
        value = float(message.text.replace(',', '.').replace(' ', ''))
        if value < 0:
            raise ValueError
    except ValueError:
        await fsm_edit(state, message,
                       "❌ Введите корректное число (≥ 0):",
                       reply_markup=cancel_kb.as_markup())
        return

    current_db = await get_db(message.from_user.id, state)
    ok = await current_db.set_contest_manual_result(
        contest_id, shop_name, value, message.from_user.id
    )
    await clear_state_keep_org(state)

    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="✏️ Другой магазин", callback_data=f"ct_manual_{contest_id}")
    back_kb.button(text="📊 Результаты", callback_data=f"ct_results_{contest_id}")
    back_kb.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{contest_id}")
    back_kb.adjust(1)

    contest = await current_db.get_contest(contest_id)
    metric = contest[4] if contest else 'turnover'
    metric_unit = '₽' if metric == 'turnover' else ' шт'

    if ok:
        val_str = format_price(value) if metric == 'turnover' else str(int(value))
        await fsm_edit(
            state, message,
            f"✅ <b>Показатель скорректирован</b>\n\n"
            f"🏪 {he(shop_name)}: <b>{val_str}{metric_unit}</b>\n\n"
            "Корректировка применена. Результаты конкурса обновлены.",
            reply_markup=back_kb.as_markup()
        )
    else:
        await fsm_edit(state, message, "❌ Ошибка при сохранении",
                       reply_markup=cancel_kb.as_markup())


@contests_router.callback_query(F.data.startswith("ct_mandel_"))
async def contest_manual_delete(callback: CallbackQuery, state: FSMContext):
    """Снять корректировку — вернуть авто-подсчёт для магазина"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()

    contest_id = int(callback.data[len("ct_mandel_"):])
    data = await state.get_data()
    shop_name = data.get('ct_manual_shop', '')
    await state.set_state(None)

    current_db = await get_db(callback.from_user.id, state)
    await current_db.delete_contest_manual_result(contest_id, shop_name)

    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="✏️ К корректировкам", callback_data=f"ct_manual_{contest_id}")
    back_kb.button(text="📊 Результаты", callback_data=f"ct_results_{contest_id}")
    back_kb.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{contest_id}")
    back_kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Корректировка снята</b>\n\n"
        f"🏪 {he(shop_name)}\n\n"
        "Показатель магазина теперь рассчитывается автоматически по фактическим продажам.",
        reply_markup=back_kb.as_markup(), parse_mode="HTML"
    )


@contests_router.callback_query(F.data.startswith("ct_manreset_"))
async def contest_manual_reset_all(callback: CallbackQuery, state: FSMContext):
    """Сбросить все корректировки — все магазины вернутся на авто-подсчёт"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()

    contest_id = int(callback.data[len("ct_manreset_"):])
    current_db = await get_db(callback.from_user.id, state)
    manual = await current_db.get_contest_manual_results(contest_id)
    count = len(manual)
    for shop_name in list(manual.keys()):
        await current_db.delete_contest_manual_result(contest_id, shop_name)

    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="✏️ Корректировки", callback_data=f"ct_manual_{contest_id}")
    back_kb.button(text="📊 Результаты", callback_data=f"ct_results_{contest_id}")
    back_kb.button(text="⬅️ К конкурсу", callback_data=f"ct_view_{contest_id}")
    back_kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Все корректировки сброшены</b>\n\n"
        f"Снято корректировок: {count}\n\n"
        "Все магазины теперь считаются автоматически по фактическим продажам.",
        reply_markup=back_kb.as_markup(), parse_mode="HTML"
    )
