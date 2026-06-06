"""
Утилиты для работы с данными
"""
import os
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.chart import BarChart, Reference
import tempfile

def format_currency(amount):
    """Форматирование валюты"""
    if amount is None:
        return "0₽"

    # Проверяем тип данных и пытаемся привести к числу
    try:
        if isinstance(amount, str):
            # Если строка содержит дату или нечисловые символы
            if '-' in amount and ':' in amount:
                return "0₽"
            # Пытаемся преобразовать строку в число
            amount = float(amount)

        # Преобразуем в целое число для отображения
        amount_int = int(float(amount))
        return f"{amount_int:,}₽".replace(',', ' ')
    except (ValueError, TypeError):
        return "0₽"

def format_price(price):
    """Форматирование цены с пробелами для тысяч"""
    return f"{price:,.0f}".replace(",", " ")

def escape_md(text):
    """Экранирование спецсимволов Markdown V1 в пользовательских строках (*_`[)"""
    if not text:
        return ""
    text = str(text)
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, '\\' + ch)
    return text

import html as _html

def he(text) -> str:
    """Экранирование HTML спецсимволов (<, >, &, \") для Telegram HTML parse_mode"""
    if text is None:
        return ""
    return _html.escape(str(text))

_shop_db_for_tz = None
_main_db_for_tz  = None

_DEFAULT_TZ = 'Europe/Moscow'


def format_date_for_user(date_str, telegram_id):
    """
    Форматирование даты с автоматическим получением часового пояса пользователя.

    Порядок поиска timezone (первый не-дефолтный выигрывает):
      1. main.db        — пишется при любом изменении TZ (org + personal)
      2. shop_bot.db    — пишется для personal-режима и после фикса орг-юзеров

    Args:
        date_str: Дата в виде строки или объекта datetime
        telegram_id: Telegram ID пользователя

    Returns:
        Отформатированная строка с датой в часовом поясе пользователя
    """
    global _shop_db_for_tz, _main_db_for_tz
    from database import Database
    if _main_db_for_tz is None:
        _main_db_for_tz = Database('data/main.db')
    if _shop_db_for_tz is None:
        _shop_db_for_tz = Database('data/shop_bot.db')

    tz = _main_db_for_tz.get_user_timezone(telegram_id)
    if not tz or tz == _DEFAULT_TZ:
        tz2 = _shop_db_for_tz.get_user_timezone(telegram_id)
        if tz2 and tz2 != _DEFAULT_TZ:
            tz = tz2
    return format_date_display(date_str, tz or _DEFAULT_TZ)

def format_date_display(date_str, user_timezone=None):
    """
    Форматирование даты для отображения с учетом часового пояса пользователя
    
    Args:
        date_str: Дата в виде строки или объекта datetime
        user_timezone: Часовой пояс пользователя (опционально)
    
    Returns:
        Отформатированная строка с датой
    """
    if not date_str:
        return "Неизвестно"
    
    try:
        # Если это не строка и не дата, возвращаем "Неизвестно"
        if not isinstance(date_str, (str, datetime)):
            # Проверяем, является ли это числом (цена)
            try:
                float(date_str)
                return "Неизвестно"
            except (ValueError, TypeError):
                pass
        
        # Если указан часовой пояс, используем timezone_utils
        if user_timezone:
            from timezone_utils import format_user_datetime
            result = format_user_datetime(date_str, user_timezone, '%d.%m.%Y %H:%M')
            if result != "Неизвестно":
                return result
        
        if isinstance(date_str, str):
            # Пробуем парсить ISO формат с помощью fromisoformat
            try:
                # Убираем 'Z' если есть и заменяем на +00:00
                iso_date = date_str.replace('Z', '+00:00')
                # Если есть 'T', это ISO формат
                if 'T' in iso_date:
                    date_obj = datetime.fromisoformat(iso_date)
                    return date_obj.strftime('%d.%m.%Y %H:%M')
            except (ValueError, AttributeError):
                pass
            
            # Пробуем разные форматы даты
            formats_with_time = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f']
            for fmt in formats_with_time:
                try:
                    date_obj = datetime.strptime(date_str, fmt)
                    return date_obj.strftime('%d.%m.%Y %H:%M')  # С временем
                except ValueError:
                    continue
            
            # Пробуем формат только с датой
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                return date_obj.strftime('%d.%m.%Y')  # Только дата
            except ValueError:
                pass
            
            # Если ни один формат не подошел, пробуем взять только дату из строки
            if ' ' in date_str:
                date_part = date_str.split()[0]
                try:
                    date_obj = datetime.strptime(date_part, '%Y-%m-%d')
                    return date_obj.strftime('%d.%m.%Y')
                except ValueError:
                    pass
            
            # Если строка содержит только время или другой формат
            return "Неизвестно"
        else:
            # Если это объект datetime
            return date_str.strftime('%d.%m.%Y')
    except Exception:
        return "Неизвестно"

