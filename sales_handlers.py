"""Обработчики для продаж и управления остатками: добавлено отображение мотивации при продаже товара и предварительный расчет мотивации при подтверждении продажи."""
import asyncio
import logging
from aiogram import Router, F

from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Database
from env_manager import env_manager
from keyboards import main_menu, back_button, create_selection_keyboard
from states import SaleStates, InventoryStates, EditSaleStates, MultipleSaleStates, QuickSaleStates, ExcelImportStates, SearchStates
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_SALES
from utils import format_currency, get_stock_color_indicator, format_date_display, he

# Создаем роутер для продаж
sales_router = Router()
logger = logging.getLogger(__name__)

from db_utils import get_db, clear_state_keep_org, is_any_admin, maybe_refresh_username, wrap_db
from keyboards import safe_cb, resolve_cb_name
from message_utils import fsm_edit, delete_message_safe
from hints import hint_suffix
from timezone_utils import get_current_user_time as _gcur_tz

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

async def _show_sale_categories(
    callback: CallbackQuery,
    state: FSMContext,
    current_db,
    shop_name: str,
    allow_change: bool = False,
    reset_cart: bool = True,
):
    """Вспомогательная функция: показывает категории для выбора товара в продаже.

    allow_change=True — показывает кнопку «Сменить магазин» (для мультимагазинных сетей).
    reset_cart=False  — не очищает корзину (используется при добавлении следующего товара).
    """
    if reset_cart:
        await state.update_data(shop_name=shop_name, sale_cart=[], sale_current_shop=shop_name)
    else:
        await state.update_data(shop_name=shop_name, sale_current_shop=shop_name)

    categories = await current_db.get_all_categories()

    if not categories:
        await callback.message.edit_text(
            "📋 Товары отсутствуют.\n\nОбратитесь к администратору для добавления товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]])
        )
        return

    # Получаем данные FSM для отображения контекста
    fsm_data = await state.get_data()
    home_shop = fsm_data.get("sale_home_shop", shop_name)
    is_other_shop = shop_name != home_shop

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔍 Найти товар", callback_data="sale_quick_search"))

    # Кнопка смены магазина (если торговая сеть с несколькими магазинами)
    if allow_change:
        change_label = f"🔄 Сменить магазин {'· ' + he(shop_name) if is_other_shop else ''}"
        builder.row(InlineKeyboardButton(text=change_label.strip("· ") if not is_other_shop else change_label,
                                         callback_data="sale_change_shop"))

    # Кнопки «Избранное» и «Недавние» — раскрываются в отдельном экране
    try:
        user_row = await current_db.get_user(callback.from_user.id)
        if user_row:
            u_db_id = user_row[0]
            fav_ids  = await current_db.get_favorite_products(u_db_id) or []
            recent   = await current_db.get_user_recent_products(u_db_id, limit=8) or []
            row_btns = []
            if fav_ids:
                row_btns.append(InlineKeyboardButton(
                    text=f"⭐ Избранное ({len(fav_ids)})",
                    callback_data="sale_show_favorites"
                ))
            if recent:
                row_btns.append(InlineKeyboardButton(
                    text=f"🔄 Недавние ({len(recent)})",
                    callback_data="sale_show_recent"
                ))
            if row_btns:
                builder.row(*row_btns)
    except Exception:
        pass

    # Категории по 2 в строку
    products = await current_db.get_all_products()
    has_no_category = any(not p[2] or p[2] == "Без категории" for p in products)
    cat_buttons = [
        InlineKeyboardButton(text=f"📂 {cat}", callback_data=safe_cb("sale_category_", cat))
        for cat in categories
    ]
    if has_no_category:
        cat_buttons.append(InlineKeyboardButton(text="📁 Без категории", callback_data="sale_category_Без категории"))
    for i in range(0, len(cat_buttons), 2):
        builder.row(*cat_buttons[i:i+2])

    # Кнопки корзины (если уже есть товары — при добавлении следующего)
    cart = fsm_data.get("sale_cart", []) if not reset_cart else []
    if cart:
        builder.row(
            InlineKeyboardButton(text=f"🛒 Корзина ({len(cart)})", callback_data="view_cart"),
            InlineKeyboardButton(text="✅ Завершить", callback_data="complete_sale"),
        )

    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale"))

    # Заголовок — выделяем если это чужой магазин
    if is_other_shop:
        header = (
            f"🛒 Новая продажа\n"
            f"🏪 Списание с: <b>{he(shop_name)}</b> <i>(другой магазин)</i>\n"
            f"👤 Ваш магазин: {he(home_shop)}"
        )
    else:
        header = f"🛒 Новая продажа <b>[{he(shop_name)}]</b>"

    _sale_user = await current_db.get_user(callback.from_user.id)
    _sale_hint = hint_suffix(current_db, _sale_user[0], 'first_sale') if _sale_user else ""
    await callback.message.edit_text(
        f"{header}\n\n🔍 Найдите товар по названию или выберите категорию:{_sale_hint}",
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
    await current_db.create_tables()
    
    is_super = env_manager.is_super_admin(callback.from_user.id)
    
    user_data = await current_db.get_user(callback.from_user.id)
    
    # Если супер-админ, создаем запись в БД если её нет
    if is_super and not user_data:
        await current_db.add_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "Admin",
            last_name=callback.from_user.last_name or "",
            shop_name=None,
            trade_network=None,
            city=None,
            phone="000",
            username=callback.from_user.username
        )
        user_data = await current_db.get_user(callback.from_user.id)

    if not user_data:
        await callback.message.edit_text(
            "❌ Сначала завершите регистрацию через /start",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Закрыть", callback_data="main_menu")]])
        )
        return

    maybe_refresh_username(
        current_db, callback.from_user.id, callback.from_user.username,
        stored_username=user_data[12] if len(user_data) > 12 else None,
    )
    shop_name = user_data[8]
    trade_network = user_data[7]

    # Для суп-админа: если shop_name пустой или дефолтный "Системный",
    # предлагаем выбрать реальный магазин из организации
    if is_super and (not shop_name or shop_name == "Системный"):
        inv_shops = await current_db.get_inventory_shops()
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

    # Для обычного пользователя: проверяем наличие других магазинов в той же торговой сети
    allow_change = False
    if not is_super and trade_network:
        try:
            net_shops = await current_db.get_shops_by_network(trade_network)
            if len(net_shops) > 1:
                allow_change = True
                await state.update_data(
                    sale_network=trade_network,
                    sale_home_shop=shop_name,
                    sale_allow_change=True,
                )
        except Exception:
            pass

    # Fallback для сотрудников и орг-админов: смотрим магазины с реальными остатками.
    # Используем inventory (не users), чтобы не зависеть от того, есть ли
    # зарегистрированные пользователи в каждом магазине.
    if not allow_change and not is_super:
        try:
            inv_shops = await current_db.get_inventory_shops()
            if len(inv_shops) > 1:
                allow_change = True
                await state.update_data(
                    sale_network=None,
                    sale_home_shop=shop_name or "",
                    sale_allow_change=True,
                )
        except Exception:
            pass

    await _show_sale_categories(callback, state, current_db, shop_name, allow_change=allow_change)


