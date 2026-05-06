"""Обработчики для продаж и управления остатками: добавлено отображение мотивации при продаже товара и предварительный расчет мотивации при подтверждении продажи."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Database
from env_manager import env_manager
from keyboards import main_menu, inventory_menu, back_button, create_selection_keyboard
from states import SaleStates, InventoryStates, EditSaleStates, MultipleSaleStates, QuickSaleStates, ExcelImportStates
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_SALES
from utils import format_currency, get_stock_color_indicator, format_date_display, he

# Создаем роутер для продаж
sales_router = Router()

from db_utils import get_db, clear_state_keep_org, is_any_admin
from keyboards import safe_cb, resolve_cb_name
from message_utils import fsm_edit, delete_message_safe

# ПРОДАЖИ

def _make_qty_keyboard(max_qty: int) -> InlineKeyboardMarkup:
    """Клавиатура быстрого выбора количества (кнопки + ввод вручную)"""
    common = [1, 2, 3, 5, 10, 20, 50]
    available = [q for q in common if q <= max_qty]
    rows = []
    # Кнопки количества, по 4 в строку
    for i in range(0, len(available), 4):
        rows.append([
            InlineKeyboardButton(text=str(q), callback_data=f"sq_qty_{q}")
            for q in available[i:i+4]
        ])
    rows.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="sq_qty_manual")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _show_sale_categories(callback: CallbackQuery, state: FSMContext, current_db, shop_name: str):
    """Вспомогательная функция: показывает категории для выбора товара в продаже"""
    await state.update_data(shop_name=shop_name, sale_cart=[])

    categories = current_db.get_all_categories()

    if not categories:
        await callback.message.edit_text(
            "📋 Товары отсутствуют.\n\nОбратитесь к администратору для добавления товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
        )
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔍 Найти товар", callback_data="sale_quick_search"))

    # Блок избранных товаров
    try:
        user_row = current_db.get_user(callback.from_user.id)
        if user_row:
            u_db_id = user_row[0]
            fav_ids = current_db.get_favorite_products(u_db_id)
            if fav_ids:
                fav_products = [current_db.get_product(pid) for pid in fav_ids[:5]]
                fav_products = [p for p in fav_products if p]
                if fav_products:
                    builder.row(InlineKeyboardButton(text="⭐ Избранное", callback_data="pg_noop"))
                    for p in fav_products:
                        builder.row(InlineKeyboardButton(
                            text=f"⭐ {p[1]}",
                            callback_data=f"sale_product_{p[0]}"
                        ))

            # Блок недавних продаж
            recent = current_db.get_user_recent_products(u_db_id, limit=5)
            if recent:
                builder.row(InlineKeyboardButton(text="🔄 Недавние", callback_data="pg_noop"))
                for pid, pname, pprice, pcat in recent:
                    builder.row(InlineKeyboardButton(
                        text=f"🔄 {pname}",
                        callback_data=f"sale_product_{pid}"
                    ))
    except Exception:
        pass

    # Категории по 2 в строку
    products = current_db.get_all_products()
    has_no_category = any(not p[2] or p[2] == "Без категории" for p in products)
    cat_buttons = [
        InlineKeyboardButton(text=f"📂 {cat}", callback_data=safe_cb("sale_category_", cat))
        for cat in categories
    ]
    if has_no_category:
        cat_buttons.append(InlineKeyboardButton(text="📁 Без категории", callback_data="sale_category_Без категории"))
    for i in range(0, len(cat_buttons), 2):
        builder.row(*cat_buttons[i:i+2])

    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale"))

    await callback.message.edit_text(
        f"🛒 Новая продажа <b>[{he(shop_name)}]</b>\n\n"
        f"🔍 Найдите товар по названию или выберите категорию:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(MultipleSaleStates.adding_items)


@sales_router.callback_query(F.data == "new_sale")
async def start_sale(callback: CallbackQuery, state: FSMContext):
    """Начало новой продажи с корзиной"""
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return
    await callback.answer()
        
    current_db = await get_db(callback.from_user.id, state)
    current_db.create_tables()
    
    is_super = env_manager.is_super_admin(callback.from_user.id)
    
    user_data = current_db.get_user(callback.from_user.id)
    
    # Если супер-админ, создаем запись в БД если её нет
    if is_super and not user_data:
        current_db.add_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "Admin",
            last_name=callback.from_user.last_name or "",
            shop_name=None,
            trade_network=None,
            city=None,
            phone="000"
        )
        user_data = current_db.get_user(callback.from_user.id)

    if not user_data:
        await callback.message.edit_text(
            "❌ Сначала завершите регистрацию через /start",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Закрыть", callback_data="main_menu")]])
        )
        return

    shop_name = user_data[8]

    # Для суп-админа: если shop_name пустой или дефолтный "Системный",
    # предлагаем выбрать реальный магазин из организации
    if is_super and (not shop_name or shop_name == "Системный"):
        inv_shops = current_db.get_inventory_shops()
        if inv_shops:
            if len(inv_shops) == 1:
                shop_name = inv_shops[0]
            else:
                builder = InlineKeyboardBuilder()
                for shop in sorted(inv_shops):
                    builder.add(InlineKeyboardButton(
                        text=f"🏪 {shop}",
                        callback_data=safe_cb("sale_shop_", shop)
                    ))
                builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale"))
                builder.adjust(1)
                await callback.message.edit_text(
                    "🛒 Новая продажа\n\nВыберите магазин для совершения продажи:",
                    reply_markup=builder.as_markup()
                )
                return

    await _show_sale_categories(callback, state, current_db, shop_name)


@sales_router.callback_query(F.data.startswith("sale_shop_"))
async def select_sale_shop(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина суп-админом для продажи"""
    await callback.answer()
    shop_raw = callback.data.replace("sale_shop_", "")
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, current_db.get_inventory_shops() or [])
    await _show_sale_categories(callback, state, current_db, shop_name)


@sales_router.callback_query(F.data == "sale_quick_search")
async def quick_search_start(callback: CallbackQuery, state: FSMContext):
    """Запуск быстрого поиска товара по названию"""
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🔍 <b>Быстрый поиск товара</b>\n\n"
        "Введите название или часть названия товара:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(QuickSaleStates.searching_product)


