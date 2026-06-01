from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

MEDALS = ["🥇", "🥈", "🥉"]


def _period_dates(period: str, today):
    from datetime import timedelta
    if period == "week":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if period == "prev_month":
        ms = today.replace(day=1)
        end = ms - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()
    if period == "all":
        return None, None
    # month
    return today.replace(day=1).isoformat(), today.isoformat()


@router.get("/rankings")
def rankings_page(
    request: Request,
    tab: str = "sellers",
    period: str = "month",
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
        "tab": tab, "period": period,
        "ranking": [], "medals": MEDALS, "error": None,
        "date_from": "", "date_to": "",
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        df, dt = _period_dates(period, today)
        ctx["date_from"] = df or ""
        ctx["date_to"] = dt or ""

        kwargs: dict = {}
        if df:
            kwargs["start_date"] = df
        if dt:
            kwargs["end_date"] = dt

        if tab == "shops":
            raw = db.get_shop_ranking(**kwargs) or []
            # shop_name[0] total_sold[1] total_revenue[2] active_sellers[3] total_sales[4]
            max_rev = float(raw[0][2]) if raw else 1.0
            ranking = []
            for i, r in enumerate(raw):
                rev = float(r[2] or 0)
                ranking.append({
                    "pos": i + 1, "medal": MEDALS[i] if i < 3 else "",
                    "label": r[0] or "—", "sub": f"{r[3]} продавцов",
                    "qty": int(r[1] or 0), "revenue": rev,
                    "count": int(r[4] or 0), "earnings": float(r[5] or 0),
                    "pct": round(rev / max_rev * 100) if max_rev else 0,
                })
            ctx["ranking"] = ranking

        elif tab == "cities":
            raw = db.get_city_ranking(**kwargs) or []
            # city[0] total_sold[1] total_revenue[2] active_sellers[3] total_sales[4] total_earnings[5]
            max_rev = float(raw[0][2]) if raw else 1.0
            ranking = []
            for i, r in enumerate(raw):
                if not r[0]:
                    continue
                rev = float(r[2] or 0)
                ranking.append({
                    "pos": i + 1, "medal": MEDALS[i] if i < 3 else "",
                    "label": r[0], "sub": f"{r[3]} продавцов",
                    "qty": int(r[1] or 0), "revenue": rev,
                    "count": int(r[4] or 0), "earnings": float(r[5] or 0),
                    "pct": round(rev / max_rev * 100) if max_rev else 0,
                })
            ctx["ranking"] = ranking

        else:  # sellers
            raw = db.get_sales_ranking(**kwargs) or []
            # first_name[0] last_name[1] shop_name[2] total_sold[3] total_revenue[4]
            # total_sales[5] total_earnings[6] user_db_id[7] username[8]
            max_rev = float(raw[0][4]) if raw else 1.0
            ranking = []
            for i, r in enumerate(raw):
                fname = (r[0] or "").strip()
                lname = (r[1] or "").strip()
                name = f"{fname} {lname}".strip() or f"@{r[8]}" if r[8] else "—"
                rev = float(r[4] or 0)
                ranking.append({
                    "pos": i + 1, "medal": MEDALS[i] if i < 3 else "",
                    "label": name, "sub": r[2] or "—",
                    "username": r[8] or "",
                    "qty": int(r[3] or 0), "revenue": rev,
                    "count": int(r[5] or 0), "earnings": float(r[6] or 0),
                    "pct": round(rev / max_rev * 100) if max_rev else 0,
                })
            ctx["ranking"] = ranking

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "rankings/index.html", ctx
    )