@sales_router.callback_query(F.data.startswith("sale_shop_"))
async def select_sale_shop(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина суп-админом для продажи"""
    await callback.answer()
    shop_raw = callback.data.replace("sale_shop_", "")
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, await current_db.get_inventory_shops() or [])
    await _show_sale_categories(callback, state, current_db, shop_name)


# ──────────────────────────────────────────────────────────────────────────────
# ИЗБРАННОЕ И НЕДАВНИЕ — отдельные экраны
# ──────────────────────────────────────────────────────────────────────────────

def _sale_list_screen_kb(items_kb_rows: list, cart: list) -> InlineKeyboardMarkup:
    """Клавиатура для подэкранов Избранное / Недавние: товары + корзина + назад."""
    rows = list(items_kb_rows)
    if cart:
        rows.append([
            InlineKeyboardButton(text=f"🛒 Корзина ({len(cart)})", callback_data="view_cart"),
            InlineKeyboardButton(text="✅ Завершить", callback_data="complete_sale"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="sale_back_to_cats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_fav_list_content(fav_prods: list, cart: list, query: str = "") -> tuple:
    """Строит (text, markup) для экрана Избранное с опциональным поиском."""
    if query:
        q = query.lower()
        filtered = [p for p in fav_prods if q in (p[1] or "").lower()]
    else:
        filtered = fav_prods

    rows = [[InlineKeyboardButton(text=f"⭐ {p[1]}", callback_data=f"sale_product_{p[0]}")]
            for p in filtered]

    search_row = [InlineKeyboardButton(text="🔍 Найти", callback_data="slr_fav_srch_start")]
    if query:
        search_row.append(InlineKeyboardButton(text="✖️ Сбросить", callback_data="slr_srch_cancel"))
    rows.append(search_row)
    if cart:
        rows.append([
            InlineKeyboardButton(text=f"🛒 Корзина ({len(cart)})", callback_data="view_cart"),
            InlineKeyboardButton(text="✅ Завершить", callback_data="complete_sale"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="sale_back_to_cats")])

    total = len(fav_prods)
    if query:
        if not filtered:
            suffix = f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос"
            text = f"⭐ <b>Избранное</b> · {total} товаров{suffix}"
        else:
            suffix = f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}"
            text = f"⭐ <b>Избранное</b> · {total} товаров{suffix}\n\nВыберите товар:"
    else:
        text = f"⭐ <b>Избранное</b> · {total} товаров\n\nВыберите товар:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _build_recent_list_content(recent: list, cart: list, query: str = "") -> tuple:
    """Строит (text, markup) для экрана Недавние с опциональным поиском."""
    if query:
        q = query.lower()
        filtered = [(pid, pname, pprice, pcat) for pid, pname, pprice, pcat in recent
                    if q in (pname or "").lower()]
    else:
        filtered = recent

    rows = [[InlineKeyboardButton(text=f"🔄 {pname}", callback_data=f"sale_product_{pid}")]
            for pid, pname, pprice, pcat in filtered]

    search_row = [InlineKeyboardButton(text="🔍 Найти", callback_data="slr_rec_srch_start")]
    if query:
        search_row.append(InlineKeyboardButton(text="✖️ Сбросить", callback_data="slr_srch_cancel"))
    rows.append(search_row)
    if cart:
        rows.append([
            InlineKeyboardButton(text=f"🛒 Корзина ({len(cart)})", callback_data="view_cart"),
            InlineKeyboardButton(text="✅ Завершить", callback_data="complete_sale"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="sale_back_to_cats")])

    total = len(recent)
    if query:
        if not filtered:
            suffix = f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос"
            text = f"🔄 <b>Недавние</b> · {total} товаров{suffix}"
        else:
            suffix = f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}"
            text = f"🔄 <b>Недавние</b> · {total} товаров{suffix}\n\nВыберите товар:"
    else:
        text = f"🔄 <b>Недавние</b> · {total} товаров\n\nВыберите товар:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@sales_router.callback_query(F.data == "sale_show_favorites")
async def sale_show_favorites(callback: CallbackQuery, state: FSMContext):
    """Подэкран: список избранных товаров."""
    current_db = await get_db(callback.from_user.id, state)
    user_row = await current_db.get_user(callback.from_user.id)
    if not user_row:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    await callback.answer()
    u_db_id   = user_row[0]
    fav_ids   = await current_db.get_favorite_products(u_db_id) or []
    fav_prods = [await current_db.get_product(pid) for pid in fav_ids]
    fav_prods = [p for p in fav_prods if p]

    data = await state.get_data()
    cart = data.get("sale_cart", [])
    await state.update_data(anchor_msg_id=callback.message.message_id,
                            sale_srch_list_type="fav")

    if not fav_prods:
        await callback.message.edit_text(
            "⭐ <b>Избранное</b>\n\nСписок пуст — добавляйте товары через карточку товара.",
            reply_markup=_sale_list_screen_kb([], cart),
            parse_mode="HTML"
        )
        return

    text, markup = _build_fav_list_content(fav_prods, cart)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@sales_router.callback_query(F.data == "sale_show_recent")
async def sale_show_recent_handler(callback: CallbackQuery, state: FSMContext):
    """Подэкран: недавно проданные товары."""
    current_db = await get_db(callback.from_user.id, state)
    user_row = await current_db.get_user(callback.from_user.id)
    if not user_row:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    await callback.answer()
    u_db_id = user_row[0]
    recent  = await current_db.get_user_recent_products(u_db_id, limit=8) or []

    data = await state.get_data()
    cart = data.get("sale_cart", [])
    await state.update_data(anchor_msg_id=callback.message.message_id,
                            sale_srch_list_type="recent")

    if not recent:
        await callback.message.edit_text(
            "🔄 <b>Недавние</b>\n\nПока нет истории продаж.",
            reply_markup=_sale_list_screen_kb([], cart),
            parse_mode="HTML"
        )
        return

    text, markup = _build_recent_list_content(recent, cart)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@sales_router.callback_query(F.data == "slr_fav_srch_start")
async def slr_fav_srch_start(callback: CallbackQuery, state: FSMContext):
    """Запуск поиска в Избранном."""
    await state.update_data(anchor_msg_id=callback.message.message_id,
                            sale_srch_list_type="fav")
    await state.set_state(SearchStates.sale_favourites)
    await callback.message.edit_text(
        "🔍 <b>Поиск в Избранном</b>\n\nВведите название товара или его часть:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="slr_srch_cancel")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@sales_router.callback_query(F.data == "slr_rec_srch_start")
async def slr_rec_srch_start(callback: CallbackQuery, state: FSMContext):
    """Запуск поиска в Недавних."""
    await state.update_data(anchor_msg_id=callback.message.message_id,
                            sale_srch_list_type="recent")
    await state.set_state(SearchStates.sale_recent)
    await callback.message.edit_text(
        "🔍 <b>Поиск в Недавних</b>\n\nВведите название товара или его часть:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="slr_srch_cancel")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@sales_router.callback_query(F.data == "slr_srch_cancel")
async def slr_srch_cancel(callback: CallbackQuery, state: FSMContext):
    """Сброс поиска — возвращает полный список Избранного или Недавних."""
    await state.set_state(MultipleSaleStates.adding_items)
    await callback.answer()
    data = await state.get_data()
    list_type = data.get("sale_srch_list_type", "fav")
    current_db = await get_db(callback.from_user.id, state)
    user_row = await current_db.get_user(callback.from_user.id)
    cart = data.get("sale_cart", [])

    if not user_row:
        await callback.message.edit_text("❌ Пользователь не найден", parse_mode="HTML")
        return

    u_db_id = user_row[0]
    if list_type == "recent":
        recent = await current_db.get_user_recent_products(u_db_id, limit=8) or []
        if not recent:
            await callback.message.edit_text(
                "🔄 <b>Недавние</b>\n\nПока нет истории продаж.",
                reply_markup=_sale_list_screen_kb([], cart),
                parse_mode="HTML"
            )
        else:
            text, markup = _build_recent_list_content(recent, cart)
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        fav_ids = await current_db.get_favorite_products(u_db_id) or []
        fav_prods = [await current_db.get_product(pid) for pid in fav_ids]
        fav_prods = [p for p in fav_prods if p]
        if not fav_prods:
            await callback.message.edit_text(
                "⭐ <b>Избранное</b>\n\nСписок пуст — добавляйте товары через карточку товара.",
                reply_markup=_sale_list_screen_kb([], cart),
                parse_mode="HTML"
            )
        else:
            text, markup = _build_fav_list_content(fav_prods, cart)
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@sales_router.message(SearchStates.sale_favourites)
async def slr_fav_srch_process(message: Message, state: FSMContext):
    """Поиск по Избранному."""
    query = (message.text or "").strip()
    await state.set_state(MultipleSaleStates.adding_items)
    current_db = await get_db(message.from_user.id, state)
    user_row = await current_db.get_user(message.from_user.id)
    if not user_row:
        return
    data = await state.get_data()
    cart = data.get("sale_cart", [])
    u_db_id = user_row[0]
    fav_ids = await current_db.get_favorite_products(u_db_id) or []
    fav_prods = [await current_db.get_product(pid) for pid in fav_ids]
    fav_prods = [p for p in fav_prods if p]
    text, markup = _build_fav_list_content(fav_prods, cart, query)
    await fsm_edit(state, message, text, reply_markup=markup, parse_mode="HTML")


@sales_router.message(SearchStates.sale_recent)
async def slr_rec_srch_process(message: Message, state: FSMContext):
    """Поиск по Недавним."""
    query = (message.text or "").strip()
    await state.set_state(MultipleSaleStates.adding_items)
    current_db = await get_db(message.from_user.id, state)
    user_row = await current_db.get_user(message.from_user.id)
    if not user_row:
        return
    data = await state.get_data()
    cart = data.get("sale_cart", [])
    u_db_id = user_row[0]
    recent = await current_db.get_user_recent_products(u_db_id, limit=8) or []
    text, markup = _build_recent_list_content(recent, cart, query)
    await fsm_edit(state, message, text, reply_markup=markup, parse_mode="HTML")


@sales_router.callback_query(F.data == "sale_back_to_cats")
async def sale_back_to_cats(callback: CallbackQuery, state: FSMContext):
    """Возврат к экрану выбора категорий из подэкранов (Избранное / Недавние)."""
    await callback.answer()
    current_db  = await get_db(callback.from_user.id, state)
    data        = await state.get_data()
    shop_name   = data.get("sale_current_shop") or data.get("shop_name", "")
    allow_change = data.get("sale_allow_change", False)
    await _show_sale_categories(
        callback, state, current_db, shop_name,
        allow_change=allow_change, reset_cart=False
    )


# ──────────────────────────────────────────────────────────────────────────────
# ПРОДАЖА / СПИСАНИЕ С ДРУГОГО МАГАЗИНА ТОРГОВОЙ СЕТИ
# ──────────────────────────────────────────────────────────────────────────────

async def _build_cross_shop_screen(callback: CallbackQuery, state: FSMContext, current_db):
    """Экран выбора магазина — поддерживает торговые сети и орг-магазины без trade_network."""
    data = await state.get_data()
    trade_network = data.get("sale_network") or ""
    current_shop = data.get("shop_name", "")

    builder = InlineKeyboardBuilder()

    if trade_network:
        # Режим торговой сети: фильтрация по сети
        net_shops = await current_db.get_shops_by_network(trade_network)
        if not net_shops:
            await callback.answer("❌ Нет других магазинов в сети.", show_alert=True)
            return
        cities = await current_db.get_cities_by_network(trade_network)
        if len(cities) > 1:
            builder.row(InlineKeyboardButton(text="🏙 Выберите город:", callback_data="pg_noop"))
            for city in sorted(cities):
                builder.row(InlineKeyboardButton(
                    text=f"📍 {he(city)}",
                    callback_data=safe_cb("sale_cty_", city)
                ))
        else:
            builder.row(InlineKeyboardButton(text="🏪 Выберите магазин:", callback_data="pg_noop"))
            for shop_name, city in sorted(net_shops, key=lambda x: x[0]):
                mark = " ✓" if shop_name == current_shop else ""
                builder.row(InlineKeyboardButton(
                    text=f"🏪 {he(shop_name)}{mark}",
                    callback_data=safe_cb("sale_net_", shop_name)
                ))
        header_line = f"🌐 Сеть: {he(trade_network)}\n"
    else:
        # Режим организации: магазины с реальными остатками в инвентаре
        all_shops = await current_db.get_inventory_shops()
        if not all_shops or len(all_shops) < 2:
            await callback.answer("❌ Нет других магазинов.", show_alert=True)
            return
        builder.row(InlineKeyboardButton(text="🏪 Выберите магазин:", callback_data="pg_noop"))
        for shop in sorted(all_shops):
            mark = " ✓" if shop == current_shop else ""
            builder.row(InlineKeyboardButton(
                text=f"🏪 {he(shop)}{mark}",
                callback_data=safe_cb("sale_net_", shop)
            ))
        header_line = ""

    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="new_sale"))

    cart = data.get("sale_cart", [])
    cart_note = f"\n⚠️ Корзина будет сброшена при смене магазина ({len(cart)} поз.)" if cart else ""

    await callback.message.edit_text(
        f"🔄 <b>Выбор магазина для списания</b>\n"
        f"{header_line}{cart_note}\n"
        f"Выберите магазин:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data == "sale_change_shop")
async def sale_change_shop(callback: CallbackQuery, state: FSMContext):
    """Открывает экран выбора другого магазина в торговой сети."""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    await _build_cross_shop_screen(callback, state, current_db)


@sales_router.callback_query(F.data.startswith("sale_cty_"))
async def sale_filter_by_city(callback: CallbackQuery, state: FSMContext):
    """Выбор города → показывает магазины в этом городе."""
    current_db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    trade_network = data.get("sale_network", "")
    current_shop = data.get("shop_name", "")

    city_raw = callback.data.replace("sale_cty_", "")
    all_cities = await current_db.get_cities_by_network(trade_network) if trade_network else []
    city = resolve_cb_name(city_raw, all_cities)

    if not city:
        await callback.answer("❌ Город не найден.", show_alert=True)
        return

    await callback.answer()

    net_shops = await current_db.get_shops_by_network(trade_network)
    city_shops = [(s, c) for s, c in net_shops if c == city]

    if not city_shops:
        await callback.message.edit_text(
            f"❌ В городе <b>{he(city)}</b> нет магазинов сети {he(trade_network)}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="sale_change_shop")]
            ]),
            parse_mode="HTML"
        )
        return

    builder = InlineKeyboardBuilder()
    for shop_name, _ in sorted(city_shops, key=lambda x: x[0]):
        mark = " ✓" if shop_name == current_shop else ""
        builder.row(InlineKeyboardButton(
            text=f"🏪 {he(shop_name)}{mark}",
            callback_data=safe_cb("sale_net_", shop_name)
        ))
    builder.row(InlineKeyboardButton(text="⬅️ К городам", callback_data="sale_change_shop"))

    cart = data.get("sale_cart", [])
    cart_note = f"\n⚠️ Корзина будет сброшена ({len(cart)} поз.)" if cart else ""

    await callback.message.edit_text(
        f"📍 <b>{he(city)}</b> — магазины сети {he(trade_network)}{cart_note}\n\n"
        f"Выберите магазин:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data.startswith("sale_net_"))
async def sale_select_network_shop(callback: CallbackQuery, state: FSMContext):
    """Выбор конкретного магазина из сети/орг — переходит к категориям."""
    current_db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    trade_network = data.get("sale_network") or ""

    shop_raw = callback.data.replace("sale_net_", "")
    if trade_network:
        net_shops = await current_db.get_shops_by_network(trade_network)
        all_shop_names = [s for s, _ in net_shops]
    else:
        # Орг-режим: магазины с реальными остатками в инвентаре
        all_shop_names = await current_db.get_inventory_shops()
    shop_name = resolve_cb_name(shop_raw, all_shop_names)

    if not shop_name:
        await callback.answer("❌ Магазин не найден. Попробуйте снова.", show_alert=True)
        return

    await callback.answer()

    # Проверяем: есть ли вообще товары с остатком в выбранном магазине
    inv_shops_with_stock = await current_db.get_inventory_shops()
    if shop_name not in inv_shops_with_stock:
        # Магазин зарегистрирован, но остатков нет вообще
        home_shop = data.get("sale_home_shop", shop_name)
        await callback.message.edit_text(
            f"⚠️ <b>Магазин «{he(shop_name)}» пуст</b>\n\n"
            f"В этом магазине нет ни одного товара в наличии.\n"
            f"Обратитесь к администратору для пополнения остатков.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Другой магазин", callback_data="sale_change_shop")],
                [InlineKeyboardButton(text=f"🏪 Вернуться в «{he(home_shop)[:30]}»", callback_data="new_sale")],
            ]),
            parse_mode="HTML"
        )
        return

    # Всё ок — показываем категории для выбранного магазина
    await _show_sale_categories(
        callback, state, current_db, shop_name,
        allow_change=True,
        reset_cart=True,
    )


@sales_router.callback_query(F.data == "sale_quick_search")
async def quick_search_start(callback: CallbackQuery, state: FSMContext):
    """Запуск быстрого поиска товара по названию"""
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🔍 <b>Быстрый поиск товара</b>\n\n"
        "Введите название, категорию или артикул (SKU) товара:",
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

    all_products = await current_db.get_all_products()
    query_lower = query.lower()

    matching = []
    for p in all_products:
        pid, name, category, price = p[0], p[1], p[2], p[3]
        article = p[7] if len(p) > 7 and p[7] else ""
        qty = await current_db.get_inventory(shop_name, pid)
        if qty > 0 and (query_lower in name.lower() or query_lower in article.lower()):
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


async def _build_sale_product_list_content(shop_name: str, category: str, current_db, home_shop: str, query: str = ""):
    """Строит текст и клавиатуру для списка товаров категории с опциональным фильтром."""
    products = await current_db.get_all_products()
    category_products = [p for p in products if p[2] == category]

    available = []
    for product in category_products:
        pid, name, price = product[0], product[1], product[3]
        qty = await current_db.get_inventory(shop_name, pid)
        if qty > 0:
            available.append((pid, name, price, qty))

    if query:
        q = query.lower()
        filtered = [(pid, name, price, qty) for pid, name, price, qty in available if q in name.lower()]
    else:
        filtered = available

    is_other = shop_name != home_shop
    shop_note = f" <i>(из «{he(shop_name)}»)</i>" if is_other else ""

    builder = InlineKeyboardBuilder()
    for pid, name, price, qty in filtered:
        color = get_stock_color_indicator(qty)
        builder.add(InlineKeyboardButton(
            text=f"{color} {name} - {qty} шт. × {price}₽",
            callback_data=f"sale_product_{pid}"
        ))
    builder.add(InlineKeyboardButton(text="🔍 Найти товар", callback_data="sale_srch_prd_start"))
    if query:
        builder.add(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="sale_srch_prd_cancel"))
    builder.add(InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale"))
    builder.adjust(1)

    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    text = f"📂 {he(category)}{shop_note}{suffix}\n\nВыберите товар для продажи:"
    return text, builder.as_markup()


async def _show_sale_product_list(message, shop_name: str, category: str, current_db, home_shop: str, query: str = ""):
    """Редактирует сообщение бота, показывая список товаров категории."""
    text, markup = await _build_sale_product_list_content(shop_name, category, current_db, home_shop, query)
    await message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@sales_router.callback_query(F.data.startswith("sale_category_"))
async def select_sale_category(callback: CallbackQuery, state: FSMContext):
    """Выбор категории для продажи"""
    await callback.answer()
    category_raw = callback.data.replace("sale_category_", "")
    current_db = await get_db(callback.from_user.id, state)
    category = resolve_cb_name(category_raw, await current_db.get_all_categories() or [])

    # Получаем товары этой категории
    products = await current_db.get_all_products()
    category_products = [p for p in products if p[2] == category]

    if not category_products:
        await callback.message.edit_text(
            "❌ В этой категории нет товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="new_sale")]])
        )
        return

    data = await state.get_data()
    shop_name = data['shop_name']
    home_shop = data.get("sale_home_shop", shop_name)

    # Проверяем доступность хотя бы одного товара
    available_check = False
    for product in category_products:
        if await current_db.get_inventory(shop_name, product[0]) > 0:
            available_check = True
            break

    if not available_check:
        allow_change2 = data.get("sale_allow_change", False)
        is_other2 = shop_name != home_shop
        extra_buttons = []
        if allow_change2:
            extra_buttons.append([InlineKeyboardButton(text="🔄 Выбрать другой магазин", callback_data="sale_change_shop")])
            if is_other2:
                extra_buttons.append([InlineKeyboardButton(
                    text=f"🏪 Вернуться в «{he(home_shop)[:28]}»", callback_data="new_sale"
                )])
        extra_buttons.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="new_sale")])
        shop_label = f"«{he(shop_name)}»" if is_other2 else "вашем магазине"
        await callback.message.edit_text(
            f"📂 {he(category)}\n\n❌ Нет товаров в наличии в {shop_label}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=extra_buttons),
            parse_mode="HTML"
        )
        return

    await state.update_data(sale_current_category=category, anchor_msg_id=callback.message.message_id)
    await _show_sale_product_list(callback.message, shop_name, category, current_db, home_shop)


@sales_router.callback_query(F.data == "sale_srch_prd_start")
async def sale_srch_prd_start(callback: CallbackQuery, state: FSMContext):
    """Запуск поиска товара внутри категории."""
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.product_sale)
    await callback.message.edit_text(
        "🔍 <b>Поиск товара</b>\n\nВведите название или его часть:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="sale_srch_prd_cancel")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@sales_router.callback_query(F.data == "sale_srch_prd_cancel")
async def sale_srch_prd_cancel(callback: CallbackQuery, state: FSMContext):
    """Сброс поиска — возвращает полный список товаров категории."""
    await state.set_state(MultipleSaleStates.adding_items)
    data = await state.get_data()
    shop_name = data.get("shop_name", "")
    home_shop = data.get("sale_home_shop", shop_name)
    category = data.get("sale_current_category", "")
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    await _show_sale_product_list(callback.message, shop_name, category, current_db, home_shop)


@sales_router.message(SearchStates.product_sale)
async def sale_srch_prd_process(message: Message, state: FSMContext):
    """Обрабатывает поисковый запрос товара внутри категории."""
    query = (message.text or "").strip()
    await state.set_state(MultipleSaleStates.adding_items)
    data = await state.get_data()
    shop_name = data.get("shop_name", "")
    home_shop = data.get("sale_home_shop", shop_name)
    category = data.get("sale_current_category", "")
    current_db = await get_db(message.from_user.id, state)
    text, markup = await _build_sale_product_list_content(shop_name, category, current_db, home_shop, query)
    await fsm_edit(state, message, text, reply_markup=markup, parse_mode="HTML")

@sales_router.callback_query(F.data.startswith("sale_product_"))
async def select_sale_product(callback: CallbackQuery, state: FSMContext):
    """Выбор товара для продажи"""
    product_id = int(callback.data.replace("sale_product_", ""))
    current_db = await get_db(callback.from_user.id, state)

    product = await current_db.get_product(product_id)

    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return

    data = await state.get_data()
    shop_name = data['shop_name']
    quantity = await current_db.get_inventory(shop_name, product_id)

    if quantity <= 0:
        # Уточняем сообщение: если выбран другой магазин — говорим какой именно
        fsm_d = await state.get_data()
        home_shop = fsm_d.get("sale_home_shop", shop_name)
        if shop_name != home_shop:
            await callback.answer(f"❌ Нет в наличии в «{shop_name}»!", show_alert=True)
        else:
            await callback.answer("❌ Товар отсутствует в наличии!", show_alert=True)
        return

    await callback.answer()
    await state.update_data(product_id=product_id)

    # Получаем информацию о мотивации для товара
    motivation_info = await current_db.get_product_motivation(product_id)
    motivation_text = ""
    if motivation_info:
        if motivation_info['motivation_type'] == 'percentage':
            motivation_text = f"\n🎯 Мотивация: {motivation_info['motivation_value']}% от продажи"
        else:
            motivation_text = f"\n🎯 Мотивация: {format_currency(motivation_info['motivation_value'])} за шт."
    else:
        motivation_text = "\n🎯 Мотивация: не установлена"

    # Показываем из какого магазина идёт списание
    fsm_d2 = await state.get_data()
    home_shop2 = fsm_d2.get("sale_home_shop", shop_name)
    shop_line = (
        f"\n🏪 Списание с: <b>{he(shop_name)}</b> <i>(другой магазин)</i>"
        if shop_name != home_shop2 else f"\n🏪 Магазин: {he(shop_name)}"
    )

    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"💰 Продажа товара:{shop_line}\n\n"
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

        current_quantity = await current_db.get_inventory(shop_name, product_id)

        if quantity > current_quantity:
            await fsm_edit(state, message,
                           f"📦 ❌ Недостаточно товара! В наличии: {current_quantity} шт. Введите меньшее количество:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))
            return

        # Сохраняем количество и показываем опции цены
        await state.update_data(quantity=quantity)

        product = await current_db.get_product(product_id)
        default_price = product[3]

        # Получаем информацию о мотивации и рассчитываем предварительную сумму
        motivation_info = await current_db.get_product_motivation(product_id)
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
        product = await current_db.get_product(product_id)
        max_qty = await current_db.get_inventory(shop_name, product_id)
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

    max_qty = await current_db.get_inventory(shop_name, product_id)
    if quantity > max_qty:
        await callback.answer(f"❌ В наличии только {max_qty} шт.", show_alert=True)
        return

    await callback.answer()
    product = await current_db.get_product(product_id)
    default_price = product[3]

    motivation_info = await current_db.get_product_motivation(product_id)
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
    product = await current_db.get_product(product_id)

    if not product:
        await callback.message.edit_text(
            "❌ Товар не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К продажам", callback_data="new_sale")]])
        )
        return

    # Получаем информацию о мотивации и рассчитываем заработок
    motivation_info = await current_db.get_product_motivation(product_id)
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
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="➕ Добавить еще товар", callback_data="add_more_items"),
        InlineKeyboardButton(text="🛒 Просмотр корзины", callback_data="view_cart"),
        InlineKeyboardButton(text="✅ Завершить продажу", callback_data="complete_sale"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")
    )
    builder.adjust(1)

    await callback.message.edit_text(
        f"✅ Товар добавлен в корзину!\n\n"
        f"🏷 {he(product[1])}\n"
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
    product = await current_db.get_product(product_id)

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
        product = await current_db.get_product(product_id)

        if not product:
            await fsm_edit(state, message, "❌ Товар не найден! Попробуйте начать продажу заново.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_sale")]]))
            return

        # Получаем информацию о мотивации и рассчитываем заработок
        motivation_info = await current_db.get_product_motivation(product_id)
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
        builder = InlineKeyboardBuilder()
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
    """Добавление ещё товаров в корзину — переиспользует экран категорий без сброса корзины."""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    shop_name = data.get("shop_name", "")
    allow_change = data.get("sale_allow_change", False)

    if not shop_name:
        await callback.message.edit_text(
            "❌ Сессия устарела. Начните продажу заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Новая продажа", callback_data="new_sale")]
            ])
        )
        return

    await _show_sale_categories(
        callback, state, current_db,
        shop_name=shop_name,
        allow_change=allow_change,
        reset_cart=False,
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
        try:
            from subscription_utils import check_sales_limit
            ok, msg = await asyncio.to_thread(check_sales_limit, callback.from_user.id)
        except Exception as _limit_err:
            logging.warning(f"check_sales_limit error (ignored, allowing sale): {_limit_err}")
            ok, msg = True, None
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

    try:
        user_id = await current_db.get_user_id(callback.from_user.id)
    except Exception as _uid_err:
        logging.error(f"get_user_id failed in complete_sale: {_uid_err!r}")
        try:
            await callback.answer("❌ Ошибка доступа к базе данных. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
        return

    if not user_id:
        try:
            await callback.answer("❌ Пользователь не найден!", show_alert=True)
        except Exception:
            pass
        return

    # Снимок прогресса планов ДО продажи (для определения только что выполненных)
    try:
        _snap_before = await current_db.get_user_plans_progress(callback.from_user.id)
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
    _sales_committed = False

    try:
        # Сначала проверяем остатки для всех товаров
        for item in sale_cart:
            product_id = item['product_id']
            quantity = item['quantity']

            current_stock = await current_db.get_inventory(shop_name, product_id)

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
                sale_id = await current_db.add_sale(product_id, shop_name, quantity, user_id, price)

                if sale_id and sale_id > 0:
                    processed_sales.append(sale_id)

                    # Получаем новые остатки после продажи
                    new_quantity = await current_db.get_inventory(shop_name, product_id)

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
                logging.error(f"Ошибка при add_sale: {sale_error!r}")
                failed = True
                break

        if failed:
            # Откатываем все продажи в случае ошибки
            for sale_id in processed_sales:
                try:
                    await current_db.delete_sale(sale_id)
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

        # ── Точка невозврата ──────────────────────────────────────────────
        # Продажи зафиксированы в БД. Любая ошибка НИЖЕ (формирование сообщения,
        # уведомления, экспорт в Google Sheets) НЕ должна откатывать продажи —
        # иначе сбой в побочном эффекте приведёт к потере реальной продажи.
        _sales_committed = True

        # Планы, которые только что перешли через 100%
        try:
            _snap_after = await current_db.get_user_plans_progress(callback.from_user.id)
            _completed_now = [
                (plan, actual, pct) for plan, actual, pct in _snap_after
                if pct >= 100 and _plans_before_pct.get(plan[0], 0) < 100
            ]
        except Exception:
            _completed_now = []

        try:
            user = await current_db.get_user(callback.from_user.id)
            user_shop = user[8] if user and len(user) > 8 else "Неизвестный магазин"
        except Exception:
            user = None
            user_shop = shop_name or "Неизвестный магазин"

        # ── Google Sheets integration trigger (до формирования сообщения) ──
        _gs_status = []
        try:
            from integration.manager import integration_manager as _int_mgr
            from datetime import datetime as _dt
            _seller_name = f"{user[2] or ''} {user[3] or ''}".strip() if user else ''
            _sync_db = object.__getattribute__(current_db, '_db') if hasattr(current_db, '_db') else current_db
            _now_str = _dt.now().strftime('%Y-%m-%d %H:%M')
            if len(results) == 1:
                _sale_event = {
                    'date': _now_str,
                    'shop_name': shop_name,
                    'quantity': results[0]['quantity'],
                    'total': results[0]['total'],
                    'seller_name': _seller_name,
                    'product_name': results[0]['name'],
                    'price': results[0]['price'],
                    'category': '',
                }
                _gs_status = await _int_mgr.trigger_export_with_result(_sync_db, 'sales', _sale_event)
            else:
                # Мультикорзина: запускаем экспорт отдельно для каждого товара,
                # чтобы update_cell мог найти колонку по реальному названию товара.
                _all_gs = []
                for _item in results:
                    _item_event = {
                        'date': _now_str,
                        'shop_name': shop_name,
                        'quantity': _item['quantity'],
                        'total': _item['total'],
                        'seller_name': _seller_name,
                        'product_name': _item['name'],
                        'price': _item['price'],
                        'category': '',
                    }
                    _item_res = await _int_mgr.trigger_export_with_result(_sync_db, 'sales', _item_event)
                    _all_gs.extend(_item_res)
                _gs_status = _all_gs
        except Exception as _ie:
            logging.warning(f"integration trigger_export_with_result (sales): {_ie}")

        # Формируем отчет о продаже с информацией о мотивации
        message_text = "✅ Продажа успешно зарегистрирована!\n\n"

        for result in results:
            # Добавляем информацию о мотивации для каждого товара, если она есть
            earning_text = f"\n   🎯 Ваша мотивация: {format_currency(result['expected_earning'])}" if result['expected_earning'] > 0 else ""

            message_text += f"🏷 {he(result['name'])}\n"
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
            newly_hit = await current_db.check_and_mark_plan_milestones(callback.from_user.id)
            _PERIOD2 = {'monthly': 'Месяц', 'weekly': 'Неделя', 'daily': 'День', 'quarter': 'Квартал'}
            for _plan, _actual, _pct, _ms in newly_hit:
                if _ms < 100:
                    _label = _PERIOD2.get(_plan[1], _plan[1])
                    _icon = "🟡" if _ms == 50 else "🟠"
                    message_text += f"\n{_icon} Выполнено {_ms}% плана ({_label})!"
        except Exception:
            pass

        # Статус экспорта в Google Таблицы (только если экспорт настроен)
        if _gs_status:
            if all(r['success'] for r in _gs_status):
                message_text += "\n\n📋 Google Таблицы: ✅ Записано"
            else:
                _err_details = "; ".join(
                    r['error'] for r in _gs_status if not r['success'] and r.get('error')
                )
                _is_revoked = _err_details and "Авторизация Google отозвана" in _err_details
                if _is_revoked:
                    message_text += (
                        "\n\n📋 Google Таблицы: 🔑 Требуется переподключение\n"
                        "Зайдите в Управление орг. → Интеграции и переавторизуйте аккаунт."
                    )
                elif _err_details:
                    message_text += f"\n\n📋 Google Таблицы: ⚠️ Ошибка записи\n{_err_details}"
                else:
                    message_text += "\n\n📋 Google Таблицы: ⚠️ Ошибка записи"

        try:
            await callback.answer()
        except Exception:
            pass
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

        # ── Уведомления коллегам по смене ─────────────────────────────────
        # Отправляем пуш всем, кто привязан к этому магазину, у кого
        # в графике стоит сегодняшний рабочий день и включена опция.
        try:
            from bot_holder import get_bot as _get_bot
            bot = _get_bot()
            if not bot:
                raise RuntimeError("bot not initialized")
            from notif_utils import add_read_btn as _add_read_btn
            _usr_tz_cw = await current_db.get_user_timezone(callback.from_user.id)
            today_str = _gcur_tz(_usr_tz_cw).date().isoformat()
            coworkers = await current_db.get_shop_coworkers_on_shift(
                shop_name, today_str, user_id
            )
            if coworkers:
                seller_name = f"{user[2] or ''} {user[3] or ''}".strip() if user else ""
                notif_lines = [f"🛍 <b>Новая продажа в магазине {he(shop_name)}</b>"]
                if seller_name:
                    notif_lines.append(f"👤 Продавец: {he(seller_name)}")
                for r in results:
                    _earn = r.get('expected_earning', 0) or 0
                    _earn_part = f" 💰 {format_currency(_earn)}" if _earn > 0 else ""
                    notif_lines.append(
                        f"• {he(r['name'])}: {r['quantity']} шт. "
                        f"× {format_currency(r['price'])} = {format_currency(r['total'])}{_earn_part}"
                    )
                _total_earn_notif = sum(r.get('expected_earning', 0) or 0 for r in results)
                if _total_earn_notif > 0:
                    notif_lines.append(f"\n💰 Мотивация продавца: {format_currency(_total_earn_notif)}")
                notif_text = "\n".join(notif_lines)
                for cw_uid, cw_tgid, _cw_name in coworkers:
                    try:
                        await bot.send_message(
                            int(cw_tgid),
                            notif_text,
                            parse_mode="HTML",
                            reply_markup=_add_read_btn()
                        )
                        await current_db.add_notification_to_history(
                            cw_uid, 'shift_sale', notif_text
                        )
                    except Exception as _send_err:
                        logging.warning(f"shift_sale notif to {cw_tgid}: {_send_err}")
        except Exception as _notif_err:
            logging.error(f"Ошибка уведомлений коллег по смене: {_notif_err}")

        try:
            await clear_state_keep_org(state)
        except Exception as _cls_err:
            logging.warning(f"complete_sale: clear_state_keep_org error: {_cls_err}")

    except Exception as e:
        logging.error(f"Ошибка в complete_sale: {e!r}", exc_info=True)

        # Откатываем продажи ТОЛЬКО если ошибка произошла ДО фиксации (этап
        # регистрации). После точки невозврата реальные продажи не трогаем.
        if not _sales_committed:
            for sale_id in processed_sales:
                try:
                    await current_db.delete_sale(sale_id)
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
    user = await current_db.get_user(callback.from_user.id)
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
    """Хаб управления продажами (для админа) или прямой вход (для сотрудника)"""
    await callback.answer()
    await clear_state_keep_org(state)
    is_admin = is_any_admin(callback.from_user.id)
    if not is_admin:
        await edit_sales_start(callback, state)
        return
    from keyboards import edit_sales_hub
    await callback.message.edit_text(
        "📝 <b>Управление продажами</b>\n\nВыберите действие:",
        reply_markup=edit_sales_hub(),
        parse_mode="HTML"
    )

@sales_router.callback_query(F.data == "edit_sales_start")
async def edit_sales_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса редактирования продаж (выбор периода)"""
    try:
        await callback.answer()
    except Exception:
        pass
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
    _usr_tz_est = await current_db.get_user_timezone(callback.from_user.id)
    today = _gcur_tz(_usr_tz_est).date().strftime('%Y-%m-%d')

    if is_admin:
        # Для админа - выбор магазина (включая магазины без сотрудников)
        await state.update_data(edit_start_date=today, edit_end_date=today)

        shops = sorted(set(await current_db.get_all_shops() or []) | set(await current_db.get_inventory_shops() or []))
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

    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.message.edit_text(
            "❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]])
        )
        return

    # Получаем продажи за сегодня
    sales = await current_db.get_user_sales_by_date(user_id, today, today)

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
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, await current_db.get_all_shops() or [])
    data = await state.get_data()
    start_date = data.get('edit_start_date')
    end_date = data.get('edit_end_date')

    # Получаем все продажи магазина за период
    sales = await current_db.get_shop_sales_by_date(shop_name, start_date, end_date)
    
    if not sales:
        await callback.message.edit_text(
            f"📝 В магазине '{shop_name}' нет продаж за выбранный период.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]])
        )
        return
        
    period_title = f"Магазин {shop_name}"
    await show_sales_for_edit(callback, sales, state, period_title)

