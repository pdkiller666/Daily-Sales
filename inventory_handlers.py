"""
Обработчики для управления остатками (административные функции)
"""

import os
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from database import Database
from keyboards import back_button, safe_cb, resolve_cb_name
from message_utils import safe_edit_message, fsm_edit
from states import InventoryStates
from utils import format_currency, get_stock_color_indicator, he
from env_manager import env_manager

inventory_router = Router()

from db_utils import get_db, clear_state_keep_org, is_any_admin, wrap_db

@inventory_router.callback_query(F.data == "manage_inventory")
async def manage_inventory_callback(callback: CallbackQuery, state: FSMContext):
    """Меню управления остатками для админа"""
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)

    if not is_super and not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()

    rows = [
        [InlineKeyboardButton(text="👁️ Просмотр остатков",    callback_data="user_inventory_view")],
        [InlineKeyboardButton(text="✏️ Редактировать остатки", callback_data="user_inventory_edit")],
        [back_button("admin_management")],
    ]

    await callback.message.edit_text(
        "📦 <b>Управление остатками</b>\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )

@inventory_router.callback_query(F.data == "user_inventory_menu")
async def user_inventory_menu(callback: CallbackQuery, state: FSMContext):
    """Единое меню остатков для пользователей"""
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    
    if not user:
        # Пробуем найти в основной БД
        import asyncio as _asyncio
        from database import Database as CentralDB
        central_db = CentralDB('data/main.db')
        user = await _asyncio.to_thread(central_db.get_user, callback.from_user.id)
        if user:
            from tenant_manager import tenant_manager
            # Если нашли в основной, используем путь к тенанту
            db_path = tenant_manager.get_user_db_path(callback.from_user.id)
            current_db = wrap_db(Database(db_path))
            # Принудительно проверяем существование таблиц
            await current_db.create_tables()
            
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
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)

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

    # Орг-админ: показываем выбор магазина из всей орг (users + inventory)
    if is_admin and not is_super:
        all_shops = sorted(set(await current_db.get_all_shops() or []) | set(await current_db.get_inventory_shops() or []))
        if len(all_shops) > 1:
            builder = InlineKeyboardBuilder()
            for shop in all_shops:
                builder.add(InlineKeyboardButton(
                    text=f"🏪 {shop}",
                    callback_data=safe_cb("inv_edit_shop_", shop)
                ))
            builder.add(back_button("manage_inventory"))
            builder.adjust(1)
            await callback.message.edit_text(
                "📦 Изменение остатков\n\nВыберите магазин:",
                reply_markup=builder.as_markup()
            )
            return
        elif len(all_shops) == 1:
            user_shop = all_shops[0]
        # Если магазинов нет — падём ниже на проверку user_shop

    # Для суп-админа: если shop_name дефолтный "Системный" — предлагаем выбор реального магазина
    elif is_super and (not user_shop or user_shop == "Системный"):
        inv_shops = await current_db.get_inventory_shops()
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
                builder.add(back_button("manage_inventory"))
                builder.adjust(1)
                await callback.message.edit_text(
                    "📦 Изменение остатков\n\nВыберите магазин:",
                    reply_markup=builder.as_markup()
                )
                return

    if not user_shop:
        back_cb_err = "manage_inventory" if (is_admin or is_super) else "main_menu"
        await callback.message.edit_text(
            "❌ У вас не указан магазин в профиле.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(back_cb_err)]])
        )
        return

    back_cb = "manage_inventory" if (is_admin or is_super) else "user_inventory_menu"
    await _show_edit_inventory_categories(callback, state, current_db, user_shop, back_cb=back_cb)

