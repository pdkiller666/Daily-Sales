"""
Обработчики команд и колбэков бота
"""
import asyncio
import os
import sqlite3
import logging
from datetime import datetime, date, timedelta
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Database
from tenant_manager import tenant_manager

from keyboards import (
    main_menu, products_menu, back_button, 
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

from db_utils import get_db, clear_state_keep_org, is_any_admin, get_user_org_role, maybe_refresh_username, wrap_db

# Инициализация базы данных (wrap_db — обязателен для async await вызовов)
db = wrap_db(Database('data/shop_bot.db'))
from timezone_utils import format_user_datetime

# Получаем ID администратора
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)

# ОСНОВНЫЕ КОМАНДЫ

@router.message(F.text == "/menu")
async def cmd_menu(message: Message, state: FSMContext):
    """Команда для принудительного обновления меню"""
    current_db = await get_db(message.from_user.id, state)
    is_super = env_manager.is_super_admin(message.from_user.id)
    
    user = await current_db.get_user(message.from_user.id)
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

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда старт (поддерживает deep-link: /start INVITE_CODE)"""
    # Извлекаем payload из deep-link (/start CODE)
    parts = (message.text or '').strip().split(maxsplit=1)
    invite_arg = parts[1].strip().upper() if len(parts) > 1 else None

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
        shop_db = wrap_db(Database('data/shop_bot.db'))
        user = await shop_db.get_user(message.from_user.id)
        if user:
            _found_db = shop_db

    # Если всё еще нет, ищем в тенантах (на всякий случай)
    if not user:
        db_path = tenant_manager.get_user_db_path(message.from_user.id)
        if db_path and db_path != 'data/shop_bot.db':
            tenant_db = wrap_db(Database(db_path))
            user = await tenant_db.get_user(message.from_user.id)
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
        # A) Deep-link: /start INVITE_CODE — сразу начать join-регистрацию
        if invite_arg and len(invite_arg) == 8 and invite_arg.isalnum():
            anchor = await message.answer("🔗 Проверяем код приглашения...")
            await state.update_data(usage_mode="join", anchor_msg_id=anchor.message_id)
            success, result = tenant_manager.join_organization_by_invite(message.from_user.id, invite_arg)
            if success:
                preset = tenant_manager.get_invite_preset_by_code(invite_arg) or {}
                await state.update_data(
                    org_name=result,
                    invite_code=invite_arg,
                    preset_role=preset.get('preset_role'),
                    preset_shop=preset.get('preset_shop'),
                )
                await _show_name_prefill_fsm(message.from_user, result, state, message)
            elif result == "KICKED":
                await anchor.edit_text(
                    "🚫 <b>Доступ ограничен</b>\n\nВы были исключены из этой организации. "
                    "Обратитесь к администратору для восстановления доступа.",
                    parse_mode="HTML",
                )
                await clear_state_keep_org(state)
            else:
                await anchor.edit_text(
                    "❌ Ссылка приглашения недействительна.\n\nКак вы планируете использовать систему?",
                    reply_markup=usage_mode_keyboard(),
                )
                await state.set_state(UserRegistrationStates.waiting_for_usage_mode)
        # B) Deep-link: /start ref_TELEGRAMID — реферальная программа
        elif invite_arg and invite_arg.startswith('REF_'):
            referrer_id_str = invite_arg[4:]  # e.g. '123456789'
            if referrer_id_str.isdigit() and int(referrer_id_str) != message.from_user.id:
                await state.update_data(referrer_telegram_id=referrer_id_str)
            await message.answer(
                "👋 Добро пожаловать в систему управления товарами!\n\n"
                "🔗 <i>Вы перешли по реферальной ссылке. Приятного использования!</i>\n\n"
                "Как вы планируете использовать систему?",
                reply_markup=usage_mode_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(UserRegistrationStates.waiting_for_usage_mode)
        else:
            await message.answer(
                "👋 Добро пожаловать в систему управления товарами!\n\n"
                "Как вы планируете использовать систему?",
                reply_markup=usage_mode_keyboard()
            )
            await state.set_state(UserRegistrationStates.waiting_for_usage_mode)

# ── Вспомогательные функции инвайт-регистрации ───────────────────────────────

async def _show_name_prefill_fsm(tg_user, org_name: str, state: FSMContext, message):
    """Г) Показать предложение использовать имя из Telegram или ввести вручную."""
    tg_first = (tg_user.first_name or '').strip()
    tg_last = (tg_user.last_name or '').strip()
    if tg_first:
        display = f"<b>{he(tg_first)}" + (f" {he(tg_last)}" if tg_last else "") + "</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, использовать", callback_data="name_tg_use"),
                InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="name_tg_manual"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_registration")],
        ])
        await fsm_edit(
            state, message,
            f"✅ Вы присоединились к организации <b>{he(org_name)}</b>!\n\n"
            f"👤 Использовать имя из Telegram-профиля?\n{display}",
            reply_markup=kb,
        )
        await state.update_data(tg_first_name=tg_first, tg_last_name=tg_last)
    else:
        await fsm_edit(
            state, message,
            f"✅ Вы присоединились к организации <b>{he(org_name)}</b>!\n\n1️⃣ Введите ваше имя:",
            reply_markup=cancel_registration_keyboard(),
        )
    await state.set_state(UserRegistrationStates.waiting_for_first_name)


async def _notify_org_join(telegram_id: int, first_name: str, last_name: str,
                           shop_name: str, org_name: str):
    """В) Отправить уведомление owner/admin орг о новом участнике."""
    try:
        from bot_holder import get_bot
        bot = get_bot()
        if not bot:
            return
        org_id = tenant_manager.get_user_org_id(telegram_id)
        if not org_id:
            return
        admin_tids = tenant_manager.get_org_admin_telegram_ids(org_id)
        if not admin_tids:
            return
        full_name = f"{first_name} {last_name}".strip()
        text = (
            f"👤 <b>Новый сотрудник</b>\n\n"
            f"Имя: {he(full_name)}\n"
            f"Магазин: {he(shop_name or '—')}\n"
            f"Организация: {he(org_name or '—')}\n\n"
            f"Присоединился через приглашение ✅"
        )
        for tid in admin_tids:
            if tid != telegram_id:
                try:
                    await bot.send_message(tid, text, parse_mode="HTML")
                except Exception:
                    pass
    except Exception:
        pass


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

@router.callback_query(F.data == "name_tg_use")
async def name_tg_use(callback: CallbackQuery, state: FSMContext):
    """Г) Пользователь подтвердил использование имени из Telegram."""
    await callback.answer()
    data = await state.get_data()
    first = data.get('tg_first_name', '').strip()
    last = data.get('tg_last_name', '').strip()
    if not first:
        await fsm_edit(
            state, callback.message,
            "1️⃣ Введите ваше имя:",
            reply_markup=cancel_registration_keyboard(),
        )
        await state.set_state(UserRegistrationStates.waiting_for_first_name)
        return
    await state.update_data(first_name=first, last_name=last)
    await fsm_edit(
        state, callback.message,
        f"✅ Имя: {he(first)} {he(last)}\n\n3️⃣ Введите отчество (или «нет»/«-» для пропуска):",
        reply_markup=cancel_registration_keyboard(),
    )
    await state.set_state(UserRegistrationStates.waiting_for_middle_name)


@router.callback_query(F.data == "name_tg_manual")
async def name_tg_manual(callback: CallbackQuery, state: FSMContext):
    """Г) Пользователь хочет ввести имя вручную."""
    await callback.answer()
    await fsm_edit(
        state, callback.message,
        "1️⃣ Введите ваше имя:",
        reply_markup=cancel_registration_keyboard(),
    )
    await state.set_state(UserRegistrationStates.waiting_for_first_name)


@router.message(UserRegistrationStates.waiting_for_invite_code)
async def process_reg_invite_code(message: Message, state: FSMContext):
    invite_code = message.text.strip().upper()
    success, result = tenant_manager.join_organization_by_invite(message.from_user.id, invite_code)
    
    if success:
        # Д) Загружаем пресет магазина/роли, Г) предлагаем имя из Telegram
        preset = tenant_manager.get_invite_preset_by_code(invite_code) or {}
        await state.update_data(
            org_name=result,
            invite_code=invite_code,
            preset_role=preset.get('preset_role'),
            preset_shop=preset.get('preset_shop'),
        )
        await _show_name_prefill_fsm(message.from_user, result, state, message)
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
    existing_networks = await db.get_all_trade_networks()
    
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

    # Д) Пресет магазина — пропустить выбор
    user_data = await state.get_data()
    preset_shop = user_data.get('preset_shop')
    if preset_shop:
        await state.update_data(shop_name=preset_shop)
        existing_cities = await db.get_all_cities()
        if existing_cities:
            keyboard = create_registration_selection_keyboard(existing_cities, "select_city", "create_new_city")
            await callback.message.edit_text(
                f"✅ Торговая сеть: {network_name}\n"
                f"✅ Магазин (назначен администратором): {he(preset_shop)}\n\n"
                f"🏙️ Выберите город или создайте новый:",
                reply_markup=keyboard, parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                f"✅ Торговая сеть: {network_name}\n"
                f"✅ Магазин (назначен администратором): {he(preset_shop)}\n\n"
                f"🏙️ Введите название города:",
                reply_markup=cancel_registration_keyboard(), parse_mode="HTML"
            )
        await state.set_state(UserRegistrationStates.waiting_for_city)
        return

    existing_shops = await db.get_all_shops()
    
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

    # Д) Пресет магазина — пропустить выбор
    user_data = await state.get_data()
    preset_shop = user_data.get('preset_shop')
    if preset_shop:
        await state.update_data(shop_name=preset_shop)
        existing_cities = await db.get_all_cities()
        if existing_cities:
            keyboard = create_registration_selection_keyboard(existing_cities, "select_city", "create_new_city")
            await fsm_edit(
                state, message,
                f"✅ Торговая сеть: {trade_network}\n"
                f"✅ Магазин (назначен администратором): {he(preset_shop)}\n\n"
                f"🏙️ Выберите город или создайте новый:",
                reply_markup=keyboard,
            )
        else:
            await fsm_edit(
                state, message,
                f"✅ Торговая сеть: {trade_network}\n"
                f"✅ Магазин (назначен администратором): {he(preset_shop)}\n\n"
                f"🏙️ Введите название города:",
                reply_markup=cancel_registration_keyboard(),
            )
        await state.set_state(UserRegistrationStates.waiting_for_city)
        return

    existing_shops = await db.get_all_shops()
    
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
    existing_cities = await db.get_all_cities()
    
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
    existing_cities = await db.get_all_cities()
    
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
            await tenant_db.create_tables()
            await tenant_db.add_user(
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
        await tenant_db.create_tables()
        await tenant_db.add_user(
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
        # Д) Применить пресет роли если задан администратором
        preset_role = user_data.get('preset_role')
        if preset_role and preset_role in ('admin', 'user'):
            try:
                tenant_manager.change_user_role(callback.from_user.id, preset_role)
            except Exception:
                pass

    # Реферальная программа: начислить +30 дней рефереру если пришли по реф-ссылке
    if user_data.get('usage_mode') in ('personal', 'corporate'):
        referrer_id_str = user_data.get('referrer_telegram_id')
        if referrer_id_str:
            try:
                from database import Database as _RefDB
                _rdb = _RefDB('data/shop_bot.db')
                _rdb.create_referral(int(referrer_id_str), callback.from_user.id)
                _rdb.apply_referral_bonus(callback.from_user.id)
            except Exception:
                pass

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
    _inv_user = await _inv_db.get_user(callback.from_user.id)

    try:
        from integration.manager import integration_manager as _int_mgr
        welcome_text += await _int_mgr.try_export_line(_inv_db, 'staff', {
            'name': f"{user_data['first_name']} {user_data['last_name']}",
            'shop_name': user_data['shop_name'],
            'role': 'user',
            'phone': user_data.get('phone', ''),
        })
    except Exception:
        pass

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
    # В) Уведомить owner/admin об новом участнике
    if user_data.get('usage_mode') == 'join':
        import asyncio as _aio
        _aio.create_task(_notify_org_join(
            callback.from_user.id,
            user_data.get('first_name', ''),
            user_data.get('last_name', ''),
            user_data.get('shop_name', ''),
            user_data.get('org_name', ''),
        ))

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
                await tenant_db.create_tables()
                await tenant_db.add_user(
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
            await tenant_db.create_tables()
            await tenant_db.add_user(
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
            # Д) Применить пресет роли если задан администратором
            _preset_role = user_data.get('preset_role')
            if _preset_role and _preset_role in ('admin', 'user'):
                try:
                    tenant_manager.change_user_role(message.from_user.id, _preset_role)
                except Exception:
                    pass
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

    # Реферальная программа: начислить +30 дней рефереру
    if user_data.get('usage_mode') in ('personal', 'corporate'):
        _ref_str2 = user_data.get('referrer_telegram_id')
        if _ref_str2:
            try:
                from database import Database as _RefDB2
                _rdb2 = _RefDB2('data/shop_bot.db')
                _rdb2.create_referral(int(_ref_str2), message.from_user.id)
                _rdb2.apply_referral_bonus(message.from_user.id)
            except Exception:
                pass

    # Пробный период — для личного и корпоративного режимов
    if user_data.get('usage_mode') in ('personal', 'corporate'):
        try:
            from database import Database as ShopDB
            shop_db = ShopDB('data/shop_bot.db')
            await shop_db.create_tables()
            existing_shop_user = await shop_db.get_user(message.from_user.id)
            if not existing_shop_user:
                await shop_db.add_user(
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
                existing_shop_user = await shop_db.get_user(message.from_user.id)
            if existing_shop_user:
                shop_user_id = existing_shop_user[0]
                existing_sub = await shop_db.get_user_subscription(shop_user_id)
                if not existing_sub:
                    trial_settings = await shop_db.get_payment_settings()
                    trial_days = int(trial_settings.get('trial_days', '14'))
                    trial_plan = trial_settings.get('trial_plan', 'Премиум')
                    if trial_days > 0:
                        await shop_db.create_trial_subscription(shop_user_id, trial_plan, trial_days)
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
    _reg_user = await _reg_db.get_user(message.from_user.id)

    try:
        from integration.manager import integration_manager as _int_mgr
        welcome_text += await _int_mgr.try_export_line(_reg_db, 'staff', {
            'name': f"{user_data['first_name']} {user_data['last_name']}",
            'shop_name': user_data['shop_name'],
            'role': 'user',
            'phone': user_data.get('phone', ''),
        })
    except Exception:
        pass

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
    # В) Уведомить owner/admin об новом участнике
    if user_data.get('usage_mode') == 'join':
        import asyncio as _aio
        _aio.create_task(_notify_org_join(
            message.from_user.id,
            user_data.get('first_name', ''),
            user_data.get('last_name', ''),
            user_data.get('shop_name', ''),
            user_data.get('org_name', ''),
        ))

@router.callback_query(F.data.startswith("hint_dismiss:"))
async def hint_dismiss_handler(callback: CallbackQuery):
    """Кнопка «✅ Понятно!» на онбординг-попапах — удаляет сообщение."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