async def show_sales_for_edit(callback_or_msg, sales, state: FSMContext,
                              period_title: str, page: int = 0):
    """Показывает список продаж для редактирования (с пагинацией + фильтр по категории).
    Принимает CallbackQuery или Message — во втором случае редактирует якорь через fsm_edit."""
    from aiogram.types import Message as _Msg
    _is_msg = isinstance(callback_or_msg, _Msg)
    user_id = callback_or_msg.from_user.id

    await state.update_data(edit_sales_cache=sales, edit_sales_title=period_title)
    state_data = await state.get_data()
    cat_filter = state_data.get("edit_sales_cat_filter")

    # Применяем фильтр по категории для отображения
    view = [s for s in sales if (s[8] if len(s) > 8 else "") == cat_filter] if cat_filter else sales

    # Кнопки категории
    cat_label = f"🗂 {cat_filter[:18]} ✕" if cat_filter else "🗂 Категория"
    cat_cb    = "esl_cat_clear" if cat_filter else "esl_cat_pick"

    display_title = f"{period_title} · 📂 {cat_filter}" if cat_filter else period_title
    message_text  = f"📝 <b>Редактирование продаж</b> — {he(display_title)}\n\n"

    builder = InlineKeyboardBuilder()

    if not view:
        if cat_filter:
            message_text += f"📂 В категории «{he(cat_filter)}» продаж не найдено."
        else:
            message_text += "❌ Продажи не найдены."
        builder.row(InlineKeyboardButton(text=cat_label, callback_data=cat_cb))
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="edit_sales"))
    else:
        page_items, has_prev, has_next, total_pages, page = paginate(view, page, PAGE_SIZE_SALES)
        pg_info = f"· стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
        message_text += f"Продаж: {len(view)} {pg_info}\nВыберите для редактирования:\n\n"

        from utils import format_date_for_user
        offset = page * PAGE_SIZE_SALES
        for i, sale in enumerate(page_items, offset + 1):
            sale_id      = sale[0]
            quantity     = sale[3]
            sale_price   = sale[4]
            sale_date    = sale[6]
            product_name = sale[7] if len(sale) > 7 else "Неизвестный товар"
            total        = quantity * sale_price
            formatted_date = format_date_for_user(sale_date, user_id)

            seller_line = ""
            if len(sale) >= 11 and sale[9]:
                seller_full = (f"{sale[9]} {sale[10] or ''}").strip()
                seller_line = f"   👤 {he(seller_full)}\n"

            message_text += f"{i}. 🏷 {he(product_name)}\n"
            message_text += f"   📦 {quantity} × {format_currency(sale_price)} = {format_currency(total)}\n"
            message_text += seller_line
            message_text += f"   📅 {formatted_date}\n\n"
            builder.add(InlineKeyboardButton(
                text=f"✏️ {i}. {product_name[:30]}",
                callback_data=f"edit_sale_{sale_id}"
            ))

        nav = page_nav_row("esl_pg_", page, has_prev, has_next, total_pages)
        if nav:
            builder.row(*nav)
        builder.row(
            InlineKeyboardButton(text="🔍 Поиск",  callback_data="esl_srch_start"),
            InlineKeyboardButton(text=cat_label,   callback_data=cat_cb),
        )
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="edit_sales"))
        builder.adjust(1)

    await state.update_data(edit_sales_page_num=page)
    kb = builder.as_markup()
    if _is_msg:
        await fsm_edit(state, callback_or_msg, message_text, reply_markup=kb, parse_mode="HTML")
    else:
        await callback_or_msg.message.edit_text(message_text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(EditSaleStates.choosing_sale)


@sales_router.callback_query(F.data == "esl_srch_start")
async def esl_srch_start(callback: CallbackQuery, state: FSMContext):
    """Начало поиска в списке продаж для редактирования"""
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.edit_sales_srch)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="edit_sales_start")
    await callback.message.edit_text(
        "🔍 <b>Поиск продажи</b>\n\nВведите название товара или его часть:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@sales_router.message(SearchStates.edit_sales_srch)
async def esl_srch_process(message: Message, state: FSMContext):
    """Фильтрация кэшированных продаж по названию товара (с учётом активного фильтра категории)"""
    query = (message.text or "").strip().lower()
    await state.set_state(None)
    data = await state.get_data()
    sales      = data.get("edit_sales_cache", [])
    title      = data.get("edit_sales_title", "Продажи")
    cat_filter = data.get("edit_sales_cat_filter")

    # Сначала применяем фильтр категории, затем текстовый
    base = [s for s in sales if (s[8] if len(s) > 8 else "") == cat_filter] if cat_filter else sales

    if query:
        matched = [s for s in base if query in (s[7] if len(s) > 7 else "").lower()]
    else:
        matched = base

    suffix     = f" · 🔍 «{query}»" if query else ""
    cat_suffix = f" · 📂 {cat_filter}" if cat_filter else ""
    await show_sales_for_edit(message, matched, state, title + cat_suffix + suffix, page=0)


# ─── Фильтр по категории в списке продаж ─────────────────────────────────────

@sales_router.callback_query(F.data == "esl_cat_pick")
async def esl_cat_pick(callback: CallbackQuery, state: FSMContext):
    """Открыть пикер категорий для фильтрации списка продаж"""
    data       = await state.get_data()
    sales      = data.get("edit_sales_cache", [])
    cat_filter = data.get("edit_sales_cat_filter")

    from collections import Counter
    cat_counts = Counter((s[8] if len(s) > 8 else "Без категории") for s in sales)

    if not cat_counts:
        await callback.answer("Категории не найдены.", show_alert=True)
        return
    await callback.answer()

    builder = InlineKeyboardBuilder()
    for cat, count in sorted(cat_counts.items()):
        marker = "✅ " if cat == cat_filter else ""
        builder.add(InlineKeyboardButton(
            text=f"{marker}📂 {cat} ({count})",
            callback_data=safe_cb("esl_cat_set_", cat)
        ))
    builder.row(InlineKeyboardButton(text=f"📋 Все категории ({len(sales)})", callback_data="esl_cat_clear"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="esl_cat_back"))
    builder.adjust(1)

    await callback.message.edit_text(
        f"🗂 <b>Фильтр по категории</b>\n\nВсего продаж: {len(sales)}\nВыберите категорию:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data.startswith("esl_cat_set_"))
async def esl_cat_set(callback: CallbackQuery, state: FSMContext):
    """Применить фильтр по выбранной категории"""
    await callback.answer()
    raw   = callback.data[len("esl_cat_set_"):]
    data  = await state.get_data()
    sales = data.get("edit_sales_cache", [])
    title = data.get("edit_sales_title", "Продажи")

    all_cats = list({(s[8] if len(s) > 8 else "Без категории") for s in sales})
    cat_name = resolve_cb_name(raw, all_cats)

    await state.update_data(edit_sales_cat_filter=cat_name)
    await show_sales_for_edit(callback, sales, state, title, page=0)


@sales_router.callback_query(F.data == "esl_cat_clear")
async def esl_cat_clear(callback: CallbackQuery, state: FSMContext):
    """Сбросить фильтр по категории — показать все продажи"""
    await callback.answer()
    await state.update_data(edit_sales_cat_filter=None)
    data  = await state.get_data()
    sales = data.get("edit_sales_cache", [])
    title = data.get("edit_sales_title", "Продажи")
    await show_sales_for_edit(callback, sales, state, title, page=0)


@sales_router.callback_query(F.data == "esl_cat_back")
async def esl_cat_back(callback: CallbackQuery, state: FSMContext):
    """Вернуться к списку продаж из пикера категорий"""
    await callback.answer()
    data  = await state.get_data()
    sales = data.get("edit_sales_cache", [])
    title = data.get("edit_sales_title", "Продажи")
    await show_sales_for_edit(callback, sales, state, title, page=0)


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
    current_db = await get_db(telegram_id, state) if telegram_id else wrap_db(Database('data/shop_bot.db'))
    # Получаем данные о продаже
    sale = await current_db.get_sale_by_id(sale_id)
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
    user_data = await current_db.get_user_by_id(user_id_from_sale)
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
        InlineKeyboardButton(text="📅 Изменить дату", callback_data="edit_sale_date"),
        InlineKeyboardButton(text="📋 История изменений", callback_data=f"sale_audit_{sale_id}"),
        InlineKeyboardButton(text="🗑 Удалить продажу", callback_data="delete_sale"),
        InlineKeyboardButton(text="⬅️ К списку", callback_data="back_to_sales_list")
    )
    builder.adjust(1)

    await message.edit_text(
        f"📝 <b>Редактирование продажи</b>\n\n"
        f"🏷 Товар: <b>{he(product_name)}</b>\n"
        f"📦 Количество: {quantity} шт.\n"
        f"💰 Цена продажи: {format_currency(sale_price)}\n"
        f"💸 Сумма: {format_currency(total)}\n"
        f"📅 Дата: {formatted_date}\n\n"
        f"Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@sales_router.callback_query(F.data.regexp(r'^edit_sale_\d+$'))
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
        _editor_id = await current_db.get_user_id(message.from_user.id)
        if await current_db.update_sale(sale_id, new_quantity, changed_by=_editor_id):
            await state.update_data(current_quantity=new_quantity)
            await fsm_edit(
                state, message,
                f"✅ <b>Количество изменено!</b>\n\n"
                f"🏷 Товар: <b>{he(data['product_name'])}</b>\n"
                f"📦 Новое количество: <b>{new_quantity} шт.</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Ещё изменить", callback_data="back_to_edit_sale")],
                    [InlineKeyboardButton(text="⬅️ К списку продаж", callback_data="back_to_sales_list")],
                    [InlineKeyboardButton(text="📋 Меню", callback_data="edit_sales")],
                ]),
            )
            await state.set_state(EditSaleStates.choosing_sale)
        else:
            await fsm_edit(state, message, "❌ Ошибка при обновлении количества!", reply_markup=_cancel_kb)
        
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
        _editor_id = await current_db.get_user_id(message.from_user.id)
        if await current_db.update_sale(sale_id, quantity, sale_price=new_price, changed_by=_editor_id):
            await state.update_data(price=new_price)
            await fsm_edit(
                state, message,
                f"✅ <b>Цена изменена!</b>\n\n"
                f"🏷 Товар: <b>{he(data['product_name'])}</b>\n"
                f"💰 Новая цена: <b>{format_currency(new_price)}</b>\n"
                f"📦 Количество: {quantity} шт.\n"
                f"💸 Новая сумма: <b>{format_currency(new_price * quantity)}</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Ещё изменить", callback_data="back_to_edit_sale")],
                    [InlineKeyboardButton(text="⬅️ К списку продаж", callback_data="back_to_sales_list")],
                    [InlineKeyboardButton(text="📋 Меню", callback_data="edit_sales")],
                ]),
            )
            await state.set_state(EditSaleStates.choosing_sale)
        else:
            await fsm_edit(state, message, "❌ Ошибка при обновлении цены!", reply_markup=_cancel_kb)
        
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
    if await current_db.delete_sale(sale_id):
        # Убираем удалённую продажу из кеша, чтобы список был актуален
        cached = data.get('edit_sales_cache') or []
        updated_cache = [s for s in cached if s[0] != sale_id]
        await state.update_data(edit_sales_cache=updated_cache)

        has_list = bool(updated_cache)
        kb_rows = []
        if has_list:
            kb_rows.append([InlineKeyboardButton(text="⬅️ К списку продаж", callback_data="back_to_sales_list")])
        kb_rows.append([InlineKeyboardButton(text="📋 Меню", callback_data="edit_sales")])

        await callback.message.edit_text(
            f"✅ <b>Продажа удалена!</b>\n\n"
            f"🏷 Товар: <b>{he(data['product_name'])}</b>\n"
            f"📦 Количество: {data['current_quantity']} шт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="HTML"
        )
        await state.set_state(EditSaleStates.choosing_sale)
    else:
        await callback.message.edit_text(
            "❌ Ошибка при удалении продажи!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_edit_sale")]
            ])
        )

