"""
Super-Admin Hub — web equivalent of system_admin_panel in the bot.
All routes require user.role == 'super_admin'.
"""
import contextlib
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from backup_manager import BackupManager
from database import Database
from tenant_manager import tenant_manager
from web.auth import get_csrf_token, get_session_user, verify_csrf_token

router = APIRouter(prefix="/admin")

SHOP_BOT_DB = "data/shop_bot.db"


def _raw_conn(path: str = SHOP_BOT_DB) -> sqlite3.Connection:
    """Open a raw SQLite connection with WAL mode and busy timeout.

    Registers a Unicode-aware `lower_u()` SQL function because SQLite's built-in
    LOWER()/LIKE only fold ASCII case — Cyrillic (e.g. «Тарасов») would never
    match a lowercased query. Use lower_u() instead of LOWER() for text search.
    """
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.create_function(
        "lower_u", 1, lambda s: s.lower() if isinstance(s, str) else s
    )
    return conn


def _guard(user) -> bool:
    """Return True if access should be denied."""
    if user is None:
        return True
    # user is a dict (decoded JWT payload), not an object
    role = user.get("role") if isinstance(user, dict) else getattr(user, "role", None)
    return role != "super_admin"


def _db() -> Database:
    return Database(SHOP_BOT_DB)


def _ctx(request: Request, user, extra: dict) -> dict:
    base = {"request": request, "user": user, "_p": str(request.url.path)}
    base.update(extra)
    return base


def _flash(request: Request) -> str:
    return request.query_params.get("msg", "")


