"""Lightweight JSON API endpoints (polling, feeds)."""
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


@router.get("/sales-feed")
def sales_feed(request: Request, since: str = ""):
    """Return new sales since ISO timestamp `since`. Used by browser notification polling."""
    from web.auth import get_session_user
    from web.deps import get_web_db

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
                SELECT s.id, p.name, s.total_price, s.created_at,
                       u.first_name, u.shop_name
                FROM sales s
                JOIN products p ON p.id = s.product_id
                JOIN users u ON u.id = s.user_id
                WHERE s.created_at > ? AND u.telegram_id != ?
                ORDER BY s.created_at DESC
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
                "created_at": r[3],
                "seller": r[4] or "",
                "shop": r[5] or "",
            }
            for r in rows
        ]
        return {"ok": True, "count": len(items), "items": items}
    except Exception:
        return {"ok": False, "count": 0, "items": []}


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
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