async def _quick_menu_summary(db, user_row, is_admin: bool, tid: int) -> str:
    """Лёгкая сводка дня для заголовка главного меню. Никогда не бросает исключений."""
    try:
        from dashboard_handlers import _scope_filter_kwargs
        from db_utils import get_user_org_scope
        today = date.today().isoformat()
        lines = []

        if is_admin:
            scope_type, scope_values = get_user_org_scope(tid)
            skwargs = _scope_filter_kwargs(scope_type, scope_values)
            summary = db.get_sales_summary(start_date=today, end_date=today, **skwargs)
        else:
            uid = user_row[0]
            summary = db.get_sales_summary(start_date=today, end_date=today, user_id=uid)

        cnt = int((summary[0] or 0) if summary else 0)
        rev = float((summary[2] or 0) if summary else 0)
        if cnt > 0:
            lines.append(f"📅 <b>Сегодня:</b> {cnt} прод. · {format_currency(rev)} ₽")

        # Малые остатки (только если есть user_row)
        if user_row:
            try:
                low = db.get_low_stock_items_for_user(user_row[0])
                n = len(low) if low else 0
                if n:
                    lines.append(f"⚠️ Малых остатков: {n}")
            except Exception:
                pass

        return ("\n" + "\n".join(lines)) if lines else ""
    except Exception:
        return ""


