"""Lightweight JSON API endpoints (polling, feeds)."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import os as _os

router = APIRouter(prefix="/api")

SHOP_BOT_DB = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))),
    "data", "shop_bot.db",
)


@router.get("/sales-feed")
def sales_feed(request: Request, since: str = ""):
    """Return new sales since ISO timestamp `since`. Used by browser notification polling."""
    from web.auth import get_session_user
    from web.deps import get_web_db
    from web.app import _api_rate_ok

    ip = request.client.host if request.client else "unknown"
    if not _api_rate_ok(ip):
        return JSONResponse({"ok": False, "count": 0, "items": []}, status_code=429)

    user = get_session_user(request)
    if not user:
        return {"ok": False, "count": 0, "items": []}

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()

        if since:
            rows = conn.execute(
                """
                SELECT s.id, p.name,
                       CAST(s.quantity_sold AS REAL) * COALESCE(s.sale_price, 0),
                       s.sale_date,
                       u.first_name, u.shop_name
                FROM sales s
                JOIN products p ON p.id = s.product_id
                JOIN users u ON u.id = s.user_id
                WHERE s.sale_date > ? AND u.telegram_id != ?
                ORDER BY s.sale_date DESC
                LIMIT 10
                """,
                (since, telegram_id),
            ).fetchall()
        else:
            rows = []

        conn.close()

        items = [
            {
                "id": r[0],
                "product": r[1] or "Товар",
                "total": int(r[2] or 0),
                "created_at": (lambda s: (lambda p: f"{p[2]}.{p[1]}.{p[0]}")(s[:10].split("-")) if s and len(s) >= 10 else (s or ""))(str(r[3] or "")),
                "seller": r[4] or "",
                "shop": r[5] or "",
            }
            for r in rows
        ]
        return {"ok": True, "count": len(items), "items": items}
    except Exception:
        return {"ok": False, "count": 0, "items": []}


def _notif_url(notification_type: str) -> str:
    """Map notification_type to the most relevant web page URL."""
    _MAP = {
        "shift_sale":        "/sales",
        "sales":             "/sales",
        "plan_milestone":    "/plans",
        "low_stock":         "/inventory",
        "daily_report":      "/reports",
        "payment":           "/subscription",
        "trial_expired":     "/subscription",
        "trial_expiring":    "/subscription",
        "contest":           "/contests",
        "contest_winner":    "/contests",
        "salary_adjustment": "/salary/earnings",
        "task_assigned":     "/tasks",
        "task_status":       "/tasks",
        "task_deadline":     "/tasks",
        "task_overdue":      "/tasks",
        "dm":                "/chat/dm",
        "admin":             "",
    }
    return _MAP.get(notification_type or "", "")


@router.get("/my-notifications")
def my_notifications(request: Request, limit: int = 20):
    """Return current user's notification history with unread count."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return {"ok": False, "unread": 0, "items": []}

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()

        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": True, "unread": 0, "items": []}
        user_db_id = row[0]

        unread = conn.execute(
            "SELECT COUNT(*) FROM notification_history WHERE user_id = ? AND is_read = 0",
            (user_db_id,),
        ).fetchone()[0]

        rows = conn.execute(
            """
            SELECT id, notification_type, message, is_read, created_at
            FROM notification_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_db_id, min(limit, 50)),
        ).fetchall()

        conn.close()

        dms = db.get_dm_unread_count(user_db_id)

        from timezone_utils import format_user_datetime as _fmt_dt
        _tz = db.get_user_timezone(telegram_id) or "Europe/Moscow"

        items = [
            {
                "id": r[0],
                "type": r[1] or "admin",
                "message": r[2] or "",
                "is_read": bool(r[3]),
                "created_at": _fmt_dt(str(r[4]).replace("T", " "), _tz, "%d.%m.%Y %H:%M") if r[4] else "",
                "url": _notif_url(r[1] or "admin"),
            }
            for r in rows
        ]
        return {"ok": True, "unread": unread, "dms": dms, "items": items}
    except Exception:
        return {"ok": False, "unread": 0, "dms": 0, "items": []}


@router.post("/my-notifications/read-all")
def my_notifications_read_all(request: Request):
    """Mark all notification_history entries as read for the current user."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return {"ok": False}

    token = request.headers.get("X-CSRF-Token", "")
    if not verify_csrf_token(request, token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE notification_history SET is_read = 1 WHERE user_id = ? AND is_read = 0",
                (row[0],),
            )
            conn.commit()
        conn.close()
        return {"ok": True}
    except Exception:
        return {"ok": False}


@router.get("/unread-count")
def unread_count(request: Request):
    """Lightweight endpoint: непрочитанные уведомления + DM. Используется App Badge API и будущим Web Push SW."""
    from web.auth import get_session_user
    from web.deps import get_web_db
    from web.app import _api_rate_ok

    ip = request.client.host if request.client else "unknown"
    if not _api_rate_ok(ip):
        return JSONResponse({"ok": False, "total": 0, "notifs": 0, "dms": 0}, status_code=429)

    user = get_session_user(request)
    if not user:
        return {"ok": False, "total": 0, "notifs": 0, "dms": 0}

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": True, "total": 0, "notifs": 0, "dms": 0}
        user_db_id = row[0]
        notifs = conn.execute(
            "SELECT COUNT(*) FROM notification_history WHERE user_id = ? AND is_read = 0",
            (user_db_id,),
        ).fetchone()[0]
        conn.close()
        dms = db.get_dm_unread_count(user_db_id)
        total = notifs + dms
        return {"ok": True, "total": total, "notifs": notifs, "dms": dms}
    except Exception:
        return {"ok": False, "total": 0, "notifs": 0, "dms": 0}


@router.get("/push/status")
def push_status(request: Request):
    """Diagnostic endpoint: VAPID configured + server-side subscription count.

    Returns:
      vapid         — True if VAPID private key is configured on the server
      subs_count    — number of active push subscriptions for this user in DB
      permission    — placeholder ("unknown"); actual browser permission is
                      read client-side via Notification.permission
      last_sent     — ISO UTC string of most recent successful delivery, or null
      recent_pushes — list of up to 3 recent deliveries [{title, sent_at}, ...]
    """
    from web.auth import get_session_user
    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    tg_id = int(user["sub"])
    try:
        from web.push_utils import is_configured, _get_subscriptions
        import sqlite3 as _sqlite3
        import os as _os2
        vapid = is_configured()
        subs_count = len(_get_subscriptions(tg_id))
        _shop_db = _os2.path.join(
            _os2.path.dirname(_os2.path.dirname(_os2.path.dirname(__file__))),
            "data", "shop_bot.db",
        )
        recent_pushes: list = []
        last_sent: str | None = None
        try:
            _conn = _sqlite3.connect(_shop_db)
            rows = _conn.execute(
                """SELECT title, sent_at FROM push_delivery_log
                   WHERE user_id = ?
                   ORDER BY sent_at DESC
                   LIMIT 3""",
                (tg_id,),
            ).fetchall()
            _conn.close()
            recent_pushes = [{"title": r[0], "sent_at": r[1]} for r in rows]
            last_sent = recent_pushes[0]["sent_at"] if recent_pushes else None
        except Exception:
            pass
        return {
            "ok": True,
            "vapid": vapid,
            "subs_count": subs_count,
            "permission": "unknown",
            "last_sent": last_sent,
            "recent_pushes": recent_pushes,
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "vapid": False, "subs_count": 0,
                             "permission": "unknown", "last_sent": None,
                             "recent_pushes": [], "error": str(exc)}, status_code=500)


