"""
Обработчики для управления уведомлениями
"""
import os
import asyncio
import logging
import sqlite3
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db_utils import clear_state_keep_org, is_any_admin
from database import Database
from keyboards import back_button
from states import NotificationStates
from env_manager import env_manager
from reports_access_control import get_subscription_offer_message
from notif_utils import add_read_btn

# Создаем роутер
notifications_router = Router()

from db_utils import get_db
from tenant_manager import tenant_manager

@notifications_router.callback_query(F.data == "notifications_menu")
async def notifications_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню управления уведомлениями"""
    # Получаем корректную БД для пользователя/организации
    current_db = await get_db(callback.from_user.id, state)

    is_super = env_manager.is_super_admin(callback.from_user.id)
    user = current_db.get_user(callback.from_user.id)
    
    # Если супер-админ, создаем запись в БД если её нет
    if is_super and not user:
        current_db.add_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "Admin",
            last_name=callback.from_user.last_name or "",
            shop_name=None,
            trade_network=None,
            city=None,
            phone="000"
        )
        user = current_db.get_user(callback.from_user.id)

    if not user:
        await callback.answer("❌ Сначала завершите регистрацию через /start", show_alert=True)
        return

    await callback.answer()
    user_id = user[0]
    # Вычисляем роль один раз — используется и в проверке подписки, и в построении меню
    is_admin = is_any_admin(callback.from_user.id)
    from subscription_utils import check_notifications_permission

    if not is_super and not check_notifications_permission(callback.from_user.id):
        message = get_subscription_offer_message("Система уведомлений", is_admin)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Оформить подписку", callback_data="subscription_menu")],
            [InlineKeyboardButton(text="📋 Посмотреть тарифы", callback_data="subscription_plans")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(message, reply_markup=keyboard, parse_mode="HTML")
        return

    settings = current_db.get_notification_settings(user_id)
    history = current_db.get_notification_history(user_id)
    unread_count = sum(1 for item in history if not item[3])
    
    text = "📥 <b>Управление уведомлениями</b>\n\n"
    text += "📱 <b>Текущие настройки:</b>\n"
    text += f"• Низкие остатки: {'✅' if settings['low_stock_alerts'] else '❌'}\n"
    text += f"• Ежедневные отчеты: {'✅' if settings['daily_reports'] else '❌'}\n"
    text += f"• Уведомления о продажах: {'✅' if settings['sales_alerts'] else '❌'}\n"
    text += f"• Платежные уведомления: {'✅' if settings['payment_alerts'] else '❌'}\n"
    text += f"• Админ уведомления: {'✅' if settings['admin_notifications'] else '❌'}\n"
    text += f"• Порог остатков: {settings['stock_threshold']} шт.\n"
    text += f"• Время уведомлений: {settings['notification_time']}\n\n"
    
    if unread_count > 0:
        text += f"📬 У вас {unread_count} непрочитанных уведомлений\n\n"

    keyboard_buttons = [
        [InlineKeyboardButton(text="⚙️ Настройки уведомлений", callback_data="notification_settings")],
        [InlineKeyboardButton(text="📋 История уведомлений", callback_data="notification_history")],
    ]
    
    if is_admin:
        keyboard_buttons.append([InlineKeyboardButton(text="📅 Запланированные уведомления", callback_data="view_scheduled_notifications")])
        keyboard_buttons.append([InlineKeyboardButton(text="📨 Отправить уведомление", callback_data="admin_send_notification")])
    
    keyboard_buttons.append([back_button("user_profile")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@notifications_router.callback_query(F.data == "admin_send_notification")
async def admin_send_notification_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса отправки уведомления администратором"""
    is_admin = is_any_admin(callback.from_user.id)
    if not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
        
    await callback.answer()
    await callback.message.edit_text(
        "📨 <b>Отправка уведомления</b>\n\nВведите текст сообщения, которое вы хотите отправить всем пользователям:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notifications_menu")]]),
        parse_mode="HTML"
    )
    await state.set_state(NotificationStates.waiting_for_admin_message)

