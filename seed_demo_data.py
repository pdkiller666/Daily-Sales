"""
Скрипт заполнения тестовыми данными для DailySales бота.
Запуск: python seed_demo_data.py
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta
import random

from dotenv import load_dotenv
load_dotenv('data/.env', override=False)

from database import Database
from tenant_manager import tenant_manager

# ─── Настройки ────────────────────────────────────────────────────────────────

ADMIN_ID = int(os.environ.get('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)
ORG_NAME = "Тестовая организация"

if ADMIN_ID == 0:
    print("❌ ADMIN_CHAT_ID не найден в переменных окружения")
    sys.exit(1)

print(f"✅ Используем ADMIN_CHAT_ID = {ADMIN_ID}")

# ─── Создание организации ─────────────────────────────────────────────────────

os.makedirs('data/tenants', exist_ok=True)

conn = sqlite3.connect(tenant_manager.main_db_path)
existing = conn.execute(
    "SELECT id, db_path FROM organizations WHERE name = ?", (ORG_NAME,)
).fetchone()
conn.close()

if existing:
    org_id, db_path = existing
    print(f"ℹ️  Организация '{ORG_NAME}' уже существует (id={org_id}), используем её")
else:
    ok, result = tenant_manager.create_organization(ORG_NAME, ADMIN_ID)
    if not ok:
        print(f"❌ Не удалось создать организацию: {result}")
        sys.exit(1)
    org_id = result
    conn = sqlite3.connect(tenant_manager.main_db_path)
    row = conn.execute("SELECT db_path FROM organizations WHERE id = ?", (org_id,)).fetchone()
    conn.close()
    db_path = row[0]
    print(f"✅ Организация создана: id={org_id}, path={db_path}")

db = Database(db_path)
db.create_tables()

# ─── Пользователи (сотрудники) ────────────────────────────────────────────────

SHOPS = ["Магазин Центр", "Магазин Север", "Магазин Юг"]

STAFF = [
    # (fake_telegram_id, first_name, last_name, shop)
    (ADMIN_ID,    "Алексей",  "Иванов",   SHOPS[0]),
    (200000001,   "Мария",    "Петрова",  SHOPS[0]),
    (200000002,   "Дмитрий",  "Сидоров",  SHOPS[1]),
    (200000003,   "Анна",     "Козлова",  SHOPS[1]),
    (200000004,   "Сергей",   "Новиков",  SHOPS[2]),
    (200000005,   "Елена",    "Морозова", SHOPS[2]),
]

for tg_id, fname, lname, shop in STAFF:
    db.add_user(
        telegram_id=tg_id,
        first_name=fname,
        last_name=lname,
        shop_name=shop,
        city="Москва",
    )

print(f"✅ Добавлено сотрудников: {len(STAFF)}")

# Добавляем всех сотрудников (кроме владельца) в маппинг организации как 'user'
main_conn = sqlite3.connect(tenant_manager.main_db_path)
for tg_id, *_ in STAFF:
    if tg_id == ADMIN_ID:
        continue
    main_conn.execute(
        """INSERT OR IGNORE INTO user_org_mapping (telegram_id, org_id, role, scope_type, scope_value, is_active)
           VALUES (?, ?, 'user', NULL, NULL, 1)""",
        (tg_id, org_id)
    )
main_conn.commit()
main_conn.close()
print("✅ Сотрудники добавлены в организацию")

# ─── Товары ───────────────────────────────────────────────────────────────────

PRODUCTS = [
    ("iPhone 15 Pro",        "Смартфоны",    89990),
    ("Samsung Galaxy S24",   "Смартфоны",    79990),
    ("Xiaomi 14",            "Смартфоны",    59990),
    ("AirPods Pro 2",        "Аксессуары",   24990),
    ("Чехол MagSafe",        "Аксессуары",    2490),
    ("Зарядка 65W",          "Аксессуары",    3490),
    ("MacBook Air M3",       "Ноутбуки",    129990),
    ("ASUS ZenBook 14",      "Ноутбуки",     74990),
    ("Lenovo IdeaPad 5",     "Ноутбуки",     54990),
    ("iPad Pro 12.9",        "Планшеты",     99990),
    ("Samsung Tab S9",       "Планшеты",     64990),
]

prod_ids = {}
existing_products = {p[1]: p[0] for p in db.get_all_products()}

for name, category, price in PRODUCTS:
    if name in existing_products:
        prod_ids[name] = existing_products[name]
    else:
        pid = db.add_product(name, category, price)
        prod_ids[name] = pid

print(f"✅ Товаров в базе: {len(prod_ids)}")

# ─── Инвентарь ────────────────────────────────────────────────────────────────

for shop in SHOPS:
    for name, _, _ in PRODUCTS:
        pid = prod_ids[name]
        # Проверяем текущий остаток
        conn2 = sqlite3.connect(db.db_file)
        row = conn2.execute(
            "SELECT quantity FROM inventory WHERE shop_name=? AND product_id=?", (shop, pid)
        ).fetchone()
        conn2.close()
        current_qty = row[0] if row else 0
        if current_qty < 50:
            db.add_inventory(shop, pid, 50 - current_qty, change_type='manual', change_reason='Тестовый склад')

print(f"✅ Инвентарь пополнен (по 50 шт на каждый товар в каждом магазине)")

# ─── Продажи за последние 30 дней ─────────────────────────────────────────────

user_ids = {}
for tg_id, *_ in STAFF:
    uid = db.get_user_id(tg_id)
    user_ids[tg_id] = uid

# Пары (сотрудник → магазин)
staff_shop = {tg_id: shop for tg_id, _, _, shop in STAFF}

random.seed(42)
sale_count = 0
today = datetime.now()

# Вставляем продажи напрямую в БД (обходя проверку остатков — т.к. уже выставили 50 шт)
conn3 = sqlite3.connect(db.db_file)
for days_ago in range(30, 0, -1):
    sale_date = (today - timedelta(days=days_ago)).strftime('%Y-%m-%dT12:00:00')
    n_sales = random.randint(2, 5)
    for _ in range(n_sales):
        tg_id, _, _, shop = random.choice(STAFF)
        product_name, _, price = random.choice(PRODUCTS)
        pid = prod_ids[product_name]
        qty = random.randint(1, 3)
        uid = user_ids.get(tg_id)
        sale_price = price * random.uniform(0.95, 1.05)
        conn3.execute(
            "INSERT INTO sales (product_id, shop_name, quantity_sold, sale_date, user_id, sale_price) VALUES (?,?,?,?,?,?)",
            (pid, shop, qty, sale_date, uid, round(sale_price, 2))
        )
        sale_count += 1

conn3.commit()
conn3.close()
print(f"✅ Добавлено продаж: {sale_count} (за последние 30 дней)")

# ─── Конкурс ──────────────────────────────────────────────────────────────────

import json
start_date = (today - timedelta(days=7)).strftime('%Y-%m-%d')
end_date   = (today + timedelta(days=21)).strftime('%Y-%m-%d')

existing_contests = []
try:
    conn4 = sqlite3.connect(db.db_file)
    existing_contests = conn4.execute("SELECT id FROM contests WHERE title='Майский конкурс продаж'").fetchall()
    conn4.close()
except Exception:
    pass

if not existing_contests:
    cid = db.create_contest(
        title="Майский конкурс продаж",
        description="Кто продаст больше всех за май — получает бонус 10 000₽!",
        contest_type='any',
        metric_type='turnover',
        target_value=300000,
        reward_type='fixed',
        reward_value=10000,
        start_date=start_date,
        end_date=end_date,
        created_by=ADMIN_ID,
        reward_mode='total',
    )
    print(f"✅ Конкурс создан: id={cid}")
else:
    print("ℹ️  Конкурс уже существует, пропускаем")

# ─── Итог ─────────────────────────────────────────────────────────────────────

print()
print("=" * 50)
print("🎉 Тестовые данные успешно загружены!")
print("=" * 50)
print(f"  Организация : {ORG_NAME}")
print(f"  Магазины    : {', '.join(SHOPS)}")
print(f"  Сотрудники  : {len(STAFF)} чел.")
print(f"  Товары      : {len(PRODUCTS)} позиций")
print(f"  Продажи     : {sale_count} записей (30 дней)")
print(f"  Конкурс     : Майский конкурс продаж")
print()
print("Теперь напишите боту /start — вы войдёте как директор организации.")
print(f"Инвайт-код для добавления коллег:")

conn5 = sqlite3.connect(tenant_manager.main_db_path)
row5 = conn5.execute("SELECT invite_code FROM organizations WHERE id=?", (org_id,)).fetchone()
conn5.close()
if row5 and row5[0]:
    print(f"  👉 {row5[0]}")
else:
    print("  (не задан)")
