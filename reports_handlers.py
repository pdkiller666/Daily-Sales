"""
Обработчики для отчетов и рейтингов
"""
import asyncio
import logging
import os
from datetime import datetime, date, timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Database
from keyboards import back_button, generate_calendar, create_selection_keyboard
from states import ReportStates, RankingStates
from utils import format_date_display, generate_excel_report, format_currency, escape_md, he
from env_manager import env_manager
from reports_access_control import check_excel_export_permission, check_analytics_permission, get_user_access_info, create_subscription_offer_keyboard, get_subscription_offer_message

# Создаем роутер для отчетов
reports_router = Router()

from db_utils import get_db, clear_state_keep_org, is_any_admin, get_user_org_scope, maybe_refresh_username
from keyboards import safe_cb, resolve_cb_name
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_BTN
from hints import hint_suffix
from states import SearchStates
from message_utils import fsm_edit

logger = logging.getLogger(__name__)


def _translit_filename(text: str) -> str:
    """Транслитерация строки для безопасного имени файла (только ASCII + цифры + _-)."""
    MAP = {
        'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'Yo','Ж':'Zh',
        'З':'Z','И':'I','Й':'Y','К':'K','Л':'L','М':'M','Н':'N','О':'O',
        'П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F','Х':'Kh','Ц':'Ts',
        'Ч':'Ch','Ш':'Sh','Щ':'Sch','Ъ':'','Ы':'Y','Ь':'','Э':'E','Ю':'Yu','Я':'Ya',
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh',
        'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
        'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts',
        'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    }
    result = ''.join(MAP.get(ch, ch) for ch in str(text))
    import re
    result = re.sub(r'[^\w\-.]', '_', result)
    result = re.sub(r'_+', '_', result).strip('_')
    return result or 'report'

@reports_router.callback_query(F.data == "reports")
async def reports_menu(callback: CallbackQuery, state: FSMContext):
    """Меню отчетов с дашбордом и проверкой ограничений подписки"""
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return

    # Отвечаем на callback сразу — кнопка перестаёт «грузиться» мгновенно,
    # пока в фоне выполняется построение дашборда и отчёта.
    await callback.answer("⏳ Загрузка...")

    current_db = await get_db(callback.from_user.id, state)

    is_super_admin = env_manager.is_super_admin(callback.from_user.id)

    user = await current_db.get_user(callback.from_user.id)

    # Если супер-админ, создаем запись в БД если её нет
    if is_super_admin and not user:
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
        user = await current_db.get_user(callback.from_user.id)

    if not user:
        await callback.message.edit_text("❌ Сначала завершите регистрацию через /start")
        return

    maybe_refresh_username(
        current_db, callback.from_user.id, callback.from_user.username,
        stored_username=user[12] if len(user) > 12 else None,
    )

    # Вычисляем роль и scope один раз — используем далее во всех местах
    is_admin = is_any_admin(callback.from_user.id) or is_super_admin
    scope_type, scope_values = get_user_org_scope(callback.from_user.id)

    # ── Дашборд-сводка ──────────────────────────────────────────────────────
    from dashboard_handlers import build_admin_dashboard, build_user_dashboard
    from timezone_utils import get_current_user_time
    _user_tz = await current_db.get_user_timezone(callback.from_user.id)
    today   = date.today().isoformat()
    now_str = get_current_user_time(_user_tz).strftime("%d.%m.%Y · %H:%M")
    try:
        if is_admin:
            dashboard_text = await build_admin_dashboard(
                current_db, today, now_str, user[0], callback.from_user.id,
                scope_type=scope_type, scope_values=scope_values
            )
        else:
            dashboard_text = await build_user_dashboard(
                current_db, user[0], callback.from_user.id, today, now_str
            )
    except Exception as _dash_err:
        import traceback as _tb
        _trace = _tb.format_exc()
        logger.error("Dashboard build failed:\n%s", _trace)
        dashboard_text = f"⚠️ <i>Ошибка дашборда ({type(_dash_err).__name__}): {he(str(_dash_err))[:120]}</i>"

    # ── Меню отчётов ────────────────────────────────────────────────────────
    from subscription_utils import get_plan_limits
    limits = get_plan_limits(callback.from_user.id)

    builder = InlineKeyboardBuilder()

    # Базовый отчет за сегодня доступен всем
    builder.add(
        InlineKeyboardButton(text="📅 За сегодня", callback_data="report_today")
    )

    if is_super_admin:
        builder.add(
            InlineKeyboardButton(text="📅 За период", callback_data="report_period")
        )
        builder.adjust(1, 1)
    elif is_admin:
        if limits.get('can_view_analytics', False):
            builder.add(
                InlineKeyboardButton(text="📅 За месяц",  callback_data="report_admin_month"),
                InlineKeyboardButton(text="📅 За период", callback_data="report_period"),
            )
            builder.adjust(1, 2)
        else:
            builder.add(
                InlineKeyboardButton(text="🔒 За период", callback_data="blocked_analytics")
            )
            builder.adjust(1, 1)
    else:
        if limits.get('can_view_analytics', False):
            builder.add(
                InlineKeyboardButton(text="📊 Мои продажи",      callback_data="report_my_shop"),
                InlineKeyboardButton(text="📅 Текущий месяц",    callback_data="report_my_month"),
                InlineKeyboardButton(text="📅 За период",        callback_data="report_period"),
            )
            builder.adjust(1, 2, 1)
        else:
            builder.add(
                InlineKeyboardButton(text="🔒 Мои продажи",      callback_data="blocked_analytics"),
                InlineKeyboardButton(text="🔒 За период",        callback_data="blocked_analytics"),
            )
            builder.adjust(1, 2)
        builder.add(
            InlineKeyboardButton(text="📥 Мои продажи → Excel", callback_data="download_excel_my_sales_free")
        )

    if is_admin:
        from filter_utils import ADMIN_FILTER_KEY, get_available_filter_values, filter_button_text, has_anything_to_filter, empty_filter
        try:
            _avail = get_available_filter_values(current_db, scope_type, scope_values)
            if has_anything_to_filter(_avail):
                data_f = await state.get_data()
                _af = data_f.get(ADMIN_FILTER_KEY, empty_filter())
                builder.add(InlineKeyboardButton(
                    text=filter_button_text(_af),
                    callback_data="flt_open_reports"
                ))
        except Exception:
            pass
    builder.add(back_button("main_menu"))

    reports_header = "━━━━━━━━━━━━━━━━\n📊 <b>Отчеты по продажам</b>"
    if is_super_admin:
        reports_header += "\n👑 Супер-администратор: полный доступ"
    elif is_admin and not limits.get('can_view_analytics', False):
        reports_header += "\n⚠️ Для доступа к админ функциям требуется платная подписка"
    elif not is_admin and not limits.get('can_view_analytics', False):
        reports_header += "\n⚠️ Расширенные отчеты доступны только в платных тарифах"

    message_text = (dashboard_text + "\n\n" + reports_header) if dashboard_text else reports_header
    message_text += hint_suffix(current_db, user[0], 'first_reports')

    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )



@reports_router.callback_query(F.data == "report_today")
async def report_today(callback: CallbackQuery, state: FSMContext):
    """Отчет за сегодня — единый формат через generate_period_report."""
    if not callback.message:
        await callback.answer("❌ Сообщение слишком старое.", show_alert=True)
        return

    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Сначала завершите регистрацию через /start", show_alert=True)
        return

    await callback.answer("⏳ Загрузка...")
    today = date.today().isoformat()
    await state.update_data(start_date=today, end_date=today)

    is_admin = is_any_admin(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)
    await generate_period_report(callback, state, user_shop_only=not is_admin)

@reports_router.callback_query(F.data == "report_full")
async def report_full(callback: CallbackQuery, state: FSMContext):
    """Полный отчет по всем продажам"""
    is_super_admin = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id) or is_super_admin
    
    if not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    sales = await current_db.get_sales_report()
    
    if not sales:
        await callback.message.edit_text(
            "📊 Полный отчет\n\n❌ Продажи отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return
    
    # Группируем продажи по категориям и товарам
    # Структура: id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
    #            user_id[5], sale_date[6], product_name[7], category[8], first_name[9], last_name[10]
    categories = {}
    total_sum = 0
    total_quantity = 0
    shops_data = {}

    for sale in sales:
        sale_id, product_id, shop_name, quantity, sale_price, user_id, sale_date, product_name, category, first_name, last_name = sale
        try:
            price = float(sale_price) if sale_price else 0
            quantity = int(quantity) if quantity else 0
        except (ValueError, TypeError):
            price = 0
            quantity = 0
        sale_total = quantity * price
        total_sum += sale_total
        total_quantity += quantity

        if category not in categories:
            categories[category] = {'total': 0, 'products': {}}
        if product_name not in categories[category]['products']:
            categories[category]['products'][product_name] = {'quantity': 0, 'total': 0}
        categories[category]['products'][product_name]['quantity'] += quantity
        categories[category]['products'][product_name]['total'] += sale_total
        categories[category]['total'] += sale_total

        if shop_name not in shops_data:
            shops_data[shop_name] = 0
        shops_data[shop_name] += sale_total

    # Считаем суммарный заработок одним запросом
    total_earnings_accumulated = 0
    if sales:
        sale_ids = [sale[0] for sale in sales]
        placeholders = ','.join('?' * len(sale_ids))
        try:
            conn = await current_db.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT COALESCE(SUM(commission_amount), 0) FROM seller_earnings WHERE sale_id IN ({placeholders})',
                sale_ids
            )
            res = cursor.fetchone()
            conn.close()
            if res:
                total_earnings_accumulated = res[0]
        except Exception:
            pass

    message_text  = "📊 <b>Полный отчет по продажам</b>\n\n"
    message_text += "📈 <b>Общая статистика:</b>\n"
    message_text += f"• Продано товаров: {total_quantity} шт.\n"
    message_text += f"• Общая сумма: {format_currency(total_sum)}\n"
    message_text += f"• Заработок: <b>{format_currency(total_earnings_accumulated)}</b>\n"
    message_text += f"• Категорий: {len(categories)}\n"
    message_text += f"• Магазинов: {len(shops_data)}\n\n"

    # Все категории
    sorted_categories = sorted(categories.items(), key=lambda x: x[1]['total'], reverse=True)
    message_text += "📦 <b>По категориям:</b>\n"
    for i, (category, cat_data) in enumerate(sorted_categories, 1):
        if len(message_text) > 3500:
            remaining = len(sorted_categories) - i + 1
            message_text += f"<i>···  ещё {remaining} кат. — скачайте Excel</i>\n"
            break
        message_text += f"{i}. {he(category)}: {format_currency(cat_data['total'])}\n"

    # Все магазины
    sorted_shops = sorted(shops_data.items(), key=lambda x: x[1], reverse=True)
    message_text += "\n🏪 <b>По магазинам:</b>\n"
    for i, (shop, total) in enumerate(sorted_shops, 1):
        if len(message_text) > 3700:
            remaining = len(sorted_shops) - i + 1
            message_text += f"<i>···  ещё {remaining} маг. — скачайте Excel</i>\n"
            break
        message_text += f"{i}. {he(shop)}: {format_currency(total)}\n"

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📥 Скачать Excel", callback_data="download_excel_full"))
    builder.add(back_button("reports"))
    builder.adjust(1)

    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}

