"""
Обработчики для системы мотивации и комиссий продавцов
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command

from database import Database
from keyboards import InlineKeyboardBuilder, safe_cb, resolve_cb_name
from env_manager import env_manager
from utils import format_price, he
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit

commission_router = Router()

class MotivationStates(StatesGroup):
    waiting_for_motivation_value = State()
    waiting_for_motivation_type = State()
    searching_product = State()

class ExtraConditionStates(StatesGroup):
    entering_min_sellers = State()
    selecting_calc_mode = State()
    entering_coefficient = State()
    selecting_categories = State()

@commission_router.callback_query(F.data == "admin_motivation")
async def admin_motivation_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню управления мотивацией"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Установить мотивацию", callback_data="set_motivation")
    builder.button(text="📊 Просмотр всех мотиваций", callback_data="view_all_motivations")
    builder.button(text="🗑️ Удалить мотивацию", callback_data="remove_motivation")
    builder.button(text="📈 Топ продавцов", callback_data="top_sellers")
    builder.button(text="⚙️ Доп. условия", callback_data="motivation_extra")
    builder.button(text="⬅️ Назад", callback_data="admin_management")
    builder.adjust(1)

    await callback.message.edit_text(
        "🎯 <b>Система мотивации продавцов</b>\n\n"
        "Управление мотивациями по товарам и просмотр статистики заработка.\n\n"
        "Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@commission_router.callback_query(F.data == "set_motivation")
async def set_motivation_start(callback: CallbackQuery, state: FSMContext):
    """Начало установки мотивации — сначала выбираем категорию"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    categories = current_db.get_all_categories()

    if not categories:
        await callback.message.edit_text(
            "❌ <b>Нет товаров</b>\n\nСначала добавьте товары в систему.",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=f"📂 {cat}", callback_data=safe_cb("motiv_cat_", cat))
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")
    builder.adjust(1)

    await callback.message.edit_text(
        "📝 <b>Установка мотивации — шаг 1/3</b>\n\n"
        "Выберите категорию товаров:",
        reply_markup=builder.as_markup(), parse_mode="HTML",
    )
    await callback.answer()


