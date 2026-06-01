from collections import defaultdict
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


def _period_dates(period: str, today):
    from datetime import timedelta
    if period == "today":
        return today.isoformat(), today.isoformat()
    if period == "week":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if period == "prev_month":
        month_start = today.replace(day=1)
        end = (month_start - timedelta(days=1))
        start = end.replace(day=1)
        return start.isoformat(), end.isoformat()
    # default: month
    return today.replace(day=1).isoformat(), today.isoformat()


def _aggregate(all_sales, group_by: str):
    """Aggregate sales rows by group_by key.
    sales cols: id[0] product_id[1] shop_name[2] qty[3] price[4]
                user_id[5] date[6] product_name[7] category[8] first_name[9] last_name[10]
    """
    groups: dict = {}

    for s in all_sales:
        qty = int(s[3] or 0)
        rev = float((s[3] or 0) * (s[4] or 0))

        if group_by == "product":
            key = s[1]
            if key not in groups:
                groups[key] = {"label": s[7] or "—", "sub": s[8] or "—", "qty": 0, "revenue": 0.0, "count": 0}
        elif group_by == "category":
            key = s[8] or "Без категории"
            if key not in groups:
                groups[key] = {"label": key, "sub": "", "qty": 0, "revenue": 0.0, "count": 0}
        elif group_by == "shop":
            key = s[2] or "—"
            if key not in groups:
                groups[key] = {"label": key, "sub": "", "qty": 0, "revenue": 0.0, "count": 0}
        else:  # seller
            key = s[5]
            fname = (s[9] or "").strip()
            lname = (s[10] or "").strip()
            name = f"{fname} {lname}".strip() or f"id{key}"
            if key not in groups:
                groups[key] = {"label": name, "sub": s[2] or "—", "qty": 0, "revenue": 0.0, "count": 0}
            else:
                # accumulate shops
                pass

        groups[key]["qty"] += qty
        groups[key]["revenue"] += rev
        groups[key]["count"] += 1

    result = sorted(groups.values(), key=lambda x: x["revenue"], reverse=True)
    max_rev = result[0]["revenue"] if result else 1.0
    for r in result:
        r["pct"] = round(r["revenue"] / max_rev * 100) if max_rev > 0 else 0
    return result


@router.get("/reports")
def reports_page(
    request: Request,
    period: str = "month",
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
    group_by: str = "product",
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
        "period": period, "shop": shop, "group_by": group_by,
        "date_from": date_from, "date_to": date_to,
        "shops": [], "summary": (0, 0, 0, 0),
        "groups": [], "all_sales": [], "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        if period == "custom" and date_from and date_to:
            df, dt = date_from, date_to
        else:
            df, dt = _period_dates(period, today)
            date_from, date_to = df, dt

        ctx["date_from"] = date_from
        ctx["date_to"] = date_to
        ctx["shops"] = db.get_all_shops() or []

        kwargs: dict = {"start_date": df, "end_date": dt}
        if shop:
            kwargs["shop_name"] = shop

        all_sales = db.get_sales_report(**kwargs) or []
        ctx["all_sales"] = all_sales
        ctx["summary"] = db.get_sales_summary(
            start_date=df, end_date=dt, shop_name=shop if shop else None
        ) or (0, 0, 0, 0)
        ctx["groups"] = _aggregate(all_sales, group_by)

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "reports/index.html", ctx
    )