@reports_router.callback_query(F.data == "view_ratings")
async def view_ratings(callback: CallbackQuery, state: FSMContext):
    """Просмотр рейтинга продавцов за текущий месяц"""
    await callback.answer("⏳ Загрузка...")
    current_db = await get_db(callback.from_user.id, state)
    today = datetime.now()
    start_date = today.replace(day=1).strftime('%Y-%m-%d')
    end_date = today.strftime('%Y-%m-%d')

    ranking = await current_db.get_sales_ranking(start_date, end_date)

    month_name = MONTHS_RU.get(today.month, today.strftime('%B'))
    text = f"🏆 <b>Рейтинг продавцов за {month_name} {today.year}</b>\n\n"

    if not ranking:
        text += "❌ Пока нет данных о продажах за этот месяц."
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(ranking):
            first_name, last_name, shop_name, quantity, total_sum, sales_count, earnings = row[:7]
            username = row[8] if len(row) > 8 else None
            medal = medals[i] if i < 3 else f"{i + 1}."
            uname_str = f" <a href='tg://resolve?domain={he(username)}'>@{he(username)}</a>" if username else ""
            text += f"{medal} <b>{he(first_name)} {he(last_name)}</b>{uname_str}\n"
            text += f"   🏪 {he(shop_name)}\n"
            text += f"   📦 {quantity} шт. • 💰 {format_currency(total_sum)} • 📈 {format_currency(earnings)}\n\n"

    _vr_user = await current_db.get_user(callback.from_user.id)
    if _vr_user:
        text += hint_suffix(current_db, _vr_user[0], 'first_rankings')

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("main_menu")]]),
        parse_mode="HTML"
    )

@reports_router.callback_query(F.data.startswith("shop_report_"))
async def report_shop_generate(callback: CallbackQuery, state: FSMContext):
    """Генерация отчета по магазину"""
    await callback.answer()
    shop_raw = callback.data.replace("shop_report_", "")
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, await current_db.get_all_shops() or [])
    sales = await current_db.get_sales_report(shop_name=shop_name)
    
    if not sales:
        await callback.message.edit_text(
            f"📊 Отчет по магазину '{shop_name}'\n\n❌ Продажи отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return
    
    # Группируем продажи по категориям и товарам как в отчете за сегодня
    categories = {}
    total_sum = 0
    total_quantity = 0
    
    for sale in sales:
        try:
            # Структура: id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
            #            user_id[5], sale_date[6], product_name[7], category[8], first_name[9], last_name[10]
            if len(sale) >= 11:
                sale_id, product_id, shop_name_db, quantity, sale_price, user_id, sale_date, product_name, category, first_name, last_name = sale
            else:
                continue
                
            try:
                price = float(sale_price) if sale_price else 0
                quantity = int(quantity) if quantity else 0
            except (ValueError, TypeError):
                price = 0
                quantity = 0
            sale_total = quantity * price
            total_sum += sale_total
            total_quantity += quantity

            if category not in categories:
                categories[category] = {'total': 0, 'products': {}}
            if product_name not in categories[category]['products']:
                categories[category]['products'][product_name] = {'quantity': 0, 'total': 0}
            categories[category]['products'][product_name]['quantity'] += quantity
            categories[category]['products'][product_name]['total'] += sale_total
            categories[category]['total'] += sale_total
        except (ValueError, TypeError, IndexError):
            continue

    # Считаем суммарный заработок одним запросом
    total_earnings_accumulated = 0
    if sales:
        sale_ids = [sale[0] for sale in sales]
        placeholders = ','.join('?' * len(sale_ids))
        try:
            conn = await current_db.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT COALESCE(SUM(commission_amount), 0) FROM seller_earnings WHERE sale_id IN ({placeholders})',
                sale_ids
            )
            res = cursor.fetchone()
            conn.close()
            if res:
                total_earnings_accumulated = res[0]
        except Exception:
            pass

    message_text  = f"📊 <b>Отчет по магазину</b>\n🏪 {he(shop_name)}\n\n"
    message_text += "📈 <b>Общая статистика:</b>\n"
    message_text += f"• Продано товаров: {total_quantity} шт.\n"
    message_text += f"• Общая сумма: {format_currency(total_sum)}\n"
    message_text += f"• Заработок: <b>{format_currency(total_earnings_accumulated)}</b>\n"
    message_text += f"• Категорий: {len(categories)}\n\n"

    # Детализация по категориям со всеми товарами
    if categories:
        sorted_categories = sorted(categories.items(), key=lambda x: x[1]['total'], reverse=True)
        message_text += "📦 <b>Детализация по категориям:</b>\n"

        for category, cat_data in sorted_categories:
            message_text += f"\n🔸 <b>{he(category)}:</b> {format_currency(cat_data['total'])}\n"

            sorted_products = sorted(cat_data['products'].items(), key=lambda x: x[1]['total'], reverse=True)
            for product_name, product_data in sorted_products:
                message_text += f"   • {he(product_name)}: {product_data['quantity']} шт. — {format_currency(product_data['total'])}\n"

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📥 Скачать Excel", callback_data=safe_cb("download_excel_shop_", shop_name)))
    builder.add(back_button("reports"))
    builder.adjust(1)

    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@reports_router.callback_query(F.data == "report_my_shop")
async def report_my_shop(callback: CallbackQuery, state: FSMContext):
    """Отчет по всем продажам пользователя"""
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Сначала завершите регистрацию через /start", show_alert=True)
        return

    await callback.answer("⏳ Загрузка...")
    # Получаем все продажи пользователя независимо от магазина
    user_id = await current_db.get_user_id(callback.from_user.id)
    sales = await current_db.get_user_sales(user_id, limit=1000)  # Увеличиваем лимит для полного отчета
    
    if not sales:
        await callback.message.edit_text(
            f"📊 Отчет по всем продажам\n\n❌ Продажи отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return
    
    # Группируем продажи по магазинам, затем по категориям и товарам
    shops_data = {}
    total_sum = 0
    total_quantity = 0
    
    for sale in sales:
        # Структура из get_user_sales: id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4], user_id[5], sale_date[6], product_name[7], category[8]
        sale_id = sale[0]
        product_id = sale[1]
        shop_name_db = sale[2]
        quantity = sale[3]
        price = sale[4] if sale[4] else 0  # Фактическая цена продажи
        user_id = sale[5]
        sale_date = sale[6]
        product_name = sale[7] if len(sale) > 7 else "Неизвестный товар"
        category = sale[8] if len(sale) > 8 else "Без категории"

        sale_total = quantity * price
        total_sum += sale_total
        total_quantity += quantity
        
        # Группируем по магазинам
        if shop_name_db not in shops_data:
            shops_data[shop_name_db] = {'categories': {}, 'shop_total': 0, 'shop_quantity': 0}
        
        shops_data[shop_name_db]['shop_total'] += sale_total
        shops_data[shop_name_db]['shop_quantity'] += quantity
        
        # Группируем по категориям внутри магазина
        if category not in shops_data[shop_name_db]['categories']:
            shops_data[shop_name_db]['categories'][category] = {'total': 0, 'products': {}}
        
        if product_name not in shops_data[shop_name_db]['categories'][category]['products']:
            shops_data[shop_name_db]['categories'][category]['products'][product_name] = {'quantity': 0, 'total': 0}
        
        shops_data[shop_name_db]['categories'][category]['products'][product_name]['quantity'] += quantity
        shops_data[shop_name_db]['categories'][category]['products'][product_name]['total'] += sale_total
        shops_data[shop_name_db]['categories'][category]['total'] += sale_total
    
    shops_names = list(shops_data.keys())
    shops_text = ", ".join(shops_names) if shops_names else "Все магазины"

    # Считаем накопленный заработок одним запросом вместо N+1
    total_earnings_accumulated = 0
    if sales:
        sale_ids = [sale[0] for sale in sales]
        placeholders = ','.join('?' * len(sale_ids))
        try:
            conn = await current_db.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT COALESCE(SUM(commission_amount), 0) FROM seller_earnings WHERE sale_id IN ({placeholders})',
                sale_ids
            )
            res = cursor.fetchone()
            conn.close()
            if res:
                total_earnings_accumulated = res[0]
        except Exception:
            pass
    
    message_text  = "📊 <b>Отчет по всем продажам</b>\n"
    if len(shops_names) > 1:
        message_text += f"🏪 Магазины: {he(shops_text)}\n"
    message_text += "\n"
    message_text += "📈 <b>Общая статистика:</b>\n"
    message_text += f"• Продано: {total_quantity} шт.\n"
    message_text += f"• Сумма: {format_currency(total_sum)}\n"
    message_text += f"• Заработок: <b>{format_currency(total_earnings_accumulated)}</b>\n"
    message_text += f"• Магазинов: {len(shops_data)}\n\n"

    for shop_name in sorted(shops_data.keys()):
        if len(message_text) > 3600:
            message_text += "<i>···  ещё магазины скрыты — скачайте Excel для полной детализации</i>\n"
            break
        shop_data = shops_data[shop_name]
        message_text += f"🏪 <b>{he(shop_name)}</b>\n"
        message_text += f"• Продано: {shop_data['shop_quantity']} шт. — {format_currency(shop_data['shop_total'])}\n"
        categories = shop_data['categories']
        if categories:
            for category, cat_data in sorted(categories.items(), key=lambda x: x[1]['total'], reverse=True):
                message_text += f"  📂 <b>{he(category)}:</b> {format_currency(cat_data['total'])}\n"
                sorted_products = sorted(cat_data['products'].items(),
                                         key=lambda x: x[1]['total'], reverse=True)
                for product_name, product_data in sorted_products:
                    if len(message_text) > 3700:
                        message_text += "     <i>···  остальные товары в Excel</i>\n"
                        break
                    message_text += f"     • {he(product_name)}: {product_data['quantity']} шт. — {format_currency(product_data['total'])}\n"
        message_text += "\n"

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📥 Скачать Excel", callback_data="download_excel_user"))
    builder.add(back_button("reports"))
    builder.adjust(1)

    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@reports_router.callback_query(F.data == "report_my_month")