@commission_router.callback_query(F.data.startswith("motiv_cat_"))
async def set_motivation_category_selected(callback: CallbackQuery, state: FSMContext):
    """Категория выбрана — показываем товары этой категории с поиском"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    raw = callback.data[len("motiv_cat_"):]
    current_db = await get_db(callback.from_user.id, state)
    categories = current_db.get_all_categories()
    category = resolve_cb_name(raw, categories)

    products = current_db.get_products_by_category(category)
    if not products:
        await callback.answer("❌ Нет товаров в этой категории", show_alert=True)
        return

    all_motivations = current_db.get_all_product_motivations()
    motivations_map = {row[0]: {'motivation_type': row[2], 'motivation_value': row[3]}
                       for row in all_motivations if row[2]}

    await state.update_data(motiv_category=category,
                            motiv_products_cache=[p[0] for p in products])

    await _render_motiv_products(callback, state, category, products, motivations_map)
    await callback.answer()


async def _render_motiv_products(callback, state, category, products, motivations_map, search_query=""):
    """Отрисовка списка товаров категории для установки мотивации (с поиском)"""
    from utils import format_price as fp
    filtered = products
    if search_query:
        q = search_query.lower()
        filtered = [p for p in products if q in p[1].lower()]

    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти товар", callback_data="motiv_prod_search")
    for p in filtered[:30]:
        info = motivations_map.get(p[0])
        suffix = ""
        if info and info['motivation_type']:
            suffix = f" ({info['motivation_value']}%)" if info['motivation_type'] == 'percentage' \
                else f" ({fp(info['motivation_value'])})"
        builder.button(text=f"{p[1]}{suffix}", callback_data=f"set_motiv_product_{p[0]}")
    builder.button(text="⬅️ Назад к категориям", callback_data="set_motivation")
    builder.adjust(1)

    extra = f"\n🔍 Результаты для: «{search_query}» — {len(filtered)} шт." if search_query else ""
    await callback.message.edit_text(
        f"📝 <b>Установка мотивации — шаг 2/3</b>\n\n"
        f"📂 Категория: <b>{he(category)}</b>{extra}\n\n"
        "Выберите товар:",
        reply_markup=builder.as_markup(), parse_mode="HTML",
    )


@commission_router.callback_query(F.data == "motiv_prod_search")
async def motiv_prod_search_start(callback: CallbackQuery, state: FSMContext):
    """Начало поиска товара при установке мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(MotivationStates.searching_product)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"motiv_cat_back")
    await callback.message.edit_text(
        "🔍 <b>Поиск товара</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@commission_router.callback_query(F.data == "motiv_cat_back")
async def motiv_cat_back(callback: CallbackQuery, state: FSMContext):
    """Вернуться к списку товаров категории (отменить поиск)"""
    await callback.answer()
    data = await state.get_data()
    category = data.get('motiv_category', '')
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    products = current_db.get_products_by_category(category)
    all_motivations = current_db.get_all_product_motivations()
    motivations_map = {row[0]: {'motivation_type': row[2], 'motivation_value': row[3]}
                       for row in all_motivations if row[2]}
    await _render_motiv_products(callback, state, category, products, motivations_map)


@commission_router.message(MotivationStates.searching_product)
async def motiv_prod_search_process(message: Message, state: FSMContext):
    """Обработка поискового запроса товара при установке мотивации"""
    query = message.text.strip()
    data = await state.get_data()
    category = data.get('motiv_category', '')
    await state.set_state(None)
    from message_utils import fsm_edit as _fe
    current_db = await get_db(message.from_user.id, state)
    products = current_db.get_products_by_category(category)
    all_motivations = current_db.get_all_product_motivations()
    motivations_map = {row[0]: {'motivation_type': row[2], 'motivation_value': row[3]}
                       for row in all_motivations if row[2]}

    class _FakeCallback:
        def __init__(self, msg): self.message = msg; self.from_user = msg.from_user
        async def answer(self): pass

    await _render_motiv_products(_FakeCallback(message), state, category, products,
                                  motivations_map, search_query=query)


@commission_router.callback_query(F.data.startswith("set_motiv_product_"))
async def set_motivation_product_selected(callback: CallbackQuery, state: FSMContext):
    """Товар выбран — показываем текущую мотивацию, историю и выбор типа"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[-1])
    current_db = await get_db(callback.from_user.id, state)
    product = current_db.get_product(product_id)

    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return

    await state.update_data(motivation_product_id=product_id, motivation_product_name=product[1])

    commission_info = current_db.get_product_motivation(product_id)
    current_text = ""
    if commission_info:
        if commission_info['motivation_type'] == 'percentage':
            current_text = f"\n📈 <i>Текущая: {commission_info['motivation_value']}% от продажи</i>"
        else:
            current_text = f"\n💰 <i>Текущая: {format_price(commission_info['motivation_value'])} за единицу</i>"

    # История изменений
    history = current_db.get_motivation_history(product_id, limit=3)
    history_text = ""
    if history:
        history_text = "\n\n📜 <b>Последние изменения:</b>\n"
        for h_type, h_val, h_at, h_fn, h_ln in history:
            h_label = f"{h_val}%" if h_type == 'percentage' else f"{format_price(h_val)}/шт"
            h_who = f"{h_fn or ''} {h_ln or ''}".strip() or "—"
            h_date = h_at[:10] if h_at else "—"
            history_text += f"  • {h_label} · {he(h_who)} · {h_date}\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Процент от продажи", callback_data="comm_type_percentage")
    builder.button(text="💰 Фиксированная сумма", callback_data="comm_type_fixed")
    category = data_cat = (await state.get_data()).get('motiv_category', '')
    back_cb = safe_cb("motiv_cat_", category) if category else "set_motivation"
    builder.button(text="⬅️ Назад", callback_data=back_cb)
    builder.adjust(1)

    await callback.message.edit_text(
        f"📝 <b>Установка мотивации — шаг 3/3</b>\n\n"
        f"📦 Товар: <b>{he(product[1])}</b>\n"
        f"📂 Категория: {he(product[2])}\n"
        f"💵 Цена: {format_price(product[3])}{current_text}{history_text}\n\n"
        "Выберите тип мотивации:",
        reply_markup=builder.as_markup(), parse_mode="HTML",
    )
    await callback.answer()

@commission_router.callback_query(F.data.startswith("comm_type_"))
async def set_motivation_type_selected(callback: CallbackQuery, state: FSMContext):
    """Установка типа мотивации и запрос значения"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    motivation_type = callback.data.split("_")[-1]
    data = await state.get_data()
    
    await state.update_data(motivation_type=motivation_type, anchor_msg_id=callback.message.message_id)
    await state.set_state(MotivationStates.waiting_for_motivation_value)

    if motivation_type == "percentage":
        prompt_text = (
            f"📝 <b>Процентная мотивация</b>\n\n"
            f"Товар: {data['motivation_product_name']}\n\n"
            "Введите процент мотивации (от 0.1 до 50):\n\n"
            "Пример: если введете <code>5</code>, то с продажи на 1000₽ "
            "продавец получит 50₽ мотивации"
        )
    else:
        prompt_text = (
            f"💰 <b>Фиксированная мотивация</b>\n\n"
            f"Товар: {data['motivation_product_name']}\n\n"
            "Введите фиксированную сумму мотивации за единицу товара (в рублях):\n\n"
            "Пример: если введете <code>25</code>, то за каждую проданную единицу "
            "продавец получит 25₽"
        )

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="set_motivation")

    await callback.message.edit_text(prompt_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@commission_router.message(MotivationStates.waiting_for_motivation_value)
async def process_motivation_value(message: Message, state: FSMContext):
    """Обработка введенного значения мотивации"""
    try:
        value = float(message.text.replace(',', '.'))
        data = await state.get_data()
        
        _cancel_kb = InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="set_motivation").as_markup()
        if data['motivation_type'] == 'percentage':
            if value <= 0 or value > 50:
                await fsm_edit(state, message,
                               "❌ <b>Неверное значение</b>\n\nПроцент должен быть от 0.1 до 50",
                               reply_markup=_cancel_kb)
                return
        else:
            if value <= 0:
                await fsm_edit(state, message,
                               "❌ <b>Неверное значение</b>\n\nСумма должна быть больше 0",
                               reply_markup=_cancel_kb)
                return

        current_db = await get_db(message.from_user.id, state)
        success = current_db.set_product_motivation(
            data['motivation_product_id'],
            data['motivation_type'],
            value,
            message.from_user.id
        )

        if success:
            if data['motivation_type'] == 'percentage':
                commission_text = f"{value}% от продажи"
            else:
                commission_text = f"{format_price(value)} за единицу"

            await fsm_edit(
                state, message,
                f"✅ <b>Мотивация установлена!</b>\n\n"
                f"📦 Товар: {data['motivation_product_name']}\n"
                f"💰 Мотивация: {commission_text}",
                reply_markup=InlineKeyboardBuilder().button(
                    text="📝 Установить еще мотивацию", callback_data="set_motivation"
                ).button(
                    text="⬅️ В меню", callback_data="admin_motivation"
                ).adjust(1).as_markup(),
            )
        else:
            await fsm_edit(state, message, "❌ Ошибка при сохранении мотивации", reply_markup=_cancel_kb)
        
        await clear_state_keep_org(state)

    except ValueError:
        await fsm_edit(state, message,
                       "❌ <b>Неверный формат</b>\n\nВведите число. Используйте точку или запятую для разделения дробной части.",
                       reply_markup=InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="set_motivation").as_markup())

