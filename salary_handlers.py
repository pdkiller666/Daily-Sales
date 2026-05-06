"""
Обработчики системы окладов и графиков работы
"""
import calendar as _cal
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from keyboards import InlineKeyboardBuilder, back_button
from env_manager import env_manager
from utils import format_price, escape_md
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit

salary_router = Router()

_MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]


class SalaryStates(StatesGroup):
    entering_rate = State()


def _calendar_kb(year: int, month: int, worked: set, uid: int = None,
                 editable: bool = False, back_cb: str = "admin_salary_menu") -> InlineKeyboardMarkup:
    """Строит инлайн-клавиатуру-календарь."""
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
                 for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])

    first_wd = _cal.weekday(year, month, 1)
    days_in_month = _cal.monthrange(year, month)[1]
    row = [InlineKeyboardButton(text=" ", callback_data="ignore")] * first_wd

    for d in range(1, days_in_month + 1):
        date_str = f"{year}-{month:02d}-{d:02d}"
        is_w = date_str in worked
        emoji = "✅" if is_w else "⬜"
        cb = f"slr_tog_{uid}_{date_str}" if (editable and uid is not None) else "ignore"
        row.append(InlineKeyboardButton(text=f"{emoji}{d}", callback_data=cb))
        if len(row) == 7:
            rows.append(row)
            row = []

    if row:
        row += [InlineKeyboardButton(text=" ", callback_data="ignore")] * (7 - len(row))
        rows.append(row)

    rows.append([InlineKeyboardButton(text=f"📊 Смен в месяце: {len(worked)}", callback_data="ignore")])
    rows.append([back_button(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    builder.adjust(1)
    await callback.message.edit_text(
        "💼 *Оклады и графики работы*\n\n"
        "Управляйте дневными ставками продавцов и контролируйте рабочие смены.\n\n"
        "📌 *Принцип расчёта:*\n"
        "Оклад = количество отмеченных смен × дневная ставка",
        reply_markup=builder.as_markup(), parse_mode="Markdown"
    )
    await callback.answer()


# ── Список ставок ─────────────────────────────────────────────────────────────

@salary_router.callback_query(F.data == "slr_rates")
async def salary_rates_list(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    current_db = await get_db(uid, state)
    rates = [r for r in current_db.get_all_salary_rates()
             if not env_manager.is_super_admin(r[4])]
    builder = InlineKeyboardBuilder()
    text = "💵 *Ставки сотрудников*\n\nНажмите на сотрудника для редактирования:\n\n"
    for user_id, fn, ln, daily_rate, tg_id in rates:
        name = escape_md(f"{fn} {ln}".strip())
        rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
        text += f"👤 {name} — {rate_str}\n"
        builder.button(text=f"✏️ {f'{fn} {ln}'.strip()}", callback_data=f"slr_set_{user_id}")
    if not rates:
        text += "❌ Нет зарегистрированных продавцов"
    builder.add(back_button("admin_salary_menu"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
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
    name = escape_md(name_raw)
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    await state.update_data(salary_target_uid=target_uid, salary_target_name=name_raw,
                             anchor_msg_id=callback.message.message_id)
    await state.set_state(SalaryStates.entering_rate)
    builder = InlineKeyboardBuilder()
    builder.add(back_button("slr_rates"))
    await callback.message.edit_text(
        f"✏️ *Ставка: {name}*\n\n"
        f"Текущая ставка: *{rate_str}*\n\n"
        "Введите новую дневную ставку (₽ за смену).\n"
        "Например: `1500` или `2000.50`",
        reply_markup=builder.as_markup(), parse_mode="Markdown"
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
    name = escape_md(name_raw)
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 К списку ставок", callback_data="slr_rates")
    builder.button(text="💼 Оклады и смены", callback_data="admin_salary_menu")
    builder.adjust(1)
    await fsm_edit(state, message,
                   f"✅ Ставка *{name}* обновлена: *{format_price(rate)}₽/смену*",
                   reply_markup=builder.as_markup(), parse_mode="Markdown")
    await clear_state_keep_org(state)


# ── Выбор сотрудника для просмотра графика (admin) ────────────────────────────

@salary_router.callback_query(F.data == "slr_scheds")
async def salary_schedules_list(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    current_db = await get_db(uid, state)
    users = [r for r in current_db.get_all_salary_rates()
             if not env_manager.is_super_admin(r[4])]
    if not users:
        builder = InlineKeyboardBuilder()
        builder.add(back_button("admin_salary_menu"))
        await callback.message.edit_text(
            "❌ Нет зарегистрированных продавцов",
            reply_markup=builder.as_markup()
        )
        await callback.answer()
        return
    now = datetime.now()
    builder = InlineKeyboardBuilder()
    for user_id, fn, ln, _, tg_id in users:
        name = f"{fn} {ln}".strip()
        builder.button(text=f"📅 {name}",
                       callback_data=f"slr_cal_{user_id}_{now.year}_{now.month}")
    builder.add(back_button("admin_salary_menu"))
    builder.adjust(1)
    await callback.message.edit_text(
        "📅 *Графики работы*\n\nВыберите сотрудника:",
        reply_markup=builder.as_markup(), parse_mode="Markdown"
    )
    await callback.answer()


# ── Календарь сотрудника (admin, с тогглом) ───────────────────────────────────

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
    user = current_db.get_user_by_id(target_uid)
    name = escape_md(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")
    daily_rate = current_db.get_salary_rate(target_uid)
    worked = current_db.get_work_schedule(target_uid, year, month)
    worked_count = len(worked)
    salary = worked_count * daily_rate
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    text = (
        f"📅 *График работы: {name}*\n"
        f"{_MONTH_NAMES[month - 1]} {year}\n\n"
        f"✅ = рабочая смена  ⬜ = выходной\n"
        f"Нажмите на день для отметки / снятия\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"📊 Смен отмечено: {worked_count}\n"
        f"💰 Оклад: {format_price(salary)}₽"
    )
    kb = _calendar_kb(year, month, worked, uid=target_uid, editable=True, back_cb="slr_scheds")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ── Тоггл рабочего дня (admin) ────────────────────────────────────────────────

@salary_router.callback_query(F.data.startswith("slr_tog_"))
async def salary_toggle_day(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not (env_manager.is_super_admin(uid) or is_any_admin(uid)):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_", 3)
    target_uid = int(parts[2])
    date_str = parts[3]
    current_db = await get_db(uid, state)
    now_working = current_db.toggle_work_day(target_uid, date_str, uid)
    status = "✅ рабочая" if now_working else "⬜ выходной"
    await callback.answer(f"{date_str}: {status}")
    year = int(date_str[:4])
    month = int(date_str[5:7])
    user = current_db.get_user_by_id(target_uid)
    name = escape_md(f"{user[2]} {user[3]}".strip() if user else f"id={target_uid}")
    daily_rate = current_db.get_salary_rate(target_uid)
    worked = current_db.get_work_schedule(target_uid, year, month)
    worked_count = len(worked)
    salary = worked_count * daily_rate
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    text = (
        f"📅 *График работы: {name}*\n"
        f"{_MONTH_NAMES[month - 1]} {year}\n\n"
        f"✅ = рабочая смена  ⬜ = выходной\n"
        f"Нажмите на день для отметки / снятия\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"📊 Смен отмечено: {worked_count}\n"
        f"💰 Оклад: {format_price(salary)}₽"
    )
    kb = _calendar_kb(year, month, worked, uid=target_uid, editable=True, back_cb="slr_scheds")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


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
    text = f"📊 *Сводка ФОТ — {_MONTH_NAMES[month - 1]} {year}*\n\n"
    total_fot = 0
    if not summary:
        text += "❌ Нет данных"
    else:
        for row in summary:
            s_uid, fn, ln, daily_rate, worked_days, salary, shop, tg_id = row
            name = escape_md(f"{fn} {ln}".strip())
            shop_str = f" · {escape_md(shop)}" if shop else ""
            rate_str = f"{format_price(daily_rate)}₽" if daily_rate else "—"
            text += (
                f"👤 *{name}*{shop_str}\n"
                f"   📅 {worked_days} смен × {rate_str} = *{format_price(salary)}₽*\n\n"
            )
            total_fot += salary
        text += f"━━━━━━━━━━━━━━━━━━\n💰 *Итого ФОТ: {format_price(total_fot)}₽*"
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
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()


# ── Мой график (продавец) ─────────────────────────────────────────────────────

@salary_router.callback_query(F.data == "my_schedule")
async def my_schedule(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    now = datetime.now()
    year, month = now.year, now.month
    daily_rate = current_db.get_salary_rate(user_id)
    worked = current_db.get_work_schedule(user_id, year, month)
    worked_count = len(worked)
    salary = worked_count * daily_rate
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    text = (
        f"📅 *Мой график работы*\n"
        f"{_MONTH_NAMES[month - 1]} {year}\n\n"
        f"✅ — рабочая смена  ⬜ — выходной\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"📊 Смен отработано: {worked_count}\n"
        f"💰 Оклад к выплате: {format_price(salary)}₽"
    )
    kb = _calendar_kb(year, month, worked, editable=False, back_cb="main_menu")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@salary_router.callback_query(F.data.startswith("my_cal_"))
async def my_schedule_nav(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    current_db = await get_db(uid, state)
    user_id = current_db.get_user_id(uid)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    daily_rate = current_db.get_salary_rate(user_id)
    worked = current_db.get_work_schedule(user_id, year, month)
    worked_count = len(worked)
    salary = worked_count * daily_rate
    rate_str = f"{format_price(daily_rate)}₽/смену" if daily_rate else "не задана"
    text = (
        f"📅 *Мой график работы*\n"
        f"{_MONTH_NAMES[month - 1]} {year}\n\n"
        f"✅ — рабочая смена  ⬜ — выходной\n\n"
        f"💼 Ставка: {rate_str}\n"
        f"📊 Смен отработано: {worked_count}\n"
        f"💰 Оклад к выплате: {format_price(salary)}₽"
    )
    kb = _calendar_kb(year, month, worked, editable=False, back_cb="main_menu")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()