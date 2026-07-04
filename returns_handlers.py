"""Обработчики возвратов товара."""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db_utils import get_db, clear_state_keep_org, is_any_admin
from keyboards import back_button, safe_cb, resolve_cb_name
from message_utils import fsm_edit
from states import ReturnStates
from utils import format_currency, he
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_SALES
from timezone_utils import get_current_user_time as _gcur_tz

returns_router = Router()
logger = logging.getLogger(__name__)

_RETURN_PAGE_SIZE = 8


# ─── helpers ────────────────────────────────────────────────────────────────

def _fmt_return_row(r) -> str:
    """r: (id, sale_id, product_name, shop_name, qty, price, seller_uid,
            returned_by_uid, return_date, reason, created_at,
            seller_first, seller_last, admin_first, admin_last)"""
    product  = he(r[2])
    shop     = he(r[3])
    qty      = r[4]
    amount   = qty * r[5]
    date_str = str(r[8])[:10]
    seller   = f"{r[11] or ''} {r[12] or ''}".strip() or "—"
    reason   = f"\n💬 {he(r[9])}" if r[9] else ""
    return (f"📦 <b>{product}</b> × {qty} шт.\n"
            f"🏪 {shop} · 💰 {format_currency(amount)}\n"
            f"📅 {date_str} · 👤 {he(seller)}{reason}")


# ─── entry point: «↩️ Возвраты» из меню редактирования продаж ───────────────

@returns_router.callback_query(F.data == "returns_menu")
async def returns_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "↩️ <b>Возвраты товара</b>\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Оформить возврат", callback_data="return_new")],
            [InlineKeyboardButton(text="📋 История возвратов", callback_data="return_history_0")],
            [back_button("edit_sales")],
        ]),
        parse_mode="HTML",
    )


# ─── новый возврат: выбор продажи ───────────────────────────────────────────

@returns_router.callback_query(F.data == "return_new")
async def return_new_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    tz = await current_db.get_user_timezone(callback.from_user.id)
    today = _gcur_tz(tz).date()

    sales = await current_db.get_recent_sales_for_return(days=30, limit=60)
    if not sales:
        await callback.message.edit_text(
            "↩️ <b>Оформить возврат</b>\n\n"
            "Продаж за последние 30 дней не найдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("returns_menu")]]),
            parse_mode="HTML",
        )
        return

    await state.update_data(return_sales_cache=sales, return_sales_page=0)
    await _show_return_sale_list(callback.message, state, sales, page=0)
    await state.set_state(ReturnStates.choosing_sale)


async def _show_return_sale_list(msg, state: FSMContext, sales: list, page: int):
    data = await state.get_data()
    page_sales, total_pages = paginate(sales, page, _RETURN_PAGE_SIZE)

    builder = InlineKeyboardBuilder()
    for s in page_sales:
        # s: (sale_id, product_name, shop_name, qty, price, sale_date, user_id, first, last)
        label = f"{s[1][:22]} · {s[2][:10]} · {str(s[5])[:10]}"
        builder.row(InlineKeyboardButton(
            text=label,
            callback_data=safe_cb("ret_sale_", str(s[0]))
        ))
    nav = page_nav_row(page, total_pages, "ret_pg_")
    if nav:
        builder.row(*nav)
    builder.row(back_button("returns_menu"))

    await msg.edit_text(
        f"↩️ <b>Выберите продажу для возврата</b>\n"
        f"Продажи за последние 30 дней (стр. {page+1}/{max(total_pages,1)}):",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@returns_router.callback_query(F.data.regexp(r'^ret_pg_\d+$'))
async def return_sale_page(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.replace("ret_pg_", ""))
    data = await state.get_data()
    sales = data.get("return_sales_cache", [])
    await state.update_data(return_sales_page=page)
    await _show_return_sale_list(callback.message, state, sales, page)


@returns_router.callback_query(F.data.regexp(r'^ret_sale_'))
async def return_sale_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    sale_id_str = resolve_cb_name(callback.data, "ret_sale_")
    try:
        sale_id = int(sale_id_str)
    except (ValueError, TypeError):
        await callback.answer("Ошибка выбора", show_alert=True)
        return

    data = await state.get_data()
    sales = data.get("return_sales_cache", [])
    sale = next((s for s in sales if s[0] == sale_id), None)
    if not sale:
        await callback.answer("Продажа не найдена", show_alert=True)
        return

    # sale: (sale_id, product_name, shop_name, qty, price, sale_date, user_id, first, last)
    current_db = await get_db(callback.from_user.id, state)

    # Получаем product_id из оригинальной продажи + уже возвращённое кол-во
    orig = await current_db.get_sale_by_id(sale_id)
    if not orig:
        await callback.message.edit_text(
            "⚠️ <b>Не удалось загрузить данные продажи</b>\n\n"
            "Попробуйте выбрать другую продажу.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("returns_menu")]]),
            parse_mode="HTML",
        )
        return

    already_returned = await current_db.get_already_returned_qty(sale_id)
    available_qty = max(0, sale[3] - already_returned)

    if available_qty == 0:
        await callback.message.edit_text(
            f"⚠️ <b>Невозможно оформить возврат</b>\n\n"
            f"По этой продаже уже возвращены все {sale[3]} шт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("returns_menu")]]),
            parse_mode="HTML",
        )
        return

    await state.update_data(
        ret_sale_id=sale[0],
        ret_product_name=sale[1],
        ret_product_id=orig[1] if orig else None,
        ret_shop_name=sale[2],
        ret_max_qty=available_qty,
        ret_sold_qty=sale[3],
        ret_price=sale[4],
        ret_sale_date=str(sale[5])[:10],
        ret_seller_user_id=sale[6],
        anchor_msg_id=callback.message.message_id,
    )

    seller = f"{sale[7] or ''} {sale[8] or ''}".strip() or "—"
    total = sale[3] * (sale[4] or 0)
    already_note = f"\n⚠️ Уже возвращено: {already_returned} шт." if already_returned > 0 else ""
    await fsm_edit(state, callback.message,
        f"↩️ <b>Возврат продажи</b>\n\n"
        f"🏷 Товар: <b>{he(sale[1])}</b>\n"
        f"🏪 Магазин: {he(sale[2])}\n"
        f"📦 Продано: {sale[3]} шт.{already_note}\n"
        f"💰 Цена: {format_currency(sale[4])} → Итого: {format_currency(total)}\n"
        f"📅 Дата продажи: {str(sale[5])[:10]}\n"
        f"👤 Продавец: {he(seller)}\n\n"
        f"Введите количество для возврата (от 1 до {available_qty}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="returns_menu")]
        ]),
        parse_mode="HTML",
    )
    await state.set_state(ReturnStates.entering_qty)