@commission_router.callback_query(F.data == "view_all_motivations")
async def view_all_motivations(callback: CallbackQuery, state: FSMContext):
    """Просмотр всех установленных комиссий"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    commissions = current_db.get_all_product_motivations()
    
    if not commissions:
        await callback.message.edit_text(
            "📊 <b>Мотивации по товарам</b>\n\n"
            "❌ Мотивации не установлены",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    text = "📊 <b>Установленные мотивации</b>\n\n"
    
    for commission in commissions:
        product_id, product_name, comm_type, comm_value, admin_first, admin_last, created_at = commission
        
        if comm_type and comm_value:
            if comm_type == 'percentage':
                comm_text = f"{comm_value}%"
            else:
                comm_text = f"{format_price(comm_value)}/шт"
            
            admin_name = f"{admin_first} {admin_last}" if admin_first and admin_first != 'None' else "Неизвестно"
            text += f"🔹 <b>{he(product_name)}</b>\n💰 {comm_text} | 👤 {he(admin_name)}\n\n"
        else:
            text += f"🔸 <b>{he(product_name)}</b>\n💰 Мотивация не установлена\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Установить мотивацию", callback_data="set_motivation")
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@commission_router.callback_query(F.data == "remove_motivation")
async def remove_motivation_start(callback: CallbackQuery, state: FSMContext):
    """Начало удаления мотивации - выбор товара"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    commissions = current_db.get_all_product_motivations()
    products_with_commission = [c for c in commissions if c[2] is not None]
    
    if not products_with_commission:
        await callback.message.edit_text(
            "🗑️ <b>Удаление комиссий</b>\n\n"
            "❌ Нет товаров с установленными мотивациями",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for commission in products_with_commission:
        product_id, product_name, comm_type, comm_value = commission[:4]
        
        if comm_type == 'percentage':
            comm_text = f" ({comm_value}%)"
        else:
            comm_text = f" ({format_price(comm_value)})"
        
        builder.button(
            text=f"🗑️ {product_name}{comm_text}",
            callback_data=f"remove_motiv_{product_id}"
        )
    
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")
    builder.adjust(1)

    await callback.message.edit_text(
        "🗑️ <b>Удаление мотивации</b>\n\n"
        "Выберите товар для удаления мотивации:",
        reply_markup=builder.as_markup(), parse_mode="HTML",
    )
    await callback.answer()

@commission_router.callback_query(F.data.startswith("remove_motiv_"))
async def remove_motivation_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[-1])
    current_db = await get_db(callback.from_user.id, state)
    product = current_db.get_product(product_id)
    commission_info = current_db.get_product_motivation(product_id)
    
    if not product or not commission_info:
        await callback.answer("❌ Товар или мотивация не найдены", show_alert=True)
        return

    if commission_info['motivation_type'] == 'percentage':
        comm_text = f"{commission_info['motivation_value']}%"
    else:
        comm_text = f"{format_price(commission_info['motivation_value'])}/шт"

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"confirm_remove_motiv_{product_id}")
    builder.button(text="❌ Отмена", callback_data="remove_motivation")
    builder.adjust(1)

    await callback.message.edit_text(
        f"🗑️ <b>Подтверждение удаления</b>\n\n"
        f"📦 Товар: {product[1]}\n"
        f"💰 Мотивация: {comm_text}\n\n"
        "⚠️ Это действие необратимо. Продолжить?",
        reply_markup=builder.as_markup(), parse_mode="HTML",
    )
    await callback.answer()

