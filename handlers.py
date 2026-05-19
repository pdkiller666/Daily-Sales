"""
Обработчики команд и колбэков бота
"""
import os
import sqlite3
import logging
from datetime import datetime, date, timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Database
from tenant_manager import tenant_manager

from keyboards import (
    main_menu, products_menu, inventory_menu, back_button, 
    cancel_registration_keyboard, generate_calendar,
    create_selection_keyboard, create_confirm_keyboard,
    create_registration_selection_keyboard, safe_cb, resolve_cb_name
)

from states import (
    ProductStates, InventoryStates, SaleStates, EditSaleStates,
    ReportStates, UserRegistrationStates, AdminUserStates, UserProfileStates,
    AdminManagementStates
)
from utils import format_date_display, generate_excel_report, format_currency, get_stock_color_indicator, he
from message_utils import safe_edit_message, safe_answer_callback, fsm_edit
from env_manager import env_manager
from hints import maybe_send_welcome, hint_suffix

# Создаем роутер
router = Router()

# Инициализация базы данных
db = Database('data/shop_bot.db')

from db_utils import get_db, clear_state_keep_org, is_any_admin, get_user_org_role, maybe_refresh_username
from timezone_utils import format_user_datetime

# Получаем ID администратора
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)

# ОСНОВНЫЕ КОМАНДЫ

@router.message(F.text == "/menu")
async def cmd_menu(message: Message, state: FSMContext):
    """Команда для принудительного обновления меню"""
    current_db = await get_db(message.from_user.id, state)
    is_super = env_manager.is_super_admin(message.from_user.id)
    
    user = current_db.get_user(message.from_user.id)
    if user:
        await message.answer(
            "🏪 Главное меню:",
            reply_markup=main_menu(message.chat.id, user[8])
        )
    elif is_super:
        await message.answer(
            "🏪 Главное меню (Админ):",
            reply_markup=main_menu(message.chat.id, "Системный")
        )
    else:
        await message.answer("❌ Пользователь не найден. Используйте /start для регистрации.")

@router.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    """Команда старт"""
    is_super = env_manager.is_super_admin(message.from_user.id)
    
    # Сначала ищем пользователя в основной БД (main.db)
    from database import Database as CentralDB
    central_db = CentralDB('data/main.db')
    _found_db = None
    user = central_db.get_user(message.from_user.id)
    if user:
        _found_db = central_db

    # Если в основной нет, ищем в shop_bot.db
    if not user:
        shop_db = Database('data/shop_bot.db')
        user = shop_db.get_user(message.from_user.id)
        if user:
            _found_db = shop_db

    # Если всё еще нет, ищем в тенантах (на всякий случай)
    if not user:
        db_path = tenant_manager.get_user_db_path(message.from_user.id)
        if db_path and db_path != 'data/shop_bot.db':
            tenant_db = Database(db_path)
            user = tenant_db.get_user(message.from_user.id)
            if user:
                _found_db = tenant_db
    
    # Если супер-админ, создаем запись в БД если её нет
    if is_super and not user:
        central_db.create_tables()
        central_db.add_user(
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name or "Admin",
            last_name=message.from_user.last_name or "",
            shop_name=None,
            trade_network=None,
            city=None,
            phone="000",
            username=message.from_user.username
        )
        user = central_db.get_user(message.from_user.id)
    
    if user:
        if _found_db:
            maybe_refresh_username(
                _found_db, message.from_user.id, message.from_user.username,
                stored_username=user[12] if len(user) > 12 else None,
            )
        welcome_text = f"👋 Добро пожаловать, {user[2]} {user[3]}!"
        welcome_text += f"\n🔗 <b>Telegram:</b> <a href='tg://user?id={message.from_user.id}'>Мой профиль</a>"
        
        if is_super:
            welcome_text += "\n\n🔧 Вы вошли как системный администратор."

        try:
            if _found_db:
                _today = date.today().isoformat()
                if is_any_admin(message.from_user.id) or is_super:
                    _s = _found_db.get_sales_summary(start_date=_today, end_date=_today)
                    _cnt, _rev = int(_s[0] or 0), float(_s[2] or 0)
                else:
                    _us = _found_db.get_user_sales_by_date(user[0], _today, _today)
                    _cnt = len(_us)
                    _rev = sum((r[3] or 0) * (r[4] or 0) for r in _us)
                if _cnt > 0:
                    welcome_text += f"\n\n📊 <b>Сегодня:</b> {_cnt} прод. · {format_currency(_rev)}"
        except Exception:
            pass

        await message.answer(
            welcome_text,
            reply_markup=main_menu(message.chat.id, user[8]),
            parse_mode="HTML"
        )
        if _found_db:
            _found_db.create_tables()
            await maybe_send_welcome(
                message, _found_db, user[0],
                is_any_admin(message.from_user.id) or is_super
            )
    else:
        from keyboards import usage_mode_keyboard
        await message.answer(
            "👋 Добро пожаловать в систему управления товарами!\n\n"
            "Как вы планируете использовать систему?",
            reply_markup=usage_mode_keyboard()
        )
        await state.set_state(UserRegistrationStates.waiting_for_usage_mode)

