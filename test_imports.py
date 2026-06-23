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

# 6b. module/bundle grant (annual): идемпотентность по payment_request_id + длительность 365
try:
    import tempfile as _tf6b, os as _os6b
    _tmp6b = _tf6b.mktemp(suffix='shop_bot.db')
    _db6b = Database(_tmp6b)
    _db6b.create_tables()
    _tg6b = 55501
    # пользователь нужен, т.к. confirm_payment_request резолвит tg_id по user_id
    _uid6b = None
    _c6b = _db6b.get_connection()
    _c6b.execute(
        "INSERT INTO users (telegram_id, first_name, last_name) VALUES (?,?,?)",
        (_tg6b, 'T', 'G')
    )
    _c6b.commit(); _c6b.close()
    _c6b = _db6b.get_connection()
    _row6b = _c6b.execute("SELECT id FROM users WHERE telegram_id=?", (_tg6b,)).fetchone()
    _uid6b = _row6b[0]
    # создаём заявку на годовой модуль и подтверждаем дважды
    _cur6b = _c6b.cursor()
    _cur6b.execute(
        "INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id) "
        "VALUES (?,?,?,?)", (_uid6b, 'module_annual_analytics', 2990.0, 'test')
    )
    _req6b = _cur6b.lastrowid
    _c6b.commit(); _c6b.close()
    _ok6b_1 = _db6b.confirm_payment_request(_req6b, admin_id=0)
    assert _ok6b_1, "confirm_payment_request (module annual) первый вызов вернул False"
    # повторное подтверждение не должно создавать второй грант
    _ok6b_2 = _db6b.confirm_payment_request(_req6b, admin_id=0)
    _cc6b = _db6b.get_connection()
    _cnt6b = _cc6b.execute(
        "SELECT COUNT(*) FROM billing_module_subs WHERE payment_request_id=?", (_req6b,)
    ).fetchone()[0]
    _dur6b = _cc6b.execute(
        "SELECT start_date, end_date FROM billing_module_subs WHERE payment_request_id=? LIMIT 1",
        (_req6b,)
    ).fetchone()
    _cc6b.close()
    assert _cnt6b == 1, f"идемпотентность модуля нарушена: создано {_cnt6b} грантов"
    from datetime import datetime as _dt6b
    _sd = _dt6b.strptime(_dur6b[0], '%Y-%m-%d %H:%M:%S')
    _ed = _dt6b.strptime(_dur6b[1], '%Y-%m-%d %H:%M:%S')
    _days6b = (_ed - _sd).days
    assert 360 <= _days6b <= 366, f"ожидалось ~365 дней, получено {_days6b}"
    _os6b.unlink(_tmp6b)
    _fn_ok("module/bundle annual grant: идемпотентность + 365 дней")
except Exception as _e:
    _fn_fail("module annual grant idempotency", _e)

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

# 10b. Выделенный AI-тред: отдельный собеседник AI изолирован от реальных ЛС
#      и от старых in-dialog AI-ответов (ai_peer_id>=1).
try:
    import tempfile, os as _os10b
    _tmp10b = tempfile.mktemp(suffix='.db')
    _db10b = Database(_tmp10b)
    _db10b.create_tables()
    _db10b.add_user(telegram_id=20001, first_name="User", last_name="A")
    _db10b.add_user(telegram_id=20002, first_name="Peer", last_name="B")
    _u = _db10b.get_user(20001)[0]
    _p = _db10b.get_user(20002)[0]

    # AI-тред: запрос пользователя (from=u, to=0) + ответ AI (from=0, to=u, ai_peer_id=0)
    _db10b.add_dm(_u, 0, "вопрос к AI")
    _conn10b = _db10b.get_connection()
    _conn10b.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message, ai_peer_id) VALUES (0, ?, ?, 0)",
        (_u, "ответ AI")
    )
    # Старый in-dialog AI (ai_peer_id=_p>=1) — НЕ должен попадать в AI-тред
    _conn10b.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message, ai_peer_id) VALUES (0, ?, ?, ?)",
        (_u, "старый AI в диалоге", _p)
    )
    # Реальное ЛС от человека — тоже не в AI-треде
    _conn10b.execute(
        "INSERT INTO direct_messages (from_user_id, to_user_id, message) VALUES (?, ?, ?)",
        (_p, _u, "реальное ЛС")
    )
    _conn10b.commit(); _conn10b.close()

    # get_ai_dm_conversation видит ровно 2 сообщения AI-треда (вопрос + ответ), ASC
    _conv = _db10b.get_ai_dm_conversation(_u)
    assert len(_conv) == 2, f"AI-тред должен содержать 2 сообщения, получено {len(_conv)}"
    _texts = [r[3] for r in _conv]
    assert _texts == ["вопрос к AI", "ответ AI"], f"состав/порядок AI-треда неверный: {_texts}"

    # summary: последний — ответ AI, один непрочитанный
    _last_msg, _last_from, _last_at, _unread = _db10b.get_ai_dm_summary(_u)
    assert _last_from == 0 and _last_msg == "ответ AI", f"summary last неверный: {_last_msg}/{_last_from}"
    assert _unread == 1, f"summary unread должно быть 1, получено {_unread}"

    # mark_ai_dm_read обнуляет только AI-тред
    _db10b.mark_ai_dm_read(_u)
    assert _db10b.get_ai_dm_summary(_u)[3] == 0, "после mark_ai_dm_read unread AI-треда должно быть 0"
    # Старый in-dialog AI и реальное ЛС остаются непрочитанными (изоляция)
    assert _db10b.get_dm_unread_count(_u) == 2, \
        f"вне AI-треда должно остаться 2 непрочитанных, получено {_db10b.get_dm_unread_count(_u)}"
    # AI-тред не плодит фантом-контакт (peer 0): контакты содержат _p, но не 0
    _ids = {c[0] for c in _db10b.get_dm_contacts(_u)}
    assert _p in _ids and 0 not in _ids, f"контакты: ожидался _p без peer 0, получено {_ids}"

    _os10b.unlink(_tmp10b)
    _fn_ok("AI-тред: отдельный собеседник изолирован от ЛС и старого in-dialog AI")
