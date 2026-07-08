"""
Бот-обработчики модуля «Абонементы» (service_packages / client_packages).
Хаб → каталог шаблонов → продажа клиенту / список у клиента / списание визита.
"""
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

from keyboards import InlineKeyboardBuilder
from db_utils import get_db, clear_state_keep_org, is_any_admin
from utils import he
from message_utils import fsm_edit, delete_message_safe
from billing_utils import has_module
from states import PackageStates
from packages_utils import (
    find_consumable_package,
    consume_package_visit,
    sell_package,
    auto_expire_packages,
)

packages_router = Router()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 8


def _pkg_hub_kb(is_admin: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎫 Каталог абонементов", callback_data="pkg_catalog:0")
    if is_admin:
        builder.button(text="💳 Продать абонемент", callback_data="pkg_sell_start")
    builder.button(text="🔍 Абонементы клиента", callback_data="pkg_view_start")
    builder.button(text="🏠 Главное меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


@packages_router.callback_query(F.data == "packages_hub")
async def packages_hub(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    await callback.answer()
    is_admin = is_any_admin(tg_id)
    await fsm_edit(state, callback.message,
                   "🎫 <b>Абонементы</b>\n\nПакеты посещений для клиентов.",
                   _pkg_hub_kb(is_admin))


# ──────────────────────────────────────────────────────────────────────────────
# Каталог шаблонов
# ──────────────────────────────────────────────────────────────────────────────

@packages_router.callback_query(F.data.startswith("pkg_catalog:"))
async def pkg_catalog(callback: CallbackQuery, state: FSMContext):
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
        conn = db._db.get_connection()
        total = conn.execute("SELECT COUNT(*) FROM service_packages WHERE is_active=1").fetchone()[0]
        rows = conn.execute(
            "SELECT id, name, visits_total, price FROM service_packages "
            "WHERE is_active=1 ORDER BY name LIMIT ? OFFSET ?",
            (_PAGE_SIZE, page * _PAGE_SIZE),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("pkg_catalog error: %s", e)
        await callback.answer("Ошибка загрузки", show_alert=True)
        return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    for r in rows:
        builder.button(text=f"🎫 {r[1]} — {r[2]} зан. · {r[3]:,.0f} ₽", callback_data=f"pkg_card:{r[0]}")
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"pkg_catalog:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"pkg_catalog:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Абонементы", callback_data="packages_hub"))

    text = f"🎫 <b>Каталог абонементов</b> — {total} поз."
    if total == 0:
        text = "🎫 <b>Каталог абонементов</b>\n\nПока пусто. Добавьте шаблоны в веб-кабинете."
    await fsm_edit(state, callback.message, text, builder.as_markup())


@packages_router.callback_query(F.data.startswith("pkg_card:"))
async def pkg_card(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    await callback.answer()
    pkg_id = int(callback.data.split(":")[1])

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db._db.get_connection()
        r = conn.execute(
            "SELECT name, visits_total, price, validity_days, description "
            "FROM service_packages WHERE id=?", (pkg_id,)
        ).fetchone()
        conn.close()
    except Exception as e:
        logger.error("pkg_card error: %s", e)
        await callback.answer("Ошибка", show_alert=True)
        return

    if not r:
        await callback.answer("Абонемент не найден", show_alert=True)
        return

    lines = [f"🎫 <b>{he(r[0])}</b>"]
    lines.append(f"📅 Занятий: <b>{r[1]}</b>")
    lines.append(f"💰 Цена: <b>{r[2]:,.0f} ₽</b>")
    if r[3]:
        lines.append(f"⏳ Срок действия: {r[3]} дн.")
    if r[4]:
        lines.append(f"\n📝 {he(r[4][:300])}")
    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    if is_any_admin(tg_id):
        builder.button(text="💳 Продать этот абонемент", callback_data=f"pkg_sell_pick:{pkg_id}")
    builder.button(text="🔙 Каталог", callback_data="pkg_catalog:0")
    builder.adjust(1)
    await fsm_edit(state, callback.message, text, builder.as_markup())


# ──────────────────────────────────────────────────────────────────────────────
# Продажа абонемента: поиск клиента → подтверждение
# ──────────────────────────────────────────────────────────────────────────────

@packages_router.callback_query(F.data == "pkg_sell_start")
async def pkg_sell_start(callback: CallbackQuery, state: FSMContext):
    await _prompt_client_search(callback, state, PackageStates.waiting_sell_client_query,
                                 "💳 <b>Продажа абонемента</b>\n\nВведите имя, телефон или email клиента:")


@packages_router.callback_query(F.data.startswith("pkg_sell_pick:"))
async def pkg_sell_pick(callback: CallbackQuery, state: FSMContext):
    pkg_id = int(callback.data.split(":")[1])
    await state.update_data(pkg_sell_package_id=pkg_id)
    await _prompt_client_search(callback, state, PackageStates.waiting_sell_client_query,
                                 "💳 <b>Продажа абонемента</b>\n\nВведите имя, телефон или email клиента:")


async def _prompt_client_search(callback: CallbackQuery, state: FSMContext, target_state, text: str):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    if not is_any_admin(tg_id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Отмена", callback_data="packages_hub")
    ]])
    await fsm_edit(state, callback.message, text, kb)
    await state.update_data(anchor_message_id=callback.message.message_id)
    await state.set_state(target_state)


@packages_router.message(F.text, PackageStates.waiting_sell_client_query)
async def pkg_sell_client_query(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    query = message.text.strip()
    await delete_message_safe(message)

    db = await get_db(tg_id, state)
    if not db:
        return

    data = await state.get_data()
    pkg_id = data.get("pkg_sell_package_id")

    try:
        like = f"%{query.lower()}%"
        conn = db._db.get_connection()
        rows = conn.execute(
            "SELECT id, first_name, last_name, phone FROM clients "
            "WHERE lower(first_name||' '||last_name) LIKE ? OR phone LIKE ? OR email LIKE ? "
            "LIMIT 15",
            (like, like, like),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("pkg_sell_client_query error: %s", e)
        rows = []

    builder = InlineKeyboardBuilder()
    if rows:
        for r in rows:
            name = f"{r[1]} {r[2]}".strip()
            phone = f" · {r[3]}" if r[3] else ""
            cb = f"pkg_sell_client:{r[0]}" if not pkg_id else f"pkg_sell_confirm:{pkg_id}:{r[0]}"
            builder.button(text=f"👤 {name}{phone}", callback_data=cb)
        builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Абонементы", callback_data="packages_hub"))

    text = (f"🔍 Найдено клиентов: {len(rows)}. Выберите:" if rows
            else f"По запросу «{he(query)}» ничего не найдено.")

    data = await state.get_data()
    anchor_id = data.get("anchor_message_id")
    if anchor_id:
        try:
            import bot_holder
            await bot_holder.bot.edit_message_text(
                text, chat_id=message.chat.id,
                message_id=anchor_id, reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception:
            pass
    await clear_state_keep_org(state)


@packages_router.callback_query(F.data.startswith("pkg_sell_client:"))
async def pkg_sell_client(callback: CallbackQuery, state: FSMContext):
    """Клиент выбран без предварительно выбранного шаблона — показать каталог для выбора."""
    client_id = int(callback.data.split(":")[1])
    await callback.answer()
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if not db:
        return
    try:
        conn = db._db.get_connection()
        rows = conn.execute(
            "SELECT id, name, visits_total, price FROM service_packages WHERE is_active=1 ORDER BY name LIMIT 20"
        ).fetchall()
        conn.close()
    except Exception:
        rows = []
    builder = InlineKeyboardBuilder()
    for r in rows:
        builder.button(text=f"🎫 {r[1]} — {r[2]} зан. · {r[3]:,.0f} ₽",
                        callback_data=f"pkg_sell_confirm:{r[0]}:{client_id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Абонементы", callback_data="packages_hub"))
    await fsm_edit(state, callback.message, "Выберите абонемент для продажи:", builder.as_markup())


@packages_router.callback_query(F.data.startswith("pkg_sell_confirm:"))
async def pkg_sell_confirm(callback: CallbackQuery, state: FSMContext):
    _, pkg_id, client_id = callback.data.split(":")
    pkg_id, client_id = int(pkg_id), int(client_id)
    tg_id = callback.from_user.id
    if not is_any_admin(tg_id):
        await callback.answer("Только для администраторов", show_alert=True)
        return

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db._db.get_connection()
        cp_id, err = sell_package(conn, pkg_id, client_id, None, tg_id, notes="Продано через бота")
        if err:
            conn.close()
            await callback.answer(f"Ошибка: {err}", show_alert=True)
            return
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("pkg_sell_confirm error: %s", e)
        await callback.answer("Ошибка продажи", show_alert=True)
        return

    try:
        from web.sale_events import post_package_sale_effects
        await post_package_sale_effects(db.db_file, cp_id, tg_id)
    except Exception as e:
        logger.warning("pkg_sell_confirm: post_package_sale_effects error: %s", e)

    await callback.answer("Абонемент продан ✅", show_alert=True)
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Абонементы", callback_data="packages_hub")
    await fsm_edit(state, callback.message, "✅ Абонемент успешно продан клиенту.", builder.as_markup())


# ──────────────────────────────────────────────────────────────────────────────
# Просмотр абонементов клиента + списание визита
# ──────────────────────────────────────────────────────────────────────────────

@packages_router.callback_query(F.data == "pkg_view_start")
async def pkg_view_start(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Отмена", callback_data="packages_hub")
    ]])
    await fsm_edit(state, callback.message,
                   "🔍 <b>Абонементы клиента</b>\n\nВведите имя, телефон или email клиента:", kb)
    await state.update_data(anchor_message_id=callback.message.message_id)
    await state.set_state(PackageStates.waiting_view_client_query)


@packages_router.message(F.text, PackageStates.waiting_view_client_query)
async def pkg_view_client_query(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    query = message.text.strip()
    await delete_message_safe(message)

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        like = f"%{query.lower()}%"
        conn = db._db.get_connection()
        rows = conn.execute(
            "SELECT id, first_name, last_name, phone FROM clients "
            "WHERE lower(first_name||' '||last_name) LIKE ? OR phone LIKE ? OR email LIKE ? "
            "LIMIT 15",
            (like, like, like),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("pkg_view_client_query error: %s", e)
        rows = []

    builder = InlineKeyboardBuilder()
    for r in rows:
        name = f"{r[1]} {r[2]}".strip()
        phone = f" · {r[3]}" if r[3] else ""
        builder.button(text=f"👤 {name}{phone}", callback_data=f"pkg_client_view:{r[0]}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Абонементы", callback_data="packages_hub"))

    text = (f"🔍 Найдено клиентов: {len(rows)}. Выберите:" if rows
            else f"По запросу «{he(query)}» ничего не найдено.")

    data = await state.get_data()
    anchor_id = data.get("anchor_message_id")
    if anchor_id:
        try:
            import bot_holder
            await bot_holder.bot.edit_message_text(
                text, chat_id=message.chat.id,
                message_id=anchor_id, reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception:
            pass
    await clear_state_keep_org(state)


@packages_router.callback_query(F.data.startswith("pkg_client_view:"))
async def pkg_client_view(callback: CallbackQuery, state: FSMContext, _answered: bool = False):
    client_id = int(callback.data.split(":")[1])
    tg_id = callback.from_user.id
    if not has_module(tg_id, "crm"):
        await callback.answer("Модуль CRM не подключён", show_alert=True)
        return
    if not _answered:
        await callback.answer()

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db._db.get_connection()
        auto_expire_packages(conn)
        conn.commit()
        client = conn.execute("SELECT first_name, last_name FROM clients WHERE id=?", (client_id,)).fetchone()
        rows = conn.execute(
            "SELECT cp.id, sp.name, cp.visits_total, cp.visits_used, cp.expires_at, cp.status "
            "FROM client_packages cp JOIN service_packages sp ON sp.id=cp.package_id "
            "WHERE cp.client_id=? ORDER BY cp.purchased_at DESC LIMIT 15",
            (client_id,),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("pkg_client_view error: %s", e)
        await callback.answer("Ошибка загрузки", show_alert=True)
        return

    client_name = he(f"{client[0]} {client[1] or ''}".strip()) if client else "—"
    builder = InlineKeyboardBuilder()
    if not rows:
        text = f"👤 <b>{client_name}</b>\n\nАбонементов нет."
    else:
        lines = [f"👤 <b>{client_name}</b> — абонементы:\n"]
        for r in rows:
            cp_id, name, total, used, expires_at, status = r
            left = total - used
            exp = f" · до {str(expires_at)[:10]}" if expires_at else ""
            status_ico = {"active": "🟢", "frozen": "🧊", "exhausted": "⚫", "expired": "🔴"}.get(status, "•")
            lines.append(f"{status_ico} {he(name)} — {left}/{total}{exp}")
            if status == "active" and left > 0:
                builder.button(text=f"− Занятие: {name}", callback_data=f"pkg_use:{cp_id}")
        text = "\n".join(lines)
        builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Абонементы", callback_data="packages_hub"))
    await fsm_edit(state, callback.message, text, builder.as_markup())


@packages_router.callback_query(F.data.startswith("pkg_use:"))
async def pkg_use(callback: CallbackQuery, state: FSMContext):
    cp_id = int(callback.data.split(":")[1])
    tg_id = callback.from_user.id
    if not is_any_admin(tg_id):
        await callback.answer("Только для администраторов", show_alert=True)
        return

    db = await get_db(tg_id, state)
    if not db:
        return

    try:
        conn = db._db.get_connection()
        client_row = conn.execute("SELECT client_id FROM client_packages WHERE id=?", (cp_id,)).fetchone()
        result = consume_package_visit(conn, cp_id, tg_id, note="Списано через бота")
        if result.get("error"):
            conn.close()
            await callback.answer(f"Ошибка: {result['error']}", show_alert=True)
            return
        conn.commit()
        client_id = client_row[0] if client_row else None
        conn.close()
    except Exception as e:
        logger.error("pkg_use error: %s", e)
        await callback.answer("Ошибка списания", show_alert=True)
        return

    await callback.answer("Занятие списано ✅")
    if client_id:
        callback.data = f"pkg_client_view:{client_id}"
        await pkg_client_view(callback, state, _answered=True)