def validate_phone_number(phone):
    """Валидация номера телефона"""
    if not phone:
        return False

    # Удаляем все символы кроме цифр
    digits_only = ''.join(filter(str.isdigit, phone))

    # Проверяем длину (10-11 цифр для российских номеров)
    return len(digits_only) >= 10

def validate_email(email):
    """Простая валидация email"""
    if not email:
        return False
    return '@' in email and '.' in email.split('@')[-1]

def get_file_size_mb(file_path):
    """Получение размера файла в МБ"""
    try:
        size_bytes = os.path.getsize(file_path)
        size_mb = size_bytes / (1024 * 1024)
        return round(size_mb, 2)
    except Exception:
        return 0

def generate_excel_report(data, title, period, shop_filter=None):
    """
    Генерация Excel отчёта по продажам.
    Создаёт до 4 листов:
      1. «Детальный отчёт»  — строки продаж с колонкой Продавец (если доступна)
      2. «По категориям»    — агрегат + BarChart
      3. «По продавцам»     — агрегат по продавцам (только если данные есть)
      4. «По дням»          — динамика по дням + BarChart (только при >1 дне)

    data: строки из get_sales_report() (11 полей, есть имя продавца)
          или get_user_sales() (9 полей, без имени продавца)
    """
    try:
        BLUE_FILL  = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        TOTAL_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
        HDR_FONT   = Font(bold=True, color="FFFFFF")
        TOTAL_FONT = Font(bold=True)
        TITLE_FONT = Font(bold=True, size=13)
        FMT_MONEY  = '#,##0.00'
        FMT_INT    = '#,##0'

        has_seller = any(len(r) >= 11 for r in data)

        wb = Workbook()

        # ── Лист 1: Детальный отчёт ─────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "Детальный отчёт"

        ws1['A1'] = title
        ws1['A1'].font = TITLE_FONT
        ws1['A2'] = f"Период: {period}"
        if shop_filter:
            ws1['A3'] = f"Магазин: {shop_filter}"

        headers = ['Дата', 'Товар', 'Категория', 'Кол-во', 'Цена за ед.', 'Сумма', 'Магазин']
        if has_seller:
            headers.append('Продавец')

        HDR_ROW = 5
        for col, hdr in enumerate(headers, 1):
            cell = ws1.cell(row=HDR_ROW, column=col, value=hdr)
            cell.font = HDR_FONT
            cell.fill = BLUE_FILL
            cell.alignment = Alignment(horizontal='center')

        category_stats = {}
        seller_stats   = {}
        day_stats      = {}
        total_qty      = 0
        total_sum      = 0
        data_row       = HDR_ROW + 1

        for sale in data:
            if len(sale) < 9:
                continue
            shop_name_v  = sale[2]
            quantity     = sale[3]
            sale_price   = sale[4]
            sale_date    = sale[6]
            product_name = sale[7]
            category     = sale[8] or 'Без категории'
            seller_name  = ''
            if has_seller and len(sale) >= 11:
                fn = sale[9] or ''
                ln = sale[10] or ''
                seller_name = f"{fn} {ln}".strip()

            date_value = 'Неизвестно'
            day_key    = None
            if sale_date:
                try:
                    day_key    = str(sale_date).split()[0]
                    date_value = format_date_display(day_key)
                except Exception:
                    date_value = str(sale_date)

            try:
                qty   = int(quantity)   if quantity   else 0
                price = float(sale_price) if sale_price else 0.0
            except (ValueError, TypeError):
                qty   = 0
                price = 0.0

            line_sum   = qty * price
            total_qty += qty
            total_sum += line_sum

            ws1.cell(row=data_row, column=1, value=date_value)
            ws1.cell(row=data_row, column=2, value=product_name)
            ws1.cell(row=data_row, column=3, value=category)
            c_qty   = ws1.cell(row=data_row, column=4, value=qty)
            c_price = ws1.cell(row=data_row, column=5, value=price)
            c_sum   = ws1.cell(row=data_row, column=6, value=line_sum)
            ws1.cell(row=data_row, column=7, value=shop_name_v)
            c_qty.number_format   = FMT_INT
            c_price.number_format = FMT_MONEY
            c_sum.number_format   = FMT_MONEY
            if has_seller:
                ws1.cell(row=data_row, column=8, value=seller_name)

            if category not in category_stats:
                category_stats[category] = {'quantity': 0, 'sum': 0}
            category_stats[category]['quantity'] += qty
            category_stats[category]['sum']      += line_sum

            if has_seller and seller_name:
                key = (seller_name, shop_name_v or '')
                if key not in seller_stats:
                    seller_stats[key] = {'quantity': 0, 'sum': 0}
                seller_stats[key]['quantity'] += qty
                seller_stats[key]['sum']      += line_sum

            if day_key:
                if day_key not in day_stats:
                    day_stats[day_key] = {'quantity': 0, 'sum': 0}
                day_stats[day_key]['quantity'] += qty
                day_stats[day_key]['sum']      += line_sum

            data_row += 1

        total_row = data_row
        ws1.cell(row=total_row, column=1, value='ИТОГО')
        c_tq = ws1.cell(row=total_row, column=4, value=total_qty)
        c_ts = ws1.cell(row=total_row, column=6, value=total_sum)
        c_tq.number_format = FMT_INT
        c_ts.number_format = FMT_MONEY
        for col in range(1, len(headers) + 1):
            ws1.cell(row=total_row, column=col).fill = TOTAL_FILL
            ws1.cell(row=total_row, column=col).font = TOTAL_FONT

        for col_cells in ws1.columns:
            length = max((len(str(cell.value or '')) for cell in col_cells), default=10)
            ws1.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 50)

        # ── Лист 2: По категориям + BarChart ────────────────────────────────
        ws2 = wb.create_sheet("По категориям")
        ws2['A1'] = "Статистика по категориям"
        ws2['A1'].font = TITLE_FONT

        stat_headers = ['Категория', 'Кол-во', 'Сумма', 'Доля%']
        for col, hdr in enumerate(stat_headers, 1):
            cell = ws2.cell(row=3, column=col, value=hdr)
            cell.font = HDR_FONT
            cell.fill = BLUE_FILL
            cell.alignment = Alignment(horizontal='center')

        stat_row = 4
        sorted_cats = sorted(category_stats.items(), key=lambda x: x[1]['sum'], reverse=True)
        for category, stats in sorted_cats:
            pct = (stats['sum'] / total_sum * 100) if total_sum > 0 else 0
            ws2.cell(row=stat_row, column=1, value=category)
            c2 = ws2.cell(row=stat_row, column=2, value=stats['quantity'])
            c3 = ws2.cell(row=stat_row, column=3, value=stats['sum'])
            c2.number_format = FMT_INT
            c3.number_format = FMT_MONEY
            ws2.cell(row=stat_row, column=4, value=round(pct, 1))
            stat_row += 1

        ws2.cell(row=stat_row, column=1, value='ИТОГО')
        c2t = ws2.cell(row=stat_row, column=2, value=total_qty)
        c3t = ws2.cell(row=stat_row, column=3, value=total_sum)
        c2t.number_format = FMT_INT
        c3t.number_format = FMT_MONEY
        for col in range(1, 5):
            ws2.cell(row=stat_row, column=col).fill = TOTAL_FILL
            ws2.cell(row=stat_row, column=col).font = TOTAL_FONT

        if 1 < len(sorted_cats) <= 30:
            chart = BarChart()
            chart.type    = "col"
            chart.title   = "Продажи по категориям"
            chart.y_axis.title = "Сумма (₽)"
            chart.y_axis.numFmt = '#,##0'
            chart.x_axis.title = "Категория"
            chart.style   = 10
            chart.width   = 20
            chart.height  = 12
            data_ref = Reference(ws2, min_col=3, min_row=3, max_row=stat_row - 1)
            cats_ref = Reference(ws2, min_col=1, min_row=4, max_row=stat_row - 1)
            chart.add_data(data_ref, titles_from_data=True)
            chart.set_categories(cats_ref)
            ws2.add_chart(chart, "F3")

        for col_cells in ws2.columns:
            length = max((len(str(cell.value or '')) for cell in col_cells), default=10)
            ws2.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 50)

        # ── Лист 3: По продавцам (если данные о продавце есть) ──────────────
        if has_seller and seller_stats:
            ws3 = wb.create_sheet("По продавцам")
            ws3['A1'] = "Статистика по продавцам"
            ws3['A1'].font = TITLE_FONT

            # добавлен «Ср. чек»
            sel_headers = ['Продавец', 'Магазин', 'Кол-во', 'Сумма', 'Ср. чек', 'Доля%']
            for col, hdr in enumerate(sel_headers, 1):
                cell = ws3.cell(row=3, column=col, value=hdr)
                cell.font = HDR_FONT
                cell.fill = BLUE_FILL
                cell.alignment = Alignment(horizontal='center')

            sel_row = 4
            for (name, shop), stats in sorted(seller_stats.items(), key=lambda x: x[1]['sum'], reverse=True):
                pct = (stats['sum'] / total_sum * 100) if total_sum > 0 else 0
                avg_chk = (stats['sum'] / stats['quantity']) if stats['quantity'] > 0 else 0
                ws3.cell(row=sel_row, column=1, value=name)
                ws3.cell(row=sel_row, column=2, value=shop)
                c3a = ws3.cell(row=sel_row, column=3, value=stats['quantity'])
                c3b = ws3.cell(row=sel_row, column=4, value=stats['sum'])
                c3c = ws3.cell(row=sel_row, column=5, value=round(avg_chk, 2))
                c3a.number_format = FMT_INT
                c3b.number_format = FMT_MONEY
                c3c.number_format = FMT_MONEY
                ws3.cell(row=sel_row, column=6, value=round(pct, 1))
                sel_row += 1

            ws3.cell(row=sel_row, column=1, value='ИТОГО')
            c3t1 = ws3.cell(row=sel_row, column=3, value=total_qty)
            c3t2 = ws3.cell(row=sel_row, column=4, value=total_sum)
            avg_total = (total_sum / total_qty) if total_qty > 0 else 0
            c3t3 = ws3.cell(row=sel_row, column=5, value=round(avg_total, 2))
            c3t1.number_format = FMT_INT
            c3t2.number_format = FMT_MONEY
            c3t3.number_format = FMT_MONEY
            for col in range(1, 7):
                ws3.cell(row=sel_row, column=col).fill = TOTAL_FILL
                ws3.cell(row=sel_row, column=col).font = TOTAL_FONT

            for col_cells in ws3.columns:
                length = max((len(str(cell.value or '')) for cell in col_cells), default=10)
                ws3.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 50)

        # ── Лист 4: По дням + BarChart (только при >1 дне) ──────────────────
        if len(day_stats) > 1:
            ws4 = wb.create_sheet("По дням")
            ws4['A1'] = "Динамика продаж по дням"
            ws4['A1'].font = TITLE_FONT

            day_headers = ['Дата', 'Кол-во', 'Сумма']
            for col, hdr in enumerate(day_headers, 1):
                cell = ws4.cell(row=3, column=col, value=hdr)
                cell.font = HDR_FONT
                cell.fill = BLUE_FILL
                cell.alignment = Alignment(horizontal='center')

            day_row = 4
            for day_key in sorted(day_stats.keys()):
                stats = day_stats[day_key]
                ws4.cell(row=day_row, column=1, value=format_date_display(day_key))
                c4a = ws4.cell(row=day_row, column=2, value=stats['quantity'])
                c4b = ws4.cell(row=day_row, column=3, value=stats['sum'])
                c4a.number_format = FMT_INT
                c4b.number_format = FMT_MONEY
                day_row += 1

            ws4.cell(row=day_row, column=1, value='ИТОГО')
            c4t1 = ws4.cell(row=day_row, column=2, value=total_qty)
            c4t2 = ws4.cell(row=day_row, column=3, value=total_sum)
            c4t1.number_format = FMT_INT
            c4t2.number_format = FMT_MONEY
            for col in range(1, 4):
                ws4.cell(row=day_row, column=col).fill = TOTAL_FILL
                ws4.cell(row=day_row, column=col).font = TOTAL_FONT

            if len(day_stats) <= 62:
                chart2 = BarChart()
                chart2.type   = "col"
                chart2.title  = "Продажи по дням"
                chart2.y_axis.title = "Сумма (₽)"
                chart2.y_axis.numFmt = '#,##0'
                chart2.x_axis.title = "Дата"
                chart2.style  = 10
                chart2.width  = 20
                chart2.height = 12
                data_ref2 = Reference(ws4, min_col=3, min_row=3, max_row=day_row - 1)
                cats_ref2 = Reference(ws4, min_col=1, min_row=4, max_row=day_row - 1)
                chart2.add_data(data_ref2, titles_from_data=True)
                chart2.set_categories(cats_ref2)
                ws4.add_chart(chart2, "E3")

            for col_cells in ws4.columns:
                length = max((len(str(cell.value or '')) for cell in col_cells), default=10)
                ws4.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 50)

        # ── Лист KPI: ключевые показатели ────────────────────────────────────
        ws_kpi = wb.create_sheet("KPI")
        ws_kpi['A1'] = "Ключевые показатели"
        ws_kpi['A1'].font = TITLE_FONT
        ws_kpi.cell(row=2, column=1, value=f"Период: {period}").font = Font(italic=True, size=10, color="6B7280")

        kpi_hdr_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        for i, h in enumerate(["Показатель", "Значение"], 1):
            c = ws_kpi.cell(row=4, column=i, value=h)
            c.font = HDR_FONT
            c.fill = kpi_hdr_fill
            c.alignment = Alignment(horizontal='center')

        avg_check = (total_sum / total_qty) if total_qty > 0 else 0
        best_cat_name, best_cat_rev = ("—", 0)
        if sorted_cats:
            best_cat_name, best_cat_stats = sorted_cats[0]
            best_cat_rev = best_cat_stats['sum']
        best_seller_name, best_seller_rev = ("—", 0)
        if seller_stats:
            best_seller_key = max(seller_stats.items(), key=lambda x: x[1]['sum'])
            best_seller_name = best_seller_key[0][0]
            best_seller_rev = best_seller_key[1]['sum']

        kpi_rows = [
            ("Выручка (₽)", round(total_sum, 2)),
            ("Продано единиц", total_qty),
            ("Средний чек (₽)", round(avg_check, 2)),
            ("Лучшая категория", f"{best_cat_name} — {best_cat_rev:,.2f} ₽"),
            ("Лучший продавец", f"{best_seller_name} — {best_seller_rev:,.2f} ₽" if best_seller_name != "—" else "—"),
        ]
        for r_idx, (label, val) in enumerate(kpi_rows, 5):
            ws_kpi.cell(row=r_idx, column=1, value=label)
            cell_v = ws_kpi.cell(row=r_idx, column=2, value=val)
            if isinstance(val, float):
                cell_v.number_format = '#,##0.00'
                cell_v.alignment = Alignment(horizontal='right')
            if r_idx % 2 == 0:
                for c in range(1, 3):
                    ws_kpi.cell(row=r_idx, column=c).fill = TOTAL_FILL
        ws_kpi.column_dimensions['A'].width = 24
        ws_kpi.column_dimensions['B'].width = 36

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        wb.save(temp_file.name)
        temp_file.close()
        return temp_file.name

    except Exception:
        return None

