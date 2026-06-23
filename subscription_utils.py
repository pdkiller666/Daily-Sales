"""
Утилиты для проверки лимитов подписки.

Логика:
  - Суп-адмн → безлимит
  - Org-пользователь → лимиты берутся из плана ОРГАНИЗАЦИИ (organizations.subscription_plan → shop_bot.db subscription_plans)
  - Личный пользователь → лимиты берутся из его личной подписки в shop_bot.db
  - Надстройки (add-ons) суммируются поверх базовых лимитов тарифа
"""

import logging
import sqlite3
import time as _time
from datetime import datetime, timedelta
from env_manager import env_manager

try:
    from billing_utils import GRACE_DAYS as _GRACE_DAYS
except Exception:
    _GRACE_DAYS = 3  # soft-expiry окно (дни) — синхронно с billing_utils.GRACE_DAYS

_logger = logging.getLogger(__name__)

SHOP_BOT_DB = 'data/shop_bot.db'
MAIN_DB = 'data/main.db'

_UNLIMITED = {
    'max_products': -1,
    'max_shops': -1,
    'max_sales_per_month': -1,
    'can_export_reports': True,
    'can_view_analytics': True,
    'can_use_notifications': True,
    'can_use_integrations': True,
}

_FREE_FALLBACK = {
    'max_products': 50,
    'max_shops': 1,
    'max_sales_per_month': 100,
    'can_export_reports': False,
    'can_view_analytics': False,
    'can_use_notifications': False,
    'can_use_integrations': False,
}


def _plan_limits_from_shop_bot(plan_name):
    """Читает ЖЁСТКИЕ лимиты плана из централизованной shop_bot.db по названию.

    can_* флаги (экспорт/аналитика/уведомления/интеграции) НЕ читаются из
    subscription_plans — они определяются исключительно модульной биллинг-системой
    (см. _apply_billing_modules). Колонки can_* в БД оставлены только для обратной
    совместимости и больше не являются источником истины."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT max_products, max_shops, max_sales_per_month "
            "FROM subscription_plans WHERE name = ? AND is_active = 1",
            (plan_name,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                'max_products': row[0],
                'max_shops': row[1],
                'max_sales_per_month': row[2],
            }
    except Exception:
        pass
    return None


def _get_org_plan_for_user(telegram_id):
    """Возвращает название тарифного плана организации пользователя или None если не в org.
    Возвращает 'Бесплатный' если срок подписки истёк."""
    try:
        conn = sqlite3.connect(MAIN_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT o.subscription_plan, o.subscription_end FROM organizations o "
            "JOIN user_org_mapping m ON o.id = m.org_id "
            "WHERE m.telegram_id = ?",
            (telegram_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            plan_name = row[0] or 'Бесплатный'
            subscription_end = row[1]
            if subscription_end and plan_name not in ('Бесплатный', 'free', None):
                try:
                    end_dt = datetime.fromisoformat(subscription_end)
                    # Soft-expiry: план держится ещё _GRACE_DAYS дней после end_date.
                    if datetime.now() > end_dt + timedelta(days=_GRACE_DAYS):
                        return 'Бесплатный'
                except Exception:
                    pass
            return plan_name
    except Exception:
        pass
    return None


def _get_personal_plan(telegram_id):
    """Возвращает название плана из личной подписки пользователя в shop_bot.db."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT s.plan_type FROM subscriptions s "
            "JOIN users u ON s.user_id = u.id "
            "WHERE u.telegram_id = ? AND s.end_date > ? "
            "ORDER BY s.end_date DESC LIMIT 1",
            # Soft-expiry: личный план держится ещё _GRACE_DAYS дней после истечения.
            (telegram_id, (datetime.now() - timedelta(days=_GRACE_DAYS)).strftime('%Y-%m-%d %H:%M:%S'))
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    return 'Бесплатный'


