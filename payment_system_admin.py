"""
Административная панель управления платежной системой и подписками
"""
import asyncio
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import Database
from keyboards import back_button, invalidate_web_url_cache
from states import PaymentSystemStates
from env_manager import env_manager
from db_utils import clear_state_keep_org
from utils import he
from message_utils import fsm_edit

# Создаем роутер
payment_system_router = Router()


def _get_db():
    """Локальная БД для платёжного/подписочного функционала"""
    return Database('data/shop_bot.db')

_CANCEL_PLAN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_plan")]
])
_CANCEL_PROMO_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_promo")]
])

@payment_system_router.callback_query(F.data == "payment_system_admin")
async def payment_system_admin_menu(callback: CallbackQuery):
    """Главное меню управления платежной системой"""
    db = _get_db()
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return
        
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    # Получаем текущие настройки
    payment_settings = db.get_payment_settings()
    
    text = "💳 <b>Управление платежной системой</b>\n\n"
    text += "🔧 <b>Текущие настройки:</b>\n"
    text += f"💳 Номер карты: {payment_settings.get('card_number', 'Не установлен')}\n"
    text += f"👤 Получатель: {payment_settings.get('recipient_name', 'Не установлен')}\n"
    text += f"🏦 Банк: {payment_settings.get('bank_name', 'Не установлен')}\n\n"
    
    # Статистика подписок
    subscriptions_stats = db.get_subscriptions_statistics()
    text += "📊 <b>Статистика подписок:</b>\n"
    text += f"👥 Всего подписчиков: {subscriptions_stats['total_subscribers']}\n"
    text += f"💰 Месячная выручка: {subscriptions_stats['monthly_revenue']:,.0f} ₽\n"
    text += f"📈 Конверсия: {subscriptions_stats['conversion_rate']:.1f}%\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Заявки на оплату", callback_data="pending_payments")],
        [InlineKeyboardButton(text="💳 Настройки оплаты", callback_data="payment_settings")],
        [InlineKeyboardButton(text="💎 Управление тарифами", callback_data="manage_plans")],
        [InlineKeyboardButton(text="📊 Статистика платежей", callback_data="payment_statistics")],
        [InlineKeyboardButton(text="👥 Управление подписками", callback_data="manage_subscriptions")],
        [InlineKeyboardButton(text="🎁 Промокоды", callback_data="manage_promocodes")],
        [InlineKeyboardButton(text="🎫 Пробный период", callback_data="trial_settings")],
        [back_button("system_admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "payment_settings")
async def payment_settings_menu(callback: CallbackQuery):
    """Настройки платежной системы"""
    db = _get_db()
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return
        
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    payment_settings = db.get_payment_settings()
    provider = db.get_payment_provider()
    from payment_provider import provider_label
    provider_display = provider_label(provider)

    text = "💳 <b>Настройки платежной системы</b>\n\n"
    text += f"🔀 <b>Активный провайдер:</b> {provider_display}\n\n"
    text += "Настройте реквизиты для получения платежей:\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔀 Провайдер оплаты: {provider_display}", callback_data="payment_provider_select")],
        [InlineKeyboardButton(text=f"💳 Номер карты: {payment_settings.get('card_number', 'Не установлен')}", callback_data="set_card_number")],
        [InlineKeyboardButton(text=f"👤 Получатель: {payment_settings.get('recipient_name', 'Не установлен')}", callback_data="set_recipient_name")],
        [InlineKeyboardButton(text=f"🏦 Банк: {payment_settings.get('bank_name', 'Не установлен')}", callback_data="set_bank_name")],
        [InlineKeyboardButton(text="💬 Инструкция для пользователей", callback_data="set_payment_instruction")],
        [InlineKeyboardButton(text="🧪 Тестовый платеж", callback_data="test_payment")],
        [back_button("payment_system_admin")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "manage_plans")
async def manage_plans_menu(callback: CallbackQuery):
    """Управление тарифными планами"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    plans = db.get_subscription_plans()
    
    text = "💎 <b>Управление тарифными планами</b>\n\n"
    text += "Текущие тарифы:\n\n"
    
    for plan in plans:
        # Структура: (id, name, duration_days, price, description, is_active, created_at)
        plan_id = plan[0]
        name = plan[1]
        duration_days = plan[2]
        price = plan[3]
        description = plan[4]
        is_active = plan[5]
        status = "✅" if is_active else "❌"
        text += f"{status} <b>{he(name)}</b> - {price:,.0f} ₽ ({duration_days} дней)\n"
        text += f"   {description}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить тариф", callback_data="add_plan")],
        [InlineKeyboardButton(text="✏️ Редактировать тариф", callback_data="edit_plan")],
        [InlineKeyboardButton(text="🔄 Переключить статус", callback_data="toggle_plan")],
        [InlineKeyboardButton(text="🗑 Удалить тариф", callback_data="delete_plan")],
        [InlineKeyboardButton(text="🎯 Установить скидки", callback_data="set_discounts")],
        [back_button("payment_system_admin")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "payment_statistics")
async def payment_statistics_menu(callback: CallbackQuery):
    """Статистика платежей"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    stats = db.get_detailed_payment_statistics()
    
    text = "📊 <b>Детальная статистика платежей</b>\n\n"
    
    # Общая статистика
    text += "📈 <b>Общие показатели:</b>\n"
    text += f"💰 Общая выручка: {stats['total_revenue']:,.0f} ₽\n"
    text += f"📦 Всего платежей: {stats['total_payments']}\n"
    text += f"💳 Средний чек: {stats['average_payment']:,.0f} ₽\n"
    text += f"📅 За последний месяц: {stats['monthly_revenue']:,.0f} ₽\n\n"
    
    # По тарифам
    text += "💎 <b>По тарифным планам:</b>\n"
    for plan_name, plan_stats in stats['by_plans'].items():
        text += f"• {plan_name}: {plan_stats['count']} подписок ({plan_stats['revenue']:,.0f} ₽)\n"
    
    text += f"\n📊 <b>Конверсия:</b> {stats['conversion_rate']:.1f}%\n"
    text += f"🔄 <b>Продления:</b> {stats['renewal_rate']:.1f}%\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 За период", callback_data="stats_by_period")],
        [InlineKeyboardButton(text="📋 Экспорт отчета", callback_data="export_payment_report")],
        [InlineKeyboardButton(text="📈 Графики", callback_data="payment_charts")],
        [back_button("payment_system_admin")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "manage_subscriptions")
async def manage_subscriptions_menu(callback: CallbackQuery):
    """Управление подписками пользователей"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    active_subscriptions = db.get_all_active_subscriptions()

    text = "👥 <b>Управление подписками пользователей</b>\n\n"
    text += f"Активных подписок: {len(active_subscriptions)}\n\n"

    if active_subscriptions:
        text += "📋 <b>Последние подписки:</b>\n"
        for sub in active_subscriptions[:5]:
            # Безопасное извлечение данных
            plan_type = sub[2] if len(sub) > 2 else "Неизвестно"
            end_date = sub[4] if len(sub) > 4 else "Неизвестно"
            first_name = sub[5] if len(sub) > 5 else "Неизвестно"
            last_name = sub[6] if len(sub) > 6 else ""

            user_name = he(f"{first_name} {last_name}".strip())
            text += f"• {user_name} - {plan_type} до {end_date[:10] if len(end_date) >= 10 else end_date}\n"

        if len(active_subscriptions) > 5:
            text += f"... и еще {len(active_subscriptions) - 5} подписок\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти пользователя", callback_data="find_user_subscription")],
        [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="grant_subscription")],
        [InlineKeyboardButton(text="⏰ Продлить подписку", callback_data="extend_subscription")],
        [InlineKeyboardButton(text="❌ Отменить подписку", callback_data="cancel_subscription")],
        [InlineKeyboardButton(text="📊 Детали подписок", callback_data="subscription_details")],
        [back_button("payment_system_admin")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "manage_promocodes")
async def manage_promocodes_menu(callback: CallbackQuery):
    """Управление промокодами"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    promocodes = db.get_all_promocodes()
    
    text = "🎁 <b>Управление промокодами</b>\n\n"

    if promocodes:
        text += "📋 <b>Промокоды:</b>\n"
        for promo in promocodes:
            promo_id, code, discount, disc_type, usage_count, max_usage, is_active, expires_at, allowed_plans, last_used, created_at = promo
            status = "✅" if is_active else "❌"
            disc_label = f"{discount}₽" if disc_type == 'fixed' else f"{discount}%"
            exp_label = f" до {expires_at}" if expires_at else ""
            text += f"{status} <code>{code}</code> — {disc_label} скидка ({usage_count}/{max_usage}){exp_label}\n"
    else:
        text += "Промокоды не созданы\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="create_promocode")],
        [InlineKeyboardButton(text="📦 Пакетное создание", callback_data="batch_promocode_start")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit_promocode")],
        [InlineKeyboardButton(text="📊 Статистика использования", callback_data="promocode_stats")],
        [InlineKeyboardButton(text="🗑 Удалить промокод", callback_data="delete_promocode")],
        [back_button("payment_system_admin")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

# Обработчики для настройки реквизитов
@payment_system_router.callback_query(F.data == "set_card_number")
async def set_card_number_start(callback: CallbackQuery, state: FSMContext):
    """Начало установки номера карты"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    text = "💳 <b>Установка номера карты</b>\n\n"
    text += "Введите номер карты для получения платежей:\n"
    text += "Формат: 1234 5678 9012 3456\n\n"
    text += "⚠️ Убедитесь, что номер указан корректно!"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("payment_settings")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_card_number)

@payment_system_router.message(PaymentSystemStates.waiting_card_number)
async def process_card_number(message: Message, state: FSMContext):
    """Обработка номера карты"""
    db = _get_db()
    card_number = message.text.strip().replace(" ", "")
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки оплаты", callback_data="payment_settings")]])

    if not card_number.isdigit() or len(card_number) != 16:
        await fsm_edit(state, message, "❌ Неверный формат номера карты. Введите 16 цифр:", reply_markup=_back_kb)
        return

    formatted_number = f"{card_number[:4]} {card_number[4:8]} {card_number[8:12]} {card_number[12:]}"

    try:
        db.update_payment_setting('card_number', formatted_number)
        await fsm_edit(state, message, f"✅ Номер карты обновлён: {formatted_number}", reply_markup=_back_kb)
    except Exception as e:
        logging.error(f"process_card_number: DB error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при сохранении номера карты. Попробуйте позже.", reply_markup=_back_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "set_recipient_name")
async def set_recipient_name_start(callback: CallbackQuery, state: FSMContext):
    """Начало установки имени получателя"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    text = "👤 <b>Установка имени получателя</b>\n\n"
    text += "Введите ФИО получателя платежей:\n"
    text += "Пример: Иванов Иван Иванович"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("payment_settings")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_recipient_name)

@payment_system_router.message(PaymentSystemStates.waiting_recipient_name)
async def process_recipient_name(message: Message, state: FSMContext):
    """Обработка имени получателя"""
    db = _get_db()
    recipient_name = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки оплаты", callback_data="payment_settings")]])

    if len(recipient_name) < 5:
        await fsm_edit(state, message, "❌ Имя получателя слишком короткое. Введите заново:", reply_markup=_back_kb)
        return

    try:
        db.update_payment_setting('recipient_name', recipient_name)
        await fsm_edit(state, message, f"✅ Имя получателя обновлено: {he(recipient_name)}", reply_markup=_back_kb)
    except Exception as e:
        logging.error(f"process_recipient_name: DB error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при сохранении имени получателя. Попробуйте позже.", reply_markup=_back_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "set_bank_name")
async def set_bank_name_start(callback: CallbackQuery, state: FSMContext):
    """Начало установки названия банка"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    text = "🏦 <b>Установка названия банка</b>\n\n"
    text += "Введите название банка:\n"
    text += "Пример: Сбербанк, ВТБ, Альфа-Банк"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("payment_settings")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_bank_name)

@payment_system_router.message(PaymentSystemStates.waiting_bank_name)
async def process_bank_name(message: Message, state: FSMContext):
    """Обработка названия банка"""
    db = _get_db()
    bank_name = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки оплаты", callback_data="payment_settings")]])

    if len(bank_name) < 3:
        await fsm_edit(state, message, "❌ Название банка слишком короткое. Введите заново:", reply_markup=_back_kb)
        return

    try:
        db.update_payment_setting('bank_name', bank_name)
        await fsm_edit(state, message, f"✅ Название банка обновлено: {he(bank_name)}", reply_markup=_back_kb)
    except Exception as e:
        logging.error(f"process_bank_name: DB error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при сохранении названия банка. Попробуйте позже.", reply_markup=_back_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "set_web_interface_url")
async def set_web_interface_url_start(callback: CallbackQuery, state: FSMContext):
    """Показать текущий URL и предложить изменить/удалить"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    current_url = db.get_web_interface_url()

    text = "🌐 <b>URL веб-интерфейса</b>\n\n"
    if current_url:
        text += f"Текущий URL:\n<code>{he(current_url)}</code>\n\n"
        text += "Кнопка «Веб-интерфейс» показывается всем пользователям в главном меню.\n\n"
        text += "Нажмите <b>Изменить</b> чтобы ввести новый URL, или <b>Удалить</b> чтобы скрыть кнопку."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_web_interface_url"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data="clear_web_interface_url"),
            ],
            [back_button("system_admin_panel")],
        ])
    else:
        text += "URL не настроен. Кнопка «Веб-интерфейс» скрыта для всех пользователей.\n\n"
        text += "Введите URL вашего веб-интерфейса (должен начинаться с <code>https://</code>):"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [back_button("system_admin_panel")],
        ])
        await state.update_data(anchor_msg_id=callback.message.message_id)
        await state.set_state(PaymentSystemStates.waiting_web_interface_url)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@payment_system_router.callback_query(F.data == "edit_web_interface_url")
