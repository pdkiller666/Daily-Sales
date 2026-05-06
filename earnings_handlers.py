"""
Обработчики для просмотра заработка продавцов
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, timedelta
import calendar

from database import Database
from keyboards import InlineKeyboardBuilder
from utils import format_price, escape_md
from db_utils import get_db

earnings_router = Router()

class EarningsStates(StatesGroup):
    selecting_period = State()

@earnings_router.callback_query(F.data == "my_earnings")
async def my_earnings_menu(callback: CallbackQuery, state: FSMContext):
    """Меню просмотра заработка"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="📅 За текущий месяц", callback_data="earnings_current_month")
    builder.button(text="📊 За всё время", callback_data="earnings_all_time")
    builder.button(text="📋 Детальный отчет", callback_data="earnings_detailed")
    builder.button(text="🗓️ Выбрать период", callback_data="earnings_select_period")
    builder.button(text="⬅️ Назад", callback_data="main_menu")
    builder.adjust(1)

    current_month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_earnings = current_db.get_seller_total_earnings(user_id, current_month_start.isoformat())
    all_time_earnings = current_db.get_seller_total_earnings(user_id)

    now = datetime.now()
    worked_days = current_db.get_worked_days_count(user_id, now.year, now.month)
    monthly_salary = current_db.calculate_monthly_salary(user_id, now.year, now.month)
    daily_rate = current_db.get_salary_rate(user_id)

    text = (
        "💰 *Мой заработок*\n\n"
        f"📅 *Текущий месяц:* {format_price(month_earnings['total_earnings'])}₽\n"
        f"📦 Продаж: {month_earnings['total_sales']}\n"
    )
    if daily_rate > 0 or worked_days > 0:
        text += (
            f"🏠 Оклад: {worked_days} смен × {format_price(daily_rate)}₽ = "
            f"{format_price(monthly_salary)}₽\n"
            f"💼 Итого: {format_price(month_earnings['total_earnings'] + monthly_salary)}₽\n"
        )
    text += (
        f"\n📊 *За всё время:* {format_price(all_time_earnings['total_earnings'])}₽\n"
        f"📦 Продаж: {all_time_earnings['total_sales']}\n\n"
        "Выберите период для подробного просмотра:"
    )

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@earnings_router.callback_query(F.data == "earnings_current_month")
async def earnings_current_month(callback: CallbackQuery, state: FSMContext):
    """Заработок за текущий месяц"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    current_month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    earnings = current_db.get_seller_earnings(user_id, current_month_start.isoformat())
    total_earnings = current_db.get_seller_total_earnings(user_id, current_month_start.isoformat())

    current_month_name = calendar.month_name[datetime.now().month]

    now = datetime.now()
    worked_days = current_db.get_worked_days_count(user_id, now.year, now.month)
    monthly_salary = current_db.calculate_monthly_salary(user_id, now.year, now.month)
    daily_rate = current_db.get_salary_rate(user_id)

    text = f"📅 *Заработок за {current_month_name} {now.year}*\n\n"
    text += f"💰 *Мотивация:* {format_price(total_earnings['total_earnings'])}₽\n"
    text += f"📦 *Количество продаж:* {total_earnings['total_sales']}\n"
    if daily_rate > 0 or worked_days > 0:
        text += (
            f"\n🏠 *Оклад:*\n"
            f"📅 Смен: {worked_days} × {format_price(daily_rate)}₽ = "
            f"*{format_price(monthly_salary)}₽*\n"
        )
        text += (
            f"\n━━━━━━━━━━━━━━━━\n"
            f"💼 *Итого (мотивация + оклад):*\n"
            f"*{format_price(total_earnings['total_earnings'] + monthly_salary)}₽*\n"
        )
    text += "\n"

    if not earnings:
        text += "❌ В этом месяце продаж пока нет"
    else:
        text += "📋 *Детали по продажам:*\n\n"
        products_earnings = {}
        for earning in earnings:
            commission_amount, motivation_type, motivation_value, product_name, quantity_sold, sale_price, sale_date, shop_name = earning
            if product_name not in products_earnings:
                products_earnings[product_name] = {'total_commission': 0, 'total_quantity': 0, 'sales_count': 0}
            products_earnings[product_name]['total_commission'] += commission_amount
            products_earnings[product_name]['total_quantity'] += quantity_sold
            products_earnings[product_name]['sales_count'] += 1

        for product_name, data in products_earnings.items():
            text += (
                f"🔹 *{escape_md(product_name)}*\n"
                f"💰 {format_price(data['total_commission'])}₽ | "
                f"📦 {data['total_quantity']} шт | "
                f"📋 {data['sales_count']} продаж\n\n"
            )

    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Подробный отчет", callback_data="earnings_detailed_current")
    builder.button(text="📅 Мой график", callback_data="my_schedule")
    builder.button(text="⬅️ Назад", callback_data="my_earnings")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@earnings_router.callback_query(F.data == "earnings_all_time")
async def earnings_all_time(callback: CallbackQuery, state: FSMContext):
    """Заработок за всё время"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    earnings = current_db.get_seller_earnings(user_id)
    total_earnings = current_db.get_seller_total_earnings(user_id)

    text = "📊 *Заработок за всё время*\n\n"
    text += f"💰 *Общий заработок:* {format_price(total_earnings['total_earnings'])}₽\n"
    text += f"📦 *Количество продаж:* {total_earnings['total_sales']}\n\n"

    if not earnings:
        text += "❌ Продаж пока нет"
    else:
        text += "📋 *Топ товаров по заработку:*\n\n"
        products_earnings = {}
        for earning in earnings:
            commission_amount, motivation_type, motivation_value, product_name, quantity_sold, sale_price, sale_date, shop_name = earning
            if product_name not in products_earnings:
                products_earnings[product_name] = {'total_commission': 0, 'total_quantity': 0, 'sales_count': 0}
            products_earnings[product_name]['total_commission'] += commission_amount
            products_earnings[product_name]['total_quantity'] += quantity_sold
            products_earnings[product_name]['sales_count'] += 1

        sorted_products = sorted(products_earnings.items(), key=lambda x: x[1]['total_commission'], reverse=True)

        for i, (product_name, data) in enumerate(sorted_products[:10], 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            text += (
                f"{medal} *{escape_md(product_name)}*\n"
                f"💰 {format_price(data['total_commission'])}₽ | "
                f"📦 {data['total_quantity']} шт | "
                f"📋 {data['sales_count']} продаж\n\n"
            )

        if len(sorted_products) > 10:
            text += f"... и еще {len(sorted_products) - 10} товаров"

    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Детальный отчет", callback_data="earnings_detailed")
    builder.button(text="⬅️ Назад", callback_data="my_earnings")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@earnings_router.callback_query(F.data.startswith("earnings_detailed"))
async def earnings_detailed(callback: CallbackQuery, state: FSMContext):
    """Детальный отчет по заработку с пагинацией по дням"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    if callback.data == "earnings_detailed_current":
        current_month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        earnings = current_db.get_seller_earnings(user_id, current_month_start.isoformat())
        period_text = f"{calendar.month_name[datetime.now().month]} {datetime.now().year}"
        period_type = "current"
    else:
        earnings = current_db.get_seller_earnings(user_id)
        period_text = "всё время"
        period_type = "all"

    if not earnings:
        text = f"📋 *Детальный отчет за {period_text}*\n\n❌ Продаж пока нет"
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="my_earnings")
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        return

    sales_by_date = {}
    for earning in earnings:
        commission_amount, motivation_type, motivation_value, product_name, quantity_sold, sale_price, sale_date, shop_name = earning
        try:
            sale_datetime = datetime.fromisoformat(sale_date.replace('Z', '+00:00'))
            date_key = sale_datetime.strftime("%Y-%m-%d")
        except Exception:
            date_key = sale_date[:10] if len(sale_date) >= 10 else sale_date

        if date_key not in sales_by_date:
            sales_by_date[date_key] = []
        sales_by_date[date_key].append({
            'commission_amount': commission_amount, 'motivation_type': motivation_type,
            'motivation_value': motivation_value, 'product_name': product_name,
            'quantity_sold': quantity_sold, 'sale_price': sale_price,
            'sale_date': sale_date, 'shop_name': shop_name
        })

    sorted_dates = sorted(sales_by_date.keys(), reverse=True)

    if not sorted_dates:
        text = f"📋 *Детальный отчет за {period_text}*\n\n❌ Продаж пока нет"
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="my_earnings")
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        return

    text = f"📋 *Детальный отчет за {period_text}*\n\n📊 Выберите день для просмотра:\n\n"

    builder = InlineKeyboardBuilder()
    for date_key in sorted_dates[:15]:
        try:
            date_obj = datetime.strptime(date_key, "%Y-%m-%d")
            formatted_date = date_obj.strftime("%d.%m.%Y")
            weekday = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][date_obj.weekday()]
        except Exception:
            formatted_date = date_key
            weekday = ""

        sales_count = len(sales_by_date[date_key])
        day_earning = sum(sale['commission_amount'] for sale in sales_by_date[date_key])

        button_text = f"{weekday} {formatted_date} | {sales_count} продаж | {format_price(day_earning)}₽"
        builder.button(text=button_text, callback_data=f"earnings_day_{period_type}_{date_key}")

    if len(sorted_dates) > 15:
        text += f"\n... показаны последние 15 дней из {len(sorted_dates)}"

    builder.button(text="⬅️ Назад", callback_data="my_earnings")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@earnings_router.callback_query(F.data.startswith("earnings_day_"))
async def earnings_day_details(callback: CallbackQuery, state: FSMContext):
    """Детальный просмотр продаж за конкретный день"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    parts = callback.data.split("_")
    period_type = parts[2]
    date_key = parts[3]

    if period_type == "current":
        current_month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        earnings = current_db.get_seller_earnings(user_id, current_month_start.isoformat())
        back_callback = "earnings_detailed_current"
    else:
        earnings = current_db.get_seller_earnings(user_id)
        back_callback = "earnings_detailed"

    day_sales = []
    for earning in earnings:
        commission_amount, motivation_type, motivation_value, product_name, quantity_sold, sale_price, sale_date, shop_name = earning
        try:
            sale_datetime = datetime.fromisoformat(sale_date.replace('Z', '+00:00'))
            sale_date_key = sale_datetime.strftime("%Y-%m-%d")
        except Exception:
            sale_date_key = sale_date[:10] if len(sale_date) >= 10 else sale_date

        if sale_date_key == date_key:
            day_sales.append(earning)

    if not day_sales:
        await callback.answer("❌ Продажи за этот день не найдены", show_alert=True)
        return

    try:
        date_obj = datetime.strptime(date_key, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d.%m.%Y")
        weekday = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"][date_obj.weekday()]
    except Exception:
        formatted_date = date_key
        weekday = ""

    text = f"📋 *Продажи за {weekday}, {formatted_date}*\n\n"

    day_total = 0
    day_earning = 0

    for i, earning in enumerate(day_sales, 1):
        commission_amount, motivation_type, motivation_value, product_name, quantity_sold, sale_price, sale_date, shop_name = earning

        try:
            sale_datetime = datetime.fromisoformat(sale_date.replace('Z', '+00:00'))
            time_str = sale_datetime.strftime("%H:%M")
        except Exception:
            time_str = sale_date

        if motivation_type == 'percentage':
            comm_info = f"{motivation_value}%"
        else:
            comm_info = f"{format_price(motivation_value)}₽/шт"

        sale_total = quantity_sold * sale_price
        day_total += sale_total
        day_earning += commission_amount

        text += (
            f"*{i}.* {product_name}\n"
            f"🕐 {time_str} | 🏪 {shop_name}\n"
            f"📦 {quantity_sold} шт × {format_price(sale_price)}₽ = {format_price(sale_total)}₽\n"
            f"💰 Мотивация: {format_price(commission_amount)}₽ ({comm_info})\n\n"
        )

    text += f"📊 *Итого за день:*\n"
    text += f"💵 Сумма продаж: {format_price(day_total)}₽\n"
    text += f"💰 Ваша мотивация: {format_price(day_earning)}₽\n"
    text += f"📦 Продаж: {len(day_sales)}"

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К выбору дней", callback_data=back_callback)
    builder.button(text="🏠 Главное меню", callback_data="my_earnings")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@earnings_router.callback_query(F.data == "earnings_select_period")
async def earnings_select_period(callback: CallbackQuery, state: FSMContext):
    """Выбор периода для просмотра заработка"""
    builder = InlineKeyboardBuilder()

    current_date = datetime.now()
    for i in range(1, 7):
        date = current_date - timedelta(days=30*i)
        month_name = calendar.month_name[date.month]
        builder.button(text=f"{month_name} {date.year}", callback_data=f"earnings_month_{date.year}_{date.month}")

    builder.button(text="📅 Прошлый год", callback_data="earnings_last_year")
    builder.button(text="⬅️ Назад", callback_data="my_earnings")
    builder.adjust(1)

    await callback.message.edit_text(
        "🗓️ *Выбор периода*\n\nВыберите месяц или период для просмотра заработка:",
        reply_markup=builder.as_markup(), parse_mode="Markdown"
    )
    await callback.answer()

@earnings_router.callback_query(F.data.startswith("earnings_month_"))
async def earnings_specific_month(callback: CallbackQuery, state: FSMContext):
    """Заработок за конкретный месяц"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    _, _, year, month = callback.data.split("_")
    year, month = int(year), int(month)

    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1) - timedelta(seconds=1)
    else:
        end_date = datetime(year, month + 1, 1) - timedelta(seconds=1)

    earnings = current_db.get_seller_earnings(user_id, start_date.isoformat(), end_date.isoformat())
    total_earnings = current_db.get_seller_total_earnings(user_id, start_date.isoformat(), end_date.isoformat())

    month_name = calendar.month_name[month]

    worked_days = current_db.get_worked_days_count(user_id, year, month)
    monthly_salary = current_db.calculate_monthly_salary(user_id, year, month)
    daily_rate = current_db.get_salary_rate(user_id)

    text = f"📅 *Заработок за {month_name} {year}*\n\n"
    text += f"💰 *Мотивация:* {format_price(total_earnings['total_earnings'])}₽\n"
    text += f"📦 *Количество продаж:* {total_earnings['total_sales']}\n"
    if daily_rate > 0 or worked_days > 0:
        text += (
            f"\n🏠 *Оклад:*\n"
            f"📅 Смен: {worked_days} × {format_price(daily_rate)}₽ = "
            f"*{format_price(monthly_salary)}₽*\n"
            f"\n━━━━━━━━━━━━━━━━\n"
            f"💼 *Итого (мотивация + оклад):*\n"
            f"*{format_price(total_earnings['total_earnings'] + monthly_salary)}₽*\n"
        )
    text += "\n"

    if not earnings:
        text += "❌ В этом месяце продаж не было"
    else:
        text += "📋 *Продажи по товарам:*\n\n"
        products_earnings = {}
        for earning in earnings:
            commission_amount, motivation_type, motivation_value, product_name, quantity_sold, sale_price, sale_date, shop_name = earning
            if product_name not in products_earnings:
                products_earnings[product_name] = {'total_commission': 0, 'total_quantity': 0, 'sales_count': 0}
            products_earnings[product_name]['total_commission'] += commission_amount
            products_earnings[product_name]['total_quantity'] += quantity_sold
            products_earnings[product_name]['sales_count'] += 1

        for product_name, data in products_earnings.items():
            text += (
                f"🔹 *{escape_md(product_name)}*\n"
                f"💰 {format_price(data['total_commission'])}₽ | "
                f"📦 {data['total_quantity']} шт | "
                f"📋 {data['sales_count']} продаж\n\n"
            )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="earnings_select_period")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@earnings_router.callback_query(F.data == "earnings_last_year")
async def earnings_last_year(callback: CallbackQuery, state: FSMContext):
    """Заработок за прошлый год"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    last_year = datetime.now().year - 1
    start_date = datetime(last_year, 1, 1)
    end_date = datetime(last_year, 12, 31, 23, 59, 59)

    earnings = current_db.get_seller_earnings(user_id, start_date.isoformat(), end_date.isoformat())
    total_earnings = current_db.get_seller_total_earnings(user_id, start_date.isoformat(), end_date.isoformat())

    text = f"📅 *Заработок за {last_year} год*\n\n"
    text += f"💰 *Общий заработок:* {format_price(total_earnings['total_earnings'])}₽\n"
    text += f"📦 *Количество продаж:* {total_earnings['total_sales']}\n\n"

    if not earnings:
        text += f"❌ В {last_year} году продаж не было"
    else:
        monthly_earnings = {}
        for earning in earnings:
            commission_amount, motivation_type, motivation_value, product_name, quantity_sold, sale_price, sale_date, shop_name = earning
            try:
                sale_datetime = datetime.fromisoformat(sale_date.replace('Z', '+00:00'))
                month_key = sale_datetime.month
                if month_key not in monthly_earnings:
                    monthly_earnings[month_key] = 0
                monthly_earnings[month_key] += commission_amount
            except Exception:
                continue

        if monthly_earnings:
            text += "📊 *По месяцам:*\n\n"
            for month in range(1, 13):
                if month in monthly_earnings:
                    month_name = calendar.month_name[month]
                    text += f"🔹 {month_name}: {format_price(monthly_earnings[month])}₽\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="earnings_select_period")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()