@router.callback_query(F.data == "mode_personal", UserRegistrationStates.waiting_for_usage_mode)
async def process_mode_personal(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(usage_mode="personal", anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "👤 <b>Личное использование</b>\n\n1️⃣ Введите ваше имя:",
        reply_markup=cancel_registration_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(UserRegistrationStates.waiting_for_first_name)

@router.callback_query(F.data == "mode_corporate", UserRegistrationStates.waiting_for_usage_mode)
async def process_mode_corporate(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(usage_mode="corporate", anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🏢 <b>Создание организации</b>\n\nВведите название вашей организации (например, Huawei, Tefal):",
        reply_markup=cancel_registration_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(UserRegistrationStates.waiting_for_org_name)

@router.callback_query(F.data == "mode_join", UserRegistrationStates.waiting_for_usage_mode)
async def process_mode_join(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(usage_mode="join", anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🔗 <b>Вход в организацию</b>\n\nВведите 8-значный код приглашения, полученный от администратора:",
        reply_markup=cancel_registration_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(UserRegistrationStates.waiting_for_invite_code)

@router.message(UserRegistrationStates.waiting_for_invite_code)
async def process_reg_invite_code(message: Message, state: FSMContext):
    invite_code = message.text.strip().upper()
    success, result = tenant_manager.join_organization_by_invite(message.from_user.id, invite_code)
    
    if success:
        await state.update_data(org_name=result, invite_code=invite_code)
        await fsm_edit(
            state, message,
            f"✅ Вы успешно присоединились к организации <b>{he(result)}</b>!\n\n1️⃣ Введите ваше имя:",
            reply_markup=cancel_registration_keyboard(),
        )
        await state.set_state(UserRegistrationStates.waiting_for_first_name)
    elif result == "KICKED":
        await fsm_edit(
            state, message,
            "🚫 <b>Доступ ограничен</b>\n\nВы были исключены из этой организации. "
            "Обратитесь к администратору для восстановления доступа.",
            reply_markup=cancel_registration_keyboard(),
        )
    else:
        await fsm_edit(
            state, message,
            f"❌ Ошибка: {result}. Попробуйте снова или отмените регистрацию:",
            reply_markup=cancel_registration_keyboard(),
        )

@router.message(UserRegistrationStates.waiting_for_org_name)
async def process_reg_org_name(message: Message, state: FSMContext):
    org_name = message.text.strip()
    if len(org_name) < 3:
        await fsm_edit(
            state, message,
            "🏢 <b>Создание организации</b>\n\n❌ Название должно быть не менее 3 символов. Введите снова:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    
    await state.update_data(org_name=org_name)
    await fsm_edit(
        state, message,
        f"✅ Организация: <b>{he(org_name)}</b>\n\n1️⃣ Введите ваше имя:",
        reply_markup=cancel_registration_keyboard(),
    )
    await state.set_state(UserRegistrationStates.waiting_for_first_name)

@router.message(UserRegistrationStates.waiting_for_first_name)
async def process_first_name(message: Message, state: FSMContext):
    first_name = message.text.strip()
    if len(first_name) < 2:
        await fsm_edit(
            state, message,
            "1️⃣ ❌ Имя должно содержать минимум 2 символа. Введите снова:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    
    await state.update_data(first_name=first_name)
    await fsm_edit(
        state, message,
        f"✅ Имя: {first_name}\n\n2️⃣ Введите вашу фамилию:",
        reply_markup=cancel_registration_keyboard(),
    )
    await state.set_state(UserRegistrationStates.waiting_for_last_name)

@router.message(UserRegistrationStates.waiting_for_last_name)
async def process_last_name(message: Message, state: FSMContext):
    last_name = message.text.strip()
    if len(last_name) < 2:
        await fsm_edit(
            state, message,
            "2️⃣ ❌ Фамилия должна содержать минимум 2 символа. Введите снова:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    
    await state.update_data(last_name=last_name)
    await fsm_edit(
        state, message,
        f"✅ Фамилия: {last_name}\n\n3️⃣ Введите ваше отчество (или «нет»):",
        reply_markup=cancel_registration_keyboard(),
    )
    await state.set_state(UserRegistrationStates.waiting_for_middle_name)

@router.message(UserRegistrationStates.waiting_for_middle_name)
async def process_middle_name(message: Message, state: FSMContext):
    middle_name = message.text.strip()
    if middle_name.lower() in ['нет', 'no', '-']:
        middle_name = None
    
    await state.update_data(middle_name=middle_name)
    await fsm_edit(
        state, message,
        f"✅ Отчество: {middle_name or 'не указано'}\n\n📞 Введите ваш номер телефона:",
        reply_markup=cancel_registration_keyboard(),
    )
    await state.set_state(UserRegistrationStates.waiting_for_phone)

@router.message(UserRegistrationStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if len(phone) < 10:
        await fsm_edit(
            state, message,
            "📞 ❌ Номер телефона должен содержать минимум 10 цифр. Введите снова:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    
    await state.update_data(phone=phone)
    await fsm_edit(
        state, message,
        f"✅ Телефон: {phone}\n\n📧 Введите ваш email (или «нет»):",
        reply_markup=cancel_registration_keyboard(),
    )
    await state.set_state(UserRegistrationStates.waiting_for_email)

@router.message(UserRegistrationStates.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if email.lower() in ['нет', 'no', '-']:
        email = None
    
    await state.update_data(email=email)
    existing_networks = db.get_all_trade_networks()
    
    if existing_networks:
        keyboard = create_registration_selection_keyboard(
            existing_networks, "select_network", "create_new_network"
        )
        await fsm_edit(
            state, message,
            f"✅ Email: {email or 'не указан'}\n\n🏢 Выберите торговую сеть или создайте новую:",
            reply_markup=keyboard,
        )
    else:
        await fsm_edit(
            state, message,
            f"✅ Email: {email or 'не указан'}\n\n🏢 Введите название торговой сети:",
            reply_markup=cancel_registration_keyboard(),
        )
    
    await state.set_state(UserRegistrationStates.waiting_for_trade_network)

@router.callback_query(lambda c: c.data.startswith("select_network_"))
async def select_trade_network(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    network_name = callback.data.replace("select_network_", "")
    await state.update_data(trade_network=network_name)
    existing_shops = db.get_all_shops()
    
    if existing_shops:
        keyboard = create_registration_selection_keyboard(
            existing_shops, "select_shop", "create_new_shop"
        )
        await callback.message.edit_text(
            f"✅ Торговая сеть: {network_name}\n\n🏪 Выберите магазин или создайте новый:",
            reply_markup=keyboard
        )
    else:
        await callback.message.edit_text(
            f"✅ Торговая сеть: {network_name}\n\n🏪 Введите название магазина:",
            reply_markup=cancel_registration_keyboard()
        )
    
    await state.set_state(UserRegistrationStates.waiting_for_shop_name)

@router.callback_query(lambda c: c.data == "create_new_network")
async def create_new_network(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "🏢 Введите название новой торговой сети:",
        reply_markup=cancel_registration_keyboard()
    )
    await state.set_state(UserRegistrationStates.waiting_for_trade_network)

@router.message(UserRegistrationStates.waiting_for_trade_network)
async def process_trade_network(message: Message, state: FSMContext):
    trade_network = message.text.strip()
    if len(trade_network) < 2:
        await fsm_edit(
            state, message,
            "🏢 ❌ Название торговой сети должно содержать минимум 2 символа. Введите снова:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    if len(trade_network) > 30:
        await fsm_edit(
            state, message,
            "🏢 ⚠️ Название торговой сети не должно превышать 30 символов. Введите короче:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    
    await state.update_data(trade_network=trade_network)
    existing_shops = db.get_all_shops()
    
    if existing_shops:
        keyboard = create_registration_selection_keyboard(
            existing_shops, "select_shop", "create_new_shop"
        )
        await fsm_edit(
            state, message,
            f"✅ Торговая сеть: {trade_network}\n\n🏪 Выберите магазин или создайте новый:",
            reply_markup=keyboard,
        )
    else:
        await fsm_edit(
            state, message,
            f"✅ Торговая сеть: {trade_network}\n\n🏪 Введите название магазина:",
            reply_markup=cancel_registration_keyboard(),
        )
    
    await state.set_state(UserRegistrationStates.waiting_for_shop_name)

@router.callback_query(lambda c: c.data.startswith("select_shop_"))
async def select_shop(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    shop_name = callback.data.replace("select_shop_", "")
    await state.update_data(shop_name=shop_name)
    existing_cities = db.get_all_cities()
    
    if existing_cities:
        keyboard = create_registration_selection_keyboard(
            existing_cities, "select_city", "create_new_city"
        )
        await callback.message.edit_text(
            f"✅ Магазин: {shop_name}\n\n🏙️ Выберите город или создайте новый:",
            reply_markup=keyboard
        )
    else:
        await callback.message.edit_text(
            f"✅ Магазин: {shop_name}\n\n🏙️ Введите название города:",
            reply_markup=cancel_registration_keyboard()
        )
    
    await state.set_state(UserRegistrationStates.waiting_for_city)

@router.callback_query(lambda c: c.data == "create_new_shop")
async def create_new_shop(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "🏪 Введите название нового магазина:",
        reply_markup=cancel_registration_keyboard()
    )
    await state.set_state(UserRegistrationStates.waiting_for_shop_name)

@router.message(UserRegistrationStates.waiting_for_shop_name)
async def process_shop_name(message: Message, state: FSMContext):
    shop_name = message.text.strip()
    if len(shop_name) < 2:
        await fsm_edit(
            state, message,
            "🏪 ❌ Название магазина должно содержать минимум 2 символа. Введите снова:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    if len(shop_name) > 30:
        await fsm_edit(
            state, message,
            "🏪 ⚠️ Название магазина не должно превышать 30 символов. Введите короче:",
            reply_markup=cancel_registration_keyboard(),
        )
        return

    await state.update_data(shop_name=shop_name)
    existing_cities = db.get_all_cities()
    
    if existing_cities:
        keyboard = create_registration_selection_keyboard(
            existing_cities, "select_city", "create_new_city"
        )
        await fsm_edit(
            state, message,
            f"✅ Магазин: {shop_name}\n\n🏙️ Выберите город или создайте новый:",
            reply_markup=keyboard,
        )
    else:
        await fsm_edit(
            state, message,
            f"✅ Магазин: {shop_name}\n\n🏙️ Введите название города:",
            reply_markup=cancel_registration_keyboard(),
        )
    
    await state.set_state(UserRegistrationStates.waiting_for_city)

@router.callback_query(lambda c: c.data.startswith("select_city_"))
async def select_city(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    city_name = callback.data.replace("select_city_", "")
    user_data = await state.get_data()

    from database import Database as CentralDB
    central_db = CentralDB('data/main.db')
    central_db.create_tables()
    central_db.add_user(
        telegram_id=callback.from_user.id,
        first_name=user_data['first_name'],
        last_name=user_data['last_name'],
        middle_name=user_data.get('middle_name'),
        phone=user_data['phone'],
        email=user_data.get('email'),
        trade_network=user_data['trade_network'],
        shop_name=user_data['shop_name'],
        city=city_name,
        username=callback.from_user.username
    )

    if user_data.get('usage_mode') == 'corporate':
        org_name = user_data.get('org_name')
        success, result = tenant_manager.create_organization(org_name, callback.from_user.id)
        if success:
            env_manager.add_admin_id(callback.from_user.id)
            tenant_db = await get_db(callback.from_user.id, state)
            tenant_db.create_tables()
            tenant_db.add_user(
                telegram_id=callback.from_user.id,
                first_name=user_data['first_name'],
                last_name=user_data['last_name'],
                middle_name=user_data.get('middle_name'),
                phone=user_data['phone'],
                email=user_data.get('email'),
                trade_network=user_data['trade_network'],
                shop_name=user_data['shop_name'],
                city=city_name,
                username=callback.from_user.username
            )
    elif user_data.get('usage_mode') == 'personal':
        env_manager.add_admin_id(callback.from_user.id)
    elif user_data.get('usage_mode') == 'join':
        tenant_db = await get_db(callback.from_user.id, state)
        tenant_db.create_tables()
        tenant_db.add_user(
            telegram_id=callback.from_user.id,
            first_name=user_data['first_name'],
            last_name=user_data['last_name'],
            middle_name=user_data.get('middle_name'),
            phone=user_data['phone'],
            email=user_data.get('email'),
            trade_network=user_data['trade_network'],
            shop_name=user_data['shop_name'],
            city=city_name,
            username=callback.from_user.username
        )

    welcome_text = (
        f"✅ Регистрация завершена!\n\n"
        f"👤 {he(user_data['first_name'])} {he(user_data['last_name'])}\n"
        f"🏢 Торговая сеть: {he(user_data['trade_network'])}\n"
        f"🏪 Магазин: {he(user_data['shop_name'])}\n"
        f"🏙️ Город: {he(city_name)}\n"
        f"📞 Телефон: {he(user_data['phone'])}\n"
    )

    if user_data.get('usage_mode') == 'corporate':
        welcome_text += f"🏢 Организация: <b>{he(user_data.get('org_name'))}</b>\n"

    _inv_db = await get_db(callback.from_user.id, state)
    _inv_user = _inv_db.get_user(callback.from_user.id)

    await callback.message.edit_text(
        welcome_text,
        reply_markup=main_menu(callback.from_user.id, user_data['shop_name']),
        parse_mode="HTML"
    )
    await clear_state_keep_org(state)
    if _inv_user:
        await maybe_send_welcome(
            callback, _inv_db, _inv_user[0],
            is_any_admin(callback.from_user.id)
        )

@router.callback_query(lambda c: c.data == "create_new_city")
async def create_new_city(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "🏙️ Введите название нового города:",
        reply_markup=cancel_registration_keyboard()
    )
    await state.set_state(UserRegistrationStates.waiting_for_city)

@router.message(UserRegistrationStates.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    city = message.text.strip()
    if len(city) < 2:
        await fsm_edit(
            state, message,
            "🏙️ ❌ Название города должно содержать минимум 2 символа. Введите снова:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    if len(city) > 30:
        await fsm_edit(
            state, message,
            "🏙️ ⚠️ Название города не должно превышать 30 символов. Введите короче:",
            reply_markup=cancel_registration_keyboard(),
        )
        return
    
    user_data = await state.get_data()
    try:
        from database import Database as CentralDB
        central_db = CentralDB('data/main.db')
        central_db.create_tables()

        central_db.add_user(
            telegram_id=message.from_user.id,
            first_name=user_data['first_name'],
            last_name=user_data['last_name'],
            middle_name=user_data.get('middle_name'),
            phone=user_data['phone'],
            email=user_data.get('email'),
            trade_network=user_data['trade_network'],
            shop_name=user_data['shop_name'],
            city=city,
            username=message.from_user.username
        )

        if user_data.get('usage_mode') == 'corporate':
            org_name = user_data.get('org_name')
            success, result = tenant_manager.create_organization(org_name, message.from_user.id)
            if success:
                # Организатор корпоративного режима автоматически становится админом
                env_manager.add_admin_id(message.from_user.id)

                tenant_db = await get_db(message.from_user.id, state)
                tenant_db.create_tables()
                tenant_db.add_user(
                    telegram_id=message.from_user.id,
                    first_name=user_data['first_name'],
                    last_name=user_data['last_name'],
                    middle_name=user_data.get('middle_name'),
                    phone=user_data['phone'],
                    email=user_data.get('email'),
                    trade_network=user_data['trade_network'],
                    shop_name=user_data['shop_name'],
                    city=city,
                    username=message.from_user.username
                )
        elif user_data.get('usage_mode') == 'personal':
            # Пользователи в личном режиме автоматически становятся админами
            env_manager.add_admin_id(message.from_user.id)
        elif user_data.get('usage_mode') == 'join':
            tenant_db = await get_db(message.from_user.id, state)
            tenant_db.create_tables()
            tenant_db.add_user(
                telegram_id=message.from_user.id,
                first_name=user_data['first_name'],
                last_name=user_data['last_name'],
                middle_name=user_data.get('middle_name'),
                phone=user_data['phone'],
                email=user_data.get('email'),
                trade_network=user_data['trade_network'],
                shop_name=user_data['shop_name'],
                city=city,
                username=message.from_user.username
            )
    except Exception as e:
        import logging
        logging.error(f"process_city: registration DB error: {e}")
        await fsm_edit(
            state, message,
            "❌ Ошибка при сохранении данных. Попробуйте снова или обратитесь в поддержку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Начать заново", callback_data="start")]
            ]),
        )
        await clear_state_keep_org(state)
        return

    # Пробный период — для личного и корпоративного режимов
    if user_data.get('usage_mode') in ('personal', 'corporate'):
        try:
            from database import Database as ShopDB
            shop_db = ShopDB('data/shop_bot.db')
            shop_db.create_tables()
            existing_shop_user = shop_db.get_user(message.from_user.id)
            if not existing_shop_user:
                shop_db.add_user(
                    telegram_id=message.from_user.id,
                    first_name=user_data['first_name'],
                    last_name=user_data['last_name'],
                    middle_name=user_data.get('middle_name'),
                    phone=user_data['phone'],
                    email=user_data.get('email'),
                    trade_network=user_data['trade_network'],
                    shop_name=user_data['shop_name'],
                    city=city,
                    username=message.from_user.username
                )
                existing_shop_user = shop_db.get_user(message.from_user.id)
            if existing_shop_user:
                shop_user_id = existing_shop_user[0]
                existing_sub = shop_db.get_user_subscription(shop_user_id)
                if not existing_sub:
                    trial_settings = shop_db.get_payment_settings()
                    trial_days = int(trial_settings.get('trial_days', '14'))
                    trial_plan = trial_settings.get('trial_plan', 'Премиум')
                    if trial_days > 0:
                        shop_db.create_trial_subscription(shop_user_id, trial_plan, trial_days)
        except Exception as _trial_err:
            import logging as _log
            _log.error(f"process_city: trial grant error: {_trial_err}")

    trial_days_info = ""
    try:
        from database import Database as _ShopDB2
        _sdb = _ShopDB2('data/shop_bot.db')
        _ts = _sdb.get_payment_settings()
        _td = int(_ts.get('trial_days', '14'))
        _tp = _ts.get('trial_plan', 'Премиум')
        if _td > 0 and user_data.get('usage_mode') in ('personal', 'corporate'):
            trial_days_info = f"\n🎁 Пробный период: <b>{_td} дн.</b> тариф <b>{_tp}</b> активирован!"
    except Exception:
        pass

    welcome_text = (
        f"✅ Регистрация завершена!\n\n"
        f"👤 {user_data['first_name']} {user_data['last_name']}\n"
        f"🏢 Торговая сеть: {user_data['trade_network']}\n"
        f"🏪 Магазин: {user_data['shop_name']}\n"
        f"🏙️ Город: {city}\n"
        f"📞 Телефон: {user_data['phone']}\n"
    )

    if user_data.get('usage_mode') == 'corporate':
        welcome_text += f"🏢 Организация: <b>{he(user_data['org_name'])}</b>\n"

    welcome_text += trial_days_info

    _reg_db = await get_db(message.from_user.id, state)
    _reg_user = _reg_db.get_user(message.from_user.id)

    await fsm_edit(
        state, message,
        welcome_text,
        reply_markup=main_menu(message.from_user.id, user_data['shop_name']),
    )
    await clear_state_keep_org(state)
    if _reg_user:
        await maybe_send_welcome(
            message, _reg_db, _reg_user[0],
            is_any_admin(message.from_user.id)
        )

@router.callback_query(F.data.startswith("hint_dismiss:"))
async def hint_dismiss_handler(callback: CallbackQuery):
    """Кнопка «✅ Понятно!» на онбординг-попапах — удаляет сообщение."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    
    # Очищаем состояния при возврате в меню, сохраняя выбранную организацию
    await clear_state_keep_org(state)
    
    if user:
        await callback.message.edit_text(
            "🏪 Главное меню:",
            reply_markup=main_menu(callback.from_user.id, user[8])
        )
    else:
        # Для супер-админов без регистрации
        if env_manager.is_super_admin(callback.from_user.id):
            await callback.message.edit_text(
                "🏪 Главное меню (Админ):",
                reply_markup=main_menu(callback.from_user.id, "Системный")
            )
        else:
            # Сначала проверяем основную БД
            from database import Database as CentralDB
            central_db = CentralDB('data/main.db')
            user_central = central_db.get_user(callback.from_user.id)
            
            if user_central:
                await callback.message.edit_text(
                    "🏪 Главное меню:",
                    reply_markup=main_menu(callback.from_user.id, user_central[8])
                )
            else:
                await callback.message.edit_text(
                    "❌ Пользователь не найден. Используйте /start",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 /start", callback_data="start")]])
                )

@router.callback_query(F.data == "user_profile")
async def user_profile_menu(callback: CallbackQuery, state: FSMContext):
    """Меню профиля пользователя"""
    await callback.answer()
    # Сначала пробуем получить из основной БД
    from database import Database as CentralDB
    central_db = CentralDB('data/main.db')
    user = central_db.get_user(callback.from_user.id)
    
    # Если нет в основной, пробуем в текущей (тенанте)
    if not user:
        current_db = await get_db(callback.from_user.id, state)
        user = current_db.get_user(callback.from_user.id)
    
    if not user:
        await callback.message.edit_text(
            "❌ Профиль не найден. Используйте /start",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 /start", callback_data="start")]])
        )
        return
        
    u_id, t_id, f_name, l_name, m_name, phone, email, network, s_name, city, tz, created, *_rest = user

    raw_role = get_user_org_role(callback.from_user.id)
    if env_manager.is_super_admin(callback.from_user.id):
        role_line = '👑 Супер Администратор'
    elif raw_role:
        from db_utils import get_user_org_scope, get_role_display_label, get_user_custom_title
        _scope_t, _scope_v = get_user_org_scope(callback.from_user.id)
        _ctitle = get_user_custom_title(callback.from_user.id)
        role_line = get_role_display_label(raw_role, _scope_t, _scope_v, _ctitle)
    else:
        role_line = '👤 Личный режим'

    reg_date = format_user_datetime(created, tz or 'Europe/Moscow', '%d.%m.%Y')

    text = (
        f"👤 <b>Мой профиль</b>\n"
        f"<i>{role_line}</i>\n\n"
        f"<b>Имя:</b> {he(f_name) or 'не указано'}\n"
        f"<b>Фамилия:</b> {he(l_name) or 'не указано'}\n"
        f"<b>Отчество:</b> {he(m_name) or 'не указано'}\n"
        f"<b>Телефон:</b> {he(phone) or 'не указан'}\n"
        f"<b>Email:</b> {he(email) or 'не указан'}\n"
        f"<b>Торговая сеть:</b> {he(network) or 'не указана'}\n"
        f"<b>Магазин:</b> {he(s_name) or 'не указан'}\n"
        f"<b>Город:</b> {he(city) or 'не указан'}\n"
        f"<b>Часовой пояс:</b> {tz or 'UTC'}\n\n"
        f"📅 Регистрация: {reg_date}"
    )
    
    # Создаем клавиатуру профиля
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✏️ Редактировать профиль", callback_data="edit_profile"))
    builder.add(InlineKeyboardButton(text="🔔 Уведомления", callback_data="notifications_menu"))
    
    # Кнопка подписки и контактов доступны всем пользователям
    builder.add(InlineKeyboardButton(text="💳 Подписка", callback_data="subscription_menu"))
    builder.add(InlineKeyboardButton(text="📞 Контакты", callback_data="view_contacts"))
    
    builder.add(back_button("main_menu"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "edit_profile")
async def edit_profile_menu(callback: CallbackQuery, state: FSMContext):
    """Меню выбора поля для редактирования.
    Разграничение прав: магазин и торговая сеть — только для администраторов."""
    await callback.answer()
    user_is_admin = is_any_admin(callback.from_user.id)

    buttons = [
        [InlineKeyboardButton(text="👤 Имя", callback_data="prof_edit_first_name"),
         InlineKeyboardButton(text="👤 Фамилия", callback_data="prof_edit_last_name")],
        [InlineKeyboardButton(text="📞 Телефон", callback_data="prof_edit_phone"),
         InlineKeyboardButton(text="📧 Email", callback_data="prof_edit_email")],
        [InlineKeyboardButton(text="🏙️ Город", callback_data="prof_edit_city"),
         InlineKeyboardButton(text="🕐 Пояс", callback_data="prof_edit_timezone")],
    ]
    if user_is_admin:
        # Магазин и торговая сеть — только администраторам
        buttons.insert(2, [
            InlineKeyboardButton(text="🏢 Торг. сеть", callback_data="prof_edit_network"),
            InlineKeyboardButton(text="🏪 Магазин", callback_data="prof_edit_shop"),
        ])
    buttons.append([back_button("user_profile")])

    hint = "" if user_is_admin else "\n\n💡 Магазин и торговую сеть изменяет администратор."
    await callback.message.edit_text(
        f"✏️ <b>Редактирование профиля</b>\n\nВыберите поле, которое хотите изменить:{hint}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("prof_edit_"))
async def start_profile_edit(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования поля. Магазин/сеть — из выпадающего списка, только для admins."""
    field = callback.data.replace("prof_edit_", "")

    # Блокируем доступ обычных сотрудников к магазину и торговой сети
    if field in ("shop", "network") and not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Это поле изменяет только администратор.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)

    if field == "timezone":
        from keyboards import timezone_keyboard
        await callback.message.edit_text("🕐 Выберите ваш часовой пояс:", reply_markup=timezone_keyboard())
        await state.set_state(UserProfileStates.choosing_timezone)
        return

    # Магазин — выбор из существующих в БД + возможность ввести новый
    if field == "shop":
        current_db = await get_db(callback.from_user.id, state)
        shops = current_db.get_all_shops()
        back_kb = [[back_button("edit_profile")]]
        if shops:
            shop_buttons = [
                [InlineKeyboardButton(text=f"🏪 {s}", callback_data=safe_cb("prof_shop_pick_", s))]
                for s in shops
            ]
            shop_buttons.append([InlineKeyboardButton(text="➕ Ввести новое название", callback_data="prof_shop_new")])
            shop_buttons += back_kb
            await callback.message.edit_text(
                "🏪 <b>Выберите магазин</b> из уже существующих в организации\nили введите новый:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=shop_buttons),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                "🏪 В базе нет магазинов. Введите название нового магазина:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=back_kb),
            )
            await state.set_state(UserProfileStates.editing_shop)
        return

    # Торговая сеть — выбор из существующих + ввести новую
    if field == "network":
        current_db = await get_db(callback.from_user.id, state)
        networks = current_db.get_all_trade_networks()
        back_kb = [[back_button("edit_profile")]]
        if networks:
            net_buttons = [
                [InlineKeyboardButton(text=f"🏢 {n}", callback_data=safe_cb("prof_net_pick_", n))]
                for n in networks
            ]
            net_buttons.append([InlineKeyboardButton(text="➕ Ввести новое название", callback_data="prof_net_new")])
            net_buttons += back_kb
            await callback.message.edit_text(
                "🏢 <b>Выберите торговую сеть</b> из уже существующих в организации\nили введите новую:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=net_buttons),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                "🏢 В базе нет торговых сетей. Введите название новой торговой сети:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=back_kb),
            )
            await state.set_state(UserProfileStates.editing_network)
        return

    field_names = {
        "first_name": "имя", "last_name": "фамилию", "phone": "телефон",
        "email": "email", "city": "город"
    }
    state_map = {
        "first_name": UserProfileStates.editing_first_name,
        "last_name": UserProfileStates.editing_last_name,
        "phone": UserProfileStates.editing_phone,
        "email": UserProfileStates.editing_email,
        "city": UserProfileStates.editing_city,
    }
    await callback.message.edit_text(
        f"✏️ Введите новое значение для поля: <b>{field_names.get(field, field)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_profile")]]),
        parse_mode="HTML"
    )
    await state.set_state(state_map.get(field))


# ── Выбор магазина из списка (собственный профиль, admin) ─────────────────────

async def _apply_user_field(telegram_id: int, db_file: str, field: str, value: str) -> None:
    """Обновляет поле пользователя в org/personal БД."""
    conn = sqlite3.connect(db_file)
    conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, telegram_id))
    conn.commit()
    conn.close()

@router.callback_query(F.data.startswith("prof_shop_pick_"))
async def prof_shop_pick(callback: CallbackQuery, state: FSMContext):
    """Администратор выбрал существующий магазин из списка для своего профиля."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    shop_raw = callback.data.removeprefix("prof_shop_pick_")
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, current_db.get_all_shops() or [])
    await callback.answer()
    try:
        await _apply_user_field(callback.from_user.id, current_db.db_file, "shop_name", shop_name)
        await callback.message.edit_text(
            f"✅ Магазин изменён на: <b>{he(shop_name)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("user_profile")]]),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_profile")]]))
    await clear_state_keep_org(state)

@router.callback_query(F.data == "prof_shop_new")
async def prof_shop_new(callback: CallbackQuery, state: FSMContext):
    """Администратор хочет ввести новое название магазина вручную."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🏪 Введите название нового магазина (2–30 символов):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_profile")]]),
    )
    await state.set_state(UserProfileStates.editing_shop)

@router.callback_query(F.data.startswith("prof_net_pick_"))
async def prof_net_pick(callback: CallbackQuery, state: FSMContext):
    """Администратор выбрал существующую торговую сеть из списка для своего профиля."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    net_raw = callback.data.removeprefix("prof_net_pick_")
    current_db = await get_db(callback.from_user.id, state)
    net_name = resolve_cb_name(net_raw, current_db.get_all_trade_networks() or [])
    await callback.answer()
    try:
        await _apply_user_field(callback.from_user.id, current_db.db_file, "trade_network", net_name)
        await callback.message.edit_text(
            f"✅ Торговая сеть изменена на: <b>{he(net_name)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("user_profile")]]),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_profile")]]))
    await clear_state_keep_org(state)

@router.callback_query(F.data == "prof_net_new")
async def prof_net_new(callback: CallbackQuery, state: FSMContext):
    """Администратор хочет ввести новое название торговой сети вручную."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🏢 Введите название новой торговой сети (2–30 символов):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_profile")]]),
    )
    await state.set_state(UserProfileStates.editing_network)