except Exception as _e:
    _fn_fail("dedicated AI thread", _e)

# 10c. Выделенная AI-тема чата: ensure/get + защита от rename/archive + флаг в списке
try:
    import tempfile, os as _os10c
    _tmp10c = tempfile.mktemp(suffix='_aitopic.db')
    _db10c = Database(_tmp10c)
    _db10c.create_tables()

    # До создания — AI-темы нет
    assert _db10c.get_ai_topic_id() is None, "AI-тема не должна существовать до ensure_ai_topic"

    # ensure создаёт ровно одну AI-тему, идемпотентен
    _aid = _db10c.ensure_ai_topic()
    assert _aid and _aid != 1, f"ensure_ai_topic вернул некорректный id: {_aid}"
    assert _db10c.ensure_ai_topic() == _aid, "ensure_ai_topic не идемпотентен"
    assert _db10c.get_ai_topic_id() == _aid, "get_ai_topic_id не совпал с ensure"

    # Флаг is_ai присутствует в get_chat_topics (7-я колонка) только у AI-темы
    _topics = {r[0]: (r[6] if len(r) > 6 else 0) for r in _db10c.get_chat_topics()}
    assert _topics.get(_aid) == 1, "AI-тема должна иметь is_ai=1"
    assert _topics.get(1, 0) == 0, "Тема «Общий» не должна быть AI"

    # AI-тему нельзя переименовать/архивировать
    assert _db10c.rename_chat_topic(_aid, "взлом", user_id=1, is_admin=True) is False, \
        "AI-тему нельзя переименовывать"
    assert _db10c.archive_chat_topic(_aid) is False, "AI-тему нельзя архивировать"
    assert _db10c.get_ai_topic_id() == _aid, "AI-тема пропала после попытки архивации"

    _os10c.unlink(_tmp10c)
    _fn_ok("AI-тема чата: ensure/get идемпотентны, защищены от rename/archive, is_ai в списке")
except Exception as _e:
    _fn_fail("AI chat topic", _e)

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