async def edit_web_interface_url_start(callback: CallbackQuery, state: FSMContext):
    """Запросить новый URL"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    text = (
        "🌐 <b>Изменение URL веб-интерфейса</b>\n\n"
        "Введите новый URL (должен начинаться с <code>https://</code>):\n\n"
        "Например: <code>https://yourapp.replit.app</code>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("system_admin_panel")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_web_interface_url)


@payment_system_router.message(PaymentSystemStates.waiting_web_interface_url)
async def process_web_interface_url(message: Message, state: FSMContext):
    """Сохранить новый URL веб-интерфейса"""
    db = _get_db()
    url = message.text.strip() if message.text else ""
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔧 Системная панель", callback_data="system_admin_panel")]])

    if not url.startswith("https://") and not url.startswith("http://"):
        await fsm_edit(state, message,
            "❌ Неверный формат. URL должен начинаться с <code>https://</code>\n\nПопробуйте ещё раз:",
            reply_markup=_back_kb)
        return

    try:
        db.set_web_interface_url(url)
        invalidate_web_url_cache()
        await fsm_edit(state, message,
            f"✅ URL веб-интерфейса обновлён:\n<code>{he(url)}</code>\n\n"
            "Кнопка «🌐 Веб-интерфейс» появится в главном меню всех пользователей.",
            reply_markup=_back_kb)
    except Exception as e:
        logging.error(f"process_web_interface_url: DB error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при сохранении URL. Попробуйте позже.", reply_markup=_back_kb)
    await clear_state_keep_org(state)


@payment_system_router.callback_query(F.data == "clear_web_interface_url")
async def clear_web_interface_url_handler(callback: CallbackQuery):
    """Удалить URL — скрыть кнопку для всех пользователей"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    try:
        db.set_web_interface_url(None)
        invalidate_web_url_cache()
        text = (
            "🌐 <b>URL веб-интерфейса удалён</b>\n\n"
            "Кнопка «Веб-интерфейс» скрыта для всех пользователей."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔧 Системная панель", callback_data="system_admin_panel")],
        ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logging.error(f"clear_web_interface_url: DB error: {e}")
        await callback.answer("❌ Ошибка при удалении URL", show_alert=True)


# Дополнительные обработчики для управления тарифами
@payment_system_router.callback_query(F.data == "add_plan")
async def add_plan_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления нового тарифного плана"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    text = "💎 <b>Добавление нового тарифного плана</b>\n\n"
    text += "Введите название тарифа:\n"
    text += "Пример: VIP план, Премиум, Корпоративный"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("manage_plans")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_plan_name)

@payment_system_router.message(PaymentSystemStates.waiting_plan_name)
async def process_plan_name(message: Message, state: FSMContext):
    """Обработка названия плана"""
    plan_name = message.text.strip()
    if len(plan_name) < 3:
        await fsm_edit(state, message, "❌ Название тарифа слишком короткое", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(plan_name=plan_name)
    text = f"💎 <b>Новый тариф: {plan_name}</b>\n\nВведите цену тарифа в рублях:\nПример: 990, 2700, 4900"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_plan_price)

@payment_system_router.message(PaymentSystemStates.waiting_plan_price)
async def process_plan_price(message: Message, state: FSMContext):
    """Обработка цены плана"""
    try:
        price = float(message.text.strip())
        if price <= 0:
            raise ValueError("Цена должна быть положительной")
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат цены. Введите число больше 0", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(plan_price=price)
    text = f"💎 <b>Цена: {price:,.0f} ₽</b>\n\nВведите длительность тарифа в днях:\nПример: 30 (месяц), 90 (3 месяца), 365 (год)"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_plan_duration)

@payment_system_router.message(PaymentSystemStates.waiting_plan_duration)
async def process_plan_duration(message: Message, state: FSMContext):
    """Обработка длительности плана"""
    try:
        duration = int(message.text.strip())
        if duration <= 0:
            raise ValueError("Длительность должна быть положительной")
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат. Введите количество дней (число больше 0)", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(plan_duration=duration)
    text = f"💎 <b>Длительность: {duration} дней</b>\n\nВведите описание тарифа:\nПример: Безлимитные продажи и отчеты"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_plan_description)

@payment_system_router.message(PaymentSystemStates.waiting_plan_description)
async def process_plan_description(message: Message, state: FSMContext):
    """Обработка описания плана"""
    description = message.text.strip()
    if len(description) < 10:
        await fsm_edit(state, message, "❌ Описание слишком короткое (минимум 10 символов)", reply_markup=_CANCEL_PLAN_KB)
        return
    data = await state.get_data()
    plan_name = data['plan_name']
    plan_price = data['plan_price']
    plan_duration = data['plan_duration']
    text = (
        f"💎 <b>Настройка лимитов для плана: {plan_name}</b>\n\n"
        f"💰 Цена: {plan_price:,.0f} ₽\n"
        f"⏱ Длительность: {plan_duration} дней\n"
        f"📝 Описание: {description}\n\n"
        "📦 Введите максимальное количество товаров:\n(-1 для безлимита)"
    )
    await state.update_data(plan_description=description)
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_max_products)

@payment_system_router.message(PaymentSystemStates.waiting_max_products)
async def process_max_products(message: Message, state: FSMContext):
    """Обработка максимального количества товаров"""
    try:
        max_products = int(message.text.strip())
        if max_products < -1 or max_products == 0:
            raise ValueError()
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат. Введите -1 для безлимита или число больше 0", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(max_products=max_products)
    text = f"📦 Товары: {'Безлимит' if max_products == -1 else max_products}\n\n🏪 Введите максимальное количество магазинов:\n(-1 для безлимита)"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_max_shops)

@payment_system_router.message(PaymentSystemStates.waiting_max_shops)
async def process_max_shops(message: Message, state: FSMContext):
    """Обработка максимального количества магазинов"""
    try:
        max_shops = int(message.text.strip())
        if max_shops < -1 or max_shops == 0:
            raise ValueError()
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат. Введите -1 для безлимита или число больше 0", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(max_shops=max_shops)
    text = f"🏪 Магазины: {'Безлимит' if max_shops == -1 else max_shops}\n\n💰 Введите максимальное количество продаж в месяц:\n(-1 для безлимита)"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_max_sales)

@payment_system_router.message(PaymentSystemStates.waiting_max_sales)
async def process_max_sales(message: Message, state: FSMContext):
    """Обработка максимального количества продаж"""
    try:
        max_sales = int(message.text.strip())
        if max_sales < -1 or max_sales == 0:
            raise ValueError()
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат. Введите -1 для безлимита или число больше 0", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(max_sales=max_sales)
    text = f"💰 Продажи/месяц: {'Безлимит' if max_sales == -1 else max_sales}\n\n📋 Разрешить экспорт отчетов?\nВведите: да/нет"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_export_reports)

@payment_system_router.message(PaymentSystemStates.waiting_export_reports)
async def process_export_reports(message: Message, state: FSMContext):
    """Обработка настройки экспорта отчетов"""
    answer = message.text.strip().lower()
    if answer in ['да', 'yes', '1', 'true']:
        can_export = True
    elif answer in ['нет', 'no', '0', 'false']:
        can_export = False
    else:
        await fsm_edit(state, message, "❌ Введите 'да' или 'нет'", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(can_export_reports=can_export)
    text = f"📋 Экспорт отчетов: {'Да' if can_export else 'Нет'}\n\n📈 Разрешить расширенную аналитику?\nВведите: да/нет"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_analytics)

@payment_system_router.message(PaymentSystemStates.waiting_analytics)
async def process_analytics(message: Message, state: FSMContext):
    """Обработка настройки аналитики"""
    answer = message.text.strip().lower()
    if answer in ['да', 'yes', '1', 'true']:
        can_analytics = True
    elif answer in ['нет', 'no', '0', 'false']:
        can_analytics = False
    else:
        await fsm_edit(state, message, "❌ Введите 'да' или 'нет'", reply_markup=_CANCEL_PLAN_KB)
        return
    await state.update_data(can_view_analytics=can_analytics)
    text = f"📈 Аналитика: {'Да' if can_analytics else 'Нет'}\n\n🔔 Разрешить уведомления?\nВведите: да/нет"
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PLAN_KB)
    await state.set_state(PaymentSystemStates.waiting_notifications)

@payment_system_router.message(PaymentSystemStates.waiting_notifications)
async def process_notifications(message: Message, state: FSMContext):
    """Обработка настройки уведомлений и создание плана"""
    db = _get_db()
    answer = message.text.strip().lower()
    if answer in ['да', 'yes', '1', 'true']:
        can_notifications = True
    elif answer in ['нет', 'no', '0', 'false']:
        can_notifications = False
    else:
        await fsm_edit(state, message, "❌ Введите 'да' или 'нет'", reply_markup=_CANCEL_PLAN_KB)
        return
    data = await state.get_data()
    _plans_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Управление тарифами", callback_data="manage_plans")]])
    try:
        db.create_subscription_plan_with_limits(
            name=data['plan_name'],
            duration_days=data['plan_duration'],
            price=data['plan_price'],
            description=data['plan_description'],
            max_products=data['max_products'],
            max_shops=data['max_shops'],
            max_sales_per_month=data['max_sales'],
            can_export_reports=data['can_export_reports'],
            can_view_analytics=data['can_view_analytics'],
            can_use_notifications=can_notifications
        )
    except Exception as e:
        logging.error(f"process_notifications: create_subscription_plan_with_limits error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при создании тарифного плана. Попробуйте позже.", reply_markup=_plans_kb)
        await clear_state_keep_org(state)
        return

    text = (
        f"✅ <b>Тарифный план создан!</b>\n\n"
        f"📋 Название: {data['plan_name']}\n"
        f"💰 Цена: {data['plan_price']:,.0f} ₽\n"
        f"⏱ Длительность: {data['plan_duration']} дней\n"
        f"📝 Описание: {data['plan_description']}\n\n"
        f"<b>🎯 Лимиты и возможности:</b>\n"
        f"📦 Товары: {'∞ Безлимит' if data['max_products'] == -1 else data['max_products']}\n"
        f"🏪 Магазины: {'∞ Безлимит' if data['max_shops'] == -1 else data['max_shops']}\n"
        f"💰 Продажи/месяц: {'∞ Безлимит' if data['max_sales'] == -1 else data['max_sales']}\n"
        f"📋 Экспорт отчетов: {'✅ Да' if data['can_export_reports'] else '❌ Нет'}\n"
        f"📈 Аналитика: {'✅ Да' if data['can_view_analytics'] else '❌ Нет'}\n"
        f"🔔 Уведомления: {'✅ Да' if can_notifications else '❌ Нет'}"
    )
    await fsm_edit(state, message, text, reply_markup=_plans_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "cancel_add_plan")
async def cancel_add_plan(callback: CallbackQuery, state: FSMContext):
    """Отмена создания тарифного плана"""
    await clear_state_keep_org(state)
    await callback.answer()
    await manage_plans_menu(callback)

