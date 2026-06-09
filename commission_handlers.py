"""
Обработчики для системы мотивации и комиссий продавцов
"""
import asyncio
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command

from database import Database
from keyboards import InlineKeyboardBuilder, safe_cb, resolve_cb_name, home_button
from env_manager import env_manager
from utils import format_price, he
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit
from states import SearchStates, MotivationScheduleStates
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_BTN

commission_router = Router()

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}
MONTH_NAMES_SHORT = {
    1: "Янв", 2: "Фев", 3: "Мар", 4: "Апр",
    5: "Май", 6: "Июн", 7: "Июл", 8: "Авг",
    9: "Сен", 10: "Окт", 11: "Ноя", 12: "Дек"
}

def _next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1

def _months_range(center_year, center_month, past=3, future=2):
    """Возвращает список (year, month) — past месяцев до центра + центр + future после."""
    from datetime import date
    result = []
    for delta in range(-past, future + 1):
        m = center_month + delta
        y = center_year
        while m <= 0:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        result.append((y, m))
    return result

class MotivationStates(StatesGroup):
    waiting_for_motivation_value = State()
    waiting_for_motivation_type = State()
    searching_product = State()
    waiting_for_cell_value = State()

class ExtraConditionStates(StatesGroup):
    entering_min_sellers = State()
    selecting_calc_mode = State()
    entering_coefficient = State()
    selecting_categories = State()

@commission_router.callback_query(F.data == "admin_motivation")
async def admin_motivation_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню управления мотивацией"""
    uid = callback.from_user.id
    if not is_any_admin(uid):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    if not env_manager.is_super_admin(uid):
        from subscription_utils import check_plans_motivation_permission
        if not check_plans_motivation_permission(uid):
            await callback.message.edit_text(
                "🔒 <b>Модуль «Планы и мотивация» не подключён</b>\n\n"
                "Управление мотивацией продавцов доступно при активном модуле <b>Планы и мотивация</b>.\n\n"
                "Подключите модуль в веб-кабинете:\n"
                "<b>Подписка → Модули → 📈 Планы и мотивация</b>",
                parse_mode="HTML"
            )
            await callback.answer()
            return

    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Установить мотивацию", callback_data="set_motivation")
    builder.button(text="📊 Просмотр всех мотиваций", callback_data="view_all_motivations")
    builder.button(text="📅 По месяцам", callback_data="view_motivation_schedule")
    builder.button(text="🗑️ Удалить мотивацию", callback_data="remove_motivation")
    builder.button(text="📈 Топ продавцов", callback_data="top_sellers")
    builder.button(text="⚙️ Доп. условия", callback_data="motivation_extra")
    builder.button(text="⬅️ Назад", callback_data="motivation_hub")
    builder.add(home_button())
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
    all_products = await current_db.get_all_products()

    if not all_products:
        await callback.message.edit_text(
            "❌ <b>Нет товаров</b>\n\nСначала добавьте товары в систему.",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    cat_counts: dict = {}
    for p in all_products:
        cat = p[2] or "Без категории"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    total_prods = len(all_products)
    total_cats  = len(cat_counts)

    builder = InlineKeyboardBuilder()
    for cat, count in sorted(cat_counts.items()):
        builder.button(text=f"📂 {cat} ({count})", callback_data=safe_cb("motiv_cat_", cat))
    builder.adjust(2)
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")
    builder.adjust(2, 1)

    await callback.message.edit_text(
        f"📝 <b>Установка мотивации — шаг 1/3</b>\n"
        f"Всего: <b>{total_prods} тов.</b> в <b>{total_cats} кат.</b>\n\n"
        "Выберите категорию:",
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
    categories = await current_db.get_all_categories()
    category = resolve_cb_name(raw, categories)

    products = await current_db.get_products_by_category(category)
    if not products:
        await callback.answer("❌ Нет товаров в этой категории", show_alert=True)
        return

    all_motivations = await current_db.get_all_product_motivations()
    motivations_map = {row[0]: {'motivation_type': row[2], 'motivation_value': row[3]}
                       for row in all_motivations if row[2]}

    await state.update_data(motiv_category=category,
                            motiv_products_cache=[p[0] for p in products])

    await _render_motiv_products(callback, state, category, products, motivations_map)
    await callback.answer()


async def _render_motiv_products(callback, state, category, products, motivations_map, search_query="", page=0):
    """Отрисовка списка товаров категории для установки мотивации (с поиском и пагинацией)"""
    from utils import format_price as fp
    filtered = products
    if search_query:
        q = search_query.lower()
        filtered = [p for p in products if q in p[1].lower()]

    page_prods, has_prev, has_next, total_pages, page = paginate(filtered, page, 10)

    builder = InlineKeyboardBuilder()
    if not search_query:
        builder.button(text="🔍 Найти товар", callback_data="motiv_prod_search")
    for p in page_prods:
        info = motivations_map.get(p[0])
        suffix = ""
        if info and info['motivation_type']:
            suffix = f" ({info['motivation_value']}%)" if info['motivation_type'] == 'percentage' \
                else f" ({fp(info['motivation_value'])})"
        builder.button(text=f"{p[1]}{suffix}", callback_data=f"set_motiv_product_{p[0]}")
    nav = page_nav_row("motiv_prod_page_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.button(text="⬅️ Назад к категориям", callback_data="set_motivation")
    builder.adjust(1)

    page_info = f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    extra = (f"\n🔍 Результаты для: «{search_query}» — {len(filtered)} шт." if filtered else f"\n🔍 По запросу «{search_query}» ничего не найдено, попробуйте другой запрос") if search_query else ""
    await callback.message.edit_text(
        f"📝 <b>Установка мотивации — шаг 2/3</b>{page_info}\n\n"
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
    products = await current_db.get_products_by_category(category)
    all_motivations = await current_db.get_all_product_motivations()
    motivations_map = {row[0]: {'motivation_type': row[2], 'motivation_value': row[3]}
                       for row in all_motivations if row[2]}
    await _render_motiv_products(callback, state, category, products, motivations_map)


@commission_router.callback_query(F.data.startswith("motiv_prod_page_"))
async def motiv_prod_page(callback: CallbackQuery, state: FSMContext):
    """Пагинация списка товаров при выборе мотивации (шаг 2/3)"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    try:
        page = int(callback.data.replace("motiv_prod_page_", ""))
    except ValueError:
        page = 0
    data = await state.get_data()
    category = data.get('motiv_category', '')
    current_db = await get_db(callback.from_user.id, state)
    products = await current_db.get_products_by_category(category)
    all_motivations = await current_db.get_all_product_motivations()
    motivations_map = {row[0]: {'motivation_type': row[2], 'motivation_value': row[3]}
                       for row in all_motivations if row[2]}
    await _render_motiv_products(callback, state, category, products, motivations_map, page=page)
    await callback.answer()


