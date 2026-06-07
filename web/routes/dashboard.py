import calendar as _cal
from datetime import date, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


def _group_plans_dash(plans_dash: list, limit: int = 6) -> list:
    """Group flat plan list by label (shop/seller) — one dict per entity."""
    groups: dict = {}
    for p in plans_dash:
        key = p["label"]
        if key not in groups:
            groups[key] = {"key": key, "plans": [], "target_type": p["plan_type"]}
        groups[key]["plans"].append(p)
    result = []
    for g in groups.values():
        g["avg_pct"] = sum(p["pct"] for p in g["plans"]) // len(g["plans"])
        result.append(g)
    result.sort(key=lambda g: g["avg_pct"])
    return result[:limit]


def _fmt(amount) -> str:
    try:
        v = int(float(amount or 0))
        return f"{v:,}".replace(',', '\u00a0') + "\u00a0₽"
    except Exception:
        return "0\u00a0₽"


def _growth(cur, prev) -> str | None:
    """Return '+12.3%' / '-5.1%' badge string, or None if no prev data."""
    try:
        cur, prev = float(cur or 0), float(prev or 0)
        if prev <= 0:
            return None
        pct = round((cur - prev) / prev * 100, 1)
        return f"+{pct}%" if pct >= 0 else f"{pct}%"
    except Exception:
        return None