@notifications_router.message(NotificationStates.waiting_for_admin_message)
async def process_admin_notification_text(message: Message, state: FSMContext):
    """Обработка текста уведомления от администратора"""
    notification_text = message.text.strip() if message.text else ""
    if not notification_text:
        await message.answer("❌ Текст уведомления не может быть пустым. Введите текст сообщения:")
        return
    await state.update_data(admin_notification_text=notification_text)
    
    await message.answer(
        f"📋 <b>Предпросмотр уведомления:</b>\n\n{notification_text}\n\nВы уверены, что хотите отправить это сообщение всем пользователям?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить сейчас", callback_data="admin_confirm_send_now")],
            [InlineKeyboardButton(text="📅 Запланировать", callback_data="admin_schedule_notification")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="notifications_menu")]
        ]),
        parse_mode="HTML"
    )

@notifications_router.callback_query(F.data == "admin_confirm_send_now")
async def admin_confirm_send_now(callback: CallbackQuery, state: FSMContext):
    """Подтверждение немедленной отправки уведомления"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    data = await state.get_data()
    text = data.get('admin_notification_text')
    
    if not text:
        await callback.answer("❌ Ошибка: текст сообщения не найден", show_alert=True)
        return
        
    await callback.message.edit_text("⏳ Отправка уведомлений...")
    
    # Определяем, по какой БД рассылать
    count = 0
    try:
        from main import bot
        is_super = env_manager.is_super_admin(callback.from_user.id)
        
        base_dir = os.getcwd()
        data_dir = os.path.join(base_dir, 'data')
        
        logging.info(f"BROADCAST DEBUG: Starting broadcast. Super-admin: {is_super}")

        # Только супер-admin рассылает по ВСЕМ организациям.
        # Обычный org-admin рассылает только по своей БД.
        if is_super:
            db_paths = []
            # 1. shop_bot.db
            shop_bot_path = os.path.join(data_dir, 'shop_bot.db')
            if os.path.exists(shop_bot_path):
                db_paths.append(shop_bot_path)

            # 2. Все .db файлы в data (кроме main.db)
            if os.path.exists(data_dir):
                for f in os.listdir(data_dir):
                    if f.endswith('.db') and f not in ['main.db', 'shop_bot.db']:
                        db_paths.append(os.path.join(data_dir, f))

            # 3. Папка tenants
            tenants_dir = os.path.join(data_dir, 'tenants')
            if os.path.exists(tenants_dir) and os.path.isdir(tenants_dir):
                for f in os.listdir(tenants_dir):
                    if f.endswith('.db'):
                        db_paths.append(os.path.join(tenants_dir, f))

            # 4. Маппинг через tenant_manager
            try:
                for org in tenant_manager.get_all_organizations():
                    org_db = org[2]
                    if org_db:
                        path = org_db if os.path.isabs(org_db) else os.path.join(base_dir, org_db)
                        if os.path.exists(path):
                            db_paths.append(path)
            except Exception as e:
                logging.error(f"BROADCAST ERROR: Failed to read orgs: {e}")

            # Нормализация и дедупликация путей
            db_paths = list(set(os.path.normpath(p) for p in db_paths))
        else:
            # Org-admin: только своя БД (тенант или shop_bot.db)
            org_db = await get_db(callback.from_user.id, state)
            db_paths = [os.path.normpath(org_db.db_file)]
        logging.info(f"BROADCAST DEBUG: DB paths for scan: {db_paths}")

        # Собираем пользователей с включёнными admin-уведомлениями (admin_notifications=1)
        # Структура get_users_for_notifications: (user_id, telegram_id, first_name, shop_name, threshold, notification_time)
        target_by_db: dict = {}
        for path in db_paths:
            try:
                if not os.path.exists(path):
                    continue
                path_db = Database(path)
                recipients = path_db.get_users_for_notifications('admin')
                if recipients:
                    target_by_db[path] = (path_db, [(r[0], r[1]) for r in recipients if r[1]])
            except Exception as e:
                logging.error(f"BROADCAST ERROR: Database {path} error: {e}")

        total_opted_in = sum(len(v[1]) for v in target_by_db.values())
        logging.info(f"BROADCAST DEBUG: Total opted-in users: {total_opted_in}")

        # Отправка с дедупликацией по telegram_id
        seen_tids: set = set()
        for path, (path_db, recipients) in target_by_db.items():
            for uid_internal, tid in recipients:
                try:
                    tid_int = int(tid)
                except (ValueError, TypeError):
                    continue
                if tid_int in seen_tids:
                    continue
                seen_tids.add(tid_int)
                try:
                    await bot.send_message(tid_int, f"🔔 <b>Уведомление от администратора</b>\n\n{text}", parse_mode="HTML", reply_markup=add_read_btn())
                    count += 1
                    path_db.add_notification_to_history(uid_internal, 'admin', text)
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logging.error(f"BROADCAST ERROR: Failed to send to {tid_int}: {e}")
                
    except Exception as e:
        logging.error(f"BROADCAST CRITICAL ERROR: {e}")
    
    final_text = f"✅ Уведомление успешно отправлено {count} пользователям!"
    await callback.message.edit_text(
        final_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notifications_menu")]])
    )
    await clear_state_keep_org(state)

@notifications_router.callback_query(F.data == "admin_schedule_notification")
async def admin_schedule_notification_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса планирования уведомления"""
    await callback.answer()
    await callback.message.edit_text(
        "📅 <b>Планирование уведомления</b>\n\nВведите время отправки в формате ДД.ММ.ГГГГ ЧЧ:ММ (напр. 02.02.2026 09:00):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notifications_menu")]]),
        parse_mode="HTML"
    )
    await state.set_state(NotificationStates.waiting_for_schedule_time)

