"""
Smoke-тест: проверяет что все модули бота импортируются без ошибок.
Запуск: python test_imports.py
"""
import sys
import os
import traceback

os.environ.setdefault('BOT_TOKEN', 'dummy:token_for_import_check')
os.environ.setdefault('ADMIN_CHAT_ID', '0')

MODULES = [
    'database',
    'db_utils',
    'env_manager',
    'keyboards',
    'states',
    'utils',
    'message_utils',
    'tenant_manager',
    'subscription_utils',
    'pickle_storage',
    'backup_manager',
    'restart_manager',
    'scheduler_module',
    'timezone_utils',
    'pagination_utils',
    'filter_utils',
    'notif_utils',
    'hints',
    'reports_access_control',
    'handlers',
    'admin_handlers',
    'products_handlers',
    'sales_handlers',
    'reports_handlers',
    'inventory_handlers',
    'contacts_handlers',
    'filter_handlers',
    'payment_provider',
    'subscription_router',
    'subscription_handlers',
    'notifications_handlers',
    'payment_system_admin',
    'payment_admin_handlers',
    'backup_handlers',
    'commission_handlers',
    'earnings_handlers',
    'sales_plans_handlers',
    'salary_handlers',
    'plan_notifications',
    'contests_handlers',
    'dashboard_handlers',
    'integration_handlers',
    'integration.manager',
    'integration.auth.google_oauth',
    'integration.providers.google_sheets',
    'referral_handlers',
    'addon_handlers',
    'pdf_utils',
]

passed = 0
failed = 0

print("=" * 55)
print("  Проверка импорта модулей")
print("=" * 55)

for mod in MODULES:
    try:
        __import__(mod)
        print(f"  ✅  {mod}")
        passed += 1
    except Exception as e:
        print(f"  ❌  {mod}")
        print(f"       {type(e).__name__}: {e}")
        failed += 1

print("=" * 55)
print(f"  Итог: {passed} ОК, {failed} ошибок")
print("=" * 55)

sys.exit(1 if failed else 0)