def _add_forecast(plans_dash: list, today: date) -> None:
    """Add forecast_pct to each plan dict (linear extrapolation)."""
    days_in_month = _cal.monthrange(today.year, today.month)[1]
    week_elapsed = today.weekday() + 1   # Mon=1 … Sun=7
    for p in plans_dash:
        if p["pct"] >= 100:
            p["forecast_pct"] = None
            continue
        elapsed = week_elapsed if p["plan_type"] == "weekly" else today.day
        total   = 7           if p["plan_type"] == "weekly" else days_in_month
        actual, target = float(p["actual"]), float(p["target"])
        if elapsed > 0 and actual > 0 and target > 0:
            p["forecast_pct"] = min(int(actual / elapsed * total / target * 100), 300)
        else:
            p["forecast_pct"] = None


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
        "chart_labels": [], "chart_data": [], "chart_dates": [],
        "recent_sales": [], "shop_ranking": [],
        "seller_ranking": [], "low_stock": [],
        "plans_dash": [],
        "on_shift_today": [],
        "today_label": date.today().strftime('%d.%m.%Y'),
        "error": None,
        "user_tz": "Europe/Moscow",
        "today_vs_yesterday": None,
        "month_vs_prev": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        ctx["user_tz"] = tz or "Europe/Moscow"
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

        # ── Period comparisons ──────────────────────────────────────────────
        try:
            yesterday = today - timedelta(days=1)
            yesterday_s = db.get_sales_summary(
                start_date=yesterday.isoformat(), end_date=yesterday.isoformat()
            ) or (0, 0, 0, 0)
            ctx["today_vs_yesterday"] = _growth(today_s[2], yesterday_s[2])
        except Exception:
            pass

        try:
            prev_end   = today.replace(day=1) - timedelta(days=1)
            prev_start = prev_end.replace(day=1)
            prev_m_s   = db.get_sales_summary(
                start_date=prev_start.isoformat(), end_date=prev_end.isoformat()
            ) or (0, 0, 0, 0)
            ctx["month_vs_prev"] = _growth(month_s[2], prev_m_s[2])
        except Exception:
            pass

        # ── 7-day chart ─────────────────────────────────────────────────────
        labels, data, dates = [], [], []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            s = db.get_sales_summary(start_date=d.isoformat(), end_date=d.isoformat())
            labels.append(d.strftime('%d.%m'))
            data.append(int(float(s[2] or 0)) if s else 0)
            dates.append(d.isoformat())
        ctx["chart_labels"] = labels
        ctx["chart_data"] = data
        ctx["chart_dates"] = dates

        ctx["recent_sales"] = db.get_recent_sales(limit=10) or []
        ctx["shop_ranking"] = (db.get_shop_ranking(start_date=month_str, end_date=today_str) or [])[:5]
        ctx["seller_ranking"] = (db.get_sales_ranking(start_date=month_str, end_date=today_str) or [])[:5]
        ctx["product_count"] = len(db.get_all_products() or [])

        if is_admin:
            ctx["user_count"] = len(db.get_all_users() or [])
            try:
                uid = db.get_user_id(telegram_id)
                conn2 = db.get_connection()
                cur2 = conn2.cursor()
                threshold = 5
                if uid:
                    th_row = cur2.execute(
                        "SELECT stock_threshold FROM notification_settings WHERE user_id=?", (uid,)
                    ).fetchone()
                    if th_row and th_row[0] is not None:
                        threshold = int(th_row[0])
                cur2.execute("""
                    SELECT p.name, i.quantity, i.shop_name, i.product_id
                    FROM inventory i
                    JOIN products p ON i.product_id = p.id
                    WHERE i.quantity <= ?
                    ORDER BY i.quantity ASC
                    LIMIT 8
                """, (threshold,))
                ctx["low_stock"] = cur2.fetchall()
                conn2.close()
            except Exception:
                pass

            try:
                conn3 = db.get_connection()
                cur3 = conn3.cursor()
                cur3.execute("""
                    SELECT u.id, u.first_name, u.last_name, u.shop_name,
                           ws.start_time, ws.end_time
                    FROM work_schedule ws
                    JOIN users u ON ws.user_id = u.id
                    WHERE ws.work_date = ?
                    ORDER BY u.shop_name, u.first_name
                """, (today_str,))
                ctx["on_shift_today"] = cur3.fetchall()
                conn3.close()
            except Exception:
                ctx["on_shift_today"] = []

            try:
                raw = db.get_plans_progress(local_today=today) or []
                plans_dash = []
                for plan, actual, pct in raw:
                    metric = plan[2]
                    target = float(plan[3] or 1)
                    target_type = plan[4]
                    label = (plan[12] or "") + (" " + (plan[13] or "")[:1] + "." if plan[13] else "").strip() \
                        if target_type == "seller" else (plan[6] or "Весь орг")
                    period_raw = plan[1] or ""
                    period_label = "Неделя" if period_raw == "weekly" else ("Месяц" if period_raw == "monthly" else period_raw)
                    filter_type = plan[7] or "all"
                    filter_value = plan[8] or ""
                    plans_dash.append({
                        "id": plan[0],
                        "plan_type": period_raw,
                        "metric": metric,
                        "label": label,
                        "actual": float(actual or 0),
                        "target": target,
                        "pct": int(pct or 0),
                        "shop_name": plan[6] or "",
                        "target_type": target_type,
                        "period_label": period_label,
                        "filter_type": filter_type,
                        "filter_value": filter_value,
                        "forecast_pct": None,
                    })
                plans_dash.sort(key=lambda x: x["pct"] if x["pct"] < 100 else 10000)
                _add_forecast(plans_dash, today)
                ctx["plans_dash"] = _group_plans_dash(plans_dash, limit=6)
            except Exception:
                ctx["plans_dash"] = []
        else:
            ctx["user_count"] = 0
            try:
                raw = db.get_user_plans_progress(telegram_id, local_today=today) or []
                plans_dash = []
                for plan, actual, pct in raw:
                    metric = plan[2]
                    target = float(plan[3] or 1)
                    label = plan[6] or "Личный план"
                    period_raw = plan[1] or ""
                    period_label = "Неделя" if period_raw == "weekly" else ("Месяц" if period_raw == "monthly" else period_raw)
                    filter_type = plan[7] or "all"
                    filter_value = plan[8] or ""
                    plans_dash.append({
                        "id": plan[0],
                        "plan_type": period_raw,
                        "metric": metric,
                        "label": label,
                        "actual": float(actual or 0),
                        "target": target,
                        "pct": int(pct or 0),
                        "shop_name": plan[6] or "",
                        "target_type": plan[4] or "shop",
                        "period_label": period_label,
                        "filter_type": filter_type,
                        "filter_value": filter_value,
                        "forecast_pct": None,
                    })
                plans_dash.sort(key=lambda x: x["pct"])
                _add_forecast(plans_dash, today)
                ctx["plans_dash"] = _group_plans_dash(plans_dash, limit=6)
            except Exception:
                ctx["plans_dash"] = []

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "dashboard/index.html", ctx)