@sales_router.message(QuickSaleStates.searching_product)
async def process_quick_search(message: Message, state: FSMContext):
    """Поиск товара по введённому запросу — показывает товары в наличии"""
    query = (message.text or "").strip()
    if not query:
        await fsm_edit(
            state, message,
            "🔍 Введите название товара:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale")]
            ])
        )
        return

    current_db = await get_db(message.from_user.id, state)
    data = await state.get_data()
    shop_name = data.get("shop_name", "")

    all_products = current_db.get_all_products()
    query_lower = query.lower()

    matching = []
    for p in all_products:
        pid, name, category, price = p[0], p[1], p[2], p[3]
        qty = current_db.get_inventory(shop_name, pid)
        if qty > 0 and query_lower in name.lower():
            matching.append((pid, name, category or "Без категории", price, qty))

    if not matching:
        await fsm_edit(
            state, message,
            f"🔍 По запросу «{he(query)}» ничего не найдено в наличии.\n\n"
            f"Попробуйте другой запрос:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Искать снова", callback_data="sale_quick_search")],
                [InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale")]
            ]),
            parse_mode="HTML"
        )
        return

    shown = matching[:15]
    rows = []
    for pid, name, category, price, qty in shown:
        color = get_stock_color_indicator(qty)
        rows.append([InlineKeyboardButton(
            text=f"{color} {name} — {qty} шт. × {price:.0f}₽",
            callback_data=f"sale_product_{pid}"
        )])

    extra = f"\n<i>(показаны первые 15 из {len(matching)})</i>" if len(matching) > 15 else ""
    rows.append([InlineKeyboardButton(text="🔍 Искать снова", callback_data="sale_quick_search")])
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale")])

    await fsm_edit(
        state, message,
        f"🔍 <b>Результаты поиска</b> «{he(query)}»{extra}\n\n"
        f"Найдено: {len(matching)} — выберите товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data.startswith("sale_category_"))
async def select_sale_category(callback: CallbackQuery, state: FSMContext):
    """Выбор категории для продажи"""
    await callback.answer()
    category_raw = callback.data.replace("sale_category_", "")
    current_db = await get_db(callback.from_user.id, state)
    category = resolve_cb_name(category_raw, current_db.get_all_categories() or [])

    # Получаем товары этой категории
    products = current_db.get_all_products()
    category_products = [p for p in products if p[2] == category]

    if not category_products:
        await callback.message.edit_text(
            "❌ В этой категории нет товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="new_sale")]])
        )
        return

    data = await state.get_data()
    shop_name = data['shop_name']

    builder = InlineKeyboardBuilder()
    available_products = []

    for product in category_products:
        product_id = product[0]
        name = product[1]
        price = product[3]
        quantity = current_db.get_inventory(shop_name, product_id)

        if quantity > 0:
            color = get_stock_color_indicator(quantity)
            builder.add(InlineKeyboardButton(
                text=f"{color} {name} - {quantity} шт. × {price}₽",
                callback_data=f"sale_product_{product_id}"
            ))
            available_products.append(product)

    if not available_products:
        await callback.message.edit_text(
            f"📂 {category}\n\n❌ Нет товаров в наличии в вашем магазине.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale")]
            ])
        )
        return

    builder.add(InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale"))
    builder.adjust(1)

    await callback.message.edit_text(
        f"📂 {category}\n\nВыберите товар для продажи:",
        reply_markup=builder.as_markup()
    )

@sales_router.callback_query(F.data.startswith("sale_product_"))
async def select_sale_product(callback: CallbackQuery, state: FSMContext):
    """Выбор товара для продажи"""
    product_id = int(callback.data.replace("sale_product_", ""))
    current_db = await get_db(callback.from_user.id, state)

    product = current_db.get_product(product_id)

    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return

    data = await state.get_data()
    shop_name = data['shop_name']
    quantity = current_db.get_inventory(shop_name, product_id)

    if quantity <= 0:
        await callback.answer("❌ Товар отсутствует в наличии!", show_alert=True)
        return

    await callback.answer()
    await state.update_data(product_id=product_id)

    # Получаем информацию о мотивации для товара
    motivation_info = current_db.get_product_motivation(product_id)
    motivation_text = ""
    if motivation_info:
        if motivation_info['motivation_type'] == 'percentage':
            motivation_text = f"\n🎯 Мотивация: {motivation_info['motivation_value']}% от продажи"
        else:
            motivation_text = f"\n🎯 Мотивация: {format_currency(motivation_info['motivation_value'])} за шт."
    else:
        motivation_text = "\n🎯 Мотивация: не установлена"

    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"💰 Продажа товара:\n\n"
        f"🏷 {he(product[1])}\n"
        f"💰 Цена: {format_currency(product[3])}\n"
        f"📦 В наличии: {quantity} шт.{motivation_text}\n\n"
        f"Выберите количество или введите вручную:",
        reply_markup=_make_qty_keyboard(quantity),
        parse_mode="HTML"
    )
    await state.set_state(SaleStates.entering_quantity)

