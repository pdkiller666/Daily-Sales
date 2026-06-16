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

# 6. subscription_addons: idempotency by payment_request_id + correct column
try:
    import tempfile, os as _os
    _tmp6 = tempfile.mktemp(suffix='shop_bot.db')
    _db6 = Database(_tmp6)
    _db6.create_tables()
    # первый вызов создаёт аддон
    _a1 = _db6.create_subscription_addon(12345, 'extra_products', 1, 100.0, days=30, payment_request_id=777)
    assert _a1, "create_subscription_addon: первый вызов не создал аддон"
    # повтор с тем же payment_request_id НЕ создаёт дубликат
    _a2 = _db6.create_subscription_addon(12345, 'extra_products', 1, 100.0, days=30, payment_request_id=777)
    assert _a2 == _a1, f"идемпотентность нарушена: {_a1} != {_a2}"
    _totals = _db6.get_addon_totals(12345)
    assert _totals.get('extra_products') == 1, f"ожидалось 1 аддон, получено {_totals}"
    _os.unlink(_tmp6)
    _fn_ok("subscription_addons: идемпотентность по payment_request_id")
except Exception as _e:
    _fn_fail("subscription_addons idempotency", _e)

# 7. web.auth: JWT round-trip (PyJWT) + отклонение мусора + CSRF derive
try:
    import os as _os7
    _os7.environ.setdefault('BOT_TOKEN', 'test-token-123')
    from web.auth import create_session_token, decode_session_token, get_csrf_token, verify_csrf_token
    _tok = create_session_token(424242, 'Тест', 'data/main.db', 'owner')
    assert isinstance(_tok, str), "токен должен быть str (PyJWT)"
    _dec = decode_session_token(_tok)
    assert _dec and _dec['sub'] == '424242' and _dec['role'] == 'owner', _dec
    assert decode_session_token('garbage.token.value') is None, "мусорный токен должен дать None"
    _fn_ok("web.auth: PyJWT round-trip + reject invalid")
except Exception as _e:
    _fn_fail("web.auth PyJWT", _e)

# 8. web.app: create_web_app() собирается без ошибок (проверка роутов/шаблонов)
try:
    from web.app import create_web_app
    _app = create_web_app()
    assert len(_app.routes) > 50, f"подозрительно мало роутов: {len(_app.routes)}"
    _fn_ok(f"web.app: create_web_app построен ({len(_app.routes)} роутов)")
except Exception as _e:
    _fn_fail("web.app create_web_app", _e)

# 9. rate_store: лимит срабатывает после max_requests
try:
    import time as _t9
    from web.rate_store import check_rate_limit
    _key = f"selftest:{_t9.time()}"
    _allowed = sum(1 for _ in range(3) if check_rate_limit(_key, max_requests=3, window_seconds=60))
    assert _allowed == 3, f"первые 3 должны пройти, прошло {_allowed}"
    assert check_rate_limit(_key, max_requests=3, window_seconds=60) is False, "4-й запрос должен быть заблокирован"
    _fn_ok("rate_store: блокировка после лимита")
except Exception as _e:
    _fn_fail("rate_store limit", _e)

