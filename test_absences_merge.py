"""
test_absences_merge.py — Regression tests for /absences/merge cross-user guard.

Verifies:
1. POSTing two absence IDs that belong to *different* user_ids returns a 302
   redirect with msg=no_access (cross-employee merge must be blocked).
2. POSTing two absence IDs that belong to the *same* user_id (same type,
   overlapping dates) returns a 302 redirect with msg=merged (success path).

Isolation: runs in its own temporary directory; does not touch data/.
"""

import hashlib
import hmac
import os
import shutil
import sqlite3
import sys
import tempfile

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))

PASS = "✅"
FAIL = "❌"
_results: list[tuple[str, str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    _results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


def _compute_secret(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _make_csrf(session_jwt: str, secret: str, nonce: str = "testnonce1") -> str:
    """Replicate web/auth.py get_csrf_token() logic (nonce variant)."""
    sig = hmac.new(
        secret.encode(),
        f"{session_jwt}.{nonce}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{nonce}.{sig}"


def main() -> None:
    # ── Isolated temp environment ─────────────────────────────────────────────
    tmp = tempfile.mkdtemp(prefix="ds_absence_merge_test_")
    os.makedirs(os.path.join(tmp, "data", "tenants"), exist_ok=True)

    _WEB_SECRET = "absence-merge-test-secret"
    os.environ["BOT_TOKEN"] = "123456:TEST_ABSENCE_MERGE_TOKEN"
    os.environ["ADMIN_CHAT_ID"] = "921098636"
    os.environ["WEB_SECRET_KEY"] = _WEB_SECRET

    if WORKSPACE_DIR not in sys.path:
        sys.path.insert(0, WORKSPACE_DIR)

    orig_dir = os.getcwd()
    os.chdir(tmp)

    try:
        _run_tests(tmp, _compute_secret(_WEB_SECRET))
    finally:
        os.chdir(orig_dir)
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for s, _, _ in _results if s == PASS)
    failed = sum(1 for s, _, _ in _results if s == FAIL)
    total = len(_results)

    print(f"\n  {'='*40}")
    print(f"  Всего тестов:  {total}")
    print(f"  ✅ Прошло:     {passed}")
    print(f"  ❌ Провалено:  {failed}")
    print(f"  {'='*40}")

    if failed == 0:
        print(f"\n  🎉 Все {total} тестов прошли успешно!")
    else:
        print("\n  ⚠️  Есть провалы — требуется проверка.")
        for status, label, detail in _results:
            if status == FAIL:
                print(f"    {FAIL} {label}" + (f" | {detail}" if detail else ""))

    sys.exit(0 if failed == 0 else 1)


def _run_tests(tmp: str, secret: str) -> None:
    from tenant_manager import tenant_manager
    from database import Database

    # ── Central DB ───────────────────────────────────────────────────────────
    shop_db = Database("data/shop_bot.db")
    shop_db.create_tables()

    # ── Org DB ───────────────────────────────────────────────────────────────
    ok, org_id = tenant_manager.create_organization("MergeTestOrg", 921098636)
    assert ok, f"create_organization failed: {org_id}"

    conn_main = sqlite3.connect("data/main.db")
    row = conn_main.execute(
        "SELECT db_path FROM organizations WHERE id = ?", (org_id,)
    ).fetchone()
    conn_main.close()
    org_db_path = row[0]

    db = Database(org_db_path)
    db.create_tables()

    # ── Two distinct users ────────────────────────────────────────────────────
    db.add_user(telegram_id=2001, first_name="Alice", last_name="A")
    db.add_user(telegram_id=2002, first_name="Bob", last_name="B")
    uid_alice = db.get_user_id(2001)
    uid_bob = db.get_user_id(2002)

    # ── Session JWT for owner (super-admin tg is accepted everywhere) ─────────
    from web.auth import create_session_token, COOKIE_NAME

    jwt_val = create_session_token(
        telegram_id=921098636,
        first_name="Admin",
        org_db=org_db_path,
        role="owner",
    )
    csrf = _make_csrf(jwt_val, secret)

    # ── FastAPI TestClient ────────────────────────────────────────────────────
    from starlette.testclient import TestClient
    from web.app import create_web_app

    app = create_web_app()
    client = TestClient(app, follow_redirects=False)
    _session_cookies = {COOKIE_NAME: jwt_val}

    # =========================================================================
    # Test 1: Cross-user merge → must be blocked (msg=no_access)
    # =========================================================================
    aid_alice = db.add_absence(
        uid_alice, "vacation", "2026-07-01", "2026-07-05",
        status="approved",
    )
    aid_bob = db.add_absence(
        uid_bob, "vacation", "2026-07-03", "2026-07-08",
        status="approved",
    )

    resp = client.post(
        "/absences/merge",
        data={
            "csrf_token": csrf,
            "keep_id": str(aid_alice),
            "drop_id": str(aid_bob),
            "year": "2026",
            "month": "7",
        },
        cookies=_session_cookies,
    )
    check(
        "merge cross-user: response is 302 redirect",
        resp.status_code == 302,
        f"status={resp.status_code}",
    )
    location = resp.headers.get("location", "")
    check(
        "merge cross-user: redirect contains msg=no_access",
        "msg=no_access" in location,
        f"location={location!r}",
    )

    # ── Confirm alice's absence record is still intact (not merged/cancelled) ─
    rec_alice = db.get_absence_by_id(aid_alice)
    check(
        "merge cross-user: alice record untouched (status still approved)",
        rec_alice is not None and rec_alice[5] == "approved",
        f"record={rec_alice}",
    )
    rec_bob = db.get_absence_by_id(aid_bob)
    check(
        "merge cross-user: bob record untouched (status still approved)",
        rec_bob is not None and rec_bob[5] == "approved",
        f"record={rec_bob}",
    )

    # =========================================================================
    # Test 2: Same user, overlapping dates → must succeed (msg=merged)
    # =========================================================================
    aid_keep = db.add_absence(
        uid_alice, "sick_leave", "2026-08-01", "2026-08-10",
        status="approved",
    )
    aid_drop = db.add_absence(
        uid_alice, "sick_leave", "2026-08-08", "2026-08-15",
        status="approved",
    )

    resp2 = client.post(
        "/absences/merge",
        data={
            "csrf_token": csrf,
            "keep_id": str(aid_keep),
            "drop_id": str(aid_drop),
            "year": "2026",
            "month": "8",
        },
        cookies=_session_cookies,
    )
    check(
        "merge same-user: response is 302 redirect",
        resp2.status_code == 302,
        f"status={resp2.status_code}",
    )
    location2 = resp2.headers.get("location", "")
    check(
        "merge same-user: redirect contains msg=merged",
        "msg=merged" in location2,
        f"location={location2!r}",
    )

    # ── Confirm the keep record now spans the full merged range ───────────────
    merged_rec = db.get_absence_by_id(aid_keep)
    check(
        "merge same-user: keep record start_date extended to 2026-08-01",
        merged_rec is not None and merged_rec[3] == "2026-08-01",
        f"start_date={merged_rec[3] if merged_rec else None}",
    )
    check(
        "merge same-user: keep record end_date extended to 2026-08-15",
        merged_rec is not None and merged_rec[4] == "2026-08-15",
        f"end_date={merged_rec[4] if merged_rec else None}",
    )
    dropped_rec = db.get_absence_by_id(aid_drop)
    check(
        "merge same-user: drop record cancelled after merge",
        dropped_rec is not None and dropped_rec[5] == "cancelled",
        f"status={dropped_rec[5] if dropped_rec else None}",
    )


if __name__ == "__main__":
    main()
