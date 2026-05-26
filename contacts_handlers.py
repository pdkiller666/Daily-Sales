"""
Обработчики для просмотра контактов пользователей
"""
import asyncio
import os
import sqlite3
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from database import Database
from keyboards import back_button, safe_cb, resolve_cb_name
from db_utils import get_db, is_any_admin
from env_manager import env_manager as _env
from utils import he
from message_utils import fsm_edit
from states import SearchStates

# Создаем роутер для контактов
contacts_router = Router()


def get_super_admin_contact(db_path="data/main.db"):
    """Получить контакт супер-админа из main.db"""
    try:
        super_admin_id = _env.get_main_admin_id()
        if not super_admin_id:
            return None
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, telegram_id, first_name, last_name, middle_name, phone, email, 
                   trade_network, shop_name, city, timezone, created_at 
            FROM users WHERE telegram_id = ?
        """, (super_admin_id,))
        user = cursor.fetchone()
        conn.close()
        return user
    except Exception:
        return None

@contacts_router.callback_query(F.data == "contacts")
async def view_contacts_menu_compat(callback: CallbackQuery, state: FSMContext):
    """Совместимость для старого колбэка contacts"""
    await view_contacts_menu(callback, state)

@contacts_router.callback_query(F.data == "view_contacts")
async def view_contacts_menu(callback: CallbackQuery, state: FSMContext):
    """Меню просмотра контактов"""
    await callback.answer()
    data = await state.get_data()
    selected_org_db = data.get("selected_org_db")
    is_personal_mode = (selected_org_db == "data/shop_bot.db") if selected_org_db else True
    
    if is_personal_mode:
        # В личном режиме упрощённое меню - только свои контакты и поддержка
        await callback.message.edit_text(
            "👥 Контакты\n\nВ личном режиме доступны только ваши контакты и поддержка.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Мой контакт", callback_data="contacts_my")],
                [InlineKeyboardButton(text="🛡️ Поддержка", callback_data="contacts_support")],
                [back_button("user_profile")]
            ])
        )
    else:
        # В корпоративном режиме полный функционал
        await callback.message.edit_text(
            "👥 Просмотр контактов\n\nВыберите способ поиска:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏪 По магазинам", callback_data="contacts_by_shop")],
                [InlineKeyboardButton(text="🏙️ По городам", callback_data="contacts_by_city")],
                [InlineKeyboardButton(text="👥 Все пользователи", callback_data="contacts_all")],
                [InlineKeyboardButton(text="🛡️ Поддержка", callback_data="contacts_support")],
                [back_button("user_profile")]
            ])
        )

@contacts_router.callback_query(F.data == "contacts_my")
async def show_my_contact(callback: CallbackQuery, state: FSMContext):
    """Показать свой контакт"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    
    if not user:
        await callback.message.edit_text(
            "❌ Ваш профиль не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("view_contacts")]])
        )
        return
    
    user_id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop, city, timezone, created_at = user[:12]
    
    full_name = f"{he(first_name or '')} {he(last_name or '')}".strip()
    if middle_name:
        full_name += f" {he(middle_name)}"
    
    message_text = f"👤 Мой контакт\n\n"
    message_text += f"👤 {full_name}\n"
    message_text += f"🆔 Telegram ID: {telegram_id}\n"
    message_text += f"📞 Телефон: {he(phone) if phone else 'не указан'}\n"
    message_text += f"📧 Email: {he(email) if email else 'не указан'}\n"
    message_text += f"🏪 Магазин: {he(shop) if shop else 'не указан'}\n"
    message_text += f"🏙️ Город: {he(city) if city else 'не указан'}\n"
    
    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("view_contacts")]]),
        parse_mode="HTML"
    )