@router.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    
    # Очищаем состояния при возврате в меню, сохраняя выбранную организацию
    await clear_state_keep_org(state)
    
    from subscription_utils import get_subscription_days_remaining
    _days = get_subscription_days_remaining(callback.from_user.id)
    _banner = ""
    if _days is not None and _days <= 7:
        if _days == 0:
            _banner = "\n\n⚠️ <b>Подписка истекла.</b> Перейдите в Профиль → Подписка."
        else:
            _banner = f"\n\n⚠️ <b>Подписка истекает через {_days} дн.</b> Не забудьте продлить."

    if user:
        _is_admin = is_any_admin(callback.from_user.id)
        _summary = await _quick_menu_summary(current_db, user, _is_admin, callback.from_user.id)
        await callback.message.edit_text(
            f"🏪 <b>Главное меню</b>{_summary}{_banner}",
            reply_markup=main_menu(callback.from_user.id, user[8]),
            parse_mode="HTML"
        )
    else:
        # Для супер-админов без регистрации
        if env_manager.is_super_admin(callback.from_user.id):
            await callback.message.edit_text(
                "🏪 <b>Главное меню (Админ)</b>",
                reply_markup=main_menu(callback.from_user.id, "Системный"),
                parse_mode="HTML"
            )
        else:
            # Сначала проверяем основную БД
            from database import Database as CentralDB
            central_db = CentralDB('data/main.db')
            user_central = central_db.get_user(callback.from_user.id)
            
            if user_central:
                await callback.message.edit_text(
                    f"🏪 <b>Главное меню</b>{_banner}",
                    reply_markup=main_menu(callback.from_user.id, user_central[8]),
                    parse_mode="HTML"
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
        user = await current_db.get_user(callback.from_user.id)
    
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
    builder.add(InlineKeyboardButton(text="📅 Мой график", callback_data="my_schedule"))
    builder.add(InlineKeyboardButton(text="📋 Мои отсутствия", callback_data="abs_my"))
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
        shops = await current_db.get_all_shops()
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
        networks = await current_db.get_all_trade_networks()
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
    """Обновляет поле пользователя в org/personal БД (через asyncio.to_thread — не блокирует event loop)."""
    def _do():
        conn = sqlite3.connect(db_file)
        conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, telegram_id))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_do)

