"""
Бот-обработчики модуля CRM (клиенты).
Команды: просмотр списка, поиск, добавление, карточка клиента.
"""
import json
import logging
import re
from datetime import datetime

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from keyboards import InlineKeyboardBuilder, back_button
from db_utils import get_db, clear_state_keep_org, is_any_admin
from utils import he
from message_utils import fsm_edit, delete_message_safe
from billing_utils import has_module

clients_router = Router()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 8


class ClientAddStates(StatesGroup):
    waiting_first_name = State()
    waiting_last_name  = State()
    waiting_phone      = State()
    waiting_email      = State()
    waiting_notes      = State()


def _clients_hub_kb(is_admin: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Список клиентов", callback_data="crm_list:0")
    builder.button(text="🔍 Поиск", callback_data="crm_search_prompt")
    if is_admin:
        builder.button(text="➕ Добавить клиента", callback_data="crm_add_start")
    builder.button(text="🏠 Главное меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def _client_card_text(c: dict) -> str:
    lines = [f"👤 <b>{he(c.get('first_name',''))} {he(c.get('last_name',''))}</b>"]
    if c.get('phone'):
        lines.append(f"📞 {he(c['phone'])}")
    if c.get('email'):
        lines.append(f"📧 {he(c['email'])}")
    if c.get('birth_date'):
        lines.append(f"🎂 {c['birth_date']}")
    if c.get('source'):
        lines.append(f"📌 Источник: {he(c['source'])}")
    tags = json.loads(c.get('tags_json') or '[]') if isinstance(c.get('tags_json'), str) else []
    if tags:
        lines.append(f"🏷 {', '.join(he(t) for t in tags)}")
    if c.get('notes'):
        lines.append(f"\n📝 {he(c['notes'][:200])}")
    return "\n".join(lines)


@clients_router.callback_query(F.data == "crm_hub")
async def crm_hub(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    await callback.answer()
    is_admin = is_any_admin(tg_id)
    await fsm_edit(state, callback.message,
                   "👥 <b>Клиенты / CRM</b>\n\nУправление базой клиентов организации.",
                   _clients_hub_kb(is_admin))


@clients_router.callback_query(F.data.startswith("crm_list:"))
async def crm_list(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    await callback.answer()

    page = int(callback.data.split(":")[1])
    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        total = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        rows = conn.execute(
            "SELECT id, first_name, last_name, phone FROM clients ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (_PAGE_SIZE, page * _PAGE_SIZE),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("crm_list error: %s", e)
        await callback.answer("Ошибка загрузки", show_alert=True)
        return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    for r in rows:
        name = f"{r[1]} {r[2]}".strip()
        phone = f" · {r[3]}" if r[3] else ""
        builder.button(text=f"👤 {name}{phone}", callback_data=f"crm_card:{r[0]}")
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"crm_list:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"crm_list:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 CRM", callback_data="crm_hub"))

    text = f"👥 <b>Клиенты</b> — стр. {page+1}/{total_pages} (всего {total})"
    await fsm_edit(state, callback.message, text, builder.as_markup())


@clients_router.callback_query(F.data == "crm_search_prompt")
async def crm_search_prompt(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Отмена", callback_data="crm_hub")
    ]])
    await fsm_edit(state, callback.message,
                   "🔍 <b>Поиск клиента</b>\n\nВведите имя, телефон или email:", kb)
    from states import CrmSearchState
    await state.set_state(CrmSearchState.waiting_query)


@clients_router.message(F.text)
async def crm_search_query(message: Message, state: FSMContext):
    from states import CrmSearchState
    current = await state.get_state()
    if current != CrmSearchState.waiting_query.state:
        return

    tg_id = message.from_user.id
    query = message.text.strip()
    await delete_message_safe(message)

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        like = f"%{query.lower()}%"
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, first_name, last_name, phone FROM clients "
            "WHERE lower(first_name||' '||last_name) LIKE ? OR phone LIKE ? OR email LIKE ? "
            "LIMIT 15",
            (like, like, like),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("crm_search error: %s", e)
        rows = []

    builder = InlineKeyboardBuilder()
    if rows:
        for r in rows:
            name = f"{r[1]} {r[2]}".strip()
            phone = f" · {r[3]}" if r[3] else ""
            builder.button(text=f"👤 {name}{phone}", callback_data=f"crm_card:{r[0]}")
        builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 CRM", callback_data="crm_hub"))

    if rows:
        text = f"🔍 <b>Результаты поиска</b> «{he(query)}»: найдено {len(rows)}"
    else:
        text = f"🔍 По запросу «{he(query)}» ничего не найдено."

    data = await state.get_data()
    anchor_id = data.get("anchor_message_id")
    if anchor_id:
        try:
            from aiogram.types import Bot
            from aiogram import Bot as AioBot
            import bot_holder
            await bot_holder.bot.edit_message_text(
                text, chat_id=message.chat.id,
                message_id=anchor_id, reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception:
            pass
    await clear_state_keep_org(state)


@clients_router.callback_query(F.data.startswith("crm_card:"))
async def crm_card(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    await callback.answer()
    client_id = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        r = conn.execute(
            "SELECT id, first_name, last_name, phone, email, birth_date, source, notes, tags_json "
            "FROM clients WHERE id=?", (client_id,)
        ).fetchone()
        if not r:
            await callback.answer("Клиент не найден", show_alert=True)
            conn.close()
            return

        sales_count = conn.execute("SELECT COUNT(*) FROM sales WHERE client_id=?", (client_id,)).fetchone()[0]
        sales_sum = conn.execute(
            "SELECT SUM(quantity_sold * sale_price) FROM sales WHERE client_id=? AND sale_price IS NOT NULL",
            (client_id,)
        ).fetchone()[0] or 0
        conn.close()
    except Exception as e:
        logger.error("crm_card error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    c = dict(zip(["id","first_name","last_name","phone","email","birth_date","source","notes","tags_json"], r))
    text = _client_card_text(c)
    if sales_count:
        text += f"\n\n💰 Покупок: {sales_count} · Сумма: {sales_sum:,.0f} ₽"

    is_admin = is_any_admin(tg_id)
    builder = InlineKeyboardBuilder()
    if is_admin:
        builder.button(text="🗑 Удалить", callback_data=f"crm_delete_confirm:{client_id}")
    builder.button(text="🔙 К списку", callback_data="crm_list:0")
    builder.button(text="🏠 Главное меню", callback_data="main_menu")
    builder.adjust(1)

    await fsm_edit(state, callback.message, text, builder.as_markup())


@clients_router.callback_query(F.data.startswith("crm_delete_confirm:"))
async def crm_delete_confirm(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not is_any_admin(tg_id):
        await callback.answer("Нет прав", show_alert=True)
        return
    await callback.answer()
    client_id = int(callback.data.split(":")[1])
    builder = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"crm_delete:{client_id}"),
         InlineKeyboardButton(text="❌ Отмена", callback_data=f"crm_card:{client_id}")],
    ])
    await fsm_edit(state, callback.message, "🗑 Удалить клиента? Это действие необратимо.", builder)


@clients_router.callback_query(F.data.startswith("crm_delete:"))
async def crm_delete(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not is_any_admin(tg_id):
        await callback.answer("Нет прав", show_alert=True)
        return
    await callback.answer()
    client_id = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        conn.execute("UPDATE sales SET client_id=NULL WHERE client_id=?", (client_id,))
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("crm_delete error: %s", e)
        await callback.answer("Ошибка удаления", show_alert=True)
        return

    await callback.answer("Клиент удалён", show_alert=True)
    builder = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 CRM", callback_data="crm_hub")
    ]])
    await fsm_edit(state, callback.message, "✅ Клиент удалён.", builder)


# ──────────────────────────────────────────────────────────────────────────────
# Добавление клиента через FSM
# ──────────────────────────────────────────────────────────────────────────────

@clients_router.callback_query(F.data == "crm_add_start")
async def crm_add_start(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm") or not is_any_admin(tg_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="crm_hub")
    ]])
    await fsm_edit(state, callback.message,
                   "👤 <b>Новый клиент</b>\n\nВведите <b>имя</b> клиента:", kb)
    await state.set_state(ClientAddStates.waiting_first_name)


