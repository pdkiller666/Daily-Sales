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


def format_date_for_user(date_str, telegram_id):
    """
    Форматирование даты с автоматическим получением часового пояса пользователя

    Args:
        date_str: Дата в виде строки или объекта datetime
        telegram_id: Telegram ID пользователя

    Returns:
        Отформатированная строка с датой в часовом поясе пользователя
    """
    global _shop_db_for_tz
    if _shop_db_for_tz is None:
        from database import Database
        _shop_db_for_tz = Database('data/shop_bot.db')
    user_timezone = _shop_db_for_tz.get_user_timezone(telegram_id)
    return format_date_display(date_str, user_timezone)

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
    """Генерация Excel отчета по продажам"""
    try:
        wb = Workbook()
        ws1 = wb.active
        ws1.title = "Детальный отчет"

        header_font = Font(bold=True, size=14)
        ws1['A1'] = title
        ws1['A1'].font = header_font
        ws1['A2'] = f"Период: {period}"
        if shop_filter:
            ws1['A3'] = f"Магазин: {shop_filter}"

        headers = ['Дата', 'Товар', 'Категория', 'Количество', 'Цена за ед.', 'Сумма', 'Магазин']
        start_row = 5
        for col, header in enumerate(headers, 1):
            cell = ws1.cell(row=start_row, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")

        row = start_row + 1
        category_stats = {}
        total_sum = 0

        for sale in data:
            # Структура из get_sales_report():
            # id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
            # user_id[5], sale_date[6], product_name[7], category[8], first_name[9], last_name[10]
            # Структура из get_user_sales():
            # id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
            # user_id[5], sale_date[6], product_name[7], category[8]
            if len(sale) >= 9:
                shop_name = sale[2]
                quantity_sold = sale[3]
                sale_price = sale[4]
                sale_date = sale[6]
                product_name = sale[7]
                category = sale[8]
            else:
                continue

            date_value = "Неизвестно"
            if sale_date:
                try:
                    date_value = format_date_display(str(sale_date).split()[0])
                except (AttributeError, IndexError):
                    date_value = str(sale_date)

            try:
                quantity_sold = int(quantity_sold) if quantity_sold else 0
                price = float(sale_price) if sale_price else 0
            except (ValueError, TypeError):
                quantity_sold = 0
                price = 0

            ws1.cell(row=row, column=1, value=date_value)
            ws1.cell(row=row, column=2, value=product_name)
            ws1.cell(row=row, column=3, value=category)
            ws1.cell(row=row, column=4, value=quantity_sold)
            ws1.cell(row=row, column=5, value=price)
            ws1.cell(row=row, column=6, value=quantity_sold * price)
            ws1.cell(row=row, column=7, value=shop_name)

            if category not in category_stats:
                category_stats[category] = {'quantity': 0, 'sum': 0}
            category_stats[category]['quantity'] += quantity_sold
            category_stats[category]['sum'] += quantity_sold * price
            total_sum += quantity_sold * price

            row += 1

        ws2 = wb.create_sheet("Статистика по категориям")
        ws2['A1'] = "Статистика по категориям"
        ws2['A1'].font = Font(bold=True, size=14)

        stat_headers = ['Категория', 'Количество', 'Сумма', 'Доля от общей суммы']
        for col, header in enumerate(stat_headers, 1):
            cell = ws2.cell(row=3, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")

        stat_row = 4
        for category, stats in category_stats.items():
            percentage = (stats['sum'] / total_sum * 100) if total_sum > 0 else 0
            ws2.cell(row=stat_row, column=1, value=category)
            ws2.cell(row=stat_row, column=2, value=stats['quantity'])
            ws2.cell(row=stat_row, column=3, value=stats['sum'])
            ws2.cell(row=stat_row, column=4, value=f"{percentage:.1f}%")
            stat_row += 1

        for ws in [ws1, ws2]:
            for column_cells in ws.columns:
                length = max((len(str(cell.value or '')) for cell in column_cells), default=10)
                ws.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 50)

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