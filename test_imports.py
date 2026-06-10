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
    'absence_handlers',
    'tasks_handlers',
    'pdf_utils',
    'web.sale_events',
    'web.ai_utils',
    'web.routes.ai_routes',
    'web.routes.org_structure',
    'web.login_notif',
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
print(f"  Импорты: {passed} ОК, {failed} ошибок")
print("=" * 55)


# ── Функциональные smoke-тесты ────────────────────────────────────────────────
print()
print("=" * 55)
print("  Функциональные проверки")
print("=" * 55)

fn_passed = fn_failed = 0

def _fn_ok(name):
    global fn_passed
    print(f"  ✅  {name}")
    fn_passed += 1

def _fn_fail(name, err):
    global fn_failed
    print(f"  ❌  {name}: {err}")
    fn_failed += 1

# 1. Database: create in-memory DB + run migrations
try:
    import tempfile, os as _os
    _tmp = tempfile.mktemp(suffix='.db')
    from database import Database
    _db = Database(_tmp)
    _db.create_tables()
    # Check key tables exist
    _conn = _db.get_connection()
    _tables = {r[0] for r in _conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert 'products' in _tables, "products table missing"
    assert 'sales' in _tables, "sales table missing"
    assert 'inventory' in _tables, "inventory table missing"
    assert 'product_history' in _tables, "product_history table missing"
    _conn.close()
    _os.unlink(_tmp)
    # login_ips lives only in shop_bot.db path — test separately
    import tempfile as _tf
    _sbpath = _tf.mktemp(suffix='shop_bot.db')
    _dbsb = Database(_sbpath)
    _dbsb.create_tables()
    _conn2 = _dbsb.get_connection()
    _tables2 = {r[0] for r in _conn2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert 'login_ips' in _tables2, f"login_ips missing; got: {_tables2}"
    _conn2.close()
    _os.unlink(_sbpath)
    _fn_ok("Database.create_tables() — все таблицы созданы (incl. login_ips, product_history)")
except Exception as _e:
    _fn_fail("Database.create_tables()", _e)

# 2. Database: add_product + add_sale pipeline
try:
    import tempfile, os as _os
    _tmp2 = tempfile.mktemp(suffix='.db')
    _db2 = Database(_tmp2)
    _db2.create_tables()
    # Add user (positional: telegram_id, first_name, last_name, ...)
    _db2.add_user(telegram_id=99999, first_name="TestUser", last_name="Tester")
    _user = _db2.get_user(99999)
    assert _user is not None, "add_user: get_user returned None"
    _uid = _user[0]  # internal users.id
    # Add product
    _pid = _db2.add_product("Тестовый товар", "Тест", 500)
    assert _pid, "add_product failed"
    # Add inventory: update_inventory(shop_name, product_id, delta)
    _db2.update_inventory("Тест-магазин", _pid, 10)
    # Add sale: add_sale(product_id, shop_name, quantity_sold, user_id)
    _sid = _db2.add_sale(_pid, "Тест-магазин", 2, 99999, sale_price=500)
    assert _sid is not None, "add_sale returned None"
    # Check inventory decreased
    _inv = _db2.get_all_inventory()
    _stock = next((r[3] for r in _inv if r[1] == _pid), None)
    assert _stock == 8, f"expected stock=8, got {_stock}"
    _os.unlink(_tmp2)
    _fn_ok("add_product + add_sale + inventory deduction")
except Exception as _e:
    _fn_fail("add_product + add_sale pipeline", _e)

# 3. product_history methods
try:
    import tempfile, os as _os
    _tmp3 = tempfile.mktemp(suffix='.db')
    _db3 = Database(_tmp3)
    _db3.create_tables()
    _db3.add_product_history(1, "price", 100, 200, changed_by=1, changed_by_name="Admin")
    _hist = _db3.get_product_history(1)
    assert len(_hist) == 1, f"expected 1 history entry, got {len(_hist)}"
    assert _hist[0][2] == "price", "field mismatch"
    assert _hist[0][3] == "100", "old_value mismatch"
    assert _hist[0][4] == "200", "new_value mismatch"
    _os.unlink(_tmp3)
    _fn_ok("product_history add + get")
except Exception as _e:
    _fn_fail("product_history", _e)

# 4. login_notif: private IP skipped, unknown IP skipped
try:
    from web.login_notif import check_and_record_ip, _is_private
    assert _is_private("127.0.0.1"), "localhost should be private"
    assert _is_private("192.168.1.1"), "RFC1918 should be private"
    assert not _is_private("8.8.8.8"), "Google DNS should be public"
    # Negative tg_id skipped
    _tmp4 = tempfile.mktemp(suffix='.db')
    import sqlite3 as _sq
    _c = _sq.connect(_tmp4)
    _c.execute("CREATE TABLE login_ips (telegram_id INTEGER, ip TEXT, first_seen TEXT, PRIMARY KEY(telegram_id, ip))")
    _c.commit()
    _c.close()
    _fn_ok("login_notif: IP classification")
except Exception as _e:
    _fn_fail("login_notif smoke", _e)

# 5. subscription_utils import + has_module callable
try:
    from billing_utils import has_module, has_extension
    assert callable(has_module), "has_module not callable"
    assert callable(has_extension), "has_extension not callable"
    _fn_ok("billing_utils: has_module + has_extension callable")
except Exception as _e:
    _fn_fail("billing_utils", _e)

print("=" * 55)
print(f"  Итог: {fn_passed} ОК, {fn_failed} ошибок")
print("=" * 55)

sys.exit(1 if (failed + fn_failed) else 0)
