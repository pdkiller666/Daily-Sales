import asyncio
"""
Административные обработчики для управления заявками на оплату
"""

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from database import Database
from subscription_handlers import get_current_subscription_plans
from env_manager import env_manager
from notif_utils import add_read_btn
from utils import he
from db_utils import wrap_db

payment_admin_router = Router()

def _get_payments_db():
    """Возвращает БД для платёжных запросов (централизованная)"""
    return wrap_db(Database('data/shop_bot.db'))

async def safe_edit_message(callback, text, reply_markup=None, parse_mode="HTML"):
    """Безопасное редактирование сообщения с обработкой ошибок.
    Если текущее сообщение — фото/медиа, удаляет его и отправляет новое текстовое."""
    if callback.message.photo or callback.message.document or callback.message.video:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        error_text = str(e).lower()
        if "message is not modified" in error_text:
            await callback.answer("Данные уже актуальны", show_alert=False)
        else:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)

async def pending_payments_menu(callback: CallbackQuery):
    """Меню просмотра ожидающих заявок на оплату"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    db = _get_payments_db()
    pending_requests = await db.get_pending_payment_requests()

    text = "💳 <b>Заявки на оплату подписок</b>\n\n"

    if not pending_requests:
        text += "📭 Нет ожидающих заявок"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Админ меню", callback_data="system_admin_panel")]
        ])
    else:
        text += f"📋 <b>Ожидающих заявок:</b> {len(pending_requests)}\n\n"

        keyboard_buttons = []

        for req in pending_requests[:10]:
            req_id = req[0]
            user_id = req[1]
            plan_type = req[2]
            amount = req[3]
            file_id = req[5]
            created_at = req[6]
            first_name = req[9]
            last_name = req[10]
            shop_name = req[11]

            plan_name = plan_type
            user_name = f"{first_name} {last_name}"

            button_text = f"#{req_id}: {user_name} - {plan_name} ({amount}₽)"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=button_text[:60] + "..." if len(button_text) > 60 else button_text,
                    callback_data=f"view_payment_{req_id}"
                )
            ])

        if len(pending_requests) > 10:
            keyboard_buttons.append([
                InlineKeyboardButton(text="📄 Показать все", callback_data="all_pending_payments")
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(text="⬅️ Админ меню", callback_data="system_admin_panel")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await safe_edit_message(callback, text, keyboard)

async def view_payment_request(callback: CallbackQuery):
    """Просмотр конкретной заявки на оплату"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    request_id = int(callback.data.split('_')[2])

    db = _get_payments_db()
    request_info = await db.get_payment_request_by_id(request_id)

    if not request_info:
        await callback.answer("❌ Заявка не найдена или уже обработана", show_alert=True)
        return

    await callback.answer()
    req_id = request_info[0]
    user_id = request_info[1]
    plan_type = request_info[2]
    amount = request_info[3]
    file_id = request_info[5]
    created_at = request_info[6]
    first_name = request_info[9]
    last_name = request_info[10]
    shop_name = request_info[11]

    plan_name = plan_type

    text = f"💳 <b>Заявка на оплату #{req_id}</b>\n\n"
    text += f"👤 <b>Пользователь:</b> {he(first_name or '')} {he(last_name or '')}\n"
    text += f"🏪 <b>Магазин:</b> {he(shop_name) if shop_name else 'Не указан'}\n"
    text += f"💎 <b>План:</b> {plan_name}\n"
    text += f"💰 <b>Сумма:</b> {amount}₽\n"
    text += f"📅 <b>Дата заявки:</b> {created_at[:19]}\n\n"
    text += f"📎 <b>Чек об оплате прикреплен</b>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_payment_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_payment_{req_id}")
        ],
        [InlineKeyboardButton(text="📎 Показать чек", callback_data=f"show_payment_proof_{req_id}")],
        [InlineKeyboardButton(text="⬅️ К заявкам", callback_data="pending_payments")]
    ])

    await safe_edit_message(callback, text, keyboard)