@router.callback_query(F.data.startswith("prof_shop_pick_"))
async def prof_shop_pick(callback: CallbackQuery, state: FSMContext):
    """Администратор выбрал существующий магазин из списка для своего профиля."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    shop_raw = callback.data.removeprefix("prof_shop_pick_")
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, await current_db.get_all_shops() or [])
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
    net_name = resolve_cb_name(net_raw, await current_db.get_all_trade_networks() or [])
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
        await asyncio.gather(
            _apply_user_field(message.from_user.id, current_db.db_file, field, new_value),
            _apply_user_field(message.from_user.id, 'data/main.db', field, new_value),
            return_exceptions=True,
        )
        
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
        await asyncio.gather(
            _apply_user_field(callback.from_user.id, current_db.db_file, 'timezone', new_tz),
            _apply_user_field(callback.from_user.id, 'data/main.db', 'timezone', new_tz),
            _apply_user_field(callback.from_user.id, 'data/shop_bot.db', 'timezone', new_tz),
            return_exceptions=True,
        )
        
        await callback.message.edit_text(f"✅ Часовой пояс изменен на {new_tz}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("user_profile")]]))
        await clear_state_keep_org(state)
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка при изменении часового пояса: {e}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("user_profile")]])
        )

# ── Справка: тексты по категориям ────────────────────────────────────────────

_HELP_TEXTS: dict[str, str] = {
    "sales": (
        "💰 <b>Продажи</b>\n\n"
        "<b>Как оформить продажу:</b>\n"
        "1. Нажмите «💰 ПРОДАЖА» в главном меню\n"
        "2. Выберите товары — добавляйте несколько позиций в одну корзину\n"
        "3. Укажите количество кнопками [1, 2, 3, 5, 10, 20, 50]\n"
        "4. Нажмите «✅ Оформить» для подтверждения\n\n"
        "<b>Быстрый выбор товаров:</b>\n"
        "⭐ <b>Избранное</b> — закрепите часто продаваемые позиции, чтобы не искать каждый раз\n"
        "🔄 <b>Недавние</b> — последние 10 использованных позиций всегда под рукой\n"
        "🔍 <b>Поиск</b> — мгновенный поиск по названию среди товаров в наличии\n\n"
        "<b>Продажа из другого магазина:</b>\n"
        "Если нужного товара нет в вашем магазине — оформите продажу со склада другого "
        "магазина сети кнопкой «🏪 Другой магазин».\n\n"
        "<b>История и редактирование:</b>\n"
        "📝 «Мои продажи» → просмотр своих продаж с возможностью изменить или отменить.\n\n"
        "<b>🔔 Уведомление коллегам:</b>\n"
        "Если администратор включил опцию — коллеги по смене получают пуш о каждой вашей продаже."
    ),
    "reports": (
        "📊 <b>Отчёты и рейтинги</b>\n\n"
        "<b>Личная статистика:</b>\n"
        "Переключайте период кнопками: <b>Сегодня / Неделя / Месяц</b>.\n"
        "Показатели: оборот, количество продаж, средний чек, топ-товары.\n\n"
        "<b>📅 Дашборд (для администратора):</b>\n"
        "Сводка по всем магазинам и продавцам. Выполнение планов, начисленные зарплаты.\n\n"
        "<b>📋 Мои планы:</b>\n"
        "«Отчёты» → «📋 Мои планы» — визуальный прогресс по всем назначенным планам "
        "с разбивкой на неделю и месяц.\n\n"
        "<b>🏆 Рейтинги:</b>\n"
        "Лучшие продавцы, магазины и города. Периоды: 7 дней / месяц / прошлый месяц.\n"
        "Если вы не в топ-10 — ваша позиция всё равно показывается внизу списка.\n\n"
        "<b>🔍 Фильтры:</b>\n"
        "Кнопка «Фильтр» позволяет смотреть данные только по конкретному магазину, "
        "городу или торговой сети.\n\n"
        "<b>📥 Excel-экспорт:</b>\n"
        "Скачайте детальный отчёт с разбивкой по товарам, магазинам и продавцам."
    ),
    "inventory": (
        "📦 <b>Остатки товаров</b>\n\n"
        "<b>Просмотр остатков:</b>\n"
        "«📦 ОСТАТКИ» → выберите категорию → список товаров с количеством в наличии "
        "в вашем магазине.\n\n"
        "<b>Что значат количества:</b>\n"
        "• Число > 0 — товар есть в наличии, продажа возможна\n"
        "• 0 — товар отсутствует, при попытке продать появится предупреждение\n"
        "• Остатки обновляются автоматически после каждой записанной продажи\n\n"
        "<b>Ручное обновление:</b>\n"
        "«✏️ Редактировать остатки» — измените количество вручную (приход товара, "
        "инвентаризация). Доступно если администратор дал соответствующий доступ.\n\n"
        "<b>Поиск по складу:</b>\n"
        "🔍 Введите название товара для быстрого поиска по всему складу магазина."
    ),
    "earnings": (
        "💵 <b>Заработок и рабочий график</b>\n\n"
        "<b>💵 Мой заработок:</b>\n"
        "Комиссия с продаж + оклад за рабочие смены за выбранный период.\n"
        "• <b>Комиссия</b>: % от суммы продажи или фиксированная ставка за единицу товара\n"
        "• Ставки задаёт администратор по конкретным товарам или категориям\n"
        "• <b>Оклад</b>: дневная ставка × количество рабочих дней за месяц\n\n"
        "<b>📅 Мой график:</b>\n"
        "Календарь рабочих смен на месяц. Администратор отмечает ваши рабочие дни.\n"
        "Нажмите на любой день, чтобы увидеть время смены (если указано).\n\n"
        "<b>⏰ Шаблоны смен:</b>\n"
        "«Мой профиль» → «⏰ Расписание смен» — задайте стандартное время начала "
        "и конца смены для каждого дня недели (Пн–Вс). Шаблон применяется автоматически "
        "при добавлении новой смены администратором.\n\n"
        "<b>📈 Период просмотра:</b>\n"
        "Переключайте месяц кнопками «‹» / «›» в разделе заработка."
    ),
    "notifications": (
        "🔔 <b>Уведомления</b>\n\n"
        "<b>Типы push-уведомлений:</b>\n"
        "• 📈 <b>Продажи коллег</b> — когда кто-то в вашем магазине оформляет продажу "
        "в вашу смену (включается в настройках)\n"
        "• 🎯 <b>Планы</b> — автоматически при достижении 50%, 75% и 100% цели\n"
        "• 🏆 <b>Конкурсы</b> — изменение позиции, завершение конкурса, объявление победителя\n"
        "• ⏰ <b>Запланированные напоминания</b> — в указанное дату и время\n"
        "• 💳 <b>Подписка</b> — напоминание об истечении (за 14, 7, 3, 1 день)\n\n"
        "<b>✅ Кнопка «Прочитано»:</b>\n"
        "На каждом push-уведомлении есть кнопка «✅ Прочитано» — нажмите, "
        "чтобы убрать уведомление из непрочитанных.\n\n"
        "<b>Рассылка администратора:</b>\n"
        "Администратор может отправлять уведомления <b>всем</b>, "
        "<b>по конкретному магазину</b> или <b>по роли</b> (продавцы / администраторы). "
        "Рассылки поддерживают отложенную отправку с точным временем.\n\n"
        "<b>Настройки:</b>\n"
        "«👤 Мой профиль» → «🔔 Уведомления» → включите или выключите каждый тип отдельно."
    ),
    "profile": (
        "👤 <b>Профиль и настройки</b>\n\n"
        "<b>Что можно изменить:</b>\n"
        "• Имя, фамилия, отчество\n"
        "• Номер телефона и email\n"
        "• Магазин (к которому привязаны ваши продажи и остатки)\n"
        "• Город и торговая сеть\n"
        "• <b>Часовой пояс</b> — влияет на время в отчётах и уведомлениях\n\n"
        "<b>⏰ Расписание смен:</b>\n"
        "«⏰ Расписание смен» → задайте стандартное время начала / конца смены "
        "на каждый день недели. Применяется автоматически при новой смене.\n\n"
        "<b>📞 Контакты организации:</b>\n"
        "«📞 Контакты» — список контактов вашей организации (телефоны, мессенджеры).\n\n"
        "<b>💳 Подписка:</b>\n"
        "«💳 Подписка» — текущий тариф, срок действия, история платежей.\n\n"
        "<b>Несколько организаций:</b>\n"
        "Если вы состоите в нескольких орг — используйте переключатель в главном меню "
        "для смены активной организации. Данные каждой орг изолированы."
    ),
    "staff": (
        "👥 <b>Управление сотрудниками</b>\n\n"
        "<b>Добавление через инвайт-код:</b>\n"
        "«👥 Упр. сотрудниками» → «🔗 Инвайт» → поделитесь ссылкой или кодом. "
        "Сотрудник переходит по ссылке (deep-link) — бот автоматически определяет орг "
        "и предлагает присоединиться.\n\n"
        "<b>🔄 Сброс инвайт-кода:</b>\n"
        "«🔄 Сбросить код» — генерирует новый код (старые ссылки перестают работать).\n\n"
        "<b>⚙️ Пресет роли и магазина:</b>\n"
        "Заранее задайте роль и магазин, которые получает новый сотрудник при вступлении "
        "через инвайт. Шаги выбора роли / магазина пропускаются автоматически.\n\n"
        "<b>🔔 Уведомление о новом участнике:</b>\n"
        "Все владельцы и администраторы орг получают пуш при каждой новой регистрации.\n\n"
        "<b>Роли:</b>\n"
        "• <b>owner</b> — владелец, полный доступ ко всему\n"
        "• <b>admin</b> — администратор с настраиваемым скоупом видимости\n"
        "• <b>user</b> — продавец (свои продажи и базовые отчёты)\n\n"
        "<b>Скоуп (область видимости):</b>\n"
        "Ограничьте доступ — сотрудник видит данные только своего магазина, "
        "города или торговой сети.\n\n"
        "<b>Кастомная должность:</b>\n"
        "Задайте произвольное название: Менеджер, Кассир, Старший продавец и т.д.\n\n"
        "<b>Поиск и управление:</b>\n"
        "🔍 Поиск по @username или имени. Смена роли, изменение скоупа, удаление."
    ),
    "products": (
        "🛍 <b>Товары и склад</b>\n\n"
        "<b>Добавление товаров:</b>\n"
        "• <b>Вручную</b>: название → категория → цена → сохранить\n"
        "• <b>📊 Импорт из Excel</b>: загрузите .xlsx, просмотрите превью и подтвердите\n"
        "• <b>Списком</b>: вставьте несколько строк в формате «Название | Цена»\n\n"
        "<b>Категории:</b>\n"
        "Создавайте категории для удобной фильтрации в продажах и отчётах.\n\n"
        "<b>Редактирование и удаление:</b>\n"
        "«✏️ Редактировать» — изменить название, категорию или цену.\n"
        "«🗑 Удалить» — удаление товара (история продаж сохраняется).\n\n"
        "<b>Управление остатками:</b>\n"
        "«📦 Упр. остатками» → выберите магазин → добавьте или обновите количества. "
        "Остатки считаются по каждому магазину отдельно.\n\n"
        "<b>Лимиты по тарифу:</b>\n"
        "Бесплатный: 50 товаров · Базовый: 200 · Стандарт: 500 · Премиум: без ограничений."
    ),
    "motivation": (
        "🎯 <b>Мотивация и оклады</b>\n\n"
        "<b>Комиссия с продаж:</b>\n"
        "«🎯 Упр. мотивацией» → задайте ставку для товара или категории:\n"
        "• Процент от суммы продажи (%)\n"
        "• Фиксированная сумма за единицу товара (₽/шт)\n\n"
        "<b>📅 Расписание ставок по месяцам:</b>\n"
        "Задайте разные ставки на разные месяцы — например, повышенная ставка "
        "в сезон. «📅 Мотивация по месяцам» показывает матрицу товар × месяц.\n\n"
        "<b>Ограничение по категориям:</b>\n"
        "«Доп. условия» — задайте продавцу фильтр: комиссия начисляется только "
        "с выбранных категорий товаров.\n\n"
        "<b>Оклады и смены:</b>\n"
        "«💰 Оклады и смены» → дневная ставка (₽/день) для каждого сотрудника. "
        "Отмечайте рабочие дни в календаре. Итог = ставка × количество смен за месяц.\n\n"
        "<b>Шаблоны смен:</b>\n"
        "Стандартное время по дням недели — применяется автоматически при новой смене.\n\n"
        "<b>Совместная мотивация:</b>\n"
        "Пул на всю команду с индивидуальными коэффициентами."
    ),
    "plans": (
        "📋 <b>Планы продаж</b>\n\n"
        "<b>Создание плана:</b>\n"
        "«📋 Планы продаж» → «➕ Создать план» — задайте:\n"
        "• <b>Кому</b>: конкретному продавцу или по магазину\n"
        "• <b>Период</b>: неделя / месяц\n"
        "• <b>Метрика</b>: оборот (₽) или количество (шт)\n"
        "• <b>Фильтр</b>: по категории или по конкретным товарам (опционально)\n\n"
        "<b>🔔 Milestone-уведомления:</b>\n"
        "При достижении <b>50%, 75% и 100%</b> цели — автоматический пуш продавцу "
        "и администратору. Каждый milestone срабатывает строго один раз за период.\n\n"
        "<b>Просмотр прогресса (для администратора):</b>\n"
        "«📊 Выполнение планов» — список всех активных планов с визуальной шкалой "
        "прогресса и фактическими данными. Поддерживается фильтрация по магазину / продавцу.\n\n"
        "<b>Для продавца:</b>\n"
        "«Отчёты» → «📋 Мои планы» — текущий прогресс своих планов с разбивкой "
        "на неделю и месяц."
    ),
    "contests": (
        "🏆 <b>Конкурсы</b>\n\n"
        "<b>Создание конкурса:</b>\n"
        "«🏆 Конкурсы» → «➕ Создать» — укажите название, даты начала и конца, "
        "метрику (оборот ₽ или количество шт), участников.\n\n"
        "<b>Фильтрация участников:</b>\n"
        "• По категориям товаров (только нужный ассортимент идёт в зачёт)\n"
        "• По конкретным магазинам\n"
        "• По отдельным сотрудникам\n\n"
        "<b>Автозавершение:</b>\n"
        "Конкурс завершается автоматически в указанную дату — "
        "победитель определяется без ручного вмешательства.\n\n"
        "<b>Корректировка показателей:</b>\n"
        "Измените результаты любого участника вручную в любой момент — "
        "итог пересчитается автоматически.\n\n"
        "<b>Архив:</b>\n"
        "Завершённые конкурсы хранятся в архиве. Можно очистить архив "
        "полностью (с подтверждением)."
    ),
    "sheets": (
        "📊 <b>Google Sheets — интеграция</b>\n\n"
        "<b>Доступно на тарифах:</b> Стандарт и Премиум.\n\n"
        "<b>Подключение (Device Flow, без браузера на ПК):</b>\n"
        "1. «📊 Google Sheets» → «➕ Добавить подключение» → OAuth\n"
        "2. Откройте ссылку на телефоне, введите код подтверждения\n"
        "3. Войдите в Google-аккаунт — бот получит доступ к таблицам\n\n"
        "<b>Что экспортируется:</b>\n"
        "• Продажи: дата, товар, магазин, количество, сумма, продавец\n"
        "• Остатки: магазин, товар, категория, количество\n"
        "Настройте нужные столбцы (mapping) и лист.\n\n"
        "<b>Режимы записи:</b>\n"
        "• <b>Добавить строку</b> — каждая новая продажа = новая строка\n"
        "• <b>Обновить ячейку</b> — суммирование в существующей таблице\n"
        "• <b>Заменить лист</b> — полная перезапись листа\n\n"
        "<b>Расписание:</b>\n"
        "Немедленно (при каждой продаже) или по расписанию (cron).\n\n"
        "<b>Перенос экспортов:</b>\n"
        "Если нужно сменить Google-аккаунт — кнопка «📤 Перенести экспорты» "
        "переносит все настройки на новое подключение без потери данных.\n\n"
        "<b>Важно:</b>\n"
        "Если интеграция отключилась с ошибкой авторизации — переподключите аккаунт "
        "через «➕ Добавить подключение». Продажи, записанные пока интеграция была "
        "отключена, не попадут в таблицу автоматически."
    ),
    "subscription": (
        "💳 <b>Подписка и тарифы</b>\n\n"
        "<b>Тарифные планы:</b>\n"
        "Актуальные условия и цены смотрите в разделе «💳 Подписка».\n\n"
        "<b>🎁 Пробный период:</b>\n"
        "14 дней бесплатно на уровне Премиум сразу после регистрации — "
        "попробуйте все функции без ограничений.\n\n"
        "<b>Способы оплаты:</b>\n"
        "• <b>СБП</b>: переведите сумму → загрузите скриншот чека → "
        "администратор подтверждает вручную (заявки >72ч отклоняются автоматически)\n"
        "• <b>YooKassa</b>: автоматическая онлайн-оплата картой\n\n"
        "<b>🎁 Промокоды:</b>\n"
        "Введите промокод при оформлении подписки. Поддерживаются:\n"
        "• Скидка в процентах (например, 20%)\n"
        "• Фиксированная скидка в рублях (например, 300₽)\n"
        "Промокод может иметь срок действия и ограничение по числу использований. "
        "При вводе отображается оставшийся остаток (если их мало)."
    ),
    "system": (
        "🔧 <b>Системная панель</b>\n\n"
        "<b>🏢 Организации:</b>\n"
        "Просмотр всех орг, создание новых, удаление (все данные удаляются). "
        "Каждая орг — изолированная база данных.\n\n"
        "<b>👥 Все пользователи:</b>\n"
        "Поиск по Telegram ID или @username, просмотр любого пользователя "
        "в любой организации.\n\n"
        "<b>💰 Платёжная система:</b>\n"
        "Подтверждение и отклонение СБП-заявок. Заявки старше 72ч отклоняются "
        "автоматически. Управление промокодами — создание (в том числе пакетное), "
        "редактирование, финансовая статистика (сколько скидок выдано в рублях).\n\n"
        "<b>🎁 Промокоды (расширенные возможности):</b>\n"
        "• Тип скидки: % или фиксированная сумма (₽)\n"
        "• Срок действия и лимит на одного пользователя\n"
        "• Ограничение по тарифам (только для Стандарт / Премиум)\n"
        "• 🎲 Генератор случайных кодов и 📦 пакетное создание (до 100 штук)\n\n"
        "<b>💾 Резервные копии:</b>\n"
        "Создание .zip-архива всех баз данных и скачивание прямо в Telegram.\n\n"
        "<b>🧪 Системные тесты:</b>\n"
        "Запуск проверки всех модулей импортов прямо из бота.\n\n"
        "<b>Режимы работы:</b>\n"
        "Переключайтесь между системной панелью, личным кабинетом "
        "и любой из организаций в любой момент."
    ),
}

_HELP_CAT_TITLES: dict[str, str] = {
    "sales":        "💰 Продажи",
    "reports":      "📊 Отчёты и рейтинги",
    "inventory":    "📦 Остатки товаров",
    "earnings":     "💵 Заработок и график",
    "notifications":"🔔 Уведомления",
    "profile":      "👤 Профиль и настройки",
    "staff":        "👥 Сотрудники",
    "products":     "🛍 Товары и склад",
    "motivation":   "🎯 Мотивация и оклады",
    "plans":        "📋 Планы продаж",
    "contests":     "🏆 Конкурсы",
    "sheets":       "📊 Google Sheets",
    "subscription": "💳 Подписка и тарифы",
    "system":       "🔧 Системная панель",
}


def _help_menu_kb(is_admin: bool, is_super: bool) -> InlineKeyboardMarkup:
    """Клавиатура главного меню справки — кнопки по категориям."""
    builder = InlineKeyboardBuilder()
    user_cats = ["sales", "reports", "inventory", "earnings", "notifications", "profile", "subscription"]
    admin_cats = ["staff", "products", "motivation", "plans", "contests", "sheets"]
    super_cats = ["system"]

    for cat in user_cats:
        builder.button(text=_HELP_CAT_TITLES[cat], callback_data=f"help_cat_{cat}")
    if is_admin:
        for cat in admin_cats:
            builder.button(text=_HELP_CAT_TITLES[cat], callback_data=f"help_cat_{cat}")
    if is_super:
        for cat in super_cats:
            builder.button(text=_HELP_CAT_TITLES[cat], callback_data=f"help_cat_{cat}")

    builder.adjust(1)
    builder.row(back_button("main_menu"))
    return builder.as_markup()


def _help_cat_kb() -> InlineKeyboardMarkup:
    """Кнопка возврата в меню справки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все разделы справки", callback_data="help")],
        [back_button("main_menu")],
    ])


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery, state: FSMContext):
    """Главное меню справки — категоризированное, с учётом роли."""
    await callback.answer()
    user_id = callback.from_user.id
    is_super = env_manager.is_super_admin(user_id)
    is_admin = is_any_admin(user_id)

    await callback.message.edit_text(
        "ℹ️ <b>Справка</b>\n\n"
        "Выберите раздел, чтобы узнать подробнее о возможностях бота:",
        reply_markup=_help_menu_kb(is_admin, is_super),
        parse_mode="HTML",
    )


