"""
Обработчики для управления остатками (административные функции)
"""

import os
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from database import Database
from keyboards import back_button, inventory_menu, safe_cb, resolve_cb_name
from message_utils import safe_edit_message, fsm_edit
from states import InventoryStates, SearchStates
from utils import format_currency, get_stock_color_indicator, he
from env_manager import env_manager

inventory_router = Router()

from db_utils import get_db, clear_state_keep_org, is_any_admin

@inventory_router.callback_query(F.data == "manage_inventory")
async def manage_inventory_callback(callback: CallbackQuery, state: FSMContext):
    """Меню управления остатками для админа"""
    current_db = await get_db(callback.from_user.id, state)
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)

    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    from keyboards import inventory_menu
    await callback.message.edit_text(
        "📦 <b>Управление остатками</b>\n\nВыберите действие:",
        reply_markup=inventory_menu(),
        parse_mode="HTML"
    )

@inventory_router.callback_query(F.data == "add_inventory")
async def add_inventory_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления остатков (выбор магазина)"""
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)

    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    
    # Для супер-админа показываем все магазины, для обычного админа - только его
    if is_super:
        shops = current_db.get_all_shops()
    else:
        user = current_db.get_user(callback.from_user.id)
        if user and user[8]:
            shops = [user[8]]  # Только магазин текущего пользователя
        else:
            shops = []
    
    if not shops:
        await callback.message.edit_text(
            "❌ Магазин не найден.\n\nУкажите магазин в своём профиле через 👤 Мой профиль.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("manage_inventory")]])
        )
        return
    
    # Если только один магазин - сразу переходим к выбору товара
    if len(shops) == 1:
        shop_name = shops[0]
        await state.update_data(add_inventory_shop=shop_name, action="add_inventory")
        
        products = current_db.get_all_products()
        if not products:
            await callback.message.edit_text(
                "📦 Товары отсутствуют. Сначала добавьте товары.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("manage_inventory")]])
            )
            return
        
        await state.update_data(add_inventory_shop=shop_name, anchor_msg_id=callback.message.message_id)
        await _show_inv_product_list(callback.message, shop_name, products)
        return
    
    await state.update_data(action="add_inventory", anchor_msg_id=callback.message.message_id)
    await _show_inv_shop_list(callback, shops)


async def _show_inv_shop_list(callback, shops, query=""):
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍 Найти магазин", callback_data="inv_srch_shop_start"))
    for shop in filtered:
        builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("add_inv_shop_", shop)))
    if query:
        builder.add(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="add_inventory"))
    builder.add(back_button("manage_inventory"))
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await callback.message.edit_text(
        f"🏪 Выберите магазин для добавления остатков:{suffix}",
        reply_markup=builder.as_markup()
    )


@inventory_router.callback_query(F.data == "inv_srch_shop_start")
async def inv_srch_shop_start(callback: CallbackQuery, state: FSMContext):
    if not (is_any_admin(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.shop_inventory)
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="add_inventory"))
    await callback.message.edit_text(
        "🔍 <b>Поиск магазина</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@inventory_router.message(SearchStates.shop_inventory)
async def inv_srch_shop_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    is_super = env_manager.is_super_admin(message.from_user.id)
    current_db = await get_db(message.from_user.id, state)
    if is_super:
        shops = current_db.get_all_shops()
    else:
        user = current_db.get_user(message.from_user.id)
        shops = [user[8]] if user and user[8] else []
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍 Найти магазин", callback_data="inv_srch_shop_start"))
    for shop in filtered:
        builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("add_inv_shop_", shop)))
    if query:
        builder.add(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="add_inventory"))
    builder.add(back_button("manage_inventory"))
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"🏪 Выберите магазин для добавления остатков:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )

@inventory_router.callback_query(F.data.startswith("add_inv_shop_"))
async def add_inventory_select_shop(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина для добавления остатков"""
    await callback.answer()
    shop_raw = callback.data.replace("add_inv_shop_", "")
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, current_db.get_all_shops() or [])
    await state.update_data(add_inventory_shop=shop_name)
    
    products = current_db.get_all_products()
    
    if not products:
        await callback.message.edit_text(
            "📦 Товары отсутствуют. Сначала добавьте товары.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("add_inventory")]])
        )
        return
    
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await _show_inv_product_list(callback.message, shop_name, products)


