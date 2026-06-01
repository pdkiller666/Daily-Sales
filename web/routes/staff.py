import sqlite3
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

ROLE_LABELS = {
    "owner": ("Владелец", "bg-purple-100 text-purple-700"),
    "admin": ("Администратор", "bg-blue-100 text-blue-700"),
    "user": ("Сотрудник", "bg-slate-100 text-slate-600"),
    "super_admin": ("Супер-Админ", "bg-red-100 text-red-700"),
}


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
        "staff": [], "shops": [], "shop": shop, "q": q,
        "role_labels": ROLE_LABELS, "total_count": 0, "error": None,
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

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "staff/index.html", ctx
    )


@router.get("/staff/{user_id}")
def staff_detail(request: Request, user_id: int):
    from web.auth import get_session_user
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

        ctx["member"] = {
            "id": u[0], "telegram_id": u[1],
            "first_name": u[2] or "", "last_name": u[3] or "",
            "username": u[12] or "", "shop_name": u[8] or "—",
            "city": u[9] or "", "trade_network": u[7] or "",
            "created_at": (u[11] or "")[:10],
            "role": role, "role_label": custom_title or label,
            "badge_cls": badge_cls,
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
        rate_row = db.get_salary_rate(user_id)
        # get_salary_rate returns (daily_rate,) or None
        ctx["daily_rate"] = float(rate_row[0] if rate_row else 0)
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

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "staff/detail.html", ctx
    )
