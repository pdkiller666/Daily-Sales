"""
Обработчики системы окладов и графиков работы.
Поддерживает шаблоны смен по дням недели + ручную корректировку времени на конкретный день.
v2 — rebuild trigger
"""
import calendar as _cal
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from keyboards import InlineKeyboardBuilder, back_button, home_button
from env_manager import env_manager
from utils import format_price, he
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_BTN

salary_router = Router()

_MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]
_WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class SalaryStates(StatesGroup):
    entering_rate = State()


# ── Вспомогательные функции для времени ──────────────────────────────────────

def _fmt_h(h) -> str:
    """Час → строка "10:00"."""
    return f"{int(h)}:00"


def _time_range_str(start_t, end_t) -> str:
    """Диапазон времени → "10:00–19:00" или "—"."""
    if start_t and end_t:
        return f"{start_t}–{end_t}"
    return "—"


def _hour_picker_kb(prefix: str, back_cb: str,
                    min_h: int = 6, max_h: int = 23,
                    day_off_cb: str = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора часа (4 кнопки в строке)."""
    rows = []
    btns = [
        InlineKeyboardButton(text=_fmt_h(h), callback_data=f"{prefix}_{h}")
        for h in range(min_h, max_h + 1)
    ]
    for i in range(0, len(btns), 4):
        rows.append(btns[i:i + 4])
    footer = []
    if day_off_cb:
        footer.append(InlineKeyboardButton(text="🚫 Выходной", callback_data=day_off_cb))
    footer.append(back_button(back_cb))
    rows.append(footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Календарная клавиатура ────────────────────────────────────────────────────

def _calendar_kb(year: int, month: int, worked: set, uid: int = None,
                 editable: bool = False, back_cb: str = "admin_salary_menu",
                 tmpl_uid: int = None, tmpl_yr: int = None,
                 tmpl_mo: int = None) -> InlineKeyboardMarkup:
    """Строит инлайн-клавиатуру-календарь.

    editable=True  (admin): ⬜ → slr_tog (добавить смену),
                             ✅ → slr_day (под-экран управления)
    editable=False (user):  ✅ → my_d (показать время всплывашкой)
    tmpl_uid/yr/mo: если заданы — добавляет кнопку '⏰ Расписание смен'.
    """
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    if editable and uid is not None:
        prev_nav = f"slr_cal_{uid}_{prev_y}_{prev_m}"
        next_nav = f"slr_cal_{uid}_{next_y}_{next_m}"
    else:
        prev_nav = f"my_cal_{prev_y}_{prev_m}"
        next_nav = f"my_cal_{next_y}_{next_m}"

    rows = []
    rows.append([
        InlineKeyboardButton(text="◀️", callback_data=prev_nav),
        InlineKeyboardButton(text=f"📅 {_MONTH_NAMES[month - 1]} {year}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=next_nav),
    ])
    rows.append([InlineKeyboardButton(text=d, callback_data="ignore")
                 for d in _WEEKDAY_NAMES])

    first_wd = _cal.weekday(year, month, 1)
    days_in_month = _cal.monthrange(year, month)[1]
    row = [InlineKeyboardButton(text=" ", callback_data="ignore")] * first_wd

    for d in range(1, days_in_month + 1):
        date_str = f"{year}-{month:02d}-{d:02d}"
        is_w = date_str in worked
        # Используем ✓ (узкий символ) для рабочих дней — двузначные цифры не обрезаются
        mark = "✓" if is_w else ""
        if editable and uid is not None:
            cb = f"slr_day_{uid}_{date_str}" if is_w else f"slr_tog_{uid}_{date_str}"
        else:
            cb = f"my_d_{date_str}" if is_w else "ignore"
        row.append(InlineKeyboardButton(text=f"{mark}{d}", callback_data=cb))
        if len(row) == 7:
            rows.append(row)
            row = []

    if row:
        row += [InlineKeyboardButton(text=" ", callback_data="ignore")] * (7 - len(row))
        rows.append(row)

    rows.append([InlineKeyboardButton(
        text=f"📊 Смен в месяце: {len(worked)}", callback_data="ignore"
    )])

    if tmpl_uid and tmpl_yr and tmpl_mo:
        rows.append([InlineKeyboardButton(
            text="⏰ Расписание смен",
            callback_data=f"slr_tmpl_{tmpl_uid}_{tmpl_yr}_{tmpl_mo}"
        )])

    rows.append([back_button(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Вспомогательный рендер календаря для admin ───────────────────────────────

async def _refresh_admin_calendar(callback: CallbackQuery, state: FSMContext,
                                  admin_uid: int, target_uid: int,
                                  year: int, month: int, current_db) -> None:
    """Перерисовать административный календарь после изменения."""
    user = current_db.get_user_by_id(target_uid)
    name = he(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")
    daily_rate = current_db.get_salary_rate(target_uid)
    worked = current_db.get_work_schedule(target_uid, year, month)
    worked_count = len(worked)
    salary = worked_count * daily_rate
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    text = (
        f"📅 <b>График работы: {name}</b>\n"
        f"{_MONTH_NAMES[month - 1]} {year}\n\n"
        f"⬜ — нажмите чтобы добавить смену\n"
        f"✅ — нажмите для управления сменой\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"📊 Смен отмечено: {worked_count}\n"
        f"💰 Оклад: {format_price(salary)}₽"
    )
    kb = _calendar_kb(year, month, worked, uid=target_uid, editable=True,
                      back_cb="slr_scheds",
                      tmpl_uid=target_uid, tmpl_yr=year, tmpl_mo=month)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ── Нажатие на пустые/нефункциональные кнопки ────────────────────────────────

@salary_router.callback_query(F.data == "ignore")
async def ignore_cb(callback: CallbackQuery):
    await callback.answer()


# ── Главное меню окладов (admin) ──────────────────────────────────────────────

@salary_router.callback_query(F.data == "admin_salary_menu")
async def admin_salary_menu_handler(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 Ставки сотрудников", callback_data="slr_rates")
    builder.button(text="📅 Графики работы", callback_data="slr_scheds")
    now = datetime.now()
    builder.button(text=f"📊 Сводка ФОТ — {_MONTH_NAMES[now.month - 1]} {now.year}",
                   callback_data=f"slr_sum_{now.year}_{now.month}")
    builder.add(back_button("admin_management"))
    builder.add(home_button())
    builder.adjust(1)
    await callback.message.edit_text(
        "💼 <b>Оклады и графики работы</b>\n\n"
        "Управляйте дневными ставками продавцов и контролируйте рабочие смены.\n\n"
        "📌 <b>Принцип расчёта:</b>\n"
        "Оклад = количество отмеченных смен × дневная ставка",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Список ставок ─────────────────────────────────────────────────────────────

def _build_rates_content(rates: list, page: int):
    """Return (text, markup) for salary rates list, paginated."""
    page_items, has_prev, has_next, total_pages, page = paginate(rates, page, PAGE_SIZE_BTN)
    pg_line = f"\n<i>Стр. {page+1}/{total_pages} · всего: {len(rates)}</i>" if total_pages > 1 else ""
    text = f"💵 <b>Ставки сотрудников</b>{pg_line}\n\nНажмите на сотрудника для редактирования:\n\n"
    builder = InlineKeyboardBuilder()
    for user_id, fn, ln, daily_rate, tg_id in page_items:
        name = he(f"{fn} {ln}".strip())
        rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
        text += f"👤 {name} — {rate_str}\n"
        builder.row(InlineKeyboardButton(text=f"✏️ {f'{fn} {ln}'.strip()}", callback_data=f"slr_set_{user_id}"))
    if not rates:
        text += "❌ Нет зарегистрированных продавцов"
    nav = page_nav_row("slr_rates_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(back_button("admin_salary_menu"))
    return text, builder.as_markup()


@salary_router.callback_query(F.data == "slr_rates")
async def salary_rates_list(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    current_db = await get_db(uid, state)
    rates = [r for r in current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    text, markup = _build_rates_content(rates, 0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@salary_router.callback_query(F.data.startswith("slr_rates_pg_"))
async def salary_rates_page(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    page = int(callback.data.removeprefix("slr_rates_pg_"))
    current_db = await get_db(uid, state)
    rates = [r for r in current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    text, markup = _build_rates_content(rates, page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


# ── Установить ставку продавцу ────────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_set_"))
async def salary_set_user(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    target_uid = int(callback.data.split("_")[2])
    current_db = await get_db(uid, state)
    user = current_db.get_user_by_id(target_uid)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    daily_rate = current_db.get_salary_rate(target_uid)
    name_raw = f"{user[2]} {user[3]}".strip()
    name = he(name_raw)
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    await state.update_data(salary_target_uid=target_uid, salary_target_name=name_raw,
                             anchor_msg_id=callback.message.message_id)
    await state.set_state(SalaryStates.entering_rate)
    builder = InlineKeyboardBuilder()
    builder.add(back_button("slr_rates"))
    await callback.message.edit_text(
        f"✏️ <b>Ставка: {name}</b>\n\n"
        f"Текущая ставка: <b>{rate_str}</b>\n\n"
        "Введите новую дневную ставку (₽ за смену).\n"
        "Например: <code>1500</code> или <code>2000.50</code>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@salary_router.message(SalaryStates.entering_rate)
async def salary_rate_enter(message: Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    target_uid = data.get("salary_target_uid")
    name_raw = data.get("salary_target_name", "")
    if not target_uid:
        await fsm_edit(state, message, "❌ Сессия устарела. Откройте меню заново.")
        await clear_state_keep_org(state)
        return
    raw = message.text.strip().replace(",", ".").replace("₽", "").replace(" ", "")
    try:
        rate = float(raw)
        if rate < 0:
            raise ValueError()
    except ValueError:
        builder = InlineKeyboardBuilder()
        builder.add(back_button("slr_rates"))
        await fsm_edit(state, message,
                       "❌ Введите корректное число, например: 1500 или 2000.50",
                       reply_markup=builder.as_markup())
        return
    current_db = await get_db(uid, state)
    current_db.set_salary_rate(target_uid, rate, uid)
    name = he(name_raw)
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 К списку ставок", callback_data="slr_rates")
    builder.button(text="💼 Оклады и смены", callback_data="admin_salary_menu")
    builder.adjust(1)
    await fsm_edit(state, message,
                   f"✅ Ставка <b>{name}</b> обновлена: <b>{format_price(rate)}₽/смену</b>",
                   reply_markup=builder.as_markup(), parse_mode="HTML")
    await clear_state_keep_org(state)


# ── Выбор сотрудника для просмотра графика (admin) ────────────────────────────

def _build_scheds_content(users: list, page: int):
    """Return (text, markup) for schedules list, paginated."""
    now = datetime.now()
    page_items, has_prev, has_next, total_pages, page = paginate(users, page, PAGE_SIZE_BTN)
    pg_line = f"\n<i>Стр. {page+1}/{total_pages} · всего: {len(users)}</i>" if total_pages > 1 else ""
    builder = InlineKeyboardBuilder()
    for user_id, fn, ln, _, tg_id in page_items:
        name = f"{fn} {ln}".strip()
        builder.row(InlineKeyboardButton(
            text=f"📅 {name}",
            callback_data=f"slr_cal_{user_id}_{now.year}_{now.month}"))
    nav = page_nav_row("slr_sched_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(back_button("admin_salary_menu"))
    return f"📅 <b>Графики работы</b>{pg_line}\n\nВыберите сотрудника:", builder.as_markup()


@salary_router.callback_query(F.data == "slr_scheds")
async def salary_schedules_list(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    current_db = await get_db(uid, state)
    users = [r for r in current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    if not users:
        builder = InlineKeyboardBuilder()
        builder.add(back_button("admin_salary_menu"))
        await callback.message.edit_text("❌ Нет зарегистрированных продавцов",
                                         reply_markup=builder.as_markup())
        await callback.answer()
        return
    text, markup = _build_scheds_content(users, 0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@salary_router.callback_query(F.data.startswith("slr_sched_pg_"))
async def salary_sched_page(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    page = int(callback.data.removeprefix("slr_sched_pg_"))
    current_db = await get_db(uid, state)
    users = [r for r in current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    text, markup = _build_scheds_content(users, page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


# ── Календарь сотрудника (admin) ───────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_cal_"))
async def salary_calendar_admin(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    target_uid = int(parts[2])
    year = int(parts[3])
    month = int(parts[4])
    current_db = await get_db(uid, state)
    await callback.answer()
    await _refresh_admin_calendar(callback, state, uid, target_uid, year, month, current_db)


# ── Добавить рабочий день (только для НЕОТМЕЧЕННЫХ дней) ─────────────────────

@salary_router.callback_query(F.data.startswith("slr_tog_"))
async def salary_toggle_day(callback: CallbackQuery, state: FSMContext):
    """Отметить НЕотмеченный день как рабочий — с временем из шаблона дня недели."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_tog_{uid}_{YYYY-MM-DD}
    target_uid = int(parts[2])
    date_str = parts[3]
    current_db = await get_db(uid, state)

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    weekday = date_dt.weekday()  # 0=Пн
    templates = current_db.get_shift_templates(target_uid)
    tmpl = templates.get(weekday)
    start_t = tmpl[0] if tmpl else None
    end_t   = tmpl[1] if tmpl else None

    current_db.add_work_day(target_uid, date_str, start_t, end_t, uid)

    if start_t and end_t:
        await callback.answer(f"✅ {date_str}: {start_t}–{end_t}")
    else:
        await callback.answer(f"✅ {date_str}: рабочая смена")

    year, month = date_dt.year, date_dt.month
    await _refresh_admin_calendar(callback, state, uid, target_uid, year, month, current_db)


# ── Под-экран управления отмеченным днём ─────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_day_"))
async def salary_day_subscreen(callback: CallbackQuery, state: FSMContext):
    """Показывает время смены и кнопки: Снять / Изменить время / Назад."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    # slr_day_{uid}_{YYYY-MM-DD}
    target_uid = int(parts[2])
    date_str   = parts[3]
    current_db = await get_db(uid, state)

    start_t, end_t = current_db.get_work_day_time(target_uid, date_str)
    time_info = _time_range_str(start_t, end_t)

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    day_name = _WEEKDAY_NAMES[date_dt.weekday()]
    date_ru  = f"{day_name}, {date_dt.day} {_MONTH_NAMES[date_dt.month - 1]}"

    user = current_db.get_user_by_id(target_uid)
    name = he(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")

    cal_cb = f"slr_cal_{target_uid}_{date_dt.year}_{date_dt.month}"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⬜ Снять смену",     callback_data=f"slr_rm_{target_uid}_{date_str}"),
        InlineKeyboardButton(text="✏️ Изменить время", callback_data=f"slr_ets_{target_uid}_{date_str}"),
    )
    builder.row(back_button(cal_cb))

    await callback.message.edit_text(
        f"📅 <b>{name}</b> · {date_ru}\n"
        f"⏰ Время смены: <b>{time_info}</b>\n\n"
        "Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


# ── Снять рабочий день ────────────────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_rm_"))
async def salary_remove_day(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_rm_{uid}_{YYYY-MM-DD}
    target_uid = int(parts[2])
    date_str   = parts[3]
    current_db = await get_db(uid, state)
    current_db.remove_work_day(target_uid, date_str)
    await callback.answer(f"⬜ {date_str}: выходной")
    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    await _refresh_admin_calendar(callback, state, uid, target_uid,
                                  date_dt.year, date_dt.month, current_db)


# ── Изменить время конкретного дня: выбор начала ─────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_ets_"))
async def salary_edit_date_start(callback: CallbackQuery, state: FSMContext):
    """Выбор времени начала для конкретной даты."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    # slr_ets_{uid}_{YYYY-MM-DD}
    target_uid = int(parts[2])
    date_str   = parts[3]

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    day_name = _WEEKDAY_NAMES[date_dt.weekday()]
    date_ru  = f"{day_name}, {date_dt.day} {_MONTH_NAMES[date_dt.month - 1]}"

    back_cb = f"slr_day_{target_uid}_{date_str}"
    prefix  = f"slr_ete_{target_uid}_{date_str}"
    kb = _hour_picker_kb(prefix, back_cb, min_h=6, max_h=22)

    await callback.message.edit_text(
        f"⏰ <b>Начало смены</b>\n{date_ru}\n\nВыберите час начала:",
        reply_markup=kb, parse_mode="HTML"
    )


@salary_router.callback_query(F.data.startswith("slr_ete_"))
async def salary_edit_date_end(callback: CallbackQuery, state: FSMContext):
    """Выбор времени конца для конкретной даты."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    # slr_ete_{uid}_{YYYY-MM-DD}_{sh}
    target_uid = int(parts[2])
    date_str   = parts[3]
    start_h    = int(parts[4])

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    day_name = _WEEKDAY_NAMES[date_dt.weekday()]
    date_ru  = f"{day_name}, {date_dt.day} {_MONTH_NAMES[date_dt.month - 1]}"

    back_cb = f"slr_ets_{target_uid}_{date_str}"
    prefix  = f"slr_etx_{target_uid}_{date_str}_{start_h}"
    kb = _hour_picker_kb(prefix, back_cb, min_h=start_h + 1, max_h=23)

    await callback.message.edit_text(
        f"⏰ <b>Конец смены</b>\n{date_ru} · начало {_fmt_h(start_h)}\n\nВыберите час конца:",
        reply_markup=kb, parse_mode="HTML"
    )


@salary_router.callback_query(F.data.startswith("slr_etx_"))
async def salary_edit_date_save(callback: CallbackQuery, state: FSMContext):
    """Сохранить изменённое время для конкретной даты."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_etx_{uid}_{YYYY-MM-DD}_{sh}_{eh}
    target_uid = int(parts[2])
    date_str   = parts[3]
    start_h    = int(parts[4])
    end_h      = int(parts[5])
    current_db = await get_db(uid, state)

    start_t = _fmt_h(start_h)
    end_t   = _fmt_h(end_h)
    current_db.set_work_day_time(target_uid, date_str, start_t, end_t)
    await callback.answer(f"✅ {date_str}: {start_t}–{end_t}")

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    await _refresh_admin_calendar(callback, state, uid, target_uid,
                                  date_dt.year, date_dt.month, current_db)


# ── Шаблон смен по дням недели ───────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_tmpl_"))
async def salary_template_screen(callback: CallbackQuery, state: FSMContext):
    """Экран шаблона: таблица пн-вс с текущими временами."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    # slr_tmpl_{uid}_{yr}_{mo}
    target_uid = int(parts[2])
    yr  = int(parts[3])
    mo  = int(parts[4])
    current_db = await get_db(uid, state)

    templates = current_db.get_shift_templates(target_uid)
    user = current_db.get_user_by_id(target_uid)
    name = he(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")

    lines = [f"⏰ <b>Шаблон смен: {name}</b>\n",
             "Нажмите на день, чтобы изменить время:\n"]
    builder = InlineKeyboardBuilder()
    for wd, wd_name in enumerate(_WEEKDAY_NAMES):
        tmpl = templates.get(wd)
        if tmpl:
            t_str = _time_range_str(tmpl[0], tmpl[1])
        else:
            t_str = "выходной"
        lines.append(f"<b>{wd_name}</b>: {t_str}")
        builder.button(
            text=f"{wd_name} · {t_str}",
            callback_data=f"slr_td_{target_uid}_{wd}_{yr}_{mo}"
        )
    builder.adjust(1)
    builder.add(back_button(f"slr_cal_{target_uid}_{yr}_{mo}"))

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@salary_router.callback_query(F.data.startswith("slr_td_"))
async def salary_template_day_start(callback: CallbackQuery, state: FSMContext):
    """Выбор времени начала для дня недели в шаблоне."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    # slr_td_{uid}_{wd}_{yr}_{mo}
    target_uid = int(parts[2])
    wd  = int(parts[3])
    yr  = int(parts[4])
    mo  = int(parts[5])

    wd_name = _WEEKDAY_NAMES[wd]
    back_cb = f"slr_tmpl_{target_uid}_{yr}_{mo}"
    prefix  = f"slr_ts_{target_uid}_{wd}_{yr}_{mo}"
    day_off_cb = f"slr_tw_{target_uid}_{wd}_{yr}_{mo}"
    kb = _hour_picker_kb(prefix, back_cb, min_h=6, max_h=22, day_off_cb=day_off_cb)

    await callback.message.edit_text(
        f"⏰ <b>Начало смены — {wd_name}</b>\n\nВыберите час начала или установите выходной:",
        reply_markup=kb, parse_mode="HTML"
    )


@salary_router.callback_query(F.data.startswith("slr_tw_"))
async def salary_template_day_off(callback: CallbackQuery, state: FSMContext):
    """Пометить день недели в шаблоне как выходной."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_tw_{uid}_{wd}_{yr}_{mo}
    target_uid = int(parts[2])
    wd  = int(parts[3])
    yr  = int(parts[4])
    mo  = int(parts[5])
    current_db = await get_db(uid, state)
    current_db.set_shift_template(target_uid, wd, None, None)
    await callback.answer(f"🚫 {_WEEKDAY_NAMES[wd]}: выходной сохранён")
    # Сразу возвращаемся в календарь
    await _refresh_admin_calendar(callback, state, uid, target_uid, yr, mo, current_db)


@salary_router.callback_query(F.data.startswith("slr_ts_"))
async def salary_template_day_end(callback: CallbackQuery, state: FSMContext):
    """Выбор времени конца для дня недели в шаблоне."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    # slr_ts_{uid}_{wd}_{yr}_{mo}_{sh}
    target_uid = int(parts[2])
    wd      = int(parts[3])
    yr      = int(parts[4])
    mo      = int(parts[5])
    start_h = int(parts[6])

    wd_name = _WEEKDAY_NAMES[wd]
    back_cb = f"slr_td_{target_uid}_{wd}_{yr}_{mo}"
    prefix  = f"slr_te_{target_uid}_{wd}_{yr}_{mo}_{start_h}"
    kb = _hour_picker_kb(prefix, back_cb, min_h=start_h + 1, max_h=23)

    await callback.message.edit_text(
        f"⏰ <b>Конец смены — {wd_name}</b>\n"
        f"Начало: {_fmt_h(start_h)}\n\nВыберите час конца:",
        reply_markup=kb, parse_mode="HTML"
    )


@salary_router.callback_query(F.data.startswith("slr_te_"))
async def salary_template_day_save(callback: CallbackQuery, state: FSMContext):
    """Сохранить шаблон для дня недели и сразу вернуться к календарю сотрудника."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_te_{uid}_{wd}_{yr}_{mo}_{sh}_{eh}
    target_uid = int(parts[2])
    wd      = int(parts[3])
    yr      = int(parts[4])
    mo      = int(parts[5])
    start_h = int(parts[6])
    end_h   = int(parts[7])
    current_db = await get_db(uid, state)

    start_t = _fmt_h(start_h)
    end_t   = _fmt_h(end_h)
    current_db.set_shift_template(target_uid, wd, start_t, end_t)
    await callback.answer(f"✅ {_WEEKDAY_NAMES[wd]}: {start_t}–{end_t} сохранено")

    # Сразу возвращаемся в календарь (без промежуточного экрана шаблона)
    await _refresh_admin_calendar(callback, state, uid, target_uid, yr, mo, current_db)


# ── Сводка ФОТ за месяц (admin) ───────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_sum_"))
async def salary_summary(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    current_db = await get_db(uid, state)
    raw_summary = current_db.get_team_salary_summary(year, month)
    summary = [r for r in raw_summary
               if not env_manager.is_super_admin(r[7])]
    text = f"📊 <b>Сводка ФОТ — {_MONTH_NAMES[month - 1]} {year}</b>\n\n"
    total_fot = 0
    if not summary:
        text += "❌ Нет данных"
    else:
        for row in summary:
            s_uid, fn, ln, daily_rate, worked_days, salary, shop, tg_id = row
            name = he(f"{fn} {ln}".strip())
            shop_str = f" · {he(shop)}" if shop else ""
            rate_str = f"{format_price(daily_rate)}₽" if daily_rate else "—"
            text += (
                f"👤 <b>{name}</b>{shop_str}\n"
                f"   📅 {worked_days} смен × {rate_str} = <b>{format_price(salary)}₽</b>\n\n"
            )
            total_fot += salary
        text += f"━━━━━━━━━━━━━━━━━━\n💰 <b>Итого ФОТ: {format_price(total_fot)}₽</b>"
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"slr_sum_{prev_y}_{prev_m}"),
        InlineKeyboardButton(text=f"{_MONTH_NAMES[month - 1][:3]} {year}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"slr_sum_{next_y}_{next_m}"),
    )
    builder.add(back_button("admin_salary_menu"))
    builder.adjust(3, 1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


# ── Мой график (продавец) ─────────────────────────────────────────────────────

def _my_schedule_text(month_name: str, year: int, daily_rate: float,
                      worked_count: int, salary: float) -> str:
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    return (
        f"📅 <b>Мой график работы</b>\n"
        f"{month_name} {year}\n\n"
        f"✅ — рабочая смена · нажмите чтобы узнать время\n"
        f"⬜ — выходной\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"📊 Смен отработано: {worked_count}\n"
        f"💰 Оклад к выплате: {format_price(salary)}₽"
    )


@salary_router.callback_query(F.data == "my_schedule")
async def my_schedule(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    await callback.answer()
    now = datetime.now()
    year, month = now.year, now.month
    daily_rate = current_db.get_salary_rate(user_id)
    worked = current_db.get_work_schedule(user_id, year, month)
    worked_count = len(worked)
    salary = worked_count * daily_rate
    text = _my_schedule_text(_MONTH_NAMES[month - 1], year, daily_rate, worked_count, salary)
    kb = _calendar_kb(year, month, worked, editable=False, back_cb="main_menu")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@salary_router.callback_query(F.data.startswith("my_cal_"))
async def my_schedule_nav(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    daily_rate = current_db.get_salary_rate(user_id)
    worked = current_db.get_work_schedule(user_id, year, month)
    worked_count = len(worked)
    salary = worked_count * daily_rate
    text = _my_schedule_text(_MONTH_NAMES[month - 1], year, daily_rate, worked_count, salary)
    kb = _calendar_kb(year, month, worked, editable=False, back_cb="main_menu")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@salary_router.callback_query(F.data.startswith("my_d_"))
async def my_day_detail(callback: CallbackQuery, state: FSMContext):
    """Сотрудник нажимает на отмеченный день — показывает время смены во всплывашке.
    Приоритет: конкретное время из work_schedule → шаблон дня недели → 'не указано'.
    """
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    # my_d_{YYYY-MM-DD}
    date_str = callback.data[5:]
    start_t, end_t = current_db.get_work_day_time(user_id, date_str)

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    day_name = _WEEKDAY_NAMES[date_dt.weekday()]
    date_ru  = f"{day_name}, {date_dt.day} {_MONTH_NAMES[date_dt.month - 1]}"

    # Если у конкретной смены нет времени — берём из шаблона дня недели
    source = ""
    if not (start_t and end_t):
        templates = current_db.get_shift_templates(user_id)
        tmpl = templates.get(date_dt.weekday())
        if tmpl:
            start_t, end_t = tmpl[0], tmpl[1]
            source = " (по шаблону)"

    if start_t and end_t:
        msg = f"📅 {date_ru}\n⏰ Смена: {start_t} – {end_t}{source}"
    else:
        msg = f"📅 {date_ru}\n✅ Рабочая смена · время не указано"

    await callback.answer(msg, show_alert=True)
