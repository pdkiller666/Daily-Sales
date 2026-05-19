"""
Обработчики для управления товарами
"""
import os
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Database
from keyboards import products_menu, back_button, create_selection_keyboard, create_confirm_keyboard
from states import ProductStates, ExcelImportStates
from utils import format_currency, he
from message_utils import safe_edit_message, safe_answer_callback, fsm_edit
from env_manager import env_manager

# Получаем ID администратора из переменных окружения
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)

BULK_IMPORT_MAX = 100  # максимум товаров за одну пакетную загрузку

# Создаем роутер для товаров
products_router = Router()

from db_utils import get_db, clear_state_keep_org, is_any_admin
from keyboards import safe_cb, resolve_cb_name
from hints import hint_suffix


def _parse_bulk_products(text: str):
    """
    Парсит текст вида «Название;Категория;Цена» (по одному товару на строку).
    Устойчив к неполным строкам: недостающие поля заменяются значениями по умолчанию.
    Возвращает (valid_list, notices, skipped_count).
      valid_list  — товары для добавления
      notices     — список информационных сообщений (подстановки + пропуски)
      skipped_count — сколько строк полностью пропущено (не добавляются)
    """
    valid = []
    notices = []       # ℹ️ подстановки и предупреждения
    skipped_count = 0
    seen = set()

    for i, raw_line in enumerate(text.strip().splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue

        if len(valid) >= BULK_IMPORT_MAX:
            notices.append(f"❌ Стр.{i}+: достигнут лимит {BULK_IMPORT_MAX} товаров — остальные пропущены")
            skipped_count += 1
            break

        parts = [p.strip() for p in line.split(';')]

        # Извлекаем поля с подстановками при нехватке
        name = parts[0] if len(parts) >= 1 else ''
        category_raw = parts[1] if len(parts) >= 2 else ''
        price_str = parts[2] if len(parts) >= 3 else ''

        # ── Название (обязательное поле) ──────────────────────────
        if not name or len(name) < 2:
            notices.append(f"❌ Стр.{i}: пустое или слишком короткое название — строка пропущена")
            skipped_count += 1
            continue
        if len(name) > 30:
            name = name[:30]
            notices.append(f"⚠️ Стр.{i}: название обрезано до 30 символов → «{name}»")

        # ── Дубликат ──────────────────────────────────────────────
        key = name.lower()
        if key in seen:
            notices.append(f"❌ Стр.{i}: «{name}» — дублируется в списке — строка пропущена")
            skipped_count += 1
            continue

        # ── Категория (необязательная, подставляем дефолт) ────────
        if not category_raw or len(category_raw) < 2:
            category = 'Без категории'
            notices.append(f"⚠️ Стр.{i}: категория не указана → подставлено «Без категории»")
        elif len(category_raw) > 30:
            category = category_raw[:30]
            notices.append(f"⚠️ Стр.{i}: категория обрезана до 30 символов → «{category}»")
        else:
            category = category_raw

        # ── Цена (необязательная, подставляем 0) ──────────────────
        price = 0.0
        if price_str:
            try:
                parsed = float(price_str.replace(',', '.').replace(' ', ''))
                if parsed < 0:
                    notices.append(f"⚠️ Стр.{i}: отрицательная цена «{price_str}» → подставлено 0")
                else:
                    price = parsed
            except ValueError:
                notices.append(f"⚠️ Стр.{i}: «{price_str}» не является числом → цена = 0")
        else:
            notices.append(f"⚠️ Стр.{i}: цена не указана → подставлено 0")

        seen.add(key)
        valid.append({'name': name, 'category': category, 'price': price})

    return valid, notices, skipped_count

@products_router.callback_query(F.data == "products")
async def products_callback(callback: CallbackQuery, state: FSMContext):
    """Главное меню товаров"""
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return
        
    current_db = await get_db(callback.from_user.id, state)
    await current_db.create_tables()
    
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)
    
    # Доступ разрешен супер-админам и администраторам (включая личный режим)
    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await callback.answer()
    _prod_user = await current_db.get_user(callback.from_user.id)
    _prod_hint = hint_suffix(current_db, _prod_user[0], 'first_products') if _prod_user else ""
    await callback.message.edit_text(
        f"🛍 <b>Управление товарами</b>\n\nВыберите действие:{_prod_hint}",
        reply_markup=products_menu(),
        parse_mode="HTML"
    )