async def report_my_month(callback: CallbackQuery, state: FSMContext):
    """Отчёт за текущий месяц для сотрудника — быстрый доступ без выбора дат"""
    if is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступен только для сотрудников", show_alert=True)
        return
    current_db = await get_db(callback.from_user.id, state)
    from subscription_utils import get_plan_limits
    limits = get_plan_limits(callback.from_user.id)
    if not limits.get('can_view_analytics', False):
        await callback.answer("🔒 Расширенные отчёты доступны в платных тарифах", show_alert=True)
        return
    await callback.answer("⏳ Загрузка...")
    today = date.today()
    start_date = today.replace(day=1).isoformat()
    end_date = today.isoformat()
    await state.update_data(start_date=start_date, end_date=end_date)
    await generate_period_report(callback, state, user_shop_only=True)


@reports_router.callback_query(F.data == "report_admin_month")
async def report_admin_month(callback: CallbackQuery, state: FSMContext):
    """Быстрый отчёт за текущий месяц для администраторов — с учётом scope зоны ответственности."""
    is_super_admin = env_manager.is_super_admin(callback.from_user.id)
    if not (is_any_admin(callback.from_user.id) or is_super_admin):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    from subscription_utils import get_plan_limits
    limits = get_plan_limits(callback.from_user.id)
    if not limits.get('can_view_analytics', False) and not is_super_admin:
        await callback.answer("🔒 Расширенные отчёты доступны в платных тарифах", show_alert=True)
        return
    await callback.answer("⏳ Загрузка...")
    today      = date.today()
    start_date = today.replace(day=1).isoformat()
    end_date   = today.isoformat()
    await state.update_data(start_date=start_date, end_date=end_date)
    await generate_period_report(callback, state, user_shop_only=False)