def _fmt_plan_duration(days: int) -> str:
    """Форматирует количество дней в читаемый срок подписки."""
    if days <= 0:
        return ""
    if days % 365 == 0:
        y = days // 365
        return f"{y} {'год' if y == 1 else 'лет'}"
    if days % 30 == 0:
        m = days // 30
        if m == 1:
            return "1 месяц"
        if m in (2, 3, 4):
            return f"{m} месяца"
        return f"{m} месяцев"
    return f"{days} дней"


def _fmt_limit(val: int) -> str:
    return "∞" if val == -1 else str(val)


@router.callback_query(F.data == "help_cat_subscription")
async def help_cat_subscription_callback(callback: CallbackQuery, state: FSMContext):
    """Справка по подписке — тарифы берутся актуально из БД."""
    await callback.answer()
    try:
        db = wrap_db(Database("data/shop_bot.db"))
        plans = await db.get_subscription_plans()
        # columns: [0]=id [1]=name [2]=duration_days [3]=price [4]=description
        #          [5]=is_active [6]=max_products [7]=max_shops [8]=max_sales_per_month
        #          [9]=can_export_reports [10]=can_view_analytics [11]=can_use_notifications
        #          [12]=can_use_integrations
        lines = ["💳 <b>Подписка и тарифы</b>\n\n<b>Тарифные планы:</b>"]
        for p in plans:
            name         = p[1]
            duration     = p[2]
            price        = p[3]
            max_prod     = p[6]
            max_shops    = p[7]
            max_sales    = p[8]
            can_export   = bool(p[9])
            can_notif    = bool(p[11])
            can_integr   = bool(p[12])

            if price == 0:
                price_str = "Бесплатно"
                dur_str   = ""
            else:
                price_str = f"{int(price):,}₽".replace(",", "\u202f")
                dur_str   = f" / {_fmt_plan_duration(duration)}"

            features = []
            if can_export:
                features.append("Excel-экспорт")
            if can_notif:
                features.append("уведомления")
            if can_integr:
                features.append("Google Sheets")
            feat_str = (", " + ", ".join(features)) if features else ""

            prod_str  = _fmt_limit(max_prod)
            shop_str  = _fmt_limit(max_shops)
            sales_str = _fmt_limit(max_sales)

            lines.append(
                f"• <b>{name}</b> — {price_str}{dur_str}\n"
                f"  {prod_str} товаров · {shop_str} маг. · {sales_str} продаж/мес{feat_str}"
            )

        text = "\n".join(lines)
        text += (
            "\n\n<b>🎁 Пробный период:</b>\n"
            "14 дней бесплатно на уровне Премиум сразу после регистрации — "
            "попробуйте все функции без ограничений.\n\n"
            "<b>Способы оплаты:</b>\n"
            "• <b>СБП</b>: переведите сумму → загрузите скриншот чека → "
            "администратор подтверждает вручную\n"
            "• <b>YooKassa</b>: автоматическая онлайн-оплата картой\n\n"
            "<b>Промокоды:</b>\n"
            "Введите промокод при оформлении подписки для получения скидки."
        )
    except Exception:
        text = _HELP_TEXTS.get("subscription", "💳 <b>Подписка и тарифы</b>\n\nИнформация временно недоступна.")

    await callback.message.edit_text(text, reply_markup=_help_cat_kb(), parse_mode="HTML")


@router.callback_query(F.data.startswith("help_cat_"))
async def help_category_callback(callback: CallbackQuery, state: FSMContext):
    """Детальная справка по выбранной категории."""
    cat = callback.data[len("help_cat_"):]
    text = _HELP_TEXTS.get(cat)
    if not text:
        await callback.answer("❓ Раздел не найден", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        text,
        reply_markup=_help_cat_kb(),
        parse_mode="HTML",
    )

@router.callback_query(lambda c: c.data == "cancel_registration")
async def cancel_registration_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("❌ Регистрация отменена. Используйте /start чтобы начать заново.")
    await clear_state_keep_org(state)