@sales_router.callback_query(F.data.startswith("sale_audit_"))
async def sale_audit_log(callback: CallbackQuery, state: FSMContext):
    """История изменений продажи"""
    await callback.answer()
    sale_id = int(callback.data.replace("sale_audit_", ""))
    current_db = await get_db(callback.from_user.id, state)
    rows = await current_db.get_sale_audit_log(sale_id)
    if not rows:
        text = "📋 <b>История изменений</b>\n\nИзменений ещё не было."
    else:
        text = "📋 <b>История изменений продажи</b>\n\n"
        for row in rows:
            # (id, sale_id, field_name, old_value, new_value, changed_by, changed_at, changer_name)
            field = he(str(row[2]))
            old_v = he(str(row[3])) if row[3] is not None else "—"
            new_v = he(str(row[4])) if row[4] is not None else "—"
            changer = he(str(row[7])) if row[7] else "—"
            changed_at = str(row[6])[:16] if row[6] else "—"
            text += (
                f"🕐 <b>{changed_at}</b> · {changer}\n"
                f"   {field}: {old_v} → <b>{new_v}</b>\n\n"
            )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_edit_sale")]
        ]),
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data == "back_to_edit_sale")
async def back_to_edit_sale(callback: CallbackQuery, state: FSMContext):
    """Возврат к редактированию продажи"""
    await callback.answer()
    data = await state.get_data()
    sale_id = data.get('sale_id')
    if sale_id is None:
        # state сброшен (перезапуск бота) — возвращаем к списку или к выбору периода
        cached = data.get('edit_sales_cache')
        title = data.get('edit_sales_title', 'Продажи')
        page = data.get('edit_sales_page_num', 0)
        if cached:
            await show_sales_for_edit(callback, cached, state, title, page=page)
        else:
            await edit_sales_start(callback, state)
        return
    await render_edit_sale_menu(callback.message, state, sale_id, callback.from_user.id)