@reports_router.callback_query(F.data == "report_city")
async def report_city_select(callback: CallbackQuery, state: FSMContext):
    """Выбор города для отчета"""
    is_super_admin = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id) or is_super_admin
    
    if not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    cities = await current_db.get_all_cities()
    
    if not cities:
        await callback.message.edit_text(
            "🏙️ Города отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    for city in cities:
        builder.add(InlineKeyboardButton(text=city, callback_data=safe_cb("city_report_", city)))
    builder.add(back_button("reports"))
    builder.adjust(2, 1)
    
    await callback.message.edit_text(
        "🏙️ Выберите город для отчета:",
        reply_markup=builder.as_markup()
    )

@reports_router.callback_query(F.data.startswith("city_report_"))
async def report_city_generate(callback: CallbackQuery, state: FSMContext):
    """Генерация отчета по городу"""
    await callback.answer()
    city_raw = callback.data.replace("city_report_", "")
    current_db = await get_db(callback.from_user.id, state)
    city_name = resolve_cb_name(city_raw, await current_db.get_all_cities() or [])

    users_in_city = await current_db.get_users_by_city(city_name)
    if not users_in_city:
        await callback.message.edit_text(
            f"🏙️ В городе '{city_name}' нет зарегистрированных пользователей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return

    shops_in_city = list(set(user[8] for user in users_in_city if user[8]))

    all_sales = await current_db.get_sales_report(shop_names=shops_in_city)

    if not all_sales:
        await callback.message.edit_text(
            f"🏙️ Отчет по городу '{city_name}'\n\n❌ Продажи отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return

    # Структура: id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
    #            user_id[5], sale_date[6], product_name[7], category[8], first_name[9], last_name[10]
    categories = {}
    shops_data = {}
    total_sum = 0
    total_quantity = 0

    for sale in all_sales:
        sale_id, product_id, shop_name, quantity, sale_price, user_id, sale_date, product_name, category, first_name, last_name = sale
        try:
            price = float(sale_price) if sale_price else 0
            quantity = int(quantity) if quantity else 0
        except (ValueError, TypeError):
            price = 0
            quantity = 0
        sale_total = quantity * price
        total_sum += sale_total
        total_quantity += quantity

        if category not in categories:
            categories[category] = {'total': 0, 'products': {}}
        if product_name not in categories[category]['products']:
            categories[category]['products'][product_name] = {'quantity': 0, 'total': 0}
        categories[category]['products'][product_name]['quantity'] += quantity
        categories[category]['products'][product_name]['total'] += sale_total
        categories[category]['total'] += sale_total

        if shop_name not in shops_data:
            shops_data[shop_name] = {'total': 0, 'quantity': 0}
        shops_data[shop_name]['total'] += sale_total
        shops_data[shop_name]['quantity'] += quantity

    # Считаем суммарный заработок одним запросом
    total_earnings_accumulated = 0
    if all_sales:
        sale_ids = [sale[0] for sale in all_sales]
        placeholders = ','.join('?' * len(sale_ids))
        try:
            conn = await current_db.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT COALESCE(SUM(commission_amount), 0) FROM seller_earnings WHERE sale_id IN ({placeholders})',
                sale_ids
            )
            res = cursor.fetchone()
            conn.close()
            if res:
                total_earnings_accumulated = res[0]
        except Exception:
            pass

    message_text  = f"🏙️ <b>Отчет по городу</b>\n📍 {he(city_name)}\n\n"
    message_text += "📈 <b>Общая статистика:</b>\n"
    message_text += f"• Продано товаров: {total_quantity} шт.\n"
    message_text += f"• Общая сумма: {format_currency(total_sum)}\n"
    message_text += f"• Заработок: <b>{format_currency(total_earnings_accumulated)}</b>\n"
    message_text += f"• Категорий: {len(categories)}\n"
    message_text += f"• Магазинов: {len(shops_data)}\n\n"

    message_text += "🏪 <b>По магазинам:</b>\n"
    for shop_name, shop_data in sorted(shops_data.items(), key=lambda x: x[1]['total'], reverse=True):
        message_text += f"• {he(shop_name)}: {format_currency(shop_data['total'])} ({shop_data['quantity']} шт.)\n"

    if categories:
        sorted_categories = sorted(categories.items(), key=lambda x: x[1]['total'], reverse=True)
        message_text += "\n📦 <b>Детализация по категориям:</b>\n"
        for category, cat_data in sorted_categories:
            message_text += f"\n🔸 <b>{he(category)}:</b> {format_currency(cat_data['total'])}\n"
            for product_name, product_data in sorted(cat_data['products'].items(), key=lambda x: x[1]['total'], reverse=True):
                message_text += f"   • {he(product_name)}: {product_data['quantity']} шт. — {format_currency(product_data['total'])}\n"

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📥 Скачать Excel", callback_data=safe_cb("download_excel_city_", city_name)))
    builder.add(back_button("reports"))
    builder.adjust(1)

    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@reports_router.callback_query(F.data == "report_period")
async def report_period_start(callback: CallbackQuery, state: FSMContext):
    """Начало выбора периода для отчета"""
    await callback.answer()
    await callback.message.edit_text(
        "📅 Отчет за период\n\nВыберите начальную дату:",
        reply_markup=generate_calendar(cancel_callback="reports")
    )
    await state.set_state(ReportStates.choosing_start_date)

@reports_router.callback_query(F.data.startswith("cal_date_"), ReportStates.choosing_start_date)
async def report_start_date_selected(callback: CallbackQuery, state: FSMContext):
    """Выбрана начальная дата"""
    await callback.answer()
    start_date = callback.data.replace("cal_date_", "")
    await state.update_data(start_date=start_date)
    
    await callback.message.edit_text(
        f"📅 Отчет за период\n\n"
        f"✅ Начальная дата: {format_date_display(start_date)}\n\n"
        f"Выберите конечную дату:",
        reply_markup=generate_calendar(cancel_callback="reports")
    )
    await state.set_state(ReportStates.choosing_end_date)

@reports_router.callback_query(F.data.startswith("cal_date_"), ReportStates.choosing_end_date)
async def report_end_date_selected(callback: CallbackQuery, state: FSMContext):
    """Выбрана конечная дата"""
    end_date = callback.data.replace("cal_date_", "")
    data = await state.get_data()
    start_date = data['start_date']
    
    # Проверяем корректность дат
    if end_date < start_date:
        await callback.answer("❌ Конечная дата не может быть раньше начальной!", show_alert=True)
        return

    await callback.answer()
    await state.update_data(end_date=end_date)
    
    # Определяем доступные опции в зависимости от прав пользователя
    if is_any_admin(callback.from_user.id):
        builder = InlineKeyboardBuilder()
        builder.add(
            InlineKeyboardButton(text="📊 Общий отчет",  callback_data="period_report_all"),
            InlineKeyboardButton(text="🏪 По магазину",  callback_data="period_report_shop"),
            InlineKeyboardButton(text="🏙️ По городу",   callback_data="period_report_city"),
        )
        builder.add(back_button("reports"))
        builder.adjust(2, 1, 1)

        await callback.message.edit_text(
            f"📅 Отчет за период\n\n"
            f"📅 С: {format_date_display(start_date)}\n"
            f"📅 По: {format_date_display(end_date)}\n\n"
            f"Выберите тип отчета:",
            reply_markup=builder.as_markup()
        )
    else:
        # Для обычного пользователя сразу генерируем отчет по его магазину
        await generate_period_report(callback, state, user_shop_only=True)

async def generate_period_report(callback: CallbackQuery, state: FSMContext,
                                 user_shop_only=False, shop_name=None, city_filter=None):
    """Генерация отчета за период"""
    current_db = await get_db(callback.from_user.id, state)

    data = await state.get_data()
    start_date = data['start_date']
    end_date = data['end_date']

    _user_id_for_period = None
    if user_shop_only:
        user = await current_db.get_user(callback.from_user.id)
        if not user:
            await callback.answer("❌ Пользователь не найден!", show_alert=True)
            return
        _user_id_for_period = user[0]  # внутренний DB id — фильтруем только свои продажи

    if city_filter:
        users_in_city = await current_db.get_users_by_city(city_filter)
        city_shops = list(set(u[8] for u in users_in_city if u[8]))
        sales = await current_db.get_sales_report(start_date=start_date, end_date=end_date, shop_names=city_shops)
    elif user_shop_only and _user_id_for_period:
        # Сотрудник: только его продажи (по user_id, а не по магазину)
        sales = await current_db.get_user_sales_by_date(_user_id_for_period, start_date, end_date)
    else:
        # Применяем ручной фильтр если установлен (только для полного admin-отчёта без shop_name/city_filter)
        if not shop_name and not city_filter:
            from filter_utils import ADMIN_FILTER_KEY, empty_filter, merge_scope_with_filter
            try:
                _af = data.get(ADMIN_FILTER_KEY, empty_filter())
                _sc, _sv = get_user_org_scope(callback.from_user.id)
                _fkw = merge_scope_with_filter(_sc, _sv, _af)
                sales = await current_db.get_sales_report(start_date=start_date, end_date=end_date, **_fkw)
            except Exception:
                sales = await current_db.get_sales_report(start_date=start_date, end_date=end_date, shop_name=shop_name)
        else:
            sales = await current_db.get_sales_report(start_date=start_date, end_date=end_date, shop_name=shop_name)
    
    period_text = f"{format_date_display(start_date)} - {format_date_display(end_date)}"
    
    if not sales:
        shop_text = f" по магазину '{shop_name}'" if shop_name else ""
        await callback.message.edit_text(
            f"📊 Отчет{shop_text}\n"
            f"📅 Период: {period_text}\n\n"
            f"❌ Продажи за указанный период отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        await clear_state_keep_org(state)
        return
    
    # Детальная обработка продаж с группировкой как в отчете за сегодня
    is_admin = is_any_admin(callback.from_user.id)
    
    # Группируем продажи по магазинам, затем по категориям и товарам
    shops_data = {}
    total_sum = 0
    total_quantity = 0
    total_earnings_accumulated = 0
    
    for sale in sales:
        try:
            # Структура: id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
            #            user_id[5], sale_date[6], product_name[7], category[8], first_name[9], last_name[10]
            if len(sale) >= 11:
                sale_id, product_id, shop_name_db, quantity, sale_price, user_id, sale_date, product_name, category, first_name, last_name = sale
            elif len(sale) >= 9:
                sale_id, product_id, shop_name_db, quantity, sale_price, user_id, sale_date, product_name, category = sale
            else:
                continue

            current_shop = shop_name if shop_name else shop_name_db

            try:
                price = float(sale_price) if sale_price else 0
                quantity = int(quantity) if quantity else 0
            except (ValueError, TypeError):
                price = 0
                quantity = 0

            sale_total = quantity * price
            total_sum += sale_total
            total_quantity += quantity
        except (ValueError, TypeError, IndexError):
            continue

        # Группируем по магазинам
        if current_shop not in shops_data:
            shops_data[current_shop] = {'categories': {}, 'shop_total': 0, 'shop_quantity': 0}

        shops_data[current_shop]['shop_total'] += sale_total
        shops_data[current_shop]['shop_quantity'] += quantity

        if category not in shops_data[current_shop]['categories']:
            shops_data[current_shop]['categories'][category] = {'total': 0, 'products': {}}

        if product_name not in shops_data[current_shop]['categories'][category]['products']:
            shops_data[current_shop]['categories'][category]['products'][product_name] = {'quantity': 0, 'total': 0}
        
        shops_data[current_shop]['categories'][category]['products'][product_name]['quantity'] += quantity
        shops_data[current_shop]['categories'][category]['products'][product_name]['total'] += sale_total
        shops_data[current_shop]['categories'][category]['total'] += sale_total

    # Считаем суммарный заработок одним запросом
    if sales:
        sale_ids = [sale[0] for sale in sales]
        placeholders = ','.join('?' * len(sale_ids))
        try:
            conn = await current_db.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT COALESCE(SUM(commission_amount), 0) FROM seller_earnings WHERE sale_id IN ({placeholders})',
                sale_ids
            )
            res = cursor.fetchone()
            conn.close()
            if res:
                total_earnings_accumulated = res[0]
        except Exception:
            pass

    # Формируем отчет
    shops_count = len(shops_data)
    shops_suffix = f" ({shops_count} маг.)" if shops_count > 1 else ""
    message_text  = f"📅 <b>Отчет за период{shops_suffix}</b>\n"
    message_text += f"📆 {period_text}\n\n"
    message_text += "📈 <b>Общая статистика:</b>\n"
    message_text += f"• Продано товаров: {total_quantity} шт.\n"
    message_text += f"• Общая сумма: {format_currency(total_sum)}\n"
    message_text += f"• Заработок: <b>{format_currency(total_earnings_accumulated)}</b>\n"
    if shops_count > 1:
        message_text += f"• Магазинов: {shops_count}\n"
    message_text += "\n"

    for shop_name_key in sorted(shops_data.keys()):
        if len(message_text) > 3600:
            message_text += "<i>···  ещё магазины скрыты — скачайте Excel для полной детализации</i>\n"
            break
        shop_data = shops_data[shop_name_key]
        message_text += f"🏪 <b>{he(shop_name_key)}</b>\n"
        message_text += f"• Продано: {shop_data['shop_quantity']} шт. — {format_currency(shop_data['shop_total'])}\n"
        categories = shop_data['categories']
        if categories:
            for category, cat_data in sorted(categories.items(), key=lambda x: x[1]['total'], reverse=True):
                message_text += f"  📂 <b>{he(category)}:</b> {format_currency(cat_data['total'])}\n"
                sorted_products = sorted(cat_data['products'].items(),
                                         key=lambda x: x[1]['total'], reverse=True)
                for product_name, product_data in sorted_products:
                    if len(message_text) > 3700:
                        message_text += "     <i>···  остальные товары в Excel</i>\n"
                        break
                    message_text += f"     • {he(product_name)}: {product_data['quantity']} шт. — {format_currency(product_data['total'])}\n"
        message_text += "\n"

    await state.update_data(excel_start=start_date, excel_end=end_date, excel_shop=shop_name)

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📥 Скачать Excel", callback_data="download_excel_period"))
    builder.add(back_button("reports"))
    builder.adjust(1)

    await callback.message.edit_text(
        message_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

    await clear_state_keep_org(state, extra_keys=['excel_start', 'excel_end', 'excel_shop'])

@reports_router.callback_query(F.data == "period_report_all")
async def period_report_all(callback: CallbackQuery, state: FSMContext):
    """Общий отчет за период"""
    is_super_admin = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id) or is_super_admin
    
    if not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    await generate_period_report(callback, state)

def _build_shop_picker_content(shops: list, page: int, *, search_cb="rep_srch_shop_start",
                                pg_prefix="rep_shop_pg_", back_cb="reports",
                                title="🏪 Выберите магазин для отчета за период:",
                                shop_cb_prefix="period_shop_"):
    """Build paginated shop picker. Returns (text, markup)."""
    page_items, has_prev, has_next, total_pages, page = paginate(shops, page, PAGE_SIZE_BTN)
    pg_line = f"\n<i>Стр. {page+1}/{total_pages} · всего: {len(shops)}</i>" if total_pages > 1 else ""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔍 Найти магазин", callback_data=search_cb))
    for shop in page_items:
        builder.row(InlineKeyboardButton(text=shop, callback_data=safe_cb(shop_cb_prefix, shop)))
    nav = page_nav_row(pg_prefix, page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(back_button(back_cb))
    return f"{title}{pg_line}", builder.as_markup()


@reports_router.callback_query(F.data == "period_report_shop")
async def period_report_shop_select(callback: CallbackQuery, state: FSMContext):
    """Выбор магазина для отчета за период"""
    is_super_admin = env_manager.is_super_admin(callback.from_user.id)
    is_admin = is_any_admin(callback.from_user.id) or is_super_admin

    if not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()

    if not shops:
        await callback.message.edit_text(
            "🏪 Магазины отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return

    await state.update_data(rep_period_shops=shops)
    text, markup = _build_shop_picker_content(shops, 0)
    await callback.message.edit_text(text, reply_markup=markup)


@reports_router.callback_query(F.data.startswith("rep_shop_pg_"))
async def period_report_shop_page(callback: CallbackQuery, state: FSMContext):
    """Пагинация в выборе магазина для отчёта."""
    if not (is_any_admin(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    page = int(callback.data.removeprefix("rep_shop_pg_"))
    data = await state.get_data()
    shops = data.get('rep_period_shops') or []
    if not shops:
        current_db = await get_db(callback.from_user.id, state)
        shops = await current_db.get_all_shops()
    await callback.answer()
    text, markup = _build_shop_picker_content(shops, page)
    await callback.message.edit_text(text, reply_markup=markup)


@reports_router.callback_query(F.data == "rep_srch_shop_start")
async def rep_srch_shop_start(callback: CallbackQuery, state: FSMContext):
    if not (is_any_admin(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.shop_reports)
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="period_report_shop"))
    await callback.message.edit_text(
        "🔍 <b>Поиск магазина</b>\n\nВведите название или часть названия:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@reports_router.message(SearchStates.shop_reports)
async def rep_srch_shop_process(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.set_state(None)
    current_db = await get_db(message.from_user.id, state)
    shops = await current_db.get_all_shops()
    filtered = [s for s in shops if query.lower() in s.lower()] if query else shops
    suffix = (f"\n🔍 «{he(query)}» — найдено: {len(filtered)}" if filtered
              else f"\n🔍 По запросу «{he(query)}» ничего не найдено") if query else ""
    extra_btn = [InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="period_report_shop")] if query else []
    text, markup = _build_shop_picker_content(
        filtered, 0,
        title=f"🏪 Выберите магазин для отчета за период:{suffix}"
    )
    # Inject reset-search button before back if query was given
    if query and extra_btn:
        from aiogram.utils.keyboard import InlineKeyboardBuilder as _IKB
        from aiogram.types import InlineKeyboardMarkup as _IKM
        rows = markup.inline_keyboard
        # insert reset btn before last row (Back)
        new_rows = rows[:-1] + [extra_btn] + [rows[-1]]
        markup = _IKM(inline_keyboard=new_rows)
    await fsm_edit(state, message, text, reply_markup=markup, parse_mode="HTML")


@reports_router.callback_query(F.data.startswith("period_shop_"))
async def period_report_shop_generate(callback: CallbackQuery, state: FSMContext):
    """Генерация отчета по магазину за период"""
    await callback.answer()
    shop_raw = callback.data.replace("period_shop_", "")
    current_db_r = await get_db(callback.from_user.id, state)
    _tmp_shops_r = await current_db_r.get_all_shops()
    shop_name = resolve_cb_name(shop_raw, _tmp_shops_r or [])
    await generate_period_report(callback, state, shop_name=shop_name)


@reports_router.callback_query(F.data == "period_report_city")
async def period_report_city_select(callback: CallbackQuery, state: FSMContext):
    """Выбор города для отчёта за период"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    cities = await current_db.get_all_cities()

    if not cities:
        await callback.message.edit_text(
            "🏙️ Города отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("reports")]])
        )
        return

    data = await state.get_data()
    start_date = data.get('start_date', '')
    end_date   = data.get('end_date', '')
    period_txt = (f"📅 С: {format_date_display(start_date)}\n"
                  f"📅 По: {format_date_display(end_date)}\n\n") if start_date else ""

    builder = InlineKeyboardBuilder()
    for city in cities:
        builder.add(InlineKeyboardButton(text=city, callback_data=safe_cb("period_city_", city)))
    builder.add(back_button("reports"))
    builder.adjust(2, 1)

    await callback.message.edit_text(
        f"🏙️ Отчёт за период по городу\n\n{period_txt}Выберите город:",
        reply_markup=builder.as_markup()
    )


@reports_router.callback_query(F.data.startswith("period_city_"))
async def period_report_city_generate(callback: CallbackQuery, state: FSMContext):
    """Генерация отчёта за период по городу"""
    await callback.answer()
    city_raw  = callback.data.replace("period_city_", "")
    current_db = await get_db(callback.from_user.id, state)
    city_name  = resolve_cb_name(city_raw, await current_db.get_all_cities() or [])
    await generate_period_report(callback, state, city_filter=city_name)


# РЕЙТИНГИ

# ─── Рейтинги: вспомогательные функции ────────────────────────────────────

def _ordinal_ru(n: int) -> str:
    """1 → '1-е место', 2 → '2-е место', 3 → '3-е место', 4+ → 'N-е место'"""
    suffixes = {1: '1-е', 2: '2-е', 3: '3-е'}
    return f"{suffixes.get(n, f'{n}-е')} место"


def _ranking_period(period: str, custom_start: str = None, custom_end: str = None):
    """Возвращает (start_date, end_date, label) для заданного периода"""
    today = date.today()
    if period == 'custom' and custom_start and custom_end:
        label = f"{format_date_display(custom_start)} – {format_date_display(custom_end)}"
        return custom_start, custom_end, label
    if period == '7d':
        start = (today - timedelta(days=6)).isoformat()
        return start, today.isoformat(), "последние 7 дней"
    elif period == 'prev':
        first_this = today.replace(day=1)
        last_prev  = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        label = f"{MONTHS_RU.get(last_prev.month, '')} {last_prev.year}"
        return first_prev.isoformat(), last_prev.isoformat(), label
    else:  # month
        label = f"{MONTHS_RU.get(today.month, '')} {today.year}"
        return today.replace(day=1).isoformat(), today.isoformat(), label


def _period_kb(active: str, rtype: str, back_cb: str) -> InlineKeyboardMarkup:
    """Клавиатура переключения периода + кнопка Назад"""
    btns = []
    for code, label in [('7d', '7 дней'), ('month', 'Этот месяц'), ('prev', 'Прошлый')]:
        cb   = f"rank_{rtype}_{code}"
        text = f"✅ {label}" if code == active else f"📅 {label}"
        btns.append(InlineKeyboardButton(text=text, callback_data=cb))
    custom_text = "✅ 📆 Период" if active == 'custom' else "📆 Свой период"
    return InlineKeyboardMarkup(inline_keyboard=[
        btns,
        [InlineKeyboardButton(text=custom_text, callback_data=f"rank_{rtype}_custom")],
        [back_button(back_cb)],
    ])


# ─── Меню рейтингов ────────────────────────────────────────────────────────

@reports_router.callback_query(F.data == "rankings_menu")
async def rankings_menu_admin(callback: CallbackQuery, state: FSMContext):
    """Меню рейтингов для администратора"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    await callback.answer()

    from filter_utils import ADMIN_FILTER_KEY, empty_filter, filter_button_text, get_available_filter_values, has_anything_to_filter
    data_r = await state.get_data()
    _af = data_r.get(ADMIN_FILTER_KEY, empty_filter())
    filter_row = []
    try:
        current_db = await get_db(callback.from_user.id, state)
        _sc, _sv = get_user_org_scope(callback.from_user.id)
        _avail = get_available_filter_values(current_db, _sc, _sv)
        if has_anything_to_filter(_avail):
            filter_row = [InlineKeyboardButton(text=filter_button_text(_af), callback_data="flt_open_rankings_menu")]
    except Exception:
        pass

    kb_rows = [
        [InlineKeyboardButton(text="👤 Продавцы", callback_data="rank_sel_month"),
         InlineKeyboardButton(text="🏪 Магазины",  callback_data="rank_shp_month")],
        [InlineKeyboardButton(text="🏙️ Города",   callback_data="rank_cty_month")],
    ]
    if filter_row:
        kb_rows.append(filter_row)
    kb_rows.append([InlineKeyboardButton(text="🗑️ Очистить рейтинги", callback_data="clear_rankings_confirm")])
    kb_rows.append([back_button("main_menu")])

    await callback.message.edit_text(
        "🏆 <b>Рейтинги</b>\n\nВыберите тип:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML"
    )


@reports_router.callback_query(F.data == "user_rankings_menu")
async def user_rankings_menu(callback: CallbackQuery):
    """Меню рейтингов для обычного пользователя"""
    await callback.answer()
    await callback.message.edit_text(
        "🏆 <b>Рейтинги</b>\n\nВыберите тип:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Продавцы", callback_data="rank_sel_month"),
             InlineKeyboardButton(text="🏪 Магазины",  callback_data="rank_shp_month")],
            [back_button("main_menu")],
        ]),
        parse_mode="HTML"
    )


# ─── Продавцы ──────────────────────────────────────────────────────────────

async def _show_sellers(callback: CallbackQuery, state: FSMContext, period: str):
    await callback.answer()
    if period == 'custom':
        data = await state.get_data()
        cs, ce = data.get('rank_start', ''), data.get('rank_end', '')
        start, end, label = _ranking_period('custom', cs, ce)
    else:
        start, end, label = _ranking_period(period)
    current_db  = await get_db(callback.from_user.id, state)
    is_admin    = is_any_admin(callback.from_user.id)
    back_cb     = "rankings_menu" if is_admin else "user_rankings_menu"
    from filter_utils import ADMIN_FILTER_KEY, empty_filter, merge_scope_with_filter
    _data_r = await state.get_data()
    _af_r = _data_r.get(ADMIN_FILTER_KEY, empty_filter())
    _sc_r, _sv_r = get_user_org_scope(callback.from_user.id)
    _fkw_r = merge_scope_with_filter(_sc_r, _sv_r, _af_r)
    ranking = await current_db.get_sales_ranking(start, end, **_fkw_r)
    medals      = ["🥇", "🥈", "🥉"]

    if not ranking:
        await callback.message.edit_text(
            f"🏆 <b>Рейтинг продавцов</b>\n📅 {label}\n\n❌ Данные за этот период отсутствуют.",
            reply_markup=_period_kb(period, 'sel', back_cb),
            parse_mode="HTML"
        )
        return

    # Строим карту конкурсов за период: user_db_id → [(title, pos, reward, actual, is_winner)]
    contest_map: dict = {}
    try:
        contests = await current_db.get_contests_for_period(start, end)
        for contest in contests:
            c_id, c_title = contest[0], contest[1]
            results = await current_db.compute_contest_results(c_id)
            for pos, r in enumerate(results, 1):
                uid = r.get('user_id')
                if uid is None:
                    continue
                contest_map.setdefault(uid, []).append({
                    'title':     c_title,
                    'pos':       pos,
                    'reward':    r.get('reward', 0.0),
                    'actual':    r.get('actual', 0.0),
                    'is_winner': r.get('is_winner', False),
                })
    except Exception:
        pass

    text = f"🏆 <b>Рейтинг продавцов</b>\n📅 {label}\n\n"
    for i, row in enumerate(ranking[:10]):
        fn, ln, sn, qty, revenue, cnt, earn = row[:7]
        uid_row = row[7] if len(row) > 7 else None
        uname   = row[8] if len(row) > 8 else None
        medal   = medals[i] if i < 3 else f"{i+1}."
        uname_str = f" <a href='tg://resolve?domain={he(uname)}'>@{he(uname)}</a>" if uname else ""

        text += f"{medal} <b>{he(fn)} {he(ln)}</b>{uname_str}\n"
        text += f"   🏪 {he(sn)}\n"
        text += f"   📦 {qty} шт. · 💰 {format_currency(revenue)}\n"
        if earn and earn > 0:
            text += f"   📈 Мотивация: <b>{format_currency(earn)}</b>\n"

        # Конкурсы
        if uid_row and uid_row in contest_map:
            for cr in contest_map[uid_row]:
                if cr['is_winner']:
                    badge = "🏆"
                    reward_str = f" · +{format_currency(cr['reward'])}" if cr['reward'] > 0 else ""
                else:
                    badge = "📋"
                    reward_str = ""
                pos_str = _ordinal_ru(cr['pos'])
                text += f"   {badge} «{he(cr['title'])}»: {pos_str}{reward_str}\n"

        text += "\n"

    # Позиция текущего пользователя — показываем, если он за пределами топ-10
    user_db_id = await current_db.get_user_id(callback.from_user.id)
    my_pos, my_row = None, None
    for i, row in enumerate(ranking):
        if len(row) > 7 and row[7] == user_db_id:
            my_pos, my_row = i + 1, row
            break
    if my_pos and my_pos > 10 and my_row:
        fn, ln, sn, qty, revenue, cnt, earn = my_row[:7]
        uid_row = my_row[7] if len(my_row) > 7 else None
        text += f"━━━━━━━━━━━\n📍 <b>Ваша позиция: {_ordinal_ru(my_pos)}</b>\n"
        text += f"   📦 {qty} шт. · 💰 {format_currency(revenue)}"
        if earn and earn > 0:
            text += f" · 📈 {format_currency(earn)}"
        text += "\n"
        if uid_row and uid_row in contest_map:
            for cr in contest_map[uid_row]:
                if cr['is_winner']:
                    reward_str = f" · +{format_currency(cr['reward'])}" if cr['reward'] > 0 else ""
                    text += f"   🏆 «{he(cr['title'])}»: {_ordinal_ru(cr['pos'])}{reward_str}\n"

    await callback.message.edit_text(
        text, reply_markup=_period_kb(period, 'sel', back_cb), parse_mode="HTML"
    )


@reports_router.callback_query(F.data.in_({"ranking_sellers", "rank_sel_month"}))
async def rank_sel_month(callback: CallbackQuery, state: FSMContext):
    await _show_sellers(callback, state, 'month')


@reports_router.callback_query(F.data == "rank_sel_7d")
async def rank_sel_7d(callback: CallbackQuery, state: FSMContext):
    await _show_sellers(callback, state, '7d')


@reports_router.callback_query(F.data == "rank_sel_prev")
async def rank_sel_prev(callback: CallbackQuery, state: FSMContext):
    await _show_sellers(callback, state, 'prev')


# ─── Магазины ──────────────────────────────────────────────────────────────

async def _show_shops(callback: CallbackQuery, state: FSMContext, period: str):
    await callback.answer()
    if period == 'custom':
        data = await state.get_data()
        cs, ce = data.get('rank_start', ''), data.get('rank_end', '')
        start, end, label = _ranking_period('custom', cs, ce)
    else:
        start, end, label = _ranking_period(period)
    current_db = await get_db(callback.from_user.id, state)
    is_admin   = is_any_admin(callback.from_user.id)
    back_cb    = "rankings_menu" if is_admin else "user_rankings_menu"
    ranking    = await current_db.get_shop_ranking(start, end)
    medals     = ["🥇", "🥈", "🥉"]

    if not ranking:
        await callback.message.edit_text(
            f"🏆 <b>Рейтинг магазинов</b>\n📅 {label}\n\n❌ Данные за этот период отсутствуют.",
            reply_markup=_period_kb(period, 'shp', back_cb),
            parse_mode="HTML"
        )
        return

    text = f"🏆 <b>Рейтинг магазинов</b>\n📅 {label}\n\n"
    for i, (sn, qty, revenue, sellers_cnt, sales_cnt, earn) in enumerate(ranking[:10]):
        medal  = medals[i] if i < 3 else f"{i+1}."
        text  += f"{medal} <b>{he(sn)}</b>\n"
        text  += f"   📦 {qty} шт. · 💰 {format_currency(revenue)} · 📈 {format_currency(earn)}\n"
        text  += f"   👥 {sellers_cnt} прод. · 📋 {sales_cnt} чеков\n\n"

    await callback.message.edit_text(
        text, reply_markup=_period_kb(period, 'shp', back_cb), parse_mode="HTML"
    )


@reports_router.callback_query(F.data.in_({"ranking_shops", "rank_shp_month"}))
async def rank_shp_month(callback: CallbackQuery, state: FSMContext):
    await _show_shops(callback, state, 'month')


@reports_router.callback_query(F.data == "rank_shp_7d")
async def rank_shp_7d(callback: CallbackQuery, state: FSMContext):
    await _show_shops(callback, state, '7d')


@reports_router.callback_query(F.data == "rank_shp_prev")
async def rank_shp_prev(callback: CallbackQuery, state: FSMContext):
    await _show_shops(callback, state, 'prev')


# ─── Города (только для админов) ───────────────────────────────────────────

async def _show_cities(callback: CallbackQuery, state: FSMContext, period: str):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    await callback.answer()
    if period == 'custom':
        data = await state.get_data()
        cs, ce = data.get('rank_start', ''), data.get('rank_end', '')
        start, end, label = _ranking_period('custom', cs, ce)
    else:
        start, end, label = _ranking_period(period)
    current_db = await get_db(callback.from_user.id, state)
    ranking    = await current_db.get_city_ranking(start, end)
    medals     = ["🥇", "🥈", "🥉"]

    if not ranking:
        await callback.message.edit_text(
            f"🏆 <b>Рейтинг городов</b>\n📅 {label}\n\n❌ Данные за этот период отсутствуют.",
            reply_markup=_period_kb(period, 'cty', "rankings_menu"),
            parse_mode="HTML"
        )
        return

    text = f"🏆 <b>Рейтинг городов</b>\n📅 {label}\n\n"
    for i, (city, qty, revenue, sellers_cnt, sales_cnt, earn) in enumerate(ranking[:10]):
        medal  = medals[i] if i < 3 else f"{i+1}."
        text  += f"{medal} <b>{he(city)}</b>\n"
        text  += f"   📦 {qty} шт. · 💰 {format_currency(revenue)} · 📈 {format_currency(earn)}\n"
        text  += f"   👥 {sellers_cnt} прод. · 📋 {sales_cnt} чеков\n\n"

    await callback.message.edit_text(
        text, reply_markup=_period_kb(period, 'cty', "rankings_menu"), parse_mode="HTML"
    )


@reports_router.callback_query(F.data.in_({"ranking_cities", "rank_cty_month"}))
async def rank_cty_month(callback: CallbackQuery, state: FSMContext):
    await _show_cities(callback, state, 'month')


@reports_router.callback_query(F.data == "rank_cty_7d")
async def rank_cty_7d(callback: CallbackQuery, state: FSMContext):
    await _show_cities(callback, state, '7d')


@reports_router.callback_query(F.data == "rank_cty_prev")
async def rank_cty_prev(callback: CallbackQuery, state: FSMContext):
    await _show_cities(callback, state, 'prev')

# ─── Рейтинги: произвольный период (календарь) ────────────────────────────

_RTYPE_BACK: dict = {
    'sel': ('rankings_menu', 'user_rankings_menu'),
    'shp': ('rankings_menu', 'user_rankings_menu'),
    'cty': ('rankings_menu', 'rankings_menu'),
}

def _rank_cal_cancel(rtype: str, is_admin: bool) -> str:
    admin_cb, user_cb = _RTYPE_BACK.get(rtype, ('rankings_menu', 'user_rankings_menu'))
    return admin_cb if is_admin else user_cb


@reports_router.callback_query(F.data.in_({"rank_sel_custom", "rank_shp_custom", "rank_cty_custom"}))
async def rank_custom_start(callback: CallbackQuery, state: FSMContext):
    """Начало выбора произвольного периода для рейтинга"""
    await callback.answer()
    rtype = callback.data.split("_")[1]  # sel / shp / cty
    cancel_cb = _rank_cal_cancel(rtype, is_any_admin(callback.from_user.id))
    await state.update_data(rank_cal_rtype=rtype)
    await state.set_state(RankingStates.choosing_start_date)
    await callback.message.edit_text(
        "📆 <b>Свой период для рейтинга</b>\n\nВыберите <b>начальную</b> дату:",
        reply_markup=generate_calendar(cancel_callback=cancel_cb, prefix="rkcal_"),
        parse_mode="HTML",
    )


@reports_router.callback_query(F.data.startswith("rkcal_nav_"))
async def rank_cal_nav(callback: CallbackQuery, state: FSMContext):
    """Навигация по календарю рейтинга"""
    await callback.answer()
    parts = callback.data.split("_")  # rkcal nav YEAR MONTH
    year, month = int(parts[2]), int(parts[3])
    current_state = await state.get_state()
    data = await state.get_data()
    rtype = data.get('rank_cal_rtype', 'sel')
    cancel_cb = _rank_cal_cancel(rtype, is_any_admin(callback.from_user.id))
    selected_start = data.get('rank_start') if current_state == RankingStates.choosing_end_date.state else None
    await callback.message.edit_reply_markup(
        reply_markup=generate_calendar(year, month, cancel_callback=cancel_cb, prefix="rkcal_")
    )


@reports_router.callback_query(F.data.startswith("rkcal_date_"), RankingStates.choosing_start_date)
async def rank_cal_start_date(callback: CallbackQuery, state: FSMContext):
    """Выбрана начальная дата рейтинга"""
    await callback.answer()
    start_date = callback.data.replace("rkcal_date_", "")
    await state.update_data(rank_start=start_date)
    await state.set_state(RankingStates.choosing_end_date)
    data = await state.get_data()
    rtype = data.get('rank_cal_rtype', 'sel')
    cancel_cb = _rank_cal_cancel(rtype, is_any_admin(callback.from_user.id))
    await callback.message.edit_text(
        f"📆 <b>Свой период для рейтинга</b>\n\n"
        f"✅ Начало: <b>{format_date_display(start_date)}</b>\n\n"
        f"Выберите <b>конечную</b> дату:",
        reply_markup=generate_calendar(cancel_callback=cancel_cb, prefix="rkcal_"),
        parse_mode="HTML",
    )


@reports_router.callback_query(F.data.startswith("rkcal_date_"), RankingStates.choosing_end_date)
async def rank_cal_end_date(callback: CallbackQuery, state: FSMContext):
    """Выбрана конечная дата рейтинга — показываем рейтинг"""
    end_date = callback.data.replace("rkcal_date_", "")
    data = await state.get_data()
    start_date = data.get('rank_start', '')
    rtype = data.get('rank_cal_rtype', 'sel')

    if end_date < start_date:
        await callback.answer("❌ Конечная дата не может быть раньше начальной!", show_alert=True)
        return

    await state.update_data(rank_end=end_date)
    await clear_state_keep_org(state)
    await state.update_data(rank_start=start_date, rank_end=end_date, rank_cal_rtype=rtype)

    if rtype == 'sel':
        await _show_sellers(callback, state, 'custom')
    elif rtype == 'shp':
        await _show_shops(callback, state, 'custom')
    else:
        await _show_cities(callback, state, 'custom')


@reports_router.callback_query(F.data == "clear_rankings_confirm")
async def clear_rankings_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение очистки рейтингов"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    # Получаем статистику для отображения
    current_db = await get_db(callback.from_user.id, state)
    total_sales = await current_db.get_sales_report()
    sales_count = len(total_sales) if total_sales else 0
    
    message_text = (
        "⚠️ Подтверждение очистки рейтингов\n\n"
        "Вы действительно хотите очистить все данные продаж?\n\n"
        f"📊 Текущая статистика:\n"
        f"• Всего продаж: {sales_count}\n\n"
        "🚨 Внимание!\n"
        "• Все данные продаж будут удалены безвозвратно\n"
        "• Рейтинги продавцов, магазинов и городов будут сброшены\n"
        "• Данные пользователей и товары останутся без изменений\n"
        "• Остатки товаров НЕ будут изменены\n\n"
        "Это действие нельзя отменить!"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Да, очистить все продажи", callback_data="clear_rankings_execute")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="rankings_menu")],
        [back_button("rankings_menu")]
    ])
    
    await callback.message.edit_text(message_text, reply_markup=keyboard)

@reports_router.callback_query(F.data == "clear_rankings_execute")
async def clear_rankings_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнение очистки рейтингов"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    try:
        # Получаем статистику перед удалением
        total_sales = await current_db.get_sales_report()
        sales_count = len(total_sales) if total_sales else 0
        
        # Очищаем таблицу продаж
        result = await current_db.clear_all_sales()
        
        if result:
            message_text = (
                "✅ Рейтинги успешно очищены!\n\n"
                f"📊 Удалено записей: {sales_count}\n\n"
                "🔄 Результат:\n"
                "• Все данные продаж удалены\n"
                "• Рейтинги сброшены\n"
                "• Пользователи и товары сохранены\n"
                "• Остатки товаров не изменены\n\n"
                "Можете начинать новый отчетный период!"
            )
        else:
            message_text = (
                "❌ Ошибка при очистке рейтингов\n\n"
                "Попробуйте еще раз или обратитесь к администратору."
            )
    except Exception as e:
        message_text = (
            "❌ Ошибка при очистке рейтингов\n\n"
            f"Ошибка: {str(e)}"
        )
    
    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("rankings_menu")]])
    )