@notifications_router.message(NotificationStates.waiting_for_schedule_time)
async def process_schedule_time(message: Message, state: FSMContext):
    """Обработка времени планирования"""
    import datetime
    import uuid
    from timezone_utils import get_utc_time, get_current_user_time
    time_text = message.text.strip()
    try:
        schedule_time_naive = datetime.datetime.strptime(time_text, "%d.%m.%Y %H:%M")

        data = await state.get_data()
        text = data.get('admin_notification_text')
        if not text:
            await message.answer("❌ Текст уведомления не найден. Начните заново.")
            await clear_state_keep_org(state)
            return

        current_db = await get_db(message.from_user.id, state)
        admin_user = current_db.get_user(message.from_user.id)
        if not admin_user:
            await message.answer("❌ Профиль администратора не найден.")
            await clear_state_keep_org(state)
            return

        # Интерпретируем введённое время в timezone администратора → конвертируем в UTC
        admin_tz = current_db.get_user_timezone(message.from_user.id)
        schedule_time_utc = get_utc_time(schedule_time_naive, admin_tz)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if schedule_time_utc <= now_utc:
            await message.answer("❌ Время должно быть в будущем!")
            return

        is_super = env_manager.is_super_admin(message.from_user.id)
        recipients_type = 'all' if is_super else 'org'
        job_id = str(uuid.uuid4())
        current_db.add_scheduled_notification(
            job_id=job_id,
            created_by=admin_user[0],
            notification_text=text,
            recipients_type=recipients_type,
            recipients_list=None,
            scheduled_datetime=schedule_time_utc.strftime('%Y-%m-%dT%H:%M:%S')
        )
        await message.answer(
            f"✅ <b>Уведомление запланировано!</b>\n\n📅 Время: {time_text} ({admin_tz})\n💬 Текст: {text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notifications_menu")]]),
            parse_mode="HTML"
        )
        await clear_state_keep_org(state)
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте ДД.ММ.ГГГГ ЧЧ:ММ (напр. 02.02.2026 09:00)")