@sales_router.message(SaleStates.entering_quantity)
async def process_sale_quantity(message: Message, state: FSMContext):
    """Обработка количества для продажи"""
    try:
        current_db = await get_db(message.from_user.id, state)

        quantity = int(message.text.strip() if message.text else "0")

        if quantity <= 0:
            await fsm_edit(state, message, "📦 ❌ Количество должно быть больше нуля! Введите снова:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))
            return

        data = await state.get_data()
        shop_name = data['shop_name']
        product_id = data['product_id']

        current_quantity = current_db.get_inventory(shop_name, product_id)

        if quantity > current_quantity:
            await fsm_edit(state, message,
                           f"📦 ❌ Недостаточно товара! В наличии: {current_quantity} шт. Введите меньшее количество:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))
            return

        # Сохраняем количество и показываем опции цены
        await state.update_data(quantity=quantity)

        product = current_db.get_product(product_id)
        default_price = product[3]

        # Получаем информацию о мотивации и рассчитываем предварительную сумму
        motivation_info = current_db.get_product_motivation(product_id)
        motivation_text = ""
        expected_earning = 0
        
        if motivation_info:
            if motivation_info['motivation_type'] == 'percentage':
                expected_earning = (default_price * quantity) * (motivation_info['motivation_value'] / 100)
                motivation_text = f"\n🎯 Мотивация: {motivation_info['motivation_value']}% = {format_currency(expected_earning)}"
            else:
                expected_earning = motivation_info['motivation_value'] * quantity
                motivation_text = f"\n🎯 Мотивация: {format_currency(motivation_info['motivation_value'])} × {quantity} = {format_currency(expected_earning)}"
        else:
            motivation_text = "\n🎯 Мотивация: не установлена"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💰 Стандартная цена ({format_currency(default_price)})", callback_data="use_default_price")],
            [InlineKeyboardButton(text="✏️ Указать свою цену", callback_data="custom_price")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]
        ])

        await fsm_edit(
            state, message,
            f"💰 Выберите цену для продажи:\n\n"
            f"🏷 {product[1]}\n"
            f"📦 Количество: {quantity} шт.\n"
            f"💰 Стандартная цена: {format_currency(default_price)}{motivation_text}\n\n"
            f"Выберите вариант:",
            reply_markup=keyboard,
        )

    except ValueError:
        await fsm_edit(state, message, "📦 ❌ Пожалуйста, введите целое число! Попробуйте снова:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))
    except Exception:
        await fsm_edit(state, message, "❌ Произошла ошибка при обработке продажи. Попробуйте ещё раз.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))


@sales_router.callback_query(F.data.startswith("sq_qty_"), SaleStates.entering_quantity)
async def quick_qty_select(callback: CallbackQuery, state: FSMContext):
    """Быстрый выбор количества кнопкой вместо ввода текста"""
    qty_str = callback.data.replace("sq_qty_", "")
    current_db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    shop_name = data.get("shop_name", "")
    product_id = data.get("product_id")

    if not product_id:
        await callback.answer("❌ Сессия устарела. Начните продажу заново.", show_alert=True)
        return

    # «Ввести вручную» — оставляем текстовый ввод, просто убираем кнопки
    if qty_str == "manual":
        await callback.answer()
        product = current_db.get_product(product_id)
        max_qty = current_db.get_inventory(shop_name, product_id)
        await callback.message.edit_text(
            f"✏️ <b>Введите количество вручную</b>\n\n"
            f"🏷 {he(product[1])}\n"
            f"📦 В наличии: {max_qty} шт.\n\n"
            f"Напишите число и отправьте:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]
            ]),
            parse_mode="HTML"
        )
        return

    try:
        quantity = int(qty_str)
    except ValueError:
        await callback.answer("❌ Неверное значение", show_alert=True)
        return

    max_qty = current_db.get_inventory(shop_name, product_id)
    if quantity > max_qty:
        await callback.answer(f"❌ В наличии только {max_qty} шт.", show_alert=True)
        return

    await callback.answer()
    product = current_db.get_product(product_id)
    default_price = product[3]

    motivation_info = current_db.get_product_motivation(product_id)
    motivation_text = ""
    if motivation_info:
        if motivation_info['motivation_type'] == 'percentage':
            earn = (default_price * quantity) * (motivation_info['motivation_value'] / 100)
            motivation_text = f"\n🎯 Мотивация: {motivation_info['motivation_value']}% = {format_currency(earn)}"
        else:
            earn = motivation_info['motivation_value'] * quantity
            motivation_text = f"\n🎯 Мотивация: {format_currency(motivation_info['motivation_value'])} × {quantity} = {format_currency(earn)}"
    else:
        motivation_text = "\n🎯 Мотивация: не установлена"

    await state.update_data(quantity=quantity)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Стандартная цена ({format_currency(default_price)})", callback_data="use_default_price")],
        [InlineKeyboardButton(text="✏️ Указать свою цену", callback_data="custom_price")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]
    ])

    await callback.message.edit_text(
        f"💰 <b>Выберите цену для продажи:</b>\n\n"
        f"🏷 {he(product[1])}\n"
        f"📦 Количество: {quantity} шт.\n"
        f"💰 Стандартная цена: {format_currency(default_price)}{motivation_text}\n\n"
        f"Выберите вариант:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data == "use_default_price")
async def use_default_price(callback: CallbackQuery, state: FSMContext):
    """Использование стандартной цены товара и добавление в корзину"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)

    data = await state.get_data()
    product_id = data.get('product_id')
    quantity = data.get('quantity')
    if not product_id or not quantity:
        await callback.message.edit_text(
            "❌ Сессия устарела. Начните продажу заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Новая продажа", callback_data="new_sale")]])
        )
        return
    product = current_db.get_product(product_id)

    if not product:
        await callback.message.edit_text(
            "❌ Товар не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К продажам", callback_data="new_sale")]])
        )
        return

    # Получаем информацию о мотивации и рассчитываем заработок
    motivation_info = current_db.get_product_motivation(product_id)
    expected_earning = 0
    motivation_text = ""
    
    if motivation_info:
        if motivation_info['motivation_type'] == 'percentage':
            expected_earning = (product[3] * quantity) * (motivation_info['motivation_value'] / 100)
            motivation_text = f"\n🎯 Ваша мотивация: {format_currency(expected_earning)}"
        else:
            expected_earning = motivation_info['motivation_value'] * quantity
            motivation_text = f"\n🎯 Ваша мотивация: {format_currency(expected_earning)}"

    # Добавляем товар в корзину с стандартной ценой
    cart_item = {
        'product_id': product_id,
        'product_name': product[1],
        'quantity': quantity,
        'price': product[3],  # Стандартная цена
        'total': quantity * product[3],
        'expected_earning': expected_earning
    }

    # Получаем корзину или инициализируем пустую
    sale_cart = data.get('sale_cart', [])
    sale_cart.append(cart_item)

    await state.update_data(sale_cart=sale_cart)

    # Показываем опции: добавить еще или завершить
    first_item = len(sale_cart) == 1
    builder = InlineKeyboardBuilder()
    if first_item:
        builder.add(InlineKeyboardButton(text="⚡ Продать сейчас", callback_data="complete_sale"))
    builder.add(
        InlineKeyboardButton(text="➕ Добавить еще товар", callback_data="add_more_items"),
        InlineKeyboardButton(text="🛒 Просмотр корзины", callback_data="view_cart"),
        InlineKeyboardButton(text="✅ Завершить продажу", callback_data="complete_sale"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")
    )
    builder.adjust(1)

    await callback.message.edit_text(
        f"✅ Товар добавлен в корзину!\n\n"
        f"🏷 {product[1]}\n"
        f"📦 Количество: {quantity} шт.\n"
        f"💰 Цена: {format_currency(product[3])}\n"
        f"💵 Сумма: {format_currency(quantity * product[3])}{motivation_text}\n\n"
        f"📋 В корзине товаров: {len(sale_cart)}\n\n"
        f"Что дальше?",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MultipleSaleStates.adding_items)

@sales_router.callback_query(F.data == "custom_price")
async def custom_price_start(callback: CallbackQuery, state: FSMContext):
    """Начало ввода пользовательской цены"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)

    data = await state.get_data()
    product_id = data['product_id']
    product = current_db.get_product(product_id)

    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"💰 Укажите цену для продажи:\n\n"
        f"🏷 {product[1]}\n"
        f"📦 Количество: {data['quantity']} шт.\n"
        f"💰 Стандартная цена: {format_currency(product[3])}\n\n"
        f"Введите новую цену (в рублях):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]
        ])
    )
    await state.set_state(SaleStates.entering_price)