@sales_router.callback_query(F.data == "back_to_sales_list")
async def back_to_sales_list(callback: CallbackQuery, state: FSMContext):
    """Возврат к списку продаж из кеша FSM (без повторного запроса к БД)."""
    await callback.answer()
    data = await state.get_data()
    cached = data.get('edit_sales_cache')
    title  = data.get('edit_sales_title', 'Продажи')
    page   = data.get('edit_sales_page_num', 0)
    if cached is not None:
        await show_sales_for_edit(callback, cached, state, title, page=page)
    else:
        # Кеш утерян — возвращаем на выбор периода
        await edit_sales_start(callback, state)

@sales_router.callback_query(F.data == "edit_sale_date")
async def edit_sale_date_menu(callback: CallbackQuery, state: FSMContext):
    """Меню смены даты продажи — быстрые варианты + календарь."""
    await callback.answer()
    from datetime import timedelta
    data = await state.get_data()
    _cur_db_esd = await get_db(callback.from_user.id, state)
    _tz_esd = await _cur_db_esd.get_user_timezone(callback.from_user.id)
    today_dt  = _gcur_tz(_tz_esd).date()
    yesterday = today_dt - timedelta(days=1)
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"📅 <b>Изменить дату продажи</b>\n\n"
        f"🏷 Товар: <b>{he(data.get('product_name', ''))}</b>\n\n"
        f"Выберите новую дату:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📅 Сегодня ({today_dt.strftime('%d.%m')})",
                                  callback_data=f"esd_quick_{today_dt.isoformat()}")],
            [InlineKeyboardButton(text=f"📅 Вчера ({yesterday.strftime('%d.%m')})",
                                  callback_data=f"esd_quick_{yesterday.isoformat()}")],
            [InlineKeyboardButton(text="🗓 Выбрать другую дату", callback_data="esd_calendar")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_edit_sale")],
        ]),
        parse_mode="HTML"
    )
    await state.set_state(EditSaleStates.editing_date)

