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


def _get_org_id_for_user(telegram_id: int) -> int | None:
    """Retrieve org_id from main.db for an active user."""
    try:
        import sqlite3
        conn = sqlite3.connect("data/main.db")
        row = conn.execute(
            "SELECT org_id FROM user_org_mapping WHERE telegram_id=? AND is_active=1",
            (telegram_id,)
        ).fetchone()
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
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": is_admin,
        "notif_settings": None,
        "user_db_id": None,
        "saved": saved == "1",
        "error": None,
        "scheduled_notifications": [],
        "notification_history": [],
        # invite block
        "org_id": None,
        "invite_code": "",
        "invite_deep_link": "",
        "invite_preset_role": None,
        "invite_preset_shop": None,
        "invite_shops": [],
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

        # Notification history for current user
        if user_db_id:
            try:
                hist_raw = db.get_notification_history(user_db_id, limit=15) or []
                ctx["notification_history"] = [
                    {
                        "type": h[2] or "",
                        "message": (h[3] or "")[:200],
                        "is_read": bool(h[4]),
                        "created_at": (h[5] or "")[:16].replace("T", " "),
                    }
                    for h in hist_raw
                ]
            except Exception:
                ctx["notification_history"] = []

        # Scheduled notifications (admin only)
        if is_admin:
            raw_sched = db.get_scheduled_notifications(status=None) or []
            scheduled = []
            for sn in raw_sched:
                fname = (sn[9] if len(sn) > 9 else "") or ""
                lname = (sn[10] if len(sn) > 10 else "") or ""
                creator = f"{fname} {lname}".strip() or "—"
                scheduled.append({
                    "id": sn[0], "text": (sn[3] or "")[:120],
                    "recipients_type": sn[4] or "",
                    "scheduled_at": (sn[6] or "")[:16].replace("T", " "),
                    "status": sn[7] or "pending",
                    "creator": creator,
                    "created_at": (sn[8] or "")[:10],
                })
            ctx["scheduled_notifications"] = scheduled

        # Org info from main.db
        import sqlite3
        conn = sqlite3.connect("data/main.db")
        cur = conn.cursor()
        cur.execute(
            "SELECT o.id, o.name, o.subscription_plan, o.subscription_end, o.invite_code, "
            "o.invite_preset_role, o.invite_preset_shop "
            "FROM organizations o "
            "JOIN user_org_mapping m ON m.org_id = o.id "
            "WHERE m.telegram_id = ? AND m.is_active = 1 LIMIT 1",
            (telegram_id,)
        )
        org_row = cur.fetchone()
        conn.close()

        if org_row:
            org_id = org_row[0]
            ctx["org_name"] = org_row[1] or "—"
            ctx["org_plan"] = org_row[2] or "—"
            ctx["org_plan_end"] = (org_row[3] or "")[:10]
            ctx["org_id"] = org_id

            # Ensure invite code exists (auto-generate if missing)
            invite_code = org_row[4]
            if not invite_code and is_admin:
                from tenant_manager import tenant_manager
                invite_code = tenant_manager.generate_invite_code(org_id)
            ctx["invite_code"] = invite_code or ""

            # Build deep link using bot username
            if invite_code:
                try:
                    import bot_holder
                    bot_uname = bot_holder.get_username() or ""
                except Exception:
                    bot_uname = ""
                if bot_uname:
                    ctx["invite_deep_link"] = f"https://t.me/{bot_uname}?start={invite_code}"

            ctx["invite_preset_role"] = org_row[5]
            ctx["invite_preset_shop"] = org_row[6]

            # Shops list for preset dropdown
            try:
                ctx["invite_shops"] = db.get_all_shops() or []
            except Exception:
                ctx["invite_shops"] = []
        else:
            ctx["org_name"] = "—"
            ctx["org_plan"] = "—"
            ctx["org_plan_end"] = ""

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


@router.post("/settings/rotate_invite")
async def rotate_invite(request: Request):
    from web.auth import get_session_user

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/settings#invite", status_code=303)

    telegram_id = int(user["sub"])
    org_id = _get_org_id_for_user(telegram_id)
    if org_id:
        from tenant_manager import tenant_manager
        tenant_manager.rotate_invite_code(org_id)

    return RedirectResponse(url="/settings#invite", status_code=303)


@router.post("/settings/save_invite_preset")
async def save_invite_preset(
    request: Request,
    preset_role: str = Form(default=""),
    preset_shop: str = Form(default=""),
):
    from web.auth import get_session_user

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/settings#invite", status_code=303)

    telegram_id = int(user["sub"])
    org_id = _get_org_id_for_user(telegram_id)
    if org_id:
        from tenant_manager import tenant_manager
        role_val = preset_role if preset_role else None
        shop_val = preset_shop if preset_shop else None
        tenant_manager.set_invite_preset(org_id, role_val, shop_val)

    return RedirectResponse(url="/settings#invite", status_code=303)