# 11b. DM API guards: невалидные peer (-2/0) и негейтированный AI (-1) → 4xx
try:
    import tempfile, os as _os11b
    from unittest.mock import patch as _patch11b
    from fastapi.testclient import TestClient as _TC11b
    from web.app import create_web_app as _cwa11b
    from web.auth import create_session_token as _cst11b, COOKIE_NAME as _CN11b, get_csrf_token as _gct11b

    _tmp11b = tempfile.mktemp(suffix='_dm_neg.db')
    _db11bn = Database(_tmp11b)
    _db11bn.create_tables()
    _db11bn.add_user(telegram_id=31001, first_name="NegA", last_name="A")

    _jwt11b = _cst11b(31001, "NegA", _tmp11b, "user")
    class _FakeReq11b:
        cookies = {_CN11b: _jwt11b}
    _csrf11b = _gct11b(_FakeReq11b())

    _app11b = _cwa11b()
    def _mock_db11b(*_a, **_kw):
        return _db11bn

    # AI-расширение НЕ оплачено → доступ к AI-треду должен отбиваться 403
    with _patch11b("web.routes.chat._chat_access_ok", return_value=True), \
         _patch11b("web.routes.chat._ai_ext_ok", return_value=False), \
         _patch11b("web.deps.get_web_db", side_effect=_mock_db11b):
        _cl11b = _TC11b(_app11b, raise_server_exceptions=True)
        _ck = {_CN11b: _jwt11b}
        # conversation к AI без оплаченного расширения → 403
        _r1 = _cl11b.get("/api/dm/conversation/-1", cookies=_ck)
        assert _r1.status_code == 403, f"conversation/-1 без extension: ожидался 403, получен {_r1.status_code}"
        # conversation к техническому peer 0 → 400
        _r2 = _cl11b.get("/api/dm/conversation/0", cookies=_ck)
        assert _r2.status_code == 400, f"conversation/0: ожидался 400, получен {_r2.status_code}"
        # отправка на невалидный отрицательный peer → 400
        _r3 = _cl11b.post("/chat/dm/send",
                          data={"to_user_id": -2, "message": "hi", "csrf_token": _csrf11b},
                          cookies=_ck)
        assert _r3.status_code == 400, f"dm/send to=-2: ожидался 400, получен {_r3.status_code}: {_r3.text}"
        # mark-read AI без extension → 403
        _r4 = _cl11b.post("/chat/dm/-1/read", data={"csrf_token": _csrf11b}, cookies=_ck)
        assert _r4.status_code == 403, f"dm/-1/read без extension: ожидался 403, получен {_r4.status_code}"

    _os11b.unlink(_tmp11b)
    _fn_ok("DM API guards: невалидные peer (-2/0) и негейтированный AI (-1) → 4xx")
except Exception as _e11b:
    _fn_fail("DM API negative peer guards", _e11b)

# 12. Chat HTTP end-to-end: POST /chat/send + POST /chat/dm/send
try:
    import tempfile, os as _os12
    from unittest.mock import patch as _patch12, AsyncMock as _AsyncMock12
    from fastapi.testclient import TestClient as _TC12
    from web.app import create_web_app as _cwa12
    from web.auth import create_session_token as _cst12, COOKIE_NAME as _CN12, get_csrf_token as _gct12

    # ── Temp org DB: two users ─────────────────────────────────────────────────
    _tmp12 = tempfile.mktemp(suffix='_chat_send_test.db')
    _db12 = Database(_tmp12)
    _db12.create_tables()

    _db12.add_user(telegram_id=40001, first_name="Writer", last_name="A")
    _db12.add_user(telegram_id=40002, first_name="Peer",   last_name="B")
    _me12   = _db12.get_user(40001)[0]   # internal users.id for sender
    _peer12 = _db12.get_user(40002)[0]   # internal users.id for DM recipient

    # ── JWT cookie + CSRF ──────────────────────────────────────────────────────
    _jwt12 = _cst12(40001, "Writer", _tmp12, "user")

    class _FakeReq12:
        cookies = {_CN12: _jwt12}
    _csrf12 = _gct12(_FakeReq12())

    # ── FastAPI test client with billing/DB/WS gates mocked ───────────────────
    _app12 = _cwa12()

    def _mock_get_web_db12(*_a, **_kw):
        return _db12

    _dm_send_mock12 = _AsyncMock12(return_value=None)

    with _patch12("web.routes.chat._chat_access_ok", return_value=True), \
         _patch12("web.routes.chat._send_rate_ok",   return_value=True), \
         _patch12("web.routes.chat._dm_send_ok",     return_value=True), \
         _patch12("web.ws_manager.dm_manager.send_to_user", _dm_send_mock12), \
         _patch12("web.deps.get_web_db", side_effect=_mock_get_web_db12):

        _client12 = _TC12(_app12, raise_server_exceptions=True)
        _ck12 = {_CN12: _jwt12}

        # ── Test A: POST /chat/send stores topic message and returns ok ────────
        _resp_send = _client12.post(
            "/chat/send",
            data={"message": "hello topic", "topic_id": 1, "csrf_token": _csrf12},
            cookies=_ck12,
        )
        assert _resp_send.status_code == 200, \
            f"/chat/send HTTP {_resp_send.status_code}: {_resp_send.text}"
        _sj = _resp_send.json()
        assert _sj.get("ok") is True, f"/chat/send body not ok: {_sj}"
        _new_id12 = _sj.get("latest_id")
        assert _new_id12, f"/chat/send missing latest_id: {_sj}"

        # Verify the message is persisted in the DB
        _stored_msgs12 = _db12.get_chat_messages_since(0, topic_id=1)
        _stored_texts12 = [r[2] for r in _stored_msgs12]   # column 2 = message text
        assert "hello topic" in _stored_texts12, \
            f"Sent message not found in DB; stored: {_stored_texts12}"

        # ── Test B: POST /chat/dm/send stores DM and returns ok ───────────────
        _resp_dm12 = _client12.post(
            "/chat/dm/send",
            data={"message": "hello dm", "to_user_id": _peer12, "csrf_token": _csrf12},
            cookies=_ck12,
        )
        assert _resp_dm12.status_code == 200, \
            f"/chat/dm/send HTTP {_resp_dm12.status_code}: {_resp_dm12.text}"
        _dmj = _resp_dm12.json()
        assert _dmj.get("ok") is True, f"/chat/dm/send body not ok: {_dmj}"

        # Verify the DM is persisted — check conversation in both directions
        _conv12 = _db12.get_dm_conversation(_me12, _peer12)
        _conv_texts12 = [r[3] for r in _conv12]   # column 3 = message text
        assert "hello dm" in _conv_texts12, \
            f"DM not found in DB; conversation rows: {_conv_texts12}"

        # Verify WS send_to_user was called once (to notify the DM recipient)
        assert _dm_send_mock12.called, "dm_manager.send_to_user not called for DM send"

    _os12.unlink(_tmp12)
    _fn_ok("chat HTTP end-to-end: /chat/send + /chat/dm/send → ok + persisted in DB")
