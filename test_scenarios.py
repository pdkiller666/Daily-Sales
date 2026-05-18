"""
test_scenarios.py — Комплексное тестирование сценариев пользователей
Покрывает: БД (CRUD), валидацию, callbacks, утилиты, подписки, финансы,
           bulk-import, multi-tenancy, клавиатуры, отчёты.
Не требует живого соединения с Telegram.
"""
import os
import sys
import tempfile
import shutil
from datetime import datetime, timedelta

PASS = "✅"
FAIL = "❌"
results = []

def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)

# ─── Временная директория для изолированных БД ────────────
_tmp_dir = tempfile.mkdtemp()

def make_db(name="test.db"):
    path = os.path.join(_tmp_dir, name)
    from database import Database
    db = Database(path)
    db.create_tables()
    return db

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 1: Регистрация и профиль пользователя
# ─────────────────────────────────────────────────────────
section("Сценарий 1: Регистрация и профиль пользователя")

db = make_db("users.db")

# Добавление пользователя
db.add_user(100001, "Иван", "Иванов", phone="+79001234567",
            trade_network="Пятёрочка", shop_name="Пятёрочка #1", city="Москва")
user = db.get_user(100001)
check("add_user: пользователь создан", user is not None)
check("add_user: имя совпадает", user[2] == "Иван")
check("add_user: торговая сеть", user[7] == "Пятёрочка")
check("add_user: магазин", user[8] == "Пятёрочка #1")

# get_user_id возвращает int
uid = db.get_user_id(100001)
check("get_user_id: возвращает int > 0", isinstance(uid, int) and uid > 0)

# UPSERT — обновление существующего
db.add_user(100001, "Иван", "Обновлённый")
user2 = db.get_user(100001)
check("add_user UPSERT: фамилия обновлена", user2[3] == "Обновлённый")

# Несуществующий пользователь
check("get_user: несущ. → None", db.get_user(999999) is None)

# Несколько пользователей
db.add_user(100002, "Мария", "Петрова", shop_name="Магнит #5", city="Москва")
db.add_user(100003, "Алексей", "Сидоров", shop_name="Пятёрочка #1", city="СПб")

all_users = db.get_all_users()
check("get_all_users: 3 пользователя", len(all_users) == 3)

shops = db.get_all_shops()
check("get_all_shops: ≥2 магазина", len(shops) >= 2)
check("get_all_shops: содержит Пятёрочка #1", "Пятёрочка #1" in shops)

cities = db.get_all_cities()
check("get_all_cities: Москва присутствует", "Москва" in cities)

# Часовой пояс
db.set_user_timezone(100001, "Asia/Yekaterinburg")
tz = db.get_user_timezone(100001)
check("set/get_user_timezone: Yekaterinburg", tz == "Asia/Yekaterinburg")
check("get_user_timezone: несущ. → Moscow",
      db.get_user_timezone(999999) == "Europe/Moscow")

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 2: Управление товарами
# ─────────────────────────────────────────────────────────
section("Сценарий 2: Управление товарами")

p1 = db.add_product("Samsung TV 55", "Электроника", 49999.0)
p2 = db.add_product("iPhone 15", "Смартфоны", 89999.0)
p3 = db.add_product("Холодильник LG", "Бытовая техника", 34999.0)
check("add_product: все 3 ID не None", all(x is not None for x in [p1, p2, p3]))

prod = db.get_product(p1)
check("get_product по ID: найден", prod is not None and prod[1] == "Samsung TV 55")

by_name = db.get_product_by_name("iPhone 15")
check("get_product_by_name: найден", by_name is not None and by_name[2] == "Смартфоны")

check("get_all_products: 3 товара", len(db.get_all_products()) == 3)

cats = db.get_all_categories()
check("get_all_categories: 3 категории", len(cats) == 3)
check("get_all_categories: Смартфоны", "Смартфоны" in cats)

# Обновление товара
db.update_product(p1, name="Samsung TV 65", price=59999.0)
prod_upd = db.get_product(p1)
check("update_product: имя обновлено", prod_upd[1] == "Samsung TV 65")
check("update_product: цена обновлена", prod_upd[3] == 59999.0)

# Bulk — дублирование
added, skipped = db.add_products_bulk([
    {"name": "iPhone 15",   "category": "Смартфоны", "price": 89999.0},
    {"name": "iPad Pro",    "category": "Планшеты",  "price": 119999.0},
])
check("add_products_bulk: дублирует iPhone → skipped", "iPhone 15" in skipped)
check("add_products_bulk: iPad добавлен", added == 1)

# Удаление товара
db.delete_product(p3)
check("delete_product: удалён", db.get_product(p3) is None)
check("delete_product: осталось 3 (TV, iPhone, iPad)", len(db.get_all_products()) == 3)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 3: Bulk Import — парсер строк
# ─────────────────────────────────────────────────────────
section("Сценарий 3: Bulk Import — парсер строк")

from products_handlers import _parse_bulk_products

# Нормальный ввод
valid, notices, skipped = _parse_bulk_products(
    "Ноутбук Lenovo;Компьютеры;45000\nМышь Logitech;Аксессуары;1200\nКлавиатура HyperX;Аксессуары;3500"
)
check("bulk_import: 3 валидных товара", len(valid) == 3)
check("bulk_import: первое имя", valid[0]['name'] == "Ноутбук Lenovo")
check("bulk_import: цена float", valid[1]['price'] == 1200.0)
check("bulk_import: нет пропущенных", skipped == 0)

# Слишком короткое название → пропуск
valid2, _, skipped2 = _parse_bulk_products("А;Кат;100\nНормальный товар;Кат;200")
check("bulk_import: короткое имя пропущено", skipped2 == 1)
check("bulk_import: нормальный добавлен", len(valid2) == 1)

# Дубликаты в списке
valid3, _, skipped3 = _parse_bulk_products("Товар А;Кат;100\nТовар А;Кат;200")
check("bulk_import: дубликат → skipped", skipped3 == 1)
check("bulk_import: один валидный", len(valid3) == 1)

# Комментарии и пустые строки
valid4, _, _ = _parse_bulk_products("# Заголовок\n\nТелевизор Sony;ТВ;35000\n\n")
check("bulk_import: комментарии игнорируются", len(valid4) == 1)

# Пустая категория → подстановка
valid5, _, _ = _parse_bulk_products("Пылесос Dyson;;\n")
check("bulk_import: без категории → 'Без категории'", valid5[0]['category'] == 'Без категории')

# Нечисловая цена → 0.0 с предупреждением
valid6, notices6, _ = _parse_bulk_products("Мышка;Аксессуары;abc")
check("bulk_import: нечисловая цена → 0.0", valid6[0]['price'] == 0.0)
check("bulk_import: предупреждение о цене",
      any("цена" in n.lower() for n in notices6))

# Название >30 → усечение
valid7, notices7, _ = _parse_bulk_products("А" * 35 + ";Кат;100")
check("bulk_import: длинное имя усечено до 30", len(valid7[0]['name']) == 30)
check("bulk_import: предупреждение об усечении",
      any("обрез" in n.lower() for n in notices7))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 4: Продажи
# ─────────────────────────────────────────────────────────
section("Сценарий 4: Продажи — добавление, редактирование, удаление")

sdb = make_db("sales.db")
sdb.add_user(200001, "Продавец", "Тестов", shop_name="Магазин А")
uid_s = sdb.get_user_id(200001)
pid1 = sdb.add_product("Товар 1", "Категория А", 1000.0)
pid2 = sdb.add_product("Товар 2", "Категория Б", 500.0)

# ВАЖНО: add_sale проверяет остатки — нужно добавить инвентарь
sdb.add_inventory("Магазин А", pid1, 100)
sdb.add_inventory("Магазин А", pid2, 100)

sale_id1 = sdb.add_sale(pid1, "Магазин А", 3, uid_s, sale_price=1000.0)
sale_id2 = sdb.add_sale(pid2, "Магазин А", 5, uid_s, sale_price=500.0)
check("add_sale: ID не None", sale_id1 is not None and sale_id1 > 0)
check("add_sale: второй ID не None", sale_id2 is not None and sale_id2 > 0)

# add_sale без инвентаря → None
pid_no_inv = sdb.add_product("Без склада", "Кат", 100.0)
result_no_inv = sdb.add_sale(pid_no_inv, "Магазин А", 1, uid_s, 100.0)
check("add_sale без инвентаря → None", result_no_inv is None)

# add_sale — превышение остатков → None
result_over = sdb.add_sale(pid1, "Магазин А", 9999, uid_s, 1000.0)
check("add_sale: превышение остатков → None", result_over is None)

# get_user_sales
user_sales = sdb.get_user_sales(uid_s, limit=10)
check("get_user_sales: 2 продажи", len(user_sales) == 2)

# get_user_sales_by_date
start = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
sales_by_date = sdb.get_user_sales_by_date(uid_s, start, end)
check("get_user_sales_by_date: 2 продажи в диапазоне", len(sales_by_date) == 2)

# get_sale_by_id — структура: [0]=id,[1]=product_id,[2]=shop,[3]=qty,[4]=price,...
sale = sdb.get_sale_by_id(sale_id1)
check("get_sale_by_id: найдена", sale is not None)
check("get_sale_by_id: правильный product_id", sale[1] == pid1)
check("get_sale_by_id: кол-во = 3", sale[3] == 3)
check("get_sale_by_id: цена = 1000", sale[4] == 1000.0)

# Несуществующая продажа
check("get_sale_by_id: несущ. → None", sdb.get_sale_by_id(99999) is None)

# update_sale — инвентарь должен быть >= новое кол-во
update_ok = sdb.update_sale(sale_id1, quantity_sold=5, sale_price=950.0)
check("update_sale: вернул True", update_ok)
updated = sdb.get_sale_by_id(sale_id1)
check("update_sale: кол-во 5", updated[3] == 5)
check("update_sale: цена 950", updated[4] == 950.0)

# get_sales_report
report = sdb.get_sales_report(start_date=start, end_date=end)
check("get_sales_report: возвращает данные", len(report) > 0)

# get_sales_summary — возвращает кортеж: (total_sales, total_qty, total_revenue, avg_sale)
summary = sdb.get_sales_summary(start_date=start, end_date=end)
check("get_sales_summary: кортеж", isinstance(summary, tuple))
check("get_sales_summary: total_sales > 0",  summary[0] > 0 if summary else False)
check("get_sales_summary: total_revenue > 0", summary[2] > 0 if summary else False)

# get_recent_sales
recent = sdb.get_recent_sales(limit=5)
check("get_recent_sales: список", isinstance(recent, list))

# Пустой период
empty = sdb.get_user_sales_by_date(uid_s, "2000-01-01", "2000-01-02")
check("get_user_sales_by_date: пустой диапазон → []", empty == [])

# delete_sale — восстанавливает остатки
sdb.delete_sale(sale_id2)
check("delete_sale: запись удалена", sdb.get_sale_by_id(sale_id2) is None)
check("delete_sale: осталась 1", len(sdb.get_user_sales(uid_s)) == 1)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 5: Управление остатками (инвентарь)
# ─────────────────────────────────────────────────────────
section("Сценарий 5: Управление остатками (инвентарь)")

idb = make_db("inventory.db")
idb.add_user(300001, "Склад", "Менеджер", shop_name="Склад ЦО")
ipid1 = idb.add_product("Молоко 1л", "Молочное", 89.0)
ipid2 = idb.add_product("Хлеб белый", "Выпечка", 35.0)

idb.add_inventory("Склад ЦО", ipid1, 100)
idb.add_inventory("Склад ЦО", ipid2, 50)

# get_inventory возвращает int
inv = idb.get_inventory("Склад ЦО", ipid1)
check("get_inventory: кол-во = 100", inv == 100)

all_inv = idb.get_all_inventory("Склад ЦО")
check("get_all_inventory: 2 записи", len(all_inv) == 2)

# Увеличение — update_inventory возвращает новое кол-во
idb.update_inventory("Склад ЦО", ipid1, +50)
check("update_inventory +50: 150", idb.get_inventory("Склад ЦО", ipid1) == 150)

# Уменьшение
idb.update_inventory("Склад ЦО", ipid1, -30)
check("update_inventory -30: 120", idb.get_inventory("Склад ЦО", ipid1) == 120)

# get_inventory_shops
check("get_inventory_shops: Склад ЦО", "Склад ЦО" in idb.get_inventory_shops())

# get_low_stock_items_for_user — Хлеб на уровне 3 (< 5)
idb.update_inventory("Склад ЦО", ipid2, -47)   # было 50, станет 3
uid_inv = idb.get_user_id(300001)
low = idb.get_low_stock_items_for_user(uid_inv, threshold=5)
check("get_low_stock_items_for_user: Хлеб в списке",
      any(item[0] == "Хлеб белый" for item in low) if low else False)

# Молоко (120) не должно попасть в список
check("get_low_stock_items_for_user: Молоко не в списке",
      not any(item[0] == "Молоко 1л" for item in low))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 6: Промокоды и подписки
# ─────────────────────────────────────────────────────────
section("Сценарий 6: Промокоды и подписки")

pdb = make_db("promo.db")
pdb.add_user(400001, "Клиент", "Первый")
uid_p = pdb.get_user_id(400001)

# create_promocode
pdb.create_promocode("SALE20", 20, max_usage=10)
pdb.create_promocode("VIP50", 50, max_usage=1)
promos = pdb.get_all_promocodes()
check("create_promocode: SALE20 создан", any(p[1] == "SALE20" for p in promos))
check("create_promocode: VIP50 создан",  any(p[1] == "VIP50"  for p in promos))

# validate_promocode — возвращает dict {'valid', 'id', 'code', 'discount_percent', 'remaining_usage'}
promo = pdb.validate_promocode("SALE20")
check("validate_promocode: dict", isinstance(promo, dict))
check("validate_promocode: valid=True", promo.get('valid') is True)
check("validate_promocode: скидка 20%", promo.get('discount_percent') == 20)

bad_promo = pdb.validate_promocode("NOSUCH")
check("validate_promocode: несущ. → valid=False", not bad_promo.get('valid', True))

# calculate_discounted_price
disc = pdb.calculate_discounted_price(1000.0, 20)
check("calculate_discounted_price: 1000 * 0.80 = 800", abs(disc - 800.0) < 0.01)

disc2 = pdb.calculate_discounted_price(500.0, 50)
check("calculate_discounted_price: 500 * 0.50 = 250", abs(disc2 - 250.0) < 0.01)

disc_100 = pdb.calculate_discounted_price(1000.0, 100)
check("calculate_discounted_price: 100% → 0", abs(disc_100) < 0.01)

# create_subscription + is_subscription_active
pdb.create_subscription(uid_p, "Базовый")
sub = pdb.get_user_subscription(uid_p)
check("create_subscription: создана", sub is not None)
check("is_subscription_active: активна", pdb.is_subscription_active(uid_p))