async def _show_inv_product_list(message, shop_name: str, products: list, query: str = ""):
    filtered = products
    if query:
        q = query.lower()
        filtered = [p for p in products if q in p[1].lower() or q in (p[2] or "").lower()]
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍 Найти товар", callback_data="inv_srch_prd_start"))
    for product in filtered:
        product_id = product[0]
        name = product[1]
        category = product[2] or "Без категории"
        builder.add(InlineKeyboardButton(
            text=f"{name} ({category})",
            callback_data=f"add_inv_product_{product_id}"
        ))
    if query:
        builder.add(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="inv_srch_prd_reset"))
    builder.add(back_button("add_inventory"))
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await message.edit_text(
        f"🏪 Магазин: {shop_name}\n\n📦 Выберите товар для добавления остатков:{suffix}",
        reply_markup=builder.as_markup()
    )


@inventory_router.callback_query(F.data == "inv_srch_prd_start")
async def inv_srch_prd_start(callback: CallbackQuery, state: FSMContext):
    if not (is_any_admin(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.product_inventory)
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="inv_srch_prd_reset"))
    await callback.message.edit_text(
        "🔍 <b>Поиск товара</b>\n\nВведите название или категорию:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@inventory_router.callback_query(F.data == "inv_srch_prd_reset")
async def inv_srch_prd_reset(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    shop_name = data.get('add_inventory_shop', '')
    products = current_db.get_all_products()
    if not shop_name or not products:
        await callback.answer()
        await callback.message.edit_text(
            "📦 Товары отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("add_inventory")]])
        )
        return
    await _show_inv_product_list(callback.message, shop_name, products)
    await callback.answer()


@inventory_router.message(SearchStates.product_inventory)
async def inv_srch_prd_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    data = await state.get_data()
    shop_name = data.get('add_inventory_shop', '')
    products = current_db.get_all_products()
    q = query.lower()
    filtered = [p for p in products if q in p[1].lower() or q in (p[2] or "").lower()] if query else products
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍 Найти товар", callback_data="inv_srch_prd_start"))
    for product in filtered:
        pid = product[0]
        name = product[1]
        category = product[2] or "Без категории"
        builder.add(InlineKeyboardButton(
            text=f"{name} ({category})",
            callback_data=f"add_inv_product_{pid}"
        ))
    if query:
        builder.add(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="inv_srch_prd_reset"))
    builder.add(back_button("add_inventory"))
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        f"🏪 Магазин: {he(shop_name)}\n\n📦 Выберите товар для добавления остатков:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )

@inventory_router.callback_query(F.data.startswith("add_inv_product_"))
async def add_inventory_select_product(callback: CallbackQuery, state: FSMContext):
    """Выбор товара для добавления остатков"""
    product_id = int(callback.data.replace("add_inv_product_", ""))
    current_db = await get_db(callback.from_user.id, state)
    
    product = current_db.get_product(product_id)
    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return
    
    await callback.answer()
    data = await state.get_data()
    shop_name = data.get('add_inventory_shop')
    
    # Проверяем текущие остатки
    current_quantity = current_db.get_inventory(shop_name, product_id)
    if current_quantity is None:
        current_quantity = 0
    
    await state.update_data(add_inventory_product_id=product_id)
    
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"📦 Добавление остатков\n\n"
        f"🏷 Товар: {product[1]}\n"
        f"📂 Категория: {product[2]}\n"
        f"🏪 Магазин: {shop_name}\n"
        f"📊 Текущие остатки: {current_quantity} шт.\n\n"
        f"Введите новое количество остатков:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="add_inventory")]
        ])
    )
    await state.set_state(InventoryStates.adding_quantity)