def format_product_display(product_name, quantity_sold, price, category=None):
    """Форматирование отображения товара"""
    display = f"🏷 {product_name}"
    if category:
        display += f" ({category})"
    display += f"\n📦 Количество: {quantity_sold} шт.\n💰 Цена: {format_currency(price)}"
    return display

def truncate_text(text, max_length=30):
    """Обрезка текста с многоточием"""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

def get_pagination_text(current_page, total_pages):
    """Текст для пагинации"""
    return f"Страница {current_page} из {total_pages}"

def safe_int_convert(value, default=0):
    """Безопасное преобразование в int"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def safe_float_convert(value, default=0.0):
    """Безопасное преобразование в float"""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def clean_phone_number(phone):
    """Очистка номера телефона"""
    if not phone:
        return ""

    # Удаляем все символы кроме цифр и +
    cleaned = ''.join(c for c in phone if c.isdigit() or c == '+')

    # Добавляем + если начинается с 7 или 8
    if cleaned.startswith('8'):
        cleaned = '+7' + cleaned[1:]
    elif cleaned.startswith('7') and not cleaned.startswith('+7'):
        cleaned = '+' + cleaned

    return cleaned

def get_stock_color_indicator(quantity, threshold=10):
    """Цветовой индикатор остатков"""
    if quantity < 2:
        return "🔴"  # Красный - критически мало (меньше 2)
    elif quantity <= 5:
        return "🟡"  # Желтый - мало (от 2 до 5)
    else:
        return "🟢"  # Зеленый - достаточно (больше 5)

def format_stock_display(product_name, quantity, price=None, category=None):
    """Форматирование отображения остатков"""
    indicator = get_stock_color_indicator(quantity)
    display = f"{indicator} {product_name}"
    if category:
        display += f" ({category})"
    display += f"\n📦 Остаток: {quantity} шт."
    if price:
        display += f"\n💰 Цена: {format_currency(price)}"
    return display