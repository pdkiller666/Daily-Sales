#!/usr/bin/env python3
"""
test_web_smoke.py — браузерные smoke-тесты ключевых действий веб-кабинета.

Зачем: регрессия CSP однажды молча сломала множество кнопок (тёмная тема,
канбан, переход в карточку товара, push, скачивание PDF), и это осталось
незамеченным до жалоб. Эти тесты поднимают реальный веб-кабинет, открывают
его настоящим headless-браузером (Playwright + системный Chromium) с теми же
заголовками безопасности (CSP), что и в проде, и проверяют, что:

  1. Inline-обработчики и Alpine.js действительно исполняются (CSP не режет JS).
  2. Переключение тёмной темы работает в шапке.
  3. Переключение тёмной темы работает в шторке «Ещё».
  4. Клик по строке товара открывает карточку товара.
  5. Быстрый перенос карточки в канбане меняет статус (POST /tasks/kanban/move).
  6. Кнопки push-уведомлений присутствуют и их обработчики определены.
  7. Скачивание PDF ценника отдаёт настоящий application/pdf.
  8. Вход по email/паролю устанавливает сессию и ведёт в /dashboard.
  9. POS-корзина: Alpine работает, товар добавляется, итог пересчитывается,
     кнопка «Оформить» активна и продажа записывается.

Изоляция: тест работает в собственном временном каталоге (свежие SQLite-БД),
ничего не пишет в рабочие data/. Аутентификация — через выписанный сессионный
JWT супер-админа (telegram_id 921098636 → полный доступ ко всем модулям).

Запуск: python3 test_web_smoke.py
Коды выхода: 0 — все проверки прошли (или браузер недоступен → SKIP);
             1 — хотя бы одна проверка упала.

Подключено к деплою: deploy.sh запускает этот файл перед синхронизацией.
"""

import os
import sys
import shutil
import socket
import tempfile
import threading
import time
import urllib.request

# ── Супер-админ зашит в env_manager.is_super_admin → полный доступ к модулям ──
SUPER_ADMIN_TG = 921098636
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))

# Константы для тестовых данных
_SMOKE_EMAIL = "smoke@test.local"
_SMOKE_PASSWORD = "smoke-test-1234"
_SMOKE_SHOP = "SmokeMag"


def _find_chromium() -> str:
    """Вернуть путь к исполняемому Chromium или '' если не найден."""
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status in (200, 302, 401):
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _setup_env_and_cwd() -> str:
    """Изолированное окружение: временный каталог + тестовые env-переменные.

    Должно выполняться ДО импорта web.* / database / tenant_manager, потому что
    эти модули читают пути (relative 'data/...') и секреты на этапе импорта.
    """
    tmp = tempfile.mkdtemp(prefix="ds_web_smoke_")
    os.makedirs(os.path.join(tmp, "data", "tenants"), exist_ok=True)

    os.environ.setdefault("BOT_TOKEN", "123456:TEST_SMOKE_TOKEN")
    os.environ["ADMIN_CHAT_ID"] = str(SUPER_ADMIN_TG)
    os.environ["WEB_SECRET_KEY"] = "web-smoke-test-secret"
    os.environ["WEB_PORT"] = "0"
    # Workspace должен оставаться на sys.path, чтобы импорты работали после chdir.
    if WORKSPACE_DIR not in sys.path:
        sys.path.insert(0, WORKSPACE_DIR)

    os.chdir(tmp)
    return tmp