# Обработчики редактирования планов
@payment_system_router.callback_query(F.data == "edit_plan")
async def edit_plan_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования тарифного плана"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    plans = db.get_subscription_plans()
    
    if not plans:
        text = "❌ Нет тарифных планов для редактирования"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [back_button("manage_plans")]
        ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return
    
    text = "✏️ <b>Выберите план для редактирования:</b>\n\n"
    keyboard_buttons = []
    
    for plan in plans:
        plan_id = plan[0]
        name = plan[1]
        duration = plan[2]
        price = plan[3]
        description = plan[4]
        is_active = plan[5]
        status = "✅" if is_active else "❌"
        text += f"{status} <b>{he(name)}</b> - {price:,.0f}₽ ({duration} дн.)\n"
        
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"✏️ {name}", callback_data=f"edit_plan_{plan_id}")
        ])
    
    keyboard_buttons.append([back_button("manage_plans")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("edit_plan_"))
async def edit_plan_details(callback: CallbackQuery, state: FSMContext):
    """Детали редактирования конкретного плана"""
    db = _get_db()
    plan_id = int(callback.data.split("_")[2])
    plan_details = db.get_subscription_plan_details(plan_id)
    
    if not plan_details:
        await callback.answer("❌ План не найден")
        return

    await callback.answer()
    await state.update_data(editing_plan_id=plan_id)
    
    text = f"✏️ <b>Редактирование плана: {plan_details['name']}</b>\n\n"
    text += f"💰 Цена: {plan_details['price']:,.0f} ₽\n"
    text += f"⏱ Длительность: {plan_details['duration_days']} дней\n"
    text += f"📝 Описание: {plan_details['description']}\n\n"
    text += f"<b>🎯 Текущие лимиты:</b>\n"
    text += f"📦 Товары: {'∞ Безлимит' if plan_details['max_products'] == -1 else plan_details['max_products']}\n"
    text += f"🏪 Магазины: {'∞ Безлимит' if plan_details['max_shops'] == -1 else plan_details['max_shops']}\n"
    text += f"💰 Продажи/месяц: {'∞ Безлимит' if plan_details['max_sales_per_month'] == -1 else plan_details['max_sales_per_month']}\n"
    text += f"📋 Экспорт: {'✅ Да' if plan_details['can_export_reports'] else '❌ Нет'}\n"
    text += f"📈 Аналитика: {'✅ Да' if plan_details['can_view_analytics'] else '❌ Нет'}\n"
    text += f"🔔 Уведомления: {'✅ Да' if plan_details['can_use_notifications'] else '❌ Нет'}\n\n"
    text += "Выберите, что хотите изменить:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Цена", callback_data="edit_field_price")],
        [InlineKeyboardButton(text="⏱ Длительность", callback_data="edit_field_duration")],
        [InlineKeyboardButton(text="📝 Описание", callback_data="edit_field_description")],
        [InlineKeyboardButton(text="📦 Лимит товаров", callback_data="edit_field_max_products")],
        [InlineKeyboardButton(text="🏪 Лимит магазинов", callback_data="edit_field_max_shops")],
        [InlineKeyboardButton(text="💰 Лимит продаж", callback_data="edit_field_max_sales")],
        [InlineKeyboardButton(text="📋 Экспорт отчетов", callback_data="edit_field_export")],
        [InlineKeyboardButton(text="📈 Аналитика", callback_data="edit_field_analytics")],
        [InlineKeyboardButton(text="🔔 Уведомления", callback_data="edit_field_notifications")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="edit_plan")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

# Обработчики редактирования полей планов
@payment_system_router.callback_query(F.data.startswith("edit_field_"))
async def edit_plan_field_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования поля плана"""
    db = _get_db()
    field = callback.data.split("_", 2)[2]
    data = await state.get_data()
    plan_id = data.get('editing_plan_id')
    
    if not plan_id:
        await callback.answer("❌ Ошибка: план не выбран")
        return
    
    field_names = {
        'price': '💰 цену',
        'duration': '⏱ длительность',
        'description': '📝 описание',
        'max_products': '📦 лимит товаров',
        'max_shops': '🏪 лимит магазинов',
        'max_sales': '💰 лимит продаж',
        'export': '📋 экспорт отчетов',
        'analytics': '📈 аналитику',
        'notifications': '🔔 уведомления'
    }
    
    await state.update_data(editing_field=field)
    
    if field in ['export', 'analytics', 'notifications']:
        # Переключение булевых значений
        plan_details = db.get_subscription_plan_details(plan_id)
        current_value = plan_details.get(f'can_{field}' if field != 'export' else 'can_export_reports')
        new_value = not current_value
        
        success = db.update_subscription_plan_field(plan_id, f'can_{field}' if field != 'export' else 'can_export_reports', new_value)
        
        if success:
            status = "включено" if new_value else "отключено"
            await callback.answer(f"✅ {field_names[field].capitalize()} {status}")
            await edit_plan_details(callback, state)
        else:
            await callback.answer("❌ Ошибка при обновлении")
    else:
        # Ввод нового значения
        await callback.answer()
        text = f"✏️ <b>Редактирование поля: {field_names[field]}</b>\n\n"
        
        if field == 'price':
            text += "Введите новую цену в рублях:\n"
            text += "Пример: 990, 2700, 4900"
        elif field == 'duration':
            text += "Введите новую длительность в днях:\n"
            text += "Пример: 30, 90, 365"
        elif field == 'description':
            text += "Введите новое описание тарифа:"
        elif field in ['max_products', 'max_shops', 'max_sales']:
            text += f"Введите новый лимит (-1 для безлимита):"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"edit_plan_{plan_id}")]
        ])
        
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        await state.update_data(anchor_msg_id=callback.message.message_id)
        await state.set_state(PaymentSystemStates.editing_plan_field)

@payment_system_router.message(PaymentSystemStates.editing_plan_field)
async def process_plan_field_edit(message: Message, state: FSMContext):
    """Обработка редактирования поля плана"""
    db = _get_db()
    data = await state.get_data()
    plan_id = data.get('editing_plan_id')
    field = data.get('editing_field')

    if not plan_id or not field:
        await fsm_edit(state, message, "❌ Ошибка: данные не найдены")
        return
    
    new_value = message.text.strip()
    
    try:
        # Валидация и преобразование значений
        if field == 'price':
            new_value = float(new_value)
            if new_value <= 0:
                raise ValueError("Цена должна быть больше 0")
            db_field = 'price'
        elif field == 'duration':
            new_value = int(new_value)
            if new_value <= 0:
                raise ValueError("Длительность должна быть больше 0")
            db_field = 'duration_days'
        elif field == 'description':
            if len(new_value) < 10:
                raise ValueError("Описание слишком короткое")
            db_field = 'description'
        elif field in ['max_products', 'max_shops', 'max_sales']:
            new_value = int(new_value)
            if new_value < -1 or new_value == 0:
                raise ValueError("Значение должно быть -1 (безлимит) или больше 0")
            db_field = f'max_{field.split("_")[1]}'
            if field == 'max_sales':
                db_field = 'max_sales_per_month'
        else:
            raise ValueError("Неизвестное поле")
            
        # Обновляем в базе данных
        success = db.update_subscription_plan_field(plan_id, db_field, new_value)
        
        if success:
            text = f"✅ <b>Поле обновлено!</b>\n\n"
            if field == 'price':
                text += f"💰 Новая цена: {new_value:,.0f} ₽"
            elif field == 'duration':
                text += f"⏱ Новая длительность: {new_value} дней"
            elif field == 'description':
                text += f"📝 Новое описание: {new_value}"
            elif field in ['max_products', 'max_shops', 'max_sales']:
                limit_text = "Безлимит" if new_value == -1 else str(new_value)
                text += f"🎯 Новый лимит: {limit_text}"

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Продолжить редактирование", callback_data=f"edit_plan_{plan_id}")],
                [InlineKeyboardButton(text="💎 К управлению тарифами", callback_data="manage_plans")]
            ])
            await fsm_edit(state, message, text, reply_markup=keyboard)
        else:
            await fsm_edit(state, message, "❌ Ошибка при обновлении плана")

    except ValueError as e:
        await fsm_edit(state, message, f"❌ Ошибка валидации: {e}")
    except Exception as e:
        await fsm_edit(state, message, f"❌ Произошла ошибка: {e}")

    await clear_state_keep_org(state)

# ─────────────────── CREATION WIZARD ───────────────────

_SKIP_EXPIRES_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⏩ Без срока (пропустить)", callback_data="promo_skip_expires")],
    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_promo")],
])
_SKIP_PLANS_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⏩ Все тарифы (пропустить)", callback_data="promo_skip_plans")],
    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_promo")],
])

@payment_system_router.callback_query(F.data == "create_promocode")
async def create_promocode_start(callback: CallbackQuery, state: FSMContext):
    """Шаг 1: Ввод кода промокода или генерация случайного."""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    await callback.answer()
    text = (
        "🎁 <b>Создание промокода — шаг 1/5</b>\n\n"
        "Введите код промокода:\n"
        "<i>Пример: SALE20, NEWCLIENT, VIP50</i>\n"
        "Только латиница, цифры и знак подчёркивания, минимум 3 символа.\n\n"
        "Или нажмите «🎲 Случайный» для автоматической генерации."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Случайный код", callback_data="gen_promo_code")],
        [back_button("manage_promocodes")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_promocode)


@payment_system_router.callback_query(F.data == "gen_promo_code")
async def gen_promo_code(callback: CallbackQuery, state: FSMContext):
    """Генерировать случайный код и перейти к шагу 2."""
    import secrets, string
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    await callback.answer()
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    await state.update_data(promocode=code)
    await _ask_discount_type(callback.message, code)
    await state.set_state(PaymentSystemStates.waiting_discount_type)


@payment_system_router.message(PaymentSystemStates.waiting_promocode)
async def process_promocode_code(message: Message, state: FSMContext):
    """Принять введённый код и перейти к шагу 2."""
    code = message.text.strip().upper()
    if not code.replace('_', '').isalnum() or len(code) < 3:
        await fsm_edit(state, message,
            "❌ Код должен содержать только латинские буквы, цифры и подчёркивания (минимум 3 символа)",
            reply_markup=_CANCEL_PROMO_KB)
        return
    await state.update_data(promocode=code)
    await _ask_discount_type(message, code, state=state)
    await state.set_state(PaymentSystemStates.waiting_discount_type)


async def _ask_discount_type(target, code: str, state=None):
    """Шаг 2: выбор типа скидки."""
    text = (
        f"🎁 <b>Создание промокода — шаг 2/5</b>\n\n"
        f"Код: <code>{code}</code>\n\n"
        f"Выберите тип скидки:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Процент (%)", callback_data="promo_disc_type_percent")],
        [InlineKeyboardButton(text="💵 Фиксированная сумма (₽)", callback_data="promo_disc_type_fixed")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_promo")],
    ])
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif state is not None:
        await fsm_edit(state, target, text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")


@payment_system_router.callback_query(F.data.in_({"promo_disc_type_percent", "promo_disc_type_fixed"}))
async def promo_disc_type_chosen(callback: CallbackQuery, state: FSMContext):
    """Шаг 3: ввод размера скидки."""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    await callback.answer()
    disc_type = 'fixed' if 'fixed' in callback.data else 'percent'
    await state.update_data(discount_type=disc_type)
    data = await state.get_data()
    code = data.get('promocode', '')
    if disc_type == 'fixed':
        hint = "Введите сумму скидки в рублях:\n<i>Пример: 100, 250, 500</i>"
    else:
        hint = "Введите размер скидки в процентах (1–90):\n<i>Пример: 10, 20, 50</i>"
    text = (
        f"🎁 <b>Создание промокода — шаг 3/5</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Тип: {'Фиксированная (₽)' if disc_type == 'fixed' else 'Процент (%)'}\n\n"
        f"{hint}"
    )
    await callback.message.edit_text(text, reply_markup=_CANCEL_PROMO_KB, parse_mode="HTML")
    await state.set_state(PaymentSystemStates.waiting_discount)


@payment_system_router.message(PaymentSystemStates.waiting_discount)
async def process_discount(message: Message, state: FSMContext):
    """Шаг 4 (одиночный) или финал (batch): ввод скидки."""
    data = await state.get_data()
    disc_type = data.get('discount_type', 'percent')
    try:
        discount = int(message.text.strip())
        if disc_type == 'percent' and not (1 <= discount <= 90):
            raise ValueError()
        if disc_type == 'fixed' and discount <= 0:
            raise ValueError()
    except ValueError:
        hint = "от 1 до 90" if disc_type == 'percent' else "больше 0"
        await fsm_edit(state, message, f"❌ Неверный формат. Введите число {hint}", reply_markup=_CANCEL_PROMO_KB)
        return
    await state.update_data(discount=discount)

    # Batch mode: сразу сохраняем набор кодов (max_usage=1 у каждого)
    if data.get('batch_mode'):
        await _save_batch_promocodes(message, state)
        return

    code = data.get('promocode', '')
    disc_label = f"{discount}₽" if disc_type == 'fixed' else f"{discount}%"
    text = (
        f"🎁 <b>Создание промокода — шаг 4/5</b>\n\n"
        f"Код: <code>{code}</code> · Скидка: {disc_label}\n\n"
        f"Введите максимальное количество использований:\n"
        f"<i>Пример: 10, 50, 100</i>"
    )
    await fsm_edit(state, message, text, reply_markup=_CANCEL_PROMO_KB)
    await state.set_state(PaymentSystemStates.waiting_max_usage)


@payment_system_router.message(PaymentSystemStates.waiting_max_usage)
async def process_max_usage(message: Message, state: FSMContext):
    """Шаг 5: срок действия."""
    try:
        max_usage = int(message.text.strip())
        if max_usage <= 0:
            raise ValueError()
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат. Введите число больше 0", reply_markup=_CANCEL_PROMO_KB)
        return
    await state.update_data(max_usage=max_usage)
    data = await state.get_data()
    code = data.get('promocode', '')
    disc_type = data.get('discount_type', 'percent')
    discount = data.get('discount', 0)
    disc_label = f"{discount}₽" if disc_type == 'fixed' else f"{discount}%"
    text = (
        f"🎁 <b>Создание промокода — шаг 5/5</b>\n\n"
        f"Код: <code>{code}</code> · Скидка: {disc_label} · Лимит: {max_usage}\n\n"
        f"Введите дату окончания действия промокода в формате <b>ГГГГ-ММ-ДД</b>:\n"
        f"<i>Пример: 2025-12-31</i>\n\n"
        f"Или нажмите «Пропустить» — промокод будет бессрочным."
    )
    await fsm_edit(state, message, text, reply_markup=_SKIP_EXPIRES_KB)
    await state.set_state(PaymentSystemStates.waiting_expires_at)


@payment_system_router.callback_query(F.data == "promo_skip_expires")
async def promo_skip_expires(callback: CallbackQuery, state: FSMContext):
    """Пропустить срок действия → спросить о тарифах."""
    await callback.answer()
    await state.update_data(expires_at=None)
    await _ask_allowed_plans(callback.message, state)


@payment_system_router.message(PaymentSystemStates.waiting_expires_at)
async def process_expires_at(message: Message, state: FSMContext):
    """Обработать дату окончания действия."""
    from datetime import datetime
    raw = message.text.strip()
    try:
        dt = datetime.strptime(raw, '%Y-%m-%d')
        if dt.date() <= datetime.utcnow().date():
            await fsm_edit(state, message, "❌ Дата должна быть в будущем. Введите в формате ГГГГ-ММ-ДД.", reply_markup=_SKIP_EXPIRES_KB)
            return
        expires_at = raw
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат. Введите дату как ГГГГ-ММ-ДД (например, 2025-12-31).", reply_markup=_SKIP_EXPIRES_KB)
        return
    await state.update_data(expires_at=expires_at)
    await _ask_allowed_plans(message, state)


async def _ask_allowed_plans(target, state: FSMContext):
    """Спросить об ограничении по тарифным планам."""
    from subscription_handlers import get_current_subscription_plans
    plans = get_current_subscription_plans()
    plan_names = [v['name'] for v in plans.values()]
    plan_list = "\n".join(f"• <code>{n}</code>" for n in plan_names) if plan_names else "(нет активных планов)"
    text = (
        f"🎁 <b>Ограничение по тарифу (необязательно)</b>\n\n"
        f"Доступные тарифы:\n{plan_list}\n\n"
        f"Введите названия тарифов через запятую, чтобы промокод работал только для них:\n"
        f"<i>Пример: Стандарт, Премиум</i>\n\n"
        f"Или нажмите «Пропустить» — промокод будет работать для всех тарифов."
    )
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=_SKIP_PLANS_KB, parse_mode="HTML")
    else:
        await fsm_edit(state, target, text, reply_markup=_SKIP_PLANS_KB)
    await state.set_state(PaymentSystemStates.waiting_allowed_plans)


@payment_system_router.callback_query(F.data == "promo_skip_plans")
async def promo_skip_plans(callback: CallbackQuery, state: FSMContext):
    """Пропустить ограничение по тарифам → сохранить промокод."""
    await callback.answer()
    await state.update_data(allowed_plans=None)
    await _save_new_promocode(callback.message, state)


@payment_system_router.message(PaymentSystemStates.waiting_allowed_plans)
async def process_allowed_plans(message: Message, state: FSMContext):
    """Обработать список разрешённых тарифов → сохранить промокод."""
    import json
    raw = message.text.strip()
    plan_names = [p.strip() for p in raw.split(',') if p.strip()]
    allowed_plans = json.dumps(plan_names, ensure_ascii=False) if plan_names else None
    await state.update_data(allowed_plans=allowed_plans)
    await _save_new_promocode(message, state)


async def _save_new_promocode(target, state: FSMContext):
    """Финальное сохранение промокода после всех шагов wizard'а."""
    db = _get_db()
    data = await state.get_data()
    code = data.get('promocode', '')
    discount = data.get('discount', 0)
    disc_type = data.get('discount_type', 'percent')
    max_usage = data.get('max_usage', 1)
    expires_at = data.get('expires_at')
    allowed_plans = data.get('allowed_plans')

    disc_label = f"{discount}₽" if disc_type == 'fixed' else f"{discount}%"
    exp_label = expires_at or "бессрочный"
    plans_label = allowed_plans or "все тарифы"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Управление промокодами", callback_data="manage_promocodes")]
    ])

    try:
        db.create_promocode(
            code=code,
            discount_percent=discount,
            max_usage=max_usage,
            discount_type=disc_type,
            expires_at=expires_at,
            allowed_plans=allowed_plans,
        )
        text = (
            f"✅ <b>Промокод создан!</b>\n\n"
            f"🎁 Код: <code>{code}</code>\n"
            f"💸 Скидка: {disc_label}\n"
            f"📊 Лимит: {max_usage} использований\n"
            f"⏳ Срок: {exp_label}\n"
            f"📋 Тарифы: {plans_label}"
        )
    except Exception as e:
        text = f"❌ Ошибка создания промокода: {he(str(e))}"

    if hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await fsm_edit(state, target, text, reply_markup=keyboard)
    await clear_state_keep_org(state)