@inventory_router.callback_query(F.data == "user_inventory_menu")
async def user_inventory_menu(callback: CallbackQuery, state: FSMContext):
    """Единое меню остатков для пользователей"""
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    
    if not user:
        # Пробуем найти в основной БД
        from database import Database as CentralDB
        central_db = CentralDB('data/main.db')
        user = central_db.get_user(callback.from_user.id)
        if user:
            from tenant_manager import tenant_manager
            # Если нашли в основной, используем путь к тенанту
            db_path = tenant_manager.get_user_db_path(callback.from_user.id)
            current_db = Database(db_path)
            # Принудительно проверяем существование таблиц
            current_db.create_tables()
            
    # Проверяем, является ли пользователь администратором
    is_admin = is_any_admin(callback.from_user.id)

    if not user:
        if is_admin:
            await callback.message.edit_text(
                "❌ Администратор не зарегистрирован в системе.\n\n"
                "Для работы с остатками нужно:\n"
                "1. Выполнить команду /start для регистрации\n"
                "2. Указать свой магазин в профиле\n\n"
                "Или используйте 'Упр. остатками' для всех магазинов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        else:
            await callback.message.edit_text(
                "❌ Сначала завершите регистрацию через /start",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        return
    
    user_shop = user[8]  # shop_name
    if not user_shop:
        if is_admin:
            await callback.message.edit_text(
                "❌ У администратора не указан магазин в профиле.\n\n"
                "Для работы с остатками своего магазина нужно:\n"
                "• Указать магазин в профиле (👤 Мой профиль)\n\n"
                "Или используйте 'Упр. остатками' для всех магазинов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        else:
            await callback.message.edit_text(
                "❌ У вас не указан магазин в профиле.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        return
    
    await callback.answer()
    # Меню остатков
    buttons = [
        [InlineKeyboardButton(text="👁️ Просмотр остатков", callback_data="user_inventory_view")],
        [InlineKeyboardButton(text="✏️ Редактировать остатки", callback_data="user_inventory_edit")],
        [back_button("main_menu")]
    ]
    
    await callback.message.edit_text(
        f"📦 Остатки в магазине '{user_shop}'\n\n"
        "Выберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@inventory_router.callback_query(F.data == "user_inventory_edit")
async def edit_inventory_user(callback: CallbackQuery, state: FSMContext):
    """Редактирование остатков пользователем в своем магазине"""
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)

    # Проверяем, является ли пользователь администратором
    is_admin = is_any_admin(callback.from_user.id)
    is_super = env_manager.is_super_admin(callback.from_user.id)
    
    if not user:
        if is_admin:
            await callback.message.edit_text(
                "❌ Администратор не зарегистрирован в системе.\n\n"
                "Для редактирования остатков администраторам нужно:\n"
                "1. Выполнить команду /start для регистрации\n"
                "2. Указать свой магазин в профиле\n\n"
                "Или используйте 'Упр. остатками' → 'Просмотр остатков' → выберите магазин",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        else:
            await callback.message.edit_text(
                "❌ Сначала завершите регистрацию через /start",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        return
    
    user_shop = user[8]  # shop_name

    # Для суп-админа: если shop_name дефолтный "Системный" — предлагаем выбор реального магазина
    if is_super and (not user_shop or user_shop == "Системный"):
        inv_shops = current_db.get_inventory_shops()
        if inv_shops:
            if len(inv_shops) == 1:
                user_shop = inv_shops[0]
            else:
                builder = InlineKeyboardBuilder()
                for shop in sorted(inv_shops):
                    builder.add(InlineKeyboardButton(
                        text=f"🏪 {shop}",
                        callback_data=safe_cb("inv_edit_shop_", shop)
                    ))
                builder.add(back_button("main_menu"))
                builder.adjust(1)
                await callback.message.edit_text(
                    "📦 Изменение остатков\n\nВыберите магазин:",
                    reply_markup=builder.as_markup()
                )
                return

    if not user_shop:
        if is_admin:
            await callback.message.edit_text(
                "❌ У администратора не указан магазин в профиле.\n\n"
                "Для редактирования остатков в своем магазине нужно:\n"
                "• Указать магазин в профиле (👤 Мой профиль)\n\n"
                "Или используйте 'Упр. остатками' для управления всеми магазинами",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        else:
            await callback.message.edit_text(
                "❌ У вас не указан магазин в профиле.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        return
    
    await callback.answer()
    await _show_edit_inventory_categories(callback, state, current_db, user_shop)

@inventory_router.callback_query(F.data.startswith("edit_inv_category_"))
async def edit_inventory_category_selected(callback: CallbackQuery, state: FSMContext):
    """Отображение товаров выбранной категории для редактирования"""
    category_raw = callback.data.replace("edit_inv_category_", "")
    current_db_tmp = await get_db(callback.from_user.id, state)
    category = resolve_cb_name(category_raw, current_db_tmp.get_all_categories() or [])
    
    data = await state.get_data()
    user_shop = data.get('user_shop')
    
    if not user_shop:
        await callback.answer("❌ Ошибка: магазин не найден!", show_alert=True)
        return
    
    # Получаем все товары выбранной категории
    current_db = await get_db(callback.from_user.id, state)
    all_products = current_db.get_all_products()
    category_products = []
    
    for product in all_products:
        # product содержит: (id, name, category, price)
        prod_category = product[2]
        if prod_category == category:
            product_id = product[0]
            product_name = product[1]
            price = product[3]
            # Проверяем текущие остатки для этого товара в магазине
            quantity = current_db.get_inventory(user_shop, product_id)
            if quantity is None:
                quantity = 0
            category_products.append((product_name, quantity, price, product_id))
    
    if not category_products:
        await callback.answer("❌ В этой категории нет товаров!", show_alert=True)
        return

    await callback.answer()
    # Создаем кнопки для товаров
    builder = InlineKeyboardBuilder()
    
    for product_name, quantity, price, product_id in sorted(category_products):
        builder.add(InlineKeyboardButton(
            text=f"{product_name} ({quantity} шт.)",
            callback_data=f"edit_inv_product_{product_id}"
        ))
    
    # Кнопка назад к категориям
    builder.add(InlineKeyboardButton(
        text="⬅️ К категориям", 
        callback_data="user_inventory_edit"
    ))
    builder.adjust(1)
    
    await callback.message.edit_text(
        f"📦 Категория: {category}\n"
        f"🏪 Магазин: {user_shop}\n\n"
        f"Выберите товар для изменения количества:",
        reply_markup=builder.as_markup()
    )

@inventory_router.callback_query(F.data.startswith("edit_inv_product_"))
async def edit_inventory_item(callback: CallbackQuery, state: FSMContext):
    """Выбор товара для изменения остатков пользователем"""
    product_id = int(callback.data.replace("edit_inv_product_", ""))
    
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    user_shop = user[8]

    # Получаем информацию о товаре
    product = current_db.get_product(product_id)
    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return

    await callback.answer()
    # Получаем текущие остатки
    current_quantity = current_db.get_inventory(user_shop, product_id)
    if current_quantity is None:
        current_quantity = 0
    
    product_name = product[1]
    category = product[2]
    
    # Сохраняем данные для редактирования
    await state.update_data(
        edit_product_id=product_id,
        edit_shop=user_shop,
        current_quantity=current_quantity,
        product_name=product_name,
        category=category
    )
    
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"📦 Изменение остатков\n\n"
        f"🏷 Товар: {product_name}\n"
        f"📂 Категория: {category}\n"
        f"🏪 Магазин: {user_shop}\n"
        f"📊 Текущие остатки: {current_quantity} шт.\n\n"
        f"Введите новое количество:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit_inventory")]
        ])
    )
    
    await state.set_state(InventoryStates.editing_quantity)

# ДОБАВЛЕН НОВЫЙ ОБРАБОТЧИК ДЛЯ ОТМЕНЫ РЕДАКТИРОВАНИЯ
@inventory_router.callback_query(F.data == "cancel_edit_inventory")
async def cancel_edit_inventory(callback: CallbackQuery, state: FSMContext):
    """Отмена редактирования остатков"""
    await clear_state_keep_org(state)

    # Возвращаемся к меню редактирования остатков
    await edit_inventory_user(callback, state)

@inventory_router.message(InventoryStates.adding_quantity)
@inventory_router.message(InventoryStates.editing_quantity)
async def process_new_quantity(message: Message, state: FSMContext):
    """Единый обработчик для изменения или добавления остатков"""
    current_db = await get_db(message.from_user.id, state)
    _add_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("manage_inventory")]])
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("user_inventory_edit")]])
    try:
        new_quantity = int(message.text)
        if new_quantity < 0:
            await fsm_edit(state, message, "❌ Количество не может быть отрицательным! Введите снова:",
                           reply_markup=_edit_kb)
            return
    except ValueError:
        await fsm_edit(state, message, "❌ Введите корректное целое число!", reply_markup=_edit_kb)
        return
    
    data = await state.get_data()
    action = data.get('action')
    user_id = current_db.get_user_id(message.from_user.id)
    
    if action == "add_inventory":
        product_id = data.get('add_inventory_product_id')
        shop_name = data.get('add_inventory_shop')
        
        if product_id is None or shop_name is None:
            await fsm_edit(state, message, "❌ Ошибка: данные не найдены!", reply_markup=_add_kb)
            await clear_state_keep_org(state)
            return
            
        current_db.add_inventory(shop_name, product_id, new_quantity, user_id, 'manual', 'Добавление остатков')

        _gs_status_add = []
        try:
            from integration.manager import integration_manager as _int_mgr
            from datetime import datetime as _dt
            _product = current_db.get_product(product_id)
            _gs_status_add = await _int_mgr.trigger_export_with_result(current_db, 'inventory', {
                'shop_name': shop_name,
                'product_name': _product[1] if _product else '',
                'category': _product[2] if _product else '',
                'quantity': new_quantity,
                'last_updated': _dt.now().strftime('%Y-%m-%d %H:%M'),
            })
        except Exception as _ie:
            import logging as _log
            _log.warning(f"integration trigger_export_with_result (inventory add): {_ie}")

        _inv_add_text = f"✅ Остатки обновлены для магазина {shop_name}!"
        if _gs_status_add:
            if all(r['success'] for r in _gs_status_add):
                _inv_add_text += "\n📋 Google Таблицы: ✅ Записано"
            else:
                _inv_add_text += "\n📋 Google Таблицы: ⚠️ Ошибка записи"
        await fsm_edit(state, message, _inv_add_text, reply_markup=_add_kb)
    else:
        product_id = data.get('edit_product_id')
        shop_name = data.get('edit_shop')
        current_quantity = data.get('current_quantity', 0)
        product_name = data.get('product_name')
        category = data.get('category')
        
        if product_id is None or shop_name is None:
            await fsm_edit(state, message, "❌ Ошибка: не найдены данные для редактирования!", reply_markup=_edit_kb)
            await clear_state_keep_org(state)
            return
        
        try:
            existing_quantity = current_db.get_inventory(shop_name, product_id)
            if existing_quantity is None:
                current_db.add_inventory(shop_name, product_id, new_quantity, user_id, 'user_edit', f'Установка остатков: {new_quantity} шт.')
            else:
                delta = new_quantity - existing_quantity
                change_reason = f'Изменение с {existing_quantity} на {new_quantity} шт. ({"+" if delta > 0 else ""}{delta})'
                current_db.update_inventory(shop_name, product_id, delta, user_id, 'user_edit', change_reason)
            
            _gs_status_edit = []
            try:
                from integration.manager import integration_manager as _int_mgr
                from datetime import datetime as _dt
                _gs_status_edit = await _int_mgr.trigger_export_with_result(current_db, 'inventory', {
                    'shop_name': shop_name,
                    'product_name': product_name or '',
                    'category': category or '',
                    'quantity': new_quantity,
                    'last_updated': _dt.now().strftime('%Y-%m-%d %H:%M'),
                })
            except Exception as _ie:
                import logging as _log
                _log.warning(f"integration trigger_export_with_result (inventory edit): {_ie}")

            _inv_edit_text = (
                f"✅ Остатки обновлены!\n\n"
                f"🏷 Товар: {product_name}\n"
                f"📂 Категория: {category}\n"
                f"🏪 Магазин: {shop_name}\n"
                f"📊 Было: {current_quantity} шт.\n"
                f"📊 Стало: {new_quantity} шт."
            )
            if _gs_status_edit:
                if all(r['success'] for r in _gs_status_edit):
                    _inv_edit_text += "\n📋 Google Таблицы: ✅ Записано"
                else:
                    _inv_edit_text += "\n📋 Google Таблицы: ⚠️ Ошибка записи"
            await fsm_edit(state, message, _inv_edit_text, reply_markup=_edit_kb)
        except Exception as e:
            await fsm_edit(state, message, f"❌ Ошибка при обновлении остатков: {str(e)}", reply_markup=_edit_kb)
    
    await clear_state_keep_org(state)

