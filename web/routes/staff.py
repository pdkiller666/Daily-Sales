import sqlite3
import logging
from typing import Annotated
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()

ROLE_LABELS = {
    "owner": ("Владелец", "bg-purple-100 text-purple-700"),
    "admin": ("Администратор", "bg-blue-100 text-blue-700"),
    "user": ("Сотрудник", "bg-slate-100 text-slate-600"),
    "super_admin": ("Супер-Админ", "bg-red-100 text-red-700"),
}


def _get_org_info(db_file: str) -> tuple:
    """Return (org_id, invite_code) from main.db for this org db path."""
    try:
        conn = sqlite3.connect("data/main.db")
        cur = conn.cursor()
        cur.execute("SELECT id, invite_code FROM organizations WHERE db_path = ?", (db_file,))
        row = cur.fetchone()
        conn.close()
        return (row[0], row[1] or "") if row else (None, "")
    except Exception:
        return (None, "")


def _get_org_roles(org_db_path: str) -> dict[int, dict]:
    """Returns {telegram_id: {role, custom_title}} from main.db for this org."""
    try:
        conn = sqlite3.connect("data/main.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM organizations WHERE db_path = ?", (org_db_path,)
        )
        org_row = cursor.fetchone()
        if not org_row:
            conn.close()
            return {}
        org_id = org_row[0]
        cursor.execute(
            """SELECT telegram_id, role, custom_title, scope_type, scope_value
               FROM user_org_mapping WHERE org_id = ? AND is_active = 1""",
            (org_id,),
        )
        result = {
            row[0]: {
                "role": row[1] or "user",
                "custom_title": row[2] or "",
                "scope_type": row[3] or "",
                "scope_value": row[4] or "",
            }
            for row in cursor.fetchall()
        }
        conn.close()
        return result
    except Exception:
        return {}


@router.get("/staff")
def staff_page(
    request: Request,
    shop: str = "",
    q: str = "",
):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "is_owner": user.get("role") in ("owner", "super_admin"),
        "staff": [], "shops": [], "shop": shop, "q": q,
        "role_labels": ROLE_LABELS, "total_count": 0, "error": None,
        "csrf_token": get_csrf_token(request),
        "invite_code": "", "bot_link": "",
    }

    try:
        db = get_web_db(telegram_id, org_db)

        org_roles = _get_org_roles(db.db_file)

        shops = db.get_all_shops() or []
        ctx["shops"] = shops

        kwargs: dict = {}
        if shop:
            kwargs["shop_name"] = shop

        all_users = db.get_all_users(**kwargs) or []
        # users: id[0] telegram_id[1] first_name[2] last_name[3] middle_name[4]
        #        phone[5] email[6] trade_network[7] shop_name[8] city[9]
        #        timezone[10] created_at[11] username[12]

        all_users = [u for u in all_users if u[8] not in ("Системный", "System", None) or shop]

        if q:
            ql = q.lower()
            all_users = [
                u for u in all_users
                if ql in (u[2] or "").lower()
                or ql in (u[3] or "").lower()
                or ql in (u[12] or "").lower()
                or ql in (u[8] or "").lower()
            ]

        def _sort_key(u):
            role_info = org_roles.get(u[1], {})
            role = role_info.get("role", "user")
            order = {"owner": 0, "admin": 1, "super_admin": 0, "user": 2}.get(role, 2)
            return (order, (u[3] or "").lower(), (u[2] or "").lower())

        all_users.sort(key=_sort_key)

        from datetime import date
        today = date.today()
        month_start = today.replace(day=1).isoformat()

        staff = []
        for u in all_users:
            role_info = org_roles.get(u[1], {})
            role = role_info.get("role", "user")
            custom_title = role_info.get("custom_title", "")
            label, badge_cls = ROLE_LABELS.get(role, ("Сотрудник", "bg-slate-100 text-slate-600"))
            display_role = custom_title if custom_title else label

            try:
                summary = db.get_sales_summary(
                    start_date=month_start, end_date=today.isoformat(), user_id=u[0]
                ) or (0, 0, 0, 0)
                month_sales = int(summary[0] or 0)
                month_revenue = float(summary[2] or 0)
            except Exception:
                month_sales = 0
                month_revenue = 0.0

            staff.append({
                "id": u[0],
                "telegram_id": u[1],
                "first_name": u[2] or "",
                "last_name": u[3] or "",
                "username": u[12] or "",
                "shop_name": u[8] or "—",
                "city": u[9] or "",
                "trade_network": u[7] or "",
                "created_at": (u[11] or "")[:10],
                "role": role,
                "role_label": display_role,
                "badge_cls": badge_cls,
                "month_sales": month_sales,
                "month_revenue": month_revenue,
            })

        ctx["staff"] = staff
        ctx["total_count"] = len(staff)

        # Invite code
        try:
            org_id, invite_code = _get_org_info(db.db_file)
            if org_id and not invite_code:
                from tenant_manager import TenantManager
                tm = TenantManager()
                invite_code = tm.generate_invite_code(org_id)
            if invite_code:
                from web.app import bot_holder
                bot_uname = bot_holder.get_username() or ""
                ctx["invite_code"] = invite_code
                ctx["bot_link"] = f"https://t.me/{bot_uname}?start={invite_code}" if bot_uname else ""
        except Exception:
            pass

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "staff/index.html", ctx
    )


