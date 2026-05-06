"""
Модуль для создания клавиатур и кнопок
"""
import os
import sqlite3
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from calendar import monthrange
from datetime import datetime, date
from env_manager import env_manager

# Получаем ID администратора из переменных окружения
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)

def main_menu(chat_id: int, user_shop: str = None):
    """Главное меню"""
    is_super_admin = env_manager.is_super_admin(chat_id)
    from db_utils import is_any_admin
    is_main_admin = is_any_admin(chat_id)
    
    from tenant_manager import tenant_manager
    conn = sqlite3.connect(tenant_manager.main_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM user_org_mapping WHERE telegram_id = ?", (chat_id,))
    mapping = cursor.fetchone()
    conn.close()
    
    is_org_admin = mapping and mapping[0] in ['owner', 'admin']
    
    buttons = [
        [
            InlineKeyboardButton(text="💰 ПРОДАЖА", callback_data="new_sale"),
        ],
    ]

    if is_org_admin or is_main_admin:
        buttons.append([InlineKeyboardButton(text="⚙️ Управление орг.", callback_data="admin_management")])
    
    # Системная панель доступна только главному супер-администратору
    if is_super_admin:
        buttons.append([InlineKeyboardButton(text="🔧 Системная панель", callback_data="system_admin_panel")])

    if not (is_org_admin or is_main_admin):
        buttons.append([InlineKeyboardButton(text="📦 ОСТАТКИ", callback_data="user_inventory_menu")])

    buttons.extend([
        [InlineKeyboardButton(text="📊 Отчеты", callback_data="reports")],
        [InlineKeyboardButton(text="🏆 Рейтинги", callback_data="user_rankings_menu")],
        [InlineKeyboardButton(text="📅 Мой график", callback_data="my_schedule")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="user_profile")],
        [InlineKeyboardButton(text="ℹ Помощь", callback_data="help")]
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def system_admin_menu():
    """Меню главного администратора бота"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="💰 Платежная система", callback_data="payment_system_admin"),
        InlineKeyboardButton(text="💾 Резервные копии", callback_data="backup_management"),
        InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin_users"),
        InlineKeyboardButton(text="🏢 Все организации", callback_data="list_all_orgs"),
        InlineKeyboardButton(text="🧪 Запустить тесты", callback_data="run_system_tests"),
    )
    builder.row(back_button("main_menu"))
    builder.adjust(1)
    return builder.as_markup()

def admin_management_menu(chat_id: int):
    """Меню управления для администратора организации"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="🛍 Упр. товарами", callback_data="products"),
        InlineKeyboardButton(text="📦 Упр. остатками", callback_data="manage_inventory"),
        InlineKeyboardButton(text="📝 Упр. продажами", callback_data="edit_sales"),
        InlineKeyboardButton(text="🎯 Упр. мотивацией", callback_data="admin_motivation"),
        InlineKeyboardButton(text="📋 Планы продаж", callback_data="admin_sales_plans"),
        InlineKeyboardButton(text="💰 Оклады и смены", callback_data="admin_salary_menu"),
        InlineKeyboardButton(text="🏆 Конкурсы", callback_data="contests_menu"),
        InlineKeyboardButton(text="👥 Упр. сотрудниками", callback_data="admin_users")
    )
    builder.add(back_button("main_menu"))
    builder.adjust(1)
    return builder.as_markup()

def products_menu():
    """Меню управления товарами (только для админа)"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_product"),
        InlineKeyboardButton(text="📥 Добавить списком", callback_data="bulk_import_products"),
        InlineKeyboardButton(text="📊 Импорт из Excel", callback_data="excel_import_products"),
        InlineKeyboardButton(text="📋 Список товаров", callback_data="list_products"),
        InlineKeyboardButton(text="📂 Категории", callback_data="categories_menu"),
        InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit_product"),
        InlineKeyboardButton(text="🗑 Удалить товар", callback_data="delete_product"),
        back_button("admin_management")
    )
    builder.adjust(2, 1, 1, 1, 2, 1)
    return builder.as_markup()

def inventory_menu():
    """Меню управления остатками"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="➕ Добавить остатки", callback_data="add_inventory"),
        InlineKeyboardButton(text="📋 Просмотр остатков", callback_data="user_inventory_view"),
        back_button("admin_management")
    )
    builder.adjust(1, 1, 1)
    return builder.as_markup()

def back_button(callback_data: str):
    """Кнопка назад"""
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)