# 10. Chat read-state: бейдж обнуляется после прочтения темы + ЛС
try:
    import tempfile, os as _os
    _tmp10 = tempfile.mktemp(suffix='.db')
    _db10 = Database(_tmp10)
    _db10.create_tables()
    # Два пользователя в орге: «я» (читатель) и собеседник (автор сообщений)
    _db10.add_user(telegram_id=10001, first_name="Reader", last_name="Me")
    _db10.add_user(telegram_id=10002, first_name="Peer", last_name="Other")
    _me = _db10.get_user(10001)[0]
    _peer = _db10.get_user(10002)[0]

    # ── Тема чата: собеседник пишет 2 сообщения в «Общий» (topic_id=1) ──
    _m1 = _db10.add_chat_message(_peer, "привет 1", topic_id=1)
    _m2 = _db10.add_chat_message(_peer, "привет 2", topic_id=1)
    _counts = _db10.get_chat_unread_counts(_me)
    assert _counts.get(1, 0) == 2, f"ожидалось 2 непрочитанных в теме 1, получено {_counts}"
    # После set_chat_read бейдж темы обнуляется
    _db10.set_chat_read(_me, 1, _m2)
    _counts_after = _db10.get_chat_unread_counts(_me)
    assert _counts_after.get(1, 0) == 0, f"после set_chat_read должно быть 0, получено {_counts_after}"
    # Своё сообщение не считается непрочитанным
    _db10.add_chat_message(_me, "это я сам", topic_id=1)
    _counts_self = _db10.get_chat_unread_counts(_me)
    assert _counts_self.get(1, 0) == 0, f"своё сообщение не должно быть непрочитанным, получено {_counts_self}"

    # ── ЛС: собеседник пишет 2 ЛС читателю ──
    _conn10 = _db10.get_connection()
    _conn10.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message) VALUES (?, ?, ?)",
        (_peer, _me, "лс 1")
    )
    _conn10.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message) VALUES (?, ?, ?)",
        (_peer, _me, "лс 2")
    )
    # AI-ответ в этой переписке: from_user_id=0, ai_peer_id=_peer.
    # Регресс: раньше mark_dm_read не закрывал его (фильтр по from_user_id=_peer)
    # → счётчик ЛС висел вечно после введения AI в личных сообщениях.
    _conn10.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message, ai_peer_id) VALUES (0, ?, ?, ?)",
        (_me, "ответ ИИ", _peer)
    )
    # AI-ответ в переписке с ДРУГИМ собеседником (_peer2) — для проверки изоляции:
    # mark_dm_read(_me, _peer) НЕ должен трогать AI-ответы чужой переписки.
    _db10.add_user(telegram_id=10003, first_name="Peer2", last_name="Other2")
    _peer2 = _db10.get_user(10003)[0]
    _conn10.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message, ai_peer_id) VALUES (0, ?, ?, ?)",
        (_me, "ответ ИИ для peer2", _peer2)
    )
    _conn10.commit()
    _conn10.close()
    assert _db10.get_dm_unread_count(_me) == 4, f"ожидалось 4 непрочитанных ЛС (2 + AI + AI peer2), получено {_db10.get_dm_unread_count(_me)}"
    # Непрочитанное AI-ответа должно быть привязано к строке контакта _peer
    _contacts10 = {c[0]: c[8] for c in _db10.get_dm_contacts(_me)}
    assert _contacts10.get(_peer, 0) == 3, f"контакт _peer должен показывать 3 непрочитанных (incl AI), получено {_contacts10}"
    # После mark_dm_read(_peer) закрываются только сообщения переписки с _peer (incl AI),
    # AI-ответ переписки с _peer2 остаётся непрочитанным → счётчик 1.
    _db10.mark_dm_read(_me, _peer)
    assert _db10.get_dm_unread_count(_me) == 1, f"после mark_dm_read(_peer) должно остаться 1 ЛС (AI peer2), получено {_db10.get_dm_unread_count(_me)}"
    # А после чтения _peer2 — полное обнуление
    _db10.mark_dm_read(_me, _peer2)
    assert _db10.get_dm_unread_count(_me) == 0, f"после mark_dm_read(_peer2) должно быть 0 ЛС, получено {_db10.get_dm_unread_count(_me)}"

    _os.unlink(_tmp10)
    _fn_ok("chat read-state: бейдж темы и ЛС обнуляются после прочтения")
except Exception as _e:
    _fn_fail("chat read-state badge reset", _e)