@payment_system_router.callback_query(F.data == "cancel_add_promo")
async def cancel_add_promo(callback: CallbackQuery, state: FSMContext):
    """Отмена создания промокода."""
    await clear_state_keep_org(state)
    await callback.answer()
    await manage_promocodes_menu(callback)


# ─────────────────── BATCH CREATION ───────────────────

@payment_system_router.callback_query(F.data == "batch_promocode_start")
async def batch_promocode_start(callback: CallbackQuery, state: FSMContext):
    """Начало пакетного создания промокодов."""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    await callback.answer()
    text = (
        "📦 <b>Пакетное создание промокодов</b>\n\n"
        "Создаёт набор уникальных одноразовых кодов (max_usage=1).\n"
        "Каждый код = префикс + 6 случайных символов.\n\n"
        "Введите количество промокодов (1–100):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="manage_promocodes")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_batch_count)


@payment_system_router.message(PaymentSystemStates.waiting_batch_count)
async def process_batch_count(message: Message, state: FSMContext):
    """Принять количество → спросить префикс."""
    _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="manage_promocodes")]])
    try:
        count = int(message.text.strip())
        if not (1 <= count <= 100):
            raise ValueError()
    except ValueError:
        await fsm_edit(state, message, "❌ Введите число от 1 до 100.", reply_markup=_cancel_kb)
        return
    await state.update_data(batch_count=count)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Без префикса", callback_data="batch_promo_no_prefix")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="manage_promocodes")],
    ])
    await fsm_edit(state, message,
        f"📦 <b>Количество:</b> {count}\n\n"
        "Введите префикс для кодов (лат. буквы/цифры, мин. 2):\n"
        "<i>Пример: PARTNER → PARTNER_A3K9X2</i>\n\n"
        "Или нажмите «Без префикса».",
        reply_markup=kb)
    await state.set_state(PaymentSystemStates.waiting_batch_prefix)


@payment_system_router.callback_query(F.data == "batch_promo_no_prefix")
async def batch_promo_no_prefix(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(batch_prefix='')
    await _ask_batch_discount(callback.message, state)


@payment_system_router.message(PaymentSystemStates.waiting_batch_prefix)
async def process_batch_prefix(message: Message, state: FSMContext):
    prefix = message.text.strip().upper()
    _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="manage_promocodes")]])
    if not prefix.replace('_', '').isalnum() or len(prefix) < 2:
        await fsm_edit(state, message, "❌ Префикс: только лат. буквы/цифры, минимум 2 символа.", reply_markup=_cancel_kb)
        return
    await state.update_data(batch_prefix=prefix + '_')
    await _ask_batch_discount(message, state)


async def _ask_batch_discount(target, state: FSMContext):
    data = await state.get_data()
    count = data.get('batch_count', 1)
    prefix = data.get('batch_prefix', '')
    text = (
        f"📦 <b>Пакет: {count} кодов · префикс: {prefix or 'нет'}</b>\n\n"
        "Введите размер скидки в процентах (1–90):\n"
        "<i>Все коды получат одинаковую скидку. Каждый код — одноразовый.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="manage_promocodes")]
    ])
    await state.update_data(batch_mode=True, discount_type='percent')
    await state.set_state(PaymentSystemStates.waiting_discount)
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await fsm_edit(state, target, text, reply_markup=kb)


async def _save_batch_promocodes(message: Message, state: FSMContext):
    """Создать все коды пакета и отчитаться."""
    db = _get_db()
    data = await state.get_data()
    count = data.get('batch_count', 1)
    prefix = data.get('batch_prefix', '')
    discount = data.get('discount', 10)
    disc_type = data.get('discount_type', 'percent')

    created = db.create_promocodes_batch(
        prefix=prefix,
        discount_percent=discount,
        count=count,
        discount_type=disc_type,
    )
    disc_label = f"{discount}₽" if disc_type == 'fixed' else f"{discount}%"
    lines = "\n".join(f"• <code>{c}</code>" for c in created[:10])
    more = f"\n<i>...и ещё {len(created) - 10}</i>" if len(created) > 10 else ""
    skipped = count - len(created)
    skip_note = f"\n⚠️ Пропущено (дубликаты): {skipped}" if skipped > 0 else ""
    text = (
        f"✅ <b>Пакет создан!</b>\n\n"
        f"📦 Создано кодов: <b>{len(created)}</b> · скидка: {disc_label}\n"
        f"{skip_note}\n\n"
        f"{lines}{more}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Управление промокодами", callback_data="manage_promocodes")]
    ])
    await fsm_edit(state, message, text, reply_markup=keyboard)
    await clear_state_keep_org(state)


# ─────────────────── EDIT: EXPIRES_AT ───────────────────

@payment_system_router.callback_query(F.data.startswith("edit_promo_expires_"))
async def edit_promo_expires_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования срока действия промокода."""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    promo_id = int(callback.data.split("_")[3])
    db = _get_db()
    promo = db.get_promocode_by_id(promo_id)
    if not promo:
        await callback.answer("❌ Промокод не найден")
        return
    await callback.answer()
    code = promo[1]
    current_exp = promo[7] or "не задан"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Убрать срок", callback_data=f"promo_clear_exp_{promo_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_promo_{promo_id}")],
    ])
    await state.set_data({'promo_id': promo_id})
    await callback.message.edit_text(
        f"⏳ <b>Срок действия промокода {code}</b>\n\n"
        f"Текущий срок: {current_exp}\n\n"
        "Введите новую дату в формате <b>ГГГГ-ММ-ДД</b>:\n"
        "<i>Пример: 2025-12-31</i>",
        reply_markup=kb, parse_mode="HTML"
    )
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.editing_promocode_expires)


@payment_system_router.callback_query(F.data.startswith("promo_clear_exp_"))
async def promo_clear_expires(callback: CallbackQuery, state: FSMContext):
    """Убрать срок действия → сделать промокод бессрочным."""
    promo_id = int(callback.data.split("_")[3])
    db = _get_db()
    db.update_promocode(promo_id, expires_at=None)
    await callback.answer("✅ Срок действия убран — промокод стал бессрочным")
    await clear_state_keep_org(state)
    data_cb = type('obj', (object,), {'data': f"edit_promo_{promo_id}", 'from_user': callback.from_user, 'message': callback.message, 'answer': callback.answer})()
    await edit_specific_promocode(data_cb)


@payment_system_router.message(PaymentSystemStates.editing_promocode_expires)
async def process_promo_expires_edit(message: Message, state: FSMContext):
    """Сохранить новую дату окончания."""
    from datetime import datetime
    data = await state.get_data()
    promo_id = data.get('promo_id')
    raw = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_promo_{promo_id}")]])
    try:
        dt = datetime.strptime(raw, '%Y-%m-%d')
        if dt.date() <= datetime.utcnow().date():
            raise ValueError("past")
    except ValueError:
        await fsm_edit(state, message, "❌ Дата должна быть будущей, формат ГГГГ-ММ-ДД.", reply_markup=_back_kb)
        return
    db = _get_db()
    db.update_promocode(promo_id, expires_at=raw)
    _done_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К промокоду", callback_data=f"edit_promo_{promo_id}")]])
    await fsm_edit(state, message, f"✅ Срок действия обновлён: {raw}", reply_markup=_done_kb)
    await clear_state_keep_org(state)


# Тестовый платеж
@payment_system_router.callback_query(F.data == "test_payment")
async def test_payment(callback: CallbackQuery):
    """Тестовый платеж"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    await callback.answer()
    settings = db.get_payment_settings()
    
    text = "🧪 <b>Тестовый платеж</b>\n\n"
    text += "💳 <b>Реквизиты для перевода:</b>\n"
    text += f"Карта: <code>{settings['card_number']}</code>\n"
    text += f"Получатель: {settings['recipient_name']}\n"
    text += f"Банк: {settings['bank_name']}\n\n"
    text += "💰 Сумма: 990 ₽ (тест базового тарифа)\n\n"
    text += "📋 После перевода прикрепите скриншот чека"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("payment_settings")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