async def _show_edit_inventory_categories(callback: CallbackQuery, state: FSMContext, current_db, user_shop: str):
    """Вспомогательная функция: показывает категории для редактирования остатков"""
    all_products = current_db.get_all_products()
    if not all_products:
        await callback.message.edit_text(
            f"📦 Остатки в магазине '{user_shop}'\n\n❌ Товары не найдены.\n\nОбратитесь к администратору для добавления товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
        )
        return
    categories = set()
    for product in all_products:
        categories.add(product[2])
    await state.update_data(user_shop=user_shop, action="edit_inventory")
    builder = InlineKeyboardBuilder()
    for category in sorted(categories):
        builder.add(InlineKeyboardButton(
            text=f"📂 {category}",
            callback_data=safe_cb("edit_inv_category_", category)
        ))
    builder.add(back_button("main_menu"))
    builder.adjust(2, 1)
    await callback.message.edit_text(
        f"📦 Изменение остатков - {user_shop}\n\nВыберите категорию:",
        reply_markup=builder.as_markup()
    )


async def _show_view_inventory_categories(callback: CallbackQuery, state: FSMContext, current_db, user_shop: str):
    """Вспомогательная функция: показывает категории для просмотра остатков"""
    all_products = current_db.get_all_products()
    if not all_products:
        await callback.message.edit_text(
            f"📦 Остатки в магазине '{user_shop}'\n\n❌ Товары не найдены.\n\nОбратитесь к администратору для добавления товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
        )
        return
    categories = set()
    total_items = 0
    low_stock_count = 0
    for product in all_products:
        category = product[2]
        product_id = product[0]
        quantity = current_db.get_inventory(user_shop, product_id)
        if quantity is None:
            quantity = 0
        categories.add(category)
        total_items += quantity
        if quantity < 2:
            low_stock_count += 1
    await state.update_data(user_shop=user_shop, action="view_inventory")
    builder = InlineKeyboardBuilder()
    for category in sorted(categories):
        builder.add(InlineKeyboardButton(
            text=f"📂 {category}",
            callback_data=safe_cb("view_user_category_", category)
        ))
    builder.add(back_button("user_inventory_menu"))
    builder.adjust(2, 1)
    message_text = f"📦 Остатки - {user_shop}\n\n"
    message_text += f"📊 Всего товаров: {total_items} шт.\n"
    if low_stock_count > 0:
        message_text += f"⚠️ Низкие остатки: {low_stock_count} позиций\n"
    message_text += "\nВыберите категорию:"
    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup()
    )