@sales_router.message(SaleStates.entering_price)
async def process_custom_price(message: Message, state: FSMContext):
    """Обработка пользовательской цены и добавление в корзину"""
    try:
        current_db = await get_db(message.from_user.id, state)

        price = float(message.text.strip() if message.text else "0")
        if price <= 0:
            await fsm_edit(state, message, "💰 ❌ Цена должна быть больше нуля! Введите снова:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))
            return

        data = await state.get_data()
        product_id = data['product_id']
        quantity = data['quantity']
        product = current_db.get_product(product_id)

        if not product:
            await fsm_edit(state, message, "❌ Товар не найден! Попробуйте начать продажу заново.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))
            return

        # Получаем информацию о мотивации и рассчитываем заработок
        motivation_info = current_db.get_product_motivation(product_id)
        expected_earning = 0
        motivation_text = ""
        
        if motivation_info:
            if motivation_info['motivation_type'] == 'percentage':
                expected_earning = (price * quantity) * (motivation_info['motivation_value'] / 100)
                motivation_text = f"\n🎯 Ваша мотивация: {format_currency(expected_earning)}"
            else:
                expected_earning = motivation_info['motivation_value'] * quantity
                motivation_text = f"\n🎯 Ваша мотивация: {format_currency(expected_earning)}"

        # Добавляем товар в корзину
        cart_item = {
            'product_id': product_id,
            'product_name': product[1],
            'quantity': quantity,
            'price': price,
            'total': quantity * price,
            'expected_earning': expected_earning
        }

        # Получаем корзину или инициализируем пустую
        sale_cart = data.get('sale_cart', [])
        sale_cart.append(cart_item)

        await state.update_data(sale_cart=sale_cart)

        # Показываем опции: добавить еще или завершить
        first_item = len(sale_cart) == 1
        builder = InlineKeyboardBuilder()
        if first_item:
            builder.add(InlineKeyboardButton(text="⚡ Продать сейчас", callback_data="complete_sale"))
        builder.add(
            InlineKeyboardButton(text="➕ Добавить еще товар", callback_data="add_more_items"),
            InlineKeyboardButton(text="🛒 Просмотр корзины", callback_data="view_cart"),
            InlineKeyboardButton(text="✅ Завершить продажу", callback_data="complete_sale"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")
        )
        builder.adjust(1)

        await fsm_edit(
            state, message,
            f"✅ Товар добавлен в корзину!\n\n"
            f"🏷 {product[1]}\n"
            f"📦 Количество: {quantity} шт.\n"
            f"💰 Цена: {format_currency(price)}\n"
            f"💵 Сумма: {format_currency(quantity * price)}{motivation_text}\n\n"
            f"📋 В корзине товаров: {len(sale_cart)}\n\n"
            f"Что дальше?",
            reply_markup=builder.as_markup(),
        )
        await state.set_state(MultipleSaleStates.adding_items)

    except ValueError:
        await fsm_edit(state, message, "💰 ❌ Пожалуйста, введите корректную цену (число)!",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))

# НОВЫЕ ОБРАБОТЧИКИ ДЛЯ МНОЖЕСТВЕННЫХ ПРОДАЖ

@sales_router.callback_query(F.data == "add_more_items")
async def add_more_items(callback: CallbackQuery, state: FSMContext):
    """Добавление еще товаров в корзину"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    # Получаем категории товаров
    categories = current_db.get_all_categories()

    if not categories:
        await callback.message.edit_text(
            "📋 Товары отсутствуют.\n\nОбратитесь к администратору для добавления товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
        )
        return

    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.add(InlineKeyboardButton(text=f"📂 {category}", callback_data=safe_cb("sale_category_", category)))

    # Добавляем кнопки для управления корзиной
    builder.add(
        InlineKeyboardButton(text="🛒 Просмотр корзины", callback_data="view_cart"),
        InlineKeyboardButton(text="✅ Завершить продажу", callback_data="complete_sale"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")
    )
    builder.adjust(2, 1, 1, 1)

    await callback.message.edit_text(
        "🛒 Добавить еще товар\n\nВыберите категорию товара:",
        reply_markup=builder.as_markup()
    )

@sales_router.callback_query(F.data == "view_cart")
async def view_cart(callback: CallbackQuery, state: FSMContext):
    """Просмотр корзины"""
    data = await state.get_data()
    sale_cart = data.get('sale_cart', [])

    if not sale_cart:
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    await callback.answer()

    # Формируем текст корзины
    total_sum = 0
    total_items = 0
    total_earning = 0
    message_text = "🛒 Ваша корзина:\n\n"

    for i, item in enumerate(sale_cart, 1):
        expected_earning = item.get('expected_earning', 0)
        earning_text = f"\n   🎯 Мотивация: {format_currency(expected_earning)}" if expected_earning > 0 else ""
        
        message_text += f"{i}. 🏷 {item['product_name']}\n"
        message_text += f"   📦 {item['quantity']} шт. × {format_currency(item['price'])} = {format_currency(item['total'])}{earning_text}\n\n"
        total_sum += item['total']
        total_items += item['quantity']
        total_earning += expected_earning

    message_text += f"📊 Итого:\n"
    message_text += f"• Товаров: {total_items} шт.\n"
    message_text += f"• Сумма: {format_currency(total_sum)}\n"
    if total_earning > 0:
        message_text += f"• Ваша мотивация: {format_currency(total_earning)}\n"
    message_text += "\n"

    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_more_items"),
        InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clear_cart"),
        InlineKeyboardButton(text="✅ Завершить продажу", callback_data="complete_sale"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")
    )
    builder.adjust(2, 2)

    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup()
    )

@sales_router.callback_query(F.data == "clear_cart")
async def clear_cart(callback: CallbackQuery, state: FSMContext):
    """Очистка корзины"""
    await callback.answer()
    await state.update_data(sale_cart=[])

    await callback.message.edit_text(
        "🗑 Корзина очищена!\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_more_items")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]
        ])
    )

# ... предыдущий код без изменений ...

@sales_router.callback_query(F.data == "complete_sale")
async def complete_sale(callback: CallbackQuery, state: FSMContext):
    """Завершение продажи и регистрация всех товаров из корзины"""
    current_db = await get_db(callback.from_user.id, state)

    data = await state.get_data()
    sale_cart = data.get('sale_cart', [])
    shop_name = data.get('shop_name')


    if not sale_cart:
        try:
            await callback.answer("Корзина пуста! Добавьте товары для продажи.", show_alert=True)
        except Exception:
            pass
        return

    # Проверяем лимит продаж по тарифу
    if not env_manager.is_super_admin(callback.from_user.id):
        from subscription_utils import check_sales_limit
        ok, msg = check_sales_limit(callback.from_user.id)
        if not ok:
            await callback.answer()
            await callback.message.edit_text(
                f"🚫 <b>Лимит продаж исчерпан</b>\n\n{msg}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Подписка", callback_data="subscription_menu")],
                    [InlineKeyboardButton(text="❌ Отменить продажу", callback_data="cancel_sale")]
                ]),
                parse_mode="HTML"
            )
            return

    user_id = current_db.get_user_id(callback.from_user.id)

    if not user_id:
        try:
            await callback.answer("❌ Пользователь не найден!", show_alert=True)
        except Exception:
            pass
        return

    # Снимок прогресса планов ДО продажи (для определения только что выполненных)
    try:
        _snap_before = current_db.get_user_plans_progress(callback.from_user.id)
        _plans_before_pct = {p[0]: pct for p, _, pct in _snap_before}
    except Exception:
        _plans_before_pct = {}

    # Рассчитываем общие показатели из корзины
    total_sum = sum(item['total'] for item in sale_cart)
    total_items = sum(item['quantity'] for item in sale_cart)
    total_earning = sum(item.get('expected_earning', 0) for item in sale_cart)  # Общая мотивация из корзины

    results = []
    processed_sales = []
    failed = False

    try:
        # Сначала проверяем остатки для всех товаров
        for item in sale_cart:
            product_id = item['product_id']
            quantity = item['quantity']

            current_stock = current_db.get_inventory(shop_name, product_id)

            if current_stock < quantity:
                try:
                    await callback.answer(
                        f"❌ Недостаточно товара '{item['product_name']}' на складе! "
                        f"Доступно: {current_stock or 0} шт., требуется: {quantity} шт.",
                        show_alert=True
                    )
                except Exception:
                    pass
                return

        # Обрабатываем каждый товар из корзины
        for i, item in enumerate(sale_cart):

            product_id = item['product_id']
            quantity = item['quantity']
            price = item['price']

            # Регистрируем продажу
            try:
                sale_id = current_db.add_sale(product_id, shop_name, quantity, user_id, price)

                if sale_id and sale_id > 0:
                    processed_sales.append(sale_id)

                    # Получаем новые остатки после продажи
                    new_quantity = current_db.get_inventory(shop_name, product_id)

                    results.append({
                        'name': item['product_name'],
                        'quantity': quantity,
                        'price': price,
                        'total': item['total'],
                        'new_stock': new_quantity,
                        'expected_earning': item.get('expected_earning', 0)
                    })
                else:
                    failed = True
                    break

            except Exception as sale_error:
                failed = True
                break

        if failed:
            # Откатываем все продажи в случае ошибки
            for sale_id in processed_sales:
                try:
                    current_db.delete_sale(sale_id)
                except Exception:
                    pass

            try:
                await callback.answer("❌ Ошибка при регистрации продажи. Попробуйте еще раз.", show_alert=True)
            except Exception:
                pass
            return

        if not results:
            try:
                await callback.answer("❌ Не удалось зарегистрировать ни одной продажи.", show_alert=True)
            except Exception:
                pass
            return

        # Планы, которые только что перешли через 100%
        try:
            _snap_after = current_db.get_user_plans_progress(callback.from_user.id)
            _completed_now = [
                (plan, actual, pct) for plan, actual, pct in _snap_after
                if pct >= 100 and _plans_before_pct.get(plan[0], 0) < 100
            ]
        except Exception:
            _completed_now = []

        # Формируем отчет о продаже с информацией о мотивации
        message_text = "✅ Продажа успешно зарегистрирована!\n\n"

        for result in results:
            # Добавляем информацию о мотивации для каждого товара, если она есть
            earning_text = f"\n   🎯 Ваша мотивация: {format_currency(result['expected_earning'])}" if result['expected_earning'] > 0 else ""

            message_text += f"🏷 {result['name']}\n"
            message_text += f"   📦 {result['quantity']} шт. × {format_currency(result['price'])} = {format_currency(result['total'])}{earning_text}\n"
            message_text += f"   📊 Остаток: {result['new_stock']} шт.\n\n"

        message_text += f"📊 Итого по продаже:\n"
        message_text += f"• Товаров продано: {total_items} шт.\n"
        message_text += f"• Общая сумма: {format_currency(total_sum)}\n"

        # Добавляем информацию об общей мотивации, если она есть
        if total_earning > 0:
            message_text += f"• Ваша общая мотивация: {format_currency(total_earning)}\n"

        if _completed_now:
            _PERIOD = {'monthly': 'Месяц', 'weekly': 'Неделя', 'daily': 'День', 'quarter': 'Квартал'}
            message_text += "\n🎉 ПЛАН ВЫПОЛНЕН! 🏆\n"
            for _plan, _actual, _pct in _completed_now:
                _label = _PERIOD.get(_plan[1], _plan[1])
                if _plan[2] == 'turnover':
                    _actual_str = f"{_actual:,.0f} ₽"
                    _target_str = f"{_plan[3]:,.0f} ₽"
                else:
                    _actual_str = f"{int(_actual)} шт."
                    _target_str = f"{int(_plan[3])} шт."
                message_text += f"✅ {_label}: {_actual_str} из {_target_str}\n"

        # Проверяем milestone 50%/75% (100% уже отражён выше)
        try:
            newly_hit = current_db.check_and_mark_plan_milestones(callback.from_user.id)
            _PERIOD2 = {'monthly': 'Месяц', 'weekly': 'Неделя', 'daily': 'День', 'quarter': 'Квартал'}
            for _plan, _actual, _pct, _ms in newly_hit:
                if _ms < 100:
                    _label = _PERIOD2.get(_plan[1], _plan[1])
                    _icon = "🟡" if _ms == 50 else "🟠"
                    message_text += f"\n{_icon} Выполнено {_ms}% плана ({_label})!"
        except Exception:
            pass

        user = current_db.get_user(callback.from_user.id)
        user_shop = user[8] if user and len(user) > 8 else "Неизвестный магазин"

        await callback.answer()
        try:
            await callback.message.edit_text(
                message_text,
                reply_markup=main_menu(callback.from_user.id, user_shop)
            )
        except Exception as edit_error:
            # Если не удается изменить сообщение, отправляем новое
            try:
                await callback.message.answer(
                    message_text,
                    reply_markup=main_menu(callback.from_user.id, user_shop)
                )
            except Exception:
                    pass

        await clear_state_keep_org(state)

    except Exception as e:

        # Откатываем все продажи в случае общей ошибки
        for sale_id in processed_sales:
            try:
                current_db.delete_sale(sale_id)
            except Exception:
                pass

        try:
            await callback.answer("❌ Произошла ошибка при обработке продажи. Попробуйте еще раз.", show_alert=True)
        except Exception:
            pass

# ... остальной код без изменений ...

@sales_router.callback_query(F.data == "cancel_sale")
async def cancel_sale(callback: CallbackQuery, state: FSMContext):
    """Отмена продажи"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    user_shop = user[8] if user and len(user) > 8 else "Неизвестный магазин"

    try:
        await callback.message.edit_text(
            "❌ Продажа отменена.",
            reply_markup=main_menu(callback.from_user.id, user_shop)
        )
    except Exception:
        try:
            await callback.message.answer(
                "❌ Продажа отменена.",
                reply_markup=main_menu(callback.from_user.id, user_shop)
            )
        except Exception:
            pass

    await clear_state_keep_org(state)

# РЕДАКТИРОВАНИЕ ПРОДАЖ

@sales_router.callback_query(F.data == "edit_sales")
async def edit_sales_menu(callback: CallbackQuery, state: FSMContext):
    """Меню управления продажами"""
    await callback.answer()
    is_admin = is_any_admin(callback.from_user.id)
    
    await clear_state_keep_org(state)
    
    if is_admin:
        builder = InlineKeyboardBuilder()
        builder.button(text="📝 Редактировать продажи", callback_data="edit_sales_start")
        builder.button(text="⬅️ Назад", callback_data="admin_management")
        builder.adjust(1)
        
        await callback.message.edit_text(
            "📝 <b>Управление продажами</b>\n\n"
            "Выберите раздел:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        # Для обычного пользователя показываем сразу выбор периода
        await edit_sales_start(callback, state)

@sales_router.callback_query(F.data == "edit_sales_start")
async def edit_sales_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса редактирования продаж (выбор периода)"""
    await callback.answer()
    is_admin = is_any_admin(callback.from_user.id)
    back_cb = "edit_sales" if is_admin else "main_menu"
    
    await callback.message.edit_text(
        "📝 Редактирование продаж\n\nВыберите период:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 За сегодня", callback_data="edit_sales_today")],
            [InlineKeyboardButton(text="🗓 За период", callback_data="edit_sales_period")],
            [back_button(back_cb)]
        ])
    )

@sales_router.callback_query(F.data == "edit_sales_today")
async def edit_sales_today(callback: CallbackQuery, state: FSMContext):
    """Редактирование продаж за сегодня"""
    await callback.answer()
    is_admin = is_any_admin(callback.from_user.id)
    current_db = await get_db(callback.from_user.id, state)
    from datetime import date
    today = date.today().strftime('%Y-%m-%d')

    if is_admin:
        # Для админа - выбор магазина
        await state.update_data(edit_start_date=today, edit_end_date=today)

        shops = current_db.get_all_shops()
        if not shops:
            await callback.message.edit_text(
                "🏪 Магазины не найдены.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]])
            )
            return

        builder = InlineKeyboardBuilder()
        for shop in shops:
            builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("admin_edit_shop_sales_", shop)))
        builder.add(back_button("edit_sales"))
        builder.adjust(2, 1)

        await callback.message.edit_text(
            "🏪 Выберите магазин для редактирования продаж за сегодня:",
            reply_markup=builder.as_markup()
        )
        return

    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.message.edit_text(
            "❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]])
        )
        return

    # Получаем продажи за сегодня
    sales = current_db.get_user_sales_by_date(user_id, today, today)

    if not sales:
        await callback.message.edit_text(
            "📝 У вас нет продаж за сегодня для редактирования.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]])
        )
        return

    await show_sales_for_edit(callback, sales, state, "За сегодня")

@sales_router.callback_query(F.data.startswith("admin_edit_shop_sales_"))
async def admin_edit_shop_sales_list(callback: CallbackQuery, state: FSMContext):
    """Список продаж магазина для администратора"""
    await callback.answer()
    shop_raw = callback.data.replace("admin_edit_shop_sales_", "")
    current_db_temp = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, current_db_temp.get_all_shops() or [])
    data = await state.get_data()
    start_date = data.get('edit_start_date')
    end_date = data.get('edit_end_date')
    
    # Получаем все продажи магазина за период
    current_db = await get_db(callback.from_user.id, state)
    sales = current_db.get_shop_sales_by_date(shop_name, start_date, end_date)
    
    if not sales:
        await callback.message.edit_text(
            f"📝 В магазине '{shop_name}' нет продаж за выбранный период.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]])
        )
        return
        
    period_title = f"Магазин {shop_name}"
    await show_sales_for_edit(callback, sales, state, period_title)

async def show_sales_for_edit(callback: CallbackQuery, sales, state: FSMContext,
                              period_title: str, page: int = 0):
    """Показывает список продаж для редактирования (с пагинацией)."""
    # Сохраняем полный список в FSM для навигации по страницам
    await state.update_data(edit_sales_cache=sales, edit_sales_title=period_title)

    builder = InlineKeyboardBuilder()
    message_text = f"📝 Редактирование продаж — {period_title}\n\n"

    if not sales:
        message_text += "❌ Продажи не найдены."
        builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="edit_sales"))
    else:
        page_items, has_prev, has_next, total_pages, page = paginate(sales, page, PAGE_SIZE_SALES)
        pg_info = f"· стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
        message_text += f"Продаж: {len(sales)} {pg_info}\nВыберите для редактирования:\n\n"

        from utils import format_date_for_user
        offset = page * PAGE_SIZE_SALES
        for i, sale in enumerate(page_items, offset + 1):
            sale_id      = sale[0]
            quantity     = sale[3]
            sale_price   = sale[4]
            sale_date    = sale[6]
            product_name = sale[7] if len(sale) > 7 else "Неизвестный товар"
            total        = quantity * sale_price
            formatted_date = format_date_for_user(sale_date, callback.from_user.id)

            message_text += f"{i}. 🏷 {product_name}\n"
            message_text += f"   📦 {quantity} × {format_currency(sale_price)} = {format_currency(total)}\n"
            message_text += f"   📅 {formatted_date}\n\n"
            builder.add(InlineKeyboardButton(
                text=f"✏️ {i}. {product_name[:30]}",
                callback_data=f"edit_sale_{sale_id}"
            ))

        nav = page_nav_row("esl_pg_", page, has_prev, has_next, total_pages)
        if nav:
            builder.row(*nav)
        builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="edit_sales"))
        builder.adjust(1)

    await callback.message.edit_text(message_text, reply_markup=builder.as_markup())
    await state.set_state(EditSaleStates.choosing_sale)


@sales_router.callback_query(F.data.startswith("esl_pg_"))
async def edit_sales_page(callback: CallbackQuery, state: FSMContext):
    """Навигация по страницам списка продаж для редактирования."""
    await callback.answer()
    try:
        page = int(callback.data.replace("esl_pg_", ""))
    except ValueError:
        page = 0
    data = await state.get_data()
    sales = data.get("edit_sales_cache", [])
    title = data.get("edit_sales_title", "Продажи")
    await show_sales_for_edit(callback, sales, state, title, page=page)

async def render_edit_sale_menu(message, state: FSMContext, sale_id: int, telegram_id: int = None):
    """Вспомогательная функция для отображения меню редактирования продажи"""
    current_db = await get_db(telegram_id, state) if telegram_id else Database('data/shop_bot.db')
    # Получаем данные о продаже
    sale = current_db.get_sale_by_id(sale_id)
    if not sale:
        await message.edit_text(
            "❌ Продажа не найдена!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 К редактированию", callback_data="edit_sales")]
            ])
        )
        return
    
    # Структура sale из get_sale_by_id: 
    # sale_id, product_id, shop_name, quantity_sold, sale_price, user_id, sale_date, product_name, first_name, last_name
    product_name = sale[7] if len(sale) > 7 else "Неизвестный товар"
    quantity = sale[3]  # quantity_sold
    sale_price = sale[4]  # sale_price - цена конкретной продажи
    user_id_from_sale = sale[5]  # user_id продажи
    total = quantity * sale_price
    sale_date = sale[6] if len(sale) > 6 else None
    
    await state.update_data(sale_id=sale_id, product_name=product_name, current_quantity=quantity, price=sale_price)
    
    # Получаем telegram_id пользователя, который сделал продажу
    user_data = current_db.get_user_by_id(user_id_from_sale)
    telegram_id = user_data[1] if user_data else None
    
    # Форматируем дату с учетом часового пояса пользователя
    from utils import format_date_for_user
    if telegram_id:
        formatted_date = format_date_for_user(sale_date, telegram_id)
    else:
        formatted_date = format_date_display(sale_date)
    
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="📦 Изменить количество", callback_data="edit_quantity"),
        InlineKeyboardButton(text="💰 Изменить цену", callback_data="edit_price"),
        InlineKeyboardButton(text="🗑 Удалить продажу", callback_data="delete_sale"),
        InlineKeyboardButton(text="⬅️ К списку", callback_data="back_to_sales_list")
    )
    builder.adjust(1)
    
    await message.edit_text(
        f"📝 Редактирование продажи\n\n"
        f"🏷 Товар: {product_name}\n"
        f"📦 Количество: {quantity} шт.\n"
        f"💰 Цена продажи: {format_currency(sale_price)}\n"
        f"💸 Сумма: {format_currency(total)}\n"
        f"📅 Дата: {formatted_date}\n\n"
        f"Выберите действие:",
        reply_markup=builder.as_markup()
    )

@sales_router.callback_query(F.data.startswith("edit_sale_"))
async def choose_edit_sale(callback: CallbackQuery, state: FSMContext):
    """Выбор конкретной продажи для редактирования"""
    await callback.answer()
    sale_id = int(callback.data.replace("edit_sale_", ""))
    await render_edit_sale_menu(callback.message, state, sale_id, callback.from_user.id)

@sales_router.callback_query(F.data == "edit_quantity")
async def edit_sale_quantity(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования количества"""
    await callback.answer()
    data = await state.get_data()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"📦 Изменение количества\n\n"
        f"🏷 Товар: {data['product_name']}\n"
        f"📦 Текущее количество: {data['current_quantity']} шт.\n\n"
        f"Введите новое количество:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_edit_sale")]
        ])
    )
    await state.set_state(EditSaleStates.editing_quantity)