# Экспорт отчета по платежам
@payment_system_router.callback_query(F.data == "export_payment_report")
async def export_payment_report(callback: CallbackQuery):
    """Экспорт отчета по платежам"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
        from datetime import datetime
        
        # Создаем книгу Excel
        wb = Workbook()
        ws = wb.active
        ws.title = "Отчет по платежам"
        
        # Заголовок
        ws['A1'] = f"Отчет по платежной системе от {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        ws['A1'].font = Font(bold=True, size=14)
        ws.merge_cells('A1:F1')
        
        # Получаем статистику
        stats = db.get_detailed_payment_statistics()
        subscriptions = db.get_all_active_subscriptions()
        
        # Общая статистика
        row = 3
        ws[f'A{row}'] = "ОБЩАЯ СТАТИСТИКА"
        ws[f'A{row}'].font = Font(bold=True)
        row += 1
        
        ws[f'A{row}'] = f"Общая выручка: {stats['total_revenue']:,.0f} ₽"
        row += 1
        ws[f'A{row}'] = f"Всего платежей: {stats['total_payments']}"
        row += 1
        ws[f'A{row}'] = f"Средний чек: {stats['average_payment']:,.0f} ₽"
        row += 1
        ws[f'A{row}'] = f"Месячная выручка: {stats['monthly_revenue']:,.0f} ₽"
        row += 2
        
        # Активные подписки
        ws[f'A{row}'] = "АКТИВНЫЕ ПОДПИСКИ"
        ws[f'A{row}'].font = Font(bold=True)
        row += 1
        
        headers = ['Пользователь', 'Тариф', 'Магазин', 'Начало', 'Окончание']
        for col, header in enumerate(headers, 1):
            ws.cell(row=row, column=col, value=header).font = Font(bold=True)
        row += 1
        
        for sub in subscriptions:
            ws.cell(row=row, column=1, value=f"{sub[7]} {sub[8]}")  # first_name, last_name
            ws.cell(row=row, column=2, value=sub[2])  # plan_type
            ws.cell(row=row, column=3, value=sub[10])  # shop_name
            ws.cell(row=row, column=4, value=sub[3][:10])  # start_date
            ws.cell(row=row, column=5, value=sub[4][:10])  # end_date
            row += 1
        
        # Сохраняем файл
        filename = f"payment_report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        filepath = f"data/{filename}"
        wb.save(filepath)
        
        # Отправляем файл
        from aiogram.types import FSInputFile
        document = FSInputFile(filepath)
        
        await callback.message.answer_document(
            document=document,
            caption="📊 Отчет по платежной системе"
        )
        
        # Удаляем временный файл
        import os
        os.remove(filepath)
        
        await callback.answer("Отчет экспортирован")
        
    except Exception as e:
        await callback.answer(f"Ошибка экспорта: {str(e)}", show_alert=True)

# Дополнительные обработчики платежной системы

@payment_system_router.callback_query(F.data == "set_payment_instruction")
async def set_payment_instruction(callback: CallbackQuery, state: FSMContext):
    """Установка инструкции для пользователей"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "📝 <b>Настройка инструкции для пользователей</b>\n\n" \
           "Введите новую инструкцию, которую будут видеть пользователи при оплате:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="payment_settings")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_payment_instruction)

@payment_system_router.message(PaymentSystemStates.waiting_payment_instruction)
async def process_payment_instruction(message: Message, state: FSMContext):
    """Сохранение инструкции для пользователей"""
    db = _get_db()
    instruction = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки оплаты", callback_data="payment_settings")]])
    if len(instruction) < 5:
        await fsm_edit(state, message, "❌ Инструкция слишком короткая. Введите более подробный текст:", reply_markup=_back_kb)
        return
    try:
        db.update_payment_setting('payment_instruction', instruction)
        await fsm_edit(state, message, "✅ Инструкция для пользователей обновлена.", reply_markup=_back_kb)
    except Exception as e:
        logging.error(f"process_payment_instruction: DB error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при сохранении инструкции. Попробуйте позже.", reply_markup=_back_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "set_discounts")
async def set_discounts(callback: CallbackQuery):
    """Настройка скидок"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "🎯 <b>Настройка скидок</b>\n\n" \
           "Функция настройки скидок находится в разработке.\n" \
           "Пока используйте промокоды для предоставления скидок."
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Промокоды", callback_data="manage_promocodes")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="payment_settings")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "subscription_details")
async def subscription_details(callback: CallbackQuery):
    """Детали подписок"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    active_subs = db.get_all_active_subscriptions()

    text = "📊 <b>Детали активных подписок</b>\n\n"

    if not active_subs:
        text += "📭 Нет активных подписок"
    else:
        for sub in active_subs[:10]:  # Показываем первые 10
            # Безопасное извлечение данных с проверкой длины кортежа
            # Структура: (id, user_id, plan_type, start_date, end_date, first_name, last_name, email, shop_name, telegram_id, phone, city)
            first_name = sub[5] if len(sub) > 5 else "Неизвестно"
            last_name = sub[6] if len(sub) > 6 else ""
            plan_type = sub[2] if len(sub) > 2 else "Неизвестно"
            end_date = sub[4] if len(sub) > 4 else "Неизвестно"

            # Безопасное извлечение shop_name
            shop_name = "Не указан"
            if len(sub) > 8:
                shop_name = sub[8] if sub[8] else "Не указан"

            user_name = f"{first_name} {last_name}".strip()
            text += f"👤 <b>{he(user_name)}</b>\n"
            text += f"💎 План: {plan_type}\n"
            text += f"🏪 Магазин: {he(shop_name)}\n"
            text += f"📅 До: {end_date[:10] if len(end_date) >= 10 else end_date}\n\n"

        if len(active_subs) > 10:
            text += f"... и еще {len(active_subs) - 10} подписок"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти пользователя", callback_data="find_user_subscription")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "find_user_subscription")
async def find_user_subscription(callback: CallbackQuery, state: FSMContext):
    """Поиск подписки пользователя"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "🔍 <b>Поиск подписки пользователя</b>\n\n" \
           "Введите Telegram ID пользователя для поиска его подписки:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_user_telegram_id)

@payment_system_router.message(PaymentSystemStates.waiting_user_telegram_id)
async def process_find_user_subscription(message: Message, state: FSMContext):
    """Поиск подписки пользователя по Telegram ID"""
    db = _get_db()
    raw = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]])
    if not raw.isdigit():
        await fsm_edit(state, message, "❌ Введите числовой Telegram ID:", reply_markup=_back_kb)
        return
    user_id = int(raw)
    sub = db.get_user_subscription(user_id)
    if not sub:
        await fsm_edit(state, message, f"📭 У пользователя {user_id} нет активной подписки.", reply_markup=_back_kb)
    else:
        plan_type = sub[2] if len(sub) > 2 else "—"
        end_date = sub[4] if len(sub) > 4 else "—"
        await fsm_edit(state, message,
            f"📋 <b>Подписка пользователя {user_id}</b>\n\n"
            f"💎 Тариф: {plan_type}\n"
            f"📅 До: {end_date[:10] if end_date and len(end_date) >= 10 else end_date}",
            reply_markup=_back_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "grant_subscription")
async def grant_subscription(callback: CallbackQuery, state: FSMContext):
    """Выдача подписки пользователю"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "🎁 <b>Выдача подписки пользователю</b>\n\n" \
           "Введите Telegram ID пользователя для выдачи подписки:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_grant_user_id)

@payment_system_router.message(PaymentSystemStates.waiting_grant_user_id)
async def process_grant_subscription(message: Message, state: FSMContext):
    """Выдача подписки пользователю — принимаем ID и план через пробел"""
    db = _get_db()
    parts = message.text.strip().split()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]])
    if len(parts) < 2:
        await fsm_edit(state, message,
            "❌ Введите данные в формате: <code>TELEGRAM_ID НАЗВАНИЕ_ТАРИФА</code>\n"
            "Пример: <code>123456789 Базовый</code>",
            reply_markup=_back_kb)
        return
    raw_id, plan_name = parts[0], " ".join(parts[1:])
    if not raw_id.isdigit():
        await fsm_edit(state, message, "❌ Telegram ID должен быть числом:", reply_markup=_back_kb)
        return
    user_id = int(raw_id)
    success = db.create_subscription(user_id, plan_name)
    if success:
        await fsm_edit(state, message,
            f"✅ Подписка <b>{he(plan_name)}</b> выдана пользователю {user_id}.",
            reply_markup=_back_kb)
    else:
        await fsm_edit(state, message,
            "❌ Не удалось выдать подписку. Проверьте ID и название тарифа.",
            reply_markup=_back_kb
        )
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "payment_charts")
async def payment_charts(callback: CallbackQuery):
    """Графики платежей"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "📈 <b>Графики и аналитика</b>\n\n" \
           "Функция визуализации данных находится в разработке.\n" \
           "Пока доступна текстовая статистика."
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="payment_statistics")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="payment_system_admin")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "edit_plan")
async def edit_plan(callback: CallbackQuery):
    """Редактирование тарифных планов"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    plans = db.get_subscription_plans()
    
    text = "✏️ <b>Редактирование тарифных планов</b>\n\n"
    
    if not plans:
        text += "📭 Нет доступных тарифных планов для редактирования"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить план", callback_data="add_plan")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_plans")]
        ])
    else:
        text += "Выберите план для редактирования:\n\n"
        keyboard_buttons = []
        
        for plan in plans:
            plan_id = plan[0]
            name = plan[1]
            duration_days = plan[2]
            price = plan[3]
            description = plan[4]
            is_active = plan[5]
            status = "✅" if is_active else "❌"
            text += f"{status} <b>{he(name)}</b> - {price}₽ ({duration_days} дней)\n"
            keyboard_buttons.append([InlineKeyboardButton(
                text=f"✏️ {name}", 
                callback_data=f"edit_plan_{plan_id}"
            )])
        
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="manage_plans")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "toggle_plan")
async def toggle_plan(callback: CallbackQuery):
    """Переключение статуса тарифных планов"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    plans = db.get_all_subscription_plans()
    
    text = "🔄 <b>Переключение статуса планов</b>\n\n"
    
    if not plans:
        text += "📭 Нет доступных тарифных планов"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить план", callback_data="add_plan")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_plans")]
        ])
    else:
        text += "Выберите план для изменения статуса:\n\n"
        keyboard_buttons = []
        
        for plan in plans:
            plan_id = plan[0]
            name = plan[1] 
            is_active = plan[5]  # is_active находится в 6-м поле (индекс 5)
            status_text = "✅ Активен" if is_active else "❌ Неактивен"
            
            if is_active:
                button_text = f"❌ {name} (Деактивировать)"
            else:
                button_text = f"✅ {name} (Активировать)"
            
            text += f"<b>{he(name)}</b> - {status_text}\n"
            keyboard_buttons.append([InlineKeyboardButton(
                text=button_text, 
                callback_data=f"toggle_plan_{plan_id}"
            )])
        
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="manage_plans")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("toggle_plan_"))
async def execute_plan_toggle(callback: CallbackQuery):
    """Выполнение переключения статуса тарифного плана"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    plan_id = int(callback.data.split('_')[2])
    
    try:
        # Получаем информацию о плане
        plans = db.get_all_subscription_plans()
        target_plan = None
        for plan in plans:
            if plan[0] == plan_id:
                target_plan = plan
                break
        
        if not target_plan:
            await callback.answer("❌ План не найден")
            return
        
        plan_name = target_plan[1]
        current_status = target_plan[5]  # is_active находится в 6-м поле (индекс 5)
        new_status = not current_status
        
        # Получаем пользователей перед изменением статуса
        affected_users = db.get_users_with_subscription_plan(plan_id)
        
        # Обновляем статус плана
        success = db.update_subscription_plan_field(plan_id, 'is_active', new_status)
        
        if not success:
            await callback.answer("❌ Не удалось обновить статус плана")
            return
        
        # Отправляем уведомления пользователям
        if affected_users:
            from plan_notifications import send_plan_deactivation_notifications, send_plan_reactivation_notifications, send_admin_notification
            import asyncio
            
            bot_instance = callback.bot
            if new_status:  # Активация
                asyncio.create_task(send_plan_reactivation_notifications(bot_instance, affected_users, plan_name))
                asyncio.create_task(send_admin_notification(bot_instance, "activate", plan_name, len(affected_users), callback.from_user.id))
            else:  # Деактивация
                asyncio.create_task(send_plan_deactivation_notifications(bot_instance, affected_users, plan_name))
                asyncio.create_task(send_admin_notification(bot_instance, "deactivate", plan_name, len(affected_users), callback.from_user.id))
        
        status_text = "активирован" if new_status else "деактивирован"
        notification_text = f"✅ План '{plan_name}' {status_text}"
        if affected_users:
            notification_text += f" ({len(affected_users)} пользователей уведомлены)"
        
        await callback.answer(notification_text)
        
        # Обновляем меню переключения статуса с актуальными данными
        plans = db.get_all_subscription_plans()
        
        # Добавляем временную метку для принудительного обновления
        import time
        timestamp = int(time.time())
        
        text = f"🔄 <b>Переключение статуса планов</b> (обновлено: {timestamp % 1000})\n\n"
        text += "Выберите план для изменения статуса:\n\n"
        keyboard_buttons = []
        
        for plan in plans:
            plan_id = plan[0]
            name = plan[1] 
            is_active = plan[5]  # is_active находится в 6-м поле (индекс 5)
            status_text = "✅ Активен" if is_active else "❌ Неактивен"
            
            if is_active:
                button_text = f"❌ {name} (Деактивировать)"
            else:
                button_text = f"✅ {name} (Активировать)"
            
            text += f"<b>{he(name)}</b> - {status_text}\n"
            keyboard_buttons.append([InlineKeyboardButton(
                text=button_text, 
                callback_data=f"toggle_plan_{plan_id}"
            )])
        
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="manage_plans")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {str(e)}")