async def show_payment_proof(callback: CallbackQuery):
    """Показ чека об оплате"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    request_id = int(callback.data.split('_')[3])

    db = _get_payments_db()
    request_info = await db.get_payment_request_by_id(request_id)

    if not request_info:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    await callback.answer()
    req_id = request_info[0]
    user_id = request_info[1]
    plan_type = request_info[2]
    amount = request_info[3]
    file_id = request_info[5]
    created_at = request_info[6]
    first_name = request_info[9]
    last_name = request_info[10]
    shop_name = request_info[11]

    plan_name = plan_type

    caption = f"💳 <b>Чек по заявке #{req_id}</b>\n\n"
    caption += f"👤 {he(first_name)} {he(last_name)}\n"
    caption += f"💎 {he(plan_name)} - {amount}₽"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_payment_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_payment_{req_id}")
        ],
        [InlineKeyboardButton(text="⬅️ К заявке", callback_data=f"view_payment_{req_id}")]
    ])

    # Удаляем текущее текстовое сообщение перед отправкой фото,
    # чтобы в чате не оставался мусор из старых сообщений
    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await callback.message.answer_photo(
            photo=file_id,
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            "❌ Не удалось загрузить чек. Файл недоступен.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К заявке", callback_data=f"view_payment_{req_id}")]
            ])
        )

async def confirm_payment_request(callback: CallbackQuery):
    """Подтверждение заявки на оплату"""
    try:
        if not env_manager.is_super_admin(callback.from_user.id):
            await callback.answer("❌ Доступ только для супер-администратора")
            return

        request_id = int(callback.data.split('_')[2])
        db = _get_payments_db()
        admin_user_id = await db.get_user_id(callback.from_user.id)

        if not admin_user_id:
            await callback.answer("❌ Ошибка: администратор не найден в базе")
            return

        success = await db.confirm_payment_request(request_id, admin_user_id)
    except Exception:
        await callback.answer("❌ Произошла ошибка при подтверждении заявки")
        return

    if success:
        try:
            from subscription_utils import invalidate_plan_cache
            invalidate_plan_cache(callback.from_user.id)
        except Exception:
            pass
        try:
            def _fetch_request_user():
                import sqlite3 as _sql
                c = _sql.connect('data/shop_bot.db')
                cur = c.cursor()
                cur.execute('''
                    SELECT pr.user_id, pr.plan_type, u.telegram_id, u.first_name, u.last_name
                    FROM payment_requests pr
                    JOIN users u ON pr.user_id = u.id
                    WHERE pr.id = ?
                ''', (request_id,))
                res = cur.fetchone()
                c.close()
                return res
            result = await asyncio.to_thread(_fetch_request_user)

            if result:
                user_id, plan_type, user_telegram_id, first_name, last_name = result
                plan_name = plan_type

                # Если пользователь — org-admin, обновляем тариф организации + срок
                try:
                    import sqlite3 as _sql3
                    from datetime import datetime as _dt, timedelta as _td
                    # Получаем длительность плана из shop_bot.db
                    _sb = _sql3.connect('data/shop_bot.db')
                    _sb_cur = _sb.cursor()
                    _sb_cur.execute(
                        "SELECT duration_days FROM subscription_plans WHERE name = ?",
                        (plan_type,)
                    )
                    _plan_row = _sb_cur.fetchone()
                    _sb.close()
                    _duration = _plan_row[0] if _plan_row and _plan_row[0] else 0
                    _org_expires = (
                        (_dt.now() + _td(days=_duration)).strftime('%Y-%m-%d %H:%M:%S')
                        if _duration > 0 else None
                    )

                    main_conn = _sql3.connect('data/main.db')
                    main_cursor = main_conn.cursor()
                    main_cursor.execute(
                        "SELECT o.id FROM organizations o "
                        "JOIN user_org_mapping m ON o.id = m.org_id "
                        "WHERE m.telegram_id = ? AND m.role IN ('owner', 'admin')",
                        (user_telegram_id,)
                    )
                    org_row = main_cursor.fetchone()
                    if org_row:
                        main_cursor.execute(
                            "UPDATE organizations SET subscription_plan = ?, subscription_end = ? WHERE id = ?",
                            (plan_type, _org_expires, org_row[0])
                        )
                        main_conn.commit()
                    main_conn.close()
                except Exception:
                    pass

                try:
                    # Получаем детали плана для квитанции
                    _plan_price_str = ''
                    _end_str = ''
                    try:
                        import sqlite3 as _sql3r
                        from datetime import datetime as _dtr, timedelta as _tdr
                        _rc = _sql3r.connect('data/shop_bot.db')
                        _rcur = _rc.cursor()
                        _rcur.execute(
                            "SELECT duration_days, price FROM subscription_plans WHERE name = ?",
                            (plan_name,)
                        )
                        _prow = _rcur.fetchone()
                        _rc.close()
                        if _prow:
                            _dur, _price = _prow
                            if _price and _price > 0:
                                _plan_price_str = f"💰 <b>Сумма оплаты:</b> {_price:,.0f} ₽\n"
                            if _dur and _dur > 0:
                                _exp = _dtr.now() + _tdr(days=_dur)
                                _end_str = f"📅 <b>Действует до:</b> {_exp.strftime('%d.%m.%Y')}\n"
                    except Exception:
                        pass

                    _fname = he(first_name or '')
                    # Различаем обычные подписки и надстройки (add-ons)
                    if plan_name and plan_name.startswith('addon_'):
                        _addon_labels = {
                            'addon_shops_1': '🏪 Дополнительный магазин (+1)',
                            'addon_products_1': '📦 +100 товаров',
                        }
                        _addon_label = _addon_labels.get(plan_name, plan_name)
                        receipt_text = (
                            f"✅ <b>{_fname}, надстройка активирована!</b>\n\n"
                            f"➕ <b>Надстройка:</b> {_addon_label}\n"
                            f"{_plan_price_str}"
                            f"📅 <b>Действует:</b> 30 дней\n"
                            f"\n🎉 Надстройка добавлена к вашим лимитам прямо сейчас."
                        )
                    elif plan_name and (plan_name.startswith('module_') or plan_name.startswith('bundle_')):
                        # Модульный биллинг: красивое имя модуля/пакета
                        _is_bundle = plan_name.startswith('bundle_')
                        _bkey = plan_name[len('bundle_') if _is_bundle else len('module_'):]
                        _bitem_name = _bkey
                        try:
                            from database import Database as _DBr
                            _dbr = _DBr('data/shop_bot.db')
                            _items = _dbr.get_all_billing_bundles() if _is_bundle else _dbr.get_all_billing_modules()
                            for _it in _items:
                                if _it.get('key') == _bkey:
                                    _bitem_name = _it.get('name') or _bkey
                                    break
                        except Exception:
                            pass
                        _kind = 'Пакет' if _is_bundle else 'Модуль'
                        receipt_text = (
                            f"✅ <b>{_fname}, {_kind.lower()} подключён!</b>\n\n"
                            f"🧩 <b>{_kind}:</b> {he(_bitem_name)}\n"
                            f"{_plan_price_str}"
                            f"📅 <b>Действует:</b> 30 дней\n"
                            f"\n🎉 Все функции уже доступны в боте и веб-кабинете."
                        )
                    else:
                        receipt_text = (
                            f"✅ <b>{_fname}, подписка активирована!</b>\n\n"
                            f"📋 <b>Тариф:</b> {he(plan_name)}\n"
                            f"{_plan_price_str}"
                            f"{_end_str}"
                            f"\n🎉 Спасибо за покупку! Все возможности тарифа "
                            f"доступны прямо сейчас."
                        )
                    await callback.bot.send_message(
                        chat_id=user_telegram_id,
                        text=receipt_text,
                        parse_mode="HTML",
                        reply_markup=add_read_btn()
                    )
                except Exception:
                    pass
        except Exception:
            pass

        await callback.answer("✅ Заявка подтверждена, подписка активирована")

        text = f"✅ <b>Заявка #{request_id} подтверждена</b>\n\nПодписка успешно активирована!"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К заявкам", callback_data="pending_payments")],
            [InlineKeyboardButton(text="🔧 Админ меню", callback_data="system_admin_panel")]
        ])

        await safe_edit_message(callback, text, keyboard)
    else:
        await callback.answer("❌ Ошибка при подтверждении заявки")

async def reject_payment_request(callback: CallbackQuery):
    """Отклонение заявки на оплату"""
    try:
        if not env_manager.is_super_admin(callback.from_user.id):
            await callback.answer("❌ Доступ только для супер-администратора")
            return

        request_id = int(callback.data.split('_')[2])
        db = _get_payments_db()
        admin_user_id = await db.get_user_id(callback.from_user.id)

        if not admin_user_id:
            await callback.answer("❌ Ошибка: администратор не найден в базе")
            return
    except Exception:
        await callback.answer("❌ Произошла ошибка при отклонении заявки")
        return

    def _fetch_reject_user():
        import sqlite3 as _sql
        c = _sql.connect('data/shop_bot.db')
        cur = c.cursor()
        cur.execute('''
            SELECT pr.user_id, pr.plan_type, u.telegram_id, u.first_name, u.last_name
            FROM payment_requests pr
            JOIN users u ON pr.user_id = u.id
            WHERE pr.id = ? AND pr.status = 'pending'
        ''', (request_id,))
        res = cur.fetchone()
        c.close()
        return res
    result = await asyncio.to_thread(_fetch_reject_user)

    db = _get_payments_db()
    admin_user_id = await db.get_user_id(callback.from_user.id)
    success = await db.reject_payment_request(request_id, admin_user_id)

    if success and result:
        user_id, plan_type, user_telegram_id, first_name, last_name = result
        plan_name = plan_type

        try:
            await callback.bot.send_message(
                chat_id=user_telegram_id,
                text=f"❌ <b>Заявка отклонена</b>\n\n"
                     f"💎 <b>План:</b> {plan_name}\n"
                     f"📝 Ваша заявка на подписку была отклонена администратором.\n\n"
                     f"Возможные причины:\n"
                     f"• Неверная сумма оплаты\n"
                     f"• Нечитаемый чек\n"
                     f"• Другие проблемы с документами\n\n"
                     f"Свяжитесь с поддержкой для уточнения.",
                parse_mode="HTML",
                reply_markup=add_read_btn()
            )
        except Exception:
            pass

        await callback.answer("❌ Заявка отклонена")

        text = f"❌ <b>Заявка #{request_id} отклонена</b>\n\nПользователь уведомлен об отклонении."

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К заявкам", callback_data="pending_payments")],
            [InlineKeyboardButton(text="🔧 Админ меню", callback_data="system_admin_panel")]
        ])

        await safe_edit_message(callback, text, keyboard)
    else:
        await callback.answer("❌ Ошибка при отклонении заявки")

payment_admin_router.callback_query(F.data == "pending_payments")(pending_payments_menu)
payment_admin_router.callback_query(F.data == "all_pending_payments")(pending_payments_menu)
payment_admin_router.callback_query(F.data.startswith("view_payment_"))(view_payment_request)
payment_admin_router.callback_query(F.data.startswith("show_payment_proof_"))(show_payment_proof)
payment_admin_router.callback_query(F.data.startswith("confirm_payment_"))(confirm_payment_request)
payment_admin_router.callback_query(F.data.startswith("reject_payment_"))(reject_payment_request)