@contacts_router.callback_query(F.data == "contacts_support")
async def show_support_contact(callback: CallbackQuery):
    """Показать контакт поддержки (супер-админ)"""
    await callback.answer()
    super_admin = await asyncio.to_thread(get_super_admin_contact)
    
    message_text = "🛡️ Контакт поддержки\n\n"
    
    if super_admin:
        user_id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop, city, timezone, created_at = super_admin[:12]
        full_name = f"{he(first_name or 'Администратор')} {he(last_name or '')}".strip()
        
        message_text += f"👤 {full_name}\n"
        message_text += f"🔗 <a href='tg://user?id={telegram_id}'>Написать в Telegram</a>\n"
        if phone:
            message_text += f"📞 Телефон: {he(phone)}\n"
        if email:
            message_text += f"📧 Email: {he(email)}\n"
    else:
        fallback_id = _env.get_main_admin_id() or ""
        message_text += f"👤 Администратор\n"
        message_text += f"🔗 <a href='tg://user?id={fallback_id}'>Написать в Telegram</a>\n"
    
    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("view_contacts")]]),
        parse_mode="HTML"
    )

@contacts_router.callback_query(F.data == "contacts_by_shop")
async def contacts_by_shop(callback: CallbackQuery, state: FSMContext):
    """Просмотр контактов по магазинам"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    
    if not shops:
        await callback.message.edit_text(
            "🏪 Магазины не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("view_contacts")]]), parse_mode="HTML"
        )
        return
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍 Найти магазин", callback_data="contacts_srch_shop_start"))
    for shop in shops:
        builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("sc_", shop)))
    builder.add(back_button("view_contacts"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        "🏪 Выберите магазин:",
        reply_markup=builder.as_markup()
    )


@contacts_router.callback_query(F.data == "contacts_srch_shop_start")
async def contacts_srch_shop_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.shop_contacts)
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="contacts_by_shop"))
    await callback.message.edit_text(
        "🔍 <b>Поиск магазина</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@contacts_router.message(SearchStates.shop_contacts)
async def contacts_srch_shop_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    shops = await current_db.get_all_shops()
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍 Найти магазин", callback_data="contacts_srch_shop_start"))
    for shop in filtered:
        builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("sc_", shop)))
    if query:
        builder.add(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="contacts_by_shop"))
    builder.add(back_button("view_contacts"))
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"🏪 Выберите магазин:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@contacts_router.callback_query(F.data == "contacts_by_city")
async def contacts_by_city(callback: CallbackQuery, state: FSMContext):
    """Просмотр контактов по городам"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    cities = await current_db.get_all_cities()
    
    if not cities:
        await callback.message.edit_text(
            "🏙️ Города не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("view_contacts")]]), parse_mode="HTML"
        )
        return
    
    builder = InlineKeyboardBuilder()
    for city in cities:
        builder.add(InlineKeyboardButton(text=city, callback_data=safe_cb("cc_", city)))
    builder.add(back_button("view_contacts"))
    builder.adjust(2, 1)
    
    await callback.message.edit_text(
        "🏙️ Выберите город:",
        reply_markup=builder.as_markup()
    )

