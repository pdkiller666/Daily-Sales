import logging
import calendar as _cal
from datetime import date
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from typing import Annotated

router = APIRouter()

MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_SYSTEM_SHOPS = ("Системный", "System")


def _adjacent_month(year: int, month: int, delta: int):
    total = (year - 1) * 12 + (month - 1) + delta
    return (total // 12 + 1, total % 12 + 1)


def _build_cal_grid(year: int, month: int):
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
    return cal_grid


def _redirect_back(user_id: int, year: int, month: int, anchor: str = "") -> RedirectResponse:
    url = f"/schedule?user_id={user_id}&year={year}&month={month}"
    if anchor:
        url += anchor
    return RedirectResponse(url=url, status_code=302)


@router.get("/schedule")
def schedule_page(
    request: Request,
    user_id: int = 0,
    year: int = 0,
    month: int = 0,
    msg: str = "",
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/subscription?msg=team_locked", status_code=302)
    org_db = user.get("org_db")

    today = date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month

    prev_y, prev_m = _adjacent_month(year, month, -1)
    next_y, next_m = _adjacent_month(year, month, 1)

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": True,
        "year": year, "month": month,
        "month_name": MONTH_NAMES.get(month, str(month)),
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "weekday_names": WEEKDAY_NAMES,
        "selected_user_id": user_id,
        "selected_user": None,
        "staff_list": [],
        "cal_grid": [],
        "work_days": set(),
        "work_day_times": {},
        "templates": {},
        "absence_map": {},
        "absence_days_in_schedule": 0,
        "today_day": today.day if (today.year == year and today.month == month) else 0,
        "msg": msg,
        "error": None,
        "csrf_token": "",
    }

    from web.auth import get_csrf_token
    ctx["csrf_token"] = get_csrf_token(request)

    try:
        db = get_web_db(telegram_id, org_db)

        all_users = db.get_all_users() or []
        staff_list = []
        for u in all_users:
            # Skip system pseudo-users
            if u[8] in _SYSTEM_SHOPS:
                continue
            staff_list.append({
                "id": u[0],
                "first_name": u[2] or "",
                "last_name": u[3] or "",
                "shop_name": u[8] or "",
                "telegram_id": u[1],
            })
        ctx["staff_list"] = staff_list

        if user_id:
            sel = next((s for s in staff_list if s["id"] == user_id), None)
            ctx["selected_user"] = sel

            work_dates = db.get_work_schedule(user_id, year, month)
            work_days: set[int] = set()
            work_day_times: dict[int, tuple] = {}
            for date_str in work_dates:
                try:
                    day_num = int(date_str[8:10])
                    work_days.add(day_num)
                    st, et = db.get_work_day_time(user_id, date_str)
                    if st or et:
                        work_day_times[day_num] = (st or "", et or "")
                except Exception as e:
                    logging.error(f"schedule_page day parse error: {e}")

            ctx["work_days"] = work_days
            ctx["work_day_times"] = work_day_times
            ctx["cal_grid"] = _build_cal_grid(year, month)
            try:
                _abs_raw = db.get_absence_days_map(year, month, user_id)
                ctx["absence_map"] = _abs_raw.get(user_id, {})
            except Exception:
                ctx["absence_map"] = {}

            # Count work days that overlap with approved absences
            try:
                approved_absence_days = {
                    day_num for day_num, info in ctx["absence_map"].items()
                    if info.get("status") == "approved"
                }
                ctx["absence_days_in_schedule"] = len(work_days & approved_absence_days)
            except Exception:
                ctx["absence_days_in_schedule"] = 0

            raw_templates = db.get_shift_templates(user_id) or {}
            templates: dict[int, dict] = {}
            for wd in range(7):
                val = raw_templates.get(wd)
                if val and val[0]:
                    templates[wd] = {"start": val[0], "end": val[1] or "", "is_off": False}
                elif wd in raw_templates:
                    templates[wd] = {"start": "", "end": "", "is_off": True}
                else:
                    templates[wd] = {"start": "", "end": "", "is_off": False}
            ctx["templates"] = templates

    except Exception as exc:
        logging.error(f"schedule_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "schedule/index.html", ctx
    )


@router.post("/schedule/toggle_day")
def schedule_toggle_day(
    request: Request,
    user_id: Annotated[int, Form()],
    work_date: Annotated[str, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect_back(user_id, year, month)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.toggle_work_day(user_id, work_date, marked_by=telegram_id)
    except Exception as e:
        logging.error(f"schedule_toggle_day error: {e}")

    return _redirect_back(user_id, year, month)


@router.post("/schedule/set_time")
def schedule_set_time(
    request: Request,
    user_id: Annotated[int, Form()],
    work_date: Annotated[str, Form()],
    start_time: Annotated[str, Form()],
    end_time: Annotated[str, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect_back(user_id, year, month)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        st = start_time or None
        et = end_time or None
        # INSERT OR IGNORE ensures the day row exists (handles both new days and
        # days already marked via toggle_work_day with no time).
        db.add_work_day(user_id, work_date, start_time=st, end_time=et,
                        marked_by=telegram_id)
        # Always UPDATE afterwards so the time is set even if the INSERT was ignored.
        db.set_work_day_time(user_id, work_date, st, et)
    except Exception as e:
        logging.error(f"schedule_set_time error: {e}")

    return _redirect_back(user_id, year, month)


@router.post("/schedule/remove_day")
def schedule_remove_day(
    request: Request,
    user_id: Annotated[int, Form()],
    work_date: Annotated[str, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect_back(user_id, year, month)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.remove_work_day(user_id, work_date)
    except Exception as e:
        logging.error(f"schedule_remove_day error: {e}")

    return _redirect_back(user_id, year, month)


@router.post("/schedule/fill_month")
def schedule_fill_month(
    request: Request,
    user_id: Annotated[int, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/schedule?user_id={user_id}&year={year}&month={month}", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    added = 0
    try:
        db = get_web_db(telegram_id, org_db)
        added = db.fill_month_by_template(user_id, year, month, marked_by=telegram_id)
    except Exception as e:
        logging.error(f"schedule_fill_month error: {e}")

    return RedirectResponse(
        url=f"/schedule?user_id={user_id}&year={year}&month={month}&msg=added:{added}",
        status_code=302,
    )


@router.post("/schedule/set_template_bulk")
async def schedule_set_template_bulk(request: Request):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    form = await request.form()
    csrf_token = str(form.get("csrf_token", ""))
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/schedule", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    try:
        user_id = int(form.get("user_id", 0))
        year = int(form.get("year", 0))
        month = int(form.get("month", 0))
    except (ValueError, TypeError):
        return RedirectResponse(url="/schedule", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        for wd in range(7):
            start = str(form.get(f"start_{wd}", "") or "")
            end = str(form.get(f"end_{wd}", "") or "")
            is_off = str(form.get(f"off_{wd}", "0"))
            if is_off == "1":
                db.set_shift_template(user_id, wd, None, None)
            else:
                db.set_shift_template(user_id, wd, start or None, end or None)
    except Exception as e:
        logging.error(f"schedule_set_template_bulk error: {e}")

    return _redirect_back(user_id, year, month, anchor="#templates")


@router.post("/schedule/set_template")
def schedule_set_template(
    request: Request,
    user_id: Annotated[int, Form()],
    weekday: Annotated[int, Form()],
    start_time: Annotated[str, Form()] = "",
    end_time: Annotated[str, Form()] = "",
    is_off: Annotated[str, Form()] = "",
    year: Annotated[int, Form()] = 0,
    month: Annotated[int, Form()] = 0,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect_back(user_id, year, month, "#templates")
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        if is_off == "1":
            db.set_shift_template(user_id, weekday, None, None)
        else:
            db.set_shift_template(user_id, weekday,
                                  start_time or None,
                                  end_time or None)
    except Exception as e:
        logging.error(f"schedule_set_template error: {e}")

    return _redirect_back(user_id, year, month, anchor="#templates")