@products_router.callback_query(F.data == "add_product")
async def add_product_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления товара"""
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)

    # Проверка прав доступа
    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    
    # Проверяем лимиты подписки (только для обычных администраторов)
    if not is_super:
        from subscription_utils import check_product_limit
        ok, msg = await asyncio.to_thread(check_product_limit, callback.from_user.id)
        if not ok:
            await callback.message.edit_text(
                f"🚫 <b>Достигнут лимит товаров</b>\n\n{msg}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Подписка", callback_data="subscription_menu")],
                    [back_button("products")]
                ]),
                parse_mode="HTML"
            )
            return
    
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "➕ Добавление нового товара\n\n1️⃣ Введите название товара:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
    )
    await state.set_state(ProductStates.waiting_for_name)

@products_router.message(ProductStates.waiting_for_name)
async def process_product_name(message: Message, state: FSMContext):
    """Обработка названия товара"""
    is_super = env_manager.is_super_admin(message.from_user.id)
    is_admin = is_any_admin(message.from_user.id)
    
    if not is_super and not is_admin:
        return
    
    if not message.text:
        await fsm_edit(state, message, "1️⃣ Введите название товара:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
        
    name = message.text.strip()
    if len(name) < 2:
        await fsm_edit(state, message, "1️⃣ ❌ Название должно содержать минимум 2 символа. Введите снова:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
    if len(name) > 30:
        await fsm_edit(state, message, "1️⃣ ⚠️ Название товара не должно превышать 30 символов. Введите короче:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
    
    await state.update_data(name=name)
    
    current_db = await get_db(message.from_user.id, state)
    categories = await current_db.get_all_categories()
    
    if categories:
        builder = InlineKeyboardBuilder()
        for category in categories:
            builder.add(InlineKeyboardButton(text=category, callback_data=safe_cb("select_category_", category)))
        builder.add(InlineKeyboardButton(text="➕ Новая категория", callback_data="new_category"))
        builder.add(back_button("products"))
        builder.adjust(2, 1, 1)
        await fsm_edit(state, message,
                       f"✅ Название: {name}\n\n2️⃣ Выберите категорию или создайте новую:",
                       reply_markup=builder.as_markup())
    else:
        await fsm_edit(state, message,
                       f"✅ Название: {name}\n\n2️⃣ Введите категорию товара:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        await state.set_state(ProductStates.waiting_for_category)

@products_router.callback_query(F.data.startswith("select_category_"))
async def select_category(callback: CallbackQuery, state: FSMContext):
    """Выбор существующей категории"""
    await callback.answer()
    category_raw = callback.data.replace("select_category_", "")
    current_db_temp = await get_db(callback.from_user.id, state)
    _tmp_cats = await current_db_temp.get_all_categories()
    category = resolve_cb_name(category_raw, _tmp_cats or [])
    await state.update_data(category=category)
    
    data = await state.get_data()
    
    # Проверяем, что название товара есть в состоянии
    if 'name' not in data:
        await callback.message.edit_text(
            "❌ Ошибка: данные о товаре потеряны. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
        )
        await clear_state_keep_org(state)
        return
    
    await callback.message.edit_text(
        f"✅ Название: {data['name']}\n"
        f"✅ Категория: {category}\n\n"
        f"3️⃣ Введите цену товара:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
    )
    await state.set_state(ProductStates.waiting_for_price)

@products_router.callback_query(F.data == "new_category")
async def new_category(callback: CallbackQuery, state: FSMContext):
    """Создание новой категории"""
    await callback.answer()
    data = await state.get_data()
    
    # Проверяем, что название товара есть в состоянии
    if 'name' not in data:
        await callback.message.edit_text(
            "❌ Ошибка: данные о товаре потеряны. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
        )
        await clear_state_keep_org(state)
        return
    
    await callback.message.edit_text(
        f"✅ Название: {data['name']}\n\n2️⃣ Введите новую категорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
    )
    await state.set_state(ProductStates.waiting_for_category)

@products_router.message(ProductStates.waiting_for_category)
async def process_product_category(message: Message, state: FSMContext):
    """Обработка категории товара"""
    is_super = env_manager.is_super_admin(message.from_user.id)
    is_admin = is_any_admin(message.from_user.id)
    
    if not is_super and not is_admin:
        return
        
    if not message.text:
        await fsm_edit(state, message, "2️⃣ Введите название категории:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
        
    category = message.text.strip()
    if len(category) < 2:
        await fsm_edit(state, message, "2️⃣ ❌ Категория должна содержать минимум 2 символа. Введите снова:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
    if len(category) > 30:
        await fsm_edit(state, message, "2️⃣ ⚠️ Название категории не должно превышать 30 символов. Введите короче:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
    
    await state.update_data(category=category)
    data = await state.get_data()
    
    await fsm_edit(state, message,
                   f"✅ Название: {data['name']}\n"
                   f"✅ Категория: {category}\n\n"
                   f"3️⃣ Введите цену товара:",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
    await state.set_state(ProductStates.waiting_for_price)

@products_router.message(ProductStates.waiting_for_price)
async def process_product_price(message: Message, state: FSMContext):
    """Обработка цены товара"""
    is_super = env_manager.is_super_admin(message.from_user.id)
    is_admin = is_any_admin(message.from_user.id)  # computed once, reused below

    if not is_super and not is_admin:
        return
        
    if not message.text:
        await fsm_edit(state, message, "3️⃣ Введите цену товара:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
        
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0:
            await fsm_edit(state, message, "3️⃣ ❌ Цена должна быть больше нуля. Введите снова:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
            return
    except ValueError:
        await fsm_edit(state, message, "3️⃣ ❌ Введите корректную цену (число). Попробуйте снова:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return
    
    data = await state.get_data()
    
    if 'name' not in data or 'category' not in data:
        await fsm_edit(state, message, "❌ Ошибка: данные о товаре потеряны. Попробуйте снова.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        await clear_state_keep_org(state)
        return
    
    current_db = await get_db(message.from_user.id, state)
    product_id = await current_db.add_product(data['name'], data['category'], price)
    
    if product_id:
        # Сохраняем ID нового товара для следующих шагов
        await state.update_data(new_product_id=product_id, new_product_name=data['name'])
        
        # Формируем кнопки в зависимости от роли
        buttons = []
        
        # Админы могут настраивать мотивацию
        if is_admin:
            buttons.append([InlineKeyboardButton(
                text="🎯 Настроить мотивацию", 
                callback_data=f"setup_motivation_new_{product_id}"
            )])
        
        # Все могут добавить остатки
        buttons.append([InlineKeyboardButton(
            text="📦 Установить остатки", 
            callback_data=f"setup_inventory_new_{product_id}"
        )])
        
        buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="products")])

        _gs_sfx = ""
        try:
            from integration.manager import integration_manager as _int_mgr
            _gs_sfx = await _int_mgr.try_export_line(current_db, 'products', {
                'name': data['name'], 'category': data['category'],
                'price': str(price), 'description': '',
            })
        except Exception:
            pass

        await fsm_edit(
            state, message,
            f"✅ Товар успешно добавлен!\n\n"
            f"🏷 Название: {data['name']}\n"
            f"📂 Категория: {data['category']}\n"
            f"💰 Цена: {format_currency(price)}\n\n"
            f"💡 <i>Хотите сразу настроить дополнительные параметры?</i>{_gs_sfx}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    else:
        await fsm_edit(
            state, message,
            f"❌ Ошибка! Товар с названием '{data['name']}' уже существует.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
        )
        await clear_state_keep_org(state)

_PRODL_CATS_PER_PAGE  = 8   # категорий на странице (уровень 1)
_PRODL_PRODS_PER_PAGE = 10  # товаров на странице  (уровень 2)


async def _render_category_list(callback: CallbackQuery, state: FSMContext, page: int = 0):
    """Уровень 1: категории кнопками с количеством товаров."""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()

    if not products:
        await callback.message.edit_text(
            "📋 Список товаров пуст.\n\nДобавьте товары для начала работы.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
        )
        return

    from pagination_utils import paginate as _paginate, page_nav_row as _nav_row

    cat_counts: dict[str, int] = {}
    for p in products:
        cat = p[2] or "Без категории"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    cat_list = sorted(cat_counts.items())
    page_cats, has_prev, has_next, total_pages, page = _paginate(cat_list, page, _PRODL_CATS_PER_PAGE)

    total_prods = len(products)
    total_cats  = len(cat_list)
    pg_info = f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    header = (
        f"📋 <b>Список товаров</b>{pg_info}\n"
        f"Итого: <b>{total_prods} шт.</b> в <b>{total_cats} кат.</b>\n\n"
        f"Выберите категорию:"
    )

    builder = InlineKeyboardBuilder()
    for cat_name, count in page_cats:
        builder.button(
            text=f"📂 {cat_name} ({count})",
            callback_data=safe_cb("prodl_cat_", cat_name)
        )
    builder.adjust(2)

    nav = _nav_row("prodl_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔍 Поиск по названию", callback_data="prodl_srch_start"))
    builder.row(back_button("products"))

    await state.update_data(prodl_cat_page=page)
    await callback.message.edit_text(header, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_products_in_category(callback: CallbackQuery, state: FSMContext,
                                        category: str, page: int = 0, sort: str = 'name'):
    """Уровень 2: товары выбранной категории с пагинацией и сортировкой."""
    current_db = await get_db(callback.from_user.id, state)
    products = list(await current_db.get_products_by_category(category))

    from pagination_utils import paginate as _paginate, page_nav_row as _nav_row

    if sort == 'price_asc':
        products.sort(key=lambda p: p[3] or 0)
    elif sort == 'price_desc':
        products.sort(key=lambda p: p[3] or 0, reverse=True)
    else:
        products.sort(key=lambda p: (p[1] or '').lower())

    total = len(products)
    page_prods, has_prev, has_next, total_pages, page = _paginate(products, page, _PRODL_PRODS_PER_PAGE)

    sort_label = {'name': '🔤 A→Я', 'price_asc': '💰 ↑', 'price_desc': '💰 ↓'}.get(sort, '🔤 A→Я')
    pg_info = f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    text = (
        f"📂 <b>{he(category)}</b> · {total} тов. · {sort_label}{pg_info}\n\n"
    )
    for p in page_prods:
        text += f"• {he(p[1])} — <b>{format_currency(p[3])}</b>\n"

    builder = InlineKeyboardBuilder()
    sort_opts = [('name', '🔤 A→Я'), ('price_asc', '💰 Цена ↑'), ('price_desc', '💰 Цена ↓')]
    sort_row = [
        InlineKeyboardButton(
            text=f"✅ {lbl}" if sort == key else lbl,
            callback_data=f"prodl_sort_{key}"
        )
        for key, lbl in sort_opts
    ]
    builder.row(*sort_row)

    nav = _nav_row("prodl_cp_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="◀ К категориям", callback_data="prodl_back_cats"))

    await state.update_data(prodl_current_cat=category, prodl_sort=sort)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@products_router.callback_query(F.data == "list_products")
async def list_products(callback: CallbackQuery, state: FSMContext):
    """Список товаров — уровень 1: категории."""
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return
    await _render_category_list(callback, state, page=0)


@products_router.callback_query(F.data.startswith("prodl_pg_"))
async def product_list_page(callback: CallbackQuery, state: FSMContext):
    """Навигация по страницам списка категорий."""
    await callback.answer()
    try:
        page = int(callback.data.replace("prodl_pg_", ""))
    except ValueError:
        page = 0
    await _render_category_list(callback, state, page=page)


@products_router.callback_query(F.data.startswith("prodl_cat_"))
async def product_open_category(callback: CallbackQuery, state: FSMContext):
    """Открыть категорию — уровень 2: товары."""
    await callback.answer()
    raw = callback.data[len("prodl_cat_"):]
    current_db = await get_db(callback.from_user.id, state)
    all_cats = await current_db.get_all_categories() or []
    category = resolve_cb_name(raw, all_cats)
    if not category:
        await callback.answer("❌ Категория не найдена.", show_alert=True)
        return
    await _render_products_in_category(callback, state, category, page=0, sort='name')


@products_router.callback_query(F.data.startswith("prodl_cp_"))
async def product_category_page(callback: CallbackQuery, state: FSMContext):
    """Навигация по страницам внутри категории."""
    await callback.answer()
    try:
        page = int(callback.data.replace("prodl_cp_", ""))
    except ValueError:
        page = 0
    data = await state.get_data()
    category = data.get("prodl_current_cat", "")
    sort = data.get("prodl_sort", "name")
    if not category:
        await _render_category_list(callback, state, page=0)
        return
    await _render_products_in_category(callback, state, category, page=page, sort=sort)


@products_router.callback_query(F.data.startswith("prodl_sort_"))
async def product_sort_toggle(callback: CallbackQuery, state: FSMContext):
    """Смена сортировки внутри категории."""
    await callback.answer()
    sort = callback.data.replace("prodl_sort_", "")
    if sort not in ('name', 'price_asc', 'price_desc'):
        sort = 'name'
    data = await state.get_data()
    category = data.get("prodl_current_cat", "")
    if not category:
        await _render_category_list(callback, state, page=0)
        return
    await _render_products_in_category(callback, state, category, page=0, sort=sort)


@products_router.callback_query(F.data == "prodl_back_cats")
async def product_back_to_cats(callback: CallbackQuery, state: FSMContext):
    """Вернуться к списку категорий."""
    await callback.answer()
    data = await state.get_data()
    page = data.get("prodl_cat_page", 0)
    await _render_category_list(callback, state, page=page)


@products_router.callback_query(F.data == "prodl_srch_start")
async def product_search_start(callback: CallbackQuery, state: FSMContext):
    """Начать поиск товара по названию."""
    await callback.answer()
    await state.set_state(ProductStates.searching_product)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖ Отмена", callback_data="prodl_srch_cancel"))
    await callback.message.edit_text(
        "🔍 <b>Поиск по названию</b>\n\nВведите часть названия товара:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@products_router.callback_query(F.data == "prodl_srch_cancel")
async def product_search_cancel(callback: CallbackQuery, state: FSMContext):
    """Отменить поиск, вернуться к категориям."""
    await state.set_state(None)
    data = await state.get_data()
    page = data.get("prodl_cat_page", 0)
    await _render_category_list(callback, state, page=page)


@products_router.message(ProductStates.searching_product)
async def product_search_input(message: Message, state: FSMContext):
    """Обработка текста поиска — ищет по всем товарам."""
    from db_utils import get_db as _get_db
    query = (message.text or "").strip().lower()
    if not query:
        await message.answer("⚠️ Введите хотя бы один символ.")
        return

    current_db = await _get_db(message.from_user.id, state)
    products = await current_db.get_all_products()

    matches = [p for p in products if query in (p[1] or '').lower()]

    await state.set_state(None)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀ К категориям", callback_data="prodl_srch_cancel"))

    if not matches:
        await message.answer(
            f"🔍 По запросу «<b>{he(query)}</b>» ничего не найдено.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    cat_groups: dict[str, list] = {}
    for p in matches:
        cat = p[2] or "Без категории"
        cat_groups.setdefault(cat, []).append(p)

    text = f"🔍 По запросу «<b>{he(query)}</b>» найдено <b>{len(matches)} тов.</b>:\n\n"
    for cat_name in sorted(cat_groups):
        text += f"📂 <b>{he(cat_name)}</b>\n"
        for p in cat_groups[cat_name]:
            text += f"  • {he(p[1])} — <b>{format_currency(p[3])}</b>\n"
        text += "\n"

    if len(text) > 3800:
        text = text[:3800] + f"\n\n<i>...показаны первые результаты</i>"

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@products_router.callback_query(F.data == "categories_menu")
async def categories_menu(callback: CallbackQuery, state: FSMContext):
    """Меню управления категориями"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
    
    if not categories:
        await callback.message.edit_text(
            "📂 Категории товаров отсутствуют.\n\nДобавьте товары для создания категорий.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.add(InlineKeyboardButton(text=f"✏️ {category}", callback_data=safe_cb("edit_category_", category)))
    builder.add(back_button("products"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        "📂 Управление категориями:\n\nВыберите категорию для редактирования:",
        reply_markup=builder.as_markup()
    )

@products_router.callback_query(F.data.startswith("edit_category_"))
async def edit_category(callback: CallbackQuery, state: FSMContext):
    """Редактирование категории"""
    await callback.answer()
    category_raw = callback.data.replace("edit_category_", "")
    current_db = await get_db(callback.from_user.id, state)
    category = resolve_cb_name(category_raw, await current_db.get_all_categories() or [])

    # Получаем товары в этой категории
    products = await current_db.get_all_products()
    category_products = [p for p in products if p[2] == category]
    
    message_text = f"📂 Категория: {category}\n\n"
    message_text += f"Товаров в категории: {len(category_products)}\n\n"
    
    if category_products:
        message_text += "Товары:\n"
        for product in category_products[:5]:  # Показываем первые 5
            message_text += f"• {product[1]} - {format_currency(product[3])}\n"
        
        if len(category_products) > 5:
            message_text += f"... и еще {len(category_products) - 5} товаров\n"
    
    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Переименовать", callback_data=safe_cb("rename_category_", category))],
            [InlineKeyboardButton(text="🗑 Удалить категорию", callback_data=safe_cb("delete_category_", category))],
            [back_button("categories_menu")]
        ])
    )