@router.message(UserProfileStates.editing_first_name)
@router.message(UserProfileStates.editing_last_name)
@router.message(UserProfileStates.editing_phone)
@router.message(UserProfileStates.editing_email)
@router.message(UserProfileStates.editing_network)
@router.message(UserProfileStates.editing_shop)
@router.message(UserProfileStates.editing_city)
async def process_profile_edit(message: Message, state: FSMContext):
    current_state = await state.get_state()
    field_map = {
        UserProfileStates.editing_first_name.state: "first_name",
        UserProfileStates.editing_last_name.state: "last_name",
        UserProfileStates.editing_phone.state: "phone",
        UserProfileStates.editing_email.state: "email",
        UserProfileStates.editing_network.state: "trade_network",
        UserProfileStates.editing_shop.state: "shop_name",
        UserProfileStates.editing_city.state: "city"
    }
    
    field = field_map.get(current_state)
    if not field:
        return
    new_value = message.text.strip()

    _MAX_LENGTHS = {
        "trade_network": 30,
        "shop_name": 30,
        "city": 30,
        "first_name": 50,
        "last_name": 50,
        "phone": 20,
        "email": 100,
    }
    _MIN_LENGTHS = {
        "trade_network": 2,
        "shop_name": 2,
        "city": 2,
        "first_name": 2,
    }
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_profile")]])
    if field in _MIN_LENGTHS and len(new_value) < _MIN_LENGTHS[field]:
        await fsm_edit(state, message,
                       f"❌ Значение должно содержать минимум {_MIN_LENGTHS[field]} символа. Введите снова:",
                       reply_markup=_back_kb)
        return
    if field in _MAX_LENGTHS and len(new_value) > _MAX_LENGTHS[field]:
        await fsm_edit(state, message,
                       f"⚠️ Значение не должно превышать {_MAX_LENGTHS[field]} символов. Введите короче:",
                       reply_markup=_back_kb)
        return

    current_db = await get_db(message.from_user.id, state)
    try:
        conn = sqlite3.connect(current_db.db_file)
        cursor = conn.cursor()
        cursor.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (new_value, message.from_user.id))
        conn.commit()
        conn.close()
        
        # Также обновляем в main.db для синхронизации
        main_conn = sqlite3.connect('data/main.db')
        main_cursor = main_conn.cursor()
        main_cursor.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (new_value, message.from_user.id))
        main_conn.commit()
        main_conn.close()
        
        await fsm_edit(state, message, "✅ Данные обновлены!",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("user_profile")]]))
        await clear_state_keep_org(state)
    except Exception as e:
        await fsm_edit(state, message, f"❌ Ошибка при обновлении: {e}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_profile")]]))