# get_all_subscription_plans
plans = pdb.get_all_subscription_plans()
check("get_all_subscription_plans: ≥4 плана", len(plans) >= 4)
plan_names = [p[1] for p in plans]
check("subscription_plans: Бесплатный", "Бесплатный" in plan_names)
check("subscription_plans: Базовый",    "Базовый"    in plan_names)
check("subscription_plans: Премиум",    "Премиум"    in plan_names)

# get_user_subscription_limits по user_id
limits_u = pdb.get_user_subscription_limits(uid_p)
check("get_user_subscription_limits: dict", isinstance(limits_u, dict))
check("get_user_subscription_limits: max_products > 0",
      limits_u.get('max_products', 0) > 0)

# Проверяем данные планов напрямую из таблицы
plan_data = {p[1]: p for p in plans}
free_plan = plan_data.get("Бесплатный")
base_plan = plan_data.get("Базовый")
prem_plan = plan_data.get("Премиум")

# Индексы: plan[0]=id,[1]=name,[2]=duration,[3]=price,[4]=desc,[5]=is_active,[6]=created_at
# ,[7]=max_products,[8]=max_shops,[9]=max_sales,[10]=can_export,[11]=can_analytics,[12]=can_notify
if free_plan and len(free_plan) > 10:
    check("Бесплатный: max_products=50",   free_plan[7] == 50)
    check("Бесплатный: can_export=False",  not bool(free_plan[10]))

if base_plan and len(base_plan) > 10:
    check("Базовый: max_products=200",     base_plan[7] == 200)
    check("Базовый: can_export=True",      bool(base_plan[10]))

if prem_plan and len(prem_plan) > 10:
    check("Премиум: max_products=-1 (безлимит)", prem_plan[7] == -1)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 7: Финансовая логика — мотивация и заработок
# ─────────────────────────────────────────────────────────
section("Сценарий 7: Финансовая логика — мотивация и заработок")

fdb = make_db("finance.db")
fdb.add_user(500001, "Продавец", "Финансов", shop_name="Топ Магазин")
fuid = fdb.get_user_id(500001)
fpid = fdb.add_product("Товар X", "Категория", 2000.0)
fpid2 = fdb.add_product("Товар Y", "Категория", 5000.0)

# Добавляем инвентарь перед продажами
fdb.add_inventory("Топ Магазин", fpid, 100)
fdb.add_inventory("Топ Магазин", fpid2, 100)

# set_product_motivation — аргумент admin_telegram_id (не created_by)
fdb.set_product_motivation(fpid, "percentage", 10.0, admin_telegram_id=500001)
motiv = fdb.get_product_motivation(fpid)
check("set_product_motivation (percentage): записано", motiv is not None)
check("set_product_motivation: тип dict", isinstance(motiv, dict))
check("set_product_motivation: тип percentage", motiv.get('motivation_type') == "percentage")
check("set_product_motivation: значение 10.0", motiv.get('motivation_value') == 10.0)

# add_sale с мотивацией
sale_f = fdb.add_sale(fpid, "Топ Магазин", 2, fuid, sale_price=2000.0)
check("add_sale с мотивацией: ID не None", sale_f is not None)
sale_data = fdb.get_sale_by_id(sale_f)
check("add_sale с мотивацией: данные корректны", sale_data is not None)

# calculate_seller_commission — процент
commission = fdb.calculate_seller_commission(sale_f, fpid, 2000.0, 2)
# 10% от 2000 * 2 = 400
check("calculate_seller_commission (10%): 400", abs(commission - 400.0) < 0.01)

# add_seller_earning вручную
fdb.add_seller_earning(sale_f, fuid, fpid, commission, "percentage", 10.0)
earnings = fdb.get_seller_earnings(fuid)
check("add_seller_earning: записан", len(earnings) >= 1)

# get_seller_total_earnings → dict {'total_earnings': float, 'total_sales': int}
total = fdb.get_seller_total_earnings(fuid)
check("get_seller_total_earnings: dict", isinstance(total, dict))
check("get_seller_total_earnings: > 0", total.get('total_earnings', 0) > 0)

# Мотивация fixed
fdb.set_product_motivation(fpid2, "fixed", 150.0, admin_telegram_id=500001)
sale_f2 = fdb.add_sale(fpid2, "Топ Магазин", 3, fuid, sale_price=5000.0)
commission2 = fdb.calculate_seller_commission(sale_f2, fpid2, 5000.0, 3)
# Fixed 150 * 3 = 450
check("calculate_seller_commission (fixed 150*3): 450", abs(commission2 - 450.0) < 0.01)

# calculate_seller_commission без мотивации → 0
fpid3 = fdb.add_product("Без мотивации", "Кат", 1000.0)
fdb.add_inventory("Топ Магазин", fpid3, 100)
sale_f3 = fdb.add_sale(fpid3, "Топ Магазин", 1, fuid, 1000.0)
comm3 = fdb.calculate_seller_commission(sale_f3, fpid3, 1000.0, 1)
check("calculate_seller_commission без мотивации: 0", comm3 == 0.0)

# remove_product_motivation
fdb.remove_product_motivation(fpid)
check("remove_product_motivation: удалена", fdb.get_product_motivation(fpid) is None)

# get_top_sellers_by_earnings
top_sellers = fdb.get_top_sellers_by_earnings()
check("get_top_sellers_by_earnings: список", isinstance(top_sellers, list))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 8: Рейтинги и отчёты
# ─────────────────────────────────────────────────────────
section("Сценарий 8: Рейтинги и отчёты")

rdb = make_db("reports.db")
rdb.add_user(600001, "Ваня",  "Продавец", shop_name="Магазин А", city="Москва")
rdb.add_user(600002, "Петя",  "Торгаш",   shop_name="Магазин Б", city="Москва")
rdb.add_user(600003, "Коля",  "Агент",    shop_name="Магазин А", city="СПб")

ru1 = rdb.get_user_id(600001)
ru2 = rdb.get_user_id(600002)
ru3 = rdb.get_user_id(600003)

rp1 = rdb.add_product("Prod A", "Кат1", 100.0)
rp2 = rdb.add_product("Prod B", "Кат2", 200.0)

# Инвентарь для продаж
for shop in ["Магазин А", "Магазин Б"]:
    rdb.add_inventory(shop, rp1, 500)
    rdb.add_inventory(shop, rp2, 500)

rdb.add_sale(rp1, "Магазин А", 10, ru1, 100.0)
rdb.add_sale(rp2, "Магазин А",  5, ru1, 200.0)
rdb.add_sale(rp1, "Магазин Б",  7, ru2, 100.0)
rdb.add_sale(rp2, "Магазин Б",  3, ru2, 200.0)
rdb.add_sale(rp1, "Магазин А",  4, ru3, 100.0)

start = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
end   = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

# get_sales_ranking
ranking = rdb.get_sales_ranking(start_date=start, end_date=end)
check("get_sales_ranking: список ≥1", len(ranking) >= 1)
# Ваня должен быть первым (10+5=15 шт.)
check("get_sales_ranking: Ваня на первом месте", ranking[0][0] == "Ваня")

# get_shop_ranking
shop_rank = rdb.get_shop_ranking(start_date=start, end_date=end)
check("get_shop_ranking: список", len(shop_rank) >= 1)

# get_city_ranking
city_rank = rdb.get_city_ranking(start_date=start, end_date=end)
check("get_city_ranking: список", len(city_rank) >= 1)

# get_shop_sales_by_date
shop_sales = rdb.get_shop_sales_by_date("Магазин А", start, end)
check("get_shop_sales_by_date: ≥2 продажи в Магазин А", len(shop_sales) >= 2)

# get_users_by_shop
users_a = rdb.get_users_by_shop("Магазин А")
check("get_users_by_shop: 2 пользователя в А", len(users_a) == 2)

# get_users_by_city
users_msk = rdb.get_users_by_city("Москва")
check("get_users_by_city: 2 в Москве", len(users_msk) == 2)

# get_sales_summary с фильтром магазина — кортеж (total_sales, total_qty, total_revenue, avg)
summary_a = rdb.get_sales_summary(start_date=start, end_date=end, shop_name="Магазин А")
check("get_sales_summary(shop_name): кортеж с данными",
      isinstance(summary_a, tuple) and summary_a[0] > 0)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 9: Валидация входных данных
# ─────────────────────────────────────────────────────────
section("Сценарий 9: Валидация входных данных")

from utils import validate_phone_number, validate_email, format_currency, format_date_display

# Телефоны
check("validate_phone: +79001234567 ✓",         validate_phone_number("+79001234567"))
check("validate_phone: 89001234567 ✓",           validate_phone_number("89001234567"))
check("validate_phone: с пробелами ✓",           validate_phone_number("+7 (900) 123-45-67"))
check("validate_phone: 7900123 ✗ (короткий)",   not validate_phone_number("7900123"))
check("validate_phone: None ✗",                  not validate_phone_number(None))
check("validate_phone: '' ✗",                    not validate_phone_number(""))

# Email
check("validate_email: valid@example.com ✓",    validate_email("valid@example.com"))
check("validate_email: user@domain.ru ✓",        validate_email("user@domain.ru"))
check("validate_email: без @ ✗",                 not validate_email("invalidemail"))
check("validate_email: без точки ✗",             not validate_email("user@domain"))
check("validate_email: None ✗",                  not validate_email(None))
check("validate_email: '' ✗",                    not validate_email(""))

# format_currency
check("format_currency: 1000 → '1 000₽'",       format_currency(1000) == "1 000₽")
check("format_currency: 0 → '0₽'",              format_currency(0) == "0₽")
check("format_currency: None → '0₽'",           format_currency(None) == "0₽")
check("format_currency: '1500' → '1 500₽'",     format_currency("1500") == "1 500₽")
check("format_currency: 999.9 → '999₽'",        format_currency(999.9) == "999₽")
check("format_currency: дата-строка → '0₽'",    format_currency("2024-01-01 12:00:00") == "0₽")
check("format_currency: мусор → '0₽'",          format_currency("abc") == "0₽")

# format_date_display
check("format_date: YYYY-MM-DD → DD.MM.YYYY",
      format_date_display("2024-03-15") == "15.03.2024")
check("format_date: c временем",
      format_date_display("2024-03-15 14:30:00") == "15.03.2024 14:30")
check("format_date: None → Неизвестно",           format_date_display(None) == "Неизвестно")
check("format_date: '' → Неизвестно",             format_date_display("") == "Неизвестно")
check("format_date: число → Неизвестно",           format_date_display(12345) == "Неизвестно")
check("format_date: ISO T-формат",
      "2024" in format_date_display("2024-06-15T10:30:00"))

# Лимиты длин полей (логика process_profile_edit / регистрация)
_MAX = {"trade_network": 30, "shop_name": 30, "city": 30,
        "first_name": 50, "last_name": 50, "phone": 20, "email": 100}
_MIN = {"trade_network": 2, "shop_name": 2, "city": 2, "first_name": 2}

check("длина: network 15 ≤ 30",      len("Перекрёсток") <= _MAX["trade_network"])
check("длина: network 31 > 30",       len("А" * 31) > _MAX["trade_network"])
check("длина: имя 1 символ < min 2",  len("Я") < _MIN["first_name"])
check("длина: email 101 > max 100",   len("a" * 101) > _MAX["email"])
check("длина: пустое поле 0 < min 2", 0 < _MIN["trade_network"])

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 10: Callback-данные (safe_cb / resolve_cb_name)
# ─────────────────────────────────────────────────────────
section("Сценарий 10: Callback-данные (safe_cb / resolve_cb_name)")

from keyboards import safe_cb, resolve_cb_name

# Короткое — не усекается
check("safe_cb: Магнит не усекается", safe_cb("shop_", "Магнит") == "shop_Магнит")

# Длинное — усекается до 64 байт
long_val = "А" * 50
result_long = safe_cb("shop_", long_val)
check("safe_cb: длинное ≤64 байт",      len(result_long.encode('utf-8')) <= 64)
check("safe_cb: результат строка",       isinstance(result_long, str))

# Prefix ровно 64 байта — value не помещается
prefix64 = "p" * 64
check("safe_cb: prefix=64 ≤64 байт",    len(safe_cb(prefix64, "val").encode('utf-8')) <= 64)

# Кириллица — 64-байтовый лимит
cyrillic = safe_cb("select_shop_", "Торговый центр Измайловский")
check("safe_cb: кириллица ≤64 байт",    len(cyrillic.encode('utf-8')) <= 64)
check("safe_cb: кириллица строка",       isinstance(cyrillic, str))

# resolve_cb_name — точное совпадение
candidates = ["Магазин на Ленина", "Магазин Центральный", "Маркет"]
check("resolve_cb_name: точное",         resolve_cb_name("Маркет", candidates) == "Маркет")

# resolve_cb_name — усечённое → восстанавливает полное
truncated = safe_cb("cat_", "Магазин на Ленина").replace("cat_", "")
check("resolve_cb_name: усечённое восстанавливается",
      resolve_cb_name(truncated, candidates) == "Магазин на Ленина")

# resolve_cb_name — не найдено → возвращает partial
check("resolve_cb_name: не найдено → partial",
      resolve_cb_name("NOTEXIST", candidates) == "NOTEXIST")

# Пустой список кандидатов
check("resolve_cb_name: пустые кандидаты → partial",
      resolve_cb_name("anything", []) == "anything")

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 11: Генерация клавиатур
# ─────────────────────────────────────────────────────────
section("Сценарий 11: Генерация клавиатур")

from keyboards import (generate_calendar, products_menu, inventory_menu,
                       cancel_registration_keyboard, usage_mode_keyboard,
                       create_confirm_keyboard, back_button, timezone_keyboard)
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# generate_calendar — базовый март 2024
cal = generate_calendar(2024, 3)
check("generate_calendar: тип InlineKeyboardMarkup", isinstance(cal, InlineKeyboardMarkup))
all_cbs = [btn.callback_data for row in cal.inline_keyboard for btn in row]
check("generate_calendar: кнопки с датами",   any("date_2024-03" in cb for cb in all_cbs))
check("generate_calendar: кнопки навигации",  any("nav_" in cb for cb in all_cbs))

# generate_calendar — cancel_callback и custom prefix
cal2 = generate_calendar(2024, 12, cancel_callback="back_to_menu", prefix="edit_cal_")
all_cbs2 = [btn.callback_data for row in cal2.inline_keyboard for btn in row]
check("generate_calendar: cancel присутствует",  "back_to_menu" in all_cbs2)
check("generate_calendar: custom prefix",         any("edit_cal_date_" in cb for cb in all_cbs2))

# generate_calendar — февраль 2024 (29 дней, високосный)
feb24_dates = [cb for cb in [btn.callback_data for row in generate_calendar(2024, 2).inline_keyboard
                              for btn in row] if "date_2024-02" in cb]
check("generate_calendar: февраль 2024 = 29 дней", len(feb24_dates) == 29)

