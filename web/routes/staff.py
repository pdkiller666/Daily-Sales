import sqlite3
import logging
from datetime import date
from typing import Annotated
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()

MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def _adjacent_month(year: int, month: int, delta: int):
    """Return (year, month) shifted by delta months."""
    total = (year - 1) * 12 + (month - 1) + delta
    return (total // 12 + 1, total % 12 + 1)


_RU_MONTHS_SHORT = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}
_ABSENCE_LABELS = {
    "vacation": "Отпуск", "sick": "Больничный",
    "compensatory": "Отгул", "absence": "Прогул",
}


def _absence_day_sets(absence_rows, year: int, month: int):
    """Expand approved absence records into sets of calendar day numbers.

    Returns (vacation_days, sick_days, other_days) — all sets of int day nums.
    absence_rows columns: id[0] type[1] start_date[2] end_date[3] status[4] is_paid[5] ...
    """
    from datetime import date as _date, timedelta as _td
    import calendar as _cal
    last_day = _cal.monthrange(year, month)[1]
    month_start = _date(year, month, 1)
    month_end = _date(year, month, last_day)
    vacation_days: set = set()
    sick_days: set = set()
    other_days: set = set()
    for ab in (absence_rows or []):
        if ab[4] != "approved":
            continue
        atype = ab[1]
        try:
            sd = max(_date.fromisoformat(ab[2]), month_start)
            ed = min(_date.fromisoformat(ab[3]), month_end)
        except Exception:
            continue
        cur = sd
        while cur <= ed:
            if atype == "vacation":
                vacation_days.add(cur.day)
            elif atype == "sick":
                sick_days.add(cur.day)
            else:
                other_days.add(cur.day)
            cur += _td(days=1)
    return vacation_days, sick_days, other_days


def _absence_tooltip_map(absence_rows, year: int, month: int) -> dict:
    """Build {day_num: tooltip_text} for approved absences in the given month.

    Shows the full (unclamped) date range so e.g. a vacation spanning two months
    reads "Отпуск: 28 мая – 10 июн" even when viewed in June.
    absence_rows columns: id[0] type[1] start_date[2] end_date[3] status[4] ...
    """
    from datetime import date as _date, timedelta as _td
    import calendar as _cal
    last_day = _cal.monthrange(year, month)[1]
    month_start = _date(year, month, 1)
    month_end = _date(year, month, last_day)
    result: dict = {}
    for ab in (absence_rows or []):
        if ab[4] != "approved":
            continue
        label = _ABSENCE_LABELS.get(ab[1], "Отсутствие")
        try:
            raw_sd = _date.fromisoformat(ab[2])
            raw_ed = _date.fromisoformat(ab[3])
            sd = max(raw_sd, month_start)
            ed = min(raw_ed, month_end)
        except Exception:
            continue
        if raw_sd.month == raw_ed.month:
            tip = f"{label}: {raw_sd.day}–{raw_ed.day} {_RU_MONTHS_SHORT[raw_sd.month]}"
        else:
            tip = f"{label}: {raw_sd.day}\u00a0{_RU_MONTHS_SHORT[raw_sd.month]} – {raw_ed.day}\u00a0{_RU_MONTHS_SHORT[raw_ed.month]}"
        cur = sd
        while cur <= ed:
            if cur.day not in result:
                result[cur.day] = tip
            cur += _td(days=1)
    return result


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
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, invite_code FROM organizations WHERE db_path = ?", (db_file,))
            row = cur.fetchone()
        finally:
            conn.close()
        return (row[0], row[1] or "") if row else (None, "")
    except Exception:
        return (None, "")


def _get_org_roles(org_db_path: str) -> dict[int, dict]:
    """Returns {telegram_id: {role, custom_title}} from main.db for this org."""
    try:
        conn = sqlite3.connect("data/main.db")
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM organizations WHERE db_path = ?", (org_db_path,)
            )
            org_row = cursor.fetchone()
            if not org_row:
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
        finally:
            conn.close()
        return result
    except Exception:
        return {}


