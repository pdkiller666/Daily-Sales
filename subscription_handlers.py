"""
Обработчики для системы подписок
"""

import os
import sqlite3
from datetime import datetime, timedelta
from aiogram import types
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from database import Database
from keyboards import back_button, create_confirm_keyboard
from states import SubscriptionStates
from env_manager import env_manager
from notif_utils import add_read_btn
from message_utils import safe_edit_message, safe_answer_callback, fsm_edit
from db_utils import get_user_org_role, clear_state_keep_org, wrap_db



def _get_db():
    """Локальная БД для платёжного/подписочного функционала"""
    return wrap_db(Database('data/shop_bot.db'))

async def notify_admins_about_payment_request(bot, user_id, plan_type, amount):
    """Уведомление супер-администратора о новой заявке на оплату"""
    db = _get_db()
    try:
        super_admin_id = env_manager.get_main_admin_id()  # Только супер-админ
        
        if not super_admin_id:
            return
        
        # Получаем данные пользователя
        user = await db.get_user_by_id(user_id)
        if not user:
            return
        
        from utils import he
        # users table: (0)id, (1)telegram_id, (2)first_name, (3)last_name, ...
        user_name = he(f"{user[2]} {user[3]}")  # first_name + last_name
        user_telegram_id = user[1]              # telegram_id
        
        notification_text = (
            "🔔 <b>Новая заявка на оплату!</b>\n\n"
            f"👤 <b>Пользователь:</b> {user_name}\n"
            f"🆔 <b>Telegram ID:</b> {user_telegram_id}\n"
            f"📋 <b>Тарифный план:</b> {plan_type}\n"
            f"💰 <b>Сумма:</b> {amount}₽\n\n"
            "⏰ Заявка ожидает рассмотрения в административной панели."
        )
        
        keyboard = add_read_btn(InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Рассмотреть заявки", callback_data="pending_payments")]
        ]))
        
        # Отправляем уведомление только супер-администратору
        try:
            await bot.send_message(
                chat_id=super_admin_id,
                text=notification_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            pass

    except Exception:
        pass

def get_current_subscription_plans():
    """Получение актуальных тарифных планов из базы данных"""
    db = Database('data/shop_bot.db')  # sync-функция, нужен прямой доступ
    plans = db.get_subscription_plans()
    plans_dict = {}
    for plan in plans:
        plan_id = plan[0]
        name = plan[1]
        duration_days = plan[2]
        price = plan[3]
        description = plan[4]
        is_active = plan[5]
        if is_active and price > 0:  # Только активные платные планы
            plans_dict[f"plan_{plan_id}"] = {
                'id': plan_id,
                'name': name,
                'price': price,
                'duration': duration_days,
                'description': description
            }
    return plans_dict

async def subscription_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню подписок"""
    await callback.answer()
    from subscription_utils import get_plan_limits, _get_org_plan_for_user, _get_personal_plan
    await clear_state_keep_org(state)

    telegram_id = callback.from_user.id
    is_super_admin = env_manager.is_super_admin(telegram_id)

    text = "💎 <b>Управление подпиской</b>\n\n"

    if is_super_admin:
        text += "<i>✨ Вы супер-администратор — доступ ко всем функциям без ограничений.</i>"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Мой статус", callback_data="subscription_limits")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
        ])
    else:
        # Определяем: org-пользователь или личный
        org_plan = _get_org_plan_for_user(telegram_id)
        is_org_user = org_plan is not None

        if is_org_user:
            plan_type = org_plan
            end_date = None
            text += f"🏢 <b>Тариф вашей организации:</b> {plan_type}\n"
            text += "<i>Тариф управляется администратором организации.</i>\n\n"
        else:
            # Личный пользователь — ищем подписку в shop_bot.db
            db = _get_db()
            user_id = await db.get_user_id(telegram_id)
            subscription = await db.get_user_subscription(user_id) if user_id else None
            if subscription:
                plan_type = subscription[2]
                end_date = subscription[4]
            else:
                plan_type = 'Бесплатный'
                end_date = None
            text += f"📦 <b>Текущий план:</b> {plan_type}\n"

        limits = get_plan_limits(telegram_id)

        if plan_type in ('Бесплатный', 'free'):
            text += "⚠️ <b>Ограничения плана:</b>\n"
        else:
            text += "✅ <b>Возможности плана:</b>\n"

        text += ("• Товары: ∞ Безлимит\n" if limits['max_products'] == -1
                 else f"• Товары: до {limits['max_products']}\n")
        text += ("• Магазины: ∞ Безлимит\n" if limits['max_shops'] == -1
                 else f"• Магазины: до {limits['max_shops']}\n")
        text += ("• Продажи/мес: ∞ Безлимит\n" if limits['max_sales_per_month'] == -1
                 else f"• Продажи/мес: до {limits['max_sales_per_month']}\n")
        text += f"• Экспорт отчётов: {'✅' if limits['can_export_reports'] else '❌'}\n"
        text += f"• Аналитика: {'✅' if limits['can_view_analytics'] else '❌'}\n"
        text += f"• Уведомления: {'✅' if limits['can_use_notifications'] else '❌'}\n"
        text += f"• Google Таблицы: {'✅' if limits.get('can_use_integrations', False) else '❌'}\n"

        if not is_org_user and plan_type not in ('Бесплатный', 'free') and end_date and end_date != '9999-12-31 23:59:59':
            try:
                end_dt = datetime.fromisoformat(end_date)
                days_left = (end_dt - datetime.now()).days
                if days_left > 0:
                    text += f"\n📅 <b>Действует до:</b> {end_dt.strftime('%d.%m.%Y')} ({days_left} дн.)\n"
                else:
                    text += "\n⚠️ <b>Подписка истекла</b>\n"
            except Exception:
                pass

        if plan_type in ('Бесплатный', 'free'):
            text += "\n💎 <b>Обновите план для получения больших возможностей!</b>"

        if is_org_user:
            org_role = get_user_org_role(telegram_id)
            is_org_admin = org_role in ('owner', 'admin')
            org_buttons = [[InlineKeyboardButton(text="📊 Мои лимиты", callback_data="subscription_limits")]]
            if is_org_admin:
                org_buttons.append([InlineKeyboardButton(text="💳 Купить подписку для организации", callback_data="subscription_plans")])
                org_buttons.append([InlineKeyboardButton(text="➕ Надстройки", callback_data="subscription_addons")])
            org_buttons.append([InlineKeyboardButton(text="🔗 Реферальная программа", callback_data="subscription_referral")])
            org_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=org_buttons)
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Купить подписку", callback_data="subscription_plans")],
                [InlineKeyboardButton(text="📊 Мои лимиты", callback_data="subscription_limits")],
                [InlineKeyboardButton(text="➕ Надстройки", callback_data="subscription_addons")],
                [InlineKeyboardButton(text="🔗 Реферальная программа", callback_data="subscription_referral")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
            ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

async def subscription_plans(callback: CallbackQuery):
    """Выбор тарифного плана"""
    await callback.answer()
    db = _get_db()
    text = "💳 <b>Выберите тарифный план:</b>\n\n"
    
    keyboard_buttons = []
    plans = get_current_subscription_plans()
    
    if not plans:
        text += "❌ Нет доступных тарифных планов"
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_menu")])
    else:
        for plan_id, plan_info in plans.items():
            price_per_month = plan_info['price'] / (plan_info['duration'] / 30)
            text += f"💎 <b>{plan_info['name']}</b>\n"
            text += f"💰 {plan_info['price']:.0f}₽ (≈{price_per_month:.0f}₽/мес)\n"
            if plan_info['description']:
                text += f"📝 {plan_info['description']}\n"
            text += "\n"
            
            callback_data = f"subscribe_{plan_id}"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"{plan_info['name']} - {plan_info['price']:.0f}₽",
                    callback_data=callback_data
                )
            ])
        
        # Динамическое отображение лимитов планов из базы данных
        all_plans = await db.get_subscription_plans()
        
        if all_plans:
            text += "📋 <b>Сравнение планов:</b>\n\n"
            
            for plan in all_plans:
                plan_details = await db.get_subscription_plan_details(plan[0])
                if plan_details:
                    text += f"💎 <b>{plan_details['name']}</b>\n"
                    
                    # Лимиты товаров
                    if plan_details['max_products'] == -1:
                        text += "📦 Товары: ∞ Безлимит\n"
                    else:
                        text += f"📦 Товары: до {plan_details['max_products']}\n"
                    
                    # Лимиты магазинов
                    if plan_details['max_shops'] == -1:
                        text += "🏪 Магазины: ∞ Безлимит\n"
                    else:
                        text += f"🏪 Магазины: до {plan_details['max_shops']}\n"
                    
                    # Лимиты продаж
                    if plan_details['max_sales_per_month'] == -1:
                        text += "💰 Продажи/месяц: ∞ Безлимит\n"
                    else:
                        text += f"💰 Продажи/месяц: до {plan_details['max_sales_per_month']}\n"
                    
                    # Доступные функции
                    text += f"📋 Экспорт: {'✅' if plan_details['can_export_reports'] else '❌'}\n"
                    text += f"📈 Аналитика: {'✅' if plan_details['can_view_analytics'] else '❌'}\n"
                    text += f"🔔 Уведомления: {'✅' if plan_details['can_use_notifications'] else '❌'}\n"
                    text += "\n"
        
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_menu")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

async def start_subscription_purchase(callback: CallbackQuery, state: FSMContext):
    """Начало покупки подписки"""
    db = _get_db()
    # Callback data имеет формат "subscribe_plan_ID"
    # Извлекаем "plan_ID" из "subscribe_plan_ID"
    if not callback.data.startswith("subscribe_"):
        await callback.answer("Ошибка: неверный формат данных")
        return
    
    plan_key = callback.data[10:]  # Убираем "subscribe_" в начале
    plans = get_current_subscription_plans()
    
    if plan_key not in plans:
        await callback.answer("Ошибка: план не найден")
        return
    
    await callback.answer()
    plan_info = plans[plan_key]
    user_id = await db.get_user_id(callback.from_user.id)
    
    # Проверяем на понижение тарифа
    is_downgrade, downgrade_info = await db.check_subscription_downgrade(user_id, plan_info['name'])
    
    if is_downgrade:
        from datetime import datetime
        end_date = downgrade_info['end_datetime'].strftime('%d.%m.%Y')
        days_left = (downgrade_info['end_datetime'] - datetime.now()).days
        
        text = f"⚠️ <b>ВНИМАНИЕ: Понижение тарифа</b>\n\n"
        text += f"У вас активна подписка <b>{downgrade_info['current_plan']}</b>\n"
        text += f"📅 Действует до: {end_date} ({days_left} дн.)\n\n"
        text += f"Вы выбрали план <b>{plan_info['name']}</b>, который предоставляет меньше возможностей.\n\n"
        text += "🔄 <b>Варианты действий:</b>\n"
        text += "• <b>Отложенная активация</b> - новый план активируется после окончания текущего\n"
        text += "• <b>Немедленная замена</b> - текущий план будет заменен сразу (остаток сгорит)\n\n"
        text += f"💰 <b>Сумма к оплате:</b> {plan_info['price']:.0f}₽\n"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Отложенная активация", callback_data=f"schedule_{plan_key}")],
            [InlineKeyboardButton(text="⚡ Немедленная замена", callback_data=f"immediate_{plan_key}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")]
        ])
        
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return
    
    # Обычный процесс покупки (улучшение или первая покупка)
    await proceed_with_purchase(callback, state, plan_info)

async def proceed_with_purchase(callback: CallbackQuery, state: FSMContext, plan_info, plan_key: str = None, is_scheduled=False, schedule_date=None):
    """Продолжение процесса покупки подписки"""
    db = _get_db()
    original_price = plan_info['price']

    # Если plan_key не передан явно — пробуем извлечь из callback.data (формат "subscribe_plan_X")
    if plan_key is None:
        plan_key = callback.data[10:]  # Убираем "subscribe_"

    text = f"💳 <b>Оформление подписки: {plan_info['name']}</b>\n\n"
    
    if is_scheduled and schedule_date:
        text += f"⏰ <b>Тип активации:</b> Отложенная\n"
        text += f"📅 <b>Начало действия:</b> {schedule_date}\n\n"
    
    text += f"💰 <b>Стоимость:</b> {original_price:.0f}₽\n"
    text += f"📅 <b>Срок действия:</b> {plan_info['duration']} дней\n\n"
    
    text += "🎁 <b>У вас есть промокод?</b>\n"
    text += "Введите его для получения скидки или продолжите без промокода."
    
    await state.update_data(
        plan_type=plan_info['name'], 
        original_amount=original_price,
        final_amount=original_price,
        is_scheduled=is_scheduled,
        schedule_date=schedule_date,
        promocode_applied=None
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data=f"enter_promocode_{plan_key}")],
        [InlineKeyboardButton(text="💳 Продолжить без промокода", callback_data=f"proceed_payment_{plan_key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

async def process_payment_proof(message: Message, state: FSMContext):
    """Обработка скриншота оплаты"""
    db = _get_db()
    if not message.photo:
        await fsm_edit(state, message, "❌ Пожалуйста, отправьте скриншот перевода (фото).",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")]]))
        return
    
    data = await state.get_data()
    plan_type = data.get('plan_type')
    amount = data.get('amount')
    is_scheduled = data.get('is_scheduled', False)
    schedule_date = data.get('schedule_date')
    promocode_data = data.get('promocode_data')
    
    user_id = await db.get_user_id(message.from_user.id)

    # Org-пользователь может отсутствовать в shop_bot.db — копируем из tenant DB
    if user_id is None:
        try:
            from tenant_manager import tenant_manager
            from database import Database as _DB
            db_path = tenant_manager.get_user_db_path(message.from_user.id)
            if db_path != 'data/shop_bot.db':
                org_db = _DB(db_path)
                u = org_db.get_user(message.from_user.id)
                if u:
                    await db.add_user(
                        telegram_id=u[1], first_name=u[2], last_name=u[3],
                        middle_name=u[4], phone=u[5], email=u[6],
                        trade_network=u[7], shop_name=u[8], city=u[9],
                        username=u[12] if len(u) > 12 else None
                    )
                    user_id = await db.get_user_id(message.from_user.id)
        except Exception:
            pass

    if not user_id:
        await fsm_edit(state, message, "❌ Ошибка: профиль не найден. Обратитесь в поддержку.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")]]))
        return

    # Дедупликация: не создавать вторую заявку если уже есть активная
    if await db.has_pending_payment_request(user_id):
        await fsm_edit(state, message,
                       "⏳ <b>У вас уже есть активная заявка на оплату</b>\n\n"
                       "Дождитесь рассмотрения текущей заявки администратором.\n"
                       "Как правило, это занимает до 24 часов.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="🔙 К подписке", callback_data="subscription_menu")]
                       ]))
        await clear_state_keep_org(state)
        return

    file_id = message.photo[-1].file_id

    # Сохраняем ID промокода для применения только при одобрении заявки
    promo_id = promocode_data.get('id') if promocode_data else None

    # Создаем заявку на оплату (промокод применится в confirm_payment_request)
    try:
        success = await db.create_payment_request(user_id, plan_type, amount, file_id, promo_id)
    except Exception as e:
        import logging
        logging.error(f"process_payment_proof: create_payment_request failed: {e}")
        from keyboards import main_menu
        user = await db.get_user(message.from_user.id)
        keyboard = main_menu(message.chat.id, user[8] if user else None)
        await fsm_edit(state, message,
                       "❌ Ошибка при создании заявки. Попробуйте позже.\n\n🏠 Возврат в главное меню:",
                       reply_markup=keyboard)
        await clear_state_keep_org(state)
        return

    if success:
        
        # Формируем текст уведомления
        if is_scheduled:
            notification_text = "✅ <b>Заявка на отложенную подписку отправлена!</b>\n\n" \
                               f"Ваша заявка на план <b>{plan_type}</b> поступила на рассмотрение администратору.\n" \
                               f"📅 <b>Активация:</b> {schedule_date}\n"
        else:
            notification_text = "✅ <b>Заявка на оплату отправлена!</b>\n\n" \
                               "Ваша заявка поступила на рассмотрение администратору.\n"
        
        if promocode_data:
            original_price = data.get('original_amount', amount)
            discount_amount = original_price - amount
            notification_text += f"🎁 <b>Промокод:</b> {promocode_data['code']}\n" \
                               f"💰 <b>Скидка:</b> {promocode_data['discount_percent']}% (-{discount_amount:.0f}₽)\n" \
                               f"💸 <b>Было:</b> {original_price:.0f}₽\n"
        
        notification_text += f"💳 <b>К оплате:</b> {amount:.0f}₽\n\n"
        
        if is_scheduled:
            notification_text += "Подписка будет активирована в указанную дату после подтверждения оплаты.\n\n"
        else:
            notification_text += "Подписка будет активирована после подтверждения оплаты.\n\n"
        
        notification_text += "⏰ Обычно проверка занимает до 24 часов."
        
        # Импортируем функцию главного меню
        from keyboards import main_menu
        
        # Отправляем финальное сообщение с главным меню
        user = await db.get_user(message.from_user.id)
        keyboard = main_menu(message.chat.id, user[8] if user else None)
        
        final_text = notification_text + "\n\n🏠 Возврат в главное меню:"
        
        await fsm_edit(state, message, final_text, reply_markup=keyboard)
        
        # Уведомляем администраторов о новой заявке
        await notify_admins_about_payment_request(message.bot, user_id, plan_type, amount)
    else:
        from keyboards import main_menu
        user = await db.get_user(message.from_user.id)
        keyboard = main_menu(message.chat.id, user[8] if user else None)
        
        await fsm_edit(state, message,
                       "❌ Ошибка при создании заявки. Попробуйте позже.\n\n🏠 Возврат в главное меню:",
                       reply_markup=keyboard)
    
    await clear_state_keep_org(state)

async def subscription_limits(callback: CallbackQuery):
    """Отображение лимитов пользователя"""
    await callback.answer()
    from subscription_utils import get_plan_limits, _get_org_plan_for_user
    from database import Database as _DB
    from tenant_manager import tenant_manager

    telegram_id = callback.from_user.id
    is_super_admin = env_manager.is_super_admin(telegram_id)

    if is_super_admin:
        text = "👑 <b>Супер-администратор</b>\n\n"
        text += "✨ У вас неограниченный доступ ко всем функциям системы.\n\n"
        text += "📦 <b>Товары:</b> Безлимит\n"
        text += "🏪 <b>Магазины:</b> Безлимит\n"
        text += "💰 <b>Продажи:</b> Безлимит\n"
        text += "📋 <b>Экспорт отчетов:</b> ✅ Доступен\n"
        text += "📈 <b>Расширенная аналитика:</b> ✅ Доступна\n"
        text += "🔔 <b>Уведомления:</b> ✅ Доступны\n"
    else:
        # Определяем тип пользователя и его план
        org_plan = _get_org_plan_for_user(telegram_id)
        is_org_user = org_plan is not None

        if is_org_user:
            plan_type = org_plan
            end_date = None
        else:
            db = _get_db()
            user_id_shop = await db.get_user_id(telegram_id)
            subscription = await db.get_user_subscription(user_id_shop) if user_id_shop else None
            plan_type = subscription[2] if subscription else 'Бесплатный'
            end_date = subscription[4] if subscription else None

        # Лимиты через subscription_utils — единственный правильный источник для всех типов
        limits = get_plan_limits(telegram_id)

        text = f"📊 <b>Текущий план:</b> {plan_type}\n"
        if is_org_user:
            text += "<i>(тариф организации)</i>\n"
        text += "\n"

        # Дата окончания только для личных платных планов
        if not is_org_user and end_date and end_date != '9999-12-31 23:59:59':
            try:
                end_datetime = datetime.fromisoformat(end_date)
                days_left = (end_datetime - datetime.now()).days
                if days_left > 0:
                    text += f"📅 <b>Действует до:</b> {end_datetime.strftime('%d.%m.%Y')} ({days_left} дн.)\n\n"
                else:
                    text += f"⚠️ <b>Подписка истекла:</b> {end_datetime.strftime('%d.%m.%Y')}\n\n"
            except Exception:
                pass

        # Счётчики из правильной БД пользователя
        try:
            user_db_path = tenant_manager.get_user_db_path(telegram_id)
            user_db = _DB(user_db_path)
            db_user_id = user_db.get_user_id(telegram_id)
        except Exception:
            user_db = Database('data/shop_bot.db')  # sync fallback — no await needed
            db_user_id = user_db.get_user_id(telegram_id)

        text += "<b>📋 Ваши лимиты:</b>\n"

        if limits['max_products'] == -1:
            text += "📦 <b>Товары:</b> ∞ Безлимит\n"
        else:
            try:
                current_products = len(user_db.get_all_products())
            except Exception:
                current_products = 0
            text += f"📦 <b>Товары:</b> {current_products}/{limits['max_products']}\n"

        if limits['max_shops'] == -1:
            text += "🏪 <b>Магазины:</b> ∞ Безлимит\n"
        else:
            try:
                current_shops = len(user_db.get_all_shops())
            except Exception:
                current_shops = 0
            text += f"🏪 <b>Магазины:</b> {current_shops}/{limits['max_shops']}\n"

        if limits['max_sales_per_month'] == -1:
            text += "💰 <b>Продажи/месяц:</b> ∞ Безлимит\n"
        else:
            try:
                now = datetime.now()
                month_start = f"{now.strftime('%Y-%m')}-01"
                month_end = now.strftime('%Y-%m-%d')
                user_sales = user_db.get_user_sales_by_date(db_user_id, month_start, month_end)
                current_sales = len(user_sales)
            except Exception:
                current_sales = 0
            text += f"💰 <b>Продажи/месяц:</b> {current_sales}/{limits['max_sales_per_month']}\n"

        text += f"\n<b>🔧 Доступные функции:</b>\n"
        text += f"📋 <b>Экспорт отчетов:</b> {'✅ Да' if limits['can_export_reports'] else '❌ Нет'}\n"
        text += f"📈 <b>Расширенная аналитика:</b> {'✅ Да' if limits['can_view_analytics'] else '❌ Нет'}\n"
        text += f"🔔 <b>Уведомления:</b> {'✅ Да' if limits['can_use_notifications'] else '❌ Нет'}\n"

        if plan_type in ('Бесплатный', 'free'):
            text += "\n💡 <b>Совет:</b> Обновите план для получения больших лимитов и дополнительных функций."

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

async def handle_scheduled_purchase(callback: CallbackQuery, state: FSMContext):
    """Обработка отложенной покупки подписки"""
    db = _get_db()
    plan_key = callback.data[9:]  # Убираем "schedule_" (9 символов)
    plans = get_current_subscription_plans()
    
    if plan_key not in plans:
        await callback.answer("Ошибка: план не найден")
        return
    
    await callback.answer()
    plan_info = plans[plan_key]
    user_id = await db.get_user_id(callback.from_user.id)
    
    # Получаем информацию о текущей подписке
    current_subscription = await db.get_user_subscription(user_id)
    end_date = current_subscription[4]
    
    from datetime import datetime
    end_datetime = datetime.fromisoformat(end_date)
    schedule_date = end_datetime.strftime('%d.%m.%Y')
    
    await proceed_with_purchase(callback, state, plan_info, plan_key=plan_key, is_scheduled=True, schedule_date=schedule_date)

async def handle_immediate_purchase(callback: CallbackQuery, state: FSMContext):
    """Обработка немедленной покупки подписки"""
    db = _get_db()
    plan_key = callback.data[10:]  # Убираем "immediate_" (10 символов)
    plans = get_current_subscription_plans()
    
    if plan_key not in plans:
        await callback.answer("Ошибка: план не найден")
        return
    
    await callback.answer()
    plan_info = plans[plan_key]
    await proceed_with_purchase(callback, state, plan_info, plan_key=plan_key)

async def upload_payment_proof(callback: CallbackQuery, state: FSMContext):
    """Загрузка скриншота оплаты - совместимость"""
    db = _get_db()
    await start_subscription_purchase(callback, state)

async def enter_promocode(callback: CallbackQuery, state: FSMContext):
    """Начало ввода промокода"""
    await callback.answer()
    db = _get_db()
    plan_key = callback.data[16:]  # Убираем "enter_promocode_" (16 символов)
    
    text = "🎁 <b>Введите промокод</b>\n\n" \
           "Отправьте текст с кодом промокода для получения скидки.\n" \
           "Промокод должен состоять из букв и цифр."
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"proceed_payment_{plan_key}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await state.update_data(plan_key=plan_key, anchor_msg_id=callback.message.message_id)
    await state.set_state(SubscriptionStates.waiting_for_promocode)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

async def process_promocode(message: Message, state: FSMContext):
    """Обработка введенного промокода"""
    db = _get_db()
    promocode = message.text.strip().upper()
    data = await state.get_data()
    plan_key = data.get('plan_key')
    promo_attempts = data.get('promo_attempts', 0)

    _promo_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")]
    ])
    if not plan_key:
        await fsm_edit(state, message, "❌ Ошибка: данные о плане потеряны. Начните заново.", reply_markup=_promo_kb)
        return

    _MAX_PROMO_ATTEMPTS = 5
    if promo_attempts >= _MAX_PROMO_ATTEMPTS:
        await fsm_edit(state, message,
                       "🚫 Превышено количество попыток ввода промокода. Попробуйте позже.",
                       reply_markup=_promo_kb)
        await clear_state_keep_org(state)
        return

    plans = get_current_subscription_plans()
    
    if plan_key not in plans:
        await fsm_edit(state, message, "❌ Ошибка: план не найден. Начните заново.", reply_markup=_promo_kb)
        return
    
    plan_info = plans[plan_key]
    original_amount = plan_info['price']
    
    validation_result = await db.validate_promocode(
        promocode, user_id=message.from_user.id, plan_key=plan_key
    )

    if not validation_result['valid']:
        await state.update_data(promo_attempts=promo_attempts + 1)
        remaining = _MAX_PROMO_ATTEMPTS - promo_attempts - 1
        await fsm_edit(state, message,
                       f"❌ {validation_result['error']}\n\nПопробуйте ещё раз или продолжите без промокода."
                       + (f" (осталось попыток: {remaining})" if remaining <= 2 else ""),
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="💳 Продолжить без промокода", callback_data=f"proceed_payment_{plan_key}")],
                           [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")]
                       ]))
        return

    # Рассчитываем скидку с учётом типа (процент или фиксированная сумма)
    discount_value = validation_result['discount_percent']
    discount_type = validation_result.get('discount_type', 'percent')
    final_amount = await db.calculate_discounted_price(original_amount, discount_value, discount_type)
    discount_amount = original_amount - final_amount

    # Обновляем данные состояния с полной информацией
    await state.update_data(
        plan_key=plan_key,
        plan_type=plan_info['name'],
        original_amount=original_amount,
        final_amount=final_amount,
        promocode_applied=validation_result,
        discount_amount=discount_amount
    )

    # Формируем строку скидки для отображения
    if discount_type == 'fixed':
        discount_str = f"{discount_value:.0f}₽ → -{discount_amount:.0f}₽"
    else:
        discount_str = f"{discount_value}% → -{discount_amount:.0f}₽"

    # Дополнительная информация
    extra_lines = ""
    remaining_slots = validation_result.get('remaining_usage', 0) - 1
    if remaining_slots >= 0 and remaining_slots <= 5:
        extra_lines += f"🔥 Осталось использований: {remaining_slots}\n"
    expires = validation_result.get('expires_at')
    if expires:
        extra_lines += f"⏳ Действует до: {expires}\n"

    text = (
        f"✅ <b>Промокод применён!</b>\n\n"
        f"🎁 <b>Промокод:</b> {promocode}\n"
        f"💰 <b>Скидка:</b> {discount_str}\n"
        f"{extra_lines}\n"
        f"💸 <b>Было:</b> {original_amount:.0f}₽\n"
        f"💳 <b>К оплате:</b> {final_amount:.0f}₽\n\n"
        f"Продолжить оформление?"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Продолжить к оплате", callback_data=f"proceed_payment_{plan_key}")],
        [InlineKeyboardButton(text="🎁 Ввести другой промокод", callback_data=f"enter_promocode_{plan_key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await fsm_edit(state, message, text, reply_markup=keyboard)

async def proceed_to_payment(callback: CallbackQuery, state: FSMContext):
    """Переход к оплате с учетом промокода"""
    db = _get_db()
    plan_key = callback.data[16:]  # Убираем "proceed_payment_" (16 символов)
    plans = get_current_subscription_plans()
    
    if plan_key not in plans:
        await callback.answer("Ошибка: план не найден")
        return
    
    await callback.answer()
    
    plan_info = plans[plan_key]
    data = await state.get_data()
    
    # Получаем финальную сумму (с учетом промокода если был применен)
    final_amount = data.get('final_amount', plan_info['price'])
    promocode_applied = data.get('promocode_applied')
    discount_amount = data.get('discount_amount', 0)
    
    # Общий заголовок
    text = f"💳 <b>Оплата подписки: {plan_info['name']}</b>\n\n"
    
    if data.get('is_scheduled') and data.get('schedule_date'):
        text += f"⏰ <b>Тип активации:</b> Отложенная\n"
        text += f"📅 <b>Начало действия:</b> {data.get('schedule_date')}\n\n"
    
    if promocode_applied:
        text += f"🎁 <b>Промокод:</b> {promocode_applied['code']}\n"
        text += f"💰 <b>Скидка:</b> {promocode_applied['discount_percent']}% (-{discount_amount:.0f}₽)\n"
        text += f"💸 <b>Было:</b> {plan_info['price']:.0f}₽\n"
    
    text += f"💳 <b>К оплате:</b> {final_amount:.0f}₽\n"
    text += f"📅 <b>Срок действия:</b> {plan_info['duration']} дней\n\n"

    # ---------------------------------------------------------------
    # Маршрутизация по провайдеру
    # ---------------------------------------------------------------
    from payment_provider import get_active_provider, create_yookassa_payment

    provider = get_active_provider(db)

    if provider == 'yookassa':
        # --- ЮKassa: создаём платёж и даём ссылку ---
        cfg = await db.get_yookassa_config()
        return_url = cfg.get('return_url') or "https://t.me/"

        user_id = await db.get_user_id(callback.from_user.id)
        promocode_data = promocode_applied or {}
        promo_id = promocode_data.get('id')

        payment_result = create_yookassa_payment(
            amount=final_amount,
            description=f"Подписка {plan_info['name']} ({plan_info['duration']} дней)",
            metadata={
                "user_id": str(callback.from_user.id),
                "plan_type": plan_info['name'],
                "plan_key": plan_key,
            },
            return_url=return_url,
            shop_id=cfg.get('shop_id', ''),
            secret_key=cfg.get('secret_key', ''),
        )

        if payment_result is None:
            # Ошибка создания платежа — информируем, не ломаем бота
            text += (
                "❌ <b>Не удалось создать платёж через ЮKassa.</b>\n"
                "Обратитесь к администратору или попробуйте позже."
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")]
            ])
            await state.update_data(
                plan_type=plan_info['name'],
                amount=final_amount,
                promocode_data=promocode_applied,
                anchor_msg_id=callback.message.message_id,
            )
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            return

        yk_payment_id = payment_result['payment_id']
        confirmation_url = payment_result['confirmation_url']

        # Сохраняем запись в БД
        if user_id:
            await db.create_yookassa_payment_record(
                yookassa_payment_id=yk_payment_id,
                user_id=user_id,
                plan_type=plan_info['name'],
                amount=final_amount,
                promocode_id=promo_id,
                is_scheduled=bool(data.get('is_scheduled')),
                schedule_date=data.get('schedule_date'),
            )

        text += (
            "🏦 <b>Оплата через ЮKassa</b>\n\n"
            "Нажмите кнопку ниже для перехода на страницу оплаты.\n"
            "После оплаты вернитесь в бот и нажмите «✅ Я оплатил — проверить»."
        )

        await state.update_data(
            plan_type=plan_info['name'],
            amount=final_amount,
            promocode_data=promocode_applied,
            anchor_msg_id=callback.message.message_id,
            yk_payment_id=yk_payment_id,
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=confirmation_url)],
            [InlineKeyboardButton(text="✅ Я оплатил — проверить", callback_data=f"yk_check_{yk_payment_id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")],
        ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    # --- СБП: классический поток (скриншот) ---
    payment_settings = await db.get_payment_settings()

    text += "📋 <b>Реквизиты для оплаты:</b>\n"
    text += f"💳 Карта: {payment_settings.get('card_number', 'Не указана')}\n"
    text += f"👤 Получатель: {payment_settings.get('recipient_name', 'Не указан')}\n"
    text += f"🏦 Банк: {payment_settings.get('bank_name', 'Не указан')}\n\n"
    text += payment_settings.get('payment_instruction',
                                 "📝 Переведите указанную сумму и отправьте скриншот перевода.")

    await state.update_data(
        plan_type=plan_info['name'], 
        amount=final_amount,
        promocode_data=promocode_applied,
        anchor_msg_id=callback.message.message_id,
    )
    await state.set_state(SubscriptionStates.waiting_payment_proof)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


async def check_yookassa_payment(callback: CallbackQuery, state: FSMContext):
    """
    Пользователь нажал «✅ Я оплатил — проверить».
    Запрашиваем статус платежа в ЮKassa и обрабатываем результат.
    """
    await callback.answer()
    db = _get_db()

    # Формат callback: yk_check_<payment_id>
    yk_payment_id = callback.data[9:]  # убираем "yk_check_"

    cfg = await db.get_yookassa_config()
    from payment_provider import check_yookassa_payment_status

    status = check_yookassa_payment_status(
        payment_id=yk_payment_id,
        shop_id=cfg.get('shop_id', ''),
        secret_key=cfg.get('secret_key', ''),
    )

    if status == 'succeeded':
        # --- Автоактивация подписки ---
        row = await db.get_yookassa_payment_by_payment_id(yk_payment_id)
        if row is None:
            await callback.message.edit_text(
                "❌ Запись о платеже не найдена. Обратитесь к администратору.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
                ]),
                parse_mode="HTML",
            )
            return

        # row: (id, yookassa_payment_id, user_id, plan_type, amount, status,
        #        promocode_id, is_scheduled, schedule_date, created_at, updated_at)

        # Идемпотентность: если уже подтверждён — не дублировать активацию
        if row[5] == 'succeeded':
            from keyboards import main_menu as _main_menu
            _u = await db.get_user(callback.from_user.id)
            _kb = _main_menu(callback.message.chat.id, _u[8] if _u else None)
            await callback.message.edit_text(
                "✅ <b>Подписка уже активирована!</b>\n\n"
                "Этот платёж был подтверждён ранее.\n\n"
                "🏠 Возврат в главное меню:",
                reply_markup=_kb,
                parse_mode="HTML",
            )
            await clear_state_keep_org(state)
            return

        user_id = row[2]
        plan_type = row[3]
        amount = row[4]
        promo_id = row[6]

        # Обновляем статус в нашей таблице
        await db.update_yookassa_payment_status(yk_payment_id, 'succeeded')

        # Активируем подписку: create_payment_request → confirm_payment_request
        # admin_id=0 означает «авто-подтверждено системой»
        try:
            req_id = await db.create_payment_request(
                user_id, plan_type, amount,
                f"yookassa:{yk_payment_id}",
                promo_id,
            )
            if req_id:
                await db.confirm_payment_request(req_id, admin_id=0)
                try:
                    from subscription_utils import invalidate_plan_cache
                    invalidate_plan_cache(callback.from_user.id)
                except Exception:
                    pass
                # Обновляем тариф организации в main.db (как ручное подтверждение СБП)
                try:
                    import sqlite3 as _sql3
                    from datetime import datetime as _dt, timedelta as _td
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
                    _main_conn = _sql3.connect('data/main.db')
                    _main_cur = _main_conn.cursor()
                    _main_cur.execute(
                        "SELECT o.id FROM organizations o "
                        "JOIN user_org_mapping m ON o.id = m.org_id "
                        "WHERE m.telegram_id = ? AND m.role IN ('owner', 'admin')",
                        (callback.from_user.id,)
                    )
                    _org_row = _main_cur.fetchone()
                    if _org_row:
                        _main_cur.execute(
                            "UPDATE organizations SET subscription_plan = ?, "
                            "subscription_end = ? WHERE id = ?",
                            (plan_type, _org_expires, _org_row[0])
                        )
                        _main_conn.commit()
                    _main_conn.close()
                except Exception:
                    pass
        except Exception as e:
            import logging
            logging.error(f"check_yookassa_payment: auto-confirm error: {e}")

        # Уведомляем супер-администратора
        # users table: (0)id, (1)telegram_id, (2)first_name, (3)last_name, ...
        try:
            from utils import he as _he
            super_admin_id = env_manager.get_main_admin_id()
            if super_admin_id:
                user = await db.get_user_by_id(user_id)
                user_name = _he(f"{user[2]} {user[3]}") if user else "—"
                await callback.bot.send_message(
                    chat_id=super_admin_id,
                    text=(
                        "✅ <b>Автоплатёж ЮKassa подтверждён</b>\n\n"
                        f"👤 <b>Пользователь:</b> {user_name}\n"
                        f"📋 <b>Тариф:</b> {plan_type}\n"
                        f"💰 <b>Сумма:</b> {amount:.0f}₽\n"
                        f"🆔 <b>Payment ID:</b> {yk_payment_id}"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

        from keyboards import main_menu
        user = await db.get_user(callback.from_user.id)
        kb = main_menu(callback.message.chat.id, user[8] if user else None)
        await callback.message.edit_text(
            "🎉 <b>Оплата подтверждена!</b>\n\n"
            f"✅ Подписка <b>{plan_type}</b> активирована.\n\n"
            "🏠 Возврат в главное меню:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await clear_state_keep_org(state)

    elif status in ('pending', 'waiting_for_capture'):
        await callback.message.edit_text(
            "⏳ <b>Оплата ещё обрабатывается</b>\n\n"
            "Платёж принят, но ещё не завершён на стороне банка.\n"
            "Подождите 1–2 минуты и нажмите «Проверить» ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"yk_check_{yk_payment_id}")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")],
            ]),
            parse_mode="HTML",
        )

    elif status == 'canceled':
        await db.update_yookassa_payment_status(yk_payment_id, 'canceled')
        await callback.message.edit_text(
            "❌ <b>Платёж отменён</b>\n\n"
            "Оплата не прошла или была отменена.\n"
            "Вы можете попробовать оформить подписку заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="subscription_plans")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")],
            ]),
            parse_mode="HTML",
        )

    else:
        # 'error' или неизвестный статус
        await callback.message.edit_text(
            "⚠️ <b>Не удалось проверить статус платежа</b>\n\n"
            "Возможно, ЮKassa временно недоступна. Попробуйте через минуту.\n"
            "Если проблема сохраняется — обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"yk_check_{yk_payment_id}")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_plans")],
            ]),
            parse_mode="HTML",
        )