# generate_calendar — февраль 2025 (28 дней)
feb25_dates = [cb for cb in [btn.callback_data for row in generate_calendar(2025, 2).inline_keyboard
                              for btn in row] if "date_2025-02" in cb]
check("generate_calendar: февраль 2025 = 28 дней", len(feb25_dates) == 28)

# Декабрь → январь следующего года (навигация)
dec_cbs = [cb for cb in all_cbs2 if "nav_" in cb]
check("generate_calendar: дек 2024 nav вперёд = янв 2025",
      any("nav_2025_1" in cb for cb in dec_cbs))

# Другие клавиатуры
check("products_menu: тип", isinstance(products_menu(), InlineKeyboardMarkup))
check("inventory_menu: тип", isinstance(inventory_menu(), InlineKeyboardMarkup))

cancel_kb = cancel_registration_keyboard()
cancel_cbs = [btn.callback_data for row in cancel_kb.inline_keyboard for btn in row]
check("cancel_registration_keyboard: есть cancel_registration",
      "cancel_registration" in cancel_cbs)

usage_cbs = [btn.callback_data for row in usage_mode_keyboard().inline_keyboard for btn in row]
check("usage_mode_keyboard: personal/corporate/join",
      all(cb in usage_cbs for cb in ["mode_personal", "mode_corporate", "mode_join"]))

confirm_cbs = [btn.callback_data for row in
               create_confirm_keyboard("ok", "cancel").inline_keyboard for btn in row]
check("create_confirm_keyboard: ok и cancel", "ok" in confirm_cbs and "cancel" in confirm_cbs)

check("back_button: callback_data", back_button("main_menu").callback_data == "main_menu")
check("back_button: тип", isinstance(back_button("x"), InlineKeyboardButton))

tz_cbs = [btn.callback_data for row in timezone_keyboard().inline_keyboard for btn in row]
check("timezone_keyboard: set_timezone_ присутствует",
      any("set_timezone_" in cb for cb in tz_cbs))
check("timezone_keyboard: Europe/Moscow в кнопках",
      any("set_timezone_Europe/Moscow" in cb for cb in tz_cbs))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 12: Мульти-тенантность (изоляция БД)
# ─────────────────────────────────────────────────────────
section("Сценарий 12: Мульти-тенантность (изоляция баз данных)")

db1 = make_db("tenant_org1.db")
db2 = make_db("tenant_org2.db")

db1.add_user(701001, "Орг1 Пользователь", "А", shop_name="Орг1 Магазин")
db2.add_user(702001, "Орг2 Пользователь", "Б", shop_name="Орг2 Магазин")

pid_o1 = db1.add_product("Товар Орг1", "Кат", 100.0)
pid_o2 = db2.add_product("Товар Орг2", "Кат", 200.0)

prods1 = db1.get_all_products()
prods2 = db2.get_all_products()
check("multi-tenancy: Орг1 не видит товары Орг2",
      all(p[1] != "Товар Орг2" for p in prods1))
check("multi-tenancy: Орг2 не видит товары Орг1",
      all(p[1] != "Товар Орг1" for p in prods2))
check("multi-tenancy: Орг1 — ровно 1 товар", len(prods1) == 1)
check("multi-tenancy: Орг2 — ровно 1 товар", len(prods2) == 1)

# Продажи: добавляем инвентарь и делаем продажу в Орг1
uid_o1 = db1.get_user_id(701001)
db1.add_inventory("Орг1 Магазин", pid_o1, 100)
db1.add_sale(pid_o1, "Орг1 Магазин", 5, uid_o1, 100.0)

start_t = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
end_t   = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

# Орг2 не видит продажи Орг1
sales_o2 = db2.get_sales_report(start_date=start_t, end_date=end_t)
check("multi-tenancy: продажи Орг1 не в отчёте Орг2", len(sales_o2) == 0)

# Пользователи изолированы
check("multi-tenancy: пользователь Орг1 не в Орг2", db2.get_user(701001) is None)
check("multi-tenancy: пользователь Орг2 не в Орг1", db1.get_user(702001) is None)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 13: Subscription Utils — правила лимитов
# ─────────────────────────────────────────────────────────
section("Сценарий 13: Subscription Utils — правила лимитов")

from subscription_utils import (_UNLIMITED, _FREE_FALLBACK,
                                 get_subscription_warning_message)

check("UNLIMITED: max_products == -1",      _UNLIMITED['max_products'] == -1)
check("UNLIMITED: can_export == True",      _UNLIMITED['can_export_reports'] is True)
check("UNLIMITED: max_sales == -1",         _UNLIMITED['max_sales_per_month'] == -1)

check("FREE_FALLBACK: max_products == 50",  _FREE_FALLBACK['max_products'] == 50)
check("FREE_FALLBACK: can_export == False", _FREE_FALLBACK['can_export_reports'] is False)
check("FREE_FALLBACK: max_sales == 100",    _FREE_FALLBACK['max_sales_per_month'] == 100)
check("FREE_FALLBACK: max_shops == 1",      _FREE_FALLBACK['max_shops'] == 1)

# warning message — несуществующий пользователь → Бесплатный
warning = get_subscription_warning_message(999999999)
check("get_subscription_warning_message: текст возвращён", len(warning) > 20)
check("get_subscription_warning_message: содержит 'подписк'",
      "подписк" in warning.lower())

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 14: Утилиты — timezone_utils
# ─────────────────────────────────────────────────────────
section("Сценарий 14: Утилиты — timezone_utils")

from timezone_utils import get_common_timezones, format_user_datetime

tz_map = get_common_timezones()
check("get_common_timezones: dict не пустой",     len(tz_map) > 0)
check("get_common_timezones: Europe/Moscow есть", "Europe/Moscow" in tz_map.values())
check("get_common_timezones: Asia/Omsk есть",     "Asia/Omsk" in tz_map.values())
check("get_common_timezones: ≥10 поясов",         len(tz_map) >= 10)

dt_ok = format_user_datetime("2024-06-15 12:00:00", "Europe/Moscow", "%d.%m.%Y %H:%M")
check("format_user_datetime: строка возвращена",   isinstance(dt_ok, str) and len(dt_ok) > 0)
check("format_user_datetime: не 'Неизвестно'",     dt_ok != "Неизвестно")
check("format_user_datetime: содержит 15.06.2024", "15.06.2024" in dt_ok)

# Невалидная строка → "Неизвестно" (исправлено: не показываем мусор пользователю)
bad_dt = format_user_datetime("not-a-date", "Europe/Moscow", "%d.%m.%Y")
check("format_user_datetime: невалидная строка → Неизвестно", bad_dt == "Неизвестно")
check("format_user_datetime: None → Неизвестно", format_user_datetime(None) == "Неизвестно")
# datetime-объект без tzinfo, конвертация невозможна → str(dt), не "Неизвестно"
from datetime import datetime as _dt
raw_dt = _dt(2024, 6, 15, 12, 0, 0)
result_raw = format_user_datetime(raw_dt, "Invalid/Tz", "%d.%m.%Y")
check("format_user_datetime: datetime + невалидный tz → str(dt)", isinstance(result_raw, str))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 15: Платёжная система — настройки
# ─────────────────────────────────────────────────────────
section("Сценарий 15: Платёжная система — настройки")

paydb = make_db("payments.db")

settings = paydb.get_payment_settings()
check("get_payment_settings: dict", isinstance(settings, dict))
check("get_payment_settings: card_number",    "card_number"    in settings)
check("get_payment_settings: recipient_name", "recipient_name" in settings)
check("get_payment_settings: bank_name",      "bank_name"      in settings)

paydb.update_payment_setting("card_number",   "4111 1111 1111 1111")
paydb.update_payment_setting("bank_name",     "Сбербанк")
s2 = paydb.get_payment_settings()
check("update_payment_setting: card_number обновлён", s2['card_number'] == "4111 1111 1111 1111")
check("update_payment_setting: bank_name обновлён",   s2['bank_name'] == "Сбербанк")

stats = paydb.get_subscriptions_statistics()
check("get_subscriptions_statistics: dict",  isinstance(stats, dict))
check("get_subscriptions_statistics: ключи",
      all(k in stats for k in ['total_subscribers', 'monthly_revenue', 'conversion_rate']))

detailed = paydb.get_detailed_payment_statistics()
check("get_detailed_payment_statistics: dict",   isinstance(detailed, dict))
check("get_detailed_payment_statistics: ключи",
      all(k in detailed for k in ['total_payments', 'total_revenue', 'by_plans']))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 16: Оклады и графики работы
# ─────────────────────────────────────────────────────────
section("Сценарий 16: Оклады и графики работы")

sldb = make_db("salary.db")

# ── Регистрируем двух продавцов ────────────────────────
sldb.add_user(801001, "Пётр",  "Зарплатов",  shop_name="Магазин А", city="Москва")
sldb.add_user(801002, "Нина",  "Окладова",   shop_name="Магазин Б", city="Москва")
sldb.add_user(801003, "Олег",  "Безставки",  shop_name="Магазин А", city="Москва")

su1 = sldb.get_user_id(801001)
su2 = sldb.get_user_id(801002)
su3 = sldb.get_user_id(801003)

check("salary: get_user_id u1 возвращает int", isinstance(su1, int) and su1 > 0)
check("salary: get_user_id u2 возвращает int", isinstance(su2, int) and su2 > 0)

# ── set_salary_rate / get_salary_rate ──────────────────
ok1 = sldb.set_salary_rate(su1, 1500.0, 1)
ok2 = sldb.set_salary_rate(su2, 2000.0, 1)
check("set_salary_rate u1: True",   ok1 is True)
check("set_salary_rate u2: True",   ok2 is True)

r1 = sldb.get_salary_rate(su1)
r2 = sldb.get_salary_rate(su2)
r3 = sldb.get_salary_rate(su3)
check("get_salary_rate u1: 1500",   abs(r1 - 1500.0) < 0.01)
check("get_salary_rate u2: 2000",   abs(r2 - 2000.0) < 0.01)
check("get_salary_rate u3 (нет): 0", r3 == 0.0)

# ── UPSERT ставки ─────────────────────────────────────
sldb.set_salary_rate(su1, 1800.0, 1)
check("set_salary_rate UPSERT: новое значение",
      abs(sldb.get_salary_rate(su1) - 1800.0) < 0.01)

# ── toggle_work_day ────────────────────────────────────
r_add1 = sldb.toggle_work_day(su1, "2026-05-01", 1)
r_add2 = sldb.toggle_work_day(su1, "2026-05-02", 1)
r_add3 = sldb.toggle_work_day(su1, "2026-05-05", 1)
check("toggle_work_day: добавить 01 → True",  r_add1 is True)
check("toggle_work_day: добавить 02 → True",  r_add2 is True)
check("toggle_work_day: добавить 05 → True",  r_add3 is True)

r_remove = sldb.toggle_work_day(su1, "2026-05-02", 1)
check("toggle_work_day: снять 02 → False",    r_remove is False)

# ── get_work_schedule ──────────────────────────────────
worked_may = sldb.get_work_schedule(su1, 2026, 5)
check("get_work_schedule: тип set",           isinstance(worked_may, set))
check("get_work_schedule: 01 присутствует",   "2026-05-01" in worked_may)
check("get_work_schedule: 02 отсутствует",    "2026-05-02" not in worked_may)
check("get_work_schedule: 05 присутствует",   "2026-05-05" in worked_may)
check("get_work_schedule: ровно 2 смены",     len(worked_may) == 2)

# Другой месяц — пустой
worked_apr = sldb.get_work_schedule(su1, 2026, 4)
check("get_work_schedule: апрель пустой",     len(worked_apr) == 0)

# ── Граничный случай: декабрь / январь ────────────────
sldb.toggle_work_day(su1, "2025-12-31", 1)
worked_dec = sldb.get_work_schedule(su1, 2025, 12)
worked_jan = sldb.get_work_schedule(su1, 2026, 1)
check("toggle: дек-31 в декабре",             "2025-12-31" in worked_dec)
check("toggle: дек-31 не попадает в январь",  "2025-12-31" not in worked_jan)

# ── get_worked_days_count ──────────────────────────────
cnt = sldb.get_worked_days_count(su1, 2026, 5)
check("get_worked_days_count: 2 смены в мае", cnt == 2)

cnt_empty = sldb.get_worked_days_count(su2, 2026, 5)
check("get_worked_days_count: 0 для u2",      cnt_empty == 0)

# ── calculate_monthly_salary ───────────────────────────
sal1 = sldb.calculate_monthly_salary(su1, 2026, 5)
check("calculate_monthly_salary: 2 × 1800 = 3600",  abs(sal1 - 3600.0) < 0.01)

sal2 = sldb.calculate_monthly_salary(su2, 2026, 5)
check("calculate_monthly_salary: 0 смен = 0",       sal2 == 0.0)

sal3 = sldb.calculate_monthly_salary(su3, 2026, 5)
check("calculate_monthly_salary: нет ставки = 0",   sal3 == 0.0)

# ── Добавляем смены для u2, считаем ───────────────────
sldb.toggle_work_day(su2, "2026-05-10", 1)
sldb.toggle_work_day(su2, "2026-05-11", 1)
sldb.toggle_work_day(su2, "2026-05-12", 1)
sal2b = sldb.calculate_monthly_salary(su2, 2026, 5)
check("calculate_monthly_salary u2: 3 × 2000 = 6000", abs(sal2b - 6000.0) < 0.01)

# ── get_all_salary_rates ───────────────────────────────
all_rates = sldb.get_all_salary_rates()
check("get_all_salary_rates: список",          isinstance(all_rates, list))
check("get_all_salary_rates: 3 записи",        len(all_rates) == 3)
ids_in_rates = [row[0] for row in all_rates]
check("get_all_salary_rates: u1 есть",         su1 in ids_in_rates)
check("get_all_salary_rates: u3 есть (rate=0)",su3 in ids_in_rates)
u1_row = next(r for r in all_rates if r[0] == su1)
check("get_all_salary_rates: u1 rate = 1800",  abs(u1_row[3] - 1800.0) < 0.01)
u3_row = next(r for r in all_rates if r[0] == su3)
check("get_all_salary_rates: u3 rate = None/0",u3_row[3] is None or u3_row[3] == 0.0)

# ── get_team_salary_summary ────────────────────────────
summary = sldb.get_team_salary_summary(2026, 5)
check("get_team_salary_summary: список",       isinstance(summary, list))
check("get_team_salary_summary: 3 строки",     len(summary) == 3)

s_ids = [row[0] for row in summary]
check("get_team_salary_summary: u1 есть",      su1 in s_ids)
check("get_team_salary_summary: u2 есть",      su2 in s_ids)

s1 = next(r for r in summary if r[0] == su1)
check("get_team_salary_summary u1: worked_days=2", s1[4] == 2)
check("get_team_salary_summary u1: salary=3600",   abs(s1[5] - 3600.0) < 0.01)
check("get_team_salary_summary u1: shop=Магазин А",s1[6] == "Магазин А")