@inventory_router.callback_query(F.data.startswith("inv_edit_shop_"))
async def inv_edit_shop_selected(callback: CallbackQuery, state: FSMContext):
    """Суп-адмін выбрал магазин для редактирования остатков"""
    await callback.answer()
    shop_raw = callback.data.replace("inv_edit_shop_", "")
    current_db = await get_db(callback.from_user.id, state)
    user_shop = resolve_cb_name(shop_raw, current_db.get_inventory_shops() or [])
    await _show_edit_inventory_categories(callback, state, current_db, user_shop)


@inventory_router.callback_query(F.data.startswith("inv_view_shop_"))
async def inv_view_shop_selected(callback: CallbackQuery, state: FSMContext):
    """Суп-админ выбрал магазин для просмотра остатков"""
    await callback.answer()
    shop_raw = callback.data.replace("inv_view_shop_", "")
    current_db = await get_db(callback.from_user.id, state)
    user_shop = resolve_cb_name(shop_raw, current_db.get_inventory_shops() or [])
    await _show_view_inventory_categories(callback, state, current_db, user_shop)


@inventory_router.callback_query(F.data == "user_inventory_view")
async def user_inventory_view(callback: CallbackQuery, state: FSMContext):
    """Просмотр остатков пользователем в своем магазине"""
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)

    # Проверяем, является ли пользователь администратором
    is_admin = is_any_admin(callback.from_user.id)
    is_super = env_manager.is_super_admin(callback.from_user.id)
    
    if not user:
        if is_admin:
            await callback.message.edit_text(
                "❌ Администратор не зарегистрирован в системе.\n\n"
                "Для просмотра остатков своего магазина нужно:\n"
                "1. Выполнить команду /start для регистрации\n"
                "2. Указать свой магазин в профиле\n\n"
                "Или используйте 'Упр. остатками' → 'Просмотр остатков' → выберите магазин",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        else:
            await callback.message.edit_text(
                "❌ Сначала завершите регистрацию через /start",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        return
    
    user_shop = user[8]  # shop_name

    # Для суп-админа: если shop_name дефолтный "Системный" — предлагаем выбор реального магазина
    if is_super and (not user_shop or user_shop == "Системный"):
        inv_shops = current_db.get_inventory_shops()
        if inv_shops:
            if len(inv_shops) == 1:
                user_shop = inv_shops[0]
            else:
                builder = InlineKeyboardBuilder()
                for shop in sorted(inv_shops):
                    builder.add(InlineKeyboardButton(
                        text=f"🏪 {shop}",
                        callback_data=safe_cb("inv_view_shop_", shop)
                    ))
                builder.add(back_button("user_inventory_menu"))
                builder.adjust(1)
                await callback.message.edit_text(
                    "📦 Просмотр остатков\n\nВыберите магазин:",
                    reply_markup=builder.as_markup()
                )
                return

    if not user_shop:
        if is_admin:
            await callback.message.edit_text(
                "❌ У администратора не указан магазин в профиле.\n\n"
                "Для просмотра остатков своего магазина нужно:\n"
                "• Указать магазин в профиле (👤 Мой профиль)\n\n"
                "Или используйте 'Упр. остатками' для всех магазинов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        else:
            await callback.message.edit_text(
                "❌ У вас не указан магазин в профиле.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
            )
        return
    
    await callback.answer()
    await _show_view_inventory_categories(callback, state, current_db, user_shop)

@inventory_router.callback_query(F.data.startswith("view_user_category_"))
async def view_user_category_items(callback: CallbackQuery, state: FSMContext):
    """Отображение товаров выбранной категории для пользователя"""
    category_raw = callback.data.replace("view_user_category_", "")
    current_db_tmp = await get_db(callback.from_user.id, state)
    category = resolve_cb_name(category_raw, current_db_tmp.get_all_categories() or [])
    
    data = await state.get_data()
    user_shop = data.get('user_shop')
    
    if not user_shop:
        await callback.answer("❌ Ошибка: магазин не найден!", show_alert=True)
        return
    
    current_db = await get_db(callback.from_user.id, state)
    # Получаем все остатки для магазина пользователя с полной информацией
    inventory = current_db.get_all_inventory(user_shop)
    category_products = []
    
    for item in inventory:
        # item содержит: (id, shop_name, product_id, quantity, last_updated, updated_by, change_type, change_reason, product_name, category, price, updated_by_name)
        if len(item) >= 12:
            prod_category = item[9]
            if prod_category == category:
                product_name = item[8]
                quantity = item[3]
                price = item[10]
                category_products.append((product_name, quantity, price))
    
    if not category_products:
        # Если в get_all_inventory ничего не нашли (товар только добавлен и остатков еще нет), 
        # ищем в get_all_products
        all_products = current_db.get_all_products()
        for product in all_products:
            if product[2] == category:
                product_id = product[0]
                product_name = product[1]
                price = product[3]
                # Проверяем остатки
                quantity = current_db.get_inventory(user_shop, product_id) or 0
                category_products.append((product_name, quantity, price))

    if not category_products:
        await callback.answer("❌ В этой категории нет товаров!", show_alert=True)
        return
    
    await callback.answer()
    message_text = f"📦 Категория: {category}\n"
    message_text += f"🏪 Магазин: {user_shop}\n\n"
    
    for name, qty, price in sorted(category_products):
        indicator = get_stock_color_indicator(qty)
        message_text += f"{indicator} <b>{he(name)}</b>: {qty} шт.\n"
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="user_inventory_view"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