@commission_router.callback_query(F.data.startswith("confirm_remove_motiv_"))
async def remove_motivation_final(callback: CallbackQuery, state: FSMContext):
    """Окончательное удаление мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[-1])
    current_db = await get_db(callback.from_user.id, state)
    product = current_db.get_product(product_id)
    
    success = current_db.remove_product_motivation(product_id)
    
    if success:
        await callback.message.edit_text(
            f"✅ <b>Мотивация удалена</b>\n\n"
            f"📦 Товар: {product[1]}\n"
            "Мотивация больше не будет начисляться за продажи этого товара.",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ В меню", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при удалении мотивации</b>",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
    
    await callback.answer()

@commission_router.callback_query(F.data == "top_sellers")
async def show_top_sellers(callback: CallbackQuery, state: FSMContext):
    """Показать топ продавцов по заработку"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    top_sellers = current_db.get_top_sellers_by_earnings(limit=10)
    
    if not top_sellers:
        await callback.message.edit_text(
            "📈 <b>Топ продавцов по заработку</b>\n\n"
            "❌ Данных пока нет",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    text = "📈 <b>Топ продавцов по заработку</b>\n\n"
    
    for i, seller in enumerate(top_sellers, 1):
        first_name, last_name, shop_name, total_earnings, total_sales = seller
        
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        
        text += (
            f"{medal} <b>{he(first_name)} {he(last_name)}</b>\n"
            f"🏪 {he(shop_name)}\n"
            f"💰 {format_price(total_earnings)}₽ | 📦 {total_sales} продаж\n\n"
        )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


# ─────────────────────────────────────────────────────────────
# ДОПОЛНИТЕЛЬНЫЕ УСЛОВИЯ МОТИВАЦИИ
# ─────────────────────────────────────────────────────────────

@commission_router.callback_query(F.data == "motivation_extra")
async def motivation_extra_menu(callback: CallbackQuery, state: FSMContext):
    """Меню доп. условий мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="📉 Коэффициент смены", callback_data="add_coeff_condition")
    builder.button(text="🔒 Фильтр категорий", callback_data="add_category_filter")
    builder.button(text="📋 Просмотр условий", callback_data="view_extra_conditions")
    builder.button(text="🗑 Удалить условие", callback_data="del_extra_start")
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")
    builder.adjust(1)

    await callback.message.edit_text(
        "⚙️ <b>Доп. условия мотивации</b>\n\n"
        "Надстройки поверх базовых мотиваций:\n\n"
        "📉 <b>Коэффициент смены</b> — если в магазине работают N и более продавцов "
        "в текущем месяце, каждый получает мотивацию × коэффициент (напр. ×0.7)\n\n"
        "🔒 <b>Фильтр категорий</b> — продавец получает комиссию только "
        "с определённых категорий товаров",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Коэффициент смены ──────────────────────────────────────

@commission_router.callback_query(F.data == "add_coeff_condition")
async def add_coeff_condition(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина для коэффициента смены"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    shops = current_db.get_all_shops()

    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Все магазины", callback_data="coeff_sh_all")
    for shop in shops:
        builder.button(text=f"🏪 {shop}", callback_data=safe_cb("coeff_sh_", shop))
    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)

    await callback.message.edit_text(
        "📉 <b>Коэффициент смены — шаг 1/3</b>\n\n"
        "Выберите магазин, для которого будет действовать коэффициент:\n\n"
        "• <b>Все магазины</b> — применять ко всем\n"
        "• Конкретный магазин — применять только к нему",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data.startswith("coeff_sh_"))
async def coeff_shop_selected(callback: CallbackQuery, state: FSMContext):
    """Магазин выбран, запрашиваем минимальное кол-во продавцов"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    raw = callback.data[len("coeff_sh_"):]
    if raw == "all":
        shop_name = None
        shop_label = "Все магазины"
    else:
        current_db = await get_db(callback.from_user.id, state)
        shops = current_db.get_all_shops()
        shop_name = resolve_cb_name(raw, shops)
        shop_label = shop_name or raw

    await state.update_data(coeff_shop=shop_name, coeff_shop_label=shop_label,
                             anchor_msg_id=callback.message.message_id)
    await state.set_state(ExtraConditionStates.entering_min_sellers)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="motivation_extra")

    await callback.message.edit_text(
        f"📉 <b>Коэффициент смены — шаг 2/3</b>\n\n"
        f"🏪 Магазин: <b>{he(shop_label)}</b>\n\n"
        "Введите минимальное количество продавцов, при котором включается коэффициент:\n\n"
        "Пример: <code>2</code> — коэффициент применяется когда в магазине 2 и более "
        "продавцов работали в текущем месяце",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.message(ExtraConditionStates.entering_min_sellers)
async def process_min_sellers(message: Message, state: FSMContext):
    """Ввод минимального количества продавцов"""
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="motivation_extra"
    ).as_markup()
    try:
        min_s = int(message.text.strip())
        if min_s < 2 or min_s > 50:
            await fsm_edit(state, message,
                           "❌ <b>Неверное значение</b>\n\nКоличество должно быть от 2 до 50",
                           reply_markup=cancel_kb)
            return
    except ValueError:
        await fsm_edit(state, message,
                       "❌ <b>Неверный формат</b>\n\nВведите целое число (например: <code>2</code>)",
                       reply_markup=cancel_kb)
        return

    data = await state.get_data()
    await state.update_data(coeff_min_sellers=min_s)
    await state.set_state(ExtraConditionStates.selecting_calc_mode)

    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Раздельный", callback_data="coeff_mode_individual")
    builder.button(text="🤝 Совместный", callback_data="coeff_mode_joint")
    builder.button(text="❌ Отмена", callback_data="motivation_extra")
    builder.adjust(2, 1)

    await fsm_edit(
        state, message,
        f"📉 <b>Коэффициент смены — шаг 3/4</b>\n\n"
        f"🏪 Магазин: <b>{he(data['coeff_shop_label'])}</b>\n"
        f"👥 Порог: {min_s}+ продавцов\n\n"
        "<b>Режим расчёта мотивации:</b>\n\n"
        "👤 <b>Раздельный</b> — каждый получает мотивацию от своих продаж × коэффициент\n"
        "   Пример: А продал 6 ед. → 6×500×0.7 = 2100₽\n\n"
        "🤝 <b>Совместный</b> — каждый получает мотивацию от суммарных продаж всей смены × коэффициент\n"
        "   Пример: А=6, Б=4, итого 10 ед. → 10×500×0.7 = 3500₽ каждому",
        reply_markup=builder.as_markup()
    )