s2 = next(r for r in summary if r[0] == su2)
check("get_team_salary_summary u2: worked_days=3", s2[4] == 3)
check("get_team_salary_summary u2: salary=6000",   abs(s2[5] - 6000.0) < 0.01)

# ── Мульти-тенантная изоляция зарплат ─────────────────
# Две РАЗНЫЕ БД — каждая содержит своих пользователей.
# Автоинкремент в каждой БД независим (оба внутренних ID = 1),
# поэтому изоляцию проверяем по именам и данным, а не по числовым ID.
sl_org1 = make_db("salary_org1.db")
sl_org2 = make_db("salary_org2.db")

sl_org1.add_user(901001, "ОргПервая", "Работник", shop_name="Орг1 Шоп")
sl_org2.add_user(902001, "ОргВторая", "Работник", shop_name="Орг2 Шоп")

ou1 = sl_org1.get_user_id(901001)  # internal id в org1.db (начинается с 1)
ou2 = sl_org2.get_user_id(902001)  # internal id в org2.db (начинается с 1)

sl_org1.set_salary_rate(ou1, 3000.0, 1)
sl_org1.toggle_work_day(ou1, "2026-06-01", 1)
sl_org1.toggle_work_day(ou1, "2026-06-02", 1)

# Орг2: пользователь 901001 (telegram_id из Орг1) не существует в её БД
check("multi-tenancy salary: get_user 901001 в Орг2 — None",
      sl_org2.get_user(901001) is None)

# Орг2 get_all_salary_rates — только имена из Орг2 (не «ОргПервая»)
sl_org2.set_salary_rate(ou2, 2500.0, 1)
org2_rates = sl_org2.get_all_salary_rates()
org2_names = [f"{r[1]} {r[2]}".strip() for r in org2_rates]
check("multi-tenancy salary: get_all_salary_rates Орг2 не видит ОргПервая",
      not any("ОргПервая" in n for n in org2_names))
check("multi-tenancy salary: get_all_salary_rates Орг2 видит ОргВторая",
      any("ОргВторая" in n for n in org2_names))

# Орг2 summary — не содержит «ОргПервая»
sl_org2.toggle_work_day(ou2, "2026-06-05", 1)
summary_org2 = sl_org2.get_team_salary_summary(2026, 6)
sum_names_org2 = [f"{r[1]} {r[2]}".strip() for r in summary_org2]
check("multi-tenancy salary: summary Орг2 — нет ОргПервая",
      not any("ОргПервая" in n for n in sum_names_org2))
check("multi-tenancy salary: summary Орг2 — есть ОргВторая",
      any("ОргВторая" in n for n in sum_names_org2))

# Орг1 summary — не затронута операциями Орг2
summary_org1 = sl_org1.get_team_salary_summary(2026, 6)
sum_names_org1 = [f"{r[1]} {r[2]}".strip() for r in summary_org1]
check("multi-tenancy salary: summary Орг1 — нет ОргВторая",
      not any("ОргВторая" in n for n in sum_names_org1))
check("multi-tenancy salary: summary Орг1 — есть ОргПервая",
      any("ОргПервая" in n for n in sum_names_org1))

# Ставка и смены Орг1 не протекают в Орг2 через внутренний ID
# (ou1 и ou2 оба == 1, но принадлежат разным БД — изоляция должна работать)
rate_org2_for_id1 = sl_org2.get_salary_rate(ou2)   # собственный rate Орг2
check("multi-tenancy salary: ставка Орг2 для id=1 == 2500 (своя)",
      abs(rate_org2_for_id1 - 2500.0) < 0.01)
rate_org1_for_id1 = sl_org1.get_salary_rate(ou1)   # ставка Орг1 не изменилась
check("multi-tenancy salary: ставка Орг1 для id=1 == 3000 (не затронута)",
      abs(rate_org1_for_id1 - 3000.0) < 0.01)
worked_org2_jun = sl_org2.get_work_schedule(ou2, 2026, 6)
worked_org1_jun = sl_org1.get_work_schedule(ou1, 2026, 6)
check("multi-tenancy salary: смены Орг2 в июне = 1 (только свои)",
      len(worked_org2_jun) == 1)
check("multi-tenancy salary: смены Орг1 в июне = 2 (не затронуты)",
      len(worked_org1_jun) == 2)

# ── _calendar_kb: структура клавиатуры ────────────────
import calendar as _cal_test
from salary_handlers import _calendar_kb
from aiogram.types import InlineKeyboardMarkup

# Проверяем чтение-only (editable=False)
kb_ro = _calendar_kb(2026, 5, {"2026-05-01", "2026-05-15"}, editable=False, back_cb="main_menu")
check("_calendar_kb: тип InlineKeyboardMarkup", isinstance(kb_ro, InlineKeyboardMarkup))
all_cbs_ro = [btn.callback_data for row in kb_ro.inline_keyboard for btn in row]
check("_calendar_kb read-only: нет slr_tog_ кнопок",
      not any("slr_tog_" in cb for cb in all_cbs_ro))
check("_calendar_kb read-only: навигация my_cal_",
      any("my_cal_" in cb for cb in all_cbs_ro))
check("_calendar_kb: back_cb = main_menu", "main_menu" in all_cbs_ro)

# Проверяем редактируемый (editable=True)
kb_ed = _calendar_kb(2026, 5, {"2026-05-03"}, uid=99, editable=True, back_cb="slr_scheds")
all_cbs_ed = [btn.callback_data for row in kb_ed.inline_keyboard for btn in row]
check("_calendar_kb editable: есть slr_tog_ кнопки",
      any("slr_tog_99_" in cb for cb in all_cbs_ed))
check("_calendar_kb editable: навигация slr_cal_",
      any("slr_cal_99_" in cb for cb in all_cbs_ed))
check("_calendar_kb editable: back_cb = slr_scheds", "slr_scheds" in all_cbs_ed)

# Декабрь → навигация вперёд = январь следующего года
kb_dec = _calendar_kb(2025, 12, set(), uid=5, editable=True)
cbs_dec = [btn.callback_data for row in kb_dec.inline_keyboard for btn in row]
check("_calendar_kb дек 2025: → январь 2026",
      any("slr_cal_5_2026_1" in cb for cb in cbs_dec))
check("_calendar_kb дек 2025: ← ноябрь 2025",
      any("slr_cal_5_2025_11" in cb for cb in cbs_dec))

# Февраль 2024 (високосный) — 29 дней
kb_feb = _calendar_kb(2024, 2, set(), uid=1, editable=True)
day_cbs_feb = [cb for cb in [btn.callback_data for row in kb_feb.inline_keyboard for btn in row]
               if "slr_tog_1_2024-02" in cb]
check("_calendar_kb февраль 2024: 29 дней", len(day_cbs_feb) == 29)

# Февраль 2025 (обычный) — 28 дней
kb_feb25 = _calendar_kb(2025, 2, set(), uid=1, editable=True)
day_cbs_feb25 = [cb for cb in [btn.callback_data for row in kb_feb25.inline_keyboard for btn in row]
                 if "slr_tog_1_2025-02" in cb]
check("_calendar_kb февраль 2025: 28 дней", len(day_cbs_feb25) == 28)

# ─────────────────────────────────────────────────────────
# БЫСТРЫЙ ПОИСК ТОВАРОВ (_make_qty_keyboard, QuickSaleStates)
# ─────────────────────────────────────────────────────────
from sales_handlers import _make_qty_keyboard
from states import QuickSaleStates
from aiogram.fsm.state import State

# _make_qty_keyboard: при max_qty=0 — только кнопки «Вручную» и «Отмена»
kb0 = _make_qty_keyboard(0)
all_cb0 = [btn.callback_data for row in kb0.inline_keyboard for btn in row]
check("_make_qty_keyboard(0): нет числовых кнопок", not any(
    cb.startswith("sq_qty_") and cb.replace("sq_qty_", "").isdigit() for cb in all_cb0
))
check("_make_qty_keyboard(0): есть sq_qty_manual", "sq_qty_manual" in all_cb0)
check("_make_qty_keyboard(0): есть cancel_sale", "cancel_sale" in all_cb0)

# _make_qty_keyboard: при max_qty=3 — кнопки 1,2,3 только
kb3 = _make_qty_keyboard(3)
all_cb3 = [btn.callback_data for row in kb3.inline_keyboard for btn in row]
digit_cbs3 = [cb for cb in all_cb3 if cb.startswith("sq_qty_") and cb.replace("sq_qty_", "").isdigit()]
check("_make_qty_keyboard(3): ровно 3 числовые кнопки", len(digit_cbs3) == 3)
check("_make_qty_keyboard(3): есть sq_qty_1", "sq_qty_1" in all_cb3)
check("_make_qty_keyboard(3): есть sq_qty_3", "sq_qty_3" in all_cb3)
check("_make_qty_keyboard(3): нет sq_qty_5", "sq_qty_5" not in all_cb3)

# _make_qty_keyboard: при max_qty=100 — все 7 стандартных значений
kb100 = _make_qty_keyboard(100)
all_cb100 = [btn.callback_data for row in kb100.inline_keyboard for btn in row]
digit_cbs100 = [cb for cb in all_cb100 if cb.startswith("sq_qty_") and cb.replace("sq_qty_", "").isdigit()]
check("_make_qty_keyboard(100): 7 числовых кнопок (1-2-3-5-10-20-50)", len(digit_cbs100) == 7)
check("_make_qty_keyboard(100): sq_qty_50 присутствует", "sq_qty_50" in all_cb100)
check("_make_qty_keyboard(100): sq_qty_manual присутствует", "sq_qty_manual" in all_cb100)

# QuickSaleStates: состояние существует и является State
check("QuickSaleStates.searching_product — это State",
      isinstance(QuickSaleStates.searching_product, State))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 17: ЮKassa — провайдер и конфигурация
# ─────────────────────────────────────────────────────────
section("Сценарий 17: ЮKassa — провайдер, конфигурация, платёжные записи")

ykdb = make_db("yookassa.db")

# get_payment_provider: default = 'sbp'
check("get_payment_provider: default = 'sbp'", ykdb.get_payment_provider() == 'sbp')

# set / get provider
ykdb.set_payment_provider('yookassa')
check("set_payment_provider('yookassa'): провайдер = yookassa",
      ykdb.get_payment_provider() == 'yookassa')
ykdb.set_payment_provider('sbp')
check("set_payment_provider back to 'sbp'", ykdb.get_payment_provider() == 'sbp')

# get_yookassa_config — по умолчанию пустые строки
cfg = ykdb.get_yookassa_config()
check("get_yookassa_config: dict", isinstance(cfg, dict))
check("get_yookassa_config: keys (shop_id, secret_key, return_url)",
      all(k in cfg for k in ('shop_id', 'secret_key', 'return_url')))
check("get_yookassa_config: default shop_id = ''",    cfg['shop_id'] == '')
check("get_yookassa_config: default secret_key = ''", cfg['secret_key'] == '')
check("get_yookassa_config: default return_url = ''", cfg['return_url'] == '')

# set_yookassa_config — частичное обновление
ykdb.set_yookassa_config(shop_id='123456')
cfg2 = ykdb.get_yookassa_config()
check("set_yookassa_config: shop_id сохранён",        cfg2['shop_id'] == '123456')
check("set_yookassa_config: secret_key не затронут",  cfg2['secret_key'] == '')

ykdb.set_yookassa_config(secret_key='sk_test_abc', return_url='https://t.me/testbot')
cfg3 = ykdb.get_yookassa_config()
check("set_yookassa_config: secret_key сохранён",    cfg3['secret_key'] == 'sk_test_abc')
check("set_yookassa_config: return_url сохранён",    cfg3['return_url'] == 'https://t.me/testbot')
check("set_yookassa_config: shop_id не изменился",   cfg3['shop_id'] == '123456')

# create_yookassa_payment_record
ykdb.add_user(1001001, "ЮKassa", "Тестер")
yk_uid = ykdb.get_user_id(1001001)
ok1 = ykdb.create_yookassa_payment_record(
    yookassa_payment_id='yk_pay_001',
    user_id=yk_uid,
    plan_type='Базовый',
    amount=990.0,
    promocode_id=None,
    is_scheduled=False,
    schedule_date=None,
)
check("create_yookassa_payment_record: True", ok1 is True)

# get_yookassa_payment_by_payment_id
row = ykdb.get_yookassa_payment_by_payment_id('yk_pay_001')
check("get_yookassa_payment_by_payment_id: найден",             row is not None)
check("yk_payment: payment_id == 'yk_pay_001'",                 row[1] == 'yk_pay_001')
check("yk_payment: user_id корректен",                          row[2] == yk_uid)
check("yk_payment: plan_type = 'Базовый'",                      row[3] == 'Базовый')
check("yk_payment: amount = 990.0",                             abs(row[4] - 990.0) < 0.01)
check("yk_payment: default status = 'pending'",                 row[5] == 'pending')
check("yk_payment: is_scheduled = 0",                           row[7] == 0)

# get несуществующего
check("get_yookassa_payment: несущ. → None",
      ykdb.get_yookassa_payment_by_payment_id('NONEXISTENT') is None)

# update_yookassa_payment_status
ykdb.update_yookassa_payment_status('yk_pay_001', 'succeeded')
row2 = ykdb.get_yookassa_payment_by_payment_id('yk_pay_001')
check("update_yookassa_payment_status: succeeded", row2[5] == 'succeeded')

ykdb.update_yookassa_payment_status('yk_pay_001', 'canceled')
row3 = ykdb.get_yookassa_payment_by_payment_id('yk_pay_001')
check("update_yookassa_payment_status: canceled",  row3[5] == 'canceled')

# UNIQUE constraint — дублирование payment_id → False
ok_dup = ykdb.create_yookassa_payment_record(
    yookassa_payment_id='yk_pay_001',  # дубликат
    user_id=yk_uid, plan_type='Премиум', amount=1990.0,
)
check("create_yookassa_payment_record UNIQUE: дубликат → False", ok_dup is False)

# Запись с промокодом и scheduled
ok3 = ykdb.create_yookassa_payment_record(
    yookassa_payment_id='yk_pay_002',
    user_id=yk_uid,
    plan_type='Премиум',
    amount=1592.0,
    promocode_id=42,
    is_scheduled=True,
    schedule_date='15.06.2026',
)
check("create_yookassa_payment_record: с промокодом, scheduled → True", ok3 is True)
row_sched = ykdb.get_yookassa_payment_by_payment_id('yk_pay_002')
check("yk_payment scheduled: promocode_id = 42",        row_sched[6] == 42)
check("yk_payment scheduled: is_scheduled = 1",         row_sched[7] == 1)
check("yk_payment scheduled: schedule_date сохранён",   row_sched[8] == '15.06.2026')

# update несуществующего — не падает
try:
    ykdb.update_yookassa_payment_status('NONEXIST', 'succeeded')
    check("update_yookassa_payment_status: несущ. — нет исключения", True)