# Обработчики для удаления тарифных планов
@payment_system_router.callback_query(F.data == "delete_plan")
async def delete_plan_menu(callback: CallbackQuery):
    """Меню удаления тарифных планов"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    plans = db.get_all_subscription_plans()
    
    text = "🗑 <b>Удаление тарифных планов</b>\n\n"
    
    if not plans:
        text += "📭 Нет доступных тарифных планов для удаления"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить план", callback_data="add_plan")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_plans")]
        ])
    else:
        text += "⚠️ <b>Внимание!</b> Удаление тарифа может повлиять на активные подписки.\n\n"
        text += "Выберите план для удаления:\n\n"
        keyboard_buttons = []
        
        # Получаем статистику использования каждого плана
        for plan in plans:
            plan_id = plan[0]
            name = plan[1]
            duration_days = plan[2]
            price = plan[3]
            description = plan[4]
            is_active = plan[5]
            
            # Проверяем сколько пользователей использует этот план
            try:
                active_users = db.get_users_with_subscription_plan(plan_id)
                users_count = len(active_users) if active_users else 0
            except Exception:
                users_count = 0
            
            status = "✅" if is_active else "❌"
            text += f"{status} <b>{he(name)}</b> - {price:,.0f} ₽ ({duration_days} дней)\n"
            text += f"   👥 Активных подписок: {users_count}\n\n"
            
            # Добавляем предупреждение если есть активные подписки
            warning = f" ⚠️ ({users_count} подписок)" if users_count > 0 else ""
            keyboard_buttons.append([InlineKeyboardButton(
                text=f"🗑 {name}{warning}", 
                callback_data=f"delete_plan_{plan_id}"
            )])
        
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="manage_plans")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("delete_plan_"))
async def confirm_delete_plan(callback: CallbackQuery):
    """Подтверждение удаления тарифного плана"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    plan_id = int(callback.data.split("_")[2])
    
    # Получаем информацию о плане
    plans = db.get_all_subscription_plans()
    target_plan = None
    for plan in plans:
        if plan[0] == plan_id:
            target_plan = plan
            break
    
    if not target_plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return
    
    await callback.answer()
    plan_name = target_plan[1]
    plan_price = target_plan[3]
    plan_duration = target_plan[2]
    
    # Проверяем активные подписки
    try:
        active_users = db.get_users_with_subscription_plan(plan_id)
        users_count = len(active_users) if active_users else 0
    except Exception:
        active_users = []
        users_count = 0
    
    text = f"🗑 <b>Подтверждение удаления тарифа</b>\n\n"
    text += f"Тариф: <b>{he(plan_name)}</b>\n"
    text += f"Цена: {plan_price:,.0f} ₽\n"
    text += f"Длительность: {plan_duration} дней\n\n"
    
    if users_count > 0:
        text += f"⚠️ <b>ВНИМАНИЕ!</b>\n"
        text += f"У этого тарифа есть {users_count} активных подписок.\n\n"
        text += f"При удалении тарифа:\n"
        text += f"• Активные подписки останутся до окончания срока\n"
        text += f"• Новые подписки на этот тариф будут невозможны\n"
        text += f"• Пользователи не смогут продлить подписку\n\n"
        
        text += f"❗️ Это действие нельзя отменить!"
    else:
        text += f"✅ У этого тарифа нет активных подписок.\n"
        text += f"Удаление безопасно.\n\n"
        text += f"❗️ Это действие нельзя отменить!"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить тариф", callback_data=f"confirm_delete_plan_{plan_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="delete_plan")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("confirm_delete_plan_"))
async def execute_delete_plan(callback: CallbackQuery):
    """Выполнение удаления тарифного плана"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора", show_alert=True)
        return
    
    plan_id = int(callback.data.split("_")[3])
    
    try:
        # Получаем информацию о плане перед удалением
        plans = db.get_all_subscription_plans()
        target_plan = None
        for plan in plans:
            if plan[0] == plan_id:
                target_plan = plan
                break
        
        if not target_plan:
            await callback.answer("❌ План не найден", show_alert=True)
            return
        
        plan_name = target_plan[1]
        
        # Удаляем план из базы данных
        deletion_result = db.delete_subscription_plan(plan_id)
        
        if deletion_result and deletion_result.get('success'):
            plan_name = deletion_result['plan_name']
            affected_users = deletion_result['affected_users']
            
            # Отправляем уведомления пользователям
            from plan_notifications import send_plan_deletion_notifications, send_admin_notification
            
            if affected_users:
                # Отправляем уведомления в фоновом режиме
                import asyncio
                bot_instance = callback.bot
                asyncio.create_task(send_plan_deletion_notifications(bot_instance, affected_users, plan_name))
                asyncio.create_task(send_admin_notification(bot_instance, "delete", plan_name, len(affected_users), callback.from_user.id))
            
            text = f"✅ <b>Тариф удален</b>\n\n"
            text += f"Тариф '{plan_name}' успешно удален из системы.\n\n"
            
            if affected_users:
                text += f"👥 <b>Уведомления отправлены:</b>\n"
                text += f"• {len(affected_users)} пользователям с активными подписками\n"
                text += f"• Все пользователи проинформированы о сохранении условий до окончания подписки\n\n"
            
            text += f"📋 Обновленный список тарифов:"
            
            # Показываем обновленный список планов
            remaining_plans = db.get_all_subscription_plans()
            if remaining_plans:
                text += f"\n\n"
                for plan in remaining_plans:
                    status = "✅" if plan[5] else "❌"
                    text += f"{status} <b>{plan[1]}</b> - {plan[3]:,.0f} ₽\n"
            else:
                text += f"\n\n📭 Тарифных планов не осталось"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Управление тарифами", callback_data="manage_plans")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="payment_system_admin")]
            ])
            
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            await callback.answer("✅ Тариф успешно удален")
            
        else:
            await callback.answer("❌ Не удалось удалить тариф", show_alert=True)
            
    except Exception as e:
        await callback.answer(f"❌ Ошибка при удалении: {str(e)}", show_alert=True)

@payment_system_router.callback_query(F.data == "delete_promocode")
async def delete_promocode(callback: CallbackQuery):
    """Удаление промокодов"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    promocodes = db.get_all_promocodes()
    
    text = "🗑 <b>Удаление промокодов</b>\n\n"
    
    if not promocodes:
        text += "📭 Нет доступных промокодов для удаления"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать промокод", callback_data="create_promocode")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_promocodes")]
        ])
    else:
        text += "Выберите промокод для удаления:\n\n"
        keyboard_buttons = []

        for promo in promocodes:
            promo_id, code, discount, disc_type, usage_count, max_usage, is_active, expires_at, allowed_plans, last_used, created_at = promo
            disc_label = f"{discount}₽" if disc_type == 'fixed' else f"{discount}%"
            text += f"🎁 <b>{code}</b> — {disc_label} (использован {usage_count}/{max_usage})\n"
            keyboard_buttons.append([InlineKeyboardButton(
                text=f"🗑 {code}",
                callback_data=f"delete_promo_{promo_id}"
            )])
        
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="manage_promocodes")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("delete_promo_"))
async def confirm_delete_promocode(callback: CallbackQuery):
    """Подтверждение удаления промокода"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    promo_id = int(callback.data.split("_")[2])
    promo = db.get_promocode_by_id(promo_id)
    
    if not promo:
        await callback.answer("❌ Промокод не найден")
        return
    
    await callback.answer()
    code = promo[1]  # code находится во втором поле
    text = f"🗑 <b>Подтверждение удаления</b>\n\n" \
           f"Вы действительно хотите удалить промокод:\n" \
           f"🎁 <b>{code}</b>\n\n" \
           f"❗️ Это действие нельзя отменить!"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_promo_{promo_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="delete_promocode")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("confirm_delete_promo_"))
async def execute_delete_promocode(callback: CallbackQuery):
    """Выполнение удаления промокода"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    promo_id = int(callback.data.split("_")[3])
    promo = db.get_promocode_by_id(promo_id)
    
    if not promo:
        await callback.answer("❌ Промокод не найден")
        return
    
    code = promo[1]
    success = db.delete_promocode(promo_id)
    
    if success:
        text = f"✅ <b>Промокод удален</b>\n\n" \
               f"Промокод <b>{code}</b> был успешно удален из системы."
        await callback.answer("✅ Промокод удален")
    else:
        text = f"❌ <b>Ошибка удаления</b>\n\n" \
               f"Не удалось удалить промокод <b>{code}</b>."
        await callback.answer("❌ Ошибка удаления")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить еще", callback_data="delete_promocode")],
        [InlineKeyboardButton(text="🔙 К управлению промокодами", callback_data="manage_promocodes")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "edit_promocode")