@sales_router.callback_query(F.data.startswith("esd_quick_"))
async def edit_sale_date_quick(callback: CallbackQuery, state: FSMContext):
    """Применить быструю дату к продаже."""
    new_date = callback.data.replace("esd_quick_", "")
    data = await state.get_data()
    sale_id = data.get('sale_id')
    if not sale_id:
        await callback.answer("❌ Продажа не найдена!", show_alert=True)
        return

    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    editor_id  = await current_db.get_user_id(callback.from_user.id)
    if await current_db.update_sale_date(sale_id, new_date, changed_by=editor_id):
        from datetime import datetime
        disp = format_date_display(new_date)
        await callback.message.edit_text(
            f"✅ <b>Дата изменена!</b>\n\n"
            f"🏷 Товар: <b>{he(data['product_name'])}</b>\n"
            f"📅 Новая дата: <b>{disp}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Ещё изменить", callback_data="back_to_edit_sale")],
                [InlineKeyboardButton(text="⬅️ К списку продаж", callback_data="back_to_sales_list")],
                [InlineKeyboardButton(text="📋 Меню", callback_data="edit_sales")],
            ]),
            parse_mode="HTML"
        )
        await state.set_state(EditSaleStates.choosing_sale)
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при изменении даты!</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_edit_sale")],
            ]),
            parse_mode="HTML",
        )