# ─── ввод количества ─────────────────────────────────────────────────────────

@returns_router.message(ReturnStates.entering_qty)
async def return_enter_qty(message: Message, state: FSMContext):
    if not is_any_admin(message.from_user.id):
        return
    data = await state.get_data()
    max_qty = data.get("ret_max_qty", 1)
    try:
        qty = int(message.text.strip())
        if qty < 1 or qty > max_qty:
            raise ValueError
    except (ValueError, AttributeError):
        await fsm_edit(state, message,
            f"⚠️ Введите число от 1 до {max_qty}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="returns_menu")]
            ]),
        )
        return

    await state.update_data(ret_qty=qty)
    await fsm_edit(state, message,
        f"↩️ <b>Причина возврата</b>\n\n"
        f"Количество: <b>{qty} шт.</b>\n\n"
        f"Введите причину возврата или нажмите «Пропустить»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Пропустить", callback_data="return_skip_reason")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="returns_menu")],
        ]),
        parse_mode="HTML",
    )
    await state.set_state(ReturnStates.entering_reason)


# ─── ввод / пропуск причины ──────────────────────────────────────────────────

@returns_router.callback_query(F.data == "return_skip_reason")
async def return_skip_reason(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    await state.update_data(ret_reason=None)
    await _show_return_confirm(callback.message, state)
    await state.set_state(ReturnStates.confirming)


@returns_router.message(ReturnStates.entering_reason)
async def return_enter_reason(message: Message, state: FSMContext):
    if not is_any_admin(message.from_user.id):
        return
    reason = (message.text or "").strip()[:200]
    await state.update_data(ret_reason=reason or None)
    await _show_return_confirm(message, state)
    await state.set_state(ReturnStates.confirming)


async def _show_return_confirm(msg, state: FSMContext):
    data = await state.get_data()
    qty    = data.get("ret_qty", 1)
    price  = data.get("ret_price", 0.0)
    pname  = data.get("ret_product_name", "")
    shop   = data.get("ret_shop_name", "")
    reason = data.get("ret_reason")
    amount = qty * price
    reason_line = f"\n💬 Причина: <i>{he(reason)}</i>" if reason else ""
    await msg.edit_text(
        f"↩️ <b>Подтвердите возврат</b>\n\n"
        f"🏷 Товар: <b>{he(pname)}</b>\n"
        f"🏪 Магазин: {he(shop)}\n"
        f"📦 Количество: <b>{qty} шт.</b>\n"
        f"💰 Сумма возврата: <b>{format_currency(amount)}</b>{reason_line}\n\n"
        f"⚠️ Остатки будут восстановлены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить возврат", callback_data="return_confirm_ok")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="returns_menu")],
        ]),
        parse_mode="HTML",
    )


# ─── подтверждение ───────────────────────────────────────────────────────────