def usage_mode_keyboard():
    """Выбор режима использования при регистрации"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Личное использование", callback_data="mode_personal")],
        [InlineKeyboardButton(text="🏢 Создать организацию", callback_data="mode_corporate")],
        [InlineKeyboardButton(text="🔗 Войти по приглашению", callback_data="mode_join")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_registration")]
    ])

def cancel_registration_keyboard():
    """Клавиатура для отмены регистрации"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить регистрацию", callback_data="cancel_registration")]
    ])

def safe_cb(prefix: str, value: str, max_bytes: int = 64) -> str:
    """Возвращает prefix+value, усечённое до max_bytes UTF-8 байт.

    Telegram отклоняет callback_data длиннее 64 байт. Кириллица занимает 2 байта
    на символ, поэтому длинные имена магазинов/категорий могут переполнить лимит.
    """
    prefix_len = len(prefix.encode('utf-8'))
    available = max_bytes - prefix_len
    if available <= 0:
        return prefix.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')
    value_encoded = value.encode('utf-8')
    if len(value_encoded) <= available:
        return prefix + value
    return prefix + value_encoded[:available].decode('utf-8', errors='ignore')

def resolve_cb_name(partial: str, candidates) -> str:
    """Находит полное имя из списка по возможно усечённому callback-значению.

    Если partial есть в списке — возвращает его. Иначе ищет кандидата,
    у которого полное имя начинается с partial (обратная операция к safe_cb).
    """
    if partial in candidates:
        return partial
    for c in candidates:
        if c.startswith(partial):
            return c
    return partial

def generate_calendar(year=None, month=None, cancel_callback: str = None, prefix="cal_"):
    """Генерация календаря для выбора даты"""
    if year is None: year = date.today().year
    if month is None: month = date.today().month
    
    month_names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    header = f"{month_names[month - 1]} {year}"
    days_of_week = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    
    _, days_in_month = monthrange(year, month)
    first_day_weekday = date(year, month, 1).weekday()
    builder = InlineKeyboardBuilder()
    
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"{prefix}nav_{prev_year}_{prev_month}"),
        InlineKeyboardButton(text=header, callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"{prefix}nav_{next_year}_{next_month}")
    )
    builder.row(*[InlineKeyboardButton(text=day, callback_data="ignore") for day in days_of_week])
    
    week = [InlineKeyboardButton(text=" ", callback_data="ignore")] * first_day_weekday
    for day in range(1, days_in_month + 1):
        week.append(InlineKeyboardButton(text=str(day), callback_data=f"{prefix}date_{year}-{month:02d}-{day:02d}"))
        if len(week) == 7:
            builder.row(*week)
            week = []
    
    if week:
        while len(week) < 7: week.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
        builder.row(*week)
    
    if cancel_callback: builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_callback))
    return builder.as_markup()

def create_selection_keyboard(items, callback_prefix, max_per_row=2, add_back=True, back_callback="main_menu"):
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.add(InlineKeyboardButton(text=item, callback_data=safe_cb(f"{callback_prefix}_", item)))
    if add_back: builder.add(back_button(back_callback))
    builder.adjust(*[max_per_row] * (len(items) // max_per_row + (1 if len(items) % max_per_row else 0)))
    if add_back: builder.adjust(1)
    return builder.as_markup()

def create_confirm_keyboard(confirm_callback, cancel_callback="main_menu"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data=confirm_callback), InlineKeyboardButton(text="❌ Нет", callback_data=cancel_callback)]])

def create_registration_selection_keyboard(items, callback_prefix, create_new_callback):
    buttons = [[InlineKeyboardButton(text=item, callback_data=safe_cb(f"{callback_prefix}_", item))] for item in items[:8]]
    buttons.append([InlineKeyboardButton(text="➕ Создать новую", callback_data=create_new_callback)])
    buttons.append([InlineKeyboardButton(text="❌ Отменить регистрацию", callback_data="cancel_registration")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def create_profile_edit_selection_keyboard(items, callback_prefix, create_new_callback):
    buttons = [[InlineKeyboardButton(text=item, callback_data=safe_cb(f"{callback_prefix}_", item))] for item in items[:8]]
    buttons.append([InlineKeyboardButton(text="➕ Создать новый", callback_data=create_new_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def timezone_keyboard():
    from timezone_utils import get_common_timezones
    timezones = get_common_timezones()
    buttons = [[InlineKeyboardButton(text=name, callback_data=f"set_timezone_{zone}")] for name, zone in timezones.items()]
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="user_profile")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