@products_router.callback_query(F.data.startswith("rename_category_"))
async def rename_category_start(callback: CallbackQuery, state: FSMContext):
    """Начало переименования категории"""
    await callback.answer()
    category_raw = callback.data.replace("rename_category_", "")
    current_db_temp = await get_db(callback.from_user.id, state)
    _tmp_cats = await current_db_temp.get_all_categories()
    category = resolve_cb_name(category_raw, _tmp_cats or [])
    await state.update_data(old_category=category)
    
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"✏️ Переименование категории '{category}'\n\nВведите новое название:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("categories_menu")]])
    )
    await state.set_state(ProductStates.waiting_for_category_rename)

@products_router.message(ProductStates.waiting_for_category_rename)
async def process_category_rename(message: Message, state: FSMContext):
    """Обработка переименования категории"""
    is_super = env_manager.is_super_admin(message.from_user.id)
    is_admin = is_any_admin(message.from_user.id)
    
    if not is_super and not is_admin:
        return
        
    _cat_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("categories_menu")]])
    if not message.text:
        await fsm_edit(state, message, "✏️ Введите новое название категории:", reply_markup=_cat_kb)
        return
        
    new_category = message.text.strip()
    if len(new_category) < 2:
        await fsm_edit(state, message, "✏️ ❌ Название категории должно содержать минимум 2 символа:", reply_markup=_cat_kb)
        return
    
    data = await state.get_data()
    if 'old_category' not in data:
        await fsm_edit(state, message, "❌ Ошибка: данные категории не найдены. Попробуйте снова.", reply_markup=_cat_kb)
        await clear_state_keep_org(state)
        return
        
    old_category = data['old_category']
    
    current_db = await get_db(message.from_user.id, state)
    products = await current_db.get_all_products()
    updated_count = 0
    
    for product in products:
        if product[2] == old_category:
            await current_db.update_product(product[0], category=new_category)
            updated_count += 1
    
    await fsm_edit(state, message,
                   f"✅ Категория переименована!\n\n"
                   f"'{old_category}' → '{new_category}'\n"
                   f"Обновлено товаров: {updated_count}",
                   reply_markup=_cat_kb)
    
    await clear_state_keep_org(state)