@sales_router.callback_query(F.data == "esd_calendar")
async def edit_sale_date_calendar(callback: CallbackQuery, state: FSMContext):
    """Открыть календарь для выбора даты продажи."""
    await callback.answer()
    from keyboards import generate_calendar
    calendar = generate_calendar(cancel_callback="edit_sale_date", prefix="esd_cal_")
    await callback.message.edit_text(
        "📅 Выберите новую дату продажи:",
        reply_markup=calendar
    )

@sales_router.callback_query(F.data.startswith("esd_cal_"))
async def edit_sale_date_calendar_handler(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора даты через календарь для смены даты продажи."""
    from keyboards import generate_calendar
    from datetime import datetime, date
    action = callback.data.replace("esd_cal_", "")
    data_state = await state.get_data()

    if action == "ignore":
        await callback.answer()
        return
    if action.startswith("nav_"):
        parts = action.replace("nav_", "").split("_")
        yr, mo = int(parts[0]), int(parts[1])
        await state.update_data(esd_cal_year=yr, esd_cal_month=mo)
        cal = generate_calendar(yr, mo, "edit_sale_date", "esd_cal_")
        await callback.message.edit_text("📅 Выберите новую дату продажи:", reply_markup=cal)
        await callback.answer()
        return
    if action in ("prev_month", "next_month"):
        yr = data_state.get('esd_cal_year', date.today().year)
        mo = data_state.get('esd_cal_month', date.today().month)
        if action == "prev_month":
            mo -= 1
            if mo == 0:
                mo = 12; yr -= 1
        else:
            mo += 1
            if mo == 13:
                mo = 1; yr += 1
        await state.update_data(esd_cal_year=yr, esd_cal_month=mo)
        cal = generate_calendar(yr, mo, "edit_sale_date", "esd_cal_")
        await callback.message.edit_text("📅 Выберите новую дату продажи:", reply_markup=cal)
        await callback.answer()
        return
    if action.startswith("date_"):
        date_str = action.replace("date_", "")
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            await callback.answer("❌ Неверный формат даты!", show_alert=True)
            return
        sale_id = data_state.get('sale_id')
        if not sale_id:
            await callback.answer("❌ Продажа не найдена!", show_alert=True)
            return
        current_db = await get_db(callback.from_user.id, state)
        editor_id  = await current_db.get_user_id(callback.from_user.id)
        if await current_db.update_sale_date(sale_id, date_str, changed_by=editor_id):
            disp = format_date_display(date_str)
            await callback.message.edit_text(
                f"✅ <b>Дата изменена!</b>\n\n"
                f"🏷 Товар: <b>{he(data_state['product_name'])}</b>\n"
                f"📅 Новая дата: <b>{disp}</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Ещё изменить", callback_data="back_to_edit_sale")],
                    [InlineKeyboardButton(text="⬅️ К списку продаж", callback_data="back_to_sales_list")],
                    [InlineKeyboardButton(text="📋 Меню", callback_data="edit_sales")],
                ]),
                parse_mode="HTML"
            )
            await state.set_state(EditSaleStates.choosing_sale)
        else:
            await callback.answer("❌ Ошибка при изменении даты!", show_alert=True)
        await callback.answer()

@sales_router.callback_query(F.data == "edit_sales_period")
async def edit_sales_period_start(callback: CallbackQuery, state: FSMContext):
    """Начало выбора периода для редактирования продаж"""
    await callback.answer()
    from datetime import timedelta
    from keyboards import generate_calendar
    _cur_db_esp = await get_db(callback.from_user.id, state)
    _tz_esp = await _cur_db_esp.get_user_timezone(callback.from_user.id)
    today = _gcur_tz(_tz_esp).date()
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
        # Для админа - выбор магазина (включая магазины без сотрудников)
        await state.update_data(edit_start_date=start_date, edit_end_date=end_date)
        
        shops = sorted(set(await current_db.get_all_shops() or []) | set(await current_db.get_inventory_shops() or []))
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

    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.message.edit_text(
            "❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales_period")]])
        )
        return
    
    # Получаем продажи за выбранный период
    sales = await current_db.get_user_sales_by_date(user_id, start_date, end_date)
    
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
                # Для админа - выбор магазина (включая магазины без сотрудников)
                await state.update_data(edit_start_date=start_date, edit_end_date=date_str)
                
                shops = sorted(set(await current_db.get_all_shops() or []) | set(await current_db.get_inventory_shops() or []))
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
            user_id = await current_db.get_user_id(callback.from_user.id)
            if not user_id:
                await callback.answer("❌ Пользователь не найден!", show_alert=True)
                return
            
            sales = await current_db.get_user_sales_by_date(user_id, start_date, date_str)
            
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


# ─── Массовое удаление продаж ─────────────────────────────────────────────────

@sales_router.callback_query(F.data == "sales_bulk_delete")
async def sales_bulk_delete_start(callback: CallbackQuery, state: FSMContext):
    """Хаб → выбор магазина для массового удаления продаж"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    shops = sorted(set(await current_db.get_all_shops() or []) | set(await current_db.get_inventory_shops() or []))

    if not shops:
        await callback.message.edit_text(
            "🏪 Магазины не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]])
        )
        return

    builder = InlineKeyboardBuilder()
    for shop in shops:
        builder.add(InlineKeyboardButton(text=shop, callback_data=safe_cb("sbd_shop_", shop)))
    builder.add(back_button("edit_sales"))
    builder.adjust(2, 1)

    await callback.message.edit_text(
        "🗑 <b>Удаление продаж за период</b>\n\n🏪 Выберите магазин:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data.startswith("sbd_shop_"))
async def sales_bulk_delete_shop(callback: CallbackQuery, state: FSMContext):
    """Магазин выбран — показываем выбор периода"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.answer()
    key = callback.data[len("sbd_shop_"):]
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops() or []
    shop_name = resolve_cb_name(key, shops)
    await state.update_data(sbd_shop_name=shop_name)
    await _sbd_show_period_menu(callback, shop_name, state)


async def _sbd_show_period_menu(callback: CallbackQuery, shop_name: str, state=None):
    """Показывает меню выбора периода для массового удаления"""
    from datetime import timedelta
    if state is not None:
        _cur_db_sbd = await get_db(callback.from_user.id, state)
        _tz_sbd = await _cur_db_sbd.get_user_timezone(callback.from_user.id)
        today = _gcur_tz(_tz_sbd).date()
    else:
        from datetime import date
        today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago  = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 За сегодня",         callback_data=f"sbd_period_{today}_{today}")],
        [InlineKeyboardButton(text="📅 Вчера",              callback_data=f"sbd_period_{yesterday}_{yesterday}")],
        [InlineKeyboardButton(text="📅 Последние 7 дней",   callback_data=f"sbd_period_{week_ago}_{today}")],
        [InlineKeyboardButton(text="📅 Последние 30 дней",  callback_data=f"sbd_period_{month_ago}_{today}")],
        [InlineKeyboardButton(text="🗓 Выбрать период",     callback_data="sbd_calendar")],
        [back_button("sales_bulk_delete")],
    ])
    await callback.message.edit_text(
        f"🗑 <b>Удаление продаж за период</b>\n\n🏪 Магазин: <b>{he(shop_name)}</b>\n\nВыберите период:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data.startswith("sbd_period_"))
async def sales_bulk_delete_period(callback: CallbackQuery, state: FSMContext):
    """Период выбран — сохраняем и показываем подтверждение"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.answer()
    raw = callback.data[len("sbd_period_"):]
    start_date, end_date = raw.split("_", 1)
    await state.update_data(sbd_start_date=start_date, sbd_end_date=end_date)
    await _sbd_show_confirm(callback, state)


async def _sbd_show_confirm(callback: CallbackQuery, state: FSMContext):
    """Экран подтверждения: показывает количество продаж и кнопки подтверждения"""
    data = await state.get_data()
    shop_name  = data.get("sbd_shop_name", "")
    start_date = data.get("sbd_start_date", "")
    end_date   = data.get("sbd_end_date", "")

    current_db = await get_db(callback.from_user.id, state)
    sales = await current_db.get_shop_sales_by_date(shop_name, start_date, end_date) or []
    count = len(sales)

    period_str = (
        format_date_display(start_date)
        if start_date == end_date
        else f"{format_date_display(start_date)} — {format_date_display(end_date)}"
    )

    if count == 0:
        await callback.message.edit_text(
            f"📝 В магазине <b>{he(shop_name)}</b> нет продаж\nза период: {period_str}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]]),
            parse_mode="HTML"
        )
        return

    await callback.message.edit_text(
        f"⚠️ <b>Подтверждение удаления</b>\n\n"
        f"🏪 Магазин: <b>{he(shop_name)}</b>\n"
        f"📅 Период: {period_str}\n"
        f"📝 Продаж к удалению: <b>{count}</b>\n\n"
        f"❗️ Остатки будут восстановлены. Действие необратимо!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Удалить {count} прод.", callback_data="sbd_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="edit_sales")],
        ]),
        parse_mode="HTML"
    )