@commission_router.message(MotivationStates.searching_product)
async def motiv_prod_search_process(message: Message, state: FSMContext):
    """Обработка поискового запроса товара при установке мотивации"""
    query = message.text.strip()
    data = await state.get_data()
    category = data.get('motiv_category', '')
    await state.set_state(None)
    from message_utils import fsm_edit as _fe
    current_db = await get_db(message.from_user.id, state)
    products = await current_db.get_products_by_category(category)
    all_motivations = await current_db.get_all_product_motivations()
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
    product = await current_db.get_product(product_id)

    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return

    await state.update_data(motivation_product_id=product_id, motivation_product_name=product[1])

    commission_info = await current_db.get_product_motivation(product_id)
    current_text = ""
    if commission_info:
        if commission_info['motivation_type'] == 'percentage':
            current_text = f"\n📈 <i>Текущая: {commission_info['motivation_value']}% от продажи</i>"
        else:
            current_text = f"\n💰 <i>Текущая: {format_price(commission_info['motivation_value'])} за единицу</i>"

    # История изменений
    history = await current_db.get_motivation_history(product_id, limit=3)
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
    """Обработка введенного значения мотивации — переходим к выбору месяца"""
    from datetime import date as _date
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

        await state.update_data(
            motivation_pending_value=value,
            motivation_pending_type=data['motivation_type']
        )
        await state.set_state(MotivationScheduleStates.selecting_month)

        today = _date.today()
        nxt_year, nxt_month = _next_month(today.year, today.month)
        cur_label = f"{MONTH_NAMES_RU[today.month]} {today.year}"
        nxt_label = f"{MONTH_NAMES_RU[nxt_month]} {nxt_year}"

        if data['motivation_type'] == 'percentage':
            commission_text = f"{value}% от продажи"
        else:
            commission_text = f"{format_price(value)} за единицу"

        builder = InlineKeyboardBuilder()
        builder.button(text=f"📅 Текущий ({cur_label})", callback_data="motiv_month_cur")
        builder.button(text=f"⏭ Следующий ({nxt_label})", callback_data="motiv_month_next")
        builder.button(text="📆 Выбрать месяц", callback_data="motiv_month_pick")
        builder.button(text="❌ Отмена", callback_data="set_motivation")
        builder.adjust(1)

        await fsm_edit(
            state, message,
            f"📅 <b>На какой месяц применить?</b>\n\n"
            f"📦 Товар: <b>{he(data['motivation_product_name'])}</b>\n"
            f"💰 Мотивация: <b>{commission_text}</b>",
            reply_markup=builder.as_markup(), parse_mode="HTML"
        )

    except ValueError:
        await fsm_edit(state, message,
                       "❌ <b>Неверный формат</b>\n\nВведите число. Используйте точку или запятую для разделения дробной части.",
                       reply_markup=InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="set_motivation").as_markup())