@router.get("/staff/invite-code")
def staff_invite_code(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        org_id, invite_code = _get_org_info(db.db_file)
        if org_id and not invite_code:
            from tenant_manager import TenantManager
            invite_code = TenantManager().generate_invite_code(org_id)
        from web.app import bot_holder
        from fastapi.responses import JSONResponse
        bot_uname = bot_holder.get_username() or ""
        return JSONResponse({
            "invite_code": invite_code or "",
            "bot_link": f"https://t.me/{bot_uname}?start={invite_code}" if (bot_uname and invite_code) else "",
        })
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Внутренняя ошибка сервера"}, status_code=500)


@router.post("/staff/invite-code/rotate")
def staff_rotate_invite(
    request: Request,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/staff", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/staff?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        org_id, _ = _get_org_info(db.db_file)
        if org_id:
            from tenant_manager import TenantManager
            TenantManager().rotate_invite_code(org_id)
    except Exception as e:
        logging.error(f"staff_rotate_invite error: {e}")

    return RedirectResponse(url="/staff", status_code=302)


@router.post("/staff/{user_id}/set-role")
def staff_set_role(
    request: Request,
    user_id: int,
    new_role: Annotated[str, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    actor_role = user.get("role", "")
    if actor_role not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)
    # Owners can promote to admin; admins can only toggle between admin/user
    allowed_targets = ("admin", "user") if actor_role == "owner" or actor_role == "super_admin" else ("user",)
    allowed_new = ("admin", "user") if actor_role in ("owner", "super_admin") else ("admin", "user")
    if new_role not in allowed_new:
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            # Check target's current role — admins can only change user-level staff
            org_roles = _get_org_roles(db.db_file)
            target_role = org_roles.get(row[0], {}).get("role", "user")
            if actor_role == "admin" and target_role not in allowed_targets:
                return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
            from tenant_manager import TenantManager
            TenantManager().change_user_role(row[0], new_role)
    except Exception as e:
        logging.error(f"staff_set_role error: {e}")

    return RedirectResponse(url=f"/staff/{user_id}", status_code=302)


@router.post("/staff/{user_id}/remove")
def staff_remove(
    request: Request,
    user_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    actor_role = user.get("role", "")
    if actor_role not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            # Enforce hierarchy: admin cannot remove admin or owner; only owner/super_admin can
            org_roles = _get_org_roles(db.db_file)
            target_role = org_roles.get(row[0], {}).get("role", "user")
            if actor_role == "admin" and target_role in ("admin", "owner", "super_admin"):
                return RedirectResponse(url=f"/staff/{user_id}?error=forbidden", status_code=302)
            if target_role == "owner" and actor_role != "super_admin":
                return RedirectResponse(url=f"/staff/{user_id}?error=forbidden", status_code=302)
            from tenant_manager import TenantManager
            TenantManager().remove_user_from_org(row[0])
    except Exception as e:
        logging.error(f"staff_remove error: {e}")

    return RedirectResponse(url="/staff", status_code=302)


@router.post("/staff/{user_id}/set-shop")
def staff_set_shop(
    request: Request,
    user_id: int,
    new_shop: Annotated[str, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    actor_role = user.get("role", "")
    if actor_role not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            target_tg_id = row[0]
            org_roles = _get_org_roles(db.db_file)
            target_role = org_roles.get(target_tg_id, {}).get("role", "user")
            # Enforce hierarchy: admin can only reassign user-level staff
            if actor_role == "admin" and target_role in ("admin", "owner", "super_admin"):
                return RedirectResponse(url=f"/staff/{user_id}?error=forbidden", status_code=302)
            # Only super_admin can reassign owners
            if target_role == "owner" and actor_role != "super_admin":
                return RedirectResponse(url=f"/staff/{user_id}?error=forbidden", status_code=302)
            shop_value = new_shop.strip()
            if shop_value:
                available_shops = db.get_all_shops() or []
                if shop_value not in available_shops:
                    return RedirectResponse(
                        url=f"/staff/{user_id}?error=Магазин+не+найден", status_code=302
                    )
            db.update_user(target_tg_id, shop_name=shop_value)
    except Exception as e:
        logging.error(f"staff_set_shop error: {e}")

    return RedirectResponse(url=f"/staff/{user_id}", status_code=303)


@router.get("/staff/{user_id}")
def staff_detail(request: Request, user_id: int):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db
    from datetime import date
    import calendar as _cal

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    today = date.today()
    year, month = today.year, today.month

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "is_owner": user.get("role") in ("owner", "super_admin"),
        "member": None, "member_id": user_id,
        "role_labels": ROLE_LABELS,
        "recent_sales": [],
        "month_summary": (0, 0, 0, 0),
        "all_summary": (0, 0, 0, 0),
        "daily_rate": 0.0, "worked_days": 0,
        "cal_grid": [], "work_days_set": set(),
        "month_name": {1:"Январь",2:"Февраль",3:"Март",4:"Апрель",5:"Май",6:"Июнь",
                       7:"Июль",8:"Август",9:"Сентябрь",10:"Октябрь",11:"Ноябрь",12:"Декабрь"
                       }.get(month, str(month)),
        "year": year, "month": month,
        "error": None,
        "csrf_token": get_csrf_token(request),
        "shops": [],
        "cities": [],
        "user_plans": [],
        "chart_labels": [],
        "chart_data": [],
    }

    try:
        db = get_web_db(telegram_id, org_db)
        org_roles = _get_org_roles(db.db_file)

        # Get user row
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        u = cur.fetchone()
        conn.close()

        if not u:
            return RedirectResponse(url="/staff", status_code=302)

        # users: id[0] telegram_id[1] first_name[2] last_name[3] middle_name[4]
        #        phone[5] email[6] trade_network[7] shop_name[8] city[9]
        #        timezone[10] created_at[11] username[12]
        role_info = org_roles.get(u[1], {})
        role = role_info.get("role", "user")
        label, badge_cls = ROLE_LABELS.get(role, ("Сотрудник", "bg-slate-100 text-slate-600"))
        custom_title = role_info.get("custom_title", "")

        import json as _json
        scope_type_raw = role_info.get("scope_type", "")
        scope_value_raw = role_info.get("scope_value", "")
        scope_values: list = []
        if scope_value_raw:
            try:
                scope_values = _json.loads(scope_value_raw)
            except Exception:
                scope_values = [scope_value_raw]

        ctx["member"] = {
            "id": u[0], "telegram_id": u[1],
            "first_name": u[2] or "", "last_name": u[3] or "",
            "username": u[12] or "", "shop_name": u[8] or "—",
            "city": u[9] or "", "trade_network": u[7] or "",
            "created_at": (u[11] or "")[:10],
            "role": role, "role_label": custom_title or label,
            "badge_cls": badge_cls,
            "custom_title": custom_title,
            "scope_type": scope_type_raw or "all",
            "scope_values": scope_values,
        }

        # Monthly sales summary
        month_start = today.replace(day=1).isoformat()
        ctx["month_summary"] = db.get_sales_summary(
            start_date=month_start, end_date=today.isoformat(), user_id=user_id
        ) or (0, 0, 0, 0)

        # All-time summary
        ctx["all_summary"] = db.get_sales_summary(user_id=user_id) or (0, 0, 0, 0)

        # Recent sales (last 20)
        ctx["recent_sales"] = db.get_user_sales(user_id, limit=20) or []

        # Salary info
        # get_salary_rate returns a float directly
        ctx["daily_rate"] = float(db.get_salary_rate(user_id) or 0)
        ctx["worked_days"] = db.get_worked_days_count(user_id, year, month)

        # Calendar grid
        work_days = db.get_work_schedule(user_id, year, month)
        first_weekday, days_in_month = _cal.monthrange(year, month)
        cal_grid: list[list[int]] = []
        week: list[int] = [0] * first_weekday
        for d in range(1, days_in_month + 1):
            week.append(d)
            if len(week) == 7:
                cal_grid.append(week)
                week = []
        if week:
            week += [0] * (7 - len(week))
            cal_grid.append(week)
        ctx["cal_grid"] = cal_grid
        ctx["work_days_set"] = {int(d[8:10]) for d in work_days}

        # Available shops and cities for scope/reassignment
        ctx["shops"] = db.get_all_shops() or []
        try:
            ctx["cities"] = [r[0] for r in (db.get_all_cities() or []) if r[0]]
        except Exception:
            ctx["cities"] = []

        # 7-day revenue chart for this user
        try:
            from datetime import timedelta
            tz_staff = db.get_user_timezone(u[1] if u else telegram_id) or "Europe/Moscow"
            from timezone_utils import get_current_user_time
            today_tz = get_current_user_time(tz_staff).date()
            chart_labels = []
            chart_data = []
            for i in range(6, -1, -1):
                d = today_tz - timedelta(days=i)
                ds = d.isoformat()
                summary = db.get_sales_summary(start_date=ds, end_date=ds, user_id=user_id) or (0, 0, 0, 0)
                chart_labels.append(d.strftime('%d.%m'))
                chart_data.append(int(float(summary[2] or 0)))
            ctx["chart_labels"] = chart_labels
            ctx["chart_data"] = chart_data
        except Exception:
            ctx["chart_labels"] = []
            ctx["chart_data"] = []

        # Active plans for this user
        try:
            from timezone_utils import get_current_user_time
            tz_local = db.get_user_timezone(telegram_id)
            today_local = get_current_user_time(tz_local).date()
            all_progress = db.get_plans_progress(local_today=today_local) or []
            member_shop = (ctx["member"] or {}).get('shop_name', '')
            user_plans = []
            for plan_row, actual, pct in all_progress:
                # plan_row[4] = target_type ('seller' or 'shop'), NOT plan_row[1] (plan_type)
                is_user_plan = (plan_row[4] == 'seller' and plan_row[5] == user_id)
                is_shop_plan = (plan_row[4] == 'shop' and plan_row[6] == member_shop)
                if is_user_plan or is_shop_plan:
                    target = float(plan_row[3] or 1)
                    user_plans.append({
                        "id": plan_row[0],
                        "plan_type": plan_row[1],
                        "metric_type": plan_row[2],
                        "target_value": target,
                        "target_type": plan_row[4],
                        "shop_name": plan_row[6] or "",
                        "current": float(actual or 0),
                        "pct": min(100, int(pct or 0)),
                    })
            ctx["user_plans"] = user_plans
        except Exception:
            ctx["user_plans"] = []

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "staff/detail.html", ctx
    )


@router.post("/staff/{user_id}/set-scope")
def staff_set_scope(
    request: Request,
    user_id: int,
    scope_type: Annotated[str, Form()],
    scope_values: Annotated[str, Form()] = "",
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    actor_role = user.get("role", "")
    if actor_role not in ("owner", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            org_roles = _get_org_roles(db.db_file)
            target_telegram_id = row[0]
            target_role = org_roles.get(target_telegram_id, {}).get("role", "user")
            if target_role == "owner" and actor_role != "super_admin":
                return RedirectResponse(url=f"/staff/{user_id}?error=forbidden", status_code=302)
            # Parse scope values (comma-separated)
            vals = [v.strip() for v in scope_values.split(",") if v.strip()] if scope_values else []
            stype = scope_type if scope_type in ("all", "shop", "city", "network") else "all"
            sv = vals if stype != "all" else None
            from tenant_manager import TenantManager
            TenantManager().change_user_role(target_telegram_id, target_role,
                                             scope_type=stype, scope_value=sv)
    except Exception as e:
        logging.error(f"staff_set_scope error: {e}")

    return RedirectResponse(url=f"/staff/{user_id}", status_code=302)


@router.post("/staff/{user_id}/set-custom-title")
def staff_set_custom_title(
    request: Request,
    user_id: int,
    custom_title: Annotated[str, Form()] = "",
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    actor_role = user.get("role", "")
    if actor_role not in ("owner", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            org_roles = _get_org_roles(db.db_file)
            target_telegram_id = row[0]
            target_role = org_roles.get(target_telegram_id, {}).get("role", "user")
            title = custom_title.strip()[:50] if custom_title.strip() else None
            from tenant_manager import TenantManager
            TenantManager().change_user_role(target_telegram_id, target_role,
                                             custom_title=title)
    except Exception as e:
        logging.error(f"staff_set_custom_title error: {e}")

    return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