@sales_router.message(EditSaleStates.editing_quantity)
async def process_quantity_edit(message: Message, state: FSMContext):
    """Обработка нового количества"""
    _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_edit_sale")]])
    try:
        new_quantity = int(message.text)
        if new_quantity <= 0:
            await fsm_edit(state, message, "📦 ❌ Количество должно быть больше 0! Введите снова:", reply_markup=_cancel_kb)
            return
        
        data = await state.get_data()
        sale_id = data['sale_id']
        
        # Обновляем количество в базе данных
        current_db = await get_db(message.from_user.id, state)
        if current_db.update_sale(sale_id, new_quantity):
            await fsm_edit(
                state, message,
                f"✅ Количество успешно изменено!\n\n"
                f"🏷 Товар: {data['product_name']}\n"
                f"📦 Новое количество: {new_quantity} шт.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 К редактированию", callback_data="edit_sales")]]),
            )
        else:
            await fsm_edit(state, message, "❌ Ошибка при обновлении количества!", reply_markup=_cancel_kb)
        
        await clear_state_keep_org(state)
        
    except ValueError:
        await fsm_edit(state, message, "📦 ❌ Введите корректное целое число!", reply_markup=_cancel_kb)

@sales_router.callback_query(F.data == "edit_price")
async def edit_sale_price(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования цены"""
    await callback.answer()
    data = await state.get_data()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"💰 Изменение цены\n\n"
        f"🏷 Товар: {data['product_name']}\n"
        f"💰 Текущая цена: {format_currency(data['price'])}\n\n"
        f"Введите новую цену (в рублях):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_edit_sale")]
        ])
    )
    await state.set_state(EditSaleStates.editing_price)

@sales_router.message(EditSaleStates.editing_price)
async def process_price_edit(message: Message, state: FSMContext):
    """Обработка новой цены"""
    _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_edit_sale")]])
    try:
        new_price = float(message.text.replace(',', '.'))
        if new_price <= 0:
            await fsm_edit(state, message, "💰 ❌ Цена должна быть больше 0! Введите снова:", reply_markup=_cancel_kb)
            return
        
        data = await state.get_data()
        sale_id = data['sale_id']
        quantity = data['current_quantity']
        
        # Обновляем только цену в продаже, не меняя цену товара глобально
        current_db = await get_db(message.from_user.id, state)
        if current_db.update_sale(sale_id, quantity, sale_price=new_price):
            await fsm_edit(
                state, message,
                f"✅ Цена продажи успешно изменена!\n\n"
                f"🏷 Товар: {data['product_name']}\n"
                f"💰 Новая цена: {format_currency(new_price)}\n"
                f"📦 Количество: {quantity} шт.\n"
                f"💸 Новая сумма: {format_currency(new_price * quantity)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 К редактированию", callback_data="edit_sales")]]),
            )
        else:
            await fsm_edit(state, message, "❌ Ошибка при обновлении цены!", reply_markup=_cancel_kb)
        
        await clear_state_keep_org(state)
        
    except ValueError:
        await fsm_edit(state, message, "💰 ❌ Введите корректное число (например: 1500 или 1500.50)!", reply_markup=_cancel_kb)

@sales_router.callback_query(F.data == "delete_sale")
async def confirm_delete_sale(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления продажи"""
    await callback.answer()
    data = await state.get_data()
    
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data="sale_delete_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_edit_sale")
    )
    builder.adjust(2)
    
    await callback.message.edit_text(
        f"🗑 Удаление продажи\n\n"
        f"🏷 Товар: {data['product_name']}\n"
        f"📦 Количество: {data['current_quantity']} шт.\n"
        f"💰 Цена: {format_currency(data['price'])}\n\n"
        f"⚠️ Вы уверены, что хотите удалить эту продажу?\n"
        f"Это действие нельзя отменить!",
        reply_markup=builder.as_markup()
    )
    await state.set_state(EditSaleStates.confirming_delete)

