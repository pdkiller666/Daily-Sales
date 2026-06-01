from datetime import date, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


def _fmt(amount) -> str:
    try:
        v = int(float(amount or 0))
        return f"{v:,}".replace(',', '\u00a0') + "\u00a0₽"
    except Exception:
        return "0\u00a0₽"


@router.get("/dashboard")
def dashboard(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user['sub'])
    org_db = user.get('org_db')
    role = user.get('role', 'user')
    is_admin = role in ('owner', 'admin', 'super_admin')

    ctx: dict = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "today_sales": 0, "today_revenue": "0\u00a0₽",
        "month_sales": 0, "month_revenue": "0\u00a0₽",
        "user_count": 0, "product_count": 0,
        "chart_labels": [], "chart_data": [],
        "recent_sales": [], "shop_ranking": [],
        "seller_ranking": [], "low_stock": [],
        "today_label": date.today().strftime('%d.%m.%Y'),
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()
        month_start = today.replace(day=1)

        today_str = today.isoformat()
        month_str = month_start.isoformat()
        ctx["today_label"] = today.strftime('%d.%m.%Y')

        today_s = db.get_sales_summary(start_date=today_str, end_date=today_str) or (0, 0, 0, 0)
        month_s = db.get_sales_summary(start_date=month_str, end_date=today_str) or (0, 0, 0, 0)

        ctx["today_sales"] = int(today_s[0] or 0)
        ctx["today_revenue"] = _fmt(today_s[2])
        ctx["month_sales"] = int(month_s[0] or 0)
        ctx["month_revenue"] = _fmt(month_s[2])

        labels, data = [], []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            s = db.get_sales_summary(start_date=d.isoformat(), end_date=d.isoformat())
            labels.append(d.strftime('%d.%m'))
            data.append(int(float(s[2] or 0)) if s else 0)
        ctx["chart_labels"] = labels
        ctx["chart_data"] = data

        ctx["recent_sales"] = db.get_recent_sales(limit=10) or []
        ctx["shop_ranking"] = (db.get_shop_ranking(start_date=month_str, end_date=today_str) or [])[:5]
        ctx["seller_ranking"] = (db.get_sales_ranking(start_date=month_str, end_date=today_str) or [])[:5]
        ctx["product_count"] = len(db.get_all_products() or [])

        if is_admin:
            ctx["user_count"] = len(db.get_all_users() or [])
            try:
                uid = db.get_user_id(telegram_id)
                if uid:
                    ctx["low_stock"] = (db.get_low_stock_items_for_user(uid) or [])[:6]
            except Exception:
                pass
        else:
            uid = db.get_user_id(telegram_id)
            ctx["user_count"] = 0

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(request, "dashboard/index.html", ctx)