@sales_router.callback_query(F.data == "sbd_calendar")
async def sales_bulk_delete_calendar(callback: CallbackQuery, state: FSMContext):
    """Начало выбора произвольного периода через календарь"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.answer()
    from keyboards import generate_calendar
    await state.update_data(sbd_selecting_start=True)
    calendar = generate_calendar(cancel_callback="sbd_calendar_cancel", prefix="sbd_cal_")
    await callback.message.edit_text("📅 Выберите начальную дату периода:", reply_markup=calendar)


@sales_router.callback_query(F.data == "sbd_calendar_cancel")
async def sales_bulk_delete_calendar_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена выбора даты через календарь — возврат к меню периодов"""
    await callback.answer()
    data = await state.get_data()
    shop_name = data.get("sbd_shop_name", "")
    await _sbd_show_period_menu(callback, shop_name, state)


@sales_router.callback_query(F.data.startswith("sbd_cal_"))
async def sales_bulk_delete_cal_handler(callback: CallbackQuery, state: FSMContext):
    """Обработка навигации и выбора дат в календаре для массового удаления"""
    from keyboards import generate_calendar
    from datetime import datetime, date

    action = callback.data[len("sbd_cal_"):]
    data = await state.get_data()

    if action == "ignore":
        await callback.answer()
        return

    if action.startswith("nav_"):
        nav_parts = action.replace("nav_", "").split("_")
        y, m = int(nav_parts[0]), int(nav_parts[1])
        await state.update_data(sbd_cal_year=y, sbd_cal_month=m)
        is_start = data.get("sbd_selecting_start", True)
        label = "начальную" if is_start else "конечную"
        cal = generate_calendar(y, m, "sbd_calendar_cancel", "sbd_cal_")
        await callback.message.edit_text(f"📅 Выберите {label} дату периода:", reply_markup=cal)
        await callback.answer()
        return

    if action.startswith("date_"):
        date_str = action.replace("date_", "")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            await callback.answer("❌ Неверный формат даты!", show_alert=True)
            return

        is_start = data.get("sbd_selecting_start", True)
        if is_start:
            await state.update_data(sbd_start_date=date_str, sbd_selecting_start=False)
            cal = generate_calendar(cancel_callback="sbd_calendar_cancel", prefix="sbd_cal_")
            await callback.message.edit_text(
                f"📅 <b>Выбор периода</b>\n✅ Начало: {format_date_display(date_str)}\n\nВыберите конечную дату:",
                reply_markup=cal,
                parse_mode="HTML"
            )
        else:
            start = data.get("sbd_start_date", "")
            if date_str < start:
                await callback.answer("❌ Конечная дата не может быть раньше начальной!", show_alert=True)
                return
            await state.update_data(sbd_end_date=date_str, sbd_selecting_start=True)
            await _sbd_show_confirm(callback, state)

        await callback.answer()
        return

    await callback.answer()


@sales_router.callback_query(F.data == "sbd_confirm")
async def sales_bulk_delete_confirm(callback: CallbackQuery, state: FSMContext):
    """Выполнение массового удаления продаж после подтверждения"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.answer()
    data = await state.get_data()
    shop_name  = data.get("sbd_shop_name", "")
    start_date = data.get("sbd_start_date", "")
    end_date   = data.get("sbd_end_date", "")

    current_db = await get_db(callback.from_user.id, state)
    deleted = await current_db.delete_sales_by_shop_period(shop_name, start_date, end_date)

    period_str = (
        format_date_display(start_date)
        if start_date == end_date
        else f"{format_date_display(start_date)} — {format_date_display(end_date)}"
    )

    if deleted < 0:
        text = "❌ Ошибка при удалении. Попробуйте ещё раз."
    elif deleted == 0:
        text = "📝 Продаж для удаления не найдено."
    else:
        text = (
            f"✅ <b>Удалено {deleted} продаж</b>\n\n"
            f"🏪 Магазин: {he(shop_name)}\n"
            f"📅 Период: {period_str}\n"
            f"📦 Остатки восстановлены."
        )

    await clear_state_keep_org(state)
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_sales")]]),
        parse_mode="HTML"
    )
