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
        "today_day": today.day if (today.year == year and today.month == month) else 0,
        "msg": msg,
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        all_users = db.get_all_users() or []
        staff_list = []
        for u in all_users:
            if u[8] in ("Системный", "System", None) and not u[8]:
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
            work_days = set()
            work_day_times = {}
            for date_str in work_dates:
                try:
                    day_num = int(date_str[8:10])
                    work_days.add(day_num)
                    st, et = db.get_work_day_time(user_id, date_str)
                    if st or et:
                        work_day_times[day_num] = (st or "", et or "")
                except Exception:
                    pass

            ctx["work_days"] = work_days
            ctx["work_day_times"] = work_day_times
            ctx["cal_grid"] = _build_cal_grid(year, month)

            raw_templates = db.get_shift_templates(user_id) or {}
            templates = {}
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
        ctx["error"] = str(exc)

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
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.toggle_work_day(user_id, work_date, marked_by=telegram_id)
    except Exception:
        pass

    return RedirectResponse(
        url=f"/schedule?user_id={user_id}&year={year}&month={month}",
        status_code=302,
    )


@router.post("/schedule/set_time")
def schedule_set_time(
    request: Request,
    user_id: Annotated[int, Form()],
    work_date: Annotated[str, Form()],
    start_time: Annotated[str, Form()],
    end_time: Annotated[str, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        existing = db.get_work_day_time(user_id, work_date)
        if existing[0] is None and existing[1] is None:
            db.add_work_day(user_id, work_date,
                            start_time=start_time or None,
                            end_time=end_time or None,
                            marked_by=telegram_id)
        else:
            db.set_work_day_time(user_id, work_date,
                                 start_time or None, end_time or None)
    except Exception:
        pass

    return RedirectResponse(
        url=f"/schedule?user_id={user_id}&year={year}&month={month}",
        status_code=302,
    )


@router.post("/schedule/remove_day")
def schedule_remove_day(
    request: Request,
    user_id: Annotated[int, Form()],
    work_date: Annotated[str, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.remove_work_day(user_id, work_date)
    except Exception:
        pass

    return RedirectResponse(
        url=f"/schedule?user_id={user_id}&year={year}&month={month}",
        status_code=302,
    )


@router.post("/schedule/fill_month")
def schedule_fill_month(
    request: Request,
    user_id: Annotated[int, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    added = 0
    try:
        db = get_web_db(telegram_id, org_db)
        added = db.fill_month_by_template(user_id, year, month, marked_by=telegram_id)
    except Exception:
        pass

    return RedirectResponse(
        url=f"/schedule?user_id={user_id}&year={year}&month={month}&msg=added:{added}",
        status_code=302,
    )


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
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
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
    except Exception:
        pass

    return RedirectResponse(
        url=f"/schedule?user_id={user_id}&year={year}&month={month}#templates",
        status_code=302,
    )