@contacts_router.callback_query(F.data.startswith("sc_"))
async def show_shop_contacts(callback: CallbackQuery, state: FSMContext):
    """Показать контакты пользователей магазина"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    shop_raw = callback.data[3:]
    shop_name = resolve_cb_name(shop_raw, await current_db.get_all_shops() or [])
    users = await current_db.get_users_by_shop(shop_name)
    
    if not users:
        await callback.message.edit_text(
            f"🏪 В магазине '{shop_name}' нет зарегистрированных пользователей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("contacts_by_shop")]])
        )
        return
    
    message_text = f"🏪 Контакты магазина '{he(shop_name)}'\n\n"
    
    for user in users:
        user_id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop, city, timezone, created_at = user[:12]
        
        full_name = f"{he(first_name)} {he(last_name)}"
        if middle_name:
            full_name += f" {he(middle_name)}"
        
        message_text += f"👤 {full_name}\n"
        message_text += f"🔗 <b>Telegram:</b> <a href='tg://user?id={telegram_id}'>Написать</a>\n"
        message_text += f"🆔 Telegram ID: {telegram_id}\n"
        message_text += f"📞 Телефон: {he(phone) if phone else 'не указан'}\n"
        message_text += f"📧 Email: {he(email) if email else 'не указан'}\n"
        message_text += f"🏢 Торговая сеть: {he(trade_network) if trade_network else 'не указана'}\n"
        message_text += f"🏙️ Город: {he(city) if city else 'не указан'}\n"
        message_text += "─────────────────\n\n"
    
    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("contacts_by_shop")]]),
        parse_mode="HTML"
    )

@contacts_router.callback_query(F.data.startswith("cc_"))
async def show_city_contacts(callback: CallbackQuery, state: FSMContext):
    """Показать контакты пользователей города"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    city_raw = callback.data[3:]
    city_name = resolve_cb_name(city_raw, await current_db.get_all_cities() or [])
    users = await current_db.get_users_by_city(city_name)
    
    if not users:
        await callback.message.edit_text(
            f"🏙️ В городе '{city_name}' нет зарегистрированных пользователей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("contacts_by_city")]])
        )
        return
    
    # Группируем по магазинам
    shops_users = {}
    for user in users:
        shop_name = user[8]  # shop_name
        if shop_name not in shops_users:
            shops_users[shop_name] = []
        shops_users[shop_name].append(user)
    
    message_text = f"🏙️ Контакты города '{he(city_name)}'\n\n"
    
    for shop_name, shop_users in sorted(shops_users.items()):
        message_text += f"🏪 {he(shop_name)}:\n"
        
        for user in shop_users:
            user_id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop, city, timezone, created_at = user
            
            full_name = f"{he(first_name)} {he(last_name)}"
            if middle_name:
                full_name += f" {he(middle_name)}"
            
            message_text += f"  👤 {full_name}"
            message_text += f" [<a href='tg://user?id={telegram_id}'>Написать</a>]"
            if phone:
                message_text += f" | 📞 {phone}"
            if email:
                message_text += f" | 📧 {email}"
            message_text += "\n"
        
        message_text += "\n"
    
    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("contacts_by_city")]]),
        parse_mode="HTML"
    )

@contacts_router.callback_query(F.data == "contacts_all")
async def show_all_contacts(callback: CallbackQuery, state: FSMContext):
    """Показать все контакты пользователей"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    users = await current_db.get_all_users()
    
    if not users:
        await callback.message.edit_text(
            "👥 Пользователи не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("view_contacts")]]), parse_mode="HTML"
        )
        return
    
    # Группируем по городам и магазинам
    cities_data = {}
    for user in users:
        city = user[9]  # city
        shop_name = user[8]  # shop_name
        telegram_id = user[1] # telegram_id
        
        if city not in cities_data:
            cities_data[city] = {}
        if shop_name not in cities_data[city]:
            cities_data[city][shop_name] = []
        cities_data[city][shop_name].append(user)
    
    _LIMIT = 3800
    message_text = "👥 Все контакты пользователей\n\n"
    truncated = False

    for city_name, shops in sorted(cities_data.items()):
        city_block = f"🏙️ {he(city_name)}:\n"
        for shop_name, shop_users in sorted(shops.items()):
            city_block += f"  🏪 {he(shop_name)}: {len(shop_users)} чел.\n"
            for user in shop_users:
                user_id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop, city, timezone, created_at = user[:12]
                full_name = f"{he(first_name)} {he(last_name)}"
                if middle_name:
                    full_name += f" {he(middle_name)}"
                line = f"    👤 {full_name} [<a href='tg://user?id={telegram_id}'>Написать</a>]"
                if phone:
                    line += f" | 📞 {phone}"
                line += "\n"
                city_block += line
        city_block += "\n"
        if len((message_text + city_block).encode('utf-8')) > _LIMIT:
            truncated = True
            break
        message_text += city_block

    if truncated:
        message_text += "…\n<i>Список слишком длинный — показаны не все пользователи.</i>\n"

    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("view_contacts")]]),
        parse_mode="HTML"
    )