@notifications_router.callback_query(F.data == "view_scheduled_notifications")
async def view_scheduled_notifications(callback: CallbackQuery, state: FSMContext):
    """Просмотр запланированных уведомлений"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    notifications = current_db.get_scheduled_notifications(status='pending')

    if not notifications:
        await callback.message.edit_text(
            "📅 <b>Запланированные уведомления</b>\n\nНет запланированных уведомлений.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notifications_menu")]]),
            parse_mode="HTML"
        )
        return

    from timezone_utils import format_user_datetime
    admin_tz = current_db.get_user_timezone(callback.from_user.id)

    text = "📅 <b>Запланированные уведомления</b>\n\n"
    buttons = []
    for notif in notifications[:10]:
        notif_id = notif[0]
        notif_text = notif[3] or ""
        scheduled_dt_raw = notif[6] or ""
        scheduled_dt = format_user_datetime(scheduled_dt_raw, admin_tz, '%d.%m.%Y %H:%M') if scheduled_dt_raw else "—"
        creator_name = f"{notif[-2] or ''} {notif[-1] or ''}".strip() or "Администратор"
        preview = notif_text[:50] + ("…" if len(notif_text) > 50 else "")
        text += f"🕐 {scheduled_dt}\n👤 {creator_name}\n💬 {preview}\n\n"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Удалить #{notif_id}",
            callback_data=f"del_sched_notif_{notif_id}"
        )])

    buttons.append([back_button("notifications_menu")])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.startswith("del_sched_notif_"))
async def delete_scheduled_notification(callback: CallbackQuery, state: FSMContext):
    """Удаление запланированного уведомления"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    try:
        notif_id = int(callback.data.replace("del_sched_notif_", ""))
    except ValueError:
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return
    current_db = await get_db(callback.from_user.id, state)
    deleted = current_db.delete_scheduled_notification(notif_id)
    if deleted:
        await callback.answer("✅ Уведомление удалено")
    else:
        await callback.answer("⚠️ Уведомление не найдено")
    await view_scheduled_notifications(callback, state)


@notifications_router.callback_query(F.data == "notification_settings")
async def notification_settings_menu(callback: CallbackQuery, state: FSMContext):
    """Меню настроек уведомлений"""
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    user_id = user[0]
    settings = current_db.get_notification_settings(user_id)
    is_admin = is_any_admin(callback.from_user.id)
    
    text = "⚙️ <b>Настройки уведомлений</b>\n\n"
    text += "Выберите тип уведомлений для настройки:\n\n"
    
    keyboard_buttons = [
        [InlineKeyboardButton(text=f"📦 Низкие остатки {'✅' if settings['low_stock_alerts'] else '❌'}", callback_data="toggle_low_stock")],
        [InlineKeyboardButton(text=f"📊 Ежедневные отчеты {'✅' if settings['daily_reports'] else '❌'}", callback_data="toggle_daily_reports")],
        [InlineKeyboardButton(text=f"💰 Уведомления о продажах {'✅' if settings['sales_alerts'] else '❌'}", callback_data="toggle_sales_alerts")],
    ]
    
    if is_admin:
        keyboard_buttons.extend([
            [InlineKeyboardButton(text=f"💳 Платежные уведомления {'✅' if settings['payment_alerts'] else '❌'}", callback_data="toggle_payment_alerts")],
            [InlineKeyboardButton(text=f"🔧 Админ уведомления {'✅' if settings['admin_notifications'] else '❌'}", callback_data="toggle_admin_notifications")],
        ])
    
    keyboard_buttons.extend([
        [InlineKeyboardButton(text=f"📏 Порог остатков ({settings['stock_threshold']} шт.)", callback_data="set_stock_threshold")],
        [InlineKeyboardButton(text=f"⏰ Время уведомлений ({settings['notification_time']})", callback_data="set_notification_time")],
        [back_button("notifications_menu")]
    ])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@notifications_router.callback_query(F.data == "set_notification_time")