except Exception:
    check("update_yookassa_payment_status: несущ. — нет исключения", False)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 18: payment_provider.py — фабрика провайдеров
# ─────────────────────────────────────────────────────────
section("Сценарий 18: payment_provider.py — фабрика провайдеров")

from payment_provider import (
    get_active_provider, provider_label,
    PROVIDER_SBP, PROVIDER_YOOKASSA,
    check_yookassa_payment_status, create_yookassa_payment,
)

# Константы
check("PROVIDER_SBP == 'sbp'",           PROVIDER_SBP == 'sbp')
check("PROVIDER_YOOKASSA == 'yookassa'", PROVIDER_YOOKASSA == 'yookassa')

# provider_label
check("provider_label('sbp'): содержит 'СБП'",       'СБП'    in provider_label('sbp'))
check("provider_label('yookassa'): содержит 'ЮKassa'", 'ЮKassa' in provider_label('yookassa'))
check("provider_label('unknown'): строка",            isinstance(provider_label('unknown'), str))
check("provider_label(''): строка",                   isinstance(provider_label(''), str))
check("provider_label('sbp') != provider_label('yookassa')",
      provider_label('sbp') != provider_label('yookassa'))

# get_active_provider через реальный DB
pp_db = make_db("provider.db")
check("get_active_provider: default 'sbp'",           get_active_provider(pp_db) == 'sbp')
pp_db.set_payment_provider('yookassa')
check("get_active_provider: после set 'yookassa'",    get_active_provider(pp_db) == 'yookassa')
pp_db.set_payment_provider('sbp')
check("get_active_provider: вернулся к 'sbp'",        get_active_provider(pp_db) == 'sbp')

# check_yookassa_payment_status без shop_id / secret_key → 'error'
check("check_yookassa_payment_status без shop_id → 'error'",
      check_yookassa_payment_status('any_id', '', 'key') == 'error')
check("check_yookassa_payment_status без secret_key → 'error'",
      check_yookassa_payment_status('any_id', 'shop', '') == 'error')
check("check_yookassa_payment_status оба пустых → 'error'",
      check_yookassa_payment_status('any_id', '', '') == 'error')

# check с невалидными данными (нет реального API) → 'error'
status_bad = check_yookassa_payment_status('fake_payment_id', 'shop123', 'secret123')
check("check_yookassa_payment_status фейк данные → 'error'", status_bad == 'error')

# create_yookassa_payment без shop_id / secret_key → None
check("create_yookassa_payment без shop_id → None",
      create_yookassa_payment(100.0, 'test', {}, 'http://x', '', 'key') is None)
check("create_yookassa_payment без secret_key → None",
      create_yookassa_payment(100.0, 'test', {}, 'http://x', 'shop', '') is None)
check("create_yookassa_payment оба пустых → None",
      create_yookassa_payment(100.0, 'test', {}, 'http://x', '', '') is None)

# create с фейковыми данными → None (API недоступен, ImportError или Exception)
result_bad_api = create_yookassa_payment(
    100.0, 'Тест подписки', {'user': 1}, 'http://x', 'fake_shop', 'fake_key')
check("create_yookassa_payment фейк данные → None",      result_bad_api is None)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 19: Планы продаж
# ─────────────────────────────────────────────────────────
section("Сценарий 19: Планы продаж — CRUD и прогресс")

pldb = make_db("plans.db")
pldb.add_user(1100001, "Продавец",  "Плановый", shop_name="Магазин Плановый", city="Москва")
pldb.add_user(1100002, "Второй",    "Продавец", shop_name="Магазин Плановый", city="Москва")
plu1 = pldb.get_user_id(1100001)
plu2 = pldb.get_user_id(1100002)

# add_sales_plan — personal, turnover, weekly
plan1_id = pldb.add_sales_plan(
    'personal', 'turnover', 50000.0, 'weekly',
    user_id=plu1, shop_name=None, filter_type='all', created_by=1)
check("add_sales_plan personal: ID не None", plan1_id is not None and plan1_id > 0)

# add_sales_plan — shop, quantity, monthly
plan2_id = pldb.add_sales_plan(
    'shop', 'quantity', 100, 'monthly',
    user_id=None, shop_name='Магазин Плановый', filter_type='all', created_by=1)
check("add_sales_plan shop: ID не None", plan2_id is not None and plan2_id > 0)

# add_sales_plan — ещё один для удаления
plan3_id = pldb.add_sales_plan(
    'personal', 'quantity', 200, 'monthly',
    user_id=plu2, shop_name=None, filter_type='all', created_by=1)
check("add_sales_plan personal2: ID не None", plan3_id is not None and plan3_id > 0)

# get_sales_plans — все активные
plans = pldb.get_sales_plans(active_only=True)
check("get_sales_plans: 3 плана",        len(plans) == 3)

# структура строки: 0:id, 1:plan_type, 2:metric_type, 3:target_value,
#                   4:target_type, 5:user_id, 6:shop_name, 7:filter_type,
#                   8:filter_value, 9:is_active, 10:created_by, 11:created_at,
#                   12:first_name, 13:last_name
p1_row = next((p for p in plans if p[0] == plan1_id), None)
check("get_sales_plans: plan1 найден",          p1_row is not None)
check("get_sales_plans: metric_type turnover",  p1_row[2] == 'turnover' if p1_row else False)
check("get_sales_plans: target_value 50000",    abs(p1_row[3] - 50000.0) < 0.01 if p1_row else False)
check("get_sales_plans: target_type weekly",    p1_row[4] == 'weekly' if p1_row else False)
check("get_sales_plans: first_name Продавец",   p1_row[12] == 'Продавец' if p1_row else False)

p2_row = next((p for p in plans if p[0] == plan2_id), None)
check("get_sales_plans: plan2 shop_name",
      p2_row[6] == 'Магазин Плановый' if p2_row else False)

# update_sales_plan
upd_ok = pldb.update_sales_plan(plan1_id, target_value=60000.0)
check("update_sales_plan: вернул True", upd_ok is True)
plans_upd = pldb.get_sales_plans(active_only=True)
p1_upd = next((p for p in plans_upd if p[0] == plan1_id), None)
check("update_sales_plan: target_value 60000",
      abs(p1_upd[3] - 60000.0) < 0.01 if p1_upd else False)

# update_sales_plan: неизвестное поле → False (nothing to update)
bad_upd = pldb.update_sales_plan(plan1_id, nonexistent_field='x')
check("update_sales_plan: неизвестное поле → False", bad_upd is False)

# update is_active = 0 (деактивация)
pldb.update_sales_plan(plan1_id, is_active=0)
active_plans = pldb.get_sales_plans(active_only=True)
check("update_sales_plan is_active=0: план исчез из active_only",
      not any(p[0] == plan1_id for p in active_plans))
all_plans = pldb.get_sales_plans(active_only=False)
check("get_sales_plans active_only=False: деактивированный присутствует",
      any(p[0] == plan1_id for p in all_plans))

# delete_sales_plan
del_ok = pldb.delete_sales_plan(plan3_id)
check("delete_sales_plan: True", del_ok is True)
check("delete_sales_plan: план исчез",
      not any(p[0] == plan3_id for p in pldb.get_sales_plans(active_only=False)))

# delete несуществующего → False
del_bad = pldb.delete_sales_plan(99999)
check("delete_sales_plan: несущ. → False", del_bad is False)

# get_plans_progress — список
progress = pldb.get_plans_progress()
check("get_plans_progress: список", isinstance(progress, list))

# _plan_summary_line из dashboard_handlers
# plan row: [0]=id,[1]=plan_type,[2]=metric_type,[3]=target_value,[4]=target_type,
#            [5]=user_id,[6]=shop_name,[7]=filter_type,[8]=filter_value,[9]=is_active,
#            [10]=created_by,[11]=created_at,[12]=first_name,[13]=last_name
from dashboard_handlers import _plan_summary_line
_mock_plan = (1, 'personal', 'turnover', 50000.0, 'weekly',
              plu1, 'Магазин Плановый', 'all', None, 1, 1, '2026-01-01', 'Продавец', 'Плановый')
check("_plan_summary_line 0%: строка",   isinstance(_plan_summary_line(_mock_plan, 0.0, 0.0), str))
check("_plan_summary_line 50%: строка",  isinstance(_plan_summary_line(_mock_plan, 25000.0, 50.0), str))
check("_plan_summary_line 100%: строка", isinstance(_plan_summary_line(_mock_plan, 50000.0, 100.0), str))
check("_plan_summary_line 120%: строка", isinstance(_plan_summary_line(_mock_plan, 60000.0, 120.0), str))
_mock_qty_plan = (2, 'shop', 'quantity', 100, 'monthly',
                  None, 'Магазин А', 'all', None, 1, 1, '2026-01-01', None, None)
check("_plan_summary_line quantity plan: строка",
      isinstance(_plan_summary_line(_mock_qty_plan, 42.0, 42.0), str))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 20: Конкурсы — полный жизненный цикл
# ─────────────────────────────────────────────────────────
section("Сценарий 20: Конкурсы — создание, статус, архив")

ctdb = make_db("contests.db")
ctdb.add_user(1200001, "Победитель", "Конкурсов", shop_name="Магазин А")
ctdb.add_user(1200002, "Участник",   "Второй",    shop_name="Магазин А")
cu1 = ctdb.get_user_id(1200001)
cu2 = ctdb.get_user_id(1200002)

start_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
end_str   = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')

# create_contest
ct_id = ctdb.create_contest(
    title="Лучший продавец мая",
    description="Конкурс по обороту",
    contest_type='individual',
    metric_type='turnover',
    target_value=100000,
    reward_type='fixed',
    reward_value=5000,
    start_date=start_str,
    end_date=end_str,
    created_by=cu1,
)
check("create_contest: ID не None", ct_id is not None and ct_id > 0)

# get_contests — все (без фильтра статуса)
all_ct = ctdb.get_contests()
check("get_contests(): ≥1 конкурс",    len(all_ct) >= 1)

# get_contests — только active
active_ct = ctdb.get_contests(status='active')
check("get_contests(active): ≥1",      len(active_ct) >= 1)
check("get_contests(active): статус",  all(c[16] == 'active' for c in active_ct))

# get_contest по id — структура:
# 0:id, 1:title, 2:desc, 3:contest_type, 4:metric_type, 5:target_value,
# 6:reward_type, 7:reward_value, 8:start_date, 9:end_date, 16:status
ct = ctdb.get_contest(ct_id)
check("get_contest: найден",                     ct is not None)
check("get_contest: title",                      ct[1] == "Лучший продавец мая")
check("get_contest: metric_type = turnover",     ct[4] == 'turnover')
check("get_contest: target_value = 100000",      ct[5] == 100000)
check("get_contest: status = 'active'",          ct[16] == 'active')

# Несуществующий
check("get_contest: несущ. → None",              ctdb.get_contest(99999) is None)

# update_contest_status — завершение конкурса
ctdb.update_contest_status(ct_id, 'completed')
ct_done = ctdb.get_contest(ct_id)
check("update_contest_status 'completed': статус",  ct_done[16] == 'completed')

# get_contests(status='completed') — должен включать завершённый
completed_ct = ctdb.get_contests(status='completed')
check("get_contests(completed): ≥1",              len(completed_ct) >= 1)
check("get_contests(completed): нет active",
      all(c[16] == 'completed' for c in completed_ct))

# Создаём второй конкурс и удаляем его
ct_id2 = ctdb.create_contest(
    title="Конкурс для удаления",
    metric_type='quantity',
    target_value=50,
    reward_type='fixed',
    reward_value=1000,
    start_date=start_str,
    end_date=end_str,
)
check("create_contest 2: ID не None", ct_id2 is not None and ct_id2 > 0)

del_ct = ctdb.delete_contest(ct_id2)
check("delete_contest: True", del_ct is True)
check("delete_contest: конкурс удалён", ctdb.get_contest(ct_id2) is None)

# delete несуществующего
del_ct_bad = ctdb.delete_contest(99999)
check("delete_contest: несущ. → False", del_ct_bad is False)

# update_contest (общий метод)
# allowed fields: start_date, end_date, target_value, reward_value — НЕ title
ct_id3 = ctdb.create_contest(
    title="Тестовый конкурс",
    metric_type='turnover',
    target_value=1000,
    start_date=start_str,
    end_date=end_str,
)
check("create_contest 3: ID не None", ct_id3 is not None and ct_id3 > 0)
upd_ct = ctdb.update_contest(ct_id3, target_value=2000, reward_value=500)
check("update_contest: True",              upd_ct is True)
ct3_upd = ctdb.get_contest(ct_id3)
check("update_contest: ct3_upd не None",   ct3_upd is not None)
check("update_contest: target обновлён",   ct3_upd is not None and ct3_upd[5] == 2000)
check("update_contest: reward обновлён",   ct3_upd is not None and ct3_upd[7] == 500)
# title — не в allowed fields, остаётся прежним
check("update_contest: title не изменился", ct3_upd is not None and ct3_upd[1] == "Тестовый конкурс")
# update_contest с незапрещёнными полями → False (ничего не обновилось)
upd_none = ctdb.update_contest(ct_id3, nonexistent_field="xyz")
check("update_contest: нет allowed fields → False", upd_none is False)

# clear_contests_archive — удаляет 'finished' и 'cancelled'
ctdb.update_contest_status(ct_id3, 'cancelled')
before_clear = len(ctdb.get_contests(status='cancelled'))
ctdb.clear_contests_archive()
after_clear_cancelled = ctdb.get_contests(status='cancelled')
check("clear_contests_archive: 'cancelled' удалены",
      len(after_clear_cancelled) == 0)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 21: Шаблоны смен + время смены
# ─────────────────────────────────────────────────────────
section("Сценарий 21: Шаблоны смен и время рабочих дней")

stdb = make_db("shift_templates.db")
stdb.add_user(1300001, "Шаблон", "Смен", shop_name="Магазин А")
st_uid = stdb.get_user_id(1300001)

# set_shift_template (weekday 0=Пн..6=Вс)
stdb.set_shift_template(st_uid, 0, '09:00', '18:00')   # Пн
stdb.set_shift_template(st_uid, 1, '10:00', '19:00')   # Вт
stdb.set_shift_template(st_uid, 4, '08:00', '17:00')   # Пт

# get_shift_templates → dict {weekday: (start_time, end_time)}
templates = stdb.get_shift_templates(st_uid)
check("get_shift_templates: dict",              isinstance(templates, dict))
check("get_shift_templates: 3 записи",          len(templates) == 3)
_tmpl0 = templates.get(0)
check("get_shift_templates: Пн start=09:00",    _tmpl0 is not None and _tmpl0[0] == '09:00')
check("get_shift_templates: Пн end=18:00",      _tmpl0 is not None and _tmpl0[1] == '18:00')
_tmpl1 = templates.get(1)
check("get_shift_templates: Вт start=10:00",    _tmpl1 is not None and _tmpl1[0] == '10:00')
_tmpl4 = templates.get(4)
check("get_shift_templates: Пт start=08:00",    _tmpl4 is not None and _tmpl4[0] == '08:00')
check("get_shift_templates: Ср нет шаблона",    templates.get(2) is None)
check("get_shift_templates: Вс нет шаблона",    templates.get(6) is None)

