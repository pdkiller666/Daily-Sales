"""Lightweight JSON API endpoints (polling, feeds)."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api")


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

        items = [
            {
                "id": r[0],
                "type": r[1] or "admin",
                "message": r[2] or "",
                "is_read": bool(r[3]),
                "created_at": (lambda s: f"{s[8:10]}.{s[5:7]}.{s[:4]} {s[11:16]}" if s and len(s) >= 16 else s)(str(r[4] or "").replace("T"," ")),
            }
            for r in rows
        ]
        return {"ok": True, "unread": unread, "items": items}
    except Exception:
        return {"ok": False, "unread": 0, "items": []}


@router.post("/my-notifications/read-all")
def my_notifications_read_all(request: Request):
    """Mark all notification_history entries as read for the current user."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return {"ok": False}

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
    from web.auth import get_session_user
    from web.deps import get_web_db
    import json as _json

    user = get_session_user(request)
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False}, status_code=401)

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