@sales_router.callback_query(F.data == "sale_delete_confirm")
async def delete_sale_confirmed(callback: CallbackQuery, state: FSMContext):
    """Окончательное удаление продажи"""
    await callback.answer()
    data = await state.get_data()
    sale_id = data['sale_id']
    
    current_db = await get_db(callback.from_user.id, state)
    if current_db.delete_sale(sale_id):
        await callback.message.edit_text(
            f"✅ Продажа успешно удалена!\n\n"
            f"🏷 Товар: {data['product_name']}\n"
            f"📦 Количество: {data['current_quantity']} шт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 К редактированию", callback_data="edit_sales")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка при удалении продажи!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 К редактированию", callback_data="edit_sales")]
            ])
        )
    
    await clear_state_keep_org(state)

@sales_router.callback_query(F.data == "back_to_edit_sale")
async def back_to_edit_sale(callback: CallbackQuery, state: FSMContext):
    """Возврат к редактированию продажи"""
    await callback.answer()
    data = await state.get_data()
    sale_id = data['sale_id']
    await render_edit_sale_menu(callback.message, state, sale_id, callback.from_user.id)

@sales_router.callback_query(F.data == "back_to_sales_list")
async def back_to_sales_list(callback: CallbackQuery, state: FSMContext):
    """Возврат к списку продаж"""
    # Возвращаемся к меню редактирования продаж
    await edit_sales_menu(callback, state)