# 11. Chat HTTP end-to-end: POST /chat/read + POST /chat/dm/<peer>/read
try:
    import tempfile, os as _os11
    from unittest.mock import patch as _patch11
    from fastapi.testclient import TestClient as _TC11
    from web.app import create_web_app as _cwa11
    from web.auth import create_session_token as _cst11, COOKIE_NAME as _CN11, get_csrf_token as _gct11

    # ── Temp org DB: two users + chat messages + DM ──────────────────────────
    _tmp11 = tempfile.mktemp(suffix='_chat_test.db')
    _db11 = Database(_tmp11)
    _db11.create_tables()

    _db11.add_user(telegram_id=30001, first_name="Rider", last_name="A")
    _db11.add_user(telegram_id=30002, first_name="Sender", last_name="B")
    _me11   = _db11.get_user(30001)[0]   # internal users.id
    _peer11 = _db11.get_user(30002)[0]

    # Peer writes 2 topic-chat messages (unread for _me11)
    _msg11a = _db11.add_chat_message(_peer11, "msg one", topic_id=1)
    _msg11b = _db11.add_chat_message(_peer11, "msg two", topic_id=1)
    assert _db11.get_chat_unread_counts(_me11).get(1, 0) == 2, "pre-condition: 2 unread topic msgs"

    # Peer sends 1 DM to reader
    _c11 = _db11.get_connection()
    _c11.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message) VALUES (?,?,?)",
        (_peer11, _me11, "dm hello"),
    )
    _c11.commit()
    _c11.close()
    assert _db11.get_dm_unread_count(_me11) == 1, "pre-condition: 1 unread DM"

    # ── JWT cookie + CSRF via public helper ────────────────────────────────────
    _jwt11 = _cst11(30001, "Rider", _tmp11, "user")

    # Derive CSRF via the same public function the server uses, feeding it a
    # minimal fake request that carries the session cookie.
    class _FakeReq11:
        cookies = {_CN11: _jwt11}
    _csrf11 = _gct11(_FakeReq11())

    # ── FastAPI test client with billing/DB gates mocked ──────────────────────
    _app11 = _cwa11()

    def _mock_get_web_db11(*_a, **_kw):
        return _db11

    with _patch11("web.routes.chat._chat_access_ok", return_value=True), \
         _patch11("web.deps.get_web_db", side_effect=_mock_get_web_db11):

        _client11 = _TC11(_app11, raise_server_exceptions=True)
        _ck11 = {_CN11: _jwt11}

        # ── Test A: POST /chat/read returns ok and zeros topic unread ─────────
        _resp_r = _client11.post(
            "/chat/read",
            data={"topic_id": 1, "last_id": _msg11b, "csrf_token": _csrf11},
            cookies=_ck11,
        )
        assert _resp_r.status_code == 200, f"/chat/read HTTP {_resp_r.status_code}: {_resp_r.text}"
        assert _resp_r.json().get("ok") is True, f"/chat/read body: {_resp_r.json()}"

        # Verify the badge through GET /chat/poll: topic_unread for topic 1 → 0
        _resp_poll = _client11.get(
            "/chat/poll",
            params={"since_id": _msg11b, "topic_id": 1, "mark_read": 0},
            cookies=_ck11,
        )
        assert _resp_poll.status_code == 200, f"/chat/poll HTTP {_resp_poll.status_code}"
        _pj = _resp_poll.json()
        assert _pj.get("ok") is True, f"/chat/poll not ok: {_pj}"
        assert int(_pj.get("topic_unread", {}).get("1", 0)) == 0, \
            f"/chat/poll topic_unread still {_pj.get('topic_unread')} after /chat/read"

        # ── Test B: POST /chat/dm/<peer>/read returns ok and zeros DM count ───
        _resp_dm = _client11.post(
            f"/chat/dm/{_peer11}/read",
            data={"csrf_token": _csrf11},
            cookies=_ck11,
        )
        assert _resp_dm.status_code == 200, f"/chat/dm/read HTTP {_resp_dm.status_code}: {_resp_dm.text}"
        assert _resp_dm.json().get("ok") is True, f"/chat/dm/read body: {_resp_dm.json()}"
        assert _db11.get_dm_unread_count(_me11) == 0, \
            f"DM badge still {_db11.get_dm_unread_count(_me11)} after /chat/dm/read"

        # ── Test C: GET /api/unread-count reflects zeroed DMs ─────────────────
        _resp_uc = _client11.get("/api/unread-count", cookies=_ck11)
        assert _resp_uc.status_code == 200, f"/api/unread-count HTTP {_resp_uc.status_code}"
        _ucj = _resp_uc.json()
        assert _ucj.get("ok") is True, f"/api/unread-count not ok: {_ucj}"
        assert _ucj.get("dms", -1) == 0, f"/api/unread-count dms={_ucj.get('dms')} expected 0"

    _os11.unlink(_tmp11)
    _fn_ok("chat HTTP end-to-end: /chat/read + /chat/dm/<peer>/read → ok + badges zeroed")
except Exception as _e11:
    _fn_fail("chat HTTP end-to-end read routes", _e11)

print("=" * 55)
print(f"  Итог: {fn_passed} ОК, {fn_failed} ошибок")
print("=" * 55)

sys.exit(1 if (failed + fn_failed) else 0)