@router.get("/staff")
def staff_page(
    request: Request,
    shop: str = "",
    q: str = "",
    sort_col: str = "role",
    sort_order: str = "asc",
    year: int = 0,
    month: int = 0,
):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db
    import calendar as _cal

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")

    _VALID_STAFF_COLS = ("role", "name", "shop", "city", "month_sales", "month_revenue")
    sort_col = sort_col if sort_col in _VALID_STAFF_COLS else "role"
    sort_order = sort_order if sort_order in ("asc", "desc") else "asc"

    today = date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month
    year  = max(2015, min(year, 2040))
    month = max(1,    min(month, 12))

    is_current_month = (year == today.year and month == today.month)
    prev_y, prev_m = _adjacent_month(year, month, -1)
    next_y, next_m = _adjacent_month(year, month, 1)

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "is_owner": user.get("role") in ("owner", "super_admin"),
        "staff": [], "shops": [], "shop": shop, "q": q,
        "sort_col": sort_col, "sort_order": sort_order,
        "role_labels": ROLE_LABELS, "total_count": 0, "error": None,
        "csrf_token": get_csrf_token(request),
        "invite_code": "", "bot_link": "",
        "year": year, "month": month,
        "month_name": MONTH_NAMES.get(month, str(month)),
        "is_current_month": is_current_month,
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
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

        def _role_order(u):
            role_info = org_roles.get(u[1], {})
            role = role_info.get("role", "user")
            return {"owner": 0, "super_admin": 0, "admin": 1, "user": 2}.get(role, 2)

        def _sort_key(u):
            return (_role_order(u), (u[3] or "").lower(), (u[2] or "").lower())

        all_users.sort(key=_sort_key)

        last_day = _cal.monthrange(year, month)[1]
        month_start = f"{year}-{month:02d}-01"
        month_end = f"{year}-{month:02d}-{last_day}"

        # Sales stats for the viewed month
        try:
            if is_current_month:
                bulk_stats = db.get_users_sales_summary_bulk(month_start, today.isoformat())
            else:
                bulk_stats = db.get_users_sales_summary_bulk(month_start, month_end)
        except Exception:
            bulk_stats = {}

        # Absence badges — today if current month, else any approved absence in the month
        absent_map: dict = {}
        try:
            if is_current_month:
                absent_map = db.get_absent_users_today(today.isoformat())
            else:
                all_absences = db.get_all_absences_admin(year, month)
                for _ar in all_absences:
                    _uid = _ar[1]
                    _atype = _ar[2]
                    _status = _ar[5]
                    if _status == "approved" and _uid not in absent_map:
                        absent_map[_uid] = _atype
        except Exception:
            pass

        staff = []
        for u in all_users:
            role_info = org_roles.get(u[1], {})
            role = role_info.get("role", "user")
            custom_title = role_info.get("custom_title", "")
            label, badge_cls = ROLE_LABELS.get(role, ("Сотрудник", "bg-slate-100 text-slate-600"))
            display_role = custom_title if custom_title else label

            s = bulk_stats.get(u[0], (0, 0, 0, 0))
            month_sales = int(s[0])
            month_revenue = float(s[2])

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
                "absence_type": absent_map.get(u[0], ""),
            })

        # Apply user-requested sort on top of the default role-order pre-sort
        rev = (sort_order == "desc")
        if sort_col == "name":
            staff.sort(key=lambda s: (s["last_name"].lower(), s["first_name"].lower()), reverse=rev)
        elif sort_col == "shop":
            staff.sort(key=lambda s: s["shop_name"].lower(), reverse=rev)
        elif sort_col == "city":
            staff.sort(key=lambda s: s["city"].lower(), reverse=rev)
        elif sort_col == "month_sales":
            staff.sort(key=lambda s: s["month_sales"], reverse=rev)
        elif sort_col == "month_revenue":
            staff.sort(key=lambda s: s["month_revenue"], reverse=rev)
        # "role" (default) keeps the pre-sort order

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
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "module_required"}, status_code=403)
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
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
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
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        finally:
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
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        finally:
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
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        finally:
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
def staff_detail(request: Request, user_id: int, year: int = 0, month: int = 0):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db
    from datetime import date
    import calendar as _cal

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)

    today = date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month
    year  = max(2015, min(year, 2040))
    month = max(1,    min(month, 12))

    is_current_month = (year == today.year and month == today.month)
    prev_y, prev_m = _adjacent_month(year, month, -1)
    next_y, next_m = _adjacent_month(year, month, 1)

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "is_owner": user.get("role") in ("owner", "super_admin"),
        "member": None, "member_id": user_id,
        "role_labels": ROLE_LABELS,
        "recent_sales": [],
        "month_summary": (0, 0, 0, 0),
        "all_summary": (0, 0, 0, 0),
        "daily_rate": 0.0, "worked_days": 0, "paid_absence_days": 0, "current_absence": None,
        "cal_grid": [], "work_days_set": set(),
        "vacation_days": set(), "sick_days": set(), "other_absence_days": set(),
        "absence_tooltip_map": {},
        "month_name": MONTH_NAMES.get(month, str(month)),
        "year": year, "month": month,
        "is_current_month": is_current_month,
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "error": None,
        "csrf_token": get_csrf_token(request),
        "shops": [],
        "cities": [],
        "user_plans": [],
        "chart_labels": [],
        "chart_data": [],
        "user_tz": "Europe/Moscow",
    }

    try:
        db = get_web_db(telegram_id, org_db)
        try:
            ctx["user_tz"] = db.get_user_timezone(telegram_id) or "Europe/Moscow"
        except Exception:
            pass
        org_roles = _get_org_roles(db.db_file)

        # Get user row
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            u = cur.fetchone()
        finally:
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

        # Monthly sales summary — use the viewed year/month, not always today
        viewed_month_start = date(year, month, 1).isoformat()
        if is_current_month:
            viewed_month_end = today.isoformat()
        else:
            last_day = _cal.monthrange(year, month)[1]
            viewed_month_end = date(year, month, last_day).isoformat()
        ctx["month_summary"] = db.get_sales_summary(
            start_date=viewed_month_start, end_date=viewed_month_end, user_id=user_id
        ) or (0, 0, 0, 0)

        # All-time summary
        ctx["all_summary"] = db.get_sales_summary(user_id=user_id) or (0, 0, 0, 0)

        # Recent sales — scoped to the viewed month
        ctx["recent_sales"] = db.get_user_sales_by_date(
            user_id, viewed_month_start, viewed_month_end
        ) or []

        # Calendar grid — build first so we have work_days_set for salary calc
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
        work_days_set: set = {int(d[8:10]) for d in work_days}
        ctx["work_days_set"] = work_days_set

        # Absence day-sets for calendar coloring (all months)
        try:
            _abs_for_cal = db.get_absences_for_user(user_id, year, month)
            _vac, _sick, _other = _absence_day_sets(_abs_for_cal, year, month)
            ctx["vacation_days"] = _vac
            ctx["sick_days"] = _sick
            ctx["other_absence_days"] = _other
            ctx["absence_tooltip_map"] = _absence_tooltip_map(_abs_for_cal, year, month)
        except Exception:
            _abs_for_cal = []

        # Absence map for the current month — used for consistent shift counting
        # and for current-absence badge
        try:
            _abs_raw = db.get_absence_days_map(year, month, user_id)
            absence_map_user: dict = _abs_raw.get(user_id, {})
        except Exception:
            absence_map_user = {}

        # Days where approved absence overlaps a scheduled work day
        try:
            approved_absence_day_nums = {
                day_num for day_num, info in absence_map_user.items()
                if info.get("status") == "approved"
            }
            absence_days_in_schedule: int = len(work_days_set & approved_absence_day_nums)
        except Exception:
            absence_days_in_schedule = 0

        # Salary info
        # effective_worked_days = scheduled days minus absence-covered days
        # (mirrors what the schedule page counter shows)
        ctx["daily_rate"] = float(db.get_salary_rate(user_id) or 0)
        raw_worked_days: int = db.get_worked_days_count(user_id, year, month)
        effective_worked_days: int = max(0, raw_worked_days - absence_days_in_schedule)
        ctx["worked_days"] = effective_worked_days
        try:
            ctx["paid_absence_days"] = db.get_paid_absence_days_count(user_id, year, month)
        except Exception:
            ctx["paid_absence_days"] = 0

        # Absence badge — month-aware: today's status for current month,
        # any approved absence in the viewed month for past months
        try:
            if is_current_month:
                today_str = today.isoformat()
                absent_map = db.get_absent_users_today(today_str)
                absent_type = absent_map.get(user_id)
                if absent_type:
                    current_absence: dict | None = {"type": absent_type, "end_date": today_str}
                    try:
                        absence_rows = db.get_absences_for_user(user_id, year, month)
                        for ab in absence_rows:
                            # ab: (id, type, start_date, end_date, status, is_paid, comment, admin_comment, created_at)
                            if ab[4] == "approved" and ab[2] <= today_str <= ab[3]:
                                current_absence["end_date"] = ab[3]
                                break
                    except Exception:
                        pass
                else:
                    current_absence = None
            else:
                # Past month — show any approved absence that occurred in the viewed month
                absence_rows = db.get_absences_for_user(user_id, year, month)
                current_absence = None
                for ab in absence_rows:
                    # ab: (id, type, start_date, end_date, status, is_paid, comment, admin_comment, created_at)
                    if ab[4] == "approved":
                        current_absence = {"type": ab[1], "end_date": ab[3]}
                        break
            ctx["current_absence"] = current_absence
        except Exception:
            ctx["current_absence"] = None

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

        # Org-structure: departments, custom roles, per-user module access
        try:
            from db_utils import org_structure_level
            from web.routes.org_structure import MODULE_OPTIONS
            target_tg = u[1]
            ctx["org_level"] = org_structure_level(telegram_id)
            ctx["departments"] = db.get_departments() or []
            ctx["module_options"] = MODULE_OPTIONS
            ctx["module_access_map"] = db.get_user_module_access_map(target_tg) or {}
            if ctx["org_level"] == "full":
                ctx["org_roles_list"] = db.get_org_roles() or []
            else:
                ctx["org_roles_list"] = []
            from tenant_manager import TenantManager
            ext = TenantManager().get_user_mapping_ext(target_tg)
            ctx["member_dept_id"] = ext.get("department_id")
            ctx["member_org_role_id"] = ext.get("org_role_id")
        except Exception as e:
            logging.error(f"staff_detail org-structure error: {e}")
            ctx["org_level"] = "minimal"
            ctx["departments"] = []
            ctx["org_roles_list"] = []
            ctx["module_options"] = []
            ctx["module_access_map"] = {}
            ctx["member_dept_id"] = None
            ctx["member_org_role_id"] = None

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
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        finally:
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
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        finally:
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