@products_router.callback_query(F.data.startswith("delete_category_"))
async def delete_category_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления категории"""
    await callback.answer()
    category_raw = callback.data.replace("delete_category_", "")
    current_db = await get_db(callback.from_user.id, state)
    category = resolve_cb_name(category_raw, await current_db.get_all_categories() or [])

    # Получаем товары в этой категории
    products = await current_db.get_all_products()
    category_products = [p for p in products if p[2] == category]
    
    await state.update_data(category_to_delete=category)
    
    message_text = f"⚠️ Удаление категории '{category}'\n\n"
    
    if category_products:
        message_text += f"В категории {len(category_products)} товаров:\n"
        for product in category_products[:3]:
            message_text += f"• {product[1]} - {format_currency(product[3])}\n"
        if len(category_products) > 3:
            message_text += f"... и еще {len(category_products) - 3} товаров\n"
        
        message_text += "\n🔄 При удалении категории все товары будут перемещены в категорию 'Без категории'.\n\n"
    else:
        message_text += "Категория пустая и может быть удалена без последствий.\n\n"
    
    message_text += "❗ Это действие нельзя отменить. Продолжить?"
    
    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete_category")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=safe_cb("edit_category_", category))]
        ])
    )

@products_router.callback_query(F.data == "confirm_delete_category")
async def delete_category_final(callback: CallbackQuery, state: FSMContext):
    """Окончательное удаление категории"""
    await callback.answer()
    data = await state.get_data()
    
    if 'category_to_delete' not in data:
        await callback.message.edit_text(
            "❌ Ошибка: данные категории не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("categories_menu")]])
        )
        await clear_state_keep_org(state)
        return
    
    category = data['category_to_delete']
    
    # Получаем товары в этой категории
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    category_products = [p for p in products if p[2] == category]
    
    # Перемещаем все товары в категорию "Без категории"
    updated_count = 0
    for product in category_products:
        await current_db.update_product(product[0], category="Без категории")
        updated_count += 1
    
    await callback.message.edit_text(
        f"✅ Категория '{category}' удалена!\n\n"
        f"Товаров перемещено в 'Без категории': {updated_count}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("categories_menu")]])
    )
    
    await clear_state_keep_org(state)

@products_router.callback_query(F.data == "edit_product")
async def edit_product_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования товара"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    
    if not products:
        await callback.message.edit_text(
            "📋 Товары для редактирования отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.add(InlineKeyboardButton(
            text=f"{product[1]} ({product[2]}) - {format_currency(product[3])}",
            callback_data=f"edit_product_choice_{product[0]}"
        ))
    builder.add(back_button("products"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        "✏️ Выберите товар для редактирования:",
        reply_markup=builder.as_markup()
    )

@products_router.callback_query(F.data.startswith("edit_product_choice_"))
async def edit_product_choice(callback: CallbackQuery, state: FSMContext):
    """Выбор товара для редактирования"""
    product_id = int(callback.data.replace("edit_product_choice_", ""))
    current_db = await get_db(callback.from_user.id, state)
    product = await current_db.get_product(product_id)
    
    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return
    
    await callback.answer()
    await state.update_data(edit_product_id=product_id)
    
    await callback.message.edit_text(
        f"✏️ Редактирование товара:\n\n"
        f"🏷 Название: {product[1]}\n"
        f"📂 Категория: {product[2]}\n"
        f"💰 Цена: {format_currency(product[3])}\n\n"
        f"Что хотите изменить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏷 Название", callback_data="edit_param_name"),
             InlineKeyboardButton(text="📂 Категория", callback_data="edit_param_category")],
            [InlineKeyboardButton(text="💰 Цена", callback_data="edit_param_price")],
            [back_button("edit_product")]
        ])
    )

@products_router.callback_query(F.data.startswith("edit_param_"))
async def edit_parameter_choice(callback: CallbackQuery, state: FSMContext):
    """Выбор параметра для редактирования"""
    await callback.answer()
    param = callback.data.replace("edit_param_", "")
    await state.update_data(edit_param=param)
    
    param_names = {
        "name": "название",
        "category": "категорию", 
        "price": "цену"
    }
    
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"✏️ Введите новое значение для '{param_names[param]}':",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_product")]])
    )
    await state.set_state(ProductStates.waiting_for_edit_value)

@products_router.message(ProductStates.waiting_for_edit_value)
async def process_edit_value_product(message: Message, state: FSMContext):
    """Обработка нового значения параметра"""
    if not is_any_admin(message.from_user.id) and not env_manager.is_super_admin(message.from_user.id):
        return
    
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("edit_product")]])
    if not message.text:
        await fsm_edit(state, message, "✏️ Введите новое значение:", reply_markup=_edit_kb)
        return
    
    data = await state.get_data()
    
    if 'edit_product_id' not in data or 'edit_param' not in data:
        await fsm_edit(state, message, "❌ Ошибка: данные для редактирования не найдены. Попробуйте снова.", reply_markup=_edit_kb)
        await clear_state_keep_org(state)
        return
        
    product_id = data['edit_product_id']
    param = data['edit_param']
    new_value = message.text.strip()
    
    if param == "price":
        try:
            new_value = float(new_value.replace(',', '.'))
            if new_value <= 0:
                await fsm_edit(state, message, "✏️ ❌ Цена должна быть больше нуля:", reply_markup=_edit_kb)
                return
        except ValueError:
            await fsm_edit(state, message, "✏️ ❌ Введите корректную цену (число):", reply_markup=_edit_kb)
            return
    elif len(new_value) < 2:
        await fsm_edit(state, message, "✏️ ❌ Значение должно содержать минимум 2 символа:", reply_markup=_edit_kb)
        return
    
    current_db = await get_db(message.from_user.id, state)
    try:
        if param == "name":
            await current_db.update_product(product_id, name=new_value)
        elif param == "category":
            await current_db.update_product(product_id, category=new_value)
        elif param == "price":
            await current_db.update_product(product_id, price=new_value)

        product = await current_db.get_product(product_id)
        if not product:
            await fsm_edit(state, message, "❌ Товар не найден после обновления.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
            await clear_state_keep_org(state)
            return
    except Exception as e:
        import logging
        logging.error(f"process_edit_value_product: DB error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при обновлении товара. Попробуйте снова.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        await clear_state_keep_org(state)
        return

    _gs_sfx = ""
    try:
        from integration.manager import integration_manager as _int_mgr
        _gs_sfx = await _int_mgr.try_export_line(current_db, 'products', {
            'name': product[1], 'category': product[2],
            'price': str(product[3]), 'description': product[4] if len(product) > 4 else '',
        })
    except Exception:
        pass

    await fsm_edit(state, message,
                   f"✅ Товар обновлен!\n\n"
                   f"🏷 Название: {product[1]}\n"
                   f"📂 Категория: {product[2]}\n"
                   f"💰 Цена: {format_currency(product[3])}{_gs_sfx}",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))

    await clear_state_keep_org(state)

@products_router.callback_query(F.data == "delete_product")
async def delete_product_start(callback: CallbackQuery, state: FSMContext):
    """Начало удаления товара"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_all_products()
    
    if not products:
        await callback.message.edit_text(
            "📋 Товары для удаления отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.add(InlineKeyboardButton(
            text=f"{product[1]} ({product[2]}) - {format_currency(product[3])}",
            callback_data=f"confirm_delete_{product[0]}"
        ))
    builder.add(back_button("products"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        "🗑 Выберите товар для удаления:",
        reply_markup=builder.as_markup()
    )

@products_router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_product(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления товара"""
    product_id = int(callback.data.replace("confirm_delete_", ""))
    current_db = await get_db(callback.from_user.id, state)
    product = await current_db.get_product(product_id)
    
    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return
    
    await callback.answer()
    await state.update_data(delete_product_id=product_id)
    
    await callback.message.edit_text(
        f"⚠️ Подтверждение удаления\n\n"
        f"Товар: {product[1]}\n"
        f"Категория: {product[2]}\n"
        f"Цена: {format_currency(product[3])}\n\n"
        f"❗ Вы действительно хотите удалить этот товар?\n"
        f"Это действие необратимо!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data="final_delete_product"),
             InlineKeyboardButton(text="❌ Отмена", callback_data="delete_product")]
        ])
    )

@products_router.callback_query(F.data == "final_delete_product")
async def final_delete_product(callback: CallbackQuery, state: FSMContext):
    """Окончательное удаление товара"""
    await callback.answer()
    data = await state.get_data()
    product_id = data['delete_product_id']
    
    current_db = await get_db(callback.from_user.id, state)
    product = await current_db.get_product(product_id)
    product_name = product[1] if product else "Неизвестный товар"
    
    # Удаляем товар
    await current_db.delete_product(product_id)

    _gs_sfx = ""
    try:
        from integration.manager import integration_manager as _int_mgr
        _gs_sfx = await _int_mgr.try_export_line(current_db, 'products', {
            'name': product_name, 'category': product[2] if product else '',
            'price': str(product[3]) if product else '', 'description': '',
        })
    except Exception:
        pass

    await callback.message.edit_text(
        f"✅ Товар '{product_name}' успешно удален!{_gs_sfx}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]])
    )


# === Обработчики для быстрой настройки нового товара ===

class NewProductSetupStates(StatesGroup):
    waiting_motivation_type = State()
    waiting_motivation_value = State()
    waiting_inventory_shop = State()
    waiting_inventory_quantity = State()


@products_router.callback_query(F.data.startswith("setup_motivation_new_"))
async def setup_motivation_new(callback: CallbackQuery, state: FSMContext):
    """Настройка мотивации для нового товара"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    
    product_id = int(callback.data.split("_")[-1])
    current_db = await get_db(callback.from_user.id, state)
    product = await current_db.get_product(product_id)
    
    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return
    
    await state.update_data(motivation_product_id=product_id, motivation_product_name=product[1])
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 Фиксированная сумма", callback_data="new_motivation_type_fixed")
    builder.button(text="📊 Процент от продажи", callback_data="new_motivation_type_percent")
    builder.button(text="❌ Отмена", callback_data="products")
    builder.adjust(1)
    
    await callback.message.edit_text(
        f"🎯 <b>Настройка мотивации</b>\n\n"
        f"Товар: <b>{he(product[1])}</b>\n\n"
        f"Выберите тип мотивации:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@products_router.callback_query(F.data.startswith("new_motivation_type_"))
async def new_motivation_type_selected(callback: CallbackQuery, state: FSMContext):
    """Выбор типа мотивации для нового товара"""
    motivation_type = callback.data.replace("new_motivation_type_", "")
    data = await state.get_data()
    product_name = data.get('motivation_product_name', 'Товар')
    
    await state.update_data(new_motivation_type=motivation_type, anchor_msg_id=callback.message.message_id)
    await state.set_state(NewProductSetupStates.waiting_motivation_value)
    
    type_text = "фиксированную сумму (₽)" if motivation_type == "fixed" else "процент от продажи (%)"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="products")
    
    await callback.message.edit_text(
        f"🎯 <b>Настройка мотивации</b>\n\n"
        f"Товар: <b>{he(product_name)}</b>\n"
        f"Тип: <b>{type_text}</b>\n\n"
        f"Введите значение мотивации:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@products_router.message(NewProductSetupStates.waiting_motivation_value)
async def process_new_motivation_value(message: Message, state: FSMContext):
    """Обработка значения мотивации"""
    _mot_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="products")]])
    try:
        value = float(message.text.replace(",", "."))
        if value <= 0:
            raise ValueError("Значение должно быть положительным")
    except ValueError:
        await fsm_edit(state, message, "🎯 ❌ Введите корректное положительное число:", reply_markup=_mot_kb)
        return
    
    data = await state.get_data()
    product_id = data.get('motivation_product_id')
    product_name = data.get('motivation_product_name')
    motivation_type = data.get('new_motivation_type')
    
    current_db = await get_db(message.from_user.id, state)
    await current_db.set_product_motivation(product_id, motivation_type, value, message.from_user.id)
    
    type_text = "₽" if motivation_type == "fixed" else "%"
    
    buttons = [
        [InlineKeyboardButton(text="📦 Установить остатки", callback_data=f"setup_inventory_new_{product_id}")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="products")]
    ]
    
    await fsm_edit(state, message,
                   f"✅ Мотивация установлена!\n\n"
                   f"Товар: <b>{he(product_name)}</b>\n"
                   f"Мотивация: <b>{value}{type_text}</b>\n\n"
                   f"💡 <i>Хотите установить остатки для этого товара?</i>",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await clear_state_keep_org(state)


@products_router.callback_query(F.data.startswith("setup_inventory_new_"))
async def setup_inventory_new(callback: CallbackQuery, state: FSMContext):
    """Установка остатков для нового товара"""
    from tenant_manager import tenant_manager
    
    product_id = int(callback.data.split("_")[-1])
    current_db = await get_db(callback.from_user.id, state)
    product = await current_db.get_product(product_id)
    
    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return
    
    # Определяем режим
    data = await state.get_data()
    selected_org_db = data.get("selected_org_db")
    
    if selected_org_db:
        is_personal_mode = (selected_org_db == "data/shop_bot.db")
    else:
        user_db_path = tenant_manager.get_user_db_path(callback.from_user.id)
        is_personal_mode = (user_db_path == "data/shop_bot.db")
    
    if is_personal_mode:
        # В личном режиме берём магазин из профиля пользователя
        user = await current_db.get_user(callback.from_user.id)
        if user and user[8]:  # shop_name в позиции 8
            shop_name = user[8]
            await state.update_data(
                inventory_product_id=product_id,
                inventory_product_name=product[1],
                inventory_shop=shop_name
            )
            await state.set_state(NewProductSetupStates.waiting_inventory_quantity)
            
            builder = InlineKeyboardBuilder()
            builder.button(text="❌ Отмена", callback_data="products")
            
            await callback.message.edit_text(
                f"📦 <b>Установка остатков</b>\n\n"
                f"Товар: <b>{he(product[1])}</b>\n"
                f"Магазин: <b>{he(shop_name)}</b>\n\n"
                f"Введите количество:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            await callback.answer()
            return
        else:
            # Нет магазина в профиле
            await callback.message.edit_text(
                f"📦 <b>Установка остатков</b>\n\n"
                f"⚠️ В вашем профиле не указан магазин.\n"
                f"Перейдите в «Личный кабинет» → «Редактировать профиль» и укажите название магазина.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Понятно", callback_data="products")
                ]]),
                parse_mode="HTML"
            )
            await callback.answer()
            return
    
    # Корпоративный режим — получаем список магазинов
    shops = await current_db.get_all_shops()
    
    if not shops:
        # Если магазинов нет, создаём магазин по умолчанию
        await callback.message.edit_text(
            f"📦 <b>Установка остатков</b>\n\n"
            f"Товар: <b>{he(product[1])}</b>\n\n"
            f"⚠️ Нет доступных магазинов.\n"
            f"Сначала добавьте магазин через меню Запасы → Магазины.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Понятно", callback_data="products")
            ]]),
            parse_mode="HTML"
        )
        return
    
    await state.update_data(
        inventory_product_id=product_id, 
        inventory_product_name=product[1]
    )
    
    # Если один магазин - сразу выбираем его
    if len(shops) == 1:
        shop_name = shops[0]
        await state.update_data(inventory_shop=shop_name)
        await state.set_state(NewProductSetupStates.waiting_inventory_quantity)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data="products")
        
        await callback.message.edit_text(
            f"📦 <b>Установка остатков</b>\n\n"
            f"Товар: <b>{he(product[1])}</b>\n"
            f"Магазин: <b>{he(shop_name)}</b>\n\n"
            f"Введите количество:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        # Выбор магазина
        builder = InlineKeyboardBuilder()
        for shop in shops:
            builder.button(text=f"🏪 {shop}", callback_data=safe_cb("new_inv_shop_", shop))
        builder.button(text="❌ Отмена", callback_data="products")
        builder.adjust(1)
        
        await callback.message.edit_text(
            f"📦 <b>Установка остатков</b>\n\n"
            f"Товар: <b>{he(product[1])}</b>\n\n"
            f"Выберите магазин:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    
    await callback.answer()


@products_router.callback_query(F.data.startswith("new_inv_shop_"))
async def new_inventory_shop_selected(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина для остатков"""
    shop_raw = callback.data.replace("new_inv_shop_", "")
    current_db_temp = await get_db(callback.from_user.id, state)
    _tmp_shops = await current_db_temp.get_all_shops()
    shop_name = resolve_cb_name(shop_raw, _tmp_shops or [])
    data = await state.get_data()
    product_name = data.get('inventory_product_name', 'Товар')
    
    await state.update_data(inventory_shop=shop_name, anchor_msg_id=callback.message.message_id)
    await state.set_state(NewProductSetupStates.waiting_inventory_quantity)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="products")
    
    await callback.message.edit_text(
        f"📦 <b>Установка остатков</b>\n\n"
        f"Товар: <b>{he(product_name)}</b>\n"
        f"Магазин: <b>{he(shop_name)}</b>\n\n"
        f"Введите количество:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@products_router.message(NewProductSetupStates.waiting_inventory_quantity)
async def process_new_inventory_quantity(message: Message, state: FSMContext):
    """Обработка количества остатков"""
    _inv_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="products")]])
    try:
        quantity = int(message.text)
        if quantity < 0:
            raise ValueError("Количество не может быть отрицательным")
    except ValueError:
        await fsm_edit(state, message, "📦 ❌ Введите корректное целое число (0 или больше):", reply_markup=_inv_kb)
        return
    
    data = await state.get_data()
    product_id = data.get('inventory_product_id')
    product_name = data.get('inventory_product_name')
    shop_name = data.get('inventory_shop')
    
    current_db = await get_db(message.from_user.id, state)
    await current_db.add_inventory(shop_name, product_id, quantity, message.from_user.id, 'manual', 'Начальные остатки')
    
    await fsm_edit(state, message,
                   f"✅ Остатки установлены!\n\n"
                   f"Товар: <b>{he(product_name)}</b>\n"
                   f"Магазин: <b>{he(shop_name)}</b>\n"
                   f"Количество: <b>{quantity} шт.</b>",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                       InlineKeyboardButton(text="✅ Готово", callback_data="products")
                   ]]))
    await clear_state_keep_org(state)


# ─────────────────────────────────────────────────────────────
#  ПАКЕТНЫЙ ИМПОРТ ТОВАРОВ
# ─────────────────────────────────────────────────────────────

@products_router.callback_query(F.data == "bulk_import_products")
async def bulk_import_start(callback: CallbackQuery, state: FSMContext):
    """Начало импорта товаров списком"""
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return

    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)

    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    # Проверяем остаток слотов по подписке
    slots_info = ""
    if not is_super:
        from subscription_utils import get_plan_limits
        current_db = await get_db(callback.from_user.id, state)
        limits = get_plan_limits(callback.from_user.id)
        max_products = limits['max_products']
        if max_products != -1:
            current_count = len(await current_db.get_all_products())
            remaining = max_products - current_count
            if remaining <= 0:
                await callback.answer()
                await callback.message.edit_text(
                    "🚫 <b>Достигнут лимит товаров</b>\n\n"
                    f"Ваш тариф позволяет {max_products} товаров — все слоты заняты.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Подписка", callback_data="subscription_menu")],
                        [back_button("products")]
                    ]),
                    parse_mode="HTML"
                )
                return
            slots_info = f"\n\n⚠️ По вашему тарифу можно добавить ещё <b>{remaining}</b> товаров."

    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.answer()
    await callback.message.edit_text(
        "📥 <b>Добавление товаров списком</b>\n\n"
        "Отправьте список — каждый товар с новой строки:\n"
        "<code>Название;Категория;Цена</code>\n\n"
        "<b>Пример:</b>\n"
        "<code>Шапка зимняя;Головные уборы;1200\n"
        "Шарф вязаный;Аксессуары;800\n"
        "Куртка детская;Одежда;3500.50</code>\n\n"
        "📌 <b>Правила:</b>\n"
        "• Разделитель полей — точка с запятой <code>;</code>\n"
        "• Название и категория: 2–30 символов\n"
        "• Цена: положительное число (можно с точкой)\n"
        f"• Максимум {BULK_IMPORT_MAX} товаров за раз\n"
        "• Строки начинающиеся с # пропускаются"
        f"{slots_info}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
        parse_mode="HTML"
    )
    await state.set_state(ProductStates.waiting_for_bulk_list)


@products_router.message(ProductStates.waiting_for_bulk_list)
async def process_bulk_list(message: Message, state: FSMContext):
    """Парсинг списка товаров и показ превью для подтверждения"""
    is_super = env_manager.is_super_admin(message.from_user.id)
    is_admin = is_any_admin(message.from_user.id)

    if not is_super and not is_admin:
        return

    if not message.text:
        await fsm_edit(state, message,
                       "📥 Отправьте список товаров текстом.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return

    valid, notices, skipped_count = _parse_bulk_products(message.text)

    if not valid and not notices:
        await fsm_edit(state, message,
                       "📥 ❌ Список пуст. Отправьте хотя бы один товар.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]))
        return

    # Учитываем лимит подписки
    capped = False
    if not is_super and valid:
        from subscription_utils import get_plan_limits
        current_db = await get_db(message.from_user.id, state)
        limits = get_plan_limits(message.from_user.id)
        max_products = limits['max_products']
        if max_products != -1:
            current_count = len(await current_db.get_all_products())
            remaining = max_products - current_count
            if remaining <= 0:
                await fsm_edit(state, message,
                    "🚫 <b>Достигнут лимит товаров.</b> Добавление невозможно.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Подписка", callback_data="subscription_menu")],
                        [back_button("products")]
                    ]),
                    parse_mode="HTML"
                )
                return
            if len(valid) > remaining:
                capped = True
                valid = valid[:remaining]

    if not valid:
        # Все строки пропущены — показываем замечания
        notice_text = "\n".join(f"• {n}" for n in notices[:10])
        if len(notices) > 10:
            notice_text += f"\n<i>...и ещё {len(notices) - 10} замечаний</i>"
        await fsm_edit(state, message,
            f"❌ <b>Нет товаров для добавления</b>\n\n"
            f"ℹ️ <b>Замечания:</b>\n{notice_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
            parse_mode="HTML"
        )
        return

    # Сохраняем валидный список в FSM
    await state.update_data(bulk_products=valid)

    # Формируем превью (первые 20 строк)
    preview_lines = []
    for idx, item in enumerate(valid[:20], 1):
        price_str = format_currency(item['price']) if item['price'] > 0 else "0 ₽"
        preview_lines.append(
            f"{idx}. <b>{item['name']}</b> | {item['category']} | {price_str}"
        )
    preview = "\n".join(preview_lines)
    if len(valid) > 20:
        preview += f"\n<i>...и ещё {len(valid) - 20} товаров</i>"

    summary = f"✅ <b>Готово к добавлению: {len(valid)} товаров</b>\n\n{preview}"

    # Замечания: подстановки (⚠️) и пропуски (❌) — показываем компактно
    if notices:
        warnings = [n for n in notices if n.startswith('⚠️')]
        skips = [n for n in notices if n.startswith('❌')]
        if warnings:
            shown = warnings[:4]
            block = "\n".join(f"• {w}" for w in shown)
            if len(warnings) > 4:
                block += f"\n<i>...и ещё {len(warnings) - 4}</i>"
            summary += f"\n\n⚠️ <b>Подставлены значения по умолчанию ({len(warnings)}):</b>\n{block}"
        if skips:
            shown = skips[:3]
            block = "\n".join(f"• {s}" for s in shown)
            if len(skips) > 3:
                block += f"\n<i>...и ещё {len(skips) - 3}</i>"
            summary += f"\n\n❌ <b>Пропущено строк ({skipped_count}):</b>\n{block}"

    if capped:
        summary += f"\n\n⚠️ <b>Лимит тарифа:</b> будет добавлено только первые {len(valid)} товаров."

    await fsm_edit(state, message, summary,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Добавить {len(valid)} товаров",
                callback_data="confirm_bulk_import"
            )],
            [InlineKeyboardButton(
                text="✏️ Исправить и отправить снова",
                callback_data="bulk_import_products"
            )],
            [back_button("products")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(ProductStates.confirming_bulk_import)


@products_router.message(ProductStates.confirming_bulk_import)
async def bulk_confirm_got_message(message: Message, state: FSMContext):
    """Пользователь прислал новый список пока ждём подтверждения — обрабатываем как новый список"""
    await process_bulk_list(message, state)


@products_router.callback_query(ProductStates.confirming_bulk_import, F.data == "confirm_bulk_import")
async def confirm_bulk_import(callback: CallbackQuery, state: FSMContext):
    """Подтверждение: записываем товары в БД"""
    data = await state.get_data()
    bulk_products = data.get('bulk_products', [])

    if not bulk_products:
        await callback.answer("❌ Список товаров потерян. Начните заново.", show_alert=True)
        await clear_state_keep_org(state)
        await callback.message.edit_text(
            "🛍 <b>Управление товарами</b>\n\nВыберите действие:",
            reply_markup=products_menu(),
            parse_mode="HTML"
        )
        return

    current_db = await get_db(callback.from_user.id, state)
    added, skipped = await current_db.add_products_bulk(bulk_products)

    await clear_state_keep_org(state)

    result_text = (
        f"✅ <b>Импорт завершён!</b>\n\n"
        f"📦 Добавлено товаров: <b>{added}</b>"
    )

    if skipped:
        skipped_block = "\n".join(f"• {name}" for name in skipped[:5])
        if len(skipped) > 5:
            skipped_block += f"\n<i>...и ещё {len(skipped) - 5}</i>"
        result_text += (
            f"\n⚠️ Пропущено (уже существуют): <b>{len(skipped)}</b>\n"
            f"{skipped_block}"
        )

    await callback.answer(
        f"✅ Добавлено {added} товаров!" if added else "⚠️ Ни один товар не был добавлен.",
        show_alert=True
    )
    await callback.message.edit_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список товаров", callback_data="list_products")],
            [back_button("products")]
        ]),
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────────────────────
#  ИМПОРТ ТОВАРОВ ИЗ EXCEL (.xlsx)
# ─────────────────────────────────────────────────────────────

@products_router.callback_query(F.data == "excel_import_products")
async def excel_import_start(callback: CallbackQuery, state: FSMContext):
    """Начало импорта товаров из Excel-файла."""
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return

    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)
    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.answer()
    await callback.message.edit_text(
        "📊 <b>Импорт товаров из Excel</b>\n\n"
        "Пришлите файл <b>.xlsx</b> со следующими столбцами:\n"
        "<code>A: Название товара\n"
        "B: Категория\n"
        "C: Цена (число)</code>\n\n"
        "📌 Первую строку (шапку) можно пропустить — "
        "строки, где цена не число, игнорируются.\n\n"
        "⬆️ Отправьте файл прямо в чат:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
        parse_mode="HTML"
    )
    await state.set_state(ExcelImportStates.waiting_file)


@products_router.message(ExcelImportStates.waiting_file)
async def excel_import_receive_file(message: Message, state: FSMContext):
    """Принимает xlsx-файл и показывает превью для подтверждения."""
    is_super = env_manager.is_super_admin(message.from_user.id)
    is_admin = is_any_admin(message.from_user.id)
    if not is_super and not is_admin:
        return

    if not message.document:
        await fsm_edit(
            state, message,
            "📊 Пожалуйста, отправьте файл <b>.xlsx</b> (Excel).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
            parse_mode="HTML"
        )
        return

    if not message.document.file_name or not message.document.file_name.lower().endswith('.xlsx'):
        await fsm_edit(
            state, message,
            "❌ Принимаются только файлы <b>.xlsx</b>. Отправьте корректный файл.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
            parse_mode="HTML"
        )
        return

    try:
        import io
        import openpyxl

        file = await message.bot.get_file(message.document.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes.read()), read_only=True, data_only=True)
        ws = wb.active

        valid = []
        skipped_rows = 0
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 3:
                skipped_rows += 1
                continue
            name = str(row[0]).strip() if row[0] is not None else ""
            category = str(row[1]).strip() if row[1] is not None else "Без категории"
            try:
                price = float(str(row[2]).replace(',', '.').strip())
            except (ValueError, TypeError):
                skipped_rows += 1
                continue

            if not name or len(name) < 2 or price <= 0:
                skipped_rows += 1
                continue

            valid.append({'name': name[:50], 'category': category[:30] or "Без категории", 'price': price})
            if len(valid) >= BULK_IMPORT_MAX:
                break

        wb.close()

        if not valid:
            await fsm_edit(
                state, message,
                "❌ <b>Файл не содержит валидных данных.</b>\n\n"
                "Убедитесь, что столбцы: A=Название, B=Категория, C=Цена.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
                parse_mode="HTML"
            )
            return

        # Показываем превью (первые 10 строк)
        preview_lines = [f"{i+1}. <b>{he(p['name'])}</b> | {he(p['category'])} | {format_currency(p['price'])}"
                         for i, p in enumerate(valid[:10])]
        extra = f"\n<i>...и ещё {len(valid) - 10}</i>" if len(valid) > 10 else ""
        preview = "\n".join(preview_lines)

        skip_msg = f"\n\n⚠️ Пропущено строк: {skipped_rows}" if skipped_rows else ""
        await state.update_data(excel_import_data=valid)

        await fsm_edit(
            state, message,
            f"📊 <b>Превью импорта</b> — {len(valid)} товаров{skip_msg}\n\n"
            f"{preview}{extra}\n\n"
            f"Подтвердите импорт:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Импортировать {len(valid)} товаров", callback_data="excel_import_confirm")],
                [back_button("products")]
            ]),
            parse_mode="HTML"
        )
        await state.set_state(ExcelImportStates.confirming_import)

    except Exception as e:
        await fsm_edit(
            state, message,
            f"❌ Ошибка при чтении файла: {he(str(e)[:100])}\n\nПопробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("products")]]),
            parse_mode="HTML"
        )


@products_router.callback_query(ExcelImportStates.confirming_import, F.data == "excel_import_confirm")
async def excel_import_confirm(callback: CallbackQuery, state: FSMContext):
    """Выполняет массовый импорт товаров из Excel."""
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)
    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    data = await state.get_data()
    excel_data = data.get("excel_import_data", [])

    if not excel_data:
        await callback.answer("❌ Данные устарели. Начните заново.", show_alert=True)
        await clear_state_keep_org(state)
        return

    current_db = await get_db(callback.from_user.id, state)
    bulk_products = [(p['name'], p['category'], p['price']) for p in excel_data]
    added, skipped = await current_db.add_products_bulk(bulk_products)

    await clear_state_keep_org(state)
    await callback.answer(
        f"✅ Добавлено {added} товаров!" if added else "⚠️ Нет новых товаров.",
        show_alert=True
    )

    result_text = f"✅ <b>Excel-импорт завершён!</b>\n\n📦 Добавлено: <b>{added}</b>"
    if skipped:
        result_text += f"\n⚠️ Пропущено (уже есть): <b>{len(skipped)}</b>"

    await callback.message.edit_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список товаров", callback_data="list_products")],
            [back_button("products")]
        ]),
        parse_mode="HTML"
    )
