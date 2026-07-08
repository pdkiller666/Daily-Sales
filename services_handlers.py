"""
Бот-обработчики модулей Услуги и Записи.
Хаб → каталог услуг → записи.
"""
import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

from keyboards import InlineKeyboardBuilder
from db_utils import get_db, clear_state_keep_org, is_any_admin
from utils import he
from message_utils import fsm_edit
from billing_utils import has_module

services_router = Router()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 8
_STATUSES = {
    "planned": "📌 Запланирована",
    "confirmed": "✅ Подтверждена",
    "completed": "✔️ Выполнена",
    "cancelled": "❌ Отменена",
    "no_show": "🚫 Неявка",
}


# ──────────────────────────────────────────────────────────────────────────────
# Хаб
# ──────────────────────────────────────────────────────────────────────────────

@services_router.callback_query(F.data == "services_hub")
async def services_hub(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services"):
        await callback.answer("Модуль Услуги не подключён", show_alert=True)
        return
    await callback.answer()
    is_admin = is_any_admin(tg_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="🎯 Каталог услуг", callback_data="svc_catalog:0")
    builder.button(text="📅 Мои записи", callback_data="appt_my:0")
    if is_admin:
        builder.button(text="📋 Все записи", callback_data="appt_all:0")
    builder.button(text="🏠 Главное меню", callback_data="main_menu")
    builder.adjust(1)
    await fsm_edit(state, callback.message,
                   "🎯 <b>Услуги</b>\n\nКаталог услуг и записи клиентов.",
                   builder.as_markup())


# ──────────────────────────────────────────────────────────────────────────────
# Каталог услуг
# ──────────────────────────────────────────────────────────────────────────────

@services_router.callback_query(F.data.startswith("svc_catalog:"))
async def svc_catalog(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services"):
        await callback.answer("Модуль Услуги не подключён", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        total = conn.execute("SELECT COUNT(*) FROM services WHERE is_active=1").fetchone()[0]
        rows = conn.execute(
            "SELECT s.id, s.name, s.price, s.duration_minutes, sc.name "
            "FROM services s LEFT JOIN service_categories sc ON s.category_id=sc.id "
            "WHERE s.is_active=1 ORDER BY s.name LIMIT ? OFFSET ?",
            (_PAGE_SIZE, page * _PAGE_SIZE),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("svc_catalog error: %s", e)
        await callback.answer("Ошибка загрузки", show_alert=True)
        return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    for r in rows:
        cat = f" [{r[4]}]" if r[4] else ""
        builder.button(
            text=f"🎯 {r[1]}{cat} — {r[2]:,.0f} ₽ · {r[3]} мин",
            callback_data=f"svc_card:{r[0]}"
        )
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"svc_catalog:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"svc_catalog:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Услуги", callback_data="services_hub"))

    text = f"🎯 <b>Каталог услуг</b> — {total} поз."
    if total == 0:
        text = "🎯 <b>Каталог услуг</b>\n\nПока пусто. Добавьте услуги в веб-кабинете."
    await fsm_edit(state, callback.message, text, builder.as_markup())


@services_router.callback_query(F.data.startswith("svc_card:"))
async def svc_card(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services"):
        await callback.answer("Модуль Услуги не подключён", show_alert=True)
        return
    await callback.answer()
    svc_id = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        r = conn.execute(
            "SELECT s.id, s.name, s.description, s.price, s.duration_minutes, "
            "s.group_max_participants, sc.name "
            "FROM services s LEFT JOIN service_categories sc ON s.category_id=sc.id "
            "WHERE s.id=?", (svc_id,)
        ).fetchone()
        if not r:
            await callback.answer("Услуга не найдена", show_alert=True)
            conn.close()
            return

        appt_count = conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE service_id=?", (svc_id,)
        ).fetchone()[0]
        conn.close()
    except Exception as e:
        logger.error("svc_card error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    lines = [f"🎯 <b>{he(r[1])}</b>"]
    if r[6]:
        lines.append(f"🏷 Категория: {he(r[6])}")
    lines.append(f"💰 Цена: <b>{r[3]:,.0f} ₽</b>")
    lines.append(f"⏱ Длительность: {r[4]} мин")
    if r[5] > 1:
        lines.append(f"👥 До {r[5]} участников")
    if r[2]:
        lines.append(f"\n📝 {he(r[2][:300])}")
    if appt_count:
        lines.append(f"\n📅 Всего записей: {appt_count}")
    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    if has_module(tg_id, "crm"):
        builder.button(text="📅 Записать клиента", callback_data=f"appt_new_svc:{svc_id}")
    builder.button(text="📋 Записи по услуге", callback_data=f"appt_by_svc:{svc_id}:0")
    builder.button(text="🔙 Каталог", callback_data="svc_catalog:0")
    builder.adjust(1)

    await fsm_edit(state, callback.message, text, builder.as_markup())


# ──────────────────────────────────────────────────────────────────────────────
# Записи
# ──────────────────────────────────────────────────────────────────────────────

def _appt_text_short(r) -> str:
    svc = r[1] or "—"
    client = r[2] or "—"
    start = (r[3] or "")[:16]
    status = _STATUSES.get(r[4], r[4])
    price = r[5] or 0
    return f"• {svc} · {client}\n  {start} · {status} · {price:,.0f} ₽"


def _appt_list_query(conn, where: str, params: list, page: int):
    total = conn.execute(
        f"SELECT COUNT(*) FROM appointments a WHERE {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT a.id, sv.name, c.first_name||' '||c.last_name, "
        f"a.start_time, a.status, a.price "
        f"FROM appointments a "
        f"LEFT JOIN services sv ON a.service_id=sv.id "
        f"LEFT JOIN clients c ON a.client_id=c.id "
        f"WHERE {where} ORDER BY a.start_time DESC LIMIT ? OFFSET ?",
        params + [_PAGE_SIZE, page * _PAGE_SIZE],
    ).fetchall()
    return total, rows


@services_router.callback_query(F.data.startswith("appt_my:"))
async def appt_my(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services"):
        await callback.answer("Модуль Услуги не подключён", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        my_user = conn.execute("SELECT id FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
        my_uid = my_user[0] if my_user else -1
        total, rows = _appt_list_query(conn, "a.staff_user_id=?", [my_uid], page)
        conn.close()
    except Exception as e:
        logger.error("appt_my error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    for r in rows:
        status_icon = {"completed": "✔️", "cancelled": "❌", "confirmed": "✅", "no_show": "🚫"}.get(r[4], "📌")
        name = f"{r[2] or '—'}"[:20]
        builder.button(
            text=f"{status_icon} {(r[3] or '')[:10]} · {name}",
            callback_data=f"appt_card:{r[0]}"
        )
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"appt_my:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"appt_my:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Услуги", callback_data="services_hub"))

    if total == 0:
        text = "📅 <b>Мои записи</b>\n\nЗаписей нет."
    else:
        text = f"📅 <b>Мои записи</b> — стр. {page+1}/{total_pages} (всего {total})"
    await fsm_edit(state, callback.message, text, builder.as_markup())


@services_router.callback_query(F.data.startswith("appt_all:"))
async def appt_all(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services") or not is_any_admin(tg_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        total, rows = _appt_list_query(conn, "1=1", [], page)
        conn.close()
    except Exception as e:
        logger.error("appt_all error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    for r in rows:
        status_icon = {"completed": "✔️", "cancelled": "❌", "confirmed": "✅", "no_show": "🚫"}.get(r[4], "📌")
        name = f"{r[2] or '—'}"[:20]
        builder.button(
            text=f"{status_icon} {(r[3] or '')[:10]} · {name}",
            callback_data=f"appt_card:{r[0]}"
        )
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"appt_all:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"appt_all:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Услуги", callback_data="services_hub"))

    text = f"📋 <b>Все записи</b> — стр. {page+1}/{total_pages} (всего {total})"
    if total == 0:
        text = "📋 <b>Все записи</b>\n\nЗаписей нет."
    await fsm_edit(state, callback.message, text, builder.as_markup())


@services_router.callback_query(F.data.startswith("appt_by_svc:"))
async def appt_by_svc(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services"):
        await callback.answer("Модуль Услуги не подключён", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split(":")
    svc_id = int(parts[1])
    page = int(parts[2])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        total, rows = _appt_list_query(conn, "a.service_id=?", [svc_id], page)
        conn.close()
    except Exception as e:
        logger.error("appt_by_svc error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    for r in rows:
        status_icon = {"completed": "✔️", "cancelled": "❌", "confirmed": "✅", "no_show": "🚫"}.get(r[4], "📌")
        name = f"{r[2] or '—'}"[:20]
        builder.button(
            text=f"{status_icon} {(r[3] or '')[:10]} · {name}",
            callback_data=f"appt_card:{r[0]}"
        )
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"appt_by_svc:{svc_id}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"appt_by_svc:{svc_id}:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 К услуге", callback_data=f"svc_card:{svc_id}"))
    text = f"📅 <b>Записи по услуге</b> — всего {total}"
    await fsm_edit(state, callback.message, text, builder.as_markup())


@services_router.callback_query(F.data.startswith("appt_card:"))
async def appt_card(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services"):
        await callback.answer("Модуль Услуги не подключён", show_alert=True)
        return
    await callback.answer()
    appt_id = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        r = conn.execute(
            "SELECT a.id, sv.name, c.first_name||' '||c.last_name, c.phone, "
            "u.first_name||' '||u.last_name, a.start_time, a.end_time, "
            "a.status, a.notes, a.price "
            "FROM appointments a "
            "LEFT JOIN services sv ON a.service_id=sv.id "
            "LEFT JOIN clients c ON a.client_id=c.id "
            "LEFT JOIN users u ON a.staff_user_id=u.id "
            "WHERE a.id=?", (appt_id,)
        ).fetchone()
        if not r:
            await callback.answer("Запись не найдена", show_alert=True)
            conn.close()
            return
        conn.close()
    except Exception as e:
        logger.error("appt_card error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    status_label = _STATUSES.get(r[7], r[7])
    lines = [
        f"📅 <b>Запись #{r[0]}</b>",
        f"🎯 Услуга: {he(r[1] or '—')}",
        f"👤 Клиент: {he((r[2] or '—').strip())}",
    ]
    if r[3]:
        lines.append(f"📞 {he(r[3])}")
    if r[4]:
        lines.append(f"💼 Мастер: {he(r[4].strip())}")
    lines.append(f"🕐 Начало: {(r[5] or '')[:16]}")
    lines.append(f"🕑 Конец: {(r[6] or '')[:16]}")
    lines.append(f"📌 Статус: {status_label}")
    lines.append(f"💰 Сумма: {(r[9] or 0):,.0f} ₽")
    if r[8]:
        lines.append(f"\n📝 {he(r[8][:200])}")
    text = "\n".join(lines)

    is_admin = is_any_admin(tg_id)
    builder = InlineKeyboardBuilder()

    if is_admin and r[7] not in ("completed", "cancelled"):
        if r[7] != "confirmed":
            builder.button(text="✅ Подтвердить", callback_data=f"appt_status:{appt_id}:confirmed")
        builder.button(text="✔️ Выполнена", callback_data=f"appt_status:{appt_id}:completed")
        builder.button(text="❌ Отменить", callback_data=f"appt_status:{appt_id}:cancelled")

    builder.button(text="🔙 Назад", callback_data="appt_all:0" if is_admin else "appt_my:0")
    builder.adjust(1)
    await fsm_edit(state, callback.message, text, builder.as_markup())


@services_router.callback_query(F.data.startswith("appt_status:"))
async def appt_change_status(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "services") or not is_any_admin(tg_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split(":")
    appt_id = int(parts[1])
    new_status = parts[2]

    if new_status not in _STATUSES:
        return

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        old = conn.execute("SELECT status, service_id, staff_user_id, price FROM appointments WHERE id=?", (appt_id,)).fetchone()
        if not old:
            conn.close()
            await callback.answer("Запись не найдена", show_alert=True)
            return

        old_status, service_id, staff_user_id, appt_price = old
        conn.execute(
            "UPDATE appointments SET status=?, updated_at=datetime('now') WHERE id=?",
            (new_status, appt_id)
        )
        conn.execute(
            "INSERT INTO appointment_status_log (appointment_id, old_status, new_status, changed_by) VALUES (?, ?, ?, ?)",
            (appt_id, old_status, new_status, tg_id)
        )

        if new_status == "completed" and old_status != "completed" and staff_user_id:
            from billing_utils import has_extension
            if has_extension(tg_id, "services_motivation"):
                _calc_commission(conn, appt_id, service_id, staff_user_id, appt_price or 0)

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("appt_change_status error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    await callback.answer(f"Статус изменён: {_STATUSES.get(new_status, new_status)}")
    await appt_card.__wrapped__(callback, state) if hasattr(appt_card, '__wrapped__') else None
    # Обновляем карточку
    builder = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Назад", callback_data="appt_all:0")
    ]])
    await fsm_edit(state, callback.message,
                   f"✅ Статус записи #{appt_id} изменён на «{_STATUSES.get(new_status)}».",
                   builder)


def _calc_commission(conn, appt_id: int, service_id: int, staff_user_id: int, price: float):
    try:
        rule = conn.execute(
            "SELECT type, value FROM service_motivation_rules WHERE service_id=? AND is_active=1 "
            "ORDER BY CASE scope_type WHEN 'user' THEN 1 WHEN 'shop' THEN 2 WHEN 'global' THEN 3 ELSE 4 END LIMIT 1",
            (service_id,),
        ).fetchone()
        if not rule:
            return
        mot_type, mot_value = rule
        commission = (price * mot_value / 100.0) if mot_type == "percentage" else float(mot_value)
        conn.execute(
            "INSERT OR REPLACE INTO service_earnings "
            "(appointment_id, user_id, service_id, commission_amount, motivation_type, motivation_value, motivation_source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'global')",
            (appt_id, staff_user_id, service_id, commission, mot_type, mot_value),
        )
    except Exception as e:
        logger.warning("_calc_commission: %s", e)


# Пустышка: создание записи через бот → редирект на веб
@services_router.callback_query(F.data.startswith("appt_new_svc:"))
async def appt_new_svc(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    svc_id = callback.data.split(":")[1]
    builder = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Создать запись в кабинете", url=f"https://t.me/your_bot?start=web")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"svc_card:{svc_id}")],
    ])
    await fsm_edit(state, callback.message,
                   "📅 Для создания записи откройте веб-кабинет.",
                   builder)