@router.get("/push/vapid-public-key")
def push_vapid_key(request: Request):
    """Return VAPID public key for browser subscription."""
    from web.auth import get_session_user
    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    key = _os.environ.get("VAPID_PUBLIC_KEY", "")
    return {"ok": bool(key), "key": key}


@router.post("/push/test")
async def push_test(request: Request):
    """Send a test push to the current user (for QA / debugging).

    Returns honest diagnostic info:
      ok=True  + code="ok"               — sent successfully
      ok=False + code="vapid_missing"    — VAPID keys not configured on server
      ok=False + code="no_subscriptions" — browser sub not registered / expired
      ok=False + code="send_failed"      — subscriptions found but push service rejected all
      ok=False + code="error"            — unexpected exception
    """
    from web.auth import get_session_user
    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "code": "auth"}, status_code=401)
    tg_id = int(user["sub"])
    try:
        import asyncio
        from web.push_utils import send_web_push, is_configured, _get_subscriptions
        if not is_configured():
            return JSONResponse({
                "ok": False, "code": "vapid_missing",
                "msg": "VAPID-ключи не настроены на сервере",
            })
        subs_count = len(_get_subscriptions(tg_id))
        if subs_count == 0:
            return JSONResponse({
                "ok": False, "code": "no_subscriptions",
                "msg": "Нет активных подписок — браузер не зарегистрирован",
            })
        result = await asyncio.to_thread(
            send_web_push, tg_id,
            "🔔 Тестовое уведомление",
            "Push-уведомления работают корректно ✅",
            "/dashboard",
        )
        sent = result.get("sent", 0) if isinstance(result, dict) else 0
        gone = result.get("gone", 0) if isinstance(result, dict) else 0
        if sent == 0:
            return JSONResponse({
                "ok": False, "code": "send_failed",
                "msg": f"Подписок найдено: {subs_count}, отправлено: 0, просрочено: {gone}",
            })
        return {"ok": True, "code": "ok", "sent": sent,
                "msg": f"Тестовый push отправлен на {sent} устройств(а)"}
    except Exception as exc:
        return JSONResponse({"ok": False, "code": "error", "error": str(exc)}, status_code=500)