@clients_router.message(ClientAddStates.waiting_first_name)
async def crm_add_first_name(message: Message, state: FSMContext):
    await delete_message_safe(message)
    if not message.text or len(message.text.strip()) < 1:
        return
    await state.update_data(first_name=message.text.strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Пропустить", callback_data="crm_add_skip_lastname"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="crm_hub"),
    ]])
    data = await state.get_data()
    anchor_id = data.get("anchor_message_id")
    if anchor_id:
        try:
            import bot_holder
            await bot_holder.bot.edit_message_text(
                f"👤 Имя: <b>{he(message.text.strip())}</b>\n\nВведите <b>фамилию</b>:",
                chat_id=message.chat.id, message_id=anchor_id,
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception:
            pass
    await state.set_state(ClientAddStates.waiting_last_name)


@clients_router.callback_query(F.data == "crm_add_skip_lastname")
async def crm_add_skip_lastname(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(last_name="")
    await _ask_phone(callback.message, state)


@clients_router.message(ClientAddStates.waiting_last_name)
async def crm_add_last_name(message: Message, state: FSMContext):
    await delete_message_safe(message)
    await state.update_data(last_name=message.text.strip() if message.text else "")
    await _ask_phone(message, state)


async def _ask_phone(msg_or_cb, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Пропустить", callback_data="crm_add_skip_phone"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="crm_hub"),
    ]])
    data = await state.get_data()
    anchor_id = data.get("anchor_message_id")
    chat_id = msg_or_cb.chat.id if hasattr(msg_or_cb, 'chat') else msg_or_cb.from_user.id
    if anchor_id:
        try:
            import bot_holder
            await bot_holder.bot.edit_message_text(
                "📞 Введите <b>телефон</b> клиента:",
                chat_id=chat_id, message_id=anchor_id,
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception:
            pass
    await state.set_state(ClientAddStates.waiting_phone)


@clients_router.callback_query(F.data == "crm_add_skip_phone")
async def crm_add_skip_phone(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(phone="")
    await _finish_add_client(callback.from_user.id, callback.message, state)


@clients_router.message(ClientAddStates.waiting_phone)
async def crm_add_phone(message: Message, state: FSMContext):
    await delete_message_safe(message)
    await state.update_data(phone=message.text.strip() if message.text else "")
    await _finish_add_client(message.from_user.id, message, state)


async def _finish_add_client(tg_id: int, msg, state: FSMContext):
    data = await state.get_data()
    first_name = data.get("first_name", "Клиент")
    last_name = data.get("last_name", "")
    phone = data.get("phone", "")

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db.get_connection()
        cur = conn.execute(
            "INSERT INTO clients (first_name, last_name, phone, created_by) VALUES (?, ?, ?, ?)",
            (first_name, last_name, phone, tg_id),
        )
        conn.commit()
        conn.close()
        client_id = cur.lastrowid
    except Exception as e:
        logger.error("crm_add error: %s", e)
        await clear_state_keep_org(state)
        return

    await clear_state_keep_org(state)

    builder = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Карточка", callback_data=f"crm_card:{client_id}")],
        [InlineKeyboardButton(text="➕ Ещё одного", callback_data="crm_add_start")],
        [InlineKeyboardButton(text="🔙 CRM", callback_data="crm_hub")],
    ])
    anchor_id = data.get("anchor_message_id")
    chat_id = msg.chat.id if hasattr(msg, 'chat') else tg_id
    if anchor_id:
        try:
            import bot_holder
            await bot_holder.bot.edit_message_text(
                f"✅ Клиент <b>{he(first_name)} {he(last_name)}</b> добавлен!",
                chat_id=chat_id, message_id=anchor_id,
                reply_markup=builder, parse_mode="HTML",
            )
        except Exception:
            pass
