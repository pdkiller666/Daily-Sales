"""
Обработчики системы окладов и графиков работы.
Поддерживает шаблоны смен по дням недели + ручную корректировку времени на конкретный день.
v2 — rebuild trigger
"""
import asyncio
import calendar as _cal
import logging
from datetime import datetime
from aiogram import Router, F

from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from keyboards import InlineKeyboardBuilder, back_button, home_button, _get_web_interface_url
from env_manager import env_manager
from utils import format_price, he
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit
from timezone_utils import get_current_user_time
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_BTN
from states import AdjustmentStates, SearchStates

salary_router = Router()
logger = logging.getLogger(__name__)

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
                 tmpl_mo: int = None,
                 payslip_cb: str = None) -> InlineKeyboardMarkup:
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
        rows.append([InlineKeyboardButton(
            text="🗓 Заполнить месяц по шаблону",
            callback_data=f"slr_fill_{tmpl_uid}_{tmpl_yr}_{tmpl_mo}"
        )])

    if payslip_cb:
        rows.append([InlineKeyboardButton(
            text="📄 Расчётный листок",
            callback_data=payslip_cb
        )])

    rows.append([back_button(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Вспомогательный рендер календаря для admin ───────────────────────────────

async def _refresh_admin_calendar(callback: CallbackQuery, state: FSMContext,
                                  admin_uid: int, target_uid: int,
                                  year: int, month: int, current_db) -> None:
    """Перерисовать административный календарь после изменения."""
    user = await current_db.get_user_by_id(target_uid)
    name = he(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")
    daily_rate = await current_db.get_salary_rate(target_uid)
    worked = await current_db.get_work_schedule(target_uid, year, month)
    # net_worked_count исключает дни, покрытые оплачиваемыми отсутствиями (отпуск,
    # больничный, отгул и т.д.), чтобы не считать их дважды при +paid_abs ниже
    net_worked_count = await current_db.get_worked_days_count(target_uid, year, month)
    non_vac_paid_abs = await current_db.get_paid_absence_days_count(target_uid, year, month, exclude_vacation=True)
    try:
        vac_pay, vac_cal_days, avg_daily, vac_fallback = await current_db.get_vacation_pay_12m(target_uid, year, month)
    except Exception:
        vac_pay, vac_cal_days, avg_daily, vac_fallback = 0.0, 0, 0.0, False
    salary = net_worked_count * daily_rate + non_vac_paid_abs * daily_rate + vac_pay
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    if non_vac_paid_abs > 0:
        shifts_str = f"📊 Смен: {net_worked_count} + оплач.: {non_vac_paid_abs} = {net_worked_count + non_vac_paid_abs}"
    else:
        shifts_str = f"📊 Смен отмечено: {net_worked_count}"
    _fallback_note = " (по текущей ставке)" if vac_fallback else ""
    vac_str = (f"\n🌴 Отпуск: {vac_cal_days} кал.дн. × {format_price(avg_daily)}₽/дн. = {format_price(vac_pay)}₽{_fallback_note}"
               if vac_cal_days > 0 else "")
    text = (
        f"📅 <b>График работы: {name}</b>\n"
        f"{_MONTH_NAMES[month - 1]} {year}\n\n"
        f"⬜ — нажмите чтобы добавить смену\n"
        f"✅ — нажмите для управления сменой\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"{shifts_str}{vac_str}\n"
        f"💰 Оклад: {format_price(salary)}₽"
    )
    kb = _calendar_kb(year, month, worked, uid=target_uid, editable=True,
                      back_cb="slr_scheds",
                      tmpl_uid=target_uid, tmpl_yr=year, tmpl_mo=month,
                      payslip_cb=f"slr_slip_{target_uid}_{year}_{month}")
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
    if not env_manager.is_super_admin(uid):
        from subscription_utils import check_team_permission
        if not check_team_permission(uid):
            await callback.message.edit_text(
                "🔒 <b>Модуль «Команда» не подключён</b>\n\n"
                "Функции управления окладами и графиками работы доступны при активном модуле <b>Команда</b>.\n\n"
                "Подключите модуль в веб-кабинете:\n"
                "<b>Подписка → Модули → 👥 Команда</b>",
                parse_mode="HTML"
            )
            await callback.answer()
            return
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 Ставки сотрудников", callback_data="slr_rates")
    builder.button(text="📅 Графики работы", callback_data="slr_scheds")
    current_db = await get_db(uid, state)
    _tz = await current_db.get_user_timezone(uid)
    now = get_current_user_time(_tz)
    builder.button(text=f"📊 Сводка ФОТ — {_MONTH_NAMES[now.month - 1]} {now.year}",
                   callback_data=f"slr_sum_{now.year}_{now.month}")
    builder.button(text="✏️ Корректировки зарплат", callback_data="slr_adj_menu")
    builder.add(back_button("personnel_hub"))
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
    builder.row(InlineKeyboardButton(text="🔍 Поиск по имени", callback_data="slr_rates_srch_start"))
    builder.row(back_button("admin_salary_menu"))
    return text, builder.as_markup()


@salary_router.callback_query(F.data == "slr_rates")
async def salary_rates_list(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(uid, state)
    rates = [r for r in await current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    text, markup = _build_rates_content(rates, 0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@salary_router.callback_query(F.data.startswith("slr_rates_pg_"))
async def salary_rates_page(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    page = int(callback.data.removeprefix("slr_rates_pg_"))
    await callback.answer()
    current_db = await get_db(uid, state)
    rates = [r for r in await current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    text, markup = _build_rates_content(rates, page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


# ── Установить ставку продавцу ────────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_set_"))
async def salary_set_user(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    target_uid = int(callback.data.split("_")[2])
    current_db = await get_db(uid, state)
    user = await current_db.get_user_by_id(target_uid)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    daily_rate = await current_db.get_salary_rate(target_uid)
    name_raw = f"{user[2]} {user[3]}".strip()
    name = he(name_raw)
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    await state.update_data(salary_target_uid=target_uid, salary_target_name=name_raw,
                             anchor_msg_id=callback.message.message_id)
    await state.set_state(SalaryStates.entering_rate)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 История ставки", callback_data=f"slr_rate_history_{target_uid}"))
    builder.row(back_button("slr_rates"))
    await callback.message.edit_text(
        f"✏️ <b>Ставка: {name}</b>\n\n"
        f"Текущая ставка: <b>{rate_str}</b>\n\n"
        "Введите новую дневную ставку (₽ за смену).\n"
        "Например: <code>1500</code> или <code>2000.50</code>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@salary_router.callback_query(F.data.startswith("slr_rate_history_"))
async def salary_rate_history(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    target_uid = int(callback.data.removeprefix("slr_rate_history_"))
    current_db = await get_db(uid, state)
    user = await current_db.get_user_by_id(target_uid)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    name = he(f"{user[2]} {user[3]}".strip())
    history = await current_db.get_salary_rate_history(target_uid, limit=10)
    if not history:
        text = f"📋 <b>История ставки: {name}</b>\n\n<i>Изменений не найдено.</i>"
    else:
        def _short_date(d: str | None) -> str:
            if not d:
                return "?"
            parts = (d or "")[:10].split("-")
            return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else (d or "")[:10]

        lines = []
        for entry in history:
            ef = _short_date(entry["effective_from"] or entry["created_at"])
            et_raw = entry["effective_to"]
            period = f"{ef}–{_short_date(et_raw)}" if et_raw else f"{ef}–н.в."
            rate_str = f"{format_price(entry['rate'])}₽/смену"
            who = he(entry["changed_by_name"]) if entry["changed_by_name"] else "—"
            lines.append(f"📅 <b>{period}</b> · {rate_str} · {who}")
        text = f"📋 <b>История ставки: {name}</b>\n\n" + "\n".join(lines)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Редактировать ставку", callback_data=f"slr_set_{target_uid}"))
    builder.row(back_button("slr_rates"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
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
    _MAX_SALARY_RATE = 10_000_000
    try:
        rate = float(raw)
        if rate < 0 or rate > _MAX_SALARY_RATE:
            raise ValueError()
    except ValueError:
        builder = InlineKeyboardBuilder()
        builder.add(back_button("slr_rates"))
        await fsm_edit(state, message,
                       f"❌ Введите корректное число от 0 до {_MAX_SALARY_RATE:,} ₽, например: 1500 или 2000.50".replace(",", " "),
                       reply_markup=builder.as_markup())
        return
    current_db = await get_db(uid, state)
    admin_internal_id = await current_db.get_user_id(uid)
    await current_db.set_salary_rate(target_uid, rate, admin_internal_id)
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

def _build_scheds_content(users: list, page: int, now=None):
    """Return (text, markup) for schedules list, paginated."""
    if now is None:
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
    builder.row(InlineKeyboardButton(text="🔍 Поиск по имени", callback_data="slr_scheds_srch_start"))
    builder.row(back_button("admin_salary_menu"))
    return f"📅 <b>Графики работы</b>{pg_line}\n\nВыберите сотрудника:", builder.as_markup()


@salary_router.callback_query(F.data == "slr_scheds")
async def salary_schedules_list(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(uid, state)
    _tz = await current_db.get_user_timezone(uid)
    _now = get_current_user_time(_tz)
    users = [r for r in await current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    if not users:
        builder = InlineKeyboardBuilder()
        builder.add(back_button("admin_salary_menu"))
        await callback.message.edit_text("❌ Нет зарегистрированных продавцов",
                                         reply_markup=builder.as_markup())
        return
    text, markup = _build_scheds_content(users, 0, now=_now)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@salary_router.callback_query(F.data.startswith("slr_sched_pg_"))
async def salary_sched_page(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    page = int(callback.data.removeprefix("slr_sched_pg_"))
    await callback.answer()
    current_db = await get_db(uid, state)
    _tz = await current_db.get_user_timezone(uid)
    _now = get_current_user_time(_tz)
    users = [r for r in await current_db.get_all_salary_rates() if not env_manager.is_super_admin(r[4])]
    text, markup = _build_scheds_content(users, page, now=_now)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


# ── Поиск по ставкам сотрудников ──────────────────────────────────────────────

@salary_router.callback_query(F.data == "slr_rates_srch_start")
async def salary_rates_search_start(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.slr_rates_srch)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖ Отмена", callback_data="slr_rates"))
    await callback.message.edit_text(
        "🔍 <b>Поиск по ставкам</b>\n\nВведите имя или фамилию сотрудника:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@salary_router.message(SearchStates.slr_rates_srch)
async def salary_rates_search_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    query = (message.text or "").strip().lower()
    if not query:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✖ Отмена", callback_data="slr_rates"))
        await fsm_edit(state, message, "⚠️ Введите имя или фамилию.",
                       reply_markup=builder.as_markup())
        return
    current_db = await get_db(uid, state)
    all_rates = [r for r in await current_db.get_all_salary_rates()
                 if not env_manager.is_super_admin(r[4])]
    matches = [r for r in all_rates
               if query in (r[1] or "").lower() or query in (r[2] or "").lower()]
    await state.set_state(None)
    builder = InlineKeyboardBuilder()
    if not matches:
        for r in all_rates[:PAGE_SIZE_BTN]:
            user_id, fn, ln, daily_rate, _ = r
            builder.row(InlineKeyboardButton(
                text=f"✏️ {f'{fn} {ln}'.strip()}",
                callback_data=f"slr_set_{user_id}"))
        builder.row(InlineKeyboardButton(text="🔍 Поиск по имени", callback_data="slr_rates_srch_start"))
        builder.row(back_button("admin_salary_menu"))
        await fsm_edit(state, message,
                       f"🔍 По запросу «<b>{he(query)}</b>» никого не найдено.\n\n"
                       f"💵 <b>Ставки сотрудников</b>\n\nНажмите на сотрудника для редактирования:",
                       reply_markup=builder.as_markup())
        return
    text = f"🔍 По запросу «<b>{he(query)}</b>» найдено: <b>{len(matches)}</b>\n\n"
    for user_id, fn, ln, daily_rate, _ in matches:
        name = he(f"{fn} {ln}".strip())
        rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
        text += f"👤 {name} — {rate_str}\n"
        builder.row(InlineKeyboardButton(text=f"✏️ {f'{fn} {ln}'.strip()}",
                                         callback_data=f"slr_set_{user_id}"))
    builder.row(InlineKeyboardButton(text="🔍 Новый поиск", callback_data="slr_rates_srch_start"))
    builder.row(back_button("slr_rates"))
    await fsm_edit(state, message, text, reply_markup=builder.as_markup())


# ── Поиск по графикам работы ───────────────────────────────────────────────────

@salary_router.callback_query(F.data == "slr_scheds_srch_start")
async def salary_scheds_search_start(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.slr_scheds_srch)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖ Отмена", callback_data="slr_scheds"))
    await callback.message.edit_text(
        "🔍 <b>Поиск по графикам</b>\n\nВведите имя или фамилию сотрудника:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@salary_router.message(SearchStates.slr_scheds_srch)
async def salary_scheds_search_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    query = (message.text or "").strip().lower()
    if not query:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✖ Отмена", callback_data="slr_scheds"))
        await fsm_edit(state, message, "⚠️ Введите имя или фамилию.",
                       reply_markup=builder.as_markup())
        return
    current_db = await get_db(uid, state)
    _tz = await current_db.get_user_timezone(uid)
    all_users = [r for r in await current_db.get_all_salary_rates()
                 if not env_manager.is_super_admin(r[4])]
    matches = [r for r in all_users
               if query in (r[1] or "").lower() or query in (r[2] or "").lower()]
    await state.set_state(None)
    now = get_current_user_time(_tz)
    builder = InlineKeyboardBuilder()
    if not matches:
        for user_id, fn, ln, _, tg_id in all_users[:PAGE_SIZE_BTN]:
            name = f"{fn} {ln}".strip()
            builder.row(InlineKeyboardButton(
                text=f"📅 {name}",
                callback_data=f"slr_cal_{user_id}_{now.year}_{now.month}"))
        builder.row(InlineKeyboardButton(text="🔍 Поиск по имени", callback_data="slr_scheds_srch_start"))
        builder.row(back_button("admin_salary_menu"))
        await fsm_edit(state, message,
                       f"🔍 По запросу «<b>{he(query)}</b>» никого не найдено.\n\n"
                       f"📅 <b>Графики работы</b>\n\nВыберите сотрудника:",
                       reply_markup=builder.as_markup())
        return
    text = f"🔍 По запросу «<b>{he(query)}</b>» найдено: <b>{len(matches)}</b>\n\nВыберите сотрудника:"
    for user_id, fn, ln, _, tg_id in matches:
        name = f"{fn} {ln}".strip()
        builder.row(InlineKeyboardButton(
            text=f"📅 {name}",
            callback_data=f"slr_cal_{user_id}_{now.year}_{now.month}"))
    builder.row(InlineKeyboardButton(text="🔍 Новый поиск", callback_data="slr_scheds_srch_start"))
    builder.row(back_button("slr_scheds"))
    await fsm_edit(state, message, text, reply_markup=builder.as_markup())


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
    await state.update_data(view_year=year, view_month=month)
    current_db = await get_db(uid, state)
    await callback.answer()
    await _refresh_admin_calendar(callback, state, uid, target_uid, year, month, current_db)


# ── Расчётный листок сотрудника (admin) ──────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_slip_"))
async def salary_payslip_admin(callback: CallbackQuery, state: FSMContext):
    """Детализированный расчётный листок: смены × ставка + оплач. отсутствия + отпускные + корректировки."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_slip_{target_uid}_{year}_{month}
    target_uid = int(parts[2])
    year = int(parts[3])
    month = int(parts[4])
    await callback.answer()
    current_db = await get_db(uid, state)

    user = await current_db.get_user_by_id(target_uid)
    name = he(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")
    month_name = _MONTH_NAMES[month - 1]

    daily_rate = await current_db.get_salary_rate(target_uid)
    net_worked_count = await current_db.get_worked_days_count(target_uid, year, month)
    non_vac_paid_abs = await current_db.get_paid_absence_days_count(target_uid, year, month, exclude_vacation=True)
    _vac_data_ok = True
    try:
        vac_pay, vac_cal_days, avg_daily, _ = await current_db.get_vacation_pay_12m(target_uid, year, month)
    except (TypeError, ValueError):
        vac_pay, vac_cal_days, avg_daily = 0.0, 0, 0.0
        _vac_data_ok = False
    except Exception:
        logger.exception("get_vacation_pay_12m failed for uid=%s %s/%s", target_uid, year, month)
        vac_pay, vac_cal_days, avg_daily = 0.0, 0, 0.0
        _vac_data_ok = False

    try:
        adj_rows = await current_db.get_salary_adjustments(target_uid, year, month)
        adj_sum = sum(r[1] for r in adj_rows)
    except Exception:
        adj_rows, adj_sum = [], 0.0

    import calendar as _cal_slip
    _slip_last = _cal_slip.monthrange(year, month)[1]
    _slip_start = f"{year}-{month:02d}-01"
    _slip_end = f"{year}-{month:02d}-{_slip_last}"
    gross_motivation = 0.0
    return_deduction = 0.0
    try:
        gross_motivation, return_deduction = await current_db.get_seller_motivation_summary(
            target_uid, _slip_start, _slip_end)
    except Exception:
        pass

    rate_val = daily_rate or 0  # guard against None for arithmetic
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    shifts_pay = net_worked_count * rate_val
    abs_pay = non_vac_paid_abs * rate_val
    total = shifts_pay + abs_pay + vac_pay + adj_sum + gross_motivation + return_deduction

    lines = [
        f"📄 <b>Расчётный листок: {name}</b>",
        f"📅 {month_name} {year}",
        "",
        f"💼 Ставка: {rate_str}",
        "",
    ]

    # Shifts line — skip multiplication formula when rate is not set
    if not daily_rate:
        lines.append(f"📊 Смен отработано: <b>{net_worked_count}</b>")
    else:
        lines.append(
            f"📊 Смен отработано: {net_worked_count} × {format_price(daily_rate)}₽"
            f" = <b>{format_price(shifts_pay)}₽</b>"
        )

    if non_vac_paid_abs > 0:
        if not daily_rate:
            lines.append(f"🏥 Оплач. отсутствия: <b>{non_vac_paid_abs} дн.</b>")
        else:
            lines.append(
                f"🏥 Оплач. отсутствия: {non_vac_paid_abs} × {format_price(daily_rate)}₽"
                f" = <b>{format_price(abs_pay)}₽</b>"
            )
    if not _vac_data_ok:
        lines.append("🌴 Отпускные: <i>данные недоступны</i>")
    elif vac_cal_days > 0:
        lines.append(
            f"🌴 Отпускные: {vac_cal_days} кал.дн. × {format_price(avg_daily)}₽/дн."
            f" = <b>{format_price(vac_pay)}₽</b>"
        )
    if gross_motivation != 0:
        lines.append(f"🎯 Мотивация: <b>+{format_price(gross_motivation)}₽</b>")
    if return_deduction < 0:
        lines.append(f"↩️ Вычет возвратов: <b>{format_price(return_deduction)}₽</b>")
    if adj_sum != 0:
        sign = "+" if adj_sum > 0 else ""
        lines.append(f"✏️ Корректировки: <b>{sign}{format_price(adj_sum)}₽</b>")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"💰 Итого: <b>{format_price(total)}₽</b>",
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [back_button(f"slr_cal_{target_uid}_{year}_{month}")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


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
    templates = await current_db.get_shift_templates(target_uid)
    tmpl = templates.get(weekday)
    start_t = tmpl[0] if tmpl else None
    end_t   = tmpl[1] if tmpl else None

    await current_db.add_work_day(target_uid, date_str, start_t, end_t, uid)

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

    start_t, end_t = await current_db.get_work_day_time(target_uid, date_str)
    time_info = _time_range_str(start_t, end_t)

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    day_name = _WEEKDAY_NAMES[date_dt.weekday()]
    date_ru  = f"{day_name}, {date_dt.day} {_MONTH_NAMES[date_dt.month - 1]}"

    user = await current_db.get_user_by_id(target_uid)
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
    await current_db.remove_work_day(target_uid, date_str)
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
    await current_db.set_work_day_time(target_uid, date_str, start_t, end_t)
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

    templates = await current_db.get_shift_templates(target_uid)
    user = await current_db.get_user_by_id(target_uid)
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
    await current_db.set_shift_template(target_uid, wd, None, None)
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
    await current_db.set_shift_template(target_uid, wd, start_t, end_t)
    await callback.answer(f"✅ {_WEEKDAY_NAMES[wd]}: {start_t}–{end_t} сохранено")

    # Сразу возвращаемся в календарь (без промежуточного экрана шаблона)
    await _refresh_admin_calendar(callback, state, uid, target_uid, yr, mo, current_db)


# ── Заполнить месяц по шаблону (кнопка на календаре) ─────────────────────────

@salary_router.callback_query(F.data.startswith("slr_fill_"))
async def salary_fill_month_confirm(callback: CallbackQuery, state: FSMContext):
    """Экран подтверждения: показывает шаблон и предлагает заполнить месяц."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_fill_{uid}_{yr}_{mo}
    target_uid = int(parts[2])
    yr = int(parts[3])
    mo = int(parts[4])
    current_db = await get_db(uid, state)

    templates = await current_db.get_shift_templates(target_uid)
    user = await current_db.get_user_by_id(target_uid)
    name = he(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")

    if not templates or all(v[0] is None for v in templates.values()):
        await callback.answer(
            "⚠️ Шаблон пуст — сначала настройте расписание смен",
            show_alert=True
        )
        return

    month_name = _MONTH_NAMES[mo - 1]
    lines = [
        f"🗓 <b>Заполнить {month_name} {yr} по шаблону?</b>\n",
        f"Сотрудник: <b>{name}</b>\n",
        "Будут добавлены рабочие дни по расписанию:\n"
    ]
    for wd, wd_name in enumerate(_WEEKDAY_NAMES):
        tmpl = templates.get(wd)
        if tmpl and tmpl[0]:
            lines.append(f"  <b>{wd_name}</b>: {_time_range_str(tmpl[0], tmpl[1])}")
        else:
            lines.append(f"  <b>{wd_name}</b>: выходной")
    lines.append("\n⚠️ Уже отмеченные дни не будут затронуты.")

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=f"✅ Да, заполнить {month_name}",
        callback_data=f"slr_fillok_{target_uid}_{yr}_{mo}"
    ))
    builder.row(InlineKeyboardButton(
        text="🗓 Применить к нескольким месяцам →",
        callback_data=f"slr_fwd_{target_uid}_{yr}_{mo}"
    ))
    builder.row(back_button(f"slr_cal_{target_uid}_{yr}_{mo}"))

    await callback.answer()
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@salary_router.callback_query(F.data.startswith("slr_fillok_"))
async def salary_fill_month_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнить заполнение месяца по шаблону."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_fillok_{uid}_{yr}_{mo}
    target_uid = int(parts[2])
    yr = int(parts[3])
    mo = int(parts[4])
    current_db = await get_db(uid, state)

    added = await current_db.fill_month_by_template(target_uid, yr, mo, marked_by=uid)
    month_name = _MONTH_NAMES[mo - 1]
    if added:
        await callback.answer(f"✅ Добавлено смен: {added}", show_alert=True)
    else:
        await callback.answer(
            f"ℹ️ {month_name}: все рабочие дни уже отмечены",
            show_alert=True
        )
    await _refresh_admin_calendar(callback, state, uid, target_uid, yr, mo, current_db)


# ── Экран выбора периода для заполнения по шаблону ────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_fwd_"))
async def salary_fill_period_select(callback: CallbackQuery, state: FSMContext):
    """Экран выбора: на сколько месяцев заполнить по шаблону."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    # slr_fwd_{uid}_{yr}_{mo}
    target_uid = int(parts[2])
    yr = int(parts[3])
    mo = int(parts[4])

    user_obj = await (await get_db(uid, state)).get_user_by_id(target_uid)
    name = he(f"{user_obj[2]} {user_obj[3]}".strip() if user_obj else f"id={target_uid}")

    mo2, yr2 = (mo + 1, yr) if mo < 12 else (1, yr + 1)
    mo3, yr3 = (mo2 + 1, yr2) if mo2 < 12 else (1, yr2 + 1)
    # Кол-во месяцев до конца года (включая текущий)
    months_till_dec = 13 - mo

    m1 = _MONTH_NAMES[mo - 1]
    m2 = _MONTH_NAMES[mo2 - 1]
    m3 = _MONTH_NAMES[mo3 - 1]
    m_dec = _MONTH_NAMES[11]  # Декабрь

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=f"📅 Только {m1} {yr}",
        callback_data=f"slr_fwdok_{target_uid}_{yr}_{mo}_1"
    ))
    builder.row(InlineKeyboardButton(
        text=f"📅 {m1} + {m2}",
        callback_data=f"slr_fwdok_{target_uid}_{yr}_{mo}_2"
    ))
    builder.row(InlineKeyboardButton(
        text=f"📅 {m1} + {m2} + {m3}",
        callback_data=f"slr_fwdok_{target_uid}_{yr}_{mo}_3"
    ))
    # «Весь год» — показываем только если охватывает больше 3 месяцев
    if months_till_dec > 3:
        builder.row(InlineKeyboardButton(
            text=f"📅 Весь год ({m1} – {m_dec})",
            callback_data=f"slr_fwdok_{target_uid}_{yr}_{mo}_{months_till_dec}"
        ))
    builder.row(back_button(f"slr_fill_{target_uid}_{yr}_{mo}"))

    await callback.message.edit_text(
        f"🗓 <b>Применить шаблон: {name}</b>\n\n"
        "Выберите период заполнения:\n"
        "<i>Уже отмеченные дни не будут затронуты.</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


# ── Применить шаблон на несколько месяцев ─────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_fwdok_"))
async def salary_fill_forward_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнить заполнение N следующих месяцев по шаблону."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_fwdok_{uid}_{yr}_{mo}_{n}
    target_uid = int(parts[2])
    yr = int(parts[3])
    mo = int(parts[4])
    n  = int(parts[5])
    current_db = await get_db(uid, state)

    total_added = 0
    cur_yr, cur_mo = yr, mo
    for _ in range(n):
        total_added += await current_db.fill_month_by_template(
            target_uid, cur_yr, cur_mo, marked_by=uid
        )
        cur_mo += 1
        if cur_mo > 12:
            cur_mo = 1
            cur_yr += 1

    if total_added:
        await callback.answer(f"✅ Добавлено смен: {total_added}", show_alert=True)
    else:
        await callback.answer("ℹ️ Все рабочие дни уже отмечены", show_alert=True)
    # Возвращаемся к календарю первого из заполненных месяцев
    await _refresh_admin_calendar(callback, state, uid, target_uid, yr, mo, current_db)


# ── Сводка ФОТ за месяц (admin) ───────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_sum_"))
async def salary_summary(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    if not env_manager.is_super_admin(uid):
        from subscription_utils import check_team_permission
        if not check_team_permission(uid):
            await callback.answer("🔒 Требуется модуль «Команда»", show_alert=True)
            return
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    await state.update_data(view_year=year, view_month=month)
    current_db = await get_db(uid, state)
    last_day = _cal.monthrange(year, month)[1]
    month_start = f"{year}-{month:02d}-01"
    month_end = f"{year}-{month:02d}-{last_day}"
    raw_summary = await current_db.get_team_salary_summary(year, month)
    summary = [r for r in raw_summary
               if not env_manager.is_super_admin(r[7])]
    if summary:
        # Параллельно: мотивация, оплаченные отсутствия, отпускные, конкурсы
        motivation_results = await asyncio.gather(
            *[current_db.get_seller_total_earnings(row[0], start_date=month_start, end_date=month_end)
              for row in summary],
            return_exceptions=True
        )
        paid_abs_results = await asyncio.gather(
            *[current_db.get_paid_absence_days_count(row[0], year, month, exclude_vacation=True)
              for row in summary],
            return_exceptions=True
        )
        vac_pay_results = await asyncio.gather(
            *[current_db.get_vacation_pay_12m(row[0], year, month)
              for row in summary],
            return_exceptions=True
        )
        try:
            contest_bulk = await current_db.get_bulk_contest_rewards_by_telegram(month_start, month_end)
        except Exception:
            contest_bulk = {}
    else:
        motivation_results = []
        paid_abs_results = []
        vac_pay_results = []
        contest_bulk = {}
    text = f"📊 <b>Сводка ФОТ — {_MONTH_NAMES[month - 1]} {year}</b>\n\n"
    total_fot = 0
    if not summary:
        text += "❌ Нет данных"
    else:
        for row, earn, paid_abs_r, vac_r in zip(summary, motivation_results, paid_abs_results, vac_pay_results):
            s_uid, fn, ln, daily_rate, worked_days, _salary_raw, shop, tg_id, adj_sum = row
            earn_dict = earn if isinstance(earn, dict) else {}
            motivation = float(earn_dict.get('total_earnings', 0.0) or 0.0)
            plan_coeff = earn_dict.get('plan_coeff')
            non_vac_paid_abs = int(paid_abs_r) if not isinstance(paid_abs_r, Exception) else 0
            vac_pay_i, vac_cal_i, avg_daily_i, vac_fallback_i = (vac_r if isinstance(vac_r, tuple) else (0.0, 0, 0.0, False))
            contest_r = float(contest_bulk.get(tg_id, 0.0) or 0.0)
            # Оклад = смены × ставка + оплач.отсутствия (не отпуск) × ставка + отпускные по ср. ЗП
            rate_val = float(daily_rate or 0)
            salary = rate_val * (worked_days + non_vac_paid_abs) + vac_pay_i
            total_salary = salary + adj_sum + motivation + contest_r
            name = he(f"{fn} {ln}".strip())
            shop_str = f" · {he(shop)}" if shop else ""
            rate_str = f"{format_price(daily_rate)}₽" if daily_rate else "—"
            if non_vac_paid_abs:
                lines = [f"   📅 ({worked_days}+{non_vac_paid_abs} оплач.) × {rate_str} = {format_price(rate_val * (worked_days + non_vac_paid_abs))}₽"]
            else:
                lines = [f"   📅 {worked_days} смен × {rate_str} = {format_price(rate_val * worked_days)}₽"]
            if vac_cal_i > 0:
                _fot_fb_note = " (по тек. ставке)" if vac_fallback_i else ""
                lines.append(f"   🌴 Отпуск: {vac_cal_i} кал.дн. × {format_price(avg_daily_i)}₽/дн. = {format_price(vac_pay_i)}₽{_fot_fb_note}")
            if motivation > 0:
                if plan_coeff is not None and plan_coeff < 1.0:
                    lines.append(f"   🎯 Мотивация: <b>+{format_price(motivation)}₽</b> (план ×{plan_coeff:.2f})")
                else:
                    lines.append(f"   🎯 Мотивация: <b>+{format_price(motivation)}₽</b>")
            if adj_sum > 0:
                lines.append(f"   ➕ Бонус: <b>+{format_price(adj_sum)}₽</b>")
            elif adj_sum < 0:
                lines.append(f"   ➖ Штраф: <b>−{format_price(abs(adj_sum))}₽</b>")
            if contest_r > 0:
                lines.append(f"   🏆 Конкурсы: <b>+{format_price(contest_r)}₽</b>")
            lines.append(f"   💰 <b>Итого: {format_price(total_salary)}₽</b>")
            text += f"👤 <b>{name}</b>{shop_str}\n" + "\n".join(lines) + "\n\n"
            total_fot += total_salary
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
                      worked_count: int, salary: float,
                      paid_abs: int = 0, vac_cal_days: int = 0,
                      avg_daily: float = 0.0, vac_pay: float = 0.0,
                      vac_fallback: bool = False) -> str:
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    if paid_abs > 0:
        shifts_str = f"📊 Смен: {worked_count} + оплач.: {paid_abs} = {worked_count + paid_abs}"
    else:
        shifts_str = f"📊 Смен отработано: {worked_count}"
    _fallback_note = " (по текущей ставке)" if vac_fallback else ""
    vac_str = (f"\n🌴 Отпускные: {vac_cal_days} дней × {format_price(avg_daily)}₽/день = {format_price(vac_pay)}₽{_fallback_note}"
               if vac_cal_days > 0 else "")
    return (
        f"📅 <b>Мой график работы</b>\n"
        f"{month_name} {year}\n\n"
        f"✅ — рабочая смена · нажмите чтобы узнать время\n"
        f"⬜ — выходной\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"{shifts_str}{vac_str}\n"
        f"💰 Оклад к выплате: {format_price(salary)}₽"
    )


@salary_router.callback_query(F.data == "my_schedule")
async def my_schedule(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = await current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    await callback.answer()
    user_tz = await current_db.get_user_timezone(uid)
    now = get_current_user_time(user_tz)
    year, month = now.year, now.month
    daily_rate = await current_db.get_salary_rate(user_id)
    worked = await current_db.get_work_schedule(user_id, year, month)
    net_worked_count = await current_db.get_worked_days_count(user_id, year, month)
    non_vac_paid_abs = await current_db.get_paid_absence_days_count(user_id, year, month, exclude_vacation=True)
    try:
        vac_pay, vac_cal_days, avg_daily, vac_fallback = await current_db.get_vacation_pay_12m(user_id, year, month)
    except Exception:
        vac_pay, vac_cal_days, avg_daily, vac_fallback = 0.0, 0, 0.0, False
    salary = net_worked_count * daily_rate + non_vac_paid_abs * daily_rate + vac_pay
    text = _my_schedule_text(_MONTH_NAMES[month - 1], year, daily_rate, net_worked_count, salary,
                             non_vac_paid_abs, vac_cal_days, avg_daily, vac_pay, vac_fallback)
    kb = _calendar_kb(year, month, worked, editable=False, back_cb="main_menu",
                      payslip_cb=f"my_slip_{year}_{month}" if daily_rate else None)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@salary_router.callback_query(F.data.startswith("my_cal_"))
async def my_schedule_nav(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = await current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    daily_rate = await current_db.get_salary_rate(user_id)
    worked = await current_db.get_work_schedule(user_id, year, month)
    net_worked_count = await current_db.get_worked_days_count(user_id, year, month)
    non_vac_paid_abs = await current_db.get_paid_absence_days_count(user_id, year, month, exclude_vacation=True)
    try:
        vac_pay, vac_cal_days, avg_daily, vac_fallback = await current_db.get_vacation_pay_12m(user_id, year, month)
    except Exception:
        vac_pay, vac_cal_days, avg_daily, vac_fallback = 0.0, 0, 0.0, False
    salary = net_worked_count * daily_rate + non_vac_paid_abs * daily_rate + vac_pay
    text = _my_schedule_text(_MONTH_NAMES[month - 1], year, daily_rate, net_worked_count, salary,
                             non_vac_paid_abs, vac_cal_days, avg_daily, vac_pay, vac_fallback)
    kb = _calendar_kb(year, month, worked, editable=False, back_cb="main_menu",
                      payslip_cb=f"my_slip_{year}_{month}" if daily_rate else None)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@salary_router.callback_query(F.data.startswith("my_slip_"))
async def my_payslip(callback: CallbackQuery, state: FSMContext):
    """Расчётный листок для самого сотрудника — та же детализация, что у admin, но без кнопок удаления."""
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = await current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    await callback.answer()
    # my_slip_{year}_{month}
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    month_name = _MONTH_NAMES[month - 1]

    daily_rate = await current_db.get_salary_rate(user_id)
    net_worked_count = await current_db.get_worked_days_count(user_id, year, month)
    non_vac_paid_abs = await current_db.get_paid_absence_days_count(user_id, year, month, exclude_vacation=True)
    try:
        vac_pay, vac_cal_days, avg_daily, _ = await current_db.get_vacation_pay_12m(user_id, year, month)
    except Exception:
        vac_pay, vac_cal_days, avg_daily = 0.0, 0, 0.0

    try:
        adj_rows = await current_db.get_salary_adjustments(user_id, year, month)
        adj_sum = sum(r[1] for r in adj_rows)
    except Exception:
        adj_rows, adj_sum = [], 0.0

    import calendar as _cal_slip
    _slip_last = _cal_slip.monthrange(year, month)[1]
    _slip_start = f"{year}-{month:02d}-01"
    _slip_end = f"{year}-{month:02d}-{_slip_last}"
    gross_motivation = 0.0
    return_deduction = 0.0
    try:
        gross_motivation, return_deduction = await current_db.get_seller_motivation_summary(
            user_id, _slip_start, _slip_end)
    except Exception:
        pass

    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    shifts_pay = net_worked_count * daily_rate
    abs_pay = non_vac_paid_abs * daily_rate
    total = shifts_pay + abs_pay + vac_pay + adj_sum + gross_motivation + return_deduction

    lines = [
        f"📄 <b>Мой расчётный листок</b>",
        f"📅 {month_name} {year}",
        "",
        f"💼 Ставка: {rate_str}",
        "",
        f"📊 Смен отработано: {net_worked_count} × {format_price(daily_rate)}₽ = <b>{format_price(shifts_pay)}₽</b>",
    ]
    if non_vac_paid_abs > 0:
        lines.append(
            f"🏥 Оплач. отсутствия: {non_vac_paid_abs} × {format_price(daily_rate)}₽ = <b>{format_price(abs_pay)}₽</b>"
        )
    if vac_cal_days > 0:
        lines.append(
            f"🌴 Отпускные: {vac_cal_days} кал.дн. × {format_price(avg_daily)}₽/дн. = <b>{format_price(vac_pay)}₽</b>"
        )
    if gross_motivation != 0:
        lines.append(f"🎯 Мотивация: <b>+{format_price(gross_motivation)}₽</b>")
    if return_deduction < 0:
        lines.append(f"↩️ Вычет возвратов: <b>{format_price(return_deduction)}₽</b>")
    if adj_rows:
        lines.append("")
        lines.append("✏️ <b>Корректировки:</b>")
        for r in adj_rows:
            amt = r[1]
            comment = r[2] or ""
            sign = "+" if amt >= 0 else ""
            comment_part = f" — {he(comment)}" if comment else ""
            lines.append(f"  {sign}{format_price(amt)}₽{comment_part}")
        sign = "+" if adj_sum > 0 else ""
        lines.append(f"  <i>Итого корректировок: {sign}{format_price(adj_sum)}₽</i>")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"💰 Итого: <b>{format_price(total)}₽</b>",
    ]

    kb_rows = []
    web_base = _get_web_interface_url()
    if web_base:
        web_url = f"{web_base.rstrip('/')}/salary/my-slip?year={year}&month={month}"
        kb_rows.append([InlineKeyboardButton(text="🌐 Открыть в веб-кабинете", url=web_url)])
    kb_rows.append([back_button(f"my_cal_{year}_{month}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@salary_router.callback_query(F.data.startswith("my_d_"))
async def my_day_detail(callback: CallbackQuery, state: FSMContext):
    """Сотрудник нажимает на отмеченный день — показывает время смены во всплывашке.
    Приоритет: конкретное время из work_schedule → шаблон дня недели → 'не указано'.
    """
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = await current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    # my_d_{YYYY-MM-DD}
    date_str = callback.data[5:]
    start_t, end_t = await current_db.get_work_day_time(user_id, date_str)

    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    day_name = _WEEKDAY_NAMES[date_dt.weekday()]
    date_ru  = f"{day_name}, {date_dt.day} {_MONTH_NAMES[date_dt.month - 1]}"

    # Если у конкретной смены нет времени — берём из шаблона дня недели
    source = ""
    if not (start_t and end_t):
        templates = await current_db.get_shift_templates(user_id)
        tmpl = templates.get(date_dt.weekday())
        if tmpl:
            start_t, end_t = tmpl[0], tmpl[1]
            source = " (по шаблону)"

    if start_t and end_t:
        msg = f"📅 {date_ru}\n⏰ Смена: {start_t} – {end_t}{source}"
    else:
        msg = f"📅 {date_ru}\n✅ Рабочая смена · время не указано"

    await callback.answer(msg, show_alert=True)


# ── Корректировки зарплат ──────────────────────────────────────────────────────

def _adj_back_kb(uid: int, year: int, month: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"slr_adj_u_{uid}_{year}_{month}")]
    ])


async def _show_adj_list(callback: CallbackQuery, state: FSMContext, db, uid: int, name: str, year: int, month: int):
    """Показать список корректировок сотрудника за месяц."""
    rows = await db.get_salary_adjustments(uid, year, month)
    adj_sum = sum(r[1] for r in rows)
    month_name = _MONTH_NAMES[month - 1]

    daily_rate = net_worked_count = non_vac_paid_abs = base_salary = None
    vac_cal_days_adj = avg_daily_adj = 0
    try:
        daily_rate = await db.get_salary_rate(uid)
        net_worked_count = await db.get_worked_days_count(uid, year, month)
        non_vac_paid_abs = await db.get_paid_absence_days_count(uid, year, month, exclude_vacation=True)
        vac_pay_adj, vac_cal_days_adj, avg_daily_adj, vac_fallback_adj = await db.get_vacation_pay_12m(uid, year, month)
        base_salary = net_worked_count * daily_rate + non_vac_paid_abs * daily_rate + vac_pay_adj
    except Exception:
        vac_fallback_adj = False

    text = f"✏️ <b>Корректировки: {he(name)}</b>\n📅 {month_name} {year}\n\n"

    if daily_rate is not None and daily_rate > 0:
        rate_str = f"{format_price(daily_rate)}₽/смену"
        if non_vac_paid_abs and non_vac_paid_abs > 0:
            shifts_str = f"Смен: {net_worked_count} + оплач.: {non_vac_paid_abs} = {net_worked_count + non_vac_paid_abs}"
        else:
            shifts_str = f"Смен отработано: {net_worked_count}"
        _fallback_note_adj = " (по текущей ставке)" if vac_fallback_adj else ""
        vac_line = (f"\n🌴 Отпуск: {vac_cal_days_adj} кал.дн. × {format_price(avg_daily_adj)}₽/дн.{_fallback_note_adj}"
                    if vac_cal_days_adj > 0 else "")
        text += (
            f"💼 Ставка: {rate_str}\n"
            f"📊 {shifts_str}{vac_line}\n"
            f"💰 Оклад: {format_price(base_salary)}₽\n\n"
        )

    if rows:
        for r in rows:
            adj_id, amount, comment, _, created_at, creator_name = r
            sign = "+" if amount >= 0 else ""
            dt = str(created_at)[:16] if created_at else "—"
            comment_str = f" · {he(comment)}" if comment else ""
            text += f"• {sign}{format_price(amount)}₽{comment_str}\n  🕐 {dt} · {he(creator_name)}\n"
            text += f"  /del_adj_{adj_id}\n\n"
        sign_sum = "+" if adj_sum >= 0 else ""
        text += f"━━━━━━━━━━━━━━━━━━\n📊 Итог корректировок: <b>{sign_sum}{format_price(adj_sum)}₽</b>"
    else:
        text += "Нет корректировок за этот месяц."
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"slr_adj_u_{uid}_{prev_y}_{prev_m}"),
        InlineKeyboardButton(text=f"{month_name[:3]} {year}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"slr_adj_u_{uid}_{next_y}_{next_m}"),
    )
    builder.row(InlineKeyboardButton(text="➕ Добавить корректировку", callback_data=f"slr_adj_add_{uid}_{year}_{month}"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="slr_adj_menu"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@salary_router.callback_query(F.data == "slr_adj_menu")
async def slr_adj_menu(callback: CallbackQuery, state: FSMContext):
    """Выбор сотрудника для просмотра/добавления корректировок."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    if not env_manager.is_super_admin(uid):
        from subscription_utils import check_team_permission
        if not check_team_permission(uid):
            await callback.answer("🔒 Требуется модуль «Команда»", show_alert=True)
            return
    current_db = await get_db(uid, state)
    user_tz = await current_db.get_user_timezone(uid)
    now = get_current_user_time(user_tz)
    summary = await current_db.get_team_salary_summary(now.year, now.month)
    employees = [r for r in summary if not env_manager.is_super_admin(r[7])]
    if not employees:
        await callback.answer("Нет сотрудников с окладами", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for row in employees:
        e_uid, fn, ln, daily_rate, worked_days, salary, shop, tg_id, adj_sum = row
        name = f"{fn} {ln}".strip()
        builder.row(InlineKeyboardButton(
            text=f"👤 {name}",
            callback_data=f"slr_adj_u_{e_uid}_{now.year}_{now.month}"
        ))
    builder.row(back_button("admin_salary_menu"))
    await callback.message.edit_text(
        "✏️ <b>Корректировки зарплат</b>\n\nВыберите сотрудника:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@salary_router.callback_query(F.data.startswith("slr_adj_u_"))
async def slr_adj_user(callback: CallbackQuery, state: FSMContext):
    """Список корректировок конкретного сотрудника за выбранный месяц."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_adj_u_{uid}_{year}_{month}
    e_uid, year, month = int(parts[3]), int(parts[4]), int(parts[5])
    await state.update_data(view_year=year, view_month=month)
    current_db = await get_db(uid, state)
    user = await current_db.get_user_by_id(e_uid)
    name = f"{user[2]} {user[3]}".strip() if user else f"ID {e_uid}"
    await _show_adj_list(callback, state, current_db, e_uid, name, year, month)


@salary_router.callback_query(F.data.startswith("slr_adj_add_"))
async def slr_adj_add_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления корректировки: запрашиваем сумму."""
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # slr_adj_add_{uid}_{year}_{month}
    e_uid, year, month = int(parts[3]), int(parts[4]), int(parts[5])
    await state.update_data(adj_target_uid=e_uid, adj_year=year, adj_month=month)
    await state.set_state(AdjustmentStates.entering_amount)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"slr_adj_u_{e_uid}_{year}_{month}")]
    ])
    month_name = _MONTH_NAMES[month - 1]
    await callback.message.edit_text(
        f"✏️ <b>Новая корректировка — {month_name} {year}</b>\n\n"
        "Введите сумму:\n"
        "• Бонус: <code>+1000</code> или просто <code>1000</code>\n"
        "• Штраф: <code>-500</code>",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await callback.answer()


@salary_router.message(AdjustmentStates.entering_amount)
async def slr_adj_amount_handler(message: Message, state: FSMContext):
    """Обработка суммы корректировки."""
    data = await state.get_data()
    e_uid, year, month = data.get('adj_target_uid'), data.get('adj_year'), data.get('adj_month')
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"slr_adj_u_{e_uid}_{year}_{month}")]
    ])
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
    except (ValueError, AttributeError):
        await fsm_edit(state, message, "❌ Введите число, например <code>+500</code> или <code>-200</code>",
                       reply_markup=cancel_kb)
        return
    if amount == 0:
        await fsm_edit(state, message, "❌ Сумма не может быть 0. Введите ненулевое число:",
                       reply_markup=cancel_kb)
        return
    await state.update_data(adj_amount=amount)
    await state.set_state(AdjustmentStates.entering_comment)
    skip_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Пропустить", callback_data="slr_adj_skip_comment")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"slr_adj_u_{e_uid}_{year}_{month}")]
    ])
    sign = "+" if amount > 0 else ""
    await fsm_edit(state, message,
                   f"✅ Сумма: <b>{sign}{format_price(amount)}₽</b>\n\n"
                   "Введите комментарий (или пропустите):",
                   reply_markup=skip_kb)


@salary_router.callback_query(F.data == "slr_adj_skip_comment")
async def slr_adj_skip_comment(callback: CallbackQuery, state: FSMContext):
    """Пропустить ввод комментария и сохранить корректировку."""
    await callback.answer()
    data = await state.get_data()
    e_uid, year, month, amount = data.get('adj_target_uid'), data.get('adj_year'), data.get('adj_month'), data.get('adj_amount')
    current_db = await get_db(callback.from_user.id, state)
    admin_id = await current_db.get_user_id(callback.from_user.id)
    await current_db.add_salary_adjustment(e_uid, year, month, amount, comment=None, created_by=admin_id)
    await clear_state_keep_org(state)
    user = await current_db.get_user_by_id(e_uid)
    name = f"{user[2]} {user[3]}".strip() if user else f"ID {e_uid}"
    await _show_adj_list(callback, state, current_db, e_uid, name, year, month)


@salary_router.message(AdjustmentStates.entering_comment)
async def slr_adj_comment_handler(message: Message, state: FSMContext):
    """Обработка комментария и сохранение корректировки."""
    data = await state.get_data()
    e_uid, year, month, amount = data.get('adj_target_uid'), data.get('adj_year'), data.get('adj_month'), data.get('adj_amount')
    comment = message.text.strip()[:200]
    current_db = await get_db(message.from_user.id, state)
    admin_id = await current_db.get_user_id(message.from_user.id)
    await current_db.add_salary_adjustment(e_uid, year, month, amount, comment=comment, created_by=admin_id)
    await clear_state_keep_org(state)
    user = await current_db.get_user_by_id(e_uid)
    name = f"{user[2]} {user[3]}".strip() if user else f"ID {e_uid}"
    # Re-send via fake callback workaround: edit anchor message
    data2 = await state.get_data()
    anchor_id = data2.get('anchor_msg_id')
    rows = await current_db.get_salary_adjustments(e_uid, year, month)
    adj_sum = sum(r[1] for r in rows)
    month_name = _MONTH_NAMES[month - 1]
    text = f"✏️ <b>Корректировки: {he(name)}</b>\n📅 {month_name} {year}\n\n"
    for r in rows:
        adj_id, _amount, _comment, _, created_at, creator_name = r
        sign = "+" if _amount >= 0 else ""
        dt = str(created_at)[:16] if created_at else "—"
        comment_str = f" · {he(_comment)}" if _comment else ""
        text += f"• {sign}{format_price(_amount)}₽{comment_str}\n  🕐 {dt} · {he(creator_name)}\n"
        text += f"  /del_adj_{adj_id}\n\n"
    sign_sum = "+" if adj_sum >= 0 else ""
    text += f"━━━━━━━━━━━━━━━━━━\n📊 Итог корректировок: <b>{sign_sum}{format_price(adj_sum)}₽</b>"
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"slr_adj_u_{e_uid}_{prev_y}_{prev_m}"),
        InlineKeyboardButton(text=f"{month_name[:3]} {year}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"slr_adj_u_{e_uid}_{next_y}_{next_m}"),
    )
    builder.row(InlineKeyboardButton(text="➕ Добавить корректировку", callback_data=f"slr_adj_add_{e_uid}_{year}_{month}"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="slr_adj_menu"))
    if anchor_id:
        try:
            await message.bot.edit_message_text(
                text, chat_id=message.chat.id, message_id=anchor_id,
                reply_markup=builder.as_markup(), parse_mode="HTML"
            )
            await message.delete()
        except Exception:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@salary_router.message(F.text.startswith("/del_adj_"))
async def slr_adj_delete(message: Message, state: FSMContext):
    """Удаление корректировки по команде /del_adj_{id}."""
    uid = message.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        return
    try:
        adj_id = int(message.text.replace("/del_adj_", "").strip())
    except ValueError:
        return
    current_db = await get_db(uid, state)
    # Получаем данные корректировки перед удалением
    conn = current_db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, year, month FROM salary_adjustments WHERE id = ?', (adj_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Корректировка не найдена")
        return
    e_uid, year, month = row
    await current_db.delete_salary_adjustment(adj_id)
    user = await current_db.get_user_by_id(e_uid)
    name = f"{user[2]} {user[3]}".strip() if user else f"ID {e_uid}"
    rows = await current_db.get_salary_adjustments(e_uid, year, month)
    adj_sum = sum(r[1] for r in rows)
    month_name = _MONTH_NAMES[month - 1]
    text = f"✏️ <b>Корректировки: {he(name)}</b>\n📅 {month_name} {year}\n\n"
    if rows:
        for r in rows:
            rid, _amount, _comment, _, created_at, creator_name = r
            sign = "+" if _amount >= 0 else ""
            dt = str(created_at)[:16] if created_at else "—"
            comment_str = f" · {he(_comment)}" if _comment else ""
            text += f"• {sign}{format_price(_amount)}₽{comment_str}\n  🕐 {dt} · {he(creator_name)}\n"
            text += f"  /del_adj_{rid}\n\n"
        sign_sum = "+" if adj_sum >= 0 else ""
        text += f"━━━━━━━━━━━━━━━━━━\n📊 Итог: <b>{sign_sum}{format_price(adj_sum)}₽</b>"
    else:
        text += "Нет корректировок за этот месяц."
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"slr_adj_u_{e_uid}_{prev_y}_{prev_m}"),
        InlineKeyboardButton(text=f"{month_name[:3]} {year}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"slr_adj_u_{e_uid}_{next_y}_{next_m}"),
    )
    builder.row(InlineKeyboardButton(text="➕ Добавить корректировку", callback_data=f"slr_adj_add_{e_uid}_{year}_{month}"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="slr_adj_menu"))
    data = await state.get_data()
    anchor_id = data.get('anchor_msg_id')
    if anchor_id:
        try:
            await message.bot.edit_message_text(
                text, chat_id=message.chat.id, message_id=anchor_id,
                reply_markup=builder.as_markup(), parse_mode="HTML"
            )
            await message.delete()
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