@router.callback_query(F.data.startswith("set_timezone_"), UserProfileStates.choosing_timezone)
async def process_profile_timezone(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    new_tz = callback.data.replace("set_timezone_", "")
    current_db = await get_db(callback.from_user.id, state)
    
    try:
        conn = sqlite3.connect(current_db.db_file)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET timezone = ? WHERE telegram_id = ?", (new_tz, callback.from_user.id))
        conn.commit()
        conn.close()
        
        main_conn = sqlite3.connect('data/main.db')
        main_cursor = main_conn.cursor()
        main_cursor.execute("UPDATE users SET timezone = ? WHERE telegram_id = ?", (new_tz, callback.from_user.id))
        main_conn.commit()
        main_conn.close()
        
        await callback.message.edit_text(f"✅ Часовой пояс изменен на {new_tz}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("user_profile")]]))
        await clear_state_keep_org(state)
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка при изменении часового пояса: {e}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("user_profile")]])
        )

@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery, state: FSMContext):
    """Справка с учётом роли пользователя"""
    await callback.answer()
    user_id = callback.from_user.id
    is_super_admin = env_manager.is_super_admin(user_id)
    is_admin = is_any_admin(user_id)
    
    # Определяем режим через state или tenant_manager
    data = await state.get_data()
    selected_org_db = data.get("selected_org_db")
    
    if selected_org_db:
        is_personal_mode = (selected_org_db == "data/shop_bot.db")
    else:
        # Fallback на tenant_manager
        user_db = tenant_manager.get_user_db_path(user_id)
        is_personal_mode = (user_db == "data/shop_bot.db")
    
    if is_super_admin:
        help_text = (
            "🛡️ <b>Справка для Супер-администратора</b>\n\n"
            "📍 <b>Режимы работы:</b>\n"
            "• <b>Системная панель</b> — управление всеми организациями и пользователями\n"
            "• <b>Личный кабинет</b> — работа как обычный продавец\n"
            "• <b>Организация</b> — управление конкретной организацией\n\n"
            "⚙️ <b>Системное управление:</b>\n"
            "• Создание и удаление организаций\n"
            "• Назначение администраторов\n"
            "• Просмотр всех пользователей\n"
            "• Управление платёжной системой\n"
            "• Резервное копирование\n\n"
            "💰 <b>Продажи:</b> Оформление продаж в выбранном режиме\n"
            "📊 <b>Отчёты:</b> Статистика по всем организациям\n"
            "📦 <b>Товары:</b> Управление каталогом\n"
            "🎯 <b>Мотивация:</b> Настройка комиссий продавцов"
        )
    elif is_admin:
        if is_personal_mode:
            help_text = (
                "👤 <b>Справка — Личный режим</b>\n\n"
                "В личном режиме вы работаете как индивидуальный продавец.\n\n"
                "💰 <b>Продажи:</b> Оформляйте свои продажи\n"
                "📊 <b>Отчёты:</b> Просмотр вашей статистики\n"
                "📦 <b>Товары:</b> Управление вашим каталогом\n"
                "🎯 <b>Мотивация:</b> Настройка заработка\n"
                "👤 <b>Профиль:</b> Ваши данные и настройки\n\n"
                "💡 <i>Все данные изолированы от корпоративных организаций.</i>"
            )
        else:
            help_text = (
                "⚙️ <b>Справка для Администратора</b>\n\n"
                "📍 <b>Ваши возможности:</b>\n"
                "• Управление сотрудниками организации\n"
                "• Просмотр отчётов по магазинам\n"
                "• Управление каталогом товаров\n"
                "• Настройка мотивации сотрудников\n"
                "• Управление запасами\n\n"
                "💰 <b>Продажи:</b> Оформление и редактирование\n"
                "📊 <b>Отчёты:</b> Статистика по организации\n"
                "👥 <b>Сотрудники:</b> Управление персоналом"
            )
    else:
        help_text = (
            "👋 <b>Справка для Продавца</b>\n\n"
            "💰 <b>Продажи:</b> Нажмите 'ПРОДАЖА' для оформления\n"
            "📊 <b>Отчёты:</b> Просмотр вашей статистики\n"
            "💵 <b>Заработок:</b> Ваши комиссионные\n"
            "👤 <b>Профиль:</b> Ваши данные и настройки\n\n"
            "💡 <i>При возникновении вопросов обратитесь к администратору через раздел 'Контакты'.</i>"
        )
    
    await callback.message.edit_text(
        help_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]]),
        parse_mode="HTML"
    )

@router.callback_query(lambda c: c.data == "cancel_registration")
async def cancel_registration_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("❌ Регистрация отменена. Используйте /start чтобы начать заново.")
    await clear_state_keep_org(state)