def _target_tg(db, user_id: int):
    """telegram_id сотрудника по его users.id."""
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@router.post("/staff/{user_id}/set-department")
def staff_set_department(
    request: Request,
    user_id: int,
    department_id: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        target_tg = _target_tg(db, user_id)
        if target_tg is not None:
            dept_id = int(department_id) if department_id.strip().isdigit() else None
            from tenant_manager import TenantManager
            TenantManager().set_user_department(target_tg, dept_id)
    except Exception as e:
        logging.error(f"staff_set_department error: {e}")

    return RedirectResponse(url=f"/staff/{user_id}", status_code=302)


@router.post("/staff/{user_id}/set-org-role")
def staff_set_org_role(
    request: Request,
    user_id: int,
    org_role_id: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from db_utils import org_structure_level

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        if org_structure_level(telegram_id) != "full":
            return RedirectResponse(url=f"/staff/{user_id}?error=paid_only", status_code=302)
        db = get_web_db(telegram_id, org_db)
        target_tg = _target_tg(db, user_id)
        if target_tg is not None:
            role_id = int(org_role_id) if org_role_id.strip().isdigit() else None
            from tenant_manager import TenantManager
            TenantManager().assign_org_role(target_tg, role_id)
    except Exception as e:
        logging.error(f"staff_set_org_role error: {e}")

    return RedirectResponse(url=f"/staff/{user_id}", status_code=302)


@router.post("/staff/{user_id}/set-module-access")
def staff_set_module_access(
    request: Request,
    user_id: int,
    module_key: Annotated[str, Form()],
    access: Annotated[str, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from db_utils import org_structure_level

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/staff/{user_id}?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    try:
        if org_structure_level(telegram_id) != "full":
            return RedirectResponse(url=f"/staff/{user_id}?error=paid_only", status_code=302)
        from web.routes.org_structure import MODULE_LABELS
        if module_key not in MODULE_LABELS:
            return RedirectResponse(url=f"/staff/{user_id}", status_code=302)
        if access not in ("allow", "deny", "inherit"):
            access = "inherit"
        db = get_web_db(telegram_id, org_db)
        target_tg = _target_tg(db, user_id)
        if target_tg is not None:
            db.set_user_module_access(target_tg, module_key, access)
    except Exception as e:
        logging.error(f"staff_set_module_access error: {e}")

    return RedirectResponse(url=f"/staff/{user_id}#modules", status_code=302)
