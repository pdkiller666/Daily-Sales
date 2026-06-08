"""
Модуль для проверки доступа к отчетам на основе подписки.
Использует subscription_utils для корректной работы с org-пользователями.
"""

from db_utils import is_any_admin
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def create_subscription_offer_keyboard():
    """Создание клавиатуры с предложением подключить модуль"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Подключить модуль", callback_data="subscription_menu")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="reports")]
    ])


def get_subscription_offer_message(feature_name, is_admin=False):
    """Получение сообщения о необходимости подключить модуль"""
    admin_note = ("\n\n👑 Как администратор, вы можете подключить нужный модуль в веб-кабинете."
                  if is_admin else "")
    return (
        f"🔒 <b>Модуль не подключён</b>\n\n"
        f"Функция «{feature_name}» требует активного модуля <b>Аналитика</b>.\n\n"
        f"Подключите модуль в веб-кабинете:\n"
        f"<b>Подписка → Модули → 📊 Аналитика</b>{admin_note}\n\n"
        f"👆 Выберите действие:"
    )


def check_excel_export_permission(db, user_id, env_manager, telegram_id):
    """Проверка разрешения на экспорт Excel. Использует telegram_id для org-поддержки."""
    if env_manager.is_super_admin(telegram_id):
        return True, None

    from subscription_utils import check_export_permission
    if check_export_permission(telegram_id):
        return True, None

    return False, (
        "🔒 Экспорт в Excel доступен при активном модуле <b>Аналитика</b>.\n\n"
        "Подключите модуль в веб-кабинете: <b>Подписка → Модули → 📊 Аналитика</b>"
    )


def check_analytics_permission(db, user_id, env_manager, telegram_id):
    """Проверка разрешения на расширенную аналитику. Использует telegram_id для org-поддержки."""
    if env_manager.is_super_admin(telegram_id):
        return True, None

    from subscription_utils import check_analytics_permission as _check
    if _check(telegram_id):
        return True, None

    return False, (
        "🔒 Расширенная аналитика доступна при активном модуле <b>Аналитика</b>.\n\n"
        "Подключите модуль в веб-кабинете: <b>Подписка → Модули → 📊 Аналитика</b>"
    )


def get_user_access_info(db, user_id, env_manager, telegram_id):
    """Информация о доступах пользователя с учётом org-плана."""
    if env_manager.is_super_admin(telegram_id):
        return {
            'is_super_admin': True,
            'is_admin': True,
            'can_export_reports': True,
            'can_view_analytics': True,
            'subscription_type': 'Супер-администратор'
        }

    from subscription_utils import get_plan_limits, _get_org_plan_for_user, _get_personal_plan
    limits = get_plan_limits(telegram_id)

    org_plan = _get_org_plan_for_user(telegram_id)
    if org_plan is not None:
        subscription_type = f"Орг: {org_plan}"
    else:
        subscription_type = _get_personal_plan(telegram_id)

    is_admin = is_any_admin(telegram_id)
    return {
        'is_super_admin': False,
        'is_admin': is_admin,
        'can_export_reports': limits.get('can_export_reports', False),
        'can_view_analytics': limits.get('can_view_analytics', False),
        'subscription_type': f"Администратор ({subscription_type})" if is_admin else subscription_type
    }