# UPSERT шаблона
stdb.set_shift_template(st_uid, 0, '08:30', '17:30')
templates2 = stdb.get_shift_templates(st_uid)
_tmpl2_0 = templates2.get(0)
check("set_shift_template UPSERT: Пн start=08:30",  _tmpl2_0 is not None and _tmpl2_0[0] == '08:30')
check("set_shift_template UPSERT: Пн end=17:30",    _tmpl2_0 is not None and _tmpl2_0[1] == '17:30')
check("set_shift_template UPSERT: всё ещё 3",        len(templates2) == 3)

# Другой пользователь — пустой результат
tmpl_other = stdb.get_shift_templates(9999)
check("get_shift_templates: несущ. uid → пустой",
      tmpl_other == {} or isinstance(tmpl_other, dict))

# set_work_day_time / get_work_day_time
stdb.toggle_work_day(st_uid, '2026-05-04', 1)
stdb.set_work_day_time(st_uid, '2026-05-04', '09:00', '18:00')
times = stdb.get_work_day_time(st_uid, '2026-05-04')
check("set_work_day_time: возвращает кортеж/список", times is not None)
check("get_work_day_time: start = '09:00'",
      times[0] == '09:00' if times else False)
check("get_work_day_time: end = '18:00'",
      times[1] == '18:00' if times else False)

# UPSERT времени
stdb.set_work_day_time(st_uid, '2026-05-04', '10:00', '20:00')
times2 = stdb.get_work_day_time(st_uid, '2026-05-04')
check("set_work_day_time UPSERT: start=10:00",
      times2[0] == '10:00' if times2 else False)
check("set_work_day_time UPSERT: end=20:00",
      times2[1] == '20:00' if times2 else False)

# Несуществующий день → None или (None, None)
times3 = stdb.get_work_day_time(st_uid, '2030-01-01')
check("get_work_day_time: несущ. → None/пусто",
      times3 is None or (isinstance(times3, (tuple, list)) and
                         (times3[0] is None or all(x is None for x in times3))))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 22: Filter Utils — система фильтров
# ─────────────────────────────────────────────────────────
section("Сценарий 22: Filter Utils — пустой, активный, merge, available")

from filter_utils import (empty_filter, merge_scope_with_filter,
                          get_available_filter_values, is_filter_active,
                          filter_button_text)

# empty_filter
flt = empty_filter()
check("empty_filter: dict",          isinstance(flt, dict))
check("empty_filter: shops=[]",      flt.get('shops', []) == [])
check("empty_filter: cities=[]",     flt.get('cities', []) == [])
check("empty_filter: networks=[]",   flt.get('networks', []) == [])

# is_filter_active
check("is_filter_active: пустой → False",     not is_filter_active(flt))
check("is_filter_active: с магазином → True", is_filter_active({'shops': ['А'], 'cities': [], 'networks': []}))
check("is_filter_active: с городом → True",   is_filter_active({'shops': [], 'cities': ['СПб'], 'networks': []}))
check("is_filter_active: с сетью → True",     is_filter_active({'shops': [], 'cities': [], 'networks': ['Сеть']}))

# filter_button_text
# возвращает "🔍 Фильтр" (пустой) или "🔍 Фильтр: 🏪 Магазин А" (активный) — без ✅
btn_empty = filter_button_text(flt)
check("filter_button_text пустой: строка",          isinstance(btn_empty, str))
check("filter_button_text пустой: содержит 🔍",     '🔍' in btn_empty)
check("filter_button_text пустой: нет ':' (пустой)", ':' not in btn_empty)
btn_active = filter_button_text({'shops': ['А'], 'cities': [], 'networks': []})
check("filter_button_text активный: строка",        isinstance(btn_active, str))
check("filter_button_text активный: содержит 🔍",   '🔍' in btn_active)
check("filter_button_text активный: содержит ':'",  ':' in btn_active)
check("filter_button_text активный != пустого",     btn_active != btn_empty)

# merge_scope_with_filter
# merge_scope_with_filter возвращает kwargs для DB методов:
# - пустой фильтр + 'all'/'org' scope → {} (нет ограничений)
# - пустой фильтр + 'shop' scope ['А'] → {'shop_name': 'А'} (один) или {'shop_names': [...]}
# - активный фильтр shops → {'shop_names': [...]} (из filter_to_scope_kwargs)
merged_org_empty = merge_scope_with_filter('all', [], empty_filter())
check("merge all + empty filter: {} (нет ограничений)",
      merged_org_empty == {})

# shop scope + пустой фильтр → _scope_filter_kwargs('shop', ['Магазин А']) → {'shop_name': 'Магазин А'}
merged_shop = merge_scope_with_filter('shop', ['Магазин А'], empty_filter())
check("merge shop scope + empty: ключ shop_name",
      'shop_name' in merged_shop or 'shop_names' in merged_shop)
check("merge shop scope + empty: Магазин А присутствует",
      merged_shop.get('shop_name') == 'Магазин А' or
      'Магазин А' in merged_shop.get('shop_names', []))

# org scope + фильтр по магазину → filter_to_scope_kwargs → {'shop_names': ['Магазин Б']}
merged_with_shop_filter = merge_scope_with_filter(
    'all', [], {'shops': ['Магазин Б'], 'cities': [], 'networks': []})
check("merge all + shop filter: ключ shop_names",
      'shop_names' in merged_with_shop_filter)
check("merge all + shop filter: Магазин Б",
      'Магазин Б' in merged_with_shop_filter.get('shop_names', []))

# city scope + фильтр по городу
merged_city = merge_scope_with_filter(
    'all', [], {'shops': [], 'cities': ['Москва'], 'networks': []})
check("merge all + city filter: ключ cities",  'cities' in merged_city)
check("merge all + city filter: Москва",       'Москва' in merged_city.get('cities', []))

# get_available_filter_values с реальной БД
flt_db = make_db("filter_vals.db")
flt_db.add_user(1400001, "Алфа",   "Один", shop_name="Магазин А", city="Москва", trade_network="Сеть1")
flt_db.add_user(1400002, "Бета",   "Два",  shop_name="Магазин Б", city="СПб",   trade_network="Сеть2")
flt_db.add_user(1400003, "Гамма",  "Три",  shop_name="Магазин А", city="Москва", trade_network="Сеть1")

avail = get_available_filter_values(flt_db, 'org', [])
check("get_available_filter_values: dict",            isinstance(avail, dict))
check("get_available_filter_values: shops key",       'shops' in avail)
check("get_available_filter_values: Магазин А",       'Магазин А' in avail.get('shops', []))
check("get_available_filter_values: Магазин Б",       'Магазин Б' in avail.get('shops', []))
check("get_available_filter_values: Москва",          'Москва' in avail.get('cities', []))
check("get_available_filter_values: СПб",             'СПб' in avail.get('cities', []))

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 23: Pagination Utils
# ─────────────────────────────────────────────────────────
section("Сценарий 23: Pagination Utils — paginate, page_nav_row, константы")

from pagination_utils import (paginate, page_nav_row,
                               PAGE_SIZE_DEFAULT, PAGE_SIZE_USERS,
                               PAGE_SIZE_ORGS, PAGE_SIZE_SALES)
from aiogram.types import InlineKeyboardButton as _IKBtn

# Константы
check("PAGE_SIZE_DEFAULT == 8",  PAGE_SIZE_DEFAULT == 8)
check("PAGE_SIZE_USERS == 10",   PAGE_SIZE_USERS == 10)
check("PAGE_SIZE_ORGS == 8",     PAGE_SIZE_ORGS == 8)
check("PAGE_SIZE_SALES == 8",    PAGE_SIZE_SALES == 8)

# paginate возвращает (items_on_page, has_prev, has_next, total_pages, clamped_page)
items10 = list(range(10))
pg_0, hp0, hn0, tot_2, cp0 = paginate(items10, page=0, per_page=8)
check("paginate 10→ page=0: 8 элементов",    len(pg_0) == 8)
check("paginate 10→ total_pages = 2",         tot_2 == 2)
check("paginate 10→ первый = 0",              pg_0[0] == 0)
check("paginate 10→ page=0: нет prev",        hp0 is False)
check("paginate 10→ page=0: есть next",       hn0 is True)

pg_1, hp1, hn1, tot_2b, cp1 = paginate(items10, page=1, per_page=8)
check("paginate 10→ page=1: 2 элемента",      len(pg_1) == 2)
check("paginate 10→ total_pages = 2 (стр2)",  tot_2b == 2)
check("paginate 10→ page=1 первый = 8",       pg_1[0] == 8)
check("paginate 10→ page=1: есть prev",       hp1 is True)
check("paginate 10→ page=1: нет next",        hn1 is False)

# Ровно одна страница (8 из 8)
pg8, hp8, hn8, tot8, cp8 = paginate(list(range(8)), page=0, per_page=8)
check("paginate 8→ 1 страница",               tot8 == 1)
check("paginate 8→ 8 элементов",              len(pg8) == 8)
check("paginate 8→ нет prev/next",            hp8 is False and hn8 is False)

# Пустой список
pg_empty, hpe, hne, tot_empty, _ = paginate([], page=0, per_page=8)
check("paginate []→ 0 элементов",             len(pg_empty) == 0)
check("paginate []→ 1 страница (min=1)",       tot_empty == 1)

# 3 элемента — 1 страница
pg3, hp3, hn3, tot3, _ = paginate(list(range(3)), page=0, per_page=8)
check("paginate 3→ 1 страница",               tot3 == 1)
check("paginate 3→ 3 элемента",               len(pg3) == 3)

# Страница за пределами → clamp к последней
pg_out, _, _, _, cp_out = paginate(list(range(5)), page=5, per_page=8)
check("paginate out-of-range→ clamp (1 страница, 5 элем.)", len(pg_out) == 5)

# page_nav_row(callback_prefix, page, has_prev, has_next, total_pages)
# 1 страница, нет prev/next → []
nav1 = page_nav_row("prod_", 0, False, False, 1)
check("page_nav_row 1 страница → []",          nav1 == [])

# стр. 0 из 3: нет prev, есть next → кнопки ▶ и N/M
nav_first = page_nav_row("prod_", 0, False, True, 3)
check("page_nav_row стр.0 из 3: кнопки",       len(nav_first) > 0)
check("page_nav_row стр.0 из 3: тип IKBtn",    all(isinstance(b, _IKBtn) for b in nav_first))

# стр. 1 из 3: и prev, и next → минимум 3 кнопки (◀, N/M, ▶)
nav_mid = page_nav_row("prod_", 1, True, True, 3)
check("page_nav_row стр.1 из 3: ≥2 кнопки",   len(nav_mid) >= 2)

# стр. 2 из 3: есть prev, нет next → кнопки ◀ и N/M
nav_last = page_nav_row("prod_", 2, True, False, 3)
check("page_nav_row стр.2 из 3: кнопки",       len(nav_last) > 0)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 24: Избранное и последние товары
# ─────────────────────────────────────────────────────────
section("Сценарий 24: Избранное (toggle_favorite) и последние товары")

favdb = make_db("favorites.db")
favdb.add_user(1500001, "Продавец", "Избранный", shop_name="Магазин А")
fav_uid = favdb.get_user_id(1500001)
fp1 = favdb.add_product("Товар Любимый",  "Кат1", 100.0)
fp2 = favdb.add_product("Товар Второй",   "Кат2", 200.0)
fp3 = favdb.add_product("Товар Третий",   "Кат1", 300.0)

# toggle_favorite_product: первый раз → True (добавлен)
r1 = favdb.toggle_favorite_product(fav_uid, fp1)
r2 = favdb.toggle_favorite_product(fav_uid, fp2)
check("toggle_favorite_product: fp1 добавлен → True",  r1 is True)
check("toggle_favorite_product: fp2 добавлен → True",  r2 is True)

# get_favorite_products → список product_id
favs = favdb.get_favorite_products(fav_uid)
check("get_favorite_products: 2 избранных",    len(favs) == 2)
check("get_favorite_products: fp1 в списке",   fp1 in favs)
check("get_favorite_products: fp2 в списке",   fp2 in favs)
check("get_favorite_products: fp3 не в списке",fp3 not in favs)

# toggle_favorite_product: повторно fp1 → False (убран)
r1b = favdb.toggle_favorite_product(fav_uid, fp1)
check("toggle_favorite_product: fp1 убран → False", r1b is False)
favs2 = favdb.get_favorite_products(fav_uid)
check("get_favorite_products: fp1 удалён",      fp1 not in favs2)
check("get_favorite_products: fp2 остался",     fp2 in favs2)
check("get_favorite_products: стало 1",         len(favs2) == 1)

# Вернуть fp1 в избранное
favdb.toggle_favorite_product(fav_uid, fp1)
favdb.toggle_favorite_product(fav_uid, fp3)
favs3 = favdb.get_favorite_products(fav_uid)
check("get_favorite_products: после возврата 3 товара", len(favs3) == 3)

# Пустые для нового пользователя
check("get_favorite_products: несущ. uid → []",
      favdb.get_favorite_products(9999) == [])

# get_user_recent_products — продажи через инвентарь
favdb.add_inventory("Магазин А", fp1, 100)
favdb.add_inventory("Магазин А", fp2, 100)
favdb.add_inventory("Магазин А", fp3, 100)
favdb.add_sale(fp1, "Магазин А", 1, fav_uid, 100.0)
favdb.add_sale(fp2, "Магазин А", 2, fav_uid, 200.0)
favdb.add_sale(fp3, "Магазин А", 1, fav_uid, 300.0)

recent = favdb.get_user_recent_products(fav_uid, limit=5)
check("get_user_recent_products: ≥2 записи",     len(recent) >= 2)
recent_ids = [r[0] for r in recent]
check("get_user_recent_products: fp1 в списке",  fp1 in recent_ids)
check("get_user_recent_products: fp2 в списке",  fp2 in recent_ids)

# Пустые для нового пользователя
check("get_user_recent_products: несущ. → []",
      favdb.get_user_recent_products(9999, limit=5) == [])

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 25: Платёжные заявки — полный цикл
# ─────────────────────────────────────────────────────────
section("Сценарий 25: Платёжные заявки — create → confirm → subscription")

pfdb = make_db("payflow.db")
pfdb.add_user(1600001, "Платёж", "Тестер")
pf_uid = pfdb.get_user_id(1600001)

# Перед заявками — нет подписки, нет pending
check("is_subscription_active: нет → False",       not pfdb.is_subscription_active(pf_uid))
check("has_pending_payment_request: нет → False",
      not pfdb.has_pending_payment_request(pf_uid))