# ─────────────────────────────────────────────────────────────────────────────
#  Hub
# ─────────────────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/")
async def admin_hub(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    stats = db.get_subscriptions_statistics()
    orgs = tenant_manager.get_all_organizations()
    active_subs = _count_active_subs()
    pending = _count_pending()
    bm = BackupManager()
    try:
        backups = bm.get_backup_list()
        last_backup = backups[0]["created"].strftime("%d.%m.%Y %H:%M") if backups else "—"
    except Exception:
        last_backup = "—"
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/hub.html",
        _ctx(request, user, {
            "stats": stats,
            "orgs_count": len(orgs),
            "active_subs_count": active_subs,
            "pending_count": pending,
            "last_backup": last_backup,
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Organizations
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/orgs")
async def admin_orgs(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    orgs_raw = tenant_manager.get_all_organizations()
    orgs = []
    for row in orgs_raw:
        org_id, name, db_path, owner_id, invite_code, plan, is_active, created = row
        users = tenant_manager.get_org_users(org_id)
        orgs.append({
            "id": org_id,
            "name": name,
            "db_path": db_path or "",
            "plan": plan or "Бесплатный",
            "is_active": bool(is_active),
            "created": (created or "")[:10],
            "users_count": len(users),
            "invite_code": invite_code or "—",
        })
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/organizations.html",
        _ctx(request, user, {
            "orgs": orgs,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/orgs/{org_id}/delete")
def admin_delete_org(
    request: Request,
    org_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    try:
        tenant_manager.delete_organization(org_id)
        msg = "deleted"
    except Exception:
        msg = "error"
    return RedirectResponse(f"/admin/orgs?msg={msg}", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Subscriptions
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/subs")
async def admin_subs(request: Request, q: str = ""):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)

    active_subs = _get_active_subs(q)
    users_for_grant = _get_all_shop_bot_users()

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/subscriptions.html",
        _ctx(request, user, {
            "active_subs": active_subs,
            "users_for_grant": users_for_grant,
            "q": q,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/subs/{user_id}/cancel")
async def admin_cancel_sub(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
):
    """Reset a user's legacy subscription back to «Бесплатный» by removing
    their active subscription rows in shop_bot.db."""
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)

    conn = None
    try:
        conn = _raw_conn()
        conn.execute(
            "DELETE FROM subscriptions "
            "WHERE user_id = ? AND datetime(end_date) > datetime('now')",
            (user_id,),
        )
        conn.commit()
        msg = "cancelled"
    except Exception:
        msg = "error"
    finally:
        if conn:
            conn.close()
    return RedirectResponse(f"/admin/subs?msg={msg}", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  APK Download Statistics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/apk")
def admin_apk(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)

    conn = None
    apk_version = "—"
    apk_date = "—"
    apk_url = ""
    apk_total = 0
    apk_by_day = []
    release_history = []

    try:
        conn = _raw_conn()
        for row in conn.execute(
            "SELECT key, value FROM payment_settings WHERE key IN ('apk_latest_version','apk_release_date','apk_release_url')"
        ).fetchall():
            k, v = row
            if k == "apk_latest_version":
                apk_version = v or "—"
            elif k == "apk_release_date":
                apk_date = (v or "")[:10] or "—"
            elif k == "apk_release_url":
                apk_url = v or ""

        try:
            apk_total = conn.execute("SELECT COUNT(*) FROM download_events").fetchone()[0]
        except Exception:
            apk_total = 0

        try:
            rows = conn.execute("""
                SELECT strftime('%Y-%m-%d', timestamp) AS day, COUNT(*)
                FROM download_events
                GROUP BY day ORDER BY day DESC LIMIT 30
            """).fetchall()
            apk_by_day = [{"day": r[0], "count": r[1]} for r in rows]
        except Exception:
            apk_by_day = []

        apk_by_owner = []
        try:
            owner_rows = conn.execute("""
                SELECT owner_id, telegram_id, COUNT(*) AS cnt,
                       MAX(timestamp) AS last_ts
                FROM download_events
                WHERE owner_id != '' AND owner_id IS NOT NULL
                GROUP BY owner_id
                ORDER BY cnt DESC LIMIT 50
            """).fetchall()
            for r in owner_rows:
                org_label = str(r[0])
                try:
                    import os as _os
                    org_label = _os.path.splitext(_os.path.basename(r[0]))[0]
                except Exception:
                    pass
                apk_by_owner.append({
                    "org": org_label,
                    "telegram_id": r[1] or "—",
                    "count": r[2],
                    "last": (r[3] or "")[:16].replace("T", " "),
                })
        except Exception:
            apk_by_owner = []

        try:
            hist_rows = conn.execute("""
                SELECT version, release_url, release_date, recorded_at
                FROM apk_release_history
                ORDER BY id DESC LIMIT 50
            """).fetchall()
            release_history = [
                {
                    "version": r[0],
                    "url": r[1] or "",
                    "release_date": (r[2] or "")[:10] or "—",
                    "recorded_at": (r[3] or "")[:16].replace("T", " "),
                }
                for r in hist_rows
            ]
        except Exception:
            release_history = []

    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/apk.html",
        _ctx(request, user, {
            "apk_version": apk_version,
            "apk_date": apk_date,
            "apk_url": apk_url,
            "apk_total": apk_total,
            "apk_by_day": apk_by_day,
            "apk_by_owner": apk_by_owner,
            "release_history": release_history,
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Payment Statistics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def admin_stats(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    detailed = db.get_detailed_payment_statistics()

    conn = None
    try:
        conn = _raw_conn()
        recent = [
            {
                "id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip() or f"ID {r[0]}",
                "plan": r[3] or "—",
                "amount": r[4] or 0,
                "date": (r[5] or "")[:10],
            }
            for r in conn.execute("""
                SELECT pr.id, u.first_name, u.last_name, pr.plan_type, pr.amount, pr.created_at
                FROM payment_requests pr
                JOIN users u ON pr.user_id = u.id
                WHERE pr.status = 'approved'
                ORDER BY pr.created_at DESC LIMIT 30
            """).fetchall()
        ]
        chart_raw = conn.execute("""
            SELECT strftime('%Y-%m', created_at) as mo, SUM(amount)
            FROM payment_requests WHERE status = 'approved'
            GROUP BY mo ORDER BY mo ASC
        """).fetchall()

        # ── APK download stats ────────────────────────────────────────────────
        try:
            apk_total = conn.execute(
                "SELECT COUNT(*) FROM download_events"
            ).fetchone()[0]
            apk_by_day_raw = conn.execute("""
                SELECT strftime('%Y-%m-%d', timestamp) AS day, COUNT(*)
                FROM download_events
                GROUP BY day ORDER BY day DESC LIMIT 30
            """).fetchall()
            apk_ref_raw = conn.execute("""
                SELECT referrer, COUNT(*) AS cnt
                FROM download_events
                GROUP BY referrer
                ORDER BY cnt DESC
            """).fetchall()
            apk_device_raw = conn.execute("""
                SELECT
                    strftime('%Y-%m-%d', timestamp) AS day,
                    CASE
                        WHEN lower(user_agent) LIKE '%mobile%'
                          OR lower(user_agent) LIKE '%android%'
                          OR lower(user_agent) LIKE '%iphone%'
                          OR lower(user_agent) LIKE '%ipad%' THEN 'Mobile'
                        ELSE 'Desktop'
                    END AS device,
                    COUNT(*) AS cnt
                FROM download_events
                GROUP BY day, device
                ORDER BY day DESC
                LIMIT 60
            """).fetchall()
        except Exception:
            apk_total = 0
            apk_by_day_raw = []
            apk_ref_raw = []
            apk_device_raw = []
    except Exception:
        recent = []
        chart_raw = []
        apk_total = 0
        apk_by_day_raw = []
        apk_ref_raw = []
        apk_device_raw = []
    finally:
        if conn:
            conn.close()

    chart = [{"month": r[0], "revenue": float(r[1] or 0)} for r in chart_raw]

    # Build device-type map: {day -> {"Mobile": n, "Desktop": n}}
    _device_map: dict = defaultdict(lambda: {"Mobile": 0, "Desktop": 0})
    for day, device, cnt in apk_device_raw:
        _device_map[day][device] = cnt

    apk_by_day = [
        {
            "day": r[0],
            "count": r[1],
            "mobile": _device_map.get(r[0], {}).get("Mobile", 0),
            "desktop": _device_map.get(r[0], {}).get("Desktop", 0),
        }
        for r in apk_by_day_raw
    ]

    # Classify referrers into named buckets
    _source_counts: dict = defaultdict(int)
    for ref, cnt in apk_ref_raw:
        ref = ref or ""
        if not ref.strip():
            label = "Прямой"
        elif "t.me" in ref or "telegram" in ref.lower():
            label = "Бот"
        else:
            try:
                domain = urlparse(ref).netloc or ref
            except Exception:
                domain = ref
            label = domain[:40] if domain else ref[:40]
        _source_counts[label] += cnt

    apk_sources = [
        {"source": s, "count": c}
        for s, c in sorted(_source_counts.items(), key=lambda x: -x[1])[:5]
    ]

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/stats.html",
        _ctx(request, user, {
            "stats": detailed,
            "recent_payments": recent,
            "chart": chart,
            "apk_total": apk_total,
            "apk_by_day": apk_by_day,
            "apk_sources": apk_sources,
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Payment Settings
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/settings")
async def admin_pay_settings(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    settings = db.get_payment_settings()
    web_url = db.get_web_interface_url() or ""
    provider = db.get_payment_provider() or "sbp"

    # Trial settings stored in payment_settings table.
    # Под модульным биллингом пробный период = полный доступ ко всем модулям
    # (см. billing_utils._is_trial), поэтому отдельного «тарифа триала» больше нет.
    trial_days = settings.get("trial_days", "14")
    payment_instruction = settings.get("payment_instruction", "")

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/payment_settings.html",
        _ctx(request, user, {
            "settings": settings,
            "web_url": web_url,
            "provider": provider,
            "trial_days": trial_days,
            "payment_instruction": payment_instruction,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/settings")
async def admin_pay_settings_save(
    request: Request,
    csrf_token: str = Form(""),
    card_number: str = Form(""),
    recipient_name: str = Form(""),
    bank_name: str = Form(""),
    payment_instruction: str = Form(""),
    web_url: str = Form(""),
    provider: str = Form("sbp"),
    trial_days: str = Form("14"),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    if card_number.strip():
        db.update_payment_setting("card_number", card_number.strip())
    if recipient_name.strip():
        db.update_payment_setting("recipient_name", recipient_name.strip())
    if bank_name.strip():
        db.update_payment_setting("bank_name", bank_name.strip())
    if payment_instruction.strip():
        db.update_payment_setting("payment_instruction", payment_instruction.strip())
    if trial_days.strip():
        db.update_payment_setting("trial_days", trial_days.strip())
    db.set_web_interface_url(web_url.strip() or None)
    if provider in ("sbp", "yookassa"):
        db.set_payment_provider(provider)
    return RedirectResponse("/admin/settings?msg=saved", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Global User Search
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users")
async def admin_users(request: Request, q: str = ""):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    results = _search_global_users(q) if len(q.strip()) >= 2 else []
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/users.html",
        _ctx(request, user, {"q": q, "results": results}),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Backups
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/backups")
async def admin_backups(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    bm = BackupManager()
    try:
        backups = bm.get_backup_list()
    except Exception:
        backups = []
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/backups.html",
        _ctx(request, user, {
            "backups": backups,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/backups/create")
def admin_create_backup(request: Request, csrf_token: str = Form("")):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    bm = BackupManager()
    try:
        bm.create_backup()
        bm.create_backup(custom_db_path=SHOP_BOT_DB, custom_label="shop_bot")
        bm.backup_all_tenants()
        msg = "created"
    except Exception:
        msg = "error"
    return RedirectResponse(f"/admin/backups?msg={msg}", 303)


@router.post("/backups/{filename}/delete")
async def admin_delete_backup(
    request: Request,
    filename: str,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    bm = BackupManager()
    try:
        path = os.path.join(bm.backup_dir, filename)
        if os.path.exists(path) and os.path.abspath(path).startswith(
            os.path.abspath(bm.backup_dir)
        ):
            os.remove(path)
        msg = "deleted"
    except Exception:
        msg = "error"
    return RedirectResponse(f"/admin/backups?msg={msg}", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Push diagnostics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/push-diagnostics")
async def admin_push_diagnostics(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    try:
        viewer_tz = _db().get_user_timezone(int(user["sub"])) or "Europe/Moscow"
    except Exception:
        viewer_tz = "Europe/Moscow"
    diag = _gather_push_diagnostics(viewer_tz)
    diag["csrf_token"] = get_csrf_token(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/push_diagnostics.html",
        _ctx(request, user, diag),
    )


@router.post("/push-diagnostics/generate-vapid")
def admin_generate_vapid(request: Request, csrf_token: str = Form("")):
    """Generate a fresh VAPID keypair for the super-admin to paste into Amvera env.

    Keys are returned once and never persisted/logged. Rotating them invalidates
    all existing push_subscriptions (clients must re-subscribe).
    """
    user = get_session_user(request)
    if _guard(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"error": "csrf"}, status_code=403)
    try:
        from web.push_utils import generate_vapid_keypair
        keys = generate_vapid_keypair()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    mailto = (os.environ.get("VAPID_MAILTO") or "").strip()
    if mailto and not mailto.startswith("mailto:"):
        mailto = "mailto:" + mailto
    return JSONResponse({
        "private_pem": keys["private_pem"],
        "public_b64": keys["public_b64"],
        "mailto": mailto or "mailto:admin@dailysales.app",
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _count_active_subs() -> int:
    conn = None
    try:
        conn = _raw_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE end_date > CURRENT_TIMESTAMP"
        ).fetchone()[0]
        return n
    except Exception:
        return 0
    finally:
        if conn:
            conn.close()


def _count_pending() -> int:
    conn = None
    try:
        conn = _raw_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM payment_requests WHERE status='pending'"
        ).fetchone()[0]
        return n
    except Exception:
        return 0
    finally:
        if conn:
            conn.close()


def _get_active_subs(q: str = "") -> list[dict]:
    conn = None
    try:
        conn = _raw_conn()
        rows = conn.execute("""
            SELECT s.id, s.user_id, s.plan_type, s.end_date,
                   u.first_name, u.last_name, u.username, u.shop_name, u.telegram_id
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.end_date > CURRENT_TIMESTAMP
            ORDER BY s.end_date ASC
        """).fetchall()
    except Exception:
        return []
    finally:
        if conn:
            conn.close()

    result = []
    for r in rows:
        name = f"{r[4] or ''} {r[5] or ''}".strip() or (f"@{r[6]}" if r[6] else f"ID {r[1]}")
        item = {
            "id": r[0],
            "user_id": r[1],
            "plan": r[2] or "—",
            "end_date": (r[3] or "")[:10],
            "name": name,
            "shop": r[7] or "—",
            "telegram_id": r[8],
        }
        if q:
            q_lo = q.lower()
            if q_lo not in name.lower() and q_lo not in (r[7] or "").lower():
                continue
        result.append(item)
    return result


def _get_all_shop_bot_users() -> list[dict]:
    conn = None
    try:
        conn = _raw_conn()
        rows = conn.execute(
            "SELECT id, first_name, last_name, username, shop_name "
            "FROM users ORDER BY first_name, last_name"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip()
                or (f"@{r[3]}" if r[3] else f"ID {r[0]}"),
                "shop": r[4] or "",
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def _search_global_users(q: str) -> list[dict]:
    results: list[dict] = []
    if not q or len(q.strip()) < 2:
        return results
    q_lo = q.lower()

    # shop_bot.db users
    conn = None
    try:
        conn = _raw_conn()
        for r in conn.execute("""
            SELECT u.id, u.first_name, u.last_name, u.username, u.shop_name,
                   u.telegram_id, s.plan_type, s.end_date
            FROM users u
            LEFT JOIN subscriptions s ON s.user_id = u.id
            WHERE lower_u(COALESCE(u.first_name,'') || ' ' || COALESCE(u.last_name,'')
                        || ' ' || COALESCE(u.username,'') || ' '
                        || COALESCE(u.shop_name,'')) LIKE ?
               OR CAST(u.telegram_id AS TEXT) LIKE ?
            ORDER BY u.first_name LIMIT 50
        """, (f"%{q_lo}%", f"%{q}%")).fetchall():
            results.append({
                "source": "Центральная БД",
                "source_type": "central",
                "id": r[0],
                "telegram_id": r[5] or "—",
                "name": f"{r[1] or ''} {r[2] or ''}".strip()
                or (f"@{r[3]}" if r[3] else f"ID {r[0]}"),
                "shop": r[4] or "—",
                "plan": r[6] or "Бесплатный",
                "sub_end": (r[7] or "")[:10] or "—",
            })
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    # Per-org tenant DBs
    try:
        orgs = tenant_manager.get_all_organizations()
        for org_row in orgs:
            org_id, org_name, db_path = org_row[0], org_row[1], org_row[2]
            if not db_path or not os.path.exists(db_path):
                continue
            # Если запрос совпадает с названием организации — показываем всех
            # её пользователей (поиск «Huawei» → все юзеры орг. Huawei).
            org_match = q_lo in (org_name or "").lower()
            org_conn = None
            try:
                org_conn = _raw_conn(db_path)
                if org_match:
                    rows = org_conn.execute("""
                        SELECT id, first_name, last_name, phone, shop_name, telegram_id
                        FROM users
                        ORDER BY first_name LIMIT 50
                    """).fetchall()
                else:
                    rows = org_conn.execute("""
                        SELECT id, first_name, last_name, phone, shop_name, telegram_id
                        FROM users
                        WHERE lower_u(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')
                                    || ' ' || COALESCE(phone,'') || ' '
                                    || COALESCE(shop_name,'')) LIKE ?
                           OR CAST(telegram_id AS TEXT) LIKE ?
                        LIMIT 20
                    """, (f"%{q_lo}%", f"%{q}%")).fetchall()
                for r in rows:
                    results.append({
                        "source": org_name,
                        "source_type": "org",
                        "id": r[0],
                        "telegram_id": r[5] or "—",
                        "name": f"{r[1] or ''} {r[2] or ''}".strip(),
                        "shop": r[4] or "—",
                        "plan": "—",
                        "sub_end": "—",
                    })
            except Exception:
                continue
            finally:
                if org_conn:
                    org_conn.close()
    except Exception:
        pass

    return results


# ─────────────────────────────────────────────────────────────────────────────
#  Push diagnostics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _push_provider(endpoint: str) -> dict:
    """Classify a Web Push endpoint by provider to flag delivery-reliability risks."""
    e = (endpoint or "").lower()
    if "fcm.googleapis.com" in e or "android.googleapis.com" in e:
        return {
            "label": "Google FCM",
            "icon": "🤖",
            "note": "Требует Google Play Services. На Huawei без GMS фоновая доставка не работает — "
                    "пуши приходят пачкой при открытии PWA.",
            "risk": True,
        }
    if "mozilla.com" in e:
        return {"label": "Mozilla (Firefox)", "icon": "🦊", "note": "", "risk": False}
    if "notify.windows.com" in e or "wns2-" in e or "wns.windows.com" in e:
        return {"label": "Windows (Edge)", "icon": "🪟", "note": "", "risk": False}
    if "push.apple.com" in e:
        return {"label": "Apple (Safari)", "icon": "🍎", "note": "", "risk": False}
    return {"label": "Другой", "icon": "🌐", "note": "", "risk": False}


def _resolve_push_user_names(tg_ids: list) -> dict:
    """Map telegram_id → {name, source, shop} across central + per-org DBs."""
    names: dict = {}
    if not tg_ids:
        return names
    ids = list({int(t) for t in tg_ids})
    placeholders = ",".join("?" * len(ids))

    conn = None
    try:
        conn = _raw_conn()
        for r in conn.execute(
            f"SELECT telegram_id, first_name, last_name, username, shop_name "
            f"FROM users WHERE telegram_id IN ({placeholders})", ids
        ).fetchall():
            nm = f"{r[1] or ''} {r[2] or ''}".strip() or (f"@{r[3]}" if r[3] else "")
            names[int(r[0])] = {
                "name": nm or f"ID {r[0]}",
                "source": "Центральная БД",
                "shop": r[4] or "",
            }
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    missing = [i for i in ids if i not in names]
    if missing:
        try:
            for org_row in tenant_manager.get_all_organizations():
                if not missing:
                    break
                org_name, db_path = org_row[1], org_row[2]
                if not db_path or not os.path.exists(db_path):
                    continue
                ph = ",".join("?" * len(missing))
                oc = None
                try:
                    oc = _raw_conn(db_path)
                    for r in oc.execute(
                        f"SELECT telegram_id, first_name, last_name, shop_name "
                        f"FROM users WHERE telegram_id IN ({ph})", missing
                    ).fetchall():
                        nm = f"{r[1] or ''} {r[2] or ''}".strip()
                        names[int(r[0])] = {
                            "name": nm or f"ID {r[0]}",
                            "source": org_name,
                            "shop": r[3] or "",
                        }
                except Exception:
                    continue
                finally:
                    if oc:
                        oc.close()
                missing = [i for i in ids if i not in names]
        except Exception:
            pass

    return names


def _gather_push_diagnostics(viewer_tz: str = "Europe/Moscow") -> dict:
    """Collect Web Push subscription state for the super-admin diagnostics page."""
    from timezone_utils import format_user_datetime as _fmt
    try:
        from web.push_utils import _is_configured
        vapid_ok = _is_configured()
    except Exception:
        vapid_ok = False
    vapid_public_set = bool(os.environ.get("VAPID_PUBLIC_KEY"))
    vapid_mailto_set = bool((os.environ.get("VAPID_MAILTO") or "").strip())

    rows = []
    conn = None
    try:
        conn = _raw_conn()
        rows = conn.execute(
            "SELECT user_id, endpoint, created_at FROM push_subscriptions "
            "ORDER BY user_id, created_at DESC"
        ).fetchall()
    except Exception:
        rows = []
    finally:
        if conn:
            conn.close()

    names = _resolve_push_user_names([r[0] for r in rows])

    by_user: dict = {}
    provider_counts: dict = {}
    for uid, endpoint, created in rows:
        prov = _push_provider(endpoint)
        provider_counts[prov["label"]] = provider_counts.get(prov["label"], 0) + 1
        info = names.get(int(uid), {"name": f"ID {uid}", "source": "—", "shop": ""})
        g = by_user.setdefault(int(uid), {
            "telegram_id": uid,
            "name": info["name"],
            "source": info["source"],
            "shop": info["shop"],
            "devices": [],
            "has_risk": False,
        })
        if prov["risk"]:
            g["has_risk"] = True
        g["devices"].append({
            "provider": prov["label"],
            "icon": prov["icon"],
            "note": prov["note"],
            "risk": prov["risk"],
            "created": _fmt(str(created).replace("T", " "), viewer_tz, "%d.%m.%Y %H:%M") if created else "—",
        })

    users = sorted(by_user.values(), key=lambda x: (not x["has_risk"], x["name"].lower()))

    return {
        "vapid_ok": vapid_ok,
        "vapid_public_set": vapid_public_set,
        "vapid_mailto_set": vapid_mailto_set,
        "total_subs": len(rows),
        "total_users": len(by_user),
        "provider_counts": provider_counts,
        "push_users": users,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  AI Rate-limit Config
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ai-limits")
async def admin_ai_limits(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)

    from web.rate_store import get_ai_rate_limits, get_ai_usage_stats_today
    base, high = get_ai_rate_limits()
    top_users = get_ai_usage_stats_today(top_n=30)

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/ai_limits.html",
        _ctx(request, user, {
            "base_daily_limit": base,
            "high_daily_limit": high,
            "top_users": top_users,
            "csrf_token": get_csrf_token(request),
            "msg": request.query_params.get("msg", ""),
        }),
    )


@router.post("/ai-limits/save")
async def admin_ai_limits_save(
    request: Request,
    csrf_token: str = Form(""),
    base_daily_limit: str = Form("20"),
    high_daily_limit: str = Form("200"),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/admin/ai-limits?msg=csrf_error", 303)

    try:
        base_val = max(1, min(10000, int(base_daily_limit.strip())))
        high_val = max(1, min(10000, int(high_daily_limit.strip())))
    except (ValueError, AttributeError):
        return RedirectResponse("/admin/ai-limits?msg=invalid", 303)

    conn = None
    try:
        conn = _raw_conn(SHOP_BOT_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ai_rate_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        for k, v in [("base_daily_limit", str(base_val)), ("high_daily_limit", str(high_val))]:
            conn.execute(
                "INSERT OR REPLACE INTO ai_rate_config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (k, v),
            )
        conn.commit()
    except Exception as e:
        import logging as _lg
        _lg.error("admin_ai_limits_save: %s", e)
        return RedirectResponse("/admin/ai-limits?msg=error", 303)
    finally:
        if conn:
            conn.close()

    return RedirectResponse("/admin/ai-limits?msg=saved", 303)
