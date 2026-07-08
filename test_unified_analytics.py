"""
Regression test — Task #11: Единая аналитика и зарплата.

Проверяет:
1. get_unified_commission_for_user включает service + package комиссии
2. get_unified_commission_bulk — bulk-версия, те же результаты
3. Для орг без услуг/абонементов результаты идентичны get_seller_total_earnings
4. _today_total_earnings суммирует все три источника
"""
import os, sys, sqlite3, tempfile, importlib

ROOT = os.path.dirname(__file__)
sys.path.insert(0, ROOT)

import database as db_mod
from database import Database

PASS = FAIL = 0


def check(label: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        suffix = f" | {detail}" if detail else ""
        print(f"  ❌ ПРОВАЛ: {label}{suffix}")


# ─── Bootstrap a minimal org DB ──────────────────────────────────────────────
def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_ua_")
    os.close(fd)
    return path


def _setup_org(path: str):
    """Create minimal schema + test data: 1 user, 1 product, 1 service, 1 package."""
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        shop_name TEXT
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        category TEXT NOT NULL DEFAULT '',
        price REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        shop_name TEXT NOT NULL DEFAULT '',
        quantity_sold INTEGER NOT NULL DEFAULT 1,
        sale_price REAL DEFAULT 0,
        user_id INTEGER NOT NULL,
        sale_date TEXT DEFAULT (date('now'))
    );
    CREATE TABLE IF NOT EXISTS seller_earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        sale_id INTEGER NOT NULL,
        commission_amount REAL NOT NULL DEFAULT 0,
        motivation_type TEXT DEFAULT 'percentage',
        motivation_value REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_name TEXT DEFAULT '',
        price REAL DEFAULT 0,
        start_time TEXT DEFAULT (datetime('now')),
        staff_user_id INTEGER,
        status TEXT DEFAULT 'completed',
        source TEXT DEFAULT 'pos_sale'
    );
    CREATE TABLE IF NOT EXISTS service_earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        appointment_id INTEGER,
        commission_amount REAL NOT NULL DEFAULT 0,
        motivation_type TEXT DEFAULT 'percentage',
        motivation_value REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS client_packages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        package_id INTEGER,
        client_id INTEGER,
        visits_total INTEGER DEFAULT 1,
        visits_used INTEGER DEFAULT 0,
        price_paid REAL DEFAULT 0,
        shop_name TEXT,
        purchased_at TEXT DEFAULT (datetime('now')),
        status TEXT DEFAULT 'active',
        sold_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS package_sale_earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        client_package_id INTEGER,
        commission_amount REAL NOT NULL DEFAULT 0,
        motivation_type TEXT DEFAULT 'percentage',
        motivation_value REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS seller_earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        sale_id INTEGER NOT NULL,
        commission_amount REAL NOT NULL DEFAULT 0,
        motivation_type TEXT DEFAULT 'percentage',
        motivation_value REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS notification_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        low_stock_alerts BOOLEAN DEFAULT TRUE,
        daily_reports BOOLEAN DEFAULT FALSE,
        sales_alerts BOOLEAN DEFAULT TRUE,
        payment_alerts BOOLEAN DEFAULT TRUE,
        admin_notifications BOOLEAN DEFAULT TRUE,
        stock_threshold INTEGER DEFAULT 5,
        notification_time TEXT DEFAULT '09:00',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        plan_coeff_enabled BOOLEAN DEFAULT FALSE,
        plan_coeff_cap BOOLEAN DEFAULT TRUE,
        UNIQUE(user_id)
    );
    CREATE TABLE IF NOT EXISTS motivation_extra_conditions (
        id INTEGER PRIMARY KEY,
        condition_type TEXT,
        calc_mode TEXT,
        shop_name TEXT,
        min_sellers INTEGER,
        coefficient REAL,
        include_transferred INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS extra_conditions_schedule (
        id INTEGER PRIMARY KEY,
        condition_type TEXT,
        calc_mode TEXT,
        shop_name TEXT,
        min_sellers INTEGER,
        coefficient REAL,
        include_transferred INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1,
        year INTEGER,
        month INTEGER
    );
    """)
    # User with internal id=1, telegram_id=777
    conn.execute("INSERT INTO users(id, telegram_id, first_name, last_name, shop_name) VALUES(1,777,'Test','User','Shop1')")
    # Product sale + seller_earning: 100 ₽ commission
    conn.execute("INSERT INTO products(id,name,category,price) VALUES(1,'Widget','Cat',500)")
    conn.execute("INSERT INTO sales(id,product_id,shop_name,quantity_sold,sale_price,user_id,sale_date) VALUES(1,1,'Shop1',1,500,1,date('now'))")
    conn.execute("INSERT INTO seller_earnings(id,user_id,sale_id,commission_amount) VALUES(1,1,1,100)")
    # Service appointment + service_earning: 50 ₽ commission
    conn.execute("INSERT INTO appointments(id,shop_name,price,start_time,staff_user_id,status,source) VALUES(1,'Shop1',300,datetime('now'),777,'completed','pos_sale')")
    conn.execute("INSERT INTO service_earnings(id,user_id,appointment_id,commission_amount) VALUES(1,1,1,50)")
    # Package + package_sale_earning: 30 ₽ commission
    conn.execute("INSERT INTO client_packages(id,package_id,client_id,visits_total,visits_used,price_paid,shop_name,purchased_at,status,sold_by) VALUES(1,1,1,10,0,300,'Shop1',datetime('now'),'active',777)")
    conn.execute("INSERT INTO package_sale_earnings(id,user_id,client_package_id,commission_amount) VALUES(1,1,1,30)")
    conn.commit()
    conn.close()


def _setup_product_only_db() -> str:
    """DB with only product sales — no services or packages."""
    path = _make_db()
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL, first_name TEXT NOT NULL DEFAULT '', last_name TEXT NOT NULL DEFAULT '', shop_name TEXT);
    CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT NOT NULL DEFAULT '', price REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, shop_name TEXT NOT NULL DEFAULT '', quantity_sold INTEGER NOT NULL DEFAULT 1, sale_price REAL DEFAULT 0, user_id INTEGER NOT NULL, sale_date TEXT DEFAULT (date('now')));
    CREATE TABLE IF NOT EXISTS seller_earnings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, sale_id INTEGER NOT NULL, commission_amount REAL NOT NULL DEFAULT 0, motivation_type TEXT DEFAULT 'percentage', motivation_value REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS service_earnings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, appointment_id INTEGER, commission_amount REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS appointments (id INTEGER PRIMARY KEY, shop_name TEXT, price REAL, start_time TEXT, staff_user_id INTEGER, status TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS client_packages (id INTEGER PRIMARY KEY, package_id INTEGER, client_id INTEGER, visits_total INTEGER, visits_used INTEGER, price_paid REAL, shop_name TEXT, purchased_at TEXT, status TEXT, sold_by INTEGER);
    CREATE TABLE IF NOT EXISTS package_sale_earnings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, client_package_id INTEGER, commission_amount REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS notification_settings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, low_stock_alerts BOOLEAN DEFAULT TRUE, daily_reports BOOLEAN DEFAULT FALSE, sales_alerts BOOLEAN DEFAULT TRUE, payment_alerts BOOLEAN DEFAULT TRUE, admin_notifications BOOLEAN DEFAULT TRUE, stock_threshold INTEGER DEFAULT 5, notification_time TEXT DEFAULT '09:00', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, plan_coeff_enabled BOOLEAN DEFAULT FALSE, plan_coeff_cap BOOLEAN DEFAULT TRUE, UNIQUE(user_id));
    CREATE TABLE IF NOT EXISTS motivation_extra_conditions (id INTEGER PRIMARY KEY, condition_type TEXT, calc_mode TEXT, shop_name TEXT, min_sellers INTEGER, coefficient REAL, include_transferred INTEGER DEFAULT 1, is_active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS extra_conditions_schedule (id INTEGER PRIMARY KEY, condition_type TEXT, calc_mode TEXT, shop_name TEXT, min_sellers INTEGER, coefficient REAL, include_transferred INTEGER DEFAULT 1, is_active INTEGER DEFAULT 1, year INTEGER, month INTEGER);
    """)
    conn.execute("INSERT INTO users(id, telegram_id, first_name, last_name, shop_name) VALUES(1,888,'Prod','Only','Shop2')")
    conn.execute("INSERT INTO products(id,name,category,price) VALUES(1,'Thing','Cat',200)")
    conn.execute("INSERT INTO sales(id,product_id,shop_name,quantity_sold,sale_price,user_id,sale_date) VALUES(1,1,'Shop2',2,200,1,date('now'))")
    conn.execute("INSERT INTO seller_earnings(id,user_id,sale_id,commission_amount) VALUES(1,1,1,80)")
    conn.commit()
    conn.close()
    return path