@router.post("/push/subscribe")
async def push_subscribe(request: Request):
    """Save browser push subscription (endpoint + keys) to shop_bot.db.

    CSRF note: JSON endpoint protected by samesite=lax session cookie — no CSRF
    token needed. Service Worker's pushsubscriptionchange handler also calls this
    and cannot attach DOM-derived tokens.
    """
    from web.auth import get_session_user
    from database import Database
    import json as _json

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)

    tg_id = int(user["sub"])
    try:
        body = await request.json()
        endpoint = (body.get("endpoint") or "")[:2048]
        keys = body.get("keys") or {}
        p256dh = (keys.get("p256dh") or "")[:256]
        auth   = (keys.get("auth")   or "")[:128]
        if not endpoint or not p256dh or not auth:
            return JSONResponse({"ok": False, "error": "missing fields"}, status_code=400)
        if not endpoint.startswith("https://"):
            return JSONResponse({"ok": False, "error": "invalid endpoint"}, status_code=400)
        db = Database(SHOP_BOT_DB)
        db.create_tables()
        ok = db.save_push_subscription(tg_id, endpoint, p256dh, auth)
        return {"ok": ok}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    """Remove push subscription from shop_bot.db."""
    from web.auth import get_session_user
    from database import Database

    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"ok": False}, status_code=403)

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)

    tg_id = int(user["sub"])
    try:
        body = await request.json()
        endpoint = body.get("endpoint", "")
        db = Database(SHOP_BOT_DB)
        if endpoint:
            db.delete_push_subscription(tg_id, endpoint)
        else:
            db.delete_all_push_subscriptions(tg_id)
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/nav-config")
def get_nav_config(request: Request):
    """Return user's mobile nav config (list of 4 keys)."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return {"nav": None}

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        cfg = db.get_user_nav_config(telegram_id)
        return {"nav": cfg}
    except Exception:
        return {"nav": None}


@router.post("/nav-config")
async def set_nav_config(request: Request):
    """Save user's mobile nav config."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    import json as _json

    user = get_session_user(request)
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False}, status_code=401)

    token = request.headers.get("X-CSRF-Token", "")
    if not verify_csrf_token(request, token):
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        form = await request.form()
        nav_json = form.get("nav_json", "[]")
        cfg = _json.loads(nav_json)
        if not isinstance(cfg, list) or not cfg:
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": False, "error": "Invalid config"}, status_code=400)
        cfg = [str(k) for k in cfg[:4]]
        db = get_web_db(telegram_id, org_db)
        db.set_user_nav_config(telegram_id, cfg)
        return {"ok": True}
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)