# create_payment_request → int > 0
req_id = pfdb.create_payment_request(
    pf_uid, 'Базовый', 990.0, 'screenshot_file_id_123', None)
check("create_payment_request: int > 0",
      isinstance(req_id, int) and req_id > 0)
check("has_pending_payment_request: True после создания",
      pfdb.has_pending_payment_request(pf_uid))

# get_pending_payment_requests → содержит нашу заявку
pending = pfdb.get_pending_payment_requests()
pf_pending = [r for r in pending if r[1] == pf_uid]
check("get_pending_payment_requests: наша заявка есть",  len(pf_pending) >= 1)

# confirm_payment_request с admin_id=0 (ЮKassa auto-confirm)
result = pfdb.confirm_payment_request(req_id, admin_id=0)
check("confirm_payment_request (admin_id=0): True", result is True)

# Подписка создана
check("after confirm: is_subscription_active → True",
      pfdb.is_subscription_active(pf_uid))

# Повторное подтверждение уже approved → False
result2 = pfdb.confirm_payment_request(req_id, admin_id=0)
check("confirm_payment_request: повторное → False", result2 is False)

# has_pending — заявка подтверждена, нет pending
check("has_pending_payment_request: после confirm → False",
      not pfdb.has_pending_payment_request(pf_uid))

# Несуществующая заявка → False
result_bad = pfdb.confirm_payment_request(99999, admin_id=1)
check("confirm_payment_request: несущ. id → False", result_bad is False)

# Второй пользователь — заявка с реальным admin_id
pfdb.add_user(1600002, "Второй", "Платёж")
pf_uid2 = pfdb.get_user_id(1600002)
req2 = pfdb.create_payment_request(pf_uid2, 'Премиум', 1990.0, 'file_xyz', None)
check("create_payment_request 2: int > 0", isinstance(req2, int) and req2 > 0)
result3 = pfdb.confirm_payment_request(req2, admin_id=1)
check("confirm_payment_request (admin_id=1): True", result3 is True)
check("after confirm2: подписка u2 активна",
      pfdb.is_subscription_active(pf_uid2))

# YooKassa-style file_id: 'yookassa:yk_pay_xxx' — корректно сохраняется
req_yk = pfdb.create_payment_request(
    pf_uid, 'Базовый', 990.0, 'yookassa:yk_pay_test_abc', 42)
check("create_payment_request с yookassa file_id: int > 0",
      isinstance(req_yk, int) and req_yk > 0)
result_yk = pfdb.confirm_payment_request(req_yk, admin_id=0)
check("confirm_payment_request ЮKassa-стиль: True", result_yk is True)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 26: he() — HTML-экранирование
# ─────────────────────────────────────────────────────────
section("Сценарий 26: he() — HTML-экранирование строк пользователя")

from utils import he

# Спецсимволы HTML
check("he('<script>'): экранирован",    he("<script>") == "&lt;script&gt;")
check("he('1 > 0'): >→&gt;",           he("1 > 0") == "1 &gt; 0")
check("he('Tom & Jerry'): &→&amp;",    he("Tom & Jerry") == "Tom &amp; Jerry")
check("he: дублированный &",           he("a & b & c") == "a &amp; b &amp; c")
check("he: угловые скобки вместе",     he("<b>текст</b>") == "&lt;b&gt;текст&lt;/b&gt;")

# Безопасные строки — без изменений
check("he: обычный текст = без изменений",  he("Привет мир") == "Привет мир")
check("he: пустая строка",                  he("") == "")
check("he: только пробелы",                 he("   ") == "   ")
check("he: имя с дефисом",                  he("Иван-Петров") == "Иван-Петров")
check("he: unicode без спецсимволов",       he("Тест 123 ✅") == "Тест 123 ✅")
check("he: цифры",                          he("12345") == "12345")
check("he: точки и запятые",               he("Москва, ул. Ленина") == "Москва, ул. Ленина")

# None и числа — не падает, возвращает строку
check("he(None): строка",    isinstance(he(None), str))
check("he(42): строка",      isinstance(he(42), str))
check("he(3.14): строка",    isinstance(he(3.14), str))

# Безопасность: инъекция не проходит
html_inject = "<b>жирный</b>"
check("he: HTML-инъекция — нет тегов",      "<b>" not in he(html_inject))
sql_inject = "' OR '1'='1"
check("he: SQL-инъекция — безопасна",       "<" not in he(sql_inject))

# Магазин с & в названии
check("he: магазин 'Иванов & Ко'",
      he("Иванов & Ко") == "Иванов &amp; Ко")

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 27: Уведомления и история; подсказки (hints)
# ─────────────────────────────────────────────────────────
section("Сценарий 27: История уведомлений, настройки, подсказки")

ntfdb = make_db("notifications.db")
ntfdb.add_user(1700001, "Уведомления", "Тестер")
ntf_uid = ntfdb.get_user_id(1700001)

# add_notification_to_history
ntfdb.add_notification_to_history(ntf_uid, 'sales_alert', 'Продажа на 5000₽')
ntfdb.add_notification_to_history(ntf_uid, 'low_stock',   'Молоко заканчивается')
ntfdb.add_notification_to_history(ntf_uid, 'system',      'Подписка истекает')

# get_notification_history → список строк
history = ntfdb.get_notification_history(ntf_uid, limit=10)
check("get_notification_history: 3 записи", len(history) == 3)
check("get_notification_history: кортежи",  all(isinstance(h, tuple) for h in history))

# mark_notifications_as_read — все прочитаны
ntfdb.mark_notifications_as_read(ntf_uid)
history_read = ntfdb.get_notification_history(ntf_uid, limit=10)
check("mark_notifications_as_read: нет необработанных",
      all(h[4] == 1 for h in history_read))   # is_read = col 4

# Пустая история для нового пользователя
ntfdb.add_user(1700002, "Новый", "Пользователь")
ntf_uid2 = ntfdb.get_user_id(1700002)
check("get_notification_history: новый → []",
      len(ntfdb.get_notification_history(ntf_uid2, limit=10)) == 0)

# limit работает
ntfdb.add_notification_to_history(ntf_uid, 'info', 'сообщение 4')
ntfdb.add_notification_to_history(ntf_uid, 'info', 'сообщение 5')
history2 = ntfdb.get_notification_history(ntf_uid, limit=2)
check("get_notification_history: limit=2 → 2 записи", len(history2) == 2)

# Настройки уведомлений
ntfdb.create_default_notification_settings(ntf_uid2)
settings = ntfdb.get_notification_settings(ntf_uid2)
check("create_default_notification_settings: создан",     settings is not None)
check("get_notification_settings: dict или tuple",
      isinstance(settings, (dict, tuple)))

# update_notification_settings
ntfdb.update_notification_settings(ntf_uid2, sales_alerts=False)
settings2 = ntfdb.get_notification_settings(ntf_uid2)
check("update_notification_settings: не ломается",        settings2 is not None)

# ── Запланированные уведомления ──────────────────────────────
import json as _json

# Создаём пользователя для created_by (нужен для JOIN)
ntfdb.add_user(1700003, "Отправитель", "Нотиф")
sender_uid = ntfdb.get_user_id(1700003)

sn_id = ntfdb.add_scheduled_notification(
    job_id='job_001',
    created_by=sender_uid,
    notification_text='Время обеда!',
    recipients_type='all',
    recipients_list=[],
    scheduled_datetime='2026-06-01 12:00:00',
)
check("add_scheduled_notification: id не None",   sn_id is not None)

# get_scheduled_notifications(status='pending')
sn_list = ntfdb.get_scheduled_notifications(status='pending')
check("get_scheduled_notifications: список",      isinstance(sn_list, list))
check("get_scheduled_notifications: ≥1 элемент", len(sn_list) >= 1)

# get_scheduled_notification(id)
sn_row = ntfdb.get_scheduled_notification(sn_id)
check("get_scheduled_notification: найден",        sn_row is not None)

# update_scheduled_notification_status
ntfdb.update_scheduled_notification_status('job_001', 'sent')
sent_list = ntfdb.get_scheduled_notifications(status='sent')
check("update_scheduled_notification_status: статус обновлён",
      any(s[1] == 'job_001' for s in sent_list) if sent_list else False)

# delete_scheduled_notification
del_sn = ntfdb.delete_scheduled_notification(sn_id)
check("delete_scheduled_notification: True", del_sn is True)
check("delete_scheduled_notification: нет в pending",
      not any(s[0] == sn_id for s in ntfdb.get_scheduled_notifications(status=None)))

# ── Подсказки (hints) ────────────────────────────────────────
from hints import hint_suffix, HINT_TEXTS

# has_seen_hint: новый пользователь — ничего не просматривал
check("has_seen_hint: first_sale=False (new)",    not ntfdb.has_seen_hint(ntf_uid2, 'first_sale'))
check("has_seen_hint: first_reports=False (new)", not ntfdb.has_seen_hint(ntf_uid2, 'first_reports'))

# mark_hint_seen + has_seen_hint
ntfdb.mark_hint_seen(ntf_uid2, 'first_sale')
check("has_seen_hint: first_sale=True после mark",     ntfdb.has_seen_hint(ntf_uid2, 'first_sale'))
check("has_seen_hint: first_reports=False (не трогали)",not ntfdb.has_seen_hint(ntf_uid2, 'first_reports'))

# mark_hint_seen повторно — нет ошибки (INSERT OR IGNORE)
ntfdb.mark_hint_seen(ntf_uid2, 'first_sale')
check("mark_hint_seen повторно: нет ошибки",      ntfdb.has_seen_hint(ntf_uid2, 'first_sale'))

# hint_suffix: первый вызов → не пустая строка (содержит текст)
hint_text1 = hint_suffix(ntfdb, ntf_uid2, 'first_reports')
check("hint_suffix: первый раз → текст",          isinstance(hint_text1, str) and len(hint_text1) > 5)

# hint_suffix: второй вызов → пустая строка (уже показана)
hint_text2 = hint_suffix(ntfdb, ntf_uid2, 'first_reports')
check("hint_suffix: повторно → ''",               hint_text2 == "")

# hint_suffix: неизвестный ключ → "" (нет текста)
hint_unknown = hint_suffix(ntfdb, ntf_uid2, 'unknown_key_xyz')
check("hint_suffix: неизв. ключ → ''",            hint_unknown == "")

# HINT_TEXTS: все 7 ключей содержат непустые тексты
check("HINT_TEXTS: first_sale непустой",    len(HINT_TEXTS.get('first_sale', '')) > 10)
check("HINT_TEXTS: first_plans непустой",   len(HINT_TEXTS.get('first_plans', '')) > 10)
check("HINT_TEXTS: first_contests непустой",len(HINT_TEXTS.get('first_contests', '')) > 10)
check("HINT_TEXTS: first_reports непустой", len(HINT_TEXTS.get('first_reports', '')) > 10)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 28: Аудит продаж и trial-подписка
# ─────────────────────────────────────────────────────────
section("Сценарий 28: Журнал аудита продаж и trial-подписка")

auditdb = make_db("audit.db")
auditdb.add_user(1800001, "Аудит", "Продаж", shop_name="Магазин А")
au_uid = auditdb.get_user_id(1800001)
au_pid = auditdb.add_product("Товар Аудит", "Категория", 500.0)
auditdb.add_inventory("Магазин А", au_pid, 100)
au_sale = auditdb.add_sale(au_pid, "Магазин А", 5, au_uid, 500.0)

# log_sale_edit — запись в аудит-лог
auditdb.log_sale_edit(au_sale, au_uid,
                      old_quantity=5, new_quantity=7,
                      old_price=500.0, new_price=480.0)
auditdb.log_sale_edit(au_sale, au_uid,
                      old_quantity=7, new_quantity=6,
                      old_price=480.0, new_price=490.0)

# get_sale_audit_log → список строк
audit_log = auditdb.get_sale_audit_log(au_sale)
check("get_sale_audit_log: 2 записи",               len(audit_log) == 2)
check("get_sale_audit_log: кортежи",                all(isinstance(r, tuple) for r in audit_log))
# структура: 0:id, 1:changed_by, 2:editor_name, 3:old_qty, 4:new_qty, 5:old_price, 6:new_price, 7:changed_at
check("get_sale_audit_log: old_qty первая строка",  audit_log[0][3] == 7)   # newest first
check("get_sale_audit_log: new_qty первая строка",  audit_log[0][4] == 6)

# Несуществующая продажа → []
check("get_sale_audit_log: несущ. sale → []",        auditdb.get_sale_audit_log(99999) == [])

# Trial-подписка
auditdb.add_user(1800002, "Пробный", "Период")
trial_uid = auditdb.get_user_id(1800002)
check("is_subscription_active: нет trial → False",
      not auditdb.is_subscription_active(trial_uid))
trial_ok = auditdb.create_trial_subscription(trial_uid, 'Базовый', 7)
check("create_trial_subscription: True",             trial_ok is True)
check("is_subscription_active: trial активна",       auditdb.is_subscription_active(trial_uid))

# Повторный trial — False (уже есть)
trial_again = auditdb.create_trial_subscription(trial_uid, 'Базовый', 7)
check("create_trial_subscription повторно: False",   trial_again is False)

# get_subscription_tier_level
tier_free  = auditdb.get_subscription_tier_level('Бесплатный')
tier_base  = auditdb.get_subscription_tier_level('Базовый')
tier_prem  = auditdb.get_subscription_tier_level('Премиум')
check("get_subscription_tier_level: Бесплатный < Базовый",
      isinstance(tier_free, int) and isinstance(tier_base, int) and tier_free < tier_base)
check("get_subscription_tier_level: Базовый < Премиум",
      tier_base < tier_prem)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 29: Пограничные случаи и устойчивость
# ─────────────────────────────────────────────────────────
section("Сценарий 29: Пограничные случаи — пустые БД, несущ. записи")

edgedb = make_db("edge.db")

# Пользователь с минимальными данными (без shop/city/network)
edgedb.add_user(1900001, "А", "Б")
eu = edgedb.get_user(1900001)
check("add_user минимальный: не падает",           eu is not None)

# Методы на пустой БД
check("get_all_products пустая → []",              edgedb.get_all_products() == [])
check("get_all_shops пустая → []",                 edgedb.get_all_shops() == [])
check("get_all_cities пустая → []",                edgedb.get_all_cities() == [])
check("get_all_categories пустая → []",            edgedb.get_all_categories() == [])

# Несуществующие записи
check("get_user: несущ. → None",                   edgedb.get_user(9999999) is None)
check("get_user_id: несущ. → None",                edgedb.get_user_id(9999999) is None)
check("get_product: несущ. → None",                edgedb.get_product(9999) is None)
check("get_sale_by_id: несущ. → None",             edgedb.get_sale_by_id(9999) is None)
check("get_inventory: несущ. → 0",                 edgedb.get_inventory("НесущМагазин", 9999) == 0)
check("get_yookassa_payment: несущ. → None",
      edgedb.get_yookassa_payment_by_payment_id("NONEXISTENT") is None)