@sales_router.callback_query(F.data == "edit_sales_period")
async def edit_sales_period_start(callback: CallbackQuery, state: FSMContext):
    """Начало выбора периода для редактирования продаж"""
    await callback.answer()
    from datetime import date, timedelta
    from keyboards import generate_calendar

    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Вчера", callback_data=f"edit_period_{yesterday}_{yesterday}")],
        [InlineKeyboardButton(text="📅 Последние 7 дней", callback_data=f"edit_period_{week_ago}_{today}")],
        [InlineKeyboardButton(text="📅 Последние 30 дней", callback_data=f"edit_period_{month_ago}_{today}")],
        [InlineKeyboardButton(text="🗓 Выбрать дату", callback_data="edit_sales_calendar")],
        [back_button("edit_sales")]
    ])

    await callback.message.edit_text(
        "📝 Выберите период для редактирования продаж:",
        reply_markup=keyboard
    )

@sales_router.callback_query(F.data.startswith("edit_period_"))
async def edit_sales_selected_period(callback: CallbackQuery, state: FSMContext):
    """Обработка выбранного периода для редактирования продаж"""
    await callback.answer()
    period_data = callback.data.replace("edit_period_", "")
    start_date, end_date = period_data.split("_", 1)
    
    is_admin = is_any_admin(callback.from_user.id)
    current_db = await get_db(callback.from_user.id, state)
    if is_admin:
        # Для админа - выбор магазина
        await state.update_data(edit_start_date=start_date, edit_end_date=end_date)
        
        shops = current_db.get_all_shops()
        builder = InlineKeyboardBuilder()
        for shop in shops:
            builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("admin_edit_shop_sales_", shop)))
        builder.add(back_button("edit_sales_period"))
        builder.adjust(2, 1)
        
        await callback.message.edit_text(
            f"🏪 Выберите магазин для периода {format_date_display(start_date)} - {format_date_display(end_date)}:",
            reply_markup=builder.as_markup()
        )
        return

    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.message.edit_text(
            "❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales_period")]])
        )
        return
    
    # Получаем продажи за выбранный период
    sales = current_db.get_user_sales_by_date(user_id, start_date, end_date)
    
    if not sales:
        period_text = "За выбранный период" if start_date != end_date else f"За {format_date_display(start_date)}"
        await callback.message.edit_text(
            f"📝 У вас нет продаж {period_text.lower()} для редактирования.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales_period")]])
        )
        return
    
    period_title = "за выбранный период" if start_date != end_date else f"за {format_date_display(start_date)}"
    await show_sales_for_edit(callback, sales, state, period_title.capitalize())

