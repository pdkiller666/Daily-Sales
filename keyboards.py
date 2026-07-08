"""
Модуль для создания клавиатур и кнопок
"""
import os
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from calendar import monthrange
from datetime import datetime, date
from env_manager import env_manager

# Получаем ID администратора из переменных окружения
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)

# Кэш URL веб-интерфейса — читается из shop_bot.db, TTL 60 сек
_web_url_cache: dict = {"url": None, "ts": 0.0}
_WEB_URL_TTL = 60.0

def _get_web_interface_url() -> str | None:
    """Вернуть URL веб-интерфейса из кэша; обновить если устарел."""
    global _web_url_cache
    now = time.monotonic()
    if now - _web_url_cache["ts"] > _WEB_URL_TTL:
        try:
            from database import Database
            db = Database("data/shop_bot.db")
            _web_url_cache["url"] = db.get_web_interface_url()
        except Exception:
            pass
        finally:
            try:
                db.close()
            except Exception:
                pass
        _web_url_cache["ts"] = now
    return _web_url_cache["url"]

def invalidate_web_url_cache() -> None:
    """Сбросить кэш — вызывать после изменения URL супер-админом."""
    _web_url_cache["ts"] = 0.0

def main_menu(chat_id: int, user_shop: str = None):
    """Главное меню"""
    is_super_admin = env_manager.is_super_admin(chat_id)
    from db_utils import is_any_admin
    is_main_admin = is_any_admin(chat_id)  # уже кеширован (TTL 60s), без sqlite3.connect

    buttons = [
        [
            InlineKeyboardButton(text="💰 Продажа", callback_data="new_sale"),
        ],
    ]

    if is_main_admin:
        buttons.append([InlineKeyboardButton(text="⚙️ Управление орг.", callback_data="admin_management")])
    
    # Системная панель доступна только главному супер-администратору
    if is_super_admin:
        buttons.append([InlineKeyboardButton(text="🔧 Системная панель", callback_data="system_admin_panel")])

    if not is_main_admin:
        buttons.append([InlineKeyboardButton(text="📦 Остатки", callback_data="user_inventory_menu")])
        buttons.append([InlineKeyboardButton(text="📝 Мои продажи", callback_data="edit_sales_start")])

    try:
        from billing_utils import has_module as _hm_pkg_menu
        if _hm_pkg_menu(chat_id, "crm"):
            buttons.append([InlineKeyboardButton(text="🎫 Абонементы", callback_data="packages_hub")])
    except Exception:
        pass

    web_url = _get_web_interface_url()
    buttons.extend([
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="analytics_hub")],
        [InlineKeyboardButton(text="📌 Задачи", callback_data="tasks_menu")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="user_profile")],
    ])
    if web_url:
        buttons.append([InlineKeyboardButton(text="🌐 Веб-интерфейс", callback_data="web_open_hub")])
    buttons.append([InlineKeyboardButton(text="ℹ Помощь", callback_data="help")])

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
        InlineKeyboardButton(text="🌐 Веб-интерфейс URL", callback_data="set_web_interface_url"),
    )
    builder.row(back_button("main_menu"))
    builder.adjust(1)
    return builder.as_markup()

def admin_management_menu(chat_id: int):
    """Меню управления для администратора организации"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="📦 Каталог товаров", callback_data="catalog_menu"),
        InlineKeyboardButton(text="📝 Упр. продажами", callback_data="edit_sales"),
        InlineKeyboardButton(text="🎯 Мотивация", callback_data="motivation_hub"),
        InlineKeyboardButton(text="👥 Персонал", callback_data="personnel_hub"),
        InlineKeyboardButton(text="🏪 Упр. магазинами", callback_data="admin_shops"),
        InlineKeyboardButton(text="📊 Google Sheets", callback_data="integration_menu"),
    )
    builder.add(back_button("main_menu"))
    builder.adjust(1)
    return builder.as_markup()


def personnel_hub_menu(include_invite: bool = False):
    """Хаб персонала: сотрудники + оклады + рассылка + отсутствия + приглашение"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="👥 Управление сотрудниками", callback_data="admin_users"),
        InlineKeyboardButton(text="💰 Оклады и смены", callback_data="admin_salary_menu"),
        InlineKeyboardButton(text="📨 Рассылка сотрудникам", callback_data="admin_send_notification"),
        InlineKeyboardButton(text="📋 Отсутствия сотрудников", callback_data="abs_admin"),
    )
    if include_invite:
        builder.add(InlineKeyboardButton(text="📩 Создать приглашение", callback_data="generate_invite"))
    builder.add(back_button("admin_management"))
    builder.adjust(1)
    return builder.as_markup()