def _seed_data():
    """Создать организацию, товар, задачу, магазин, склад и email-кредентиал.

    Возвращает (org_db_path, product_id).
    """
    from tenant_manager import tenant_manager
    from database import Database

    # Базовые централизованные БД (revocation/billing-таблицы и т.п.).
    shop_db = Database("data/shop_bot.db")
    shop_db.create_tables()

    ok, result = tenant_manager.create_organization("Smoke Test Org", SUPER_ADMIN_TG)
    if not ok:
        raise RuntimeError(f"create_organization failed: {result}")
    org_id = result

    import sqlite3
    conn = sqlite3.connect("data/main.db")
    row = conn.execute(
        "SELECT db_path FROM organizations WHERE id = ?", (org_id,)
    ).fetchone()
    conn.close()
    org_db = row[0]

    db = Database(org_db)
    db.create_tables()

    # ── Базовый товар ────────────────────────────────────────────────────────
    product_id = db.add_product(
        name="Тестовый товар", category="Тест", price=199.0,
        article="SMOKE-001", barcode="4600000000017",
    )
    db.create_task(
        title="Smoke-задача для канбана", description="перенос статуса",
        created_by=0, assign_all=1, priority="normal",
    )

    # ── Магазин + пользователь + склад для POS-теста ─────────────────────────
    db.add_shop(_SMOKE_SHOP)
    db.add_user(
        telegram_id=SUPER_ADMIN_TG,
        first_name="Smoke",
        last_name="Admin",
        shop_name=_SMOKE_SHOP,
    )
    db.add_inventory(
        shop_name=_SMOKE_SHOP,
        product_id=int(product_id),
        quantity=100,
    )

    # ── Email-кредентиал для теста логина ────────────────────────────────────
    from web.auth import hash_password
    pwd_hash = hash_password(_SMOKE_PASSWORD)
    shop_db.create_web_credential(
        email=_SMOKE_EMAIL,
        password_hash=pwd_hash,
        telegram_id=SUPER_ADMIN_TG,
    )

    return org_db, int(product_id)


def _start_server(org_db: str):
    """Поднять uvicorn с реальным веб-приложением в фоновом потоке."""
    import uvicorn
    from web.app import create_web_app

    app = create_web_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server, port


# ─── Проверки (каждая бросает AssertionError при провале) ─────────────────────