@commission_router.callback_query(
    ExtraConditionStates.selecting_calc_mode,
    F.data.in_({"coeff_mode_individual", "coeff_mode_joint"})
)
async def coeff_calc_mode_selected(callback: CallbackQuery, state: FSMContext):
    """Режим расчёта выбран — переходим к вводу коэффициента"""
    calc_mode = "joint" if callback.data == "coeff_mode_joint" else "individual"
    mode_label = "🤝 Совместный" if calc_mode == "joint" else "👤 Раздельный"
    await state.update_data(coeff_calc_mode=calc_mode)
    await state.set_state(ExtraConditionStates.entering_coefficient)

    data = await state.get_data()
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="motivation_extra"
    ).as_markup()

    await callback.message.edit_text(
        f"📉 <b>Коэффициент смены — шаг 4/4</b>\n\n"
        f"🏪 Магазин: <b>{he(data['coeff_shop_label'])}</b>\n"
        f"👥 Порог: {data['coeff_min_sellers']}+ продавцов\n"
        f"📊 Режим: {mode_label}\n\n"
        "Введите коэффициент мотивации (от 0.01 до 0.99):\n\n"
        "Пример: <code>0.7</code> — итоговая сумма составит 70% от базовой мотивации",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await callback.answer()


@commission_router.message(ExtraConditionStates.entering_coefficient)
async def process_coefficient(message: Message, state: FSMContext):
    """Ввод коэффициента и сохранение условия"""
    cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="motivation_extra"
    ).as_markup()
    try:
        coeff = float(message.text.replace(',', '.'))
        if coeff <= 0.0 or coeff >= 1.0:
            await fsm_edit(state, message,
                           "❌ <b>Неверное значение</b>\n\nКоэффициент должен быть от 0.01 до 0.99",
                           reply_markup=cancel_kb)
            return
    except ValueError:
        await fsm_edit(state, message,
                       "❌ <b>Неверный формат</b>\n\nВведите число с точкой или запятой (например: <code>0.7</code>)",
                       reply_markup=cancel_kb)
        return

    data = await state.get_data()
    current_db = await get_db(message.from_user.id, state)

    shop_label = data.get('coeff_shop_label', 'Все магазины')
    min_s = data.get('coeff_min_sellers', 2)
    calc_mode = data.get('coeff_calc_mode', 'individual')
    mode_label = "🤝 Совместный" if calc_mode == "joint" else "👤 Раздельный"
    mode_suffix = " [совм.]" if calc_mode == "joint" else ""

    cond_id = current_db.add_extra_condition(
        condition_type='multi_seller_coeff',
        shop_name=data.get('coeff_shop'),
        min_sellers=min_s,
        coefficient=coeff,
        description=f"×{coeff} при {min_s}+ продавцах — {shop_label}{mode_suffix}",
        calc_mode=calc_mode,
    )

    if cond_id:
        joint_note = (
            f"\n\nВ совместном режиме каждый участник смены получает мотивацию "
            f"от суммарного оборота всех продавцов × {coeff}."
        ) if calc_mode == "joint" else (
            f"\n\nПри {min_s}+ продавцах каждый получает "
            f"{coeff * 100:.0f}% от своей базовой мотивации."
        )
        await fsm_edit(
            state, message,
            f"✅ <b>Коэффициент смены добавлен!</b>\n\n"
            f"🏪 Магазин: <b>{he(shop_label)}</b>\n"
            f"👥 Порог: {min_s}+ продавцов\n"
            f"📊 Режим: {mode_label}\n"
            f"📉 Коэффициент: ×{coeff}"
            f"{joint_note}",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Доп. условия", callback_data="motivation_extra"
            ).as_markup()
        )
    else:
        await fsm_edit(state, message, "❌ Ошибка при сохранении условия",
                       reply_markup=cancel_kb)
    await clear_state_keep_org(state)