@sales_router.callback_query(F.data == "edit_sales_calendar")
async def edit_sales_calendar_start(callback: CallbackQuery, state: FSMContext):
    """Начало выбора даты через календарь для редактирования продаж"""
    await callback.answer()
    from keyboards import generate_calendar

    await state.update_data(selecting_start_date=True)
    calendar = generate_calendar(cancel_callback="edit_sales_period", prefix="edit_cal_")

    await callback.message.edit_text(
        "📅 Выберите начальную дату периода:",
        reply_markup=calendar
    )

@sales_router.callback_query(F.data.startswith("edit_cal_"))
async def edit_sales_calendar_handler(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора даты в календаре для редактирования продаж"""
    from keyboards import generate_calendar
    from datetime import datetime, date

    action = callback.data.replace("edit_cal_", "")
    data = await state.get_data()

    if action == "ignore":
        await callback.answer()
        return
    elif action.startswith("nav_"):
        # Обработка навигации по месяцам (новый формат)
        nav_data = action.replace("nav_", "").split("_")
        current_year = int(nav_data[0])
        current_month = int(nav_data[1])

        await state.update_data(calendar_year=current_year, calendar_month=current_month)

        is_start = data.get('selecting_start_date', True)
        date_type = "начальную" if is_start else "конечную"

        calendar = generate_calendar(current_year, current_month, "edit_sales_period", "edit_cal_")
        await callback.message.edit_text(
            f"📅 Выберите {date_type} дату периода:",
            reply_markup=calendar
        )
        await callback.answer()
        return
    elif action == "prev_month" or action == "next_month":
        # Обработка смены месяца
        current_year = data.get('calendar_year', date.today().year)
        current_month = data.get('calendar_month', date.today().month)

        if action == "prev_month":
            if current_month == 1:
                current_month = 12
                current_year -= 1
            else:
                current_month -= 1
        else:  # next_month
            if current_month == 12:
                current_month = 1
                current_year += 1
            else:
                current_month += 1

        await state.update_data(calendar_year=current_year, calendar_month=current_month)

        is_start = data.get('selecting_start_date', True)
        date_type = "начальную" if is_start else "конечную"

        calendar = generate_calendar(current_year, current_month, "edit_sales_period", "edit_cal_")
        await callback.message.edit_text(
            f"📅 Выберите {date_type} дату периода:",
            reply_markup=calendar
        )
        await callback.answer()
        return

    # Обработка выбора даты
    if action.startswith("date_"):
        date_str = action.replace("date_", "")
        try:
            selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            await callback.answer("❌ Неверный формат даты!", show_alert=True)
            return

        is_start = data.get('selecting_start_date', True)
        
        if is_start:
            # Выбрана начальная дата
            await state.update_data(
                start_date=date_str,
                selecting_start_date=False
            )
            
            # Переходим к выбору конечной даты
            calendar = generate_calendar(cancel_callback="edit_sales_period", prefix="edit_cal_")
            await callback.message.edit_text(
                f"📅 Выберите период для редактирования продаж\n\n"
                f"✅ Начальная дата: {format_date_display(date_str)}\n\n"
                f"Выберите конечную дату:",
                reply_markup=calendar
            )
        else:
            # Выбрана конечная дата
            start_date = data.get('start_date')
            
            if not start_date:
                await callback.answer("❌ Ошибка: начальная дата не выбрана!", show_alert=True)
                return
            
            # Проверяем корректность дат
            if date_str < start_date:
                await callback.answer("❌ Конечная дата не может быть раньше начальной!", show_alert=True)
                return
            
            is_admin = is_any_admin(callback.from_user.id)
            current_db = await get_db(callback.from_user.id, state)
            if is_admin:
                # Для админа - выбор магазина
                await state.update_data(edit_start_date=start_date, edit_end_date=date_str)
                
                shops = current_db.get_all_shops()
                builder = InlineKeyboardBuilder()
                for shop in shops:
                    builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("admin_edit_shop_sales_", shop)))
                builder.add(back_button("edit_sales_calendar"))
                builder.adjust(2, 1)
                
                await callback.message.edit_text(
                    f"🏪 Выберите магазин для периода {format_date_display(start_date)} - {format_date_display(date_str)}:",
                    reply_markup=builder.as_markup()
                )
                await callback.answer()
                return

            # Получаем пользователя и продажи за период
            user_id = current_db.get_user_id(callback.from_user.id)
            if not user_id:
                await callback.answer("❌ Пользователь не найден!", show_alert=True)
                return
            
            sales = current_db.get_user_sales_by_date(user_id, start_date, date_str)
            
            if not sales:
                await callback.message.edit_text(
                    f"📝 У вас нет продаж за период с {format_date_display(start_date)} по {format_date_display(date_str)} для редактирования.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales_period")]])
                )
                await callback.answer()
                return
            
            period_title = f"с {format_date_display(start_date)} по {format_date_display(date_str)}"
            await show_sales_for_edit(callback, sales, state, f"За период {period_title}")

        await callback.answer()