async def edit_promocode(callback: CallbackQuery):
    """Редактирование промокодов"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    promocodes = db.get_all_promocodes()
    
    text = "✏️ <b>Редактирование промокодов</b>\n\n"
    
    if not promocodes:
        text += "📭 Нет доступных промокодов для редактирования"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать промокод", callback_data="create_promocode")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_promocodes")]
        ])
    else:
        text += "Выберите промокод для редактирования:\n\n"
        keyboard_buttons = []
        
        for promo in promocodes:
            promo_id, code, discount, disc_type, usage_count, max_usage, is_active, expires_at, allowed_plans, last_used, created_at = promo
            status = "✅" if is_active else "❌"
            disc_label = f"{discount}₽" if disc_type == 'fixed' else f"{discount}%"
            exp_label = f" до {expires_at}" if expires_at else ""
            text += f"{status} <b>{code}</b> — {disc_label}{exp_label}\n"
            text += f"   Использован: {usage_count}/{max_usage}\n\n"
            keyboard_buttons.append([InlineKeyboardButton(
                text=f"✏️ {code}",
                callback_data=f"edit_promo_{promo_id}"
            )])
        
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="manage_promocodes")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("edit_promo_"))
async def edit_specific_promocode(callback: CallbackQuery):
    """Редактирование конкретного промокода"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    promo_id = int(callback.data.split("_")[2])
    promo = db.get_promocode_by_id(promo_id)
    
    if not promo:
        await callback.answer("❌ Промокод не найден")
        return
    
    await callback.answer()
    promo_id, code, discount_percent, disc_type, current_usage, max_usage, is_active, expires_at, allowed_plans, last_used, created_at = promo
    status = "✅ Активен" if is_active else "❌ Неактивен"
    disc_label = f"{discount_percent}₽" if disc_type == 'fixed' else f"{discount_percent}%"
    exp_label = expires_at or "не задан"
    plans_label = allowed_plans or "все тарифы"

    text = (
        f"✏️ <b>Редактирование промокода</b>\n\n"
        f"🎁 <b>Код:</b> {code}\n"
        f"💰 <b>Скидка:</b> {disc_label} ({disc_type})\n"
        f"📊 <b>Использований:</b> {current_usage}/{max_usage}\n"
        f"⏳ <b>Действует до:</b> {exp_label}\n"
        f"📋 <b>Тарифы:</b> {plans_label}\n"
        f"📈 <b>Статус:</b> {status}\n"
        f"📅 <b>Создан:</b> {created_at[:10]}\n\n"
        f"Что изменить?"
    )

    keyboard_buttons = []

    if is_active:
        keyboard_buttons.append([InlineKeyboardButton(text="❌ Деактивировать", callback_data=f"toggle_promo_status_{promo_id}")])
    else:
        keyboard_buttons.append([InlineKeyboardButton(text="✅ Активировать", callback_data=f"toggle_promo_status_{promo_id}")])

    keyboard_buttons.append([InlineKeyboardButton(text="✏️ Изменить скидку", callback_data=f"edit_promo_discount_{promo_id}")])
    keyboard_buttons.append([InlineKeyboardButton(text="🔢 Изменить лимит использований", callback_data=f"edit_promo_max_usage_{promo_id}")])
    keyboard_buttons.append([InlineKeyboardButton(text="⏳ Срок действия", callback_data=f"edit_promo_expires_{promo_id}")])

    keyboard_buttons.append([InlineKeyboardButton(text="🗑 Удалить промокод", callback_data=f"delete_promo_{promo_id}")])
    keyboard_buttons.append([InlineKeyboardButton(text="🔙 К списку промокодов", callback_data="edit_promocode")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data.startswith("toggle_promo_status_"))
async def toggle_promocode_status(callback: CallbackQuery):
    """Переключение статуса промокода"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    promo_id = int(callback.data.split("_")[3])
    promo = db.get_promocode_by_id(promo_id)
    
    if not promo:
        await callback.answer("❌ Промокод не найден")
        return
    
    current_status = bool(promo[6])  # is_active поле (индекс 6 в новой схеме)
    new_status = not current_status
    
    success = db.update_promocode(promo_id, is_active=new_status)
    
    if success:
        status_text = "активирован" if new_status else "деактивирован"
        await callback.answer(f"✅ Промокод {status_text}")
        # Обновляем информацию о промокоде
        await edit_specific_promocode(callback)
    else:
        await callback.answer("❌ Ошибка изменения статуса")

@payment_system_router.callback_query(F.data.startswith("edit_promo_discount_"))
async def edit_promocode_discount_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования скидки промокода"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    promo_id = int(callback.data.split("_")[3])
    promo = db.get_promocode_by_id(promo_id)
    
    if not promo:
        await callback.answer("❌ Промокод не найден")
        return
    
    await callback.answer()
    code = promo[1]
    current_discount = promo[2]
    
    text = f"✏️ <b>Изменение скидки промокода</b>\n\n" \
           f"🎁 <b>Код:</b> {code}\n" \
           f"💰 <b>Текущая скидка:</b> {current_discount}%\n\n" \
           f"Введите новый размер скидки (от 1 до 99):"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_promo_{promo_id}")]
    ])
    
    await state.set_data({"promo_id": promo_id})
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.editing_promocode_discount)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.message(PaymentSystemStates.editing_promocode_discount)
async def process_promocode_discount_edit(message: Message, state: FSMContext):
    """Обработка изменения скидки промокода"""
    db = _get_db()
    data = await state.get_data()
    promo_id = data.get("promo_id")
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_promo_{promo_id}")]])

    try:
        new_discount = int(message.text.strip())
        if not (1 <= new_discount <= 99):
            raise ValueError("Скидка должна быть от 1 до 99 процентов")
    except ValueError:
        await fsm_edit(state, message, "❌ Некорректное значение! Введите число от 1 до 99:", reply_markup=_back_kb)
        return

    success = db.update_promocode(promo_id, discount_percent=new_discount)
    if success:
        promo = db.get_promocode_by_id(promo_id)
        code = promo[1]
        await fsm_edit(state, message,
            f"✅ <b>Скидка обновлена</b>\n\nСкидка промокода <b>{code}</b> изменена на {new_discount}%",
            reply_markup=_back_kb)
    else:
        await fsm_edit(state, message, "❌ Ошибка обновления скидки", reply_markup=_back_kb)

    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data.startswith("edit_promo_max_usage_"))
async def edit_promocode_max_usage_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования лимита использований промокода"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    promo_id = int(callback.data.split("_")[4])
    promo = db.get_promocode_by_id(promo_id)
    
    if not promo:
        await callback.answer("❌ Промокод не найден")
        return
    
    await callback.answer()
    code = promo[1]
    current_usage = promo[3]
    current_max = promo[4]
    
    text = f"✏️ <b>Изменение лимита использований</b>\n\n" \
           f"🎁 <b>Код:</b> {code}\n" \
           f"📊 <b>Использований:</b> {current_usage}/{current_max}\n\n" \
           f"Введите новый лимит использований (минимум {current_usage}):"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_promo_{promo_id}")]
    ])
    
    await state.set_data({"promo_id": promo_id, "current_usage": current_usage})
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.editing_promocode_max_usage)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.message(PaymentSystemStates.editing_promocode_max_usage)
async def process_promocode_max_usage_edit(message: Message, state: FSMContext):
    """Обработка изменения лимита использований промокода"""
    db = _get_db()
    data = await state.get_data()
    promo_id = data.get("promo_id")
    current_usage = data.get("current_usage")
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_promo_{promo_id}")]])

    try:
        new_max_usage = int(message.text.strip())
        if new_max_usage < current_usage:
            raise ValueError(f"Лимит не может быть меньше текущего количества использований ({current_usage})")
        if new_max_usage <= 0:
            raise ValueError("Лимит должен быть больше 0")
    except ValueError as e:
        await fsm_edit(state, message, f"❌ {str(e)}\nВведите корректное число:", reply_markup=_back_kb)
        return

    success = db.update_promocode(promo_id, max_usage=new_max_usage)
    if success:
        promo = db.get_promocode_by_id(promo_id)
        code = promo[1]
        await fsm_edit(state, message,
            f"✅ <b>Лимит обновлён</b>\n\nЛимит использований промокода <b>{code}</b> изменён на {new_max_usage}",
            reply_markup=_back_kb)
    else:
        await fsm_edit(state, message, "❌ Ошибка обновления лимита", reply_markup=_back_kb)

    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "promocode_stats")
async def promocode_stats(callback: CallbackQuery):
    """Расширенная статистика использования промокодов."""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    stats = db.get_promocode_stats_detailed()

    text = "📊 <b>Статистика промокодов</b>\n\n"

    if not stats:
        text += "📭 Нет промокодов для анализа"
    else:
        total_usage = 0
        total_discount_rub = 0.0
        for p in stats:
            usage_pct = (p['usage_count'] / p['max_usage'] * 100) if p['max_usage'] > 0 else 0
            filled = int(usage_pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            disc_label = f"{p['discount_percent']}₽" if p['discount_type'] == 'fixed' else f"{p['discount_percent']}%"
            status = "✅" if p['is_active'] else "❌"
            last_used = (p['last_used_at'] or '')[:10] or "—"
            exp = f" · до {p['expires_at']}" if p['expires_at'] else ""
            text += (
                f"{status} <b>{p['code']}</b> — {disc_label}{exp}\n"
                f"   [{bar}] {p['usage_count']}/{p['max_usage']} ({usage_pct:.0f}%)\n"
                f"   💸 Скидок выдано: {p['total_discount_rub']:.0f}₽ · последнее: {last_used}\n\n"
            )
            total_usage += p['usage_count']
            total_discount_rub += p['total_discount_rub']

        text += (
            f"──────────────\n"
            f"📊 Итого использований: <b>{total_usage}</b>\n"
            f"💸 Итого скидок: <b>{total_discount_rub:.0f}₽</b>"
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_promocodes")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@payment_system_router.callback_query(F.data == "extend_subscription")
async def extend_subscription(callback: CallbackQuery, state: FSMContext):
    """Продление подписки"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "⏰ <b>Продление подписки</b>\n\n" \
           "Введите Telegram ID пользователя для продления подписки:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_extend_user_id)

@payment_system_router.message(PaymentSystemStates.waiting_extend_user_id)
async def process_extend_subscription(message: Message, state: FSMContext):
    """Продление подписки — принимаем ID и количество дней через пробел"""
    db = _get_db()
    parts = message.text.strip().split()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]])
    if len(parts) < 2:
        await fsm_edit(state, message,
            "❌ Введите данные в формате: <code>TELEGRAM_ID КОЛИЧЕСТВО_ДНЕЙ</code>\n"
            "Пример: <code>123456789 30</code>",
            reply_markup=_back_kb)
        return
    raw_id, raw_days = parts[0], parts[1]
    if not raw_id.isdigit() or not raw_days.isdigit():
        await fsm_edit(state, message, "❌ Telegram ID и количество дней должны быть числами:", reply_markup=_back_kb)
        return
    user_id = int(raw_id)
    days = int(raw_days)
    sub = db.get_user_subscription(user_id)
    if not sub:
        await fsm_edit(state, message, f"❌ У пользователя {user_id} нет подписки для продления.", reply_markup=_back_kb)
        await clear_state_keep_org(state)
        return
    plan_type = sub[2] if len(sub) > 2 else "Базовый"
    success = db.create_subscription(user_id, plan_type)
    if success:
        await fsm_edit(state, message, f"✅ Подписка пользователя {user_id} продлена на {days} дн.", reply_markup=_back_kb)
    else:
        await fsm_edit(state, message, "❌ Не удалось продлить подписку.", reply_markup=_back_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "cancel_subscription")
async def cancel_subscription(callback: CallbackQuery, state: FSMContext):
    """Отмена подписки"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "❌ <b>Отмена подписки</b>\n\n" \
           "Введите Telegram ID пользователя для отмены подписки:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_cancel_user_id)

@payment_system_router.message(PaymentSystemStates.waiting_cancel_user_id)
async def process_cancel_subscription(message: Message, state: FSMContext):
    """Отмена подписки пользователя"""
    db = _get_db()
    raw = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_subscriptions")]])
    if not raw.isdigit():
        await fsm_edit(state, message, "❌ Введите числовой Telegram ID:", reply_markup=_back_kb)
        return
    user_id = int(raw)
    try:
        conn = __import__('sqlite3').connect(db.db_file)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subscriptions SET end_date = datetime('now'), plan_type = 'Бесплатный' WHERE user_id = ?",
            (user_id,)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        if affected > 0:
            await fsm_edit(state, message, f"✅ Подписка пользователя {user_id} отменена.", reply_markup=_back_kb)
        else:
            await fsm_edit(state, message, f"❌ Подписка пользователя {user_id} не найдена.", reply_markup=_back_kb)
    except Exception as e:
        logger.error(f"process_cancel_subscription error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при отмене подписки.", reply_markup=_back_kb)
    await clear_state_keep_org(state)

@payment_system_router.callback_query(F.data == "stats_by_period")
async def stats_by_period(callback: CallbackQuery):
    """Статистика за период"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return
    
    await callback.answer()
    text = "📅 <b>Статистика за период</b>\n\n" \
           "Выберите период для анализа:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 За сегодня", callback_data="stats_today")],
        [InlineKeyboardButton(text="📅 За неделю", callback_data="stats_week")],
        [InlineKeyboardButton(text="📅 За месяц", callback_data="stats_month")],
        [InlineKeyboardButton(text="📅 За год", callback_data="stats_year")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="payment_statistics")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

# ── Пробный период ────────────────────────────────────────────────────────────

