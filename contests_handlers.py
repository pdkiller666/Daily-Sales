"""
Обработчики модуля «Конкурсы»
"""
import json as _json
import logging
from datetime import datetime, timedelta
import calendar as _calendar

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from keyboards import InlineKeyboardBuilder, safe_cb, resolve_cb_name, back_button
from env_manager import env_manager
from utils import format_price, he
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit, safe_edit_message

contests_router = Router()
logger = logging.getLogger(__name__)

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
    builder.adjust(1)

    await safe_edit_message(
        callback,
        "🏆 <b>Конкурсы</b>\n\n"
        "Создавайте мотивационные конкурсы для продавцов: по товарам, "
        "категориям, магазинам.\n\n"
        "Победители получают фиксированную надбавку или % от оборота за период.",
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
        anchor_msg_id=callback.message.message_id
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


@contests_router.callback_query(F.data.startswith("ctscp_"))
async def contest_scope_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    scope = callback.data[len("ctscp_"):]
    await state.update_data(ct_contest_type=scope, ct_products=None, ct_categories=None)
    current_db = await get_db(callback.from_user.id, state)

    if scope == 'product':
        products = current_db.get_all_products()
        if not products:
            await callback.answer("❌ Нет товаров в системе", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        for p in products:
            builder.button(text=f"◻️ {p[1]}", callback_data=f"ctprd_{p[0]}")
        builder.button(text="💾 Далее", callback_data="ctprd_done")
        builder.button(text="❌ Отмена", callback_data="contests_menu")
        builder.adjust(1)
        await callback.message.edit_text(
            "📦 <b>Выберите товары для конкурса:</b>",
            reply_markup=builder.as_markup(), parse_mode="HTML"
        )
        await callback.answer()

    elif scope == 'category':
        categories = current_db.get_all_categories()
        if not categories:
            await callback.answer("❌ Нет категорий в системе", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        for cat in categories:
            builder.button(text=f"◻️ {cat}", callback_data=safe_cb("ctcat_", cat))
        builder.button(text="💾 Далее", callback_data="ctcat_done")
        builder.button(text="❌ Отмена", callback_data="contests_menu")
        builder.adjust(1)
        await callback.message.edit_text(
            "📂 <b>Выберите категории для конкурса:</b>",
            reply_markup=builder.as_markup(), parse_mode="HTML"
        )
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
    products = current_db.get_all_products()
    builder = InlineKeyboardBuilder()
    for p in products:
        icon = "✅" if p[0] in selected else "◻️"
        builder.button(text=f"{icon} {p[1]}", callback_data=f"ctprd_{p[0]}")
    builder.button(text="💾 Далее", callback_data="ctprd_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        f"📦 <b>Выберите товары</b> (выбрано: {len(selected)}):",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
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
    raw = callback.data[len("ctcat_"):]
    current_db = await get_db(callback.from_user.id, state)
    categories = current_db.get_all_categories()
    cat = resolve_cb_name(raw, categories)
    if not cat:
        await callback.answer()
        return

    data = await state.get_data()
    selected = list(data.get('ct_categories') or [])
    if cat in selected:
        selected.remove(cat)
    else:
        selected.append(cat)
    await state.update_data(ct_categories=selected)

    builder = InlineKeyboardBuilder()
    for c in categories:
        icon = "✅" if c in selected else "◻️"
        builder.button(text=f"{icon} {c}", callback_data=safe_cb("ctcat_", c))
    builder.button(text="💾 Далее", callback_data="ctcat_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        f"📂 <b>Выберите категории</b> (выбрано: {len(selected)}):",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


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
    metric = callback.data[len("ctmet_"):]
    await state.update_data(ct_metric=metric, anchor_msg_id=callback.message.message_id)
    await state.set_state(ContestStates.entering_target_value)

    unit = "₽ (минимальный оборот для победы)" if metric == 'turnover' else "шт (минимальное кол-во для победы)"
    example = "100000" if metric == 'turnover' else "50"

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="contests_menu")

    await callback.message.edit_text(
        f"🏆 <b>Новый конкурс — шаг 5/8</b>\n\n"
        f"Введите минимальный порог для победы ({unit}):\n"
        f"Пример: <code>{example}</code>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.message(ContestStates.entering_target_value)
async def contest_target_entered(message: Message, state: FSMContext):
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

    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Фиксированная надбавка (₽)", callback_data="ctrwd_fixed")
    builder.button(text="📊 Процент от оборота (%)", callback_data="ctrwd_percent")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)

    await fsm_edit(
        state, message,
        "🏆 <b>Новый конкурс — шаг 6/8</b>\n\nТип награды победителям:",
        reply_markup=builder.as_markup()
    )


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

    await callback.message.edit_text(
        "🏆 <b>Новый конкурс — шаг 7/8</b>\n\nВыберите период проведения конкурса:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


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
            ok = current_db.update_contest(edit_cid, start_date=start_str, end_date=date_str)
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
    shops = current_db.get_all_shops()
    if not shops:
        await callback.answer("❌ Нет магазинов в системе", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get('ct_shops') or []
    builder = InlineKeyboardBuilder()
    for shop in shops:
        icon = "✅" if shop in selected else "◻️"
        builder.button(text=f"{icon} {shop}", callback_data=safe_cb("ctshopchk_", shop))
    builder.button(text="💾 Применить", callback_data="ctshop_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "🏪 Выберите магазины для конкурса:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@contests_router.callback_query(F.data.startswith("ctshopchk_"))
async def contest_toggle_shop(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("ctshopchk_"):]
    current_db = await get_db(callback.from_user.id, state)
    shops = current_db.get_all_shops()
    shop = resolve_cb_name(raw, shops)
    data = await state.get_data()
    selected = list(data.get('ct_shops') or [])
    if shop in selected:
        selected.remove(shop)
    else:
        selected.append(shop)
    await state.update_data(ct_shops=selected if selected else None)
    builder = InlineKeyboardBuilder()
    for s in shops:
        icon = "✅" if s in selected else "◻️"
        builder.button(text=f"{icon} {s}", callback_data=safe_cb("ctshopchk_", s))
    builder.button(text="💾 Применить", callback_data="ctshop_done")
    builder.button(text="❌ Отмена", callback_data="contests_menu")
    builder.adjust(1)
    await callback.message.edit_text(
        f"🏪 Магазины (выбрано: {len(selected)}):",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


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
    all_users = current_db.get_all_users()
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
    all_users = current_db.get_all_users()
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
    target = data.get('ct_target', 0)
    reward_type = data.get('ct_reward_type', 'fixed')
    reward = data.get('ct_reward', 0)
    start = data.get('ct_start', '?')
    end = data.get('ct_end', '?')
    shops = data.get('ct_shops')
    users = data.get('ct_users')
    notify = data.get('ct_notify', 0)

    metric_unit = '₽' if data.get('ct_metric') == 'turnover' else ' шт'
    reward_str = f"{format_price(reward)}₽" if reward_type == 'fixed' else f"{reward}% от оборота"
    shops_str = ", ".join(shops) if shops else "Все магазины"
    users_str = f"{len(users)} чел." if users else "Все сотрудники"

    text = (
        f"✅ <b>Подтверждение создания конкурса</b>\n\n"
        f"🏆 <b>{he(title)}</b>\n"
        + (f"📝 {he(desc)}\n" if desc else "")
        + f"\n🎯 Охват: {scope}\n"
        f"📊 Метрика: {metric}\n"
        f"🎯 Порог победы: {format_price(target)}{metric_unit}\n"
        f"🏅 Награда: {reward_str}\n"
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
    user = current_db.get_user(callback.from_user.id)
    created_by = user[0] if user else None

    contest_id = current_db.create_contest(
        title=data.get('ct_title'),
        description=data.get('ct_description'),
        contest_type=data.get('ct_contest_type', 'any'),
        metric_type=data.get('ct_metric', 'turnover'),
        target_value=data.get('ct_target', 0),
        reward_type=data.get('ct_reward_type', 'fixed'),
        reward_value=data.get('ct_reward', 0),
        start_date=data.get('ct_start'),
        end_date=data.get('ct_end'),
        shop_filter=_json.dumps(data.get('ct_shops'), ensure_ascii=False) if data.get('ct_shops') else None,
        user_filter=_json.dumps(data.get('ct_users')) if data.get('ct_users') else None,
        product_filter=_json.dumps(data.get('ct_products')) if data.get('ct_products') else None,
        category_filter=_json.dumps(data.get('ct_categories'), ensure_ascii=False) if data.get('ct_categories') else None,
        notify_on_start=data.get('ct_notify', 0),
        created_by=created_by
    )

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
        all_users = current_db.get_all_users()
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
                await callback.bot.send_message(tg_id, notif_text, parse_mode="HTML")
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

@contests_router.callback_query(F.data == "contest_list_active")
async def contest_list_active(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    contests = current_db.get_contests(status='active')

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

    text = "🏆 <b>Активные конкурсы</b>\n\n"
    for c in contests:
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
    builder.button(text="🔄 Обновить", callback_data="contest_list_active")
    builder.button(text="➕ Создать", callback_data="contest_create")
    builder.button(text="⬅️ Назад", callback_data="contests_menu")
    builder.adjust(1)

    await safe_edit_message(callback, text, builder.as_markup())


@contests_router.callback_query(F.data == "contest_list_archive")
async def contest_list_archive(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    finished = current_db.get_contests(status='finished')
    cancelled = current_db.get_contests(status='cancelled')
    contests = finished + cancelled

    builder = InlineKeyboardBuilder()
    if not contests:
        builder.button(text="⬅️ Назад", callback_data="contests_menu")
        await safe_edit_message(callback,
                                "📋 <b>Архив конкурсов</b>\n\n❌ Архив пуст",
                                builder.as_markup())
        return

    text = "📋 <b>Архив конкурсов</b>\n\n"
    for c in contests:
        cid = c[0]
        title = c[1]
        start = c[8]
        end = c[9]
        status = c[16]
        icon = "✅" if status == 'finished' else "❌"
        text += f"{icon} <b>{he(title)}</b> ({_fmt_date(start)}–{_fmt_date(end)})\n"
        builder.button(text=f"📊 {title[:35]}", callback_data=f"ct_view_{cid}")
    builder.button(text="🗑️ Очистить архив", callback_data="contest_archive_clear_confirm")
    builder.button(text="⬅️ Назад", callback_data="contests_menu")
    builder.adjust(1)

    await safe_edit_message(callback, text, builder.as_markup())


@contests_router.callback_query(F.data == "contest_archive_clear_confirm")
async def contest_archive_clear_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    finished = current_db.get_contests(status='finished')
    cancelled = current_db.get_contests(status='cancelled')
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
    deleted = current_db.clear_contests_archive()

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
    contest = current_db.get_contest(contest_id)

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

    ctype_labels = {'product': 'Товары', 'category': 'Категории', 'any': 'Все товары'}
    metric_label = {'turnover': 'Оборот', 'quantity': 'Количество'}.get(metric, metric)
    metric_unit = '₽' if metric == 'turnover' else ' шт'
    reward_str = f"{format_price(rval)}₽ (фиксированно)" if rtype == 'fixed' else f"{rval}% от оборота"
    status_labels = {'active': '🟢 Активен', 'finished': '✅ Завершён', 'cancelled': '❌ Отменён'}
    shops_str = ", ".join(_json.loads(shop_f)) if shop_f else "Все"
    cats_str = ", ".join(_json.loads(cat_f)) if cat_f else None

    text = (
        f"🏆 <b>{he(title)}</b>\n"
        + (f"📝 {he(desc)}\n" if desc else "")
        + f"\n📊 Статус: {status_labels.get(status, status)}\n"
        f"📅 Период: {_fmt_date(start)} — {_fmt_date(end)}\n"
        f"🎯 Охват: {ctype_labels.get(ctype, ctype)}\n"
        f"📈 Метрика: {metric_label}\n"
        f"🎯 Порог: {format_price(target)}{metric_unit}\n"
        f"🏅 Награда: {reward_str}\n"
        f"🏪 Магазины: {he(shops_str)}\n"
        + (f"📂 Категории: {he(cats_str)}\n" if cats_str else "")
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Текущие результаты", callback_data=f"ct_results_{cid}")
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
    contest = current_db.get_contest(contest_id)

    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return

    cid = contest[0]
    title = contest[1]
    metric = contest[4]
    start = contest[8]
    end = contest[9]
    status = contest[16]

    results = current_db.compute_contest_results(contest_id)
    metric_unit = '₽' if metric == 'turnover' else ' шт'

    text = f"📊 <b>Результаты: {he(title)}</b>\n📅 {_fmt_date(start)} — {_fmt_date(end)}\n\n"

    if not results:
        text += "❌ Нет данных о продажах за указанный период"
    else:
        winners = [r for r in results if r['is_winner']]
        others = [r for r in results if not r['is_winner']]

        if winners:
            text += "🏆 <b>Победители:</b>\n"
            for i, r in enumerate(winners, 1):
                name = he(f"{r['first_name']} {r['last_name']}".strip())
                actual = format_price(r['actual']) if metric == 'turnover' else str(int(r['actual']))
                reward = format_price(r['reward'])
                text += f"  {i}. {name} — {actual}{metric_unit} → 🏅 +{reward}₽\n"
            text += "\n"

        if others:
            text += "📉 <b>Не достигли порога:</b>\n"
            for r in others[:10]:
                name = he(f"{r['first_name']} {r['last_name']}".strip())
                actual = format_price(r['actual']) if metric == 'turnover' else str(int(r['actual']))
                text += f"  • {name} — {actual}{metric_unit}\n"
            if len(others) > 10:
                text += f"  … и ещё {len(others) - 10}\n"

    winners_list = [r for r in results if r['is_winner']] if results else []

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить результаты", callback_data=f"ct_results_{cid}")
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
    contest = current_db.get_contest(contest_id)
    if not contest:
        await callback.answer("❌ Конкурс не найден", show_alert=True)
        return

    title = contest[1]
    metric = contest[4]
    results = current_db.compute_contest_results(contest_id)
    winners = [r for r in results if r['is_winner']]
    sent = 0
    for r in winners:
        try:
            actual_str = f"{format_price(r['actual'])}₽" if metric == 'turnover' else f"{int(r['actual'])} шт"
            await callback.bot.send_message(
                r['telegram_id'],
                f"🏆 <b>Поздравляем с победой!</b>\n\n"
                f"Вы победили в конкурсе «{he(title)}»!\n\n"
                f"📊 Ваш результат: {actual_str}\n"
                f"🏅 Ваша награда: {format_price(r['reward'])}₽\n\n"
                f"Отличная работа! 🎉",
                parse_mode="HTML"
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
    success = current_db.update_contest_status(contest_id, 'finished')

    if success:
        results = current_db.compute_contest_results(contest_id)
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
    success = current_db.update_contest_status(contest_id, 'cancelled')
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
    contest = current_db.get_contest(contest_id)
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
    contest = current_db.get_contest(contest_id)
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
    contest = current_db.get_contest(contest_id)
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
    ok = current_db.update_contest(contest_id, target_value=value)
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
    ok = current_db.update_contest(contest_id, reward_value=value)
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