def _has_active_trial(telegram_id) -> bool:
    """Возвращает True если у пользователя есть активная пробная подписка (is_trial=1)."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM subscriptions s "
            "JOIN users u ON s.user_id = u.id "
            "WHERE u.telegram_id = ? AND s.is_trial = 1 AND s.end_date > ? LIMIT 1",
            (telegram_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


# TTL-кеш для get_plan_limits: {telegram_id: (limits_dict, timestamp)}
_plan_cache: dict = {}
_PLAN_CACHE_TTL = 120  # 2 минуты — план меняется редко


def invalidate_plan_cache(telegram_id: int) -> None:
    """Сбросить кеш get_plan_limits() после изменения подписки."""
    _plan_cache.pop(telegram_id, None)


def _apply_billing_modules(telegram_id: int, limits: dict) -> dict:
    """Устанавливает can_* флаги ИСКЛЮЧИТЕЛЬНО из модульной биллинг-системы.

    Вызывается для обычных пользователей (не super_admin, не trial). Колонки can_*
    в subscription_plans больше НЕ являются источником истины — флаги доступа
    полностью определяются модулями (billing_utils.has_module).

    При недоступности billing_utils флаги фейлятся ЗАКРЫТО (всё False), а не
    откатываются к legacy-значениям из тарифа."""
    try:
        from billing_utils import has_module as _bm
        limits['can_view_analytics']    = _bm(telegram_id, 'analytics')
        limits['can_export_reports']    = _bm(telegram_id, 'analytics')
        limits['can_use_notifications'] = _bm(telegram_id, 'notifications')
        limits['can_use_integrations']  = _bm(telegram_id, 'integrations')
    except Exception:
        _logger.exception("billing_utils недоступен — can_* флаги фейлятся закрыто")
        limits['can_view_analytics']    = False
        limits['can_export_reports']    = False
        limits['can_use_notifications'] = False
        limits['can_use_integrations']  = False
    return limits


def get_plan_limits(telegram_id):
    """
    Главная функция получения лимитов для пользователя.
    Принимает telegram_id (не внутренний user_id).
    Результат кешируется на _PLAN_CACHE_TTL секунд.

    can_* feature flags определяются модульной биллинг-системой (billing_utils.has_module).
    Жёсткие лимиты (max_products, max_shops, max_sales_per_month) берутся из subscription_plans.
    """
    if env_manager.is_super_admin(telegram_id):
        return _UNLIMITED

    now = _time.time()
    cached = _plan_cache.get(telegram_id)
    if cached and now - cached[1] < _PLAN_CACHE_TTL:
        return cached[0]

    # Пробный период = полный функционал
    if _has_active_trial(telegram_id):
        _plan_cache[telegram_id] = (_UNLIMITED, now)
        return _UNLIMITED

    # Сначала проверяем: в org?
    org_plan = _get_org_plan_for_user(telegram_id)
    if org_plan is not None:
        limits = dict(_plan_limits_from_shop_bot(org_plan) or _FREE_FALLBACK)
    else:
        # Личный пользователь
        plan_name = _get_personal_plan(telegram_id)
        limits = dict(_plan_limits_from_shop_bot(plan_name) or _FREE_FALLBACK)

    # Перекрываем can_* флаги модульной биллинг-системой
    limits = _apply_billing_modules(telegram_id, limits)

    _plan_cache[telegram_id] = (limits, now)
    return limits


def get_subscription_days_remaining(telegram_id) -> "int | None":
    """
    Возвращает количество дней до истечения подписки, или None если лимит неограничен / нет подписки.
    Возвращает 0 если подписка уже истекла в этот же день.
    Используется только для вывода баннера — не для ограничения доступа.
    """
    if env_manager.is_super_admin(telegram_id):
        return None
    if _has_active_trial(telegram_id):
        try:
            conn = sqlite3.connect(SHOP_BOT_DB)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT s.end_date FROM subscriptions s "
                "JOIN users u ON s.user_id = u.id "
                "WHERE u.telegram_id = ? AND s.is_trial = 1 AND s.end_date > ? "
                "ORDER BY s.end_date DESC LIMIT 1",
                (telegram_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                end_dt = datetime.fromisoformat(row[0])
                return max(0, (end_dt - datetime.now()).days)
        except Exception:
            pass
        return None
    # Проверяем org подписку (только owner)
    try:
        conn = sqlite3.connect(MAIN_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT o.subscription_plan, o.subscription_end FROM organizations o "
            "JOIN user_org_mapping m ON o.id = m.org_id "
            "WHERE m.telegram_id = ? AND m.role = 'owner'",
            (telegram_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            plan_name = row[0] or 'Бесплатный'
            subscription_end = row[1]
            if subscription_end and plan_name not in ('Бесплатный', 'free', None):
                try:
                    end_dt = datetime.fromisoformat(subscription_end)
                    return max(0, (end_dt - datetime.now()).days)
                except Exception:
                    pass
    except Exception:
        pass
    # Проверяем личную подписку
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT s.end_date FROM subscriptions s "
            "JOIN users u ON s.user_id = u.id "
            "WHERE u.telegram_id = ? AND s.is_trial = 0 AND s.end_date > ? "
            "ORDER BY s.end_date DESC LIMIT 1",
            (telegram_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            end_dt = datetime.fromisoformat(row[0])
            return max(0, (end_dt - datetime.now()).days)
    except Exception:
        pass
    return None


def _get_addon_totals_for_user(telegram_id: int) -> dict:
    """Получить суммарные активные надстройки пользователя из shop_bot.db."""
    try:
        from database import Database
        db = Database(SHOP_BOT_DB)
        return db.get_addon_totals(telegram_id)
    except Exception as e:
        _logger.debug(f"_get_addon_totals_for_user: {e}")
        return {}


def check_product_limit(telegram_id):
    """Проверка лимита на количество товаров (с учётом надстроек). Возвращает (ok: bool, message: str|None)."""
    if env_manager.is_super_admin(telegram_id):
        return True, None

    limits = get_plan_limits(telegram_id)
    if limits['max_products'] == -1:
        return True, None

    # Учитываем надстройки: extra_products (каждая единица = +100 товаров)
    addons = _get_addon_totals_for_user(telegram_id)
    addon_extra = addons.get('extra_products', 0) * 100
    effective_limit = limits['max_products'] + addon_extra

    from tenant_manager import tenant_manager
    from database import Database
    db_path = tenant_manager.get_user_db_path(telegram_id)
    db = Database(db_path)
    current = len(db.get_all_products())

    if current >= effective_limit:
        base_msg = (
            f"❌ Достигнут лимит товаров по вашему тарифу: {effective_limit}.\n"
            f"Перейдите в раздел «🔔 Подписка» для улучшения тарифа"
        )
        base_msg += " или купите надстройку «+100 товаров»." if addon_extra == 0 else "."
        return False, base_msg
    return True, None


def check_sales_limit(telegram_id):
    """Проверка лимита продаж в месяц. Возвращает (ok: bool, message: str|None)."""
    if env_manager.is_super_admin(telegram_id):
        return True, None

    limits = get_plan_limits(telegram_id)
    if limits['max_sales_per_month'] == -1:
        return True, None

    from tenant_manager import tenant_manager
    from database import Database
    db_path = tenant_manager.get_user_db_path(telegram_id)
    db = Database(db_path)

    user = db.get_user(telegram_id)
    if not user:
        return True, None
    user_id = user[0]

    month_start = datetime.now().strftime('%Y-%m-01')
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM sales WHERE user_id = ? AND sale_date >= ?",
            (user_id, month_start)
        )
        count = cursor.fetchone()[0]
        conn.close()
    except Exception:
        return True, None

    if count >= limits['max_sales_per_month']:
        return False, (
            f"❌ Достигнут лимит продаж в месяц по вашему тарифу: {limits['max_sales_per_month']}.\n"
            f"Перейдите в раздел «🔔 Подписка» для улучшения тарифа."
        )
    return True, None


def check_shop_limit(telegram_id):
    """Проверка лимита на количество магазинов (с учётом надстроек). Возвращает (ok: bool, message: str|None)."""
    if env_manager.is_super_admin(telegram_id):
        return True, None

    limits = get_plan_limits(telegram_id)
    if limits['max_shops'] == -1:
        return True, None

    # Учитываем надстройки: extra_shops (каждая единица = +1 магазин)
    addons = _get_addon_totals_for_user(telegram_id)
    addon_extra = addons.get('extra_shops', 0)
    effective_limit = limits['max_shops'] + addon_extra

    from tenant_manager import tenant_manager
    from database import Database
    db_path = tenant_manager.get_user_db_path(telegram_id)
    db = Database(db_path)
    shops = db.get_all_shops()
    if len(shops) >= effective_limit:
        base_msg = (
            f"❌ Достигнут лимит магазинов по вашему тарифу: {effective_limit}.\n"
            f"Перейдите в раздел «🔔 Подписка» для улучшения тарифа"
        )
        base_msg += " или купите надстройку «Доп. магазин»." if addon_extra == 0 else "."
        return False, base_msg
    return True, None


def check_integrations_permission(telegram_id):
    """Проверка разрешения на Google Sheets и другие интеграции (модуль 'integrations')."""
    if env_manager.is_super_admin(telegram_id):
        return True
    try:
        from billing_utils import has_module
        return has_module(telegram_id, 'integrations')
    except Exception:
        return get_plan_limits(telegram_id).get('can_use_integrations', False)


def _get_org_plan_by_db_path(org_db: str):
    """Возвращает название плана организации по пути её db_file.
    Используется для email-only пользователей (synthetic tg_id), которых нет в user_org_mapping."""
    try:
        conn = sqlite3.connect(MAIN_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT subscription_plan, subscription_end FROM organizations WHERE db_file = ?",
            (org_db,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            plan_name = row[0] or 'Бесплатный'
            subscription_end = row[1]
            if subscription_end and plan_name not in ('Бесплатный', 'free', None):
                try:
                    end_dt = datetime.fromisoformat(subscription_end)
                    # Soft-expiry: план держится ещё _GRACE_DAYS дней после end_date.
                    if datetime.now() > end_dt + timedelta(days=_GRACE_DAYS):
                        return 'Бесплатный'
                except Exception:
                    pass
            return plan_name
    except Exception:
        pass
    return None


def check_export_permission(telegram_id, org_db: str = None):
    """Проверка разрешения на экспорт отчётов (модуль 'analytics').
    org_db — путь к БД организации; обязателен для email-only пользователей (tg_id < 0)."""
    if env_manager.is_super_admin(telegram_id):
        return True
    # Email-only users have synthetic (negative) tg_id — look up via org db path.
    # У них нет биллинг-модулей (модули привязаны к реальному telegram_id), поэтому
    # для legacy email-only пути источником истины остаётся колонка can_export_reports
    # тарифа. _plan_limits_from_shop_bot отдаёт только жёсткие лимиты, поэтому читаем
    # флаг напрямую из subscription_plans.
    if telegram_id < 0 and org_db:
        plan_name = _get_org_plan_by_db_path(org_db)
        if plan_name is None:
            return False
        try:
            conn = sqlite3.connect(SHOP_BOT_DB)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT can_export_reports FROM subscription_plans "
                "WHERE name = ? AND is_active = 1",
                (plan_name,)
            )
            row = cursor.fetchone()
            conn.close()
            return bool(row[0]) if row else False
        except Exception:
            return False
    try:
        from billing_utils import has_module
        return has_module(telegram_id, 'analytics')
    except Exception:
        return get_plan_limits(telegram_id)['can_export_reports']


def check_analytics_permission(telegram_id):
    """Проверка разрешения на расширенную аналитику (модуль 'analytics')."""
    if env_manager.is_super_admin(telegram_id):
        return True
    try:
        from billing_utils import has_module
        return has_module(telegram_id, 'analytics')
    except Exception:
        return get_plan_limits(telegram_id)['can_view_analytics']


def check_notifications_permission(telegram_id):
    """Проверка разрешения на систему уведомлений (модуль 'notifications')."""
    if env_manager.is_super_admin(telegram_id):
        return True
    try:
        from billing_utils import has_module
        return has_module(telegram_id, 'notifications')
    except Exception:
        return get_plan_limits(telegram_id)['can_use_notifications']


def check_team_permission(telegram_id):
    """Проверка разрешения на командные функции (модуль 'team'): оклады, графики, отсутствия."""
    if env_manager.is_super_admin(telegram_id):
        return True
    try:
        from billing_utils import has_module
        return has_module(telegram_id, 'team')
    except Exception:
        return False


def check_plans_motivation_permission(telegram_id):
    """Проверка разрешения на планы продаж и мотивацию (модуль 'plans_motivation')."""
    if env_manager.is_super_admin(telegram_id):
        return True
    try:
        from billing_utils import has_module
        return has_module(telegram_id, 'plans_motivation')
    except Exception:
        return False


def get_subscription_warning_message(telegram_id):
    """Предупреждающее сообщение о недоступности функции."""
    return (
        "⚠️ <b>Функция не подключена</b>\n\n"
        "Подключите нужный модуль в веб-кабинете:\n"
        "📊 <b>Аналитика</b> — отчёты, экспорт, рейтинги\n"
        "🔗 <b>Интеграции</b> — Google Таблицы и другие\n"
        "🔔 <b>Уведомления</b> — push и рассылки\n\n"
        "Перейдите в <b>Подписка → Модули</b> для подключения."
    )
