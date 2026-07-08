#!/usr/bin/env python3
"""
test_receipt_id.py — Регресс-тесты для Task #10: Единый чек для смешанной продажи.

Проверяем:
1. Один товар → receipt_id в ответе и в таблице sales
2. Смешанный чекаут (товар + услуга + абонемент) → все три записи получают
   одинаковый receipt_id
3. Два независимых чекаута → разные receipt_id
4. Старая запись без receipt_id (NULL) не ломает /sales
5. Отчётные методы возвращают кортежи ≥ 14 элементов с receipt_id на [13]
6. XLSX-экспорт работает

Запуск: python3 test_receipt_id.py
Коды выхода: 0 — все проверки прошли; 1 — хотя бы одна упала.
"""
import os
import sys
import json
import tempfile

PASS = "✅"
FAIL = "❌"
results = []

SUPER_ADMIN_TG = 921098636
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
_SHOP = "ReceiptTestShop"


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


def _setup_env_and_cwd():
    tmp = tempfile.mkdtemp(prefix="ds_receipt_test_")
    os.makedirs(os.path.join(tmp, "data", "tenants"), exist_ok=True)
    os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
    os.environ["ADMIN_CHAT_ID"] = str(SUPER_ADMIN_TG)
    os.environ["WEB_SECRET_KEY"] = "receipt-test-secret"
    os.environ["WEB_PORT"] = "0"
    if WORKSPACE_DIR not in sys.path:
        sys.path.insert(0, WORKSPACE_DIR)
    os.chdir(tmp)
    return tmp


def _seed():
    from tenant_manager import tenant_manager
    from database import Database
    import sqlite3

    shop_db = Database("data/shop_bot.db")
    shop_db.create_tables()

    ok, org_id = tenant_manager.create_organization("Receipt Test Org", SUPER_ADMIN_TG)
    if not ok:
        raise RuntimeError(f"create_organization failed: {org_id}")

    conn = sqlite3.connect("data/main.db")
    row = conn.execute("SELECT db_path FROM organizations WHERE id = ?", (org_id,)).fetchone()
    conn.close()
    org_db = row[0]

    db = Database(org_db)
    db.create_tables()

    product_id = db.add_product(name="Тест-товар", category="Тест", price=200.0)
    db.add_shop(_SHOP)
    db.add_user(telegram_id=SUPER_ADMIN_TG, first_name="Owner", last_name="Test", shop_name=_SHOP)
    db.add_inventory(shop_name=_SHOP, product_id=int(product_id), quantity=50)

    conn2 = db.get_connection()
    try:
        # Client
        cur = conn2.execute(
            "INSERT INTO clients (first_name, last_name, phone) VALUES (?, ?, ?)",
            ("Тест", "Клиент", "+79990001111"),
        )
        conn2.commit()
        client_id = cur.lastrowid

        # Service
        cur = conn2.execute(
            "INSERT INTO services (name, price, duration_minutes, is_active) VALUES (?, ?, ?, 1)",
            ("Тест-услуга", 500.0, 30),
        )
        conn2.commit()
        service_id = cur.lastrowid

        # Package template
        cur = conn2.execute(
            "INSERT INTO service_packages (name, visits_total, price, is_active) VALUES (?, ?, ?, 1)",
            ("Тест-абонемент", 5, 1000.0),
        )
        conn2.commit()
        pkg_id = cur.lastrowid

        # Global motivation rule (needed for package commission)
        conn2.execute(
            "INSERT INTO service_motivation_rules (service_id, scope_type, type, value, is_active) "
            "VALUES (NULL, 'global', 'percentage', 5, 1)"
        )
        conn2.commit()
    finally:
        conn2.close()

    return org_db, db, int(product_id), service_id, pkg_id, client_id


def _client():
    from starlette.testclient import TestClient
    from web.app import create_web_app
    app = create_web_app()
    return TestClient(app)


def _login(client, org_db):
    from web.auth import create_session_token
    token = create_session_token(SUPER_ADMIN_TG, "Owner", org_db, "owner")
    client.cookies.set("web_session", token)


def _get_csrf(client, path="/pos"):
    import re
    r = client.get(path)
    m = re.search(r"csrfToken:\s*'([^']+)'", r.text) or re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    return m.group(1) if m else ""


def main():
    _setup_env_and_cwd()
    org_db, db, product_id, service_id, pkg_id, client_id = _seed()
    c = _client()
    _login(c, org_db)
    csrf = _get_csrf(c)
    check("CSRF-токен получен", bool(csrf))

    # ── 1. Один товар → receipt_id ─────────────────────────────────────────
    items1 = [{"id": product_id, "name": "Тест-товар", "qty": 1, "price": 200.0, "type": "product"}]
    r = c.post("/pos/checkout", data={
        "csrf_token": csrf, "shop_name": _SHOP,
        "items_json": json.dumps(items1), "client_id": 0,
    })
    resp1 = r.json()
    check("Чекаут 1 товара: ok=True", resp1.get("ok") is True, str(resp1))
    rid1 = resp1.get("receipt_id", "")
    check("Чекаут 1 товара: receipt_id в ответе", bool(rid1), repr(rid1))
    check("receipt_id — 12 верхнерегистровых символов", len(rid1) == 12 and rid1 == rid1.upper(), repr(rid1))

    # Verify it's stored in DB
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT receipt_id FROM sales ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    check("receipt_id сохранён в sales", row and row[0] == rid1, str(row))

    # ── 2. Смешанный чекаут: товар + услуга + абонемент ────────────────────
    items2 = [
        {"id": product_id, "name": "Тест-товар",    "qty": 1, "price": 200.0,  "type": "product"},
        {"id": service_id, "name": "Тест-услуга",   "qty": 1, "price": 500.0,  "type": "service"},
        {"id": pkg_id,     "name": "Тест-абонемент","qty": 1, "price": 1000.0, "type": "package"},
    ]
    r = c.post("/pos/checkout", data={
        "csrf_token": csrf, "shop_name": _SHOP,
        "items_json": json.dumps(items2), "client_id": client_id,
    })
    resp2 = r.json()
    check("Смешанный чекаут: ok=True", resp2.get("ok") is True, str(resp2))
    rid2 = resp2.get("receipt_id", "")
    check("Смешанный чекаут: receipt_id в ответе", bool(rid2), repr(rid2))
    check("Смешанный чекаут: receipt_id отличается от первого", rid2 != rid1, f"{rid1} vs {rid2}")

    conn = db.get_connection()
    try:
        prod_rid  = conn.execute("SELECT receipt_id FROM sales        ORDER BY id DESC LIMIT 1").fetchone()
        appt_rid  = conn.execute("SELECT receipt_id FROM appointments WHERE source='pos_sale' ORDER BY id DESC LIMIT 1").fetchone()
        cp_rid    = conn.execute("SELECT receipt_id FROM client_packages ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()

    check("Смешанный: товар → тот же receipt_id",     prod_rid and prod_rid[0] == rid2, f"prod={prod_rid}")
    check("Смешанный: услуга → тот же receipt_id",    appt_rid and appt_rid[0] == rid2, f"appt={appt_rid}")
    check("Смешанный: абонемент → тот же receipt_id", cp_rid   and cp_rid[0]   == rid2, f"cp={cp_rid}")

    # ── 3. Два независимых чекаута → разные receipt_id ─────────────────────
    r = c.post("/pos/checkout", data={
        "csrf_token": csrf, "shop_name": _SHOP,
        "items_json": json.dumps(items1), "client_id": 0,
    })
    rid3 = r.json().get("receipt_id", "")
    check("Два чекаута → разные receipt_id", rid3 and rid3 != rid2 and rid3 != rid1,
          f"rid1={rid1} rid2={rid2} rid3={rid3}")

    # ── 4. GET /sales загружается без ошибок (в т.ч. для строк с NULL rid) ─
    # Вставляем «старую» запись без receipt_id вручную
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO sales (product_id, shop_name, quantity_sold, sale_date, user_id, sale_price, receipt_id) "
            "VALUES (?, ?, 1, datetime('now'), 1, 99.0, NULL)",
            (product_id, _SHOP),
        )
        conn.commit()
    finally:
        conn.close()

    r = c.get("/sales")
    check("GET /sales: 200 OK", r.status_code == 200, f"status={r.status_code}")
    check("GET /sales: нет Traceback в ответе", "Traceback" not in r.text and "Internal Server Error" not in r.text)

    # ── 5. Отчётные методы: кортежи ≥ 14 с receipt_id на [13] ─────────────
    rows_prod = db.get_sales_report(start_date="2020-01-01", end_date="2099-12-31")
    check("get_sales_report: возвращает строки", bool(rows_prod))
    if rows_prod:
        # берём строку с нашим rid2 (смешанный чекаут — самый свежий товар)
        row_p = next((r for r in rows_prod if r[13] == rid2), rows_prod[0])
        check("get_sales_report: len ≥ 14", len(row_p) >= 14, f"len={len(row_p)}")
        check("get_sales_report: s[12]='product'", row_p[12] == "product", f"s[12]={row_p[12]!r}")
        check("get_sales_report: s[13]=receipt_id (POS строка)", row_p[13] == rid2, f"s[13]={row_p[13]!r}")

    rows_svc = db.get_service_sales_report(start_date="2020-01-01", end_date="2099-12-31")
    check("get_service_sales_report: возвращает строки", bool(rows_svc))
    if rows_svc:
        row_s = rows_svc[0]
        check("get_service_sales_report: len ≥ 14", len(row_s) >= 14, f"len={len(row_s)}")
        check("get_service_sales_report: s[12]='service'", row_s[12] == "service", f"s[12]={row_s[12]!r}")
        check("get_service_sales_report: s[13]=receipt_id",  row_s[13] == rid2, f"s[13]={row_s[13]!r}")

    rows_pkg = db.get_package_sales_report(start_date="2020-01-01", end_date="2099-12-31")
    check("get_package_sales_report: возвращает строки", bool(rows_pkg))
    if rows_pkg:
        row_k = rows_pkg[0]
        check("get_package_sales_report: len ≥ 14", len(row_k) >= 14, f"len={len(row_k)}")
        check("get_package_sales_report: s[12]='package'", row_k[12] == "package", f"s[12]={row_k[12]!r}")
        check("get_package_sales_report: s[13]=receipt_id",  row_k[13] == rid2, f"s[13]={row_k[13]!r}")

    # ── 6. XLSX-экспорт не падает ─────────────────────────────────────────
    r = c.get("/sales/export.xlsx")
    check("GET /sales/export.xlsx: 200 OK", r.status_code == 200, f"status={r.status_code}")

    # ── Итог ─────────────────────────────────────────────────────────────
    print()
    total  = len(results)
    passed = sum(1 for s, _, _ in results if s == PASS)
    print(f"Итого: {passed}/{total} проверок пройдено")
    if passed < total:
        print("  Провалы:")
        for s, lbl, det in results:
            if s != PASS:
                print(f"  ❌ {lbl}" + (f" | {det}" if det else ""))
    return passed == total


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