@returns_router.callback_query(F.data == "return_confirm_ok")
async def return_confirm_ok(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    data = await state.get_data()

    current_db = await get_db(callback.from_user.id, state)
    returned_by_uid = None
    try:
        conn = current_db.get_connection()
        cur  = conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (callback.from_user.id,))
        row = cur.fetchone()
        conn.close()
        returned_by_uid = row[0] if row else None
    except Exception as e:
        logger.error(f"return: get returned_by_uid error: {e}")

    if not returned_by_uid:
        await callback.message.edit_text(
            "❌ Ошибка: пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("returns_menu")]]),
        )
        return

    from timezone_utils import get_current_user_time as _gcur
    tz = await current_db.get_user_timezone(callback.from_user.id)
    return_date = _gcur(tz).date().isoformat()

    qty    = data.get("ret_qty", 1)
    price  = data.get("ret_price", 0.0)
    pname  = data.get("ret_product_name", "")
    shop   = data.get("ret_shop_name", "")

    try:
        return_id = await current_db.create_sale_return(
            sale_id             = data.get("ret_sale_id"),
            product_id          = data.get("ret_product_id"),
            product_name        = pname,
            shop_name           = shop,
            quantity_returned   = qty,
            return_price        = price,
            seller_user_id      = data.get("ret_seller_user_id"),
            returned_by_user_id = returned_by_uid,
            return_date         = return_date,
            reason              = data.get("ret_reason"),
        )
    except ValueError as e:
        await callback.message.edit_text(
            f"⚠️ <b>Возврат невозможен</b>\n\n{he(str(e))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [back_button("returns_menu")]
            ]),
            parse_mode="HTML",
        )
        return

    if return_id:
        text = (f"✅ <b>Возврат оформлен!</b>\n\n"
                f"🏷 Товар: <b>{he(pname)}</b>\n"
                f"🏪 Магазин: {he(shop)}\n"
                f"📦 Количество: {qty} шт.\n"
                f"💰 Сумма: {format_currency(qty * price)}\n"
                f"📦 Остатки восстановлены.")

        # Уведомляем продавца, если возврат оформляет другой пользователь
        seller_internal_uid = data.get("ret_seller_user_id")
        if seller_internal_uid and seller_internal_uid != returned_by_uid:
            try:
                conn2 = current_db.get_connection()
                cur2  = conn2.cursor()
                cur2.execute("SELECT telegram_id FROM users WHERE id = ?", (seller_internal_uid,))
                srow = cur2.fetchone()
                conn2.close()
                seller_tg_id = srow[0] if srow else None
                # synthetic_tg_id < 0 — email-only пользователь, Telegram недоступен
                if seller_tg_id and seller_tg_id > 0:
                    reason_note = f"\n💬 Причина: {he(data.get('ret_reason'))}" if data.get("ret_reason") else ""
                    await callback.bot.send_message(
                        chat_id=seller_tg_id,
                        text=(f"↩️ <b>Оформлен возврат по вашей продаже</b>\n\n"
                              f"🏷 Товар: <b>{he(pname)}</b>\n"
                              f"🏪 Магазин: {he(shop)}\n"
                              f"📦 Количество: {qty} шт.\n"
                              f"💰 Сумма: {format_currency(qty * price)}\n"
                              f"📅 Дата: {return_date}{reason_note}\n\n"
                              f"ℹ️ Это отразится в вашем расчётном листке."),
                        parse_mode="HTML",
                    )
            except Exception as e:
                logger.warning(f"return: не удалось отправить уведомление продавцу: {e}")
    else:
        text = "❌ Ошибка при оформлении возврата. Попробуйте ещё раз."

    await clear_state_keep_org(state)
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Ещё возврат", callback_data="return_new")],
            [InlineKeyboardButton(text="📋 История", callback_data="return_history_0")],
            [back_button("edit_sales")],
        ]),
        parse_mode="HTML",
    )


# ─── история возвратов ───────────────────────────────────────────────────────

@returns_router.callback_query(F.data.regexp(r'^return_history_\d+$'))
async def return_history(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.replace("return_history_", ""))
    offset = page * _RETURN_PAGE_SIZE

    current_db = await get_db(callback.from_user.id, state)
    rows  = await current_db.get_returns(limit=_RETURN_PAGE_SIZE, offset=offset)
    total = await current_db.get_returns_count()
    total_pages = max(1, (total + _RETURN_PAGE_SIZE - 1) // _RETURN_PAGE_SIZE)

    if not rows:
        await callback.message.edit_text(
            "📋 <b>История возвратов</b>\n\nВозвратов пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("returns_menu")]]),
            parse_mode="HTML",
        )
        await state.set_state(ReturnStates.viewing_history)
        return

    lines = "\n\n".join(_fmt_return_row(r) for r in rows)
    text  = (f"📋 <b>История возвратов</b> (стр. {page+1}/{total_pages}, всего {total})\n\n"
             f"{lines}")

    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton(text="◀️", callback_data=f"return_history_{page-1}"))
    if page + 1 < total_pages:
        nav_btns.append(InlineKeyboardButton(text="▶️", callback_data=f"return_history_{page+1}"))

    kb = []
    if nav_btns:
        kb.append(nav_btns)
    kb.append([back_button("returns_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await state.set_state(ReturnStates.viewing_history)