print("\n=== Тест: get_unified_commission_for_user ===")
path_full = _make_db()
_setup_org(path_full)
dbo = Database(path_full)
# Warm up the pool so create_tables doesn't run (no full schema)
db_mod._INITIALIZED_DBS.add(path_full)

today_str = __import__("datetime").date.today().isoformat()
start = f"{today_str[:7]}-01"
end = today_str

# Base product commission
base = dbo.get_seller_total_earnings(1, start, end)
unified = dbo.get_unified_commission_for_user(1, start, end)

check("unified total = product + service + package",
      round(unified['total_earnings'], 2) == round(base['total_earnings'] + 50 + 30, 2),
      f"unified={unified['total_earnings']} base={base['total_earnings']}")
check("service_commission = 50.0",
      unified.get('service_commission') == 50.0,
      f"got {unified.get('service_commission')}")
check("package_commission = 30.0",
      unified.get('package_commission') == 30.0,
      f"got {unified.get('package_commission')}")
check("total_sales preserved from base",
      unified.get('total_sales') == base.get('total_sales'),
      f"unified={unified.get('total_sales')} base={base.get('total_sales')}")

print("\n=== Тест: get_unified_commission_bulk ===")
bulk_res = dbo.get_unified_commission_bulk([1], start, end, int(today_str[:4]), int(today_str[5:7]))
check("bulk uid=1 present", 1 in bulk_res)
u1 = bulk_res.get(1, {})
check("bulk total_earnings == single total",
      round(u1.get('total_earnings', -1), 2) == round(unified['total_earnings'], 2),
      f"bulk={u1.get('total_earnings')} single={unified['total_earnings']}")
check("bulk service_commission = 50.0", u1.get('service_commission') == 50.0, f"got {u1.get('service_commission')}")
check("bulk package_commission = 30.0", u1.get('package_commission') == 30.0, f"got {u1.get('package_commission')}")

print("\n=== Тест: backward compat — product-only org ===")
path_po = _setup_product_only_db()
db_mod._INITIALIZED_DBS.add(path_po)
dbo2 = Database(path_po)
base2 = dbo2.get_seller_total_earnings(1, start, end)
unified2 = dbo2.get_unified_commission_for_user(1, start, end)
bulk2 = dbo2.get_unified_commission_bulk([1], start, end, int(today_str[:4]), int(today_str[5:7]))

check("product-only: unified total == base total",
      round(unified2['total_earnings'], 2) == round(base2['total_earnings'], 2),
      f"unified={unified2['total_earnings']} base={base2['total_earnings']}")
check("product-only: service_commission = 0",
      unified2.get('service_commission', -1) == 0.0,
      f"got {unified2.get('service_commission')}")
check("product-only: package_commission = 0",
      unified2.get('package_commission', -1) == 0.0,
      f"got {unified2.get('package_commission')}")
check("product-only: bulk == single",
      round(bulk2.get(1, {}).get('total_earnings', -1), 2) == round(unified2['total_earnings'], 2))

print("\n=== Тест: _today_total_earnings включает все три источника ===")
from dashboard_handlers import _today_total_earnings

today_earnings = _today_total_earnings(path_full, today_str)
check("_today_total_earnings >= 100+50+30 (product+service+package)",
      today_earnings >= 180.0,
      f"got {today_earnings}")

# Product-only DB: today_earnings == product commission only
today_earnings_po = _today_total_earnings(path_po, today_str)
check("product-only: _today_total_earnings == 80",
      round(today_earnings_po, 2) == 80.0,
      f"got {today_earnings_po}")

# Cleanup
import os as _os
for p in (path_full, path_po):
    try:
        _os.unlink(p)
    except Exception:
        pass

print(f"\nИтого: {PASS}/{PASS + FAIL} проверок пройдено")
if FAIL:
    print("  Провалы:")
    sys.exit(1)
