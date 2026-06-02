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