async def set_notification_time_start(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    await callback.answer()
    await state.update_data(user_id=user[0])
    text = "⏰ <b>Установка времени уведомлений</b>\n\nВведите время в формате ЧЧ:ММ (напр. 09:00):"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]]), parse_mode="HTML")
    await state.set_state(NotificationStates.waiting_for_time)

@notifications_router.callback_query(F.data.in_(["toggle_low_stock", "toggle_daily_reports", "toggle_sales_alerts", "toggle_payment_alerts", "toggle_admin_notifications"]))
async def toggle_notification_setting(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    user_id = user[0]
    setting_mapping = {
        'toggle_low_stock': 'low_stock_alerts',
        'toggle_daily_reports': 'daily_reports',
        'toggle_sales_alerts': 'sales_alerts',
        'toggle_payment_alerts': 'payment_alerts',
        'toggle_admin_notifications': 'admin_notifications'
    }
    
    setting_type = setting_mapping.get(callback.data)
    if not setting_type:
        await callback.answer("❌ Неизвестная настройка", show_alert=True)
        return
    
    if setting_type in ['payment_alerts', 'admin_notifications']:
        is_super = env_manager.is_super_admin(callback.from_user.id)
        if not (is_super or is_any_admin(callback.from_user.id)):
            await callback.answer("❌ Только для администраторов!", show_alert=True)
            return
    
    settings = current_db.get_notification_settings(user_id)
    new_value = not settings.get(setting_type, False)
    current_db.update_notification_settings(user_id, **{setting_type: new_value})
    
    await callback.answer(f"✅ Настройка изменена")
    await notification_settings_menu(callback, state)

@notifications_router.callback_query(F.data == "set_stock_threshold")
async def set_stock_threshold_start(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    await callback.answer()
    await state.update_data(user_id=user[0])
    text = "📏 <b>Установка порога остатков</b>\n\nВведите число (напр. 5):"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]]), parse_mode="HTML")
    await state.set_state(NotificationStates.waiting_for_threshold)

@notifications_router.message(NotificationStates.waiting_for_threshold)
async def process_stock_threshold(message: Message, state: FSMContext):
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]])
    try:
        threshold = int(message.text.strip())
        if threshold < 1:
            await message.answer("❌ Должно быть больше 0", reply_markup=_back_kb)
            return
        data = await state.get_data()
        current_db = await get_db(message.from_user.id, state)
        current_db.update_notification_settings(data['user_id'], stock_threshold=threshold)
        await message.answer(f"✅ Порог установлен: {threshold} шт.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", callback_data="notification_settings")]]))
        await clear_state_keep_org(state)
    except ValueError:
        await message.answer("❌ Введите корректное число", reply_markup=_back_kb)

@notifications_router.message(NotificationStates.waiting_for_time)
async def process_notification_time(message: Message, state: FSMContext):
    import re
    time_text = message.text.strip()
    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', time_text):
        await message.answer("❌ Неверный формат. Используйте ЧЧ:ММ (напр. 09:00)", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]]))
        return
    data = await state.get_data()
    try:
        current_db = await get_db(message.from_user.id, state)
        current_db.update_notification_settings(data['user_id'], notification_time=time_text)
        await message.answer(f"✅ Время установлено: {time_text}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", callback_data="notification_settings")]]))
    except Exception as e:
        logging.error(f"process_notification_time: DB error: {e}")
        await message.answer("❌ Ошибка при сохранении времени. Попробуйте позже.")
    await clear_state_keep_org(state)

@notifications_router.callback_query(F.data == "notification_history")
async def notification_history_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_notification_history(callback, state, period_type='week', offset=0)

@notifications_router.callback_query(F.data.startswith("history_period:"))
async def history_period_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split(':')
    period = parts[1]
    offset_str = parts[2]
    await show_notification_history(callback, state, period_type=period, offset=int(offset_str))