@payment_system_router.callback_query(F.data == "trial_settings")
async def trial_settings_menu(callback: CallbackQuery):
    """Настройки пробного периода для новых пользователей"""
    db = _get_db()
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    settings = db.get_payment_settings()
    trial_days = settings.get('trial_days', '14')
    trial_plan = settings.get('trial_plan', 'Премиум')

    # Получаем доступные планы для отображения
    plans = db.get_subscription_plans()
    plans_str = ", ".join(p[1] for p in plans) if plans else "—"

    text = (
        "🎫 <b>Настройки пробного периода</b>\n\n"
        f"⏱ Длительность: <b>{trial_days} дней</b>\n"
        f"💎 Тариф при регистрации: <b>{he(trial_plan)}</b>\n\n"
        f"📋 Доступные тарифы: {he(plans_str)}\n\n"
        "Пробный период активируется автоматически при регистрации "
        "(личный и корпоративный режимы). Установите 0 дней чтобы отключить."
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Изменить длительность (дней)", callback_data="trial_edit_days")],
        [InlineKeyboardButton(text="💎 Изменить тариф", callback_data="trial_edit_plan")],
        [back_button("payment_system_admin")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@payment_system_router.callback_query(F.data == "trial_edit_days")
async def trial_edit_days_start(callback: CallbackQuery, state: FSMContext):
    """Редактирование длительности пробного периода"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    db = _get_db()
    settings = db.get_payment_settings()
    current = settings.get('trial_days', '14')

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="trial_settings")]
    ])
    await callback.message.edit_text(
        f"⏱ <b>Длительность пробного периода</b>\n\n"
        f"Текущее значение: <b>{current} дней</b>\n\n"
        "Введите новое количество дней (0 — отключить пробный период):",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(PaymentSystemStates.waiting_trial_days)


@payment_system_router.message(PaymentSystemStates.waiting_trial_days)
async def process_trial_days(message: Message, state: FSMContext):
    """Сохранение нового значения длительности пробного периода"""
    db = _get_db()
    raw = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К настройкам пробного периода", callback_data="trial_settings")]])
    if not raw.isdigit():
        await fsm_edit(state, message, "❌ Введите целое число (количество дней):", reply_markup=_back_kb)
        return
    days = int(raw)
    if days > 365:
        await fsm_edit(state, message, "❌ Максимум 365 дней:", reply_markup=_back_kb)
        return
    db.update_payment_setting('trial_days', str(days))
    status = f"отключён (0 дней)" if days == 0 else f"<b>{days} дней</b>"
    await fsm_edit(state, message, f"✅ Длительность пробного периода обновлена: {status}", reply_markup=_back_kb)
    await clear_state_keep_org(state)


@payment_system_router.callback_query(F.data == "trial_edit_plan")
async def trial_edit_plan_start(callback: CallbackQuery, state: FSMContext):
    """Выбор тарифа для пробного периода"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    await callback.answer()
    db = _get_db()
    plans = db.get_subscription_plans()

    if not plans:
        await callback.answer("❌ Нет доступных тарифных планов.", show_alert=True)
        return

    # Кнопки с названиями планов
    plan_buttons = [
        [InlineKeyboardButton(text=p[1], callback_data=f"trial_plan_select_{p[1]}")]
        for p in plans
        if p[1] != 'Бесплатный'
    ]
    plan_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="trial_settings")])

    settings = db.get_payment_settings()
    current_plan = settings.get('trial_plan', 'Премиум')

    await callback.message.edit_text(
        f"💎 <b>Тариф для пробного периода</b>\n\n"
        f"Текущий: <b>{he(current_plan)}</b>\n\n"
        "Выберите тариф, который получают новые пользователи на время пробного периода:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=plan_buttons),
        parse_mode="HTML"
    )


@payment_system_router.callback_query(F.data.startswith("trial_plan_select_"))
async def trial_plan_selected(callback: CallbackQuery):
    """Сохранение выбранного тарифа для пробного периода"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ только для супер-администратора")
        return

    plan_name = callback.data.removeprefix("trial_plan_select_")
    db = _get_db()
    db.update_payment_setting('trial_plan', plan_name)

    await callback.answer(f"✅ Тариф пробного периода: {plan_name}")
    await trial_settings_menu(callback)


# ===========================================================================
# Выбор провайдера оплаты (СБП / ЮKassa)
# ===========================================================================

@payment_system_router.callback_query(F.data == "payment_provider_select")
async def payment_provider_select(callback: CallbackQuery):
    """Экран выбора провайдера оплаты"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    db = _get_db()
    current = db.get_payment_provider()

    sbp_mark = "✅ " if current == "sbp" else ""
    yk_mark = "✅ " if current == "yookassa" else ""

    text = (
        "🔀 <b>Провайдер оплаты</b>\n\n"
        "Выберите, как пользователи будут оплачивать подписку:\n\n"
        "💳 <b>СБП</b> — пользователь переводит деньги вручную и присылает скриншот.\n"
        "   Вы проверяете и подтверждаете оплату вручную.\n\n"
        "🏦 <b>ЮKassa</b> — пользователь получает ссылку на оплату, подписка\n"
        "   активируется автоматически после успешного платежа.\n\n"
        "⚠️ <i>Для ЮKassa необходимо: ИП/ООО, договор с ЮKassa, Shop ID и секретный ключ.</i>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{sbp_mark}💳 СБП (ручная проверка)", callback_data="set_provider_sbp")],
        [InlineKeyboardButton(text=f"{yk_mark}🏦 ЮKassa (автоматически)", callback_data="set_provider_yookassa")],
        [InlineKeyboardButton(text="⚙️ Настройки ЮKassa", callback_data="yookassa_config_menu")],
        [back_button("payment_settings")],
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@payment_system_router.callback_query(F.data == "set_provider_sbp")
async def set_provider_sbp(callback: CallbackQuery):
    """Переключить на СБП"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только для супер-администратора", show_alert=True)
        return

    db = _get_db()
    db.set_payment_provider("sbp")
    await callback.answer("✅ Провайдер переключён на СБП")
    await payment_provider_select(callback)


@payment_system_router.callback_query(F.data == "set_provider_yookassa")
async def set_provider_yookassa(callback: CallbackQuery):
    """Переключить на ЮKassa"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только для супер-администратора", show_alert=True)
        return

    db = _get_db()
    cfg = db.get_yookassa_config()
    if not cfg['shop_id'] or not cfg['secret_key']:
        await callback.answer(
            "⚠️ Сначала заполните Shop ID и секретный ключ в настройках ЮKassa",
            show_alert=True,
        )
        return  # пользователь видит предупреждение и может нажать «⚙️ Настройки ЮKassa»

    db.set_payment_provider("yookassa")
    await callback.answer("✅ Провайдер переключён на ЮKassa")
    await payment_provider_select(callback)


# ---------------------------------------------------------------------------
# Настройки ЮKassa
# ---------------------------------------------------------------------------

@payment_system_router.callback_query(F.data == "yookassa_config_menu")
async def yookassa_config_menu(callback: CallbackQuery):
    """Экран настройки ЮKassa"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    db = _get_db()
    cfg = db.get_yookassa_config()

    shop_id_display = cfg['shop_id'] or "Не установлен"
    # Маскируем секретный ключ — показываем только первые 8 и последние 4 символа
    sk = cfg['secret_key']
    if sk and len(sk) > 12:
        secret_display = sk[:8] + "…" + sk[-4:]
    elif sk:
        secret_display = "••••••••"
    else:
        secret_display = "Не установлен"

    return_url_display = cfg['return_url'] or "Не установлен"

    text = (
        "⚙️ <b>Настройки ЮKassa</b>\n\n"
        f"🏪 <b>Shop ID:</b> {he(shop_id_display)}\n"
        f"🔑 <b>Секретный ключ:</b> {he(secret_display)}\n"
        f"🔗 <b>Return URL:</b> {he(return_url_display)}\n\n"
        "<i>Данные берутся из личного кабинета ЮKassa.\n"
        "Секретный ключ хранится в БД — не передавайте его третьим лицам.</i>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏪 Изменить Shop ID", callback_data="yk_set_shop_id")],
        [InlineKeyboardButton(text="🔑 Изменить секретный ключ", callback_data="yk_set_secret_key")],
        [InlineKeyboardButton(text="🔗 Изменить Return URL", callback_data="yk_set_return_url")],
        [back_button("payment_provider_select")],
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@payment_system_router.callback_query(F.data == "yk_set_shop_id")
async def yk_set_shop_id_start(callback: CallbackQuery, state: FSMContext):
    """Начать ввод Shop ID"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    await state.set_state(PaymentSystemStates.waiting_yookassa_shop_id)
    await state.update_data(anchor_msg_id=callback.message.message_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="yookassa_config_menu")]
    ])
    await callback.message.edit_text(
        "🏪 <b>Введите Shop ID ЮKassa</b>\n\n"
        "Это числовой идентификатор вашего магазина в личном кабинете ЮKassa.\n"
        "<i>Пример: 123456</i>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@payment_system_router.message(PaymentSystemStates.waiting_yookassa_shop_id)
async def yk_set_shop_id_save(message: Message, state: FSMContext):
    """Сохранить Shop ID"""
    from message_utils import fsm_edit
    db = _get_db()
    shop_id = message.text.strip() if message.text else ""
    if not shop_id:
        await fsm_edit(state, message, "❌ Пустое значение. Попробуйте ещё раз.")
        return

    db.set_yookassa_config(shop_id=shop_id)
    await clear_state_keep_org(state)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настройки ЮKassa", callback_data="yookassa_config_menu")]
    ])
    await fsm_edit(state, message,
                   f"✅ <b>Shop ID сохранён:</b> {he(shop_id)}",
                   reply_markup=keyboard)


@payment_system_router.callback_query(F.data == "yk_set_secret_key")
async def yk_set_secret_key_start(callback: CallbackQuery, state: FSMContext):
    """Начать ввод секретного ключа"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    await state.set_state(PaymentSystemStates.waiting_yookassa_secret_key)
    await state.update_data(anchor_msg_id=callback.message.message_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="yookassa_config_menu")]
    ])
    await callback.message.edit_text(
        "🔑 <b>Введите секретный ключ ЮKassa</b>\n\n"
        "Находится в личном кабинете ЮKassa → Настройки → Ключи API.\n"
        "<i>Начинается с live_ или test_</i>\n\n"
        "⚠️ <b>Не передавайте ключ посторонним!</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@payment_system_router.message(PaymentSystemStates.waiting_yookassa_secret_key)
async def yk_set_secret_key_save(message: Message, state: FSMContext):
    """Сохранить секретный ключ"""
    from message_utils import fsm_edit
    db = _get_db()
    secret_key = message.text.strip() if message.text else ""
    if not secret_key:
        await fsm_edit(state, message, "❌ Пустое значение. Попробуйте ещё раз.")
        return

    db.set_yookassa_config(secret_key=secret_key)
    await clear_state_keep_org(state)

    sk = secret_key
    masked = (sk[:8] + "…" + sk[-4:]) if len(sk) > 12 else "••••••••"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настройки ЮKassa", callback_data="yookassa_config_menu")]
    ])
    await fsm_edit(state, message,
                   f"✅ <b>Секретный ключ сохранён:</b> {he(masked)}",
                   reply_markup=keyboard)


@payment_system_router.callback_query(F.data == "yk_set_return_url")
async def yk_set_return_url_start(callback: CallbackQuery, state: FSMContext):
    """Начать ввод Return URL"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только для супер-администратора", show_alert=True)
        return

    await callback.answer()
    await state.set_state(PaymentSystemStates.waiting_yookassa_return_url)
    await state.update_data(anchor_msg_id=callback.message.message_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="yookassa_config_menu")]
    ])
    await callback.message.edit_text(
        "🔗 <b>Введите Return URL</b>\n\n"
        "Страница, на которую ЮKassa перенаправит пользователя после оплаты.\n"
        "Если бота открывают через t.me — можно указать ссылку на бот.\n"
        "<i>Пример: https://t.me/YourBotName</i>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@payment_system_router.message(PaymentSystemStates.waiting_yookassa_return_url)
async def yk_set_return_url_save(message: Message, state: FSMContext):
    """Сохранить Return URL"""
    db = _get_db()
    return_url = message.text.strip() if message.text else ""
    if not return_url:
        await fsm_edit(state, message, "❌ Пустое значение. Попробуйте ещё раз.")
        return

    db.set_yookassa_config(return_url=return_url)
    await clear_state_keep_org(state)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настройки ЮKassa", callback_data="yookassa_config_menu")]
    ])
    await fsm_edit(state, message,
                   f"✅ <b>Return URL сохранён:</b> {he(return_url)}",
                   reply_markup=keyboard)