@inventory_router.callback_query(F.data.startswith("edit_inv_category_"))
async def edit_inventory_category_selected(callback: CallbackQuery, state: FSMContext):
    """Отображение товаров выбранной категории для редактирования"""
    category_raw = callback.data.replace("edit_inv_category_", "")
    current_db_tmp = await get_db(callback.from_user.id, state)
    _tmp_cats = await current_db_tmp.get_all_categories()
    category = resolve_cb_name(category_raw, _tmp_cats or [])
    
    data = await state.get_data()
    user_shop = data.get('user_shop')
    
    if not user_shop:
        await callback.answer("❌ Ошибка: магазин не найден!", show_alert=True)
        return
    
    # Получаем все товары выбранной категории
    current_db = await get_db(callback.from_user.id, state)
    all_products = await current_db.get_all_products()
    category_products = []
    
    for product in all_products:
        # product содержит: (id, name, category, price)
        prod_category = product[2]
        if prod_category == category:
            product_id = product[0]
            product_name = product[1]
            price = product[3]
            # Проверяем текущие остатки для этого товара в магазине
            quantity = await current_db.get_inventory(user_shop, product_id)
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
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    # Приоритет: магазин, выбранный в сессии (при multi-shop), иначе — профиль
    data = await state.get_data()
    user_shop = data.get('user_shop') or (user[8] if user else None)

    # Получаем информацию о товаре
    product = await current_db.get_product(product_id)
    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return

    await callback.answer()
    # Получаем текущие остатки
    current_quantity = await current_db.get_inventory(user_shop, product_id)
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
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить количество", callback_data=f"inv_do_edit_{product_id}"),
        InlineKeyboardButton(text="📋 История", callback_data=f"inv_log_{product_id}"),
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit_inventory"))
    await callback.message.edit_text(
        f"📦 <b>Управление остатками</b>\n\n"
        f"🏷 Товар: <b>{he(product_name)}</b>\n"
        f"📂 Категория: {he(category)}\n"
        f"🏪 Магазин: {he(user_shop)}\n"
        f"📊 Текущие остатки: <b>{current_quantity} шт.</b>\n\n"
        f"Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

# ДОБАВЛЕН НОВЫЙ ОБРАБОТЧИК ДЛЯ ОТМЕНЫ РЕДАКТИРОВАНИЯ
@inventory_router.callback_query(F.data == "cancel_edit_inventory")
async def cancel_edit_inventory(callback: CallbackQuery, state: FSMContext):
    """Отмена редактирования остатков"""
    await clear_state_keep_org(state)
    await edit_inventory_user(callback, state)


@inventory_router.callback_query(F.data.startswith("inv_do_edit_"))
async def inv_do_edit_handler(callback: CallbackQuery, state: FSMContext):
    """Переход к вводу нового количества (из меню управления товаром)."""
    await callback.answer()
    data = await state.get_data()
    product_name = data.get('product_name', '')
    user_shop = data.get('edit_shop', '')
    current_quantity = data.get('current_quantity', 0)
    await state.set_state(InventoryStates.editing_quantity)
    await callback.message.edit_text(
        f"📦 Изменение остатков\n\n"
        f"🏷 Товар: {he(product_name)}\n"
        f"🏪 Магазин: {he(user_shop)}\n"
        f"📊 Текущие остатки: {current_quantity} шт.\n\n"
        f"Введите новое количество:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit_inventory")]
        ])
    )


@inventory_router.callback_query(F.data.startswith("inv_log_"))
async def inv_log_handler(callback: CallbackQuery, state: FSMContext):
    """История движения остатков товара."""
    await callback.answer()
    product_id = int(callback.data.replace("inv_log_", ""))
    data = await state.get_data()
    shop_name = data.get('edit_shop', '')
    product_name = data.get('product_name', '')
    current_db = await get_db(callback.from_user.id, state)
    rows = await current_db.get_inventory_log(shop_name, product_id, limit=30)
    if not rows:
        text = (
            f"📋 <b>История движения: {he(product_name)}</b>\n"
            f"🏪 {he(shop_name)}\n\n"
            "Изменений ещё не было."
        )
    else:
        text = (
            f"📋 <b>История движения: {he(product_name)}</b>\n"
            f"🏪 {he(shop_name)}\n\n"
        )
        for row in rows:
            _, old_qty, new_qty, delta, change_type, change_reason, changed_by, changed_at, changer_name = row
            changed_at_str = str(changed_at)[:16] if changed_at else "—"
            if delta is not None:
                sign = "+" if delta >= 0 else ""
                delta_str = f"{sign}{delta}"
            else:
                delta_str = "?"
            who = he(changer_name) if changer_name else "—"
            reason_str = f" · {he(change_reason)}" if change_reason else ""
            text += (
                f"🕐 <b>{changed_at_str}</b> · {who}\n"
                f"   {old_qty} → <b>{new_qty}</b> ({delta_str}){reason_str}\n\n"
            )
    back_cb = f"edit_inv_product_{product_id}"
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)]
        ]),
        parse_mode="HTML"
    )