async def show_notification_history(callback: CallbackQuery, state: FSMContext, period_type='week', offset=0):
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    user_id = user[0]
    user_timezone = current_db.get_user_timezone(callback.from_user.id)
    history, period_info = current_db.get_notification_history_by_period(user_id, period_type, offset)
    from timezone_utils import format_user_datetime
    
    text = f"📋 <b>История уведомлений</b>\n\n"
    if period_info:
        text += f"📅 Период: {period_info['start_date'].strftime('%d.%m.%Y')} - {period_info['end_date'].strftime('%d.%m.%Y')}\n\n"
    
    if not history:
        text += "❌ Нет уведомлений."
    else:
        for item in history:
            status = "✅" if item[4] else "🔵"
            date_str = format_user_datetime(item[5], user_timezone, '%d.%m.%Y %H:%M')
            text += f"{status} {item[2]}\n📅 {date_str}\n💬 {item[3][:100]}\n\n"
    
    keyboard_buttons = []
    if period_type != 'all':
        nav = [InlineKeyboardButton(text="◀️ Назад", callback_data=f"history_period:{period_type}:{offset + 1}")]
        if offset > 0: nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"history_period:{period_type}:{offset - 1}"))
        keyboard_buttons.append(nav)
    
    periods = []
    if period_type != 'week': periods.append(InlineKeyboardButton(text="📅 Неделя", callback_data="history_period:week:0"))
    if period_type != 'month': periods.append(InlineKeyboardButton(text="📅 Месяц", callback_data="history_period:month:0"))
    if period_type != 'all': periods.append(InlineKeyboardButton(text="📅 Все", callback_data="history_period:all:0"))
    if periods: keyboard_buttons.append(periods)
    
    keyboard_buttons.append([InlineKeyboardButton(text="🗑 Очистить историю", callback_data="cleanup_notifications_menu")])
    keyboard_buttons.append([InlineKeyboardButton(text="✅ Прочитать все", callback_data="mark_all_read")])
    keyboard_buttons.append([back_button("notifications_menu")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="HTML")

@notifications_router.callback_query(F.data == "mark_all_read")
async def mark_all_notifications_read(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if user: current_db.mark_notifications_as_read(user[0])
    await callback.answer("✅ Прочитано")
    await show_notification_history(callback, state, period_type='week', offset=0)

@notifications_router.callback_query(F.data == "cleanup_notifications_menu")
async def cleanup_notifications_menu(callback: CallbackQuery):
    await callback.answer()
    text = "🗑 <b>Очистка истории</b>\n\nВыберите период:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Старше недели", callback_data="cleanup_confirm:week")],
        [InlineKeyboardButton(text="🗑 Старше месяца", callback_data="cleanup_confirm:month")],
        [InlineKeyboardButton(text="🗑 Все", callback_data="cleanup_confirm:all")],
        [back_button("notification_history")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@notifications_router.callback_query(F.data.startswith("cleanup_confirm:"))
async def cleanup_notifications_confirm(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.split(':')
    period = parts[1]
    text = f"🗑 <b>Удалить {period}?</b>"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data=f"cleanup_execute:{period}"), InlineKeyboardButton(text="❌ Нет", callback_data="notification_history")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@notifications_router.callback_query(F.data.startswith("cleanup_execute:"))
async def cleanup_notifications_execute(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(':')
    period = parts[1]
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if user:
        count = current_db.delete_old_notifications(user[0], period)
        await callback.answer(f"✅ Удалено: {count}")
    await show_notification_history(callback, state, period_type='week', offset=0)


@notifications_router.callback_query(F.data == "notif_read")
async def notif_read_handler(callback: CallbackQuery):
    """Кнопка «✅ Прочитано»: удаляет сообщение уведомления из чата.
    Уведомление уже сохранено в notification_history — дополнительных действий не нужно.
    """
    await callback.answer("✅ Прочитано")
    try:
        await callback.message.delete()
    except Exception:
        pass
