from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()


def _get_user_db_id(db, telegram_id: int) -> int | None:
    """Get internal user id from telegram_id."""
    try:
        import sqlite3
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


@router.get("/settings")
def settings_page(request: Request, saved: str = ""):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "notif_settings": None,
        "user_db_id": None,
        "saved": saved == "1",
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id)
        ctx["user_db_id"] = user_db_id

        if user_db_id:
            ctx["notif_settings"] = db.get_notification_settings(user_db_id)
        else:
            ctx["notif_settings"] = {
                "low_stock_alerts": True, "daily_reports": False,
                "sales_alerts": True, "payment_alerts": True,
                "admin_notifications": True, "stock_threshold": 5,
                "notification_time": "09:00", "shift_sale_alerts": True,
            }

        # Org info from main.db
        import sqlite3
        conn = sqlite3.connect("data/main.db")
        cur = conn.cursor()
        cur.execute(
            "SELECT o.name, o.subscription_plan, o.subscription_end, o.invite_code "
            "FROM organizations o "
            "JOIN user_org_mapping m ON m.org_id = o.id "
            "WHERE m.telegram_id = ? AND m.is_active = 1 LIMIT 1",
            (telegram_id,)
        )
        org_row = cur.fetchone()
        conn.close()
        ctx["org_name"] = org_row[0] if org_row else "—"
        ctx["org_plan"] = org_row[1] if org_row else "—"
        ctx["org_plan_end"] = (org_row[2] or "")[:10] if org_row else ""
        ctx["invite_code"] = org_row[3] if org_row else ""

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "settings/index.html", ctx
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    low_stock_alerts: str = Form(default=""),
    daily_reports: str = Form(default=""),
    sales_alerts: str = Form(default=""),
    payment_alerts: str = Form(default=""),
    admin_notifications: str = Form(default=""),
    shift_sale_alerts: str = Form(default=""),
    stock_threshold: int = Form(default=5),
    notification_time: str = Form(default="09:00"),
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id)
        if user_db_id:
            db.update_notification_settings(
                user_db_id,
                low_stock_alerts=1 if low_stock_alerts == "on" else 0,
                daily_reports=1 if daily_reports == "on" else 0,
                sales_alerts=1 if sales_alerts == "on" else 0,
                payment_alerts=1 if payment_alerts == "on" else 0,
                admin_notifications=1 if admin_notifications == "on" else 0,
                shift_sale_alerts=1 if shift_sale_alerts == "on" else 0,
                stock_threshold=max(0, stock_threshold),
                notification_time=notification_time or "09:00",
            )
    except Exception:
        pass

    return RedirectResponse(url="/settings?saved=1", status_code=303)
