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

_PROOF_PLACEHOLDERS = {"web_module_request", "web_tariff_request", "", None}

def _is_real_proof(file_id) -> bool:
    """Возвращает True, если file_id содержит реальный чек (не заглушку)."""
    return bool(file_id) and file_id not in _PROOF_PLACEHOLDERS

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
    """Меню просмотра ожидающих и отозванных заявок на оплату"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    db = _get_payments_db()
    pending_requests = await db.get_pending_payment_requests()
    cancelled_count = await db.get_cancelled_payment_requests_count()

    text = "💳 <b>Заявки на оплату подписок</b>\n\n"

    keyboard_buttons = []

    if not pending_requests:
        text += "📭 Нет ожидающих заявок"
        if cancelled_count:
            text += f"\n🚫 <b>Отозванных:</b> {cancelled_count}"

        keyboard_buttons.append([
            InlineKeyboardButton(text="🚫 Отозванные заявки", callback_data="cancelled_payments")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="⬅️ Админ меню", callback_data="system_admin_panel")
        ])
    else:
        text += f"📋 <b>Ожидающих заявок:</b> {len(pending_requests)}\n"
        text += f"🚫 <b>Отозванных:</b> {cancelled_count}\n\n"

        for req in pending_requests[:10]:
            req_id = req[0]
            plan_type = req[2]
            amount = req[3]
            first_name = req[9]
            last_name = req[10]

            user_name = f"{first_name} {last_name}"
            button_text = f"#{req_id}: {user_name} - {plan_type} ({amount}₽)"
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
            InlineKeyboardButton(text="🚫 Отозванные заявки", callback_data="cancelled_payments")
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
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    await callback.answer()
    req_id = request_info[0]
    plan_type = request_info[2]
    amount = request_info[3]
    status = request_info[4]
    file_id = request_info[5]
    created_at = request_info[6]
    first_name = request_info[9]
    last_name = request_info[10]
    shop_name = request_info[11]

    _STATUS_LABELS = {
        'pending':   '⏳ Ожидает рассмотрения',
        'approved':  '✅ Подтверждена',
        'rejected':  '❌ Отклонена',
        'cancelled': '🚫 Отозвано',
    }
    status_label = _STATUS_LABELS.get(status, status)

    text = f"💳 <b>Заявка на оплату #{req_id}</b>\n\n"
    text += f"📌 <b>Статус:</b> {status_label}\n"
    text += f"👤 <b>Пользователь:</b> {he(first_name or '')} {he(last_name or '')}\n"
    text += f"🏪 <b>Магазин:</b> {he(shop_name) if shop_name else 'Не указан'}\n"
    text += f"💎 <b>План:</b> {plan_type}\n"
    text += f"💰 <b>Сумма:</b> {amount}₽\n"
    text += f"📅 <b>Дата заявки:</b> {created_at[:19]}\n"
    has_proof = _is_real_proof(file_id)
    if has_proof:
        proof_label = "веб-скриншот" if file_id.startswith("web_proof:") else "Telegram-фото"
        text += f"\n📎 <b>Чек об оплате прикреплён</b> ({proof_label})"
    elif file_id in ("web_module_request", "web_tariff_request"):
        text += f"\n📎 <b>Скриншот не прикреплён</b> (заявка из веб-кабинета)"

    if status == 'cancelled':
        keyboard_rows = []
        if has_proof:
            keyboard_rows.append([InlineKeyboardButton(text="📎 Показать чек", callback_data=f"show_payment_proof_{req_id}")])
        keyboard_rows.append([InlineKeyboardButton(text="⬅️ К заявкам", callback_data="pending_payments")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    elif status == 'pending':
        action_row = [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_payment_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_payment_{req_id}")
        ]
        proof_row = [InlineKeyboardButton(text="📎 Показать чек", callback_data=f"show_payment_proof_{req_id}")] if has_proof else []
        nav_row = [InlineKeyboardButton(text="⬅️ К заявкам", callback_data="pending_payments")]
        keyboard = InlineKeyboardMarkup(inline_keyboard=([action_row] + ([proof_row] if proof_row else []) + [nav_row]))
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
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
    status = request_info[4]
    file_id = request_info[5]
    created_at = request_info[6]
    first_name = request_info[9]
    last_name = request_info[10]
    shop_name = request_info[11]

    plan_name = plan_type

    caption = f"💳 <b>Чек по заявке #{req_id}</b>\n\n"
    caption += f"👤 {he(first_name)} {he(last_name)}\n"
    caption += f"💎 {he(plan_name)} - {amount}₽"

    # Кнопки действий показываем только для ожидающих заявок.
    # Для отозванных/подтверждённых/отклонённых — только возврат к заявке.
    if status == 'pending':
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_payment_{req_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_payment_{req_id}")
            ],
            [InlineKeyboardButton(text="⬅️ К заявке", callback_data=f"view_payment_{req_id}")]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К заявке", callback_data=f"view_payment_{req_id}")]
        ])

    # Удаляем текущее текстовое сообщение перед отправкой фото,
    # чтобы в чате не оставался мусор из старых сообщений
    try:
        await callback.message.delete()
    except Exception:
        pass

    if file_id and file_id.startswith("web_proof:"):
        import os
        domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
        if domain:
            proof_url = f"https://{domain}/payment-proof-req/{req_id}"
            link_text = f'\n\n🔗 <a href="{proof_url}">Открыть скриншот →</a>'
        else:
            link_text = "\n\n📎 Скриншот загружен через веб-кабинет."
        await callback.message.answer(
            caption + link_text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return

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

                # Сбрасываем кэш тарифа ПЛАТЯЩЕГО пользователя (не админа),
                # чтобы новый доступ применился сразу без задержки.
                try:
                    from subscription_utils import invalidate_plan_cache
                    invalidate_plan_cache(user_telegram_id)
                except Exception:
                    pass

                # Тариф организации (main.db) теперь продлевается ВНУТРИ
                # confirm_payment_request → единый путь для бота и веб-кабинета
                # (см. Database._apply_org_subscription_after_payment).

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

                    # Реальная дата окончания доступа (single source of truth):
                    # читаем то, что фактически записал грант — для модулей/пакетов/
                    # расширений это billing_module_subs.end_date, для надстроек —
                    # subscription_addons.expires_at. Никаких догадок про длительность.
                    _grant_end_str = ''
                    try:
                        import sqlite3 as _sqle
                        from datetime import datetime as _dte
                        _ec = _sqle.connect('data/shop_bot.db')
                        _ecur = _ec.cursor()
                        _raw_end = None
                        if plan_name and plan_name.startswith('addon_'):
                            _ecur.execute(
                                "SELECT expires_at FROM subscription_addons "
                                "WHERE payment_request_id = ? ORDER BY id DESC LIMIT 1",
                                (request_id,)
                            )
                            _erow = _ecur.fetchone()
                            if _erow:
                                _raw_end = _erow[0]
                        elif plan_name and (
                            plan_name.startswith('module_')
                            or plan_name.startswith('bundle_')
                        ):
                            _ecur.execute(
                                "SELECT end_date FROM billing_module_subs "
                                "WHERE payment_request_id = ? ORDER BY id DESC LIMIT 1",
                                (request_id,)
                            )
                            _erow = _ecur.fetchone()
                            if _erow:
                                _raw_end = _erow[0]
                        if _raw_end is None:
                            # Обычная подписка — читаем реальную дату из subscriptions
                            _ecur.execute(
                                "SELECT end_date FROM subscriptions "
                                "WHERE user_id = ("
                                "  SELECT id FROM users WHERE telegram_id = ? LIMIT 1"
                                ") ORDER BY end_date DESC LIMIT 1",
                                (user_telegram_id,)
                            )
                            _srow = _ecur.fetchone()
                            if _srow:
                                _raw_end = _srow[0]
                        _ec.close()
                        if _raw_end:
                            _ds = str(_raw_end).replace('T', ' ')[:10]
                            _dobj = _dte.strptime(_ds, '%Y-%m-%d')
                            _grant_end_str = (
                                f"📅 <b>Действует до:</b> "
                                f"{_dobj.strftime('%d.%m.%Y')}\n"
                            )
                    except Exception:
                        pass

                    _fname = he(first_name or '')
                    # Годовые модули/пакеты: plan_type вида module_annual_<key>
                    _is_annual = bool(plan_name) and '_annual_' in plan_name
                    _period_days_label = '365 дней' if _is_annual else '30 дней'
                    # Предпочитаем реальную дату окончания; обобщённая длительность —
                    # только запасной вариант, если дату не удалось прочитать.
                    _duration_line = _grant_end_str or (
                        f"📅 <b>Действует:</b> {_period_days_label}\n"
                    )
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
                            f"{_duration_line}"
                            f"\n🎉 Надстройка добавлена к вашим лимитам прямо сейчас."
                        )
                    elif plan_name and (plan_name.startswith('module_') or plan_name.startswith('bundle_')):
                        # Модульный биллинг: красивое имя модуля/пакета
                        _is_bundle = plan_name.startswith('bundle_')
                        _bkey = plan_name[len('bundle_') if _is_bundle else len('module_'):]
                        if _bkey.startswith('annual_'):
                            _bkey = _bkey[len('annual_'):]
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
                            f"{_duration_line}"
                            f"\n🎉 Все функции уже доступны в боте и веб-кабинете."
                        )
                    else:
                        receipt_text = (
                            f"✅ <b>{_fname}, подписка активирована!</b>\n\n"
                            f"📋 <b>Тариф:</b> {he(plan_name)}\n"
                            f"{_plan_price_str}"
                            f"{_grant_end_str or _end_str}"
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

def _parse_cancelled_filter(data: str):
    """Извлекает since_days и метку из callback_data.

    Форматы:
      cancelled_payments        → все (None)
      cancelled_payments_7d     → последние 7 дней
      cancelled_payments_30d    → последние 30 дней
      all_cancelled_payments    → все (None, псевдоним)
    """
    if data.endswith("_7d"):
        return 7, "за последние 7 дней"
    if data.endswith("_30d"):
        return 30, "за последние 30 дней"
    return None, "за всё время"


async def cancelled_payments_menu(callback: CallbackQuery):
    """Список отозванных/отменённых заявок с фильтром по периоду."""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()

    since_days, period_label = _parse_cancelled_filter(callback.data)

    db = _get_payments_db()
    cancelled = await db.get_cancelled_payment_requests(since_days=since_days)

    active_7 = "✅ " if since_days == 7 else ""
    active_30 = "✅ " if since_days == 30 else ""
    active_all = "✅ " if since_days is None else ""

    filter_row = [
        InlineKeyboardButton(text=f"{active_7}7 дней", callback_data="cancelled_payments_7d"),
        InlineKeyboardButton(text=f"{active_30}30 дней", callback_data="cancelled_payments_30d"),
        InlineKeyboardButton(text=f"{active_all}Все", callback_data="cancelled_payments"),
    ]

    text = "🚫 <b>Отозванные заявки на оплату</b>\n\n"

    if not cancelled:
        text += f"📭 Нет отозванных заявок {period_label}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            filter_row,
            [InlineKeyboardButton(text="⬅️ К заявкам", callback_data="pending_payments")]
        ])
    else:
        text += f"📋 <b>Отозванных заявок {period_label}:</b> {len(cancelled)}\n\n"
        keyboard_buttons = [filter_row]

        for req in cancelled[:15]:
            req_id = req[0]
            plan_type = req[2]
            amount = req[3]
            first_name = req[9]
            last_name = req[10]

            user_name = f"{first_name or ''} {last_name or ''}".strip() or "—"
            button_text = f"🚫 #{req_id}: {user_name} — {plan_type} ({amount}₽)"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=button_text[:60] + "…" if len(button_text) > 60 else button_text,
                    callback_data=f"view_cancelled_{req_id}"
                )
            ])

        if len(cancelled) > 15:
            text += f"<i>Показаны первые 15 из {len(cancelled)}. Уточните период фильтром выше.</i>\n"

        keyboard_buttons.append([
            InlineKeyboardButton(text="⬅️ К заявкам", callback_data="pending_payments")
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await safe_edit_message(callback, text, keyboard)


async def view_cancelled_payment(callback: CallbackQuery):
    """Просмотр отозванной заявки (только чтение, без кнопок действий)"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    request_id = int(callback.data.split('_')[2])

    db = _get_payments_db()
    request_info = await db.get_payment_request_by_id(request_id)

    if not request_info:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    req_id = request_info[0]
    plan_type = request_info[2]
    amount = request_info[3]
    status = request_info[4]
    created_at = request_info[6]
    processed_at = request_info[7]
    first_name = request_info[9]
    last_name = request_info[10]
    shop_name = request_info[11]

    if status != 'cancelled':
        await callback.answer("⚠️ Статус заявки изменился", show_alert=True)
        return

    await callback.answer()

    withdrawn_at = (processed_at or "")[:19] or "неизвестно"

    text = f"🚫 <b>Отозванная заявка #{req_id}</b>\n\n"
    text += f"👤 <b>Пользователь:</b> {he(first_name or '')} {he(last_name or '')}\n"
    text += f"🏪 <b>Магазин:</b> {he(shop_name) if shop_name else 'Не указан'}\n"
    text += f"💎 <b>Тариф/модуль:</b> {he(plan_type or '—')}\n"
    text += f"💰 <b>Сумма:</b> {amount}₽\n"
    text += f"📅 <b>Дата подачи:</b> {created_at[:19]}\n"
    text += f"🗑 <b>Отозвана:</b> {withdrawn_at}\n\n"
    text += "ℹ️ Заявка отозвана владельцем и не требует действий."

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К отозванным", callback_data="cancelled_payments")]
    ])

    await safe_edit_message(callback, text, keyboard)


payment_admin_router.callback_query(F.data == "pending_payments")(pending_payments_menu)
payment_admin_router.callback_query(F.data == "all_pending_payments")(pending_payments_menu)
payment_admin_router.callback_query(F.data == "cancelled_payments")(cancelled_payments_menu)
payment_admin_router.callback_query(F.data == "all_cancelled_payments")(cancelled_payments_menu)
payment_admin_router.callback_query(F.data == "cancelled_payments_7d")(cancelled_payments_menu)
payment_admin_router.callback_query(F.data == "cancelled_payments_30d")(cancelled_payments_menu)
payment_admin_router.callback_query(F.data.startswith("view_payment_"))(view_payment_request)
payment_admin_router.callback_query(F.data.startswith("view_cancelled_"))(view_cancelled_payment)
payment_admin_router.callback_query(F.data.startswith("show_payment_proof_"))(show_payment_proof)
payment_admin_router.callback_query(F.data.startswith("confirm_payment_"))(confirm_payment_request)
payment_admin_router.callback_query(F.data.startswith("reject_payment_"))(reject_payment_request)