except Exception as _e12:
    _fn_fail("chat HTTP end-to-end write routes", _e12)

# N. GS motivation sync: network-column alias (DNS → Днс) + unmatched-chain report
try:
    import asyncio as _aio_m, tempfile as _tf_m, os as _os_m
    from database import Database as _Db_m
    from integration.manager import integration_manager as _im

    _tmp_m = _tf_m.mktemp(suffix='.db')
    _dbm = _Db_m(_tmp_m)
    _dbm.create_tables()
    # Seller profile network is Cyrillic 'Днс'; sheet column header is Latin 'DNS'
    _dbm.add_user(telegram_id=4242, first_name="Илья", last_name="Т", trade_network="Днс")
    _pid_m = _dbm.add_product("Nova 15 Max", "Тест", 32999)
    assert _pid_m, "add_product failed"

    # Stub the connection lookup, token refresh and the sheet read so we exercise
    # the real sync_motivation_from_sheet alias/scope logic without Google.
    _dbm.get_integration_connection = lambda cid: (cid, "google_sheets", "name", "{}")

    async def _fake_token(db, conn_id, conn_config):
        return {}
    _im._ensure_valid_token = _fake_token

    _prov = _im.providers.get("google_sheets")
    _orig_read = _prov.read_motivation_table
    async def _fake_read(conn_config, sheet, **kw):
        return [{"model": "Nova 15 Max", "rrp": 0.0,
                 "bonuses": {"DNS": 1155.0, "MVM": 825.0}}]
    _prov.read_motivation_table = _fake_read
    try:
        _res_m = _aio_m.run(_im.sync_motivation_from_sheet(
            _dbm, 1, "Лист1", header_row=1, model_col=1,
            bonus_col_map={"2": "DNS", "3": "MVM"}, rrp_col=None,
            aliases={"DNS": "Днс"},  # sheet header → profile network
        ))
    finally:
        _prov.read_motivation_table = _orig_read

    # The DNS column must be written under the resolvable Cyrillic scope 'Днс'
    _mv = _dbm.resolve_motivation(_pid_m, {"trade_network": "Днс"})
    assert _mv is not None, "resolve_motivation returned None for 'Днс'"
    assert float(_mv["motivation_value"]) == 1155.0, \
        f"expected 1155 for Днс, got {_mv['motivation_value']}"
    # MVM has no matching profile network → must be reported as unmatched
    assert "MVM" in _res_m.get("unmatched_chains", []), \
        f"MVM should be unmatched; got {_res_m.get('unmatched_chains')}"
    _os_m.unlink(_tmp_m)
    _fn_ok("GS мотивация: алиас сети DNS→Днс + отчёт о несовпавших сетях")
except Exception as _e:
    _fn_fail("GS motivation network alias", _e)

print("=" * 55)
print(f"  Итог: {fn_passed} ОК, {fn_failed} ошибок")
print("=" * 55)

sys.exit(1 if (failed + fn_failed) else 0)
