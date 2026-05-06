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
# ИТОГ
# ─────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("  ИТОГИ ТЕСТИРОВАНИЯ")
print('='*60)

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