def _new_page(context, base_url, console_errors):
    page = context.new_page()
    page.on("console", lambda m: console_errors.append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
    return page


def check_inline_handlers_alive(page, base_url):
    """CSP не режет inline JS: глобальные обработчики кнопок определены."""
    page.goto(f"{base_url}/products", wait_until="networkidle")
    for fn in ("dsToggleDark", "dsRequestNotif", "dsPushTest", "dsPushReconnect"):
        defined = page.evaluate(f"typeof window.{fn} === 'function'")
        assert defined, f"window.{fn} не определена — inline-скрипт заблокирован CSP?"


def check_alpine_initialized(page, base_url):
    """Alpine.js инициализировался (CSP разрешает 'unsafe-eval')."""
    page.goto(f"{base_url}/products", wait_until="networkidle")
    has_alpine = page.evaluate("typeof window.Alpine !== 'undefined'")
    assert has_alpine, "window.Alpine отсутствует — Alpine.js не загрузился"
    # Корневой компонент должен иметь рабочие реактивные данные.
    ok = page.evaluate(
        "(() => { const r = document.querySelector('[x-data]');"
        " return !!(r && window.Alpine && Alpine.$data(r)); })()"
    )
    assert ok, "Alpine.$data(root) недоступен — x-* атрибуты не вычисляются"


def check_theme_toggle_header(page, base_url):
    """Переключение тёмной темы кнопкой в шапке."""
    page.goto(f"{base_url}/products", wait_until="networkidle")
    page.evaluate("document.documentElement.classList.remove('dark');"
                  " localStorage.removeItem('ds_dark')")
    page.evaluate("window.dsToggleDark()")
    assert page.evaluate("document.documentElement.classList.contains('dark')"), \
        "Тёмная тема не включилась"
    assert page.evaluate("localStorage.getItem('ds_dark')") == "1", \
        "ds_dark не сохранился в localStorage"
    page.evaluate("window.dsToggleDark()")
    assert not page.evaluate("document.documentElement.classList.contains('dark')"), \
        "Тёмная тема не выключилась повторным нажатием"


def check_theme_toggle_more_sheet(page, base_url):
    """Переключение тёмной темы из шторки «Ещё» (отдельная кнопка).

    Нижняя навигация и шторка «Ещё» — мобильная фича (на десктопе скрыта через
    sm:hidden), поэтому проверяем на мобильном вьюпорте.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    try:
        page.goto(f"{base_url}/products", wait_until="networkidle")
        page.evaluate("document.documentElement.classList.remove('dark');"
                      " localStorage.removeItem('ds_dark')")
        # Открыть шторку «Ещё» тем же custom-event, что и свайп/кнопка nav.
        page.evaluate("document.dispatchEvent(new Event('ds-open-more'))")
        # Кнопка темы внутри шторки (id=ds-dark-toggle) → видима и кликается.
        btn = page.locator("#ds-dark-toggle")
        btn.wait_for(state="visible", timeout=8000)
        btn.click()
        assert page.evaluate("document.documentElement.classList.contains('dark')"), \
            "Кнопка темы в шторке «Ещё» не сработала"
    finally:
        page.set_viewport_size({"width": 1280, "height": 800})


def check_product_row_click(page, base_url, product_id):
    """Клик по строке товара открывает карточку товара."""
    page.goto(f"{base_url}/products", wait_until="networkidle")
    row = page.locator(f"tr[data-pid='{product_id}']")
    assert row.count() >= 1, "Строка товара не найдена в таблице"
    row.first.click()
    page.wait_for_url(f"**/products/{product_id}", timeout=8000)
    assert page.url.endswith(f"/products/{product_id}"), \
        f"Клик по строке не открыл карточку, URL={page.url}"


def check_kanban_quick_move(page, base_url):
    """Быстрый перенос карточки в канбане → статус меняется через POST."""
    page.goto(f"{base_url}/tasks/kanban", wait_until="networkidle")
    assert "/tasks/kanban" in page.url, \
        f"Канбан недоступен (редирект на {page.url}) — модуль/доступ?"
    card = page.locator(".kanban-card").first
    assert card.count() >= 1, "Нет карточек на канбан-доске"
    # Quick-move кнопки скрыты до hover (opacity-0 group-hover) — наводим курсор.
    card.hover()
    move_btn = card.locator("button[onclick^='moveCard']").first
    assert move_btn.count() >= 1, "Кнопка быстрого переноса не найдена"
    move_btn.click(force=True)
    # Успех подтверждается тостом «Статус изменён …».
    toast = page.locator("#kanban-toast")
    toast.wait_for(state="visible", timeout=8000)
    txt = page.locator("#kanban-toast-inner").inner_text()
    assert "Статус изменён" in txt, f"Перенос карточки не удался, тост: {txt!r}"


def check_push_buttons_present(page, base_url):
    """Кнопки push-уведомлений присутствуют в DOM."""
    page.goto(f"{base_url}/products", wait_until="networkidle")
    for fn in ("dsPushTest", "dsPushReconnect", "dsRequestNotif"):
        assert page.evaluate(f"typeof window.{fn} === 'function'"), \
            f"Обработчик push {fn} не определён"


def check_pdf_download(page, base_url, product_id):
    """Скачивание PDF ценника отдаёт настоящий application/pdf."""
    url = f"{base_url}/products/{product_id}/label?format=pdf&size=58x40"
    resp = page.request.get(url)
    assert resp.status == 200, f"PDF endpoint вернул {resp.status}"
    ctype = resp.headers.get("content-type", "")
    assert "application/pdf" in ctype, f"Content-Type не PDF: {ctype!r}"
    body = resp.body()
    assert body[:4] == b"%PDF", "Тело ответа не начинается с %PDF"


def check_login_email_flow(browser, base_url, console_errors):
    """Вход по email/паролю: форма видна, отправка ведёт в /dashboard.

    Использует свежий browser-context без сессионного cookie, чтобы
    проверить полный путь: /login → POST /auth/email → /dashboard.
    """
    ctx = browser.new_context()
    ctx.on("console", lambda m: console_errors.append(m.text)
           if m.type == "error" else None)
    page = ctx.new_page()
    try:
        # Открываем страницу логина.
        page.goto(f"{base_url}/login", wait_until="networkidle")
        assert page.url.endswith("/login") or "/login" in page.url, \
            f"Страница /login не открылась, URL={page.url}"

        # Переключаемся на вкладку email.
        email_tab = page.locator("#tab-email")
        assert email_tab.count() >= 1, "Вкладка email не найдена на /login"
        email_tab.click()

        # Email-форма должна стать видимой.
        email_pane = page.locator("#pane-email")
        email_pane.wait_for(state="visible", timeout=5000)

        # Заполняем поля.
        page.locator("input[name='email']").fill(_SMOKE_EMAIL)
        page.locator("input[name='password']").fill(_SMOKE_PASSWORD)

        # Отправляем форму.
        page.locator("#pane-email form").evaluate("f => f.submit()")
        page.wait_for_url("**/dashboard", timeout=10000)

        assert "/dashboard" in page.url, \
            f"После успешного логина ожидался /dashboard, получен {page.url}"
    finally:
        page.close()
        ctx.close()


def check_pos_cart(page, base_url, product_id):
    """POS-корзина: Alpine работает, товар добавляется, итог пересчитывается,
    кнопка «Оформить» активна, продажа отправляется на сервер.

    Проверяет:
    - Alpine.js инициализировался на странице /pos.
    - x-cloak: мобильная кнопка корзины скрыта, пока корзина пуста.
    - addToCart добавляет товар: cart.length == 1, cartTotal > 0.
    - Кнопка «Оформить» становится активной (не disabled).
    - POST /pos/checkout проходит без ошибки (ok=true или «Продано N поз.»).
    """
    page.goto(f"{base_url}/pos?shop={_SMOKE_SHOP}", wait_until="networkidle")

    # Alpine должен инициализироваться.
    page.wait_for_function("typeof window.Alpine !== 'undefined'", timeout=10000)
    page.wait_for_function(
        "document.querySelector('[x-data]') && "
        "typeof Alpine.$data(document.querySelector('[x-data]')).cart !== 'undefined'",
        timeout=10000,
    )

    # x-cloak: пока корзина пуста, мобильная кнопка с x-cloak должна быть скрыта.
    mobile_cart_btn = page.locator("button[x-cloak][x-show*='cart.length > 0']")
    if mobile_cart_btn.count() > 0:
        hidden = page.evaluate(
            "(() => {"
            "  const el = document.querySelector('button[x-cloak]');"
            "  if (!el) return true;"
            "  return el.style.display === 'none' || !el.offsetParent;"
            "})()"
        )
        assert hidden, \
            "Элемент с x-cloak виден до добавления товара — Alpine не скрыл его"

    # Ждём загрузки товаров из API.
    page.wait_for_function(
        "Alpine.$data(document.querySelector('[x-data]')).allProducts.length > 0",
        timeout=12000,
    )

    # Добавляем первый доступный товар в корзину через Alpine.
    added = page.evaluate(
        "(() => {"
        "  const comp = Alpine.$data(document.querySelector('[x-data]'));"
        "  if (!comp.allProducts.length) return false;"
        "  comp.addToCart(comp.allProducts[0]);"
        "  return true;"
        "})()"
    )
    assert added, "allProducts пуст — товары не загрузились через API"

    # Короткая пауза, чтобы Alpine обновил реактивные данные.
    time.sleep(0.3)

    # cart.length == 1, cartTotal > 0.
    cart_len = page.evaluate(
        "Alpine.$data(document.querySelector('[x-data]')).cart.length"
    )
    assert cart_len >= 1, f"После addToCart корзина пуста: cart.length={cart_len}"

    cart_total = page.evaluate(
        "Alpine.$data(document.querySelector('[x-data]')).cartTotal"
    )
    assert cart_total > 0, f"cartTotal не пересчитался: {cart_total}"

    # Кнопка «Провести продажу» должна стать активной.
    checkout_btn = page.locator("button").filter(has_text="Провести продажу").first
    assert checkout_btn.count() >= 1, "Кнопка «Провести продажу» не найдена"
    assert not checkout_btn.is_disabled(), \
        "Кнопка «Провести продажу» всё ещё disabled после добавления товара"

    # Отправляем корзину через Alpine checkout() и ждём результата.
    # Результат: либо successData появился, либо checkoutError (пишем в assert).
    page.evaluate(
        "Alpine.$data(document.querySelector('[x-data]')).checkout()"
    )
    # Ждём либо successData (ok), либо checkoutError (fail) — максимум 8 сек.
    page.wait_for_function(
        "(() => {"
        "  const c = Alpine.$data(document.querySelector('[x-data]'));"
        "  return !!c.successData || !!c.checkoutError;"
        "})()",
        timeout=8000,
    )
    result_state = page.evaluate(
        "(() => {"
        "  const c = Alpine.$data(document.querySelector('[x-data]'));"
        "  return { ok: !!c.successData, err: c.checkoutError || '' };"
        "})()"
    )
    assert result_state["ok"], \
        f"POS checkout не прошёл: {result_state['err']!r}"


def check_cyrillic_content_disposition(page, base_url, product_id):
    """Content-Disposition для товара с кириллическим именем содержит RFC 5987 filename*."""
    url = f"{base_url}/products/{product_id}/label?format=pdf&size=58x40"
    resp = page.request.get(url)
    assert resp.status == 200, f"PDF endpoint вернул {resp.status} для кириллического товара"
    cd = resp.headers.get("content-disposition", "")
    assert "filename*=UTF-8''" in cd, (
        f"Content-Disposition не содержит RFC 5987 filename*: {cd!r}. "
        "Кириллические имена файлов будут вызывать 500."
    )
    assert "filename=" in cd, (
        f"Content-Disposition не содержит ASCII-fallback filename=: {cd!r}"
    )


def _run_checks(page, base_url, product_id, browser, console_errors):
    """Запустить все проверки, вернуть список (name, ok, error)."""
    checks = [
        ("inline handlers alive (CSP)", lambda: check_inline_handlers_alive(page, base_url)),
        ("Alpine.js initialized (CSP)", lambda: check_alpine_initialized(page, base_url)),
        ("theme toggle — header", lambda: check_theme_toggle_header(page, base_url)),
        ("theme toggle — «Ещё» sheet", lambda: check_theme_toggle_more_sheet(page, base_url)),
        ("product row click → card", lambda: check_product_row_click(page, base_url, product_id)),
        ("kanban quick move", lambda: check_kanban_quick_move(page, base_url)),
        ("push buttons present", lambda: check_push_buttons_present(page, base_url)),
        ("PDF label download", lambda: check_pdf_download(page, base_url, product_id)),
        ("login — email/password flow", lambda: check_login_email_flow(browser, base_url, console_errors)),
        ("POS cart — add item + checkout", lambda: check_pos_cart(page, base_url, product_id)),
        ("Cyrillic filename → RFC 5987 Content-Disposition",
         lambda: check_cyrillic_content_disposition(page, base_url, product_id)),
    ]
    results = []
    for name, fn in checks:
        try:
            fn()
            results.append((name, True, None))
        except Exception as e:
            results.append((name, False, str(e)))
    return results


def main() -> int:
    chromium = _find_chromium()
    if not chromium:
        print("⚠️  SKIP: системный Chromium не найден "
              "(installSystemDependencies('chromium')). Браузерные тесты пропущены.")
        return 0
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        print("⚠️  SKIP: playwright не установлен (pip install playwright). "
              "Браузерные тесты пропущены.")
        return 0

    tmp = _setup_env_and_cwd()
    try:
        org_db, product_id = _seed_data()
        server, port = _start_server(org_db)
        base_url = f"http://127.0.0.1:{port}"
        if not _wait_for_server(f"{base_url}/login"):
            print("❌ Веб-сервер не поднялся вовремя.")
            return 1

        from web.auth import create_session_token
        token = create_session_token(SUPER_ADMIN_TG, "Smoke", org_db, "owner")

        from playwright.sync_api import sync_playwright
        console_errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, executable_path=chromium, args=["--no-sandbox"]
            )
            context = browser.new_context()
            context.add_cookies([{
                "name": "web_session", "value": token,
                "domain": "127.0.0.1", "path": "/",
            }])
            page = _new_page(context, base_url, console_errors)
            results = _run_checks(page, base_url, product_id, browser, console_errors)
            browser.close()

        # ── Отчёт ────────────────────────────────────────────────────────────
        print("\n=== Web cabinet smoke tests ===")
        failed = 0
        for name, ok, err in results:
            if ok:
                print(f"  ✓ {name}")
            else:
                failed += 1
                print(f"  ✗ {name}\n      {err}")

        # Ошибки CSP/JS в консоли — сильный индикатор той самой регрессии.
        csp_like = [e for e in console_errors
                    if "Content Security Policy" in e or "Script error" in e
                    or "Refused to" in e]
        if csp_like:
            print(f"\n  ⚠️  Подозрительные ошибки в консоли браузера ({len(csp_like)}):")
            for e in csp_like[:5]:
                print(f"      • {e}")

        if failed:
            print(f"\n❌ Провалено проверок: {failed}/{len(results)}")
            return 1
        print(f"\n✅ Все {len(results)} проверок прошли.")
        return 0
    finally:
        try:
            os.chdir(WORKSPACE_DIR)
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