# ── Фильтр категорий ──────────────────────────────────────

async def _render_category_selection(
        message: Message, username: str, categories: list, selected: list):
    """Отрисовка меню выбора категорий (переиспользуется для toggle)"""
    builder = InlineKeyboardBuilder()
    for cat in categories:
        icon = "✅" if cat in selected else "◻️"
        builder.button(text=f"{icon} {cat}", callback_data=safe_cb("ctg_", cat))
    builder.button(text="💾 Сохранить", callback_data="catfilt_confirm")
    builder.button(text="🔓 Разрешить все категории", callback_data="catfilt_allow_all")
    builder.button(text="⬅️ Назад", callback_data="add_category_filter")
    builder.adjust(1)

    selected_text = (
        "\n".join(f"• {c}" for c in selected) if selected else "— (все категории)"
    )
    await message.edit_text(
        f"🔒 <b>Фильтр категорий для:</b> {username}\n\n"
        "Отметьте категории, с которых продавец получает мотивацию.\n"
        "Остальные категории будут давать 0 комиссии.\n\n"
        f"<b>Выбрано:</b>\n{selected_text}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@commission_router.callback_query(F.data == "add_category_filter")
async def add_category_filter(callback: CallbackQuery, state: FSMContext):
    """Выбор продавца для фильтра категорий"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    all_users = current_db.get_all_users()
    sellers = [u for u in all_users
               if not env_manager.is_super_admin(u[1])]

    if not sellers:
        await callback.message.edit_text(
            "🔒 <b>Фильтр категорий</b>\n\n❌ Нет продавцов для настройки",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for u in sellers:
        uid, tg_id, fname, lname = u[0], u[1], u[2], u[3]
        shop = u[8] or ""
        label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
        builder.button(text=label, callback_data=f"catfilt_user_{uid}")
    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)

    await callback.message.edit_text(
        "🔒 <b>Фильтр категорий — выбор продавца</b>\n\n"
        "Выберите продавца для настройки ограничений по категориям:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data.startswith("catfilt_user_"))
async def catfilt_user_selected(callback: CallbackQuery, state: FSMContext):
    """Продавец выбран — показываем категории для выбора"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    import json as _json
    user_id = int(callback.data[len("catfilt_user_"):])
    current_db = await get_db(callback.from_user.id, state)

    all_users = current_db.get_all_users()
    user_row = next((u for u in all_users if u[0] == user_id), None)
    if not user_row:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    username = f"{user_row[2]} {user_row[3]}"
    categories = current_db.get_all_categories()

    if not categories:
        await callback.message.edit_text(
            "🔒 <b>Фильтр категорий</b>\n\n❌ Нет категорий товаров",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="add_category_filter"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    conditions = current_db.get_extra_conditions()
    existing = next((c for c in conditions
                     if c[1] == 'category_filter' and c[6] == user_id), None)
    preselected = []
    if existing and existing[7]:
        try:
            preselected = _json.loads(existing[7])
        except Exception:
            preselected = []

    await state.update_data(
        catfilt_user_id=user_id,
        catfilt_username=username,
        selected_categories=preselected,
        anchor_msg_id=callback.message.message_id
    )
    await state.set_state(ExtraConditionStates.selecting_categories)

    await _render_category_selection(callback.message, username, categories, preselected)
    await callback.answer()


@commission_router.callback_query(ExtraConditionStates.selecting_categories, F.data.startswith("ctg_"))
async def catfilt_toggle_category(callback: CallbackQuery, state: FSMContext):
    """Переключить категорию в фильтре"""
    raw = callback.data[len("ctg_"):]
    current_db = await get_db(callback.from_user.id, state)
    categories = current_db.get_all_categories()
    cat = resolve_cb_name(raw, categories)

    data = await state.get_data()
    selected = list(data.get('selected_categories', []))
    username = data.get('catfilt_username', '')

    if cat in selected:
        selected.remove(cat)
    else:
        selected.append(cat)

    await state.update_data(selected_categories=selected)
    await _render_category_selection(callback.message, username, categories, selected)
    await callback.answer()


@commission_router.callback_query(ExtraConditionStates.selecting_categories, F.data == "catfilt_confirm")
async def catfilt_confirm(callback: CallbackQuery, state: FSMContext):
    """Сохранить фильтр категорий"""
    data = await state.get_data()
    user_id = data.get('catfilt_user_id')
    username = data.get('catfilt_username', '')
    selected = data.get('selected_categories', [])
    current_db = await get_db(callback.from_user.id, state)

    if not selected:
        await callback.answer(
            "⚠️ Выберите хотя бы одну категорию или нажмите «Разрешить все»",
            show_alert=True
        )
        return

    conditions = current_db.get_extra_conditions()
    for c in conditions:
        if c[1] == 'category_filter' and c[6] == user_id:
            current_db.delete_extra_condition(c[0])

    cond_id = current_db.add_extra_condition(
        condition_type='category_filter',
        user_id=user_id,
        allowed_categories=selected,
        description=f"Фильтр категорий для {username}"
    )

    await clear_state_keep_org(state)

    if cond_id:
        cats_text = "\n".join(f"• {c}" for c in selected)
        await callback.message.edit_text(
            f"✅ <b>Фильтр категорий сохранён!</b>\n\n"
            f"👤 Продавец: <b>{he(username)}</b>\n\n"
            f"<b>Разрешённые категории ({len(selected)}):</b>\n{cats_text}",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Доп. условия", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка при сохранении фильтра",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )
    await callback.answer()


@commission_router.callback_query(ExtraConditionStates.selecting_categories, F.data == "catfilt_allow_all")
async def catfilt_allow_all(callback: CallbackQuery, state: FSMContext):
    """Снять все ограничения по категориям для продавца"""
    data = await state.get_data()
    user_id = data.get('catfilt_user_id')
    username = data.get('catfilt_username', '')
    current_db = await get_db(callback.from_user.id, state)

    conditions = current_db.get_extra_conditions()
    for c in conditions:
        if c[1] == 'category_filter' and c[6] == user_id:
            current_db.delete_extra_condition(c[0])

    await clear_state_keep_org(state)
    await callback.message.edit_text(
        f"✅ <b>Ограничения сняты</b>\n\n"
        f"👤 {username}\n\n"
        "Продавец теперь получает мотивацию со всех категорий товаров.",
        reply_markup=InlineKeyboardBuilder().button(
            text="⬅️ Доп. условия", callback_data="motivation_extra"
        ).as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ── Просмотр и удаление ───────────────────────────────────

@commission_router.callback_query(F.data == "view_extra_conditions")
async def view_extra_conditions(callback: CallbackQuery, state: FSMContext):
    """Просмотр всех доп. условий мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    import json as _json
    current_db = await get_db(callback.from_user.id, state)
    conditions = current_db.get_extra_conditions()

    if not conditions:
        await callback.message.edit_text(
            "⚙️ <b>Доп. условия мотивации</b>\n\n❌ Условий не добавлено",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    text = "⚙️ <b>Доп. условия мотивации</b>\n\n"

    coeff_list = [c for c in conditions if c[1] == 'multi_seller_coeff']
    filter_list = [c for c in conditions if c[1] == 'category_filter']

    if coeff_list:
        text += "📉 <b>Коэффициенты смены:</b>\n"
        for c in coeff_list:
            shop = c[3] or "Все магазины"
            calc_mode = c[12] if len(c) > 12 else "individual"
            mode_icon = "🤝" if calc_mode == "joint" else "👤"
            text += f"  • {shop}: {c[4]}+ продавцов → ×{c[5]} {mode_icon}\n"
        text += "\n"

    if filter_list:
        text += "🔒 <b>Фильтры категорий:</b>\n"
        for c in filter_list:
            fname = c[10] or ""
            lname = c[11] or ""
            uname = f"{fname} {lname}".strip() or f"user_id={c[6]}"
            allowed = []
            if c[7]:
                try:
                    allowed = _json.loads(c[7])
                except Exception:
                    pass
            cats_str = ", ".join(allowed) if allowed else "все"
            text += f"  • {uname}: {cats_str}\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить условие", callback_data="del_extra_start")
    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@commission_router.callback_query(F.data == "del_extra_start")
async def del_extra_start(callback: CallbackQuery, state: FSMContext):
    """Начало удаления доп. условия"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    import json as _json
    current_db = await get_db(callback.from_user.id, state)
    conditions = current_db.get_extra_conditions()

    if not conditions:
        await callback.message.edit_text(
            "🗑 <b>Удаление условий</b>\n\n❌ Нет условий для удаления",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for c in conditions:
        cond_id = c[0]
        if c[1] == 'multi_seller_coeff':
            shop = c[3] or "Все магазины"
            label = f"📉 {shop}: {c[4]}+ → ×{c[5]}"
        else:
            fname = c[10] or ""
            lname = c[11] or ""
            uname = f"{fname} {lname}".strip() or f"id={c[6]}"
            allowed = []
            if c[7]:
                try:
                    allowed = _json.loads(c[7])
                except Exception:
                    pass
            cats = ", ".join(allowed[:2]) + ("..." if len(allowed) > 2 else "") if allowed else "все"
            label = f"🔒 {uname}: {cats}"
        builder.button(text=f"🗑 {label}", callback_data=f"xdel_{cond_id}")

    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)

    await callback.message.edit_text(
        "🗑 <b>Удаление доп. условия</b>\n\nВыберите условие для удаления:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data.startswith("xdel_"))
async def del_extra_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления доп. условия"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    cond_id = int(callback.data[len("xdel_"):])

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"xdok_{cond_id}")
    builder.button(text="❌ Отмена", callback_data="del_extra_start")
    builder.adjust(1)

    await callback.message.edit_text(
        f"🗑 <b>Подтверждение удаления</b>\n\n"
        f"Удалить условие #{cond_id}?\n"
        "⚠️ Это действие необратимо.",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data.startswith("xdok_"))
async def del_extra_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнение удаления доп. условия"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    cond_id = int(callback.data[len("xdok_"):])
    current_db = await get_db(callback.from_user.id, state)
    success = current_db.delete_extra_condition(cond_id)

    if success:
        await callback.message.edit_text(
            "✅ <b>Условие удалено</b>",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Доп. условия", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка при удалении или условие уже было удалено",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )
    await callback.answer()