# Навигация по календарю
@reports_router.callback_query(F.data.startswith("cal_nav_"))
async def calendar_navigation(callback: CallbackQuery, state: FSMContext):
    """Навигация по календарю"""
    await callback.answer()
    _, _, year, month = callback.data.split("_")
    year, month = int(year), int(month)
    
    current_state = await state.get_state()
    cancel_callback = "reports"
    
    await callback.message.edit_reply_markup(
        reply_markup=generate_calendar(year, month, cancel_callback)
    )

# ОБРАБОТЧИКИ СКАЧИВАНИЯ EXCEL ФАЙЛОВ (lazy generation — файл создаётся при нажатии)

@reports_router.callback_query(F.data == "download_excel_full")
async def download_excel_full(callback: CallbackQuery, state: FSMContext):
    """Скачивание полного отчета в Excel (с учётом scope-зоны ответственности admin)"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    can_export, error_message = check_excel_export_permission(current_db, user_id, env_manager, callback.from_user.id)
    if not can_export:
        await callback.answer(error_message, show_alert=True)
        return

    await callback.answer("⏳ Формирую файл...")

    from filter_utils import ADMIN_FILTER_KEY, empty_filter, merge_scope_with_filter
    try:
        fsm_data = await state.get_data()
        _af = fsm_data.get(ADMIN_FILTER_KEY, empty_filter())
        _sc, _sv = get_user_org_scope(callback.from_user.id)
        _fkw = merge_scope_with_filter(_sc, _sv, _af)
        sales = await current_db.get_sales_report(**_fkw)
    except Exception:
        sales = await current_db.get_sales_report()

    if not sales:
        await callback.message.answer("❌ Нет данных для экспорта")
        return

    file_path = generate_excel_report(sales, "Полный отчёт по продажам", "Все периоды")
    if not file_path:
        await callback.message.answer("❌ Ошибка при создании файла")
        return

    try:
        await callback.message.answer_document(
            FSInputFile(file_path, filename="Polnyy_otchet.xlsx"),
            caption=f"📊 Полный отчёт · {len(sales)} строк"
        )
    except Exception:
        await callback.message.answer("❌ Ошибка при отправке файла")
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass

@reports_router.callback_query(F.data.startswith("download_excel_shop_"))
async def download_excel_shop(callback: CallbackQuery, state: FSMContext):
    """Скачивание отчета по магазину в Excel"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    can_export, error_message = check_excel_export_permission(current_db, user_id, env_manager, callback.from_user.id)
    if not can_export:
        await callback.answer(error_message, show_alert=True)
        return

    await callback.answer("⏳ Формирую файл...")

    shop_raw = callback.data.replace("download_excel_shop_", "")
    shop_name = resolve_cb_name(shop_raw, await current_db.get_all_shops() or [])

    sales = await current_db.get_sales_report(shop_name=shop_name)
    if not sales:
        await callback.message.answer("❌ Нет данных для экспорта")
        return

    file_path = generate_excel_report(sales, f"Отчёт по магазину «{shop_name}»", "Все периоды", shop_name)
    if not file_path:
        await callback.message.answer("❌ Ошибка при создании файла")
        return

    safe_name = _translit_filename(shop_name)
    try:
        await callback.message.answer_document(
            FSInputFile(file_path, filename=f"Otchet_{safe_name}.xlsx"),
            caption=f"🏪 {shop_name} · {len(sales)} строк"
        )
    except Exception:
        await callback.message.answer("❌ Ошибка при отправке файла")
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass

@reports_router.callback_query(F.data == "download_excel_user")
async def download_excel_user(callback: CallbackQuery, state: FSMContext):
    """Скачивание отчёта пользователя в Excel (все его продажи без лимита)"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    can_export, error_message = check_excel_export_permission(current_db, user_id, env_manager, callback.from_user.id)
    if not can_export:
        await callback.answer(error_message, show_alert=True)
        return

    await callback.answer("⏳ Формирую файл...")

    sales = await current_db.get_user_sales(user_id, limit=50000)
    if not sales:
        await callback.message.answer("❌ Нет данных для экспорта")
        return

    user = await current_db.get_user(callback.from_user.id)
    seller_label = f"{user[1]} {user[2]}".strip() if user else "Пользователь"

    file_path = generate_excel_report(sales, f"Продажи: {seller_label}", "Все периоды", None)
    if not file_path:
        await callback.message.answer("❌ Ошибка при создании файла")
        return

    try:
        await callback.message.answer_document(
            FSInputFile(file_path, filename="Otchet_polzovatelya.xlsx"),
            caption=f"👤 {seller_label} · {len(sales)} строк"
        )
    except Exception:
        await callback.message.answer("❌ Ошибка при отправке файла")
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass

@reports_router.callback_query(F.data == "download_excel_period")
async def download_excel_period(callback: CallbackQuery, state: FSMContext):
    """Скачивание отчета за период в Excel (параметры читаются из state)."""
    current_db = await get_db(callback.from_user.id, state)
    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    can_export, error_message = check_excel_export_permission(current_db, user_id, env_manager, callback.from_user.id)
    if not can_export:
        await callback.answer(error_message, show_alert=True)
        return

    fsm_data = await state.get_data()
    start_date = fsm_data.get('excel_start')
    end_date = fsm_data.get('excel_end')
    shop_name = fsm_data.get('excel_shop')

    if not start_date or not end_date:
        await callback.answer("❌ Сессия устарела, откройте отчёт заново.", show_alert=True)
        return

    await callback.answer("⏳ Формирую файл...")

    sales = await current_db.get_sales_report(start_date=start_date, end_date=end_date, shop_name=shop_name)
    if not sales:
        await callback.message.answer("❌ Нет данных для экспорта")
        return

    period_text = f"{format_date_display(start_date)} – {format_date_display(end_date)}"
    report_title = "Отчёт за период"
    if shop_name:
        report_title += f" · {shop_name}"

    file_path = generate_excel_report(sales, report_title, period_text, shop_name)
    if not file_path:
        await callback.message.answer("❌ Ошибка при создании файла")
        return

    safe_shop = f"_{_translit_filename(shop_name)}" if shop_name else ""
    filename = f"Otchet_{start_date}_{end_date}{safe_shop}.xlsx"

    try:
        await callback.message.answer_document(
            FSInputFile(file_path, filename=filename),
            caption=f"📅 {period_text}{(' · ' + shop_name) if shop_name else ''} · {len(sales)} строк"
        )
    except Exception:
        await callback.message.answer("❌ Ошибка при отправке файла")
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass

@reports_router.callback_query(F.data.startswith("download_excel_city_"))
async def download_excel_city(callback: CallbackQuery, state: FSMContext):
    """Скачивание отчёта по городу в Excel (один SQL-запрос через shop_names)"""
    current_db = await get_db(callback.from_user.id, state)
    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    can_export, error_message = check_excel_export_permission(current_db, user_id, env_manager, callback.from_user.id)
    if not can_export:
        await callback.answer(error_message, show_alert=True)
        return

    await callback.answer("⏳ Формирую файл...")

    city_raw = callback.data.replace("download_excel_city_", "")
    city_name = resolve_cb_name(city_raw, await current_db.get_all_cities() or [])

    users_in_city = await current_db.get_users_by_city(city_name)
    if not users_in_city:
        await callback.message.answer("❌ Нет пользователей в этом городе")
        return

    shops_in_city = list(set(u[8] for u in users_in_city if u[8]))
    if not shops_in_city:
        await callback.message.answer("❌ Нет магазинов в этом городе")
        return

    all_sales = await current_db.get_sales_report(shop_names=shops_in_city)

    if not all_sales:
        await callback.message.answer("❌ Нет данных для экспорта")
        return

    file_path = generate_excel_report(all_sales, f"Отчёт по городу «{city_name}»", "Все периоды")
    if not file_path:
        await callback.message.answer("❌ Ошибка при создании файла")
        return

    safe_city = _translit_filename(city_name)
    try:
        await callback.message.answer_document(
            FSInputFile(file_path, filename=f"Otchet_{safe_city}.xlsx"),
            caption=f"🏙️ {city_name} · {len(shops_in_city)} маг. · {len(all_sales)} строк"
        )
    except Exception:
        await callback.message.answer("❌ Ошибка при отправке файла")
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass

@reports_router.callback_query(F.data == "download_excel_my_sales_free")
async def download_excel_my_sales_free(callback: CallbackQuery, state: FSMContext):
    """Бесплатный Excel-экспорт для продавца — только его собственные продажи, без проверки подписки."""
    current_db = await get_db(callback.from_user.id, state)
    user_id = await current_db.get_user_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    sales = await current_db.get_user_sales(user_id, limit=50000)
    if not sales:
        await callback.answer("❌ У вас пока нет продаж для экспорта", show_alert=True)
        return

    await callback.answer("⏳ Формирую файл...")

    user = await current_db.get_user(callback.from_user.id)
    seller_label = f"{user[1]} {user[2]}".strip() if user else "Продавец"

    file_path = generate_excel_report(sales, f"Мои продажи: {seller_label}", "Всё время", None)
    if not file_path:
        await callback.message.answer("❌ Ошибка при создании файла")
        return

    try:
        await callback.message.answer_document(
            FSInputFile(file_path, filename="Moi_prodazhi.xlsx"),
            caption=f"📥 Ваши продажи за всё время · {len(sales)} строк"
        )
    except Exception:
        await callback.message.answer("❌ Ошибка при отправке файла")
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass


# ОБРАБОТЧИКИ НЕДОСТУПНЫХ ФУНКЦИЙ (ПРЕДЛОЖЕНИЕ ПОДПИСКИ)

@reports_router.callback_query(F.data == "blocked_excel_export")
async def blocked_excel_export(callback: CallbackQuery):
    """Обработчик недоступного экспорта Excel с предложением подписки"""
    await callback.answer()
    is_admin = is_any_admin(callback.from_user.id)
    
    message = get_subscription_offer_message("Экспорт в Excel", is_admin)
    keyboard = create_subscription_offer_keyboard()
    
    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        parse_mode='HTML'
    )

@reports_router.callback_query(F.data == "blocked_analytics")
async def blocked_analytics(callback: CallbackQuery):
    """Обработчик недоступной аналитики с предложением подписки"""
    await callback.answer()
    is_admin = is_any_admin(callback.from_user.id)
    
    message = get_subscription_offer_message("Расширенная аналитика", is_admin)
    keyboard = create_subscription_offer_keyboard()
    
    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        parse_mode='HTML'
    )

@reports_router.callback_query(F.data == "blocked_custom_reports")
async def blocked_custom_reports(callback: CallbackQuery):
    """Обработчик недоступных настраиваемых отчетов с предложением подписки"""
    await callback.answer()
    is_admin = is_any_admin(callback.from_user.id)
    
    message = get_subscription_offer_message("Настраиваемые отчеты", is_admin)
    keyboard = create_subscription_offer_keyboard()
    
    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        parse_mode='HTML'
    )

@reports_router.callback_query(F.data == "blocked_notifications")
async def blocked_notifications(callback: CallbackQuery):
    """Обработчик недоступных уведомлений с предложением подписки"""
    await callback.answer()
    is_admin = is_any_admin(callback.from_user.id)
    
    # Создаем специальное сообщение для уведомлений
    admin_note = "\n\n👑 Как администратор, вы также можете получить доступ через платную подписку." if is_admin else ""
    
    message = f"""❌ <b>Доступ ограничен</b>

🔒 Функция "Система уведомлений" доступна только в платных тарифах.

💎 <b>Что включают уведомления:</b>
• 📦 Оповещения о низких остатках
• 📊 Ежедневные отчеты по продажам
• 💰 Уведомления о новых продажах
• 💳 Уведомления о платежах
• ⚙️ Административные уведомления
• 🔔 Персонализированные оповещения{admin_note}

👆 Выберите действие:"""
    
    # Создаем клавиатуру с возвратом в главное меню
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Оформить подписку", callback_data="subscription_menu")],
        [InlineKeyboardButton(text="📋 Посмотреть тарифы", callback_data="subscription_plans")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        parse_mode='HTML'
    )