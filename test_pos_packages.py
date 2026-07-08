#!/usr/bin/env python3
"""
test_pos_packages.py — Регресс-тесты продажи абонементов из единой корзины кассы (POS).

Покрывает интеграцию, добавленную в /api/pos/packages и /pos/checkout:
  - GET /api/pos/packages отдаёт только активные шаблоны абонементов;
  - успешная продажа абонемента через /pos/checkout (client_packages создаётся,
    комиссия продавцу начислена через ту же sell_package, что и /packages/sell);
  - без выбранного клиента продажа абонемента блокируется («для абонемента
    нужно выбрать клиента»), но остальные позиции того же чека (товар/услуга)
    всё равно проводятся — единая позиция не должна ронять весь чек;
  - несуществующий/неактивный абонемент → понятная ошибка по позиции;
  - смешанный чек товар + услуга + абонемент проводится одним запросом,
    и все три вида продаж записываются корректно;
  - продажа со страницы «Абонементы» (/packages/sell — через ту же sell_package)
    продолжает работать без регрессий.

Не требует живого соединения с Telegram/браузера — работает через
starlette.testclient.TestClient поверх реального FastAPI-приложения.

Запуск: python3 test_pos_packages.py
Коды выхода: 0 — все проверки прошли; 1 — хотя бы одна упала.
"""
import os
import sys
import tempfile

PASS = "✅"
FAIL = "❌"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


SUPER_ADMIN_TG = 921098636
STAFF_NO_CRM_TG = 555222333  # обычный сотрудник, модуль CRM/абонементы НЕ выдан
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
_SHOP = "PosPkgShop"


def _setup_env_and_cwd() -> str:
    tmp = tempfile.mkdtemp(prefix="ds_pos_pkg_test_")
    os.makedirs(os.path.join(tmp, "data", "tenants"), exist_ok=True)
    os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
    os.environ["ADMIN_CHAT_ID"] = str(SUPER_ADMIN_TG)
    os.environ["WEB_SECRET_KEY"] = "pos-pkg-test-secret"
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

    ok, org_id = tenant_manager.create_organization("POS Pkg Test Org", SUPER_ADMIN_TG)
    if not ok:
        raise RuntimeError(f"create_organization failed: {org_id}")

    conn = sqlite3.connect("data/main.db")
    row = conn.execute("SELECT db_path FROM organizations WHERE id = ?", (org_id,)).fetchone()
    conn.close()
    org_db = row[0]

    db = Database(org_db)
    db.create_tables()

    product_id = db.add_product(name="Тестовый товар", category="Тест", price=100.0)
    db.add_shop(_SHOP)
    db.add_user(telegram_id=SUPER_ADMIN_TG, first_name="Owner", last_name="Test", shop_name=_SHOP)
    db.add_inventory(shop_name=_SHOP, product_id=int(product_id), quantity=50)

    # Сотрудник без модуля CRM/абонементов (для проверки гейта доступа)
    db.add_user(telegram_id=STAFF_NO_CRM_TG, first_name="Staff", last_name="NoCrm", shop_name=_SHOP)

    # Клиент CRM (client_id обязателен для sell_package)
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO clients (first_name, last_name, phone) VALUES (?, ?, ?)",
            ("Клиент", "Тестовый", "+70000000000"),
        )
        conn.commit()
        client_id = cur.lastrowid
    finally:
        conn.close()

    # Услуга + активный шаблон абонемента + отдельный неактивный шаблон
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO services (name, price) VALUES (?, ?)", ("Стрижка", 500.0),
        )
        conn.commit()
        service_id = cur.lastrowid
    finally:
        conn.close()
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO service_packages (name, service_id, visits_total, price, validity_days, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("Абонемент на 5 стрижек", service_id, 5, 4000.0, 90),
        )
        conn.execute(
            "INSERT INTO service_packages (name, service_id, visits_total, price, validity_days, is_active) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            ("Старый неактивный абонемент", service_id, 3, 1000.0, 30),
        )
        conn.commit()
        pkg_id = conn.execute(
            "SELECT id FROM service_packages WHERE name='Абонемент на 5 стрижек'"
        ).fetchone()[0]
        inactive_pkg_id = conn.execute(
            "SELECT id FROM service_packages WHERE name='Старый неактивный абонемент'"
        ).fetchone()[0]
        # Глобальное правило мотивации, чтобы sell_package начислял комиссию
        # продавцу (иначе calc_package_commission молча ничего не создаёт).
        conn.execute(
            "INSERT INTO service_motivation_rules (service_id, scope_type, type, value, is_active) "
            "VALUES (NULL, 'global', 'percentage', 10, 1)"
        )
        conn.commit()
    finally:
        conn.close()

    return org_db, int(product_id), service_id, pkg_id, inactive_pkg_id, client_id


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
    r = client.get(path)
    check(f"GET {path} → 200", r.status_code == 200, f"got {r.status_code}")
    import re
    m = re.search(r'csrfToken:\s*\'([^\']+)\'', r.text) or re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    return m.group(1) if m else ""


def main():
    _setup_env_and_cwd()
    org_db, product_id, service_id, pkg_id, inactive_pkg_id, client_id = _seed()
    client = _client()
    _login(client, org_db)
    csrf = _get_csrf(client)
    check("CSRF-токен получен со страницы /pos", bool(csrf))

    # ── 1. GET /api/pos/packages отдаёт только активный шаблон ──────────────
    r = client.get("/api/pos/packages")
    check("GET /api/pos/packages → 200", r.status_code == 200, f"got {r.status_code}")
    data = r.json()
    pkg_names = [p["name"] for p in data.get("packages", [])]
    check("Активный абонемент присутствует в списке", "Абонемент на 5 стрижек" in pkg_names)
    check("Неактивный абонемент НЕ присутствует в списке", "Старый неактивный абонемент" not in pkg_names)

    # ── 2. Успешная продажа абонемента через кассу (с клиентом) ─────────────
    import json
    from database import Database
    db = Database(org_db)

    def before_counts():
        conn = db.get_connection()
        try:
            cp = conn.execute("SELECT COUNT(*) FROM client_packages").fetchone()[0]
            return cp
        finally:
            conn.close()

    n0 = before_counts()
    items = [{"id": pkg_id, "name": "Абонемент на 5 стрижек", "qty": 1, "price": 4000.0, "type": "package"}]
    r = client.post("/pos/checkout", data={
        "csrf_token": csrf, "shop_name": _SHOP,
        "items_json": json.dumps(items), "client_id": client_id,
    })
    check("Чекаут абонемента с клиентом → 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    resp = r.json()
    check("Чекаут абонемента с клиентом → ok=True", resp.get("ok") is True, str(resp))
    n1 = before_counts()
    check("client_packages: +1 запись после продажи абонемента", n1 == n0 + 1, f"{n0} → {n1}")

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT client_id, price_paid, status FROM client_packages ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    check("Проданный абонемент привязан к правильному клиенту", row is not None and row[0] == client_id)
    check("Цена абонемента записана верно", row is not None and float(row[1]) == 4000.0)
    check("Статус нового абонемента — active", row is not None and row[2] == "active")

    conn = db.get_connection()
    try:
        cp_row = conn.execute(
            "SELECT id FROM client_packages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cp_id = cp_row[0]
        pse = conn.execute(
            "SELECT user_id, commission_amount FROM package_sale_earnings WHERE client_package_id = ?",
            (cp_id,),
        ).fetchone()
    finally:
        conn.close()
    check("package_sale_earnings: запись комиссии создана для проданного абонемента", pse is not None, str(pse))
    if pse is not None:
        internal_uid_admin = db.get_user_id(SUPER_ADMIN_TG) if hasattr(db, "get_user_id") else None
        check("package_sale_earnings.user_id указывает на продавца",
              internal_uid_admin is None or pse[0] == internal_uid_admin, f"pse.user_id={pse[0]}")

    # ── 3. Без клиента — позиция блокируется, но чек не падает целиком ──────
    items_mixed = [
        {"id": product_id, "name": "Тестовый товар", "qty": 2, "price": 100.0, "type": "product"},
        {"id": pkg_id, "name": "Абонемент на 5 стрижек", "qty": 1, "price": 4000.0, "type": "package"},
    ]
    r = client.post("/pos/checkout", data={
        "csrf_token": csrf, "shop_name": _SHOP,
        "items_json": json.dumps(items_mixed), "client_id": 0,
    })
    check("Чекаут без клиента (товар+абонемент) → 200", r.status_code == 200, f"got {r.status_code}")
    resp = r.json()
    check("Товар без клиента продаётся успешно (ok=True)", resp.get("ok") is True, str(resp))
    check("Ошибка по абонементу упоминает необходимость клиента",
          any("клиент" in e.lower() for e in resp.get("errors", [])), str(resp.get("errors")))
    n2 = before_counts()
    check("Абонемент НЕ создан без клиента (count не вырос)", n2 == n1, f"{n1} → {n2}")

    # ── 4. Несуществующий/неактивный абонемент → понятная ошибка ────────────
    items_bad = [{"id": inactive_pkg_id, "name": "Старый неактивный абонемент", "qty": 1, "price": 1000.0, "type": "package"}]
    r = client.post("/pos/checkout", data={
        "csrf_token": csrf, "shop_name": _SHOP,
        "items_json": json.dumps(items_bad), "client_id": client_id,
    })
    resp = r.json()
    check("Неактивный абонемент → ok=False (нет успешных позиций)", resp.get("ok") is False, str(resp))
    check("Ошибка по неактивному абонементу понятная",
          "не найден" in resp.get("error", "").lower() or "неактив" in resp.get("error", "").lower(),
          str(resp))
    n3 = before_counts()
    check("Неактивный абонемент НЕ создал запись", n3 == n2, f"{n2} → {n3}")

    # ── 5. Смешанный чек: товар + услуга + абонемент одним запросом ─────────
    items_full = [
        {"id": product_id, "name": "Тестовый товар", "qty": 1, "price": 100.0, "type": "product"},
        {"id": service_id, "name": "Стрижка", "qty": 1, "price": 500.0, "type": "service"},
        {"id": pkg_id, "name": "Абонемент на 5 стрижек", "qty": 1, "price": 4000.0, "type": "package"},
    ]
    r = client.post("/pos/checkout", data={
        "csrf_token": csrf, "shop_name": _SHOP,
        "items_json": json.dumps(items_full), "client_id": client_id,
    })
    resp = r.json()
    check("Смешанный чек (товар+услуга+абонемент) → ok=True", resp.get("ok") is True, str(resp))
    check("Смешанный чек: 3 позиции проведены", resp.get("sold_count") == 3, str(resp))
    n4 = before_counts()
    check("Смешанный чек: ещё один абонемент создан", n4 == n3 + 1, f"{n3} → {n4}")

    # ── 6. /packages/sell (страница «Абонементы») без регрессий ─────────────
    csrf2 = _get_csrf(client, "/packages")
    r = client.post("/packages/sell", data={
        "csrf_token": csrf2, "package_id": pkg_id, "client_id": client_id,
        "price_paid": "4000", "notes": "",
    }, follow_redirects=False)
    check("/packages/sell продолжает работать (редирект 303)", r.status_code == 303, f"got {r.status_code}")
    n5 = before_counts()
    check("/packages/sell создал запись как раньше", n5 == n4 + 1, f"{n4} → {n5}")

    # ── 7. Гейт доступа: если модуль CRM недоступен по тарифу — абонементы
    # в POS блокируются (сейчас 'crm' — бесплатный модуль (price_monthly=0),
    # поэтому has_module('crm') доступен всем; проверяем сам гейт через
    # monkey-patch billing_utils.has_module, чтобы не зависеть от текущей
    # цены модуля и ловить регрессию, если гейт из pos.py вообще уберут).
    from unittest.mock import patch

    def _fake_has_module(tg, key):
        return key != "crm"

    with patch("billing_utils.has_module", side_effect=_fake_has_module):
        r = client.get("/api/pos/packages")
        check("Без модуля CRM: GET /api/pos/packages → 403", r.status_code == 403, f"got {r.status_code}")

        items_gate = [{"id": pkg_id, "name": "Абонемент на 5 стрижек", "qty": 1, "price": 4000.0, "type": "package"}]
        r = client.post("/pos/checkout", data={
            "csrf_token": csrf, "shop_name": _SHOP,
            "items_json": json.dumps(items_gate), "client_id": client_id,
        })
        check("Без модуля CRM: POST /pos/checkout(package) → 403", r.status_code == 403, f"got {r.status_code}")
        n6 = before_counts()
        check("Без модуля CRM: продажа абонемента НЕ прошла в обход гейта", n6 == n5, f"{n5} → {n6}")

        # Товары остаются доступны без модуля CRM (гейт затрагивает только package)
        items_product_only = [{"id": product_id, "name": "Тестовый товар", "qty": 1, "price": 100.0, "type": "product"}]
        r = client.post("/pos/checkout", data={
            "csrf_token": csrf, "shop_name": _SHOP,
            "items_json": json.dumps(items_product_only), "client_id": 0,
        })
        resp = r.json()
        check("Без модуля CRM: продажа товара (без абонемента) по-прежнему работает", resp.get("ok") is True, str(resp))

    print()
    total = len(results)
    passed = sum(1 for s, _, _ in results if s == PASS)
    print(f"Итого: {passed}/{total} проверок пройдено")
    for status, label, detail in results:
        if status == FAIL:
            print(f"  {status} {label}" + (f" | {detail}" if detail else ""))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