@inventory_router.message(InventoryStates.editing_quantity)
async def process_new_quantity(message: Message, state: FSMContext):
    """Обработчик изменения остатков"""
    current_db = await get_db(message.from_user.id, state)
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
    user_id = await current_db.get_user_id(message.from_user.id)
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
        existing_quantity = await current_db.get_inventory(shop_name, product_id)
        if existing_quantity is None:
            await current_db.add_inventory(shop_name, product_id, new_quantity, user_id, 'user_edit', f'Установка остатков: {new_quantity} шт.')
        else:
            delta = new_quantity - existing_quantity
            change_reason = f'Изменение с {existing_quantity} на {new_quantity} шт. ({"+" if delta > 0 else ""}{delta})'
            await current_db.update_inventory(shop_name, product_id, delta, user_id, 'user_edit', change_reason)

        _gs_status_edit = []
        try:
            from integration.manager import integration_manager as _int_mgr
            from datetime import datetime as _dt
            _sync_db_edit = object.__getattribute__(current_db, '_db') if hasattr(current_db, '_db') else current_db
            _gs_status_edit = await _int_mgr.trigger_export_with_result(_sync_db_edit, 'inventory', {
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

async def _show_edit_inventory_categories(callback: CallbackQuery, state: FSMContext, current_db, user_shop: str,
                                          back_cb: str = "user_inventory_menu"):
    """Вспомогательная функция: показывает категории для редактирования остатков"""
    all_products = await current_db.get_all_products()
    if not all_products:
        await callback.message.edit_text(
            f"📦 Остатки в магазине '{user_shop}'\n\n❌ Товары не найдены.\n\nОбратитесь к администратору для добавления товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(back_cb)]])
        )
        return
    categories = set()
    for product in all_products:
        categories.add(product[2])
    await state.update_data(user_shop=user_shop, action="edit_inventory", inv_edit_back_cb=back_cb)
    builder = InlineKeyboardBuilder()
    for category in sorted(categories):
        builder.add(InlineKeyboardButton(
            text=f"📂 {category}",
            callback_data=safe_cb("edit_inv_category_", category)
        ))
    builder.add(back_button(back_cb))
    builder.adjust(2, 1)
    await callback.message.edit_text(
        f"📦 Изменение остатков - {user_shop}\n\nВыберите категорию:",
        reply_markup=builder.as_markup()
    )


async def _show_view_inventory_categories(callback: CallbackQuery, state: FSMContext, current_db, user_shop: str,
                                          back_cb: str = "user_inventory_menu"):
    """Вспомогательная функция: показывает категории для просмотра остатков"""
    all_products = await current_db.get_all_products()
    if not all_products:
        await callback.message.edit_text(
            f"📦 Остатки в магазине '{user_shop}'\n\n❌ Товары не найдены.\n\nОбратитесь к администратору для добавления товаров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(back_cb)]])
        )
        return
    categories = set()
    total_items = 0
    low_stock_count = 0
    for product in all_products:
        category = product[2]
        product_id = product[0]
        quantity = await current_db.get_inventory(user_shop, product_id)
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
    builder.add(back_button(back_cb))
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
    """Выбор магазина для редактирования остатков (суп-адмін или орг-адмін)"""
    await callback.answer()
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)
    shop_raw = callback.data.replace("inv_edit_shop_", "")
    current_db = await get_db(callback.from_user.id, state)
    all_names = list(await current_db.get_all_shops() or []) + list(await current_db.get_inventory_shops() or [])
    user_shop = resolve_cb_name(shop_raw, all_names)
    back_cb = "user_inventory_edit" if (is_admin and not is_super) else "user_inventory_edit"
    await _show_edit_inventory_categories(callback, state, current_db, user_shop, back_cb=back_cb)


@inventory_router.callback_query(F.data.startswith("inv_view_shop_"))
async def inv_view_shop_selected(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина для просмотра остатков (суп-адмін или орг-адмін)"""
    await callback.answer()
    is_super = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id)
    shop_raw = callback.data.replace("inv_view_shop_", "")
    current_db = await get_db(callback.from_user.id, state)
    all_names = list(await current_db.get_all_shops() or []) + list(await current_db.get_inventory_shops() or [])
    user_shop = resolve_cb_name(shop_raw, all_names)
    back_cb = "user_inventory_view"
    await _show_view_inventory_categories(callback, state, current_db, user_shop, back_cb=back_cb)


@inventory_router.callback_query(F.data == "user_inventory_view")
async def user_inventory_view(callback: CallbackQuery, state: FSMContext):
    """Просмотр остатков пользователем в своем магазине"""
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)

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

    # Орг-админ: показываем выбор магазина из всей орг (users + inventory)
    if is_admin and not is_super:
        all_shops = sorted(set(await current_db.get_all_shops() or []) | set(await current_db.get_inventory_shops() or []))
        if len(all_shops) > 1:
            builder = InlineKeyboardBuilder()
            for shop in all_shops:
                builder.add(InlineKeyboardButton(
                    text=f"🏪 {shop}",
                    callback_data=safe_cb("inv_view_shop_", shop)
                ))
            builder.add(back_button("manage_inventory"))
            builder.adjust(1)
            await callback.message.edit_text(
                "📦 Просмотр остатков\n\nВыберите магазин:",
                reply_markup=builder.as_markup()
            )
            return
        elif len(all_shops) == 1:
            user_shop = all_shops[0]

    # Для суп-админа: если shop_name дефолтный "Системный" — предлагаем выбор реального магазина
    elif is_super and (not user_shop or user_shop == "Системный"):
        inv_shops = await current_db.get_inventory_shops()
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
                builder.add(back_button("manage_inventory"))
                builder.adjust(1)
                await callback.message.edit_text(
                    "📦 Просмотр остатков\n\nВыберите магазин:",
                    reply_markup=builder.as_markup()
                )
                return

    if not user_shop:
        back_cb_err = "manage_inventory" if (is_admin or is_super) else "main_menu"
        await callback.message.edit_text(
            "❌ У вас не указан магазин в профиле.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(back_cb_err)]])
        )
        return

    await callback.answer()
    back_cb = "manage_inventory" if (is_admin or is_super) else "user_inventory_menu"
    await _show_view_inventory_categories(callback, state, current_db, user_shop, back_cb=back_cb)

@inventory_router.callback_query(F.data.startswith("view_user_category_"))
async def view_user_category_items(callback: CallbackQuery, state: FSMContext):
    """Отображение товаров выбранной категории для пользователя"""
    category_raw = callback.data.replace("view_user_category_", "")
    current_db_tmp = await get_db(callback.from_user.id, state)
    _tmp_cats = await current_db_tmp.get_all_categories()
    category = resolve_cb_name(category_raw, _tmp_cats or [])
    
    data = await state.get_data()
    user_shop = data.get('user_shop')
    
    if not user_shop:
        await callback.answer("❌ Ошибка: магазин не найден!", show_alert=True)
        return
    
    current_db = await get_db(callback.from_user.id, state)
    # Получаем все остатки для магазина пользователя с полной информацией
    inventory = await current_db.get_all_inventory(user_shop)
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
        all_products = await current_db.get_all_products()
        for product in all_products:
            if product[2] == category:
                product_id = product[0]
                product_name = product[1]
                price = product[3]
                # Проверяем остатки
                quantity = await current_db.get_inventory(user_shop, product_id) or 0
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
