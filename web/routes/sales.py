from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
PAGE_SIZE = 50


def _summary_empty():
    return (0, 0, 0, 0)


@router.get("/sales")
def sales_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
    page: int = 1,
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
        "sales": [], "shops": [],
        "date_from": date_from, "date_to": date_to, "selected_shop": shop,
        "page": 1, "total_pages": 1, "total_count": 0,
        "summary": _summary_empty(), "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()
        month_start = today.replace(day=1)

        if not date_from:
            date_from = month_start.isoformat()
        if not date_to:
            date_to = today.isoformat()
        ctx["date_from"] = date_from
        ctx["date_to"] = date_to

        ctx["shops"] = db.get_all_shops() or []

        kwargs: dict = {"start_date": date_from, "end_date": date_to}
        if shop:
            kwargs["shop_name"] = shop

        all_sales = db.get_sales_report(**kwargs) or []

        total = len(all_sales)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PAGE_SIZE

        ctx["sales"] = all_sales[start : start + PAGE_SIZE]
        ctx["total_count"] = total
        ctx["total_pages"] = total_pages
        ctx["page"] = page
        ctx["summary"] = db.get_sales_summary(
            start_date=date_from, end_date=date_to,
            shop_name=shop if shop else None
        ) or _summary_empty()

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "sales/index.html", ctx
    )