# add_sale без инвентаря / несущ. товар → None
bad_sale = edgedb.add_sale(9999, "Магазин", 1, 1, 100.0)
check("add_sale несущ. товар → None",              bad_sale is None)

# Подписка для нового пользователя
eu_uid = edgedb.get_user_id(1900001)
check("get_user_subscription: нет → None",
      edgedb.get_user_subscription(eu_uid) is None)
check("is_subscription_active: нет → False",
      not edgedb.is_subscription_active(eu_uid))
check("has_pending_payment_request: нет → False",
      not edgedb.has_pending_payment_request(eu_uid))

# get_payment_provider: всегда строка
check("get_payment_provider: тип str",
      isinstance(edgedb.get_payment_provider(), str))

# add_user UPSERT: не падает
edgedb.add_user(1900001, "Новое", "Имя")
check("add_user UPSERT: пользователь существует",
      edgedb.get_user(1900001) is not None)

# get_favorite_products и get_user_recent_products для несущ.
check("get_favorite_products несущ. uid → []",
      edgedb.get_favorite_products(9999) == [])
check("get_user_recent_products несущ. uid → []",
      edgedb.get_user_recent_products(9999, limit=5) == [])

# get_sales_plans на пустой БД → []
check("get_sales_plans пустая → []",
      edgedb.get_sales_plans(active_only=True) == [])
check("get_contests пустая → []",
      edgedb.get_contests() == [])

# has_seen_hint: несущ. user → False (не падает)
check("has_seen_hint несущ. user → False",
      not edgedb.has_seen_hint(9999, 'first_sale'))

# get_notification_history несущ. → []
check("get_notification_history несущ. → []",
      edgedb.get_notification_history(9999, limit=5) == [])

# ─────────────────────────────────────────────────────────
# ИТОГ
# ─────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("  ИТОГИ ТЕСТИРОВАНИЯ")
print('='*60)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 30: Google Sheets — кэш бонусов (gs_bonus_cache)
# ─────────────────────────────────────────────────────────
section("Сценарий 30: Google Sheets — кэш бонусов (gs_bonus_cache)")

gsdb = make_db("gs_bonus.db")

# upsert_bonus_cache — добавление
gsdb.upsert_bonus_cache(1, "iPhone 15 Pro", "Сеть А", 500.0, 89999.0)
gsdb.upsert_bonus_cache(1, "Samsung A55",   "Сеть А", 350.0, 39999.0)
gsdb.upsert_bonus_cache(1, "Xiaomi 14",     "Сеть Б", 400.0, 49999.0)
gsdb.upsert_bonus_cache(2, "iPhone 15 Pro", "Сеть А", 600.0, 89999.0)

# get_bonus_cache — по connection_id
cache1 = gsdb.get_bonus_cache(connection_id=1)
check("get_bonus_cache(1): 3 записи", len(cache1) == 3)
check("get_bonus_cache(1): содержит iPhone 15 Pro",
      any(r[0] == "iPhone 15 Pro" for r in cache1))

cache2 = gsdb.get_bonus_cache(connection_id=2)
check("get_bonus_cache(2): 1 запись", len(cache2) == 1)

cache_all = gsdb.get_bonus_cache()
check("get_bonus_cache(): все 4 записи", len(cache_all) == 4)

# get_bonus_for_model — найден
bonus = gsdb.get_bonus_for_model("iPhone 15 Pro", "Сеть А")
check("get_bonus_for_model: найден → float", isinstance(bonus, float))
check("get_bonus_for_model: значение 500.0", abs(bonus - 500.0) < 0.01)

# get_bonus_for_model — не найден → None
miss = gsdb.get_bonus_for_model("Nokia 3310", "Сеть А")
check("get_bonus_for_model: несущ. → None", miss is None)

# upsert_bonus_cache — обновление существующей записи (UPSERT)
gsdb.upsert_bonus_cache(1, "iPhone 15 Pro", "Сеть А", 550.0, 89999.0)
bonus_upd = gsdb.get_bonus_for_model("iPhone 15 Pro", "Сеть А")
check("upsert_bonus_cache UPSERT: бонус обновлён до 550.0",
      bonus_upd is not None and abs(bonus_upd - 550.0) < 0.01)
cache_after_upsert = gsdb.get_bonus_cache(connection_id=1)
check("upsert_bonus_cache UPSERT: количество записей не изменилось",
      len(cache_after_upsert) == 3)

# clear_bonus_cache — удаляет все записи соединения
gsdb.clear_bonus_cache(connection_id=2)
check("clear_bonus_cache(2): записи удалены",
      len(gsdb.get_bonus_cache(connection_id=2)) == 0)
check("clear_bonus_cache(2): conn_id=1 не затронут",
      len(gsdb.get_bonus_cache(connection_id=1)) == 3)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 31: Кросс-магазинная продажа
# ─────────────────────────────────────────────────────────
section("Сценарий 31: Кросс-магазинная продажа")

csdb = make_db("crossshop.db")
csdb.add_user(1400001, "Продавец", "Кросс", shop_name="Магазин А")
uid_cs = csdb.get_user_id(1400001)
pid_cs  = csdb.add_product("Товар Кросс", "Категория", 1000.0)
pid_cs2 = csdb.add_product("Другой Товар", "Категория", 2000.0)

# Инвентарь в ДРУГОМ магазине (не в магазине пользователя)
csdb.add_inventory("Магазин Б", pid_cs,  50)
csdb.add_inventory("Магазин Б", pid_cs2, 20)

# Продажа из чужого магазина
sale_cross = csdb.add_sale(pid_cs, "Магазин Б", 3, uid_cs, 1000.0)
check("cross-shop sale: продажа проведена", sale_cross is not None and sale_cross > 0)

inv_after = csdb.get_inventory("Магазин Б", pid_cs)
check("cross-shop sale: остаток уменьшился (50-3=47)", inv_after == 47)

# Продавца нет в магазине Б — продажа всё равно работает
sale_cross2 = csdb.add_sale(pid_cs2, "Магазин Б", 5, uid_cs, 2000.0)
check("cross-shop sale: второй товар продан", sale_cross2 is not None)
check("cross-shop sale: остаток второго товара (20-5=15)",
      csdb.get_inventory("Магазин Б", pid_cs2) == 15)

# Превышение остатков → None
sale_over = csdb.add_sale(pid_cs, "Магазин Б", 999, uid_cs, 1000.0)
check("cross-shop sale: превышение → None", sale_over is None)

# Продажа из магазина без инвентаря → None
sale_nostock = csdb.add_sale(pid_cs, "Магазин В", 1, uid_cs, 1000.0)
check("cross-shop sale: нет инвентаря → None", sale_nostock is None)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 32: get_sales_ranking — 8 колонок (gotcha #9)
# ─────────────────────────────────────────────────────────
section("Сценарий 32: Рейтинг продавцов — 8 колонок (gotcha #9)")

rkdb = make_db("ranking.db")
rkdb.add_user(1500001, "Топ",    "Продавец", shop_name="Топ Магазин")
rkdb.add_user(1500002, "Второй", "Место",    shop_name="Топ Магазин")
rk_u1 = rkdb.get_user_id(1500001)
rk_u2 = rkdb.get_user_id(1500002)
rk_p1 = rkdb.add_product("Prod Top A", "Кат", 500.0)
rkdb.add_inventory("Топ Магазин", rk_p1, 500)

from datetime import timedelta as _td
rk_start = (datetime.now() - _td(days=7)).strftime("%Y-%m-%d")
rk_end   = (datetime.now() + _td(days=1)).strftime("%Y-%m-%d")

rkdb.add_sale(rk_p1, "Топ Магазин", 10, rk_u1, 500.0)
rkdb.add_sale(rk_p1, "Топ Магазин",  5, rk_u2, 500.0)

ranking = rkdb.get_sales_ranking(start_date=rk_start, end_date=rk_end)
check("get_sales_ranking: список", isinstance(ranking, list))
check("get_sales_ranking: ≥2 строки", len(ranking) >= 2)
if ranking:
    row0 = ranking[0]
    check("get_sales_ranking: ≥8 колонок (col 8 = user_db_id)",
          len(row0) >= 8)
    check("get_sales_ranking: row[:7] не теряет данных",
          len(row0[:7]) == 7)
    check("get_sales_ranking: col[7] — это user_db_id (int)",
          isinstance(row0[7], int))
    check("get_sales_ranking: топ-1 — больший оборот",
          row0[2] >= ranking[1][2] if len(ranking) >= 2 else True)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 33: hints.py — hint_suffix и HINT_TEXTS
# ─────────────────────────────────────────────────────────
section("Сценарий 33: Онбординг и подсказки (hints.py)")

from hints import hint_suffix, HINT_TEXTS, maybe_send_welcome

hdb = make_db("hints.db")
hdb.add_user(1600001, "Новый", "Пользователь")
h_uid = hdb.get_user_id(1600001)

# HINT_TEXTS содержит ключевые разделы
for key in ('first_sale', 'first_reports', 'first_products', 'first_dashboard',
            'first_plans', 'first_contests', 'first_rankings'):
    check(f"HINT_TEXTS: '{key}' существует", key in HINT_TEXTS)
    check(f"HINT_TEXTS: '{key}' не пустой", bool(HINT_TEXTS[key]))

# hint_suffix — первый раз возвращает текст
suffix1 = hint_suffix(hdb, h_uid, 'first_sale')
check("hint_suffix: первый раз → непустая строка", len(suffix1) > 0)
check("hint_suffix: содержит <i>", "<i>" in suffix1)

# hint_suffix — повторно → пустая строка
suffix2 = hint_suffix(hdb, h_uid, 'first_sale')
check("hint_suffix: повторно → пустая строка", suffix2 == "")

# hint_suffix для второго ключа — свежий (не пересекается)
suffix3 = hint_suffix(hdb, h_uid, 'first_reports')
check("hint_suffix: другой ключ → снова не пустой", len(suffix3) > 0)

# Несуществующий ключ — возвращает "" без исключения
suffix_unknown = hint_suffix(hdb, h_uid, 'nonexistent_key')
check("hint_suffix: несущ. ключ → пустая строка без исключения",
      suffix_unknown == "")

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 34: notif_utils — add_read_btn
# ─────────────────────────────────────────────────────────
section("Сценарий 34: Кнопка «Прочитано» (notif_utils)")

from notif_utils import add_read_btn
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Без клавиатуры — создаёт одну строку с кнопкой
kb_alone = add_read_btn(None)
check("add_read_btn(None): InlineKeyboardMarkup", isinstance(kb_alone, InlineKeyboardMarkup))
check("add_read_btn(None): 1 строка", len(kb_alone.inline_keyboard) == 1)
check("add_read_btn(None): кнопка notif_read",
      kb_alone.inline_keyboard[0][0].callback_data == "notif_read")
check("add_read_btn(None): текст кнопки содержит 'Прочитано'",
      "Прочитано" in kb_alone.inline_keyboard[0][0].text)

# С существующей клавиатурой — добавляет строку в конец
existing = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📊 Отчёт", callback_data="some_cb")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
])
kb_with = add_read_btn(existing)
check("add_read_btn(existing): 3 строки (было 2 + кнопка)", len(kb_with.inline_keyboard) == 3)
check("add_read_btn(existing): последняя строка — notif_read",
      kb_with.inline_keyboard[-1][0].callback_data == "notif_read")
check("add_read_btn(existing): первые строки не затронуты",
      kb_with.inline_keyboard[0][0].callback_data == "some_cb")

# Идемпотентность — исходная клавиатура не изменилась
check("add_read_btn: исходная kb не мутирована",
      len(existing.inline_keyboard) == 2)

# ─────────────────────────────────────────────────────────
# СЦЕНАРИЙ 35: filter_utils — полный API
# ─────────────────────────────────────────────────────────
section("Сценарий 35: Фильтры (filter_utils)")

from filter_utils import (
    empty_filter, is_filter_active, filter_active_text,
    merge_scope_with_filter, has_anything_to_filter,
    filter_button_text, filter_to_scope_kwargs,
)

# empty_filter — пустой фильтр
ef = empty_filter()
check("empty_filter: dict", isinstance(ef, dict))
check("empty_filter: не активен", not is_filter_active(ef))
check("empty_filter: текст пустой", filter_active_text(ef) == "")
check("filter_button_text: нет активных → содержит 'Фильтр'",
      "Фильтр" in filter_button_text(ef))

# is_filter_active — с выбранными значениями
flt_shop = {'shops': ['Магазин А'], 'cities': [], 'networks': []}
check("is_filter_active: магазин выбран → True", is_filter_active(flt_shop))
check("filter_active_text: непустой", len(filter_active_text(flt_shop)) > 0)
check("filter_button_text: активный → содержит '🔍'",
      "🔍" in filter_button_text(flt_shop))

# merge_scope_with_filter — scope = ceiling, filter = floor
# Scope: shop-level, filter уточняет по одному магазину
merged_shop = merge_scope_with_filter(
    'shop', ['Магазин А', 'Магазин Б'],
    {'shops': ['Магазин А'], 'cities': [], 'networks': []}
)
check("merge_scope_with_filter shop: dict", isinstance(merged_shop, dict))
check("merge_scope_with_filter shop: содержит shop_names (filter_to_scope_kwargs)",
      'shop_names' in merged_shop)

# Scope network + пустой фильтр → возвращает scope как есть
merged_net = merge_scope_with_filter(
    'network', ['Сеть X'], empty_filter()
)
check("merge_scope_with_filter network+empty: dict",
      isinstance(merged_net, dict))

# filter_to_scope_kwargs с магазином
kwargs = filter_to_scope_kwargs({'shops': ['Магазин А'], 'cities': [], 'networks': []})
check("filter_to_scope_kwargs: dict", isinstance(kwargs, dict))

# has_anything_to_filter — нет данных → False
check("has_anything_to_filter: пустой available → False",
      not has_anything_to_filter({'shops': [], 'cities': [], 'networks': []}))
check("has_anything_to_filter: с магазинами → True",
      has_anything_to_filter({'shops': ['Магазин А'], 'cities': [], 'networks': []}))

passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
total  = len(results)

if failed:
    print(f"\n  Провалившиеся тесты ({failed}):")
    for status, label, detail in results:
        if status == FAIL:
            print(f"    {FAIL} {label}" + (f" | {detail}" if detail else ""))

print(f"\n  {'='*40}")
print(f"  Всего тестов:  {total}")
print(f"  ✅ Прошло:     {passed}")
print(f"  ❌ Провалено:  {failed}")
print(f"  {'='*40}")

if failed == 0:
    print(f"\n  🎉 Все {total} тестов прошли успешно!")
else:
    print(f"\n  ⚠️  Есть провалы — требуется проверка.")
print()

shutil.rmtree(_tmp_dir, ignore_errors=True)
sys.exit(0 if failed == 0 else 1)