def catalog_menu():
    """Меню каталога: товары + остатки"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="🛍 Управление товарами", callback_data="products"),
        InlineKeyboardButton(text="📦 Управление остатками", callback_data="manage_inventory"),
        back_button("admin_management"),
    )
    builder.adjust(1)
    return builder.as_markup()

def edit_sales_hub():
    """Хаб управления продажами: редактировать + массовое удаление + возвраты"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="✏️ Редактировать / удалить", callback_data="edit_sales_start"),
        InlineKeyboardButton(text="↩️ Возвраты товара",         callback_data="returns_menu"),
        InlineKeyboardButton(text="🗑 Удалить за период",        callback_data="sales_bulk_delete"),
        back_button("admin_management"),
    )
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
        back_button("catalog_menu")
    )
    builder.adjust(2, 1, 1, 1, 2, 1)
    return builder.as_markup()


def motivation_hub_menu():
    """Меню мотивации: система мотивации + планы продаж + конкурсы"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="🎯 Система мотивации", callback_data="admin_motivation"),
        InlineKeyboardButton(text="📋 Планы продаж", callback_data="admin_sales_plans"),
        InlineKeyboardButton(text="🏆 Конкурсы", callback_data="contests_menu"),
        back_button("admin_management"),
    )
    builder.adjust(1)
    return builder.as_markup()


def team_hub_menu():
    """Меню команды: сотрудники + оклады"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="👥 Управление сотрудниками", callback_data="admin_users"),
        InlineKeyboardButton(text="💰 Оклады и смены", callback_data="admin_salary_menu"),
        back_button("admin_management"),
    )
    builder.adjust(1)
    return builder.as_markup()


def analytics_hub_menu(is_admin: bool = False):
    """Меню аналитики: дашборд + отчёты + планы + конкурсы + рейтинги"""
    rankings_cb = "rankings_menu" if is_admin else "user_rankings_menu"
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="📈 Отчёты по продажам", callback_data="reports"),
        InlineKeyboardButton(text="📋 Мои планы", callback_data="my_plans"),
        InlineKeyboardButton(text="🏆 Мои конкурсы", callback_data="my_contests"),
        InlineKeyboardButton(text="🥇 Рейтинги", callback_data=rankings_cb),
        back_button("main_menu"),
    )
    builder.adjust(1)
    return builder.as_markup()


def back_button(callback_data: str, text: str = "⬅️ Назад"):
    """Единая кнопка «Назад» (roadmap 1.3).

    text позволяет задать свою подпись (напр. «⬅️ К списку»), сохраняя
    единый стиль стрелки. По умолчанию — «⬅️ Назад».
    """
    return InlineKeyboardButton(text=text, callback_data=callback_data)

def home_button():
    """Единая кнопка возврата в главное меню"""
    return InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")

def usage_mode_keyboard():
    """Выбор режима использования при регистрации"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Личное использование", callback_data="mode_personal")],
        [InlineKeyboardButton(text="🏢 Создать организацию", callback_data="mode_corporate")],
        [InlineKeyboardButton(text="🔗 Войти по приглашению", callback_data="mode_join")],
        [InlineKeyboardButton(text="❌ Отменить регистрацию", callback_data="cancel_registration")]
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
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="user_profile")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