async def _apply_product_motivation_month(callback, state, year, month, is_current=False):
    """Сохранить мотивацию товара на указанный месяц и показать результат."""
    from datetime import date as _date
    data = await state.get_data()
    product_id = data['motivation_product_id']
    product_name = data['motivation_product_name']
    mtype = data['motivation_pending_type']
    mvalue = data['motivation_pending_value']

    current_db = await get_db(callback.from_user.id, state)
    ok = await current_db.set_motivation_for_month(product_id, year, month, mtype, mvalue, callback.from_user.id)

    today = _date.today()
    is_past_or_current = (year < today.year) or (year == today.year and month <= today.month)
    recalc_note = ""
    if ok and is_past_or_current:
        await current_db.recalculate_month_earnings(product_id, year, month)
        recalc_note = "\n\nЗаработки за этот месяц пересчитаны."

    await clear_state_keep_org(state)

    month_label = f"{MONTH_NAMES_RU[month]} {year}"
    commission_text = f"{mvalue}% от продажи" if mtype == 'percentage' else f"{format_price(mvalue)} за единицу"

    await callback.message.edit_text(
        f"✅ <b>Мотивация установлена!</b>\n\n"
        f"📦 Товар: {he(product_name)}\n"
        f"📅 Месяц: {month_label}\n"
        f"💰 Мотивация: {commission_text}{recalc_note}",
        reply_markup=InlineKeyboardBuilder().button(
            text="📝 Установить ещё", callback_data="set_motivation"
        ).button(
            text="⬅️ В меню", callback_data="admin_motivation"
        ).adjust(1).as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(MotivationScheduleStates.selecting_month, F.data == "motiv_month_cur")
async def motiv_month_cur(callback: CallbackQuery, state: FSMContext):
    """Применить мотивацию к текущему месяцу"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    await _apply_product_motivation_month(callback, state, today.year, today.month, is_current=True)


@commission_router.callback_query(MotivationScheduleStates.selecting_month, F.data == "motiv_month_next")
async def motiv_month_next_handler(callback: CallbackQuery, state: FSMContext):
    """Применить мотивацию к следующему месяцу"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    ny, nm = _next_month(today.year, today.month)
    await _apply_product_motivation_month(callback, state, ny, nm)


@commission_router.callback_query(MotivationScheduleStates.selecting_month, F.data == "motiv_month_pick")
async def motiv_month_pick(callback: CallbackQuery, state: FSMContext):
    """Показать пикер месяца для мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    months = _months_range(today.year, today.month, past=3, future=2)
    builder = InlineKeyboardBuilder()
    for y, m in months:
        builder.button(text=f"{MONTH_NAMES_SHORT[m]} {y}", callback_data=f"motiv_ym_{y}_{m}")
    builder.button(text="⬅️ Назад", callback_data="motiv_month_back")
    builder.adjust(3)
    await callback.message.edit_text(
        "📆 <b>Выберите месяц для применения мотивации:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(MotivationScheduleStates.selecting_month, F.data == "motiv_month_back")
async def motiv_month_back(callback: CallbackQuery, state: FSMContext):
    """Вернуться к выбору месяца (с пикера)"""
    from datetime import date as _date
    today = _date.today()
    nxt_year, nxt_month = _next_month(today.year, today.month)
    cur_label = f"{MONTH_NAMES_RU[today.month]} {today.year}"
    nxt_label = f"{MONTH_NAMES_RU[nxt_month]} {nxt_year}"
    data = await state.get_data()
    mtype = data.get('motivation_pending_type', 'percentage')
    mvalue = data.get('motivation_pending_value', 0)
    commission_text = f"{mvalue}% от продажи" if mtype == 'percentage' else f"{format_price(mvalue)} за единицу"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📅 Текущий ({cur_label})", callback_data="motiv_month_cur")
    builder.button(text=f"⏭ Следующий ({nxt_label})", callback_data="motiv_month_next")
    builder.button(text="📆 Выбрать месяц", callback_data="motiv_month_pick")
    builder.button(text="❌ Отмена", callback_data="set_motivation")
    builder.adjust(1)
    await callback.message.edit_text(
        f"📅 <b>На какой месяц применить?</b>\n\n"
        f"📦 Товар: <b>{he(data.get('motivation_product_name', ''))}</b>\n"
        f"💰 Мотивация: <b>{commission_text}</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(MotivationScheduleStates.selecting_month, F.data.startswith("motiv_ym_"))
async def motiv_ym_selected(callback: CallbackQuery, state: FSMContext):
    """Выбран конкретный месяц для мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    await _apply_product_motivation_month(callback, state, year, month)

def _build_motiv_view_text_markup(cat_comms: dict, total: int, page: int):
    """Строит текст + разметку для страницы просмотра мотиваций (плоская пагинация по товарам)."""
    flat = []
    for cat_name in sorted(cat_comms.keys()):
        for item in cat_comms[cat_name]:
            flat.append((cat_name, *item))

    page_items, has_prev, has_next, total_pages, page = paginate(flat, page, 10)

    text = f"📊 <b>Установленные мотивации</b> · {total} тов."
    if total_pages > 1:
        text += f" · стр. {page + 1}/{total_pages}"
    text += "\n\n"

    cur_cat = None
    for item in page_items:
        cat_name, product_name, comm_type, comm_value, admin_first, admin_last = item
        if cat_name != cur_cat:
            if cur_cat is not None:
                text += "\n"
            text += f"📂 <b>{he(cat_name)}</b>\n"
            cur_cat = cat_name
        comm_text = f"{comm_value}%" if comm_type == 'percentage' else f"{format_price(comm_value)}/шт"
        admin_name = f"{admin_first} {admin_last}".strip() if admin_first and admin_first != 'None' else "—"
        text += f"  🔹 {he(product_name)} · 💰 {comm_text} · 👤 {he(admin_name)}\n"

    builder = InlineKeyboardBuilder()
    nav = page_nav_row("view_all_motiv_p", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.button(text="📝 Установить мотивацию", callback_data="set_motivation")
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")
    builder.adjust(1)
    return text, builder.as_markup()


@commission_router.callback_query(F.data == "view_all_motivations")
async def view_all_motivations(callback: CallbackQuery, state: FSMContext):
    """Просмотр всех установленных комиссий"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    commissions, all_products = await asyncio.gather(
        current_db.get_all_product_motivations(),
        current_db.get_all_products()
    )
    products_with_comm = [c for c in commissions if c[2] is not None]

    if not products_with_comm:
        await callback.message.edit_text(
            "📊 <b>Мотивации по товарам</b>\n\n"
            "❌ Мотивации не установлены",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    prod_cat_map = {p[0]: (p[2] or "Без категории") for p in all_products}
    cat_comms: dict = {}
    for commission in products_with_comm:
        product_id, product_name, comm_type, comm_value, admin_first, admin_last, created_at = commission
        cat = prod_cat_map.get(product_id, "Без категории")
        cat_comms.setdefault(cat, []).append((product_name, comm_type, comm_value, admin_first, admin_last))

    text, markup = _build_motiv_view_text_markup(cat_comms, len(products_with_comm), 0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@commission_router.callback_query(F.data.startswith("view_all_motiv_p"))
async def view_all_motivations_page(callback: CallbackQuery, state: FSMContext):
    """Пагинация списка мотиваций"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    try:
        page = int(callback.data.replace("view_all_motiv_p", ""))
    except ValueError:
        page = 0

    current_db = await get_db(callback.from_user.id, state)
    commissions, all_products = await asyncio.gather(
        current_db.get_all_product_motivations(),
        current_db.get_all_products()
    )
    products_with_comm = [c for c in commissions if c[2] is not None]

    prod_cat_map = {p[0]: (p[2] or "Без категории") for p in all_products}
    cat_comms: dict = {}
    for commission in products_with_comm:
        product_id, product_name, comm_type, comm_value, admin_first, admin_last, created_at = commission
        cat = prod_cat_map.get(product_id, "Без категории")
        cat_comms.setdefault(cat, []).append((product_name, comm_type, comm_value, admin_first, admin_last))

    text, markup = _build_motiv_view_text_markup(cat_comms, len(products_with_comm), page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()

@commission_router.callback_query(F.data == "remove_motivation")
async def remove_motivation_start(callback: CallbackQuery, state: FSMContext):
    """Начало удаления мотивации — выбор категории"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    commissions, all_products = await asyncio.gather(
        current_db.get_all_product_motivations(),
        current_db.get_all_products()
    )
    products_with_commission = [c for c in commissions if c[2] is not None]

    if not products_with_commission:
        await callback.message.edit_text(
            "🗑️ <b>Удаление мотивации</b>\n\n"
            "❌ Нет товаров с установленными мотивациями",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="admin_motivation"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    # Группируем по категориям
    prod_cat_map = {p[0]: (p[2] or "Без категории") for p in all_products}
    cat_products: dict = {}
    for commission in products_with_commission:
        product_id, product_name, comm_type, comm_value = commission[:4]
        cat = prod_cat_map.get(product_id, "Без категории")
        cat_products.setdefault(cat, []).append((product_id, product_name, comm_type, comm_value))

    total = len(products_with_commission)
    if len(cat_products) == 1:
        # Только одна категория — сразу показываем товары
        cat_name = list(cat_products.keys())[0]
        await _show_remove_motiv_products(callback, cat_name, cat_products[cat_name])
    else:
        builder = InlineKeyboardBuilder()
        for cat_name, prods in sorted(cat_products.items()):
            builder.button(text=f"📂 {cat_name} ({len(prods)})", callback_data=safe_cb("remove_motiv_cat_", cat_name))
        builder.adjust(2)
        builder.button(text="⬅️ Назад", callback_data="admin_motivation")
        builder.adjust(2, 1)
        await callback.message.edit_text(
            f"🗑️ <b>Удаление мотивации</b>\n"
            f"Товаров с мотивацией: <b>{total}</b>\n\n"
            "Выберите категорию:",
            reply_markup=builder.as_markup(), parse_mode="HTML",
        )
    await callback.answer()


async def _show_remove_motiv_products(callback, cat_name: str, products: list):
    """Показать список товаров категории для удаления мотивации"""
    builder = InlineKeyboardBuilder()
    for product_id, product_name, comm_type, comm_value in products:
        comm_text = f" ({comm_value}%)" if comm_type == 'percentage' else f" ({format_price(comm_value)})"
        builder.button(
            text=f"🗑️ {product_name}{comm_text}",
            callback_data=f"remove_motiv_{product_id}"
        )
    builder.button(text="⬅️ Назад", callback_data="remove_motivation")
    builder.adjust(1)
    await callback.message.edit_text(
        f"🗑️ <b>Удаление мотивации</b>\n"
        f"📂 Категория: <b>{he(cat_name)}</b>\n\n"
        "Выберите товар для удаления:",
        reply_markup=builder.as_markup(), parse_mode="HTML",
    )


@commission_router.callback_query(F.data.startswith("remove_motiv_cat_"))
async def remove_motiv_cat_selected(callback: CallbackQuery, state: FSMContext):
    """Категория выбрана — показываем товары с мотивацией"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    raw = callback.data[len("remove_motiv_cat_"):]
    current_db = await get_db(callback.from_user.id, state)
    all_cats, commissions, all_products = await asyncio.gather(
        current_db.get_all_categories(),
        current_db.get_all_product_motivations(),
        current_db.get_all_products()
    )
    cat_name = resolve_cb_name(raw, all_cats)
    prod_cat_map = {p[0]: (p[2] or "Без категории") for p in all_products}
    products = [
        (c[0], c[1], c[2], c[3])
        for c in commissions
        if c[2] is not None and prod_cat_map.get(c[0], "Без категории") == cat_name
    ]
    if not products:
        await callback.answer("❌ Нет товаров с мотивацией в этой категории", show_alert=True)
        return
    await callback.answer()
    await _show_remove_motiv_products(callback, cat_name, products)

@commission_router.callback_query(F.data.startswith("remove_motiv_"))
async def remove_motivation_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[-1])
    current_db = await get_db(callback.from_user.id, state)
    product = await current_db.get_product(product_id)
    commission_info = await current_db.get_product_motivation(product_id)
    
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
    product = await current_db.get_product(product_id)
    
    success = await current_db.remove_product_motivation(product_id)
    
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
    top_sellers = await current_db.get_top_sellers_by_earnings(limit=10)
    
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

    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    plan_coeff_on = False
    plan_coeff_cap_on = True
    if user:
        ns = await current_db.get_notification_settings(user[0])
        plan_coeff_on = bool(ns.get('plan_coeff_enabled', False))
        plan_coeff_cap_on = bool(ns.get('plan_coeff_cap', True))

    builder = InlineKeyboardBuilder()
    builder.button(text="📉 Коэффициент смены", callback_data="add_coeff_condition")
    builder.button(text="🔒 Фильтр категорий", callback_data="add_category_filter")
    builder.button(text="📋 Просмотр условий", callback_data="view_extra_conditions")
    builder.button(text="🗑 Удалить условие", callback_data="del_extra_start")
    builder.button(
        text=f"📈 Коэф. выполнения плана {'✅' if plan_coeff_on else '❌'}",
        callback_data="motiv_toggle_plan_coeff"
    )
    if plan_coeff_on:
        builder.button(
            text=f"✂️ Обрезать до 100% {'✅' if plan_coeff_cap_on else '❌'}",
            callback_data="motiv_toggle_plan_coeff_cap"
        )
    builder.button(text="⬅️ Назад", callback_data="admin_motivation")
    builder.adjust(1)

    await callback.message.edit_text(
        "⚙️ <b>Доп. условия мотивации</b>\n\n"
        "Надстройки поверх базовых мотиваций:\n\n"
        "📉 <b>Коэффициент смены</b> — если в магазине работают N и более продавцов "
        "в текущем месяце, каждый получает мотивацию × коэффициент (напр. ×0.7)\n\n"
        "🔒 <b>Фильтр категорий</b> — продавец получает комиссию только "
        "с определённых категорий товаров\n\n"
        "📈 <b>Коэф. выполнения плана</b> — итоговая мотивация умножается "
        "на среднее % выполнения недельных планов",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data.in_(["motiv_toggle_plan_coeff", "motiv_toggle_plan_coeff_cap"]))
async def motiv_toggle_plan_coeff(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    user_id = user[0]
    ns = await current_db.get_notification_settings(user_id)
    if callback.data == "motiv_toggle_plan_coeff":
        new_val = not bool(ns.get('plan_coeff_enabled', False))
        await current_db.update_notification_settings(user_id, plan_coeff_enabled=new_val)
    else:
        new_val = not bool(ns.get('plan_coeff_cap', True))
        await current_db.update_notification_settings(user_id, plan_coeff_cap=new_val)
    await motivation_extra(callback, state)


# ── Коэффициент смены ──────────────────────────────────────

@commission_router.callback_query(F.data == "add_coeff_condition")
async def add_coeff_condition(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина для коэффициента смены"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()

    await _show_coeff_shop_list(callback, shops)
    await callback.answer()


async def _show_coeff_shop_list(callback, shops, query=""):
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти магазин", callback_data="coeff_srch_shop_start")
    builder.button(text="🌐 Все магазины", callback_data="coeff_sh_all")
    for shop in filtered:
        builder.button(text=f"🏪 {shop}", callback_data=safe_cb("coeff_sh_", shop))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="coeff_srch_shop_cancel")
    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await callback.message.edit_text(
        "📉 <b>Коэффициент смены — шаг 1/3</b>\n\n"
        "Выберите магазин, для которого будет действовать коэффициент:\n\n"
        "• <b>Все магазины</b> — применять ко всем\n"
        f"• Конкретный магазин — применять только к нему{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@commission_router.callback_query(F.data == "coeff_srch_shop_start")
async def coeff_srch_shop_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.shop_commission)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="coeff_srch_shop_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск магазина</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@commission_router.callback_query(F.data == "coeff_srch_shop_cancel")
async def coeff_srch_shop_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    await _show_coeff_shop_list(callback, shops)


@commission_router.message(SearchStates.shop_commission)
async def coeff_srch_shop_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    shops = await current_db.get_all_shops()
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти магазин", callback_data="coeff_srch_shop_start")
    builder.button(text="🌐 Все магазины", callback_data="coeff_sh_all")
    for shop in filtered:
        builder.button(text=f"🏪 {shop}", callback_data=safe_cb("coeff_sh_", shop))
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="coeff_srch_shop_cancel")
    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        "📉 <b>Коэффициент смены — шаг 1/3</b>\n\n"
        "Выберите магазин, для которого будет действовать коэффициент:\n\n"
        "• <b>Все магазины</b> — применять ко всем\n"
        f"• Конкретный магазин — применять только к нему{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


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
        shops = await current_db.get_all_shops()
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

    await state.update_data(
        coeff_pending_coeff=coeff,
        coeff_pending_desc=f"×{coeff} при {min_s}+ продавцах — {shop_label}{mode_suffix}",
        coeff_pending_mode=calc_mode,
        coeff_pending_mode_label=mode_label,
    )
    await state.set_state(MotivationScheduleStates.selecting_extra_month)
    await state.update_data(extra_cond_type='coeff')

    from datetime import date as _date
    today = _date.today()
    nxt_year, nxt_month = _next_month(today.year, today.month)
    cur_label = f"{MONTH_NAMES_RU[today.month]} {today.year}"
    nxt_label = f"{MONTH_NAMES_RU[nxt_month]} {nxt_year}"

    builder = InlineKeyboardBuilder()
    builder.button(text=f"📅 Текущий ({cur_label})", callback_data="extra_month_cur")
    builder.button(text=f"⏭ Следующий ({nxt_label})", callback_data="extra_month_next")
    builder.button(text="📆 Выбрать месяц", callback_data="extra_month_pick")
    builder.button(text="🌐 На все время (глобально)", callback_data="extra_month_global")
    builder.button(text="❌ Отмена", callback_data="motivation_extra")
    builder.adjust(1)

    await fsm_edit(
        state, message,
        f"📅 <b>На какой месяц применить коэффициент?</b>\n\n"
        f"🏪 Магазин: <b>{he(shop_label)}</b>\n"
        f"👥 Порог: {min_s}+ продавцов\n"
        f"📊 Режим: {mode_label}\n"
        f"📉 Коэффициент: ×{coeff}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


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
        f"🔒 <b>Фильтр категорий для:</b> {he(username)}\n\n"
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
    all_users = await current_db.get_all_users()
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

    await _show_catfilt_user_list(callback, sellers)
    await callback.answer()


async def _show_catfilt_user_list(callback, sellers, query=""):
    filtered = sellers
    if query:
        q = query.lower()
        filtered = [u for u in sellers
                    if q in f"{u[2]} {u[3]}".lower() or q in (u[8] or "").lower()]
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти сотрудника", callback_data="catfilt_srch_start")
    for u in filtered:
        uid, fname, lname = u[0], u[2], u[3]
        shop = u[8] or ""
        label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
        builder.button(text=label, callback_data=f"catfilt_user_{uid}")
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="catfilt_srch_cancel")
    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await callback.message.edit_text(
        "🔒 <b>Фильтр категорий — выбор продавца</b>\n\n"
        f"Выберите продавца для настройки ограничений по категориям:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@commission_router.callback_query(F.data == "catfilt_srch_start")
async def catfilt_srch_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.user_catfilt)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="catfilt_srch_cancel")
    await callback.message.edit_text(
        "🔍 <b>Поиск сотрудника</b>\n\nВведите имя или магазин:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@commission_router.callback_query(F.data == "catfilt_srch_cancel")
async def catfilt_srch_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    current_db = await get_db(callback.from_user.id, state)
    all_users = await current_db.get_all_users()
    sellers = [u for u in all_users if not env_manager.is_super_admin(u[1])]
    await _show_catfilt_user_list(callback, sellers)


@commission_router.message(SearchStates.user_catfilt)
async def catfilt_srch_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    all_users = await current_db.get_all_users()
    sellers = [u for u in all_users if not env_manager.is_super_admin(u[1])]
    q = query.lower()
    filtered = [u for u in sellers
                if q in f"{u[2]} {u[3]}".lower() or q in (u[8] or "").lower()] if query else sellers
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти сотрудника", callback_data="catfilt_srch_start")
    for u in filtered:
        uid, fname, lname = u[0], u[2], u[3]
        shop = u[8] or ""
        label = f"{fname} {lname}" + (f" ({shop})" if shop else "")
        builder.button(text=label, callback_data=f"catfilt_user_{uid}")
    if query:
        builder.button(text="✖️ Сбросить поиск", callback_data="catfilt_srch_cancel")
    builder.button(text="⬅️ Назад", callback_data="motivation_extra")
    builder.adjust(1)
    suffix = (f"\n\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered else f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос") if query else ""
    await fsm_edit(
        state, message,
        "🔒 <b>Фильтр категорий — выбор продавца</b>\n\n"
        f"Выберите продавца для настройки ограничений по категориям:{suffix}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@commission_router.callback_query(F.data.startswith("catfilt_user_"))
async def catfilt_user_selected(callback: CallbackQuery, state: FSMContext):
    """Продавец выбран — показываем категории для выбора"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    import json as _json
    user_id = int(callback.data[len("catfilt_user_"):])
    current_db = await get_db(callback.from_user.id, state)

    all_users = await current_db.get_all_users()
    user_row = next((u for u in all_users if u[0] == user_id), None)
    if not user_row:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    username = f"{user_row[2]} {user_row[3]}"
    categories = await current_db.get_all_categories()

    if not categories:
        await callback.message.edit_text(
            "🔒 <b>Фильтр категорий</b>\n\n❌ Нет категорий товаров",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Назад", callback_data="add_category_filter"
            ).as_markup(), parse_mode="HTML"
        )
        await callback.answer()
        return

    conditions = await current_db.get_extra_conditions()
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
    await callback.answer()
    raw = callback.data[len("ctg_"):]
    current_db = await get_db(callback.from_user.id, state)
    categories = await current_db.get_all_categories()
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

    await state.update_data(
        catfilt_pending_selected=selected,
        extra_cond_type='catfilt'
    )
    await state.set_state(MotivationScheduleStates.selecting_extra_month)

    from datetime import date as _date
    today = _date.today()
    nxt_year, nxt_month = _next_month(today.year, today.month)
    cur_label = f"{MONTH_NAMES_RU[today.month]} {today.year}"
    nxt_label = f"{MONTH_NAMES_RU[nxt_month]} {nxt_year}"
    cats_text = "\n".join(f"• {c}" for c in selected[:5])
    if len(selected) > 5:
        cats_text += f"\n  ...ещё {len(selected) - 5}"

    builder = InlineKeyboardBuilder()
    builder.button(text=f"📅 Текущий ({cur_label})", callback_data="extra_month_cur")
    builder.button(text=f"⏭ Следующий ({nxt_label})", callback_data="extra_month_next")
    builder.button(text="📆 Выбрать месяц", callback_data="extra_month_pick")
    builder.button(text="🌐 На все время (глобально)", callback_data="extra_month_global")
    builder.button(text="❌ Отмена", callback_data="motivation_extra")
    builder.adjust(1)

    await callback.message.edit_text(
        f"📅 <b>На какой месяц применить фильтр?</b>\n\n"
        f"👤 Продавец: <b>{he(username)}</b>\n\n"
        f"<b>Категории ({len(selected)}):</b>\n{cats_text}",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(ExtraConditionStates.selecting_categories, F.data == "catfilt_allow_all")
async def catfilt_allow_all(callback: CallbackQuery, state: FSMContext):
    """Снять все ограничения по категориям для продавца"""
    await callback.answer()
    data = await state.get_data()
    user_id = data.get('catfilt_user_id')
    username = data.get('catfilt_username', '')
    current_db = await get_db(callback.from_user.id, state)

    conditions = await current_db.get_extra_conditions()
    for c in conditions:
        if c[1] == 'category_filter' and c[6] == user_id:
            await current_db.delete_extra_condition(c[0])

    await current_db.recalculate_month_earnings(None)

    await clear_state_keep_org(state)
    await callback.message.edit_text(
        f"✅ <b>Ограничения сняты</b>\n\n"
        f"👤 {username}\n\n"
        "Продавец теперь получает мотивацию со всех категорий товаров.",
        reply_markup=InlineKeyboardBuilder().button(
            text="⬅️ Доп. условия", callback_data="motivation_extra"
        ).as_markup(), parse_mode="HTML"
    )


# ── Просмотр и удаление ───────────────────────────────────

@commission_router.callback_query(F.data == "view_extra_conditions")
async def view_extra_conditions(callback: CallbackQuery, state: FSMContext):
    """Просмотр всех доп. условий мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    import json as _json
    current_db = await get_db(callback.from_user.id, state)
    conditions = await current_db.get_extra_conditions()

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
    conditions = await current_db.get_extra_conditions()

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
    success = await current_db.delete_extra_condition(cond_id)

    if success:
        await current_db.recalculate_month_earnings(None)
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


# ─────────────────────────────────────────────────────────────
# ВЫБОР МЕСЯЦА ДЛЯ ДОП. УСЛОВИЙ
# ─────────────────────────────────────────────────────────────

async def _save_extra_condition_for_month(callback, state, year, month, is_global=False):
    """Сохранить доп. условие на конкретный месяц или глобально, показать результат."""
    from datetime import date as _date
    data = await state.get_data()
    cond_type = data.get('extra_cond_type', 'coeff')
    current_db = await get_db(callback.from_user.id, state)

    if cond_type == 'coeff':
        shop_name = data.get('coeff_shop')
        shop_label = data.get('coeff_shop_label', 'Все магазины')
        min_s = data.get('coeff_min_sellers', 2)
        coeff = data.get('coeff_pending_coeff')
        calc_mode = data.get('coeff_pending_mode', 'individual')
        mode_label = data.get('coeff_pending_mode_label', '👤 Раздельный')
        desc = data.get('coeff_pending_desc', '')

        if is_global:
            cond_id = await current_db.add_extra_condition(
                condition_type='multi_seller_coeff',
                shop_name=shop_name,
                min_sellers=min_s,
                coefficient=coeff,
                description=desc,
                calc_mode=calc_mode,
            )
            ok = bool(cond_id)
        else:
            ok = await current_db.set_extra_condition_for_month(
                condition_type='multi_seller_coeff', year=year, month=month,
                shop_name=shop_name, min_sellers=min_s, coefficient=coeff,
                description=desc, calc_mode=calc_mode,
                admin_telegram_id=callback.from_user.id,
            )

        today = _date.today()
        if ok:
            is_past_or_cur = is_global or (year < today.year) or (year == today.year and month <= today.month)
            if is_past_or_cur:
                recalc_year = today.year if is_global else year
                recalc_month = today.month if is_global else month
                await current_db.recalculate_month_earnings(None, recalc_year, recalc_month)

        await clear_state_keep_org(state)
        month_label = "Глобально (все периоды)" if is_global else f"{MONTH_NAMES_RU[month]} {year}"
        joint_note = (
            f"\n\nВ совместном режиме каждый получает мотивацию от суммарного оборота × {coeff}."
        ) if calc_mode == "joint" else (
            f"\n\nПри {min_s}+ продавцах каждый получает {coeff * 100:.0f}% от базовой мотивации."
        )
        await callback.message.edit_text(
            f"✅ <b>Коэффициент смены добавлен!</b>\n\n"
            f"🏪 Магазин: <b>{he(shop_label)}</b>\n"
            f"👥 Порог: {min_s}+ продавцов\n"
            f"📊 Режим: {mode_label}\n"
            f"📉 Коэффициент: ×{coeff}\n"
            f"📅 Период: {month_label}"
            f"{joint_note}",
            reply_markup=InlineKeyboardBuilder().button(
                text="⬅️ Доп. условия", callback_data="motivation_extra"
            ).as_markup(), parse_mode="HTML"
        )

    elif cond_type == 'catfilt':
        user_id = data.get('catfilt_user_id')
        username = data.get('catfilt_username', '')
        selected = data.get('catfilt_pending_selected', [])

        if is_global:
            conditions = await current_db.get_extra_conditions()
            for c in conditions:
                if c[1] == 'category_filter' and c[6] == user_id:
                    await current_db.delete_extra_condition(c[0])
            cond_id = await current_db.add_extra_condition(
                condition_type='category_filter',
                user_id=user_id,
                allowed_categories=selected,
                description=f"Фильтр категорий для {username}"
            )
            ok = bool(cond_id)
        else:
            ok = await current_db.set_extra_condition_for_month(
                condition_type='category_filter', year=year, month=month,
                user_id=user_id, allowed_categories=selected,
                description=f"Фильтр категорий для {username}",
                admin_telegram_id=callback.from_user.id,
            )

        today = _date.today()
        if ok:
            is_past_or_cur = is_global or (year < today.year) or (year == today.year and month <= today.month)
            if is_past_or_cur:
                recalc_year = today.year if is_global else year
                recalc_month = today.month if is_global else month
                await current_db.recalculate_month_earnings(None, recalc_year, recalc_month)

        await clear_state_keep_org(state)
        month_label = "Глобально (все периоды)" if is_global else f"{MONTH_NAMES_RU[month]} {year}"
        cats_text = "\n".join(f"• {c}" for c in selected)

        if ok:
            await callback.message.edit_text(
                f"✅ <b>Фильтр категорий сохранён!</b>\n\n"
                f"👤 Продавец: <b>{he(username)}</b>\n"
                f"📅 Период: {month_label}\n\n"
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


@commission_router.callback_query(MotivationScheduleStates.selecting_extra_month, F.data == "extra_month_cur")
async def extra_month_cur(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    await _save_extra_condition_for_month(callback, state, today.year, today.month)


@commission_router.callback_query(MotivationScheduleStates.selecting_extra_month, F.data == "extra_month_next")
async def extra_month_next_handler(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    ny, nm = _next_month(today.year, today.month)
    await _save_extra_condition_for_month(callback, state, ny, nm)


@commission_router.callback_query(MotivationScheduleStates.selecting_extra_month, F.data == "extra_month_global")
async def extra_month_global(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    await _save_extra_condition_for_month(callback, state, today.year, today.month, is_global=True)


@commission_router.callback_query(MotivationScheduleStates.selecting_extra_month, F.data == "extra_month_pick")
async def extra_month_pick(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    months = _months_range(today.year, today.month, past=3, future=2)
    builder = InlineKeyboardBuilder()
    for y, m in months:
        builder.button(text=f"{MONTH_NAMES_SHORT[m]} {y}", callback_data=f"extra_ym_{y}_{m}")
    builder.button(text="⬅️ Назад", callback_data="extra_month_back")
    builder.adjust(3)
    await callback.message.edit_text(
        "📆 <b>Выберите месяц для применения условия:</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(MotivationScheduleStates.selecting_extra_month, F.data == "extra_month_back")
async def extra_month_back(callback: CallbackQuery, state: FSMContext):
    """Вернуться к выбору месяца для доп. условия"""
    from datetime import date as _date
    today = _date.today()
    nxt_year, nxt_month = _next_month(today.year, today.month)
    cur_label = f"{MONTH_NAMES_RU[today.month]} {today.year}"
    nxt_label = f"{MONTH_NAMES_RU[nxt_month]} {nxt_year}"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📅 Текущий ({cur_label})", callback_data="extra_month_cur")
    builder.button(text=f"⏭ Следующий ({nxt_label})", callback_data="extra_month_next")
    builder.button(text="📆 Выбрать месяц", callback_data="extra_month_pick")
    builder.button(text="🌐 На все время (глобально)", callback_data="extra_month_global")
    builder.button(text="❌ Отмена", callback_data="motivation_extra")
    builder.adjust(1)
    await callback.message.edit_text(
        "📅 <b>На какой месяц применить условие?</b>",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(MotivationScheduleStates.selecting_extra_month, F.data.startswith("extra_ym_"))
async def extra_ym_selected(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    await _save_extra_condition_for_month(callback, state, year, month)


# ─────────────────────────────────────────────────────────────
# ПРОСМОТР РАСПИСАНИЯ МОТИВАЦИИ ПО МЕСЯЦАМ
# ─────────────────────────────────────────────────────────────

MATRIX_PRODS_PER_PAGE = 1  # 1 товар = максимум 5 кнопок месяцев, чисто и читаемо
MATRIX_COL_PAST = 3        # 3 прошлых + текущий + 1 будущий = 5 столбцов
MATRIX_COL_FUTURE = 1


@commission_router.callback_query(F.data == "view_motivation_schedule")
async def view_motivation_schedule(callback: CallbackQuery, state: FSMContext):
    """Матрица мотивации: строки = товары, столбцы = последние 6 + следующий"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    await _show_schedule_matrix(callback, state, page=0)


async def _show_schedule_matrix(callback, state, page=0):
    from datetime import date as _date
    today = _date.today()
    col_months = _months_range(today.year, today.month, past=MATRIX_COL_PAST, future=MATRIX_COL_FUTURE)

    current_db = await get_db(callback.from_user.id, state)
    prod_order, product_names, cell_data = await current_db.get_effective_motivation_matrix(col_months)

    if not prod_order:
        builder = InlineKeyboardBuilder()
        builder.button(text="📝 Установить мотивацию", callback_data="set_motivation")
        builder.button(text="📋 Архив по месяцам", callback_data="archive_months")
        builder.button(text="⬅️ Назад", callback_data="admin_motivation")
        builder.adjust(1)
        await callback.message.edit_text(
            "📅 <b>Мотивация по месяцам</b>\n\n"
            "❌ Нет товаров с мотивацией.\n\n"
            "Используйте «📝 Установить мотивацию», чтобы задать ставку товару.",
            reply_markup=builder.as_markup(), parse_mode="HTML"
        )
        return

    page_prods, has_prev, has_next, total_pages, page = paginate(prod_order, page, MATRIX_PRODS_PER_PAGE)

    # Header with page counter
    hdr = " ".join(f"{MONTH_NAMES_SHORT[m]}{str(y)[-2:]}" for y, m in col_months)
    page_str = f" · {page + 1}/{total_pages}" if total_pages > 1 else ""
    text = f"📅 <b>Мотивация по месяцам</b>{page_str}\n<code>{hdr}</code>\n\n"

    for prod_id in page_prods:
        name = product_names.get(prod_id, f"#{prod_id}")
        rate_parts = []
        for ym in col_months:
            cell = cell_data[prod_id].get(ym)
            if cell:
                mv = cell['value']
                mt = cell['type']
                sched_mark = "●" if cell['is_scheduled'] else "○"
                rate_parts.append(f"{sched_mark}{mv:.0f}{'%' if mt == 'percentage' else '₽'}")
            else:
                rate_parts.append("—")
        text += f"📦 <b>{he(name)}</b>\n"
        text += "<code>" + "  ".join(rate_parts) + "</code>\n\n"

    text += "<i>● расписание  ○ глобальная</i>"

    builder = InlineKeyboardBuilder()

    # Edit buttons: 1 product × N months → one compact row per product
    for prod_id in page_prods:
        month_row = []
        for yr, mo in col_months:
            mo_name = MONTH_NAMES_SHORT[mo]
            month_row.append(InlineKeyboardButton(
                text=mo_name,
                callback_data=f"sched_cell_{prod_id}_{yr}_{mo}"
            ))
        builder.row(*month_row)

    # Navigation row
    nav = page_nav_row("sched_page_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="📝 Установить мотивацию", callback_data="set_motivation"))
    builder.row(InlineKeyboardButton(text="📋 Архив по месяцам", callback_data="archive_months"))
    builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="admin_motivation"))

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@commission_router.callback_query(F.data.startswith("sched_page_"))
async def sched_page(callback: CallbackQuery, state: FSMContext):
    """Переключение страниц в матрице мотивации"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.split("_")[-1])
    await _show_schedule_matrix(callback, state, page=page)


@commission_router.callback_query(F.data.regexp(r"^sched_cell_\d+_\d{4}_\d+$"))
async def sched_cell_edit(callback: CallbackQuery, state: FSMContext):
    """Редактирование ячейки матрицы мотивации (товар × месяц)"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    parts = callback.data.split("_")
    prod_id = int(parts[2])
    year = int(parts[3])
    month = int(parts[4])

    current_db = await get_db(callback.from_user.id, state)
    # Получаем текущую ставку для этой ячейки (если есть)
    current_rate = await current_db.get_motivation_for_month(prod_id, year, month)

    # Определяем имя товара
    prod_rows = await current_db.get_all_motivation_schedules()
    prod_name = next((r[1] for r in prod_rows if r[0] == prod_id), f"Товар #{prod_id}")

    month_label = f"{MONTH_NAMES_RU[month]} {year}"
    cur_str = ""
    if current_rate:
        mt = current_rate['motivation_type']
        mv = current_rate['motivation_value']
        cur_str = f"\nТекущая: {mv}% от продажи" if mt == 'percentage' else f"\nТекущая: {format_price(mv)}/шт"
        cur_str += " (из расписания)" if current_rate.get('is_scheduled') else " (глобальная)"

    await state.update_data(
        motivation_product_id=prod_id,
        motivation_product_name=prod_name,
        motivation_type='percentage',  # will be updated by type selection if needed
        motivation_pending_year=year,
        motivation_pending_month=month,
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="💹 % от продажи", callback_data=f"sched_cell_pct_{prod_id}_{year}_{month}")
    builder.button(text="💰 Фикс. за штуку", callback_data=f"sched_cell_fix_{prod_id}_{year}_{month}")
    builder.button(text="⬅️ Назад", callback_data="view_motivation_schedule")
    builder.adjust(2, 1)

    await callback.message.edit_text(
        f"✏️ <b>Редактирование ячейки</b>\n\n"
        f"📦 Товар: <b>{he(prod_name)}</b>\n"
        f"📅 Месяц: <b>{month_label}</b>{cur_str}\n\n"
        f"Выберите тип мотивации:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data.startswith("sched_cell_pct_") | F.data.startswith("sched_cell_fix_"))
async def sched_cell_type_selected(callback: CallbackQuery, state: FSMContext):
    """Выбран тип мотивации для ячейки, ждём ввод значения"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    is_pct = callback.data.startswith("sched_cell_pct_")
    parts = callback.data.replace("sched_cell_pct_", "").replace("sched_cell_fix_", "").split("_")
    prod_id = int(parts[0])
    year = int(parts[1])
    month = int(parts[2])

    mtype = 'percentage' if is_pct else 'fixed'
    data = await state.get_data()
    prod_name = data.get('motivation_product_name', f"Товар #{prod_id}")

    await state.update_data(
        motivation_product_id=prod_id,
        motivation_product_name=prod_name,
        motivation_type=mtype,
        motivation_pending_year=year,
        motivation_pending_month=month,
    )
    await state.set_state(MotivationStates.waiting_for_cell_value)

    month_label = f"{MONTH_NAMES_RU[month]} {year}"
    hint = "Введите процент (от 0.1 до 50):" if is_pct else "Введите сумму за единицу (руб.):"
    await callback.message.edit_text(
        f"📝 <b>Введите значение мотивации</b>\n\n"
        f"📦 Товар: <b>{he(prod_name)}</b>\n"
        f"📅 Месяц: <b>{month_label}</b>\n\n"
        f"{hint}",
        reply_markup=InlineKeyboardBuilder().button(
            text="❌ Отмена", callback_data="view_motivation_schedule"
        ).as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data == "archive_months")
async def archive_months(callback: CallbackQuery, state: FSMContext):
    """Архив по месяцам — выбор месяца для просмотра"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    from datetime import date as _date
    today = _date.today()
    months = _months_range(today.year, today.month, past=5, future=1)
    builder = InlineKeyboardBuilder()
    for yr, mo in months:
        builder.button(
            text=f"{MONTH_NAMES_RU[mo]} {yr}",
            callback_data=f"archive_month_{yr}_{mo}"
        )
    builder.button(text="⬅️ По месяцам", callback_data="view_motivation_schedule")
    builder.adjust(2)
    await callback.message.edit_text(
        "📋 <b>Архив по месяцам</b>\n\nВыберите месяц для просмотра ставок и условий:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@commission_router.callback_query(F.data.startswith("archive_month_"))
async def archive_month_view(callback: CallbackQuery, state: FSMContext):
    """Детальный просмотр мотиваций и доп. условий за выбранный месяц.
    Показывает эффективную ставку для КАЖДОГО товара с мотивацией (расписание или глобальная)."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    import json as _json
    parts = callback.data.split("_")
    year, month = int(parts[2]), int(parts[3])
    month_label = f"{MONTH_NAMES_RU[month]} {year}"

    current_db = await get_db(callback.from_user.id, state)

    # Эффективные ставки для всех товаров за этот месяц (расписание + fallback глобальные)
    prod_order, product_names, cell_data = await current_db.get_effective_motivation_matrix([(year, month)])

    # Доп. условия за этот месяц
    conditions = await current_db.get_extra_conditions_for_month(year, month)
    # Глобальные доп. условия (для показа если нет месячных)
    global_conditions = await current_db.get_extra_conditions()

    text = f"📋 <b>Архив мотивации — {month_label}</b>\n\n"

    if prod_order:
        text += "📦 <b>Ставки товаров:</b>\n"
        for pid in prod_order:
            name = product_names.get(pid, f"#{pid}")
            cell = cell_data[pid].get((year, month))
            if cell:
                mt = cell['type']
                mv = cell['value']
                is_sched = cell['is_scheduled']
                val_str = f"{mv}%" if mt == 'percentage' else f"{format_price(mv)}/шт"
                src = "📅 расписание" if is_sched else "🌐 глобальная"
                text += f"  • {he(name)}: {val_str} ({src})\n"
        text += "\n"
    else:
        text += "📦 <i>Нет товаров с мотивацией</i>\n\n"

    def _format_conditions(conds, label_prefix=""):
        out = ""
        coeff_list = [c for c in conds if c[1] == 'multi_seller_coeff']
        filter_list = [c for c in conds if c[1] == 'category_filter']
        if coeff_list:
            out += f"📉 <b>Коэффициенты смены{label_prefix}:</b>\n"
            for c in coeff_list:
                shop = he(c[3]) if c[3] else "Все магазины"
                out += f"  • {shop}: {c[4]}+ → ×{c[5]}\n"
        if filter_list:
            out += f"🔒 <b>Фильтры категорий{label_prefix}:</b>\n"
            for c in filter_list:
                fname = c[10] or ""
                lname = c[11] or ""
                uname = f"{fname} {lname}".strip() or f"id={c[6]}"
                allowed = []
                if c[7]:
                    try:
                        allowed = _json.loads(c[7])
                    except Exception:
                        pass
                cats = ", ".join(he(x) for x in allowed[:3])
                if len(allowed) > 3:
                    cats += "…"
                if not cats:
                    cats = "все"
                out += f"  • {he(uname)}: {cats}\n"
        return out

    if conditions:
        text += _format_conditions(conditions, " (месяц)")
    if global_conditions:
        text += _format_conditions(global_conditions, " (глобальные)")
    if not conditions and not global_conditions:
        text += "⚙️ <i>Доп. условий нет</i>\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Выбор месяца", callback_data="archive_months")
    builder.button(text="📅 Матрица", callback_data="view_motivation_schedule")
    builder.button(text="⬅️ В меню", callback_data="admin_motivation")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@commission_router.message(MotivationStates.waiting_for_cell_value)
async def process_cell_value(message: Message, state: FSMContext):
    """Обработка введённого значения мотивации для конкретной ячейки матрицы"""
    from datetime import date as _date
    _cancel_kb = InlineKeyboardBuilder().button(
        text="❌ Отмена", callback_data="view_motivation_schedule"
    ).as_markup()
    try:
        value = float(message.text.replace(',', '.'))
    except ValueError:
        await fsm_edit(state, message,
                       "❌ <b>Неверный формат</b>\n\nВведите число.",
                       reply_markup=_cancel_kb)
        return

    data = await state.get_data()
    mtype = data.get('motivation_type', 'percentage')
    prod_id = data.get('motivation_product_id')
    prod_name = data.get('motivation_product_name', '')
    year = data.get('motivation_pending_year')
    month = data.get('motivation_pending_month')

    if mtype == 'percentage' and (value <= 0 or value > 50):
        await fsm_edit(state, message, "❌ Процент должен быть от 0.1 до 50", reply_markup=_cancel_kb)
        return
    if mtype == 'fixed' and value <= 0:
        await fsm_edit(state, message, "❌ Сумма должна быть больше 0", reply_markup=_cancel_kb)
        return

    current_db = await get_db(message.from_user.id, state)
    ok = await current_db.set_motivation_for_month(prod_id, year, month, mtype, value, message.from_user.id)

    today = _date.today()
    recalc_note = ""
    if ok:
        is_past_or_cur = (year < today.year) or (year == today.year and month <= today.month)
        if is_past_or_cur:
            await current_db.recalculate_month_earnings(prod_id, year, month)
            recalc_note = "\n\nЗаработки за этот месяц пересчитаны."

    await clear_state_keep_org(state)
    month_label = f"{MONTH_NAMES_RU[month]} {year}"
    val_str = f"{value}% от продажи" if mtype == 'percentage' else f"{format_price(value)}/шт"

    await fsm_edit(
        state, message,
        f"✅ <b>Мотивация обновлена!</b>\n\n"
        f"📦 Товар: {he(prod_name)}\n"
        f"📅 Месяц: {month_label}\n"
        f"💰 Ставка: {val_str}{recalc_note}",
        reply_markup=InlineKeyboardBuilder().button(
            text="📅 В матрицу", callback_data="view_motivation_schedule"
        ).button(
            text="⬅️ В меню", callback_data="admin_motivation"
        ).adjust(1).as_markup(), parse_mode="HTML"
    )
