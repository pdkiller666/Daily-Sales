"""
Утилиты для проверки лимитов подписки.

Логика:
  - Суп-адмн → безлимит
  - Org-пользователь → лимиты берутся из плана ОРГАНИЗАЦИИ (organizations.subscription_plan → shop_bot.db subscription_plans)
  - Личный пользователь → лимиты берутся из его личной подписки в shop_bot.db
"""

import sqlite3
import time as _time
from datetime import datetime
from env_manager import env_manager

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
    """Читает лимиты плана из централизованной shop_bot.db по названию плана."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT max_products, max_shops, max_sales_per_month, "
            "can_export_reports, can_view_analytics, can_use_notifications, can_use_integrations "
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
                'can_export_reports': bool(row[3]),
                'can_view_analytics': bool(row[4]),
                'can_use_notifications': bool(row[5]),
                'can_use_integrations': bool(row[6]),
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
                    if datetime.now() > end_dt:
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
            (telegram_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
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


def get_plan_limits(telegram_id):
    """
    Главная функция получения лимитов для пользователя.
    Принимает telegram_id (не внутренний user_id).
    Результат кешируется на _PLAN_CACHE_TTL секунд.
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
        limits = _plan_limits_from_shop_bot(org_plan) or _FREE_FALLBACK
    else:
        # Личный пользователь
        plan_name = _get_personal_plan(telegram_id)
        limits = _plan_limits_from_shop_bot(plan_name) or _FREE_FALLBACK

    _plan_cache[telegram_id] = (limits, now)
    return limits


def check_product_limit(telegram_id):
    """Проверка лимита на количество товаров. Возвращает (ok: bool, message: str|None)."""
    if env_manager.is_super_admin(telegram_id):
        return True, None

    limits = get_plan_limits(telegram_id)
    if limits['max_products'] == -1:
        return True, None

    from tenant_manager import tenant_manager
    from database import Database
    db_path = tenant_manager.get_user_db_path(telegram_id)
    db = Database(db_path)
    current = len(db.get_all_products())

    if current >= limits['max_products']:
        return False, (
            f"❌ Достигнут лимит товаров по вашему тарифу: {limits['max_products']}.\n"
            f"Перейдите в раздел «🔔 Подписка» для улучшения тарифа."
        )
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
    """Проверка лимита на количество магазинов. Возвращает (ok: bool, message: str|None)."""
    if env_manager.is_super_admin(telegram_id):
        return True, None

    limits = get_plan_limits(telegram_id)
    if limits['max_shops'] == -1:
        return True, None

    from tenant_manager import tenant_manager
    from database import Database
    db_path = tenant_manager.get_user_db_path(telegram_id)
    db = Database(db_path)
    shops = db.get_all_shops()
    if len(shops) >= limits['max_shops']:
        return False, (
            f"❌ Достигнут лимит магазинов по вашему тарифу: {limits['max_shops']}.\n"
            f"Перейдите в раздел «🔔 Подписка» для улучшения тарифа."
        )
    return True, None


def check_integrations_permission(telegram_id):
    """Проверка разрешения на Google Sheets и другие интеграции."""
    if env_manager.is_super_admin(telegram_id):
        return True
    return get_plan_limits(telegram_id).get('can_use_integrations', False)


def check_export_permission(telegram_id):
    """Проверка разрешения на экспорт отчётов."""
    if env_manager.is_super_admin(telegram_id):
        return True
    return get_plan_limits(telegram_id)['can_export_reports']


def check_analytics_permission(telegram_id):
    """Проверка разрешения на расширенную аналитику."""
    if env_manager.is_super_admin(telegram_id):
        return True
    return get_plan_limits(telegram_id)['can_view_analytics']


def check_notifications_permission(telegram_id):
    """Проверка разрешения на систему уведомлений."""
    if env_manager.is_super_admin(telegram_id):
        return True
    return get_plan_limits(telegram_id)['can_use_notifications']


def get_subscription_warning_message(telegram_id):
    """Предупреждающее сообщение о лимитах плана."""
    org_plan = _get_org_plan_for_user(telegram_id)
    plan_name = org_plan if org_plan is not None else _get_personal_plan(telegram_id)

    if plan_name in ('Бесплатный', 'free', None):
        return (
            "⚠️ <b>Функция недоступна в бесплатном плане</b>\n\n"
            "💎 Оформите платную подписку:\n"
            "• <b>Базовый</b> — экспорт, аналитика, уведомления\n"
            "• <b>Стандарт</b> — всё выше + Google Таблицы\n"
            "• <b>Премиум</b> — безлимит на всё\n\n"
            "Нажмите «🔔 Подписка» → «💳 Купить подписку»."
        )

    limits = get_plan_limits(telegram_id)
    if not limits.get('can_use_integrations', False):
        return (
            "⚠️ <b>Google Таблицы доступны с тарифа «Стандарт»</b>\n\n"
            "Ваш текущий тариф: <b>{}</b>\n\n"
            "Нажмите «🔔 Подписка» → «💳 Купить подписку» чтобы сменить тариф."
        ).format(plan_name)

    return "⚠️ Функция ограничена текущим тарифным планом."
