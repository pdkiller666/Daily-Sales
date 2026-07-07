import calendar as _cal
import time as _time
from datetime import date, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from timezone_utils import DEFAULT_TZ

router = APIRouter()

# ── TTL кэш для rankings (60 с) — снижает нагрузку при частых переходах ──────
_RANK_CACHE: dict = {}
_RANK_TTL = 60  # секунд


def _rank_cached(key: str, fn, *args, **kwargs):
    """Return cached value if fresh, else call fn(*args, **kwargs) and cache."""
    now = _time.monotonic()
    entry = _RANK_CACHE.get(key)
    if entry and now - entry[0] < _RANK_TTL:
        return entry[1]
    result = fn(*args, **kwargs)
    _RANK_CACHE[key] = (now, result)
    return result


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


def _fmt(amount, symbol: str = '₽') -> str:
    try:
        v = int(float(amount or 0))
        return f"{v:,}".replace(',', '\u00a0') + f"\u00a0{symbol}"
    except Exception:
        return f"0\u00a0{symbol}"


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


_GATE_MSGS = {
    "module_analytics_required":      ("📊 Модуль «Аналитика» не подключён",        "Подключите модуль в разделе Подписка → Модули, чтобы использовать отчёты."),
    "module_plans_required":          ("🎯 Модуль «Планы и мотивация» не подключён", "Подключите модуль в разделе Подписка → Модули, чтобы управлять планами и мотивацией."),
    "module_team_required":           ("👥 Модуль «Команда» не подключён",           "Подключите модуль в разделе Подписка → Модули, чтобы использовать расписание, зарплаты и отсутствия."),
    "module_notifications_required":  ("🔔 Модуль «Уведомления» не подключён",       "Подключите модуль в разделе Подписка → Модули, чтобы отправлять уведомления."),
    "ext_heatmap_required":           ("🌡️ Расширение «Тепловая карта» не подключено",  "Подключите расширение в разделе Подписка → Модули."),
    "ext_abc_required":               ("🔤 Расширение «ABC-анализ» не подключено",       "Подключите расширение в разделе Подписка → Модули."),
    "ext_turnover_required":          ("🔄 Расширение «Оборачиваемость» не подключено",  "Подключите расширение в разделе Подписка → Модули."),
    "ext_dead_stock_required":        ("📦 Расширение «Залежалые товары» не подключено", "Подключите расширение в разделе Подписка → Модули."),
    "ext_contests_required":          ("🏆 Расширение «Конкурсы» не подключено",         "Подключите расширение в разделе Подписка → Модули."),
    "ext_salary_export_required":     ("📥 Расширение «Экспорт зарплат» не подключено",  "Подключите расширение в разделе Подписка → Модули."),
    "services_locked":                ("🔒 Модуль «Услуги» не подключён",                "Этот раздел доступен по подписке. Владелец организации может подключить модуль в разделе Подписка → Модули."),
}


@router.get("/dashboard")
def dashboard(request: Request, msg: str = ""):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user['sub'])
    org_db = user.get('org_db')
    role = user.get('role', 'user')
    is_admin = role in ('owner', 'admin', 'super_admin')

    gate_title, gate_text = _GATE_MSGS.get(msg, (None, None))

    apk_new_version = ""
    apk_release_url = ""
    try:
        import sqlite3 as _sqlite3
        _shop_db = "data/shop_bot.db"
        _aconn = _sqlite3.connect(_shop_db)
        try:
            _row_ver = _aconn.execute(
                "SELECT value FROM payment_settings WHERE key='apk_latest_version'"
            ).fetchone()
            _row_url = _aconn.execute(
                "SELECT value FROM payment_settings WHERE key='apk_release_url'"
            ).fetchone()
        finally:
            _aconn.close()
        apk_new_version = _row_ver[0].strip() if _row_ver and _row_ver[0] else ""
        apk_release_url = _row_url[0].strip() if _row_url and _row_url[0] else "/download/android"
    except Exception:
        pass

    _csym = getattr(request.state, 'currency_symbol', '₽')
    ctx: dict = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "gate_title": gate_title,
        "gate_text": gate_text,
        "today_sales": 0, "today_revenue": f"0\u00a0{_csym}",
        "month_sales": 0, "month_revenue": f"0\u00a0{_csym}",
        "month_returns": {"count": 0, "total_qty": 0, "total_amount": 0.0},
        "user_count": 0, "product_count": 0,
        "chart_labels": [], "chart_data": [], "chart_dates": [],
        "recent_sales": [], "shop_ranking": [],
        "seller_ranking": [], "low_stock": [],
        "plans_dash": [],
        "on_shift_today": [],
        "today_label": date.today().strftime('%d.%m.%Y'),
        "error": None,
        "user_tz": DEFAULT_TZ,
        "today_vs_yesterday": None,
        "month_vs_prev": None,
        "apk_new_version": apk_new_version,
        "apk_release_url": apk_release_url,
        "user_shop": "",
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        ctx["user_tz"] = tz or DEFAULT_TZ
        today = get_current_user_time(tz).date()
        month_start = today.replace(day=1)

        today_str = today.isoformat()
        month_str = month_start.isoformat()
        ctx["today_label"] = today.strftime('%d.%m.%Y')
        ctx["today_iso"] = today_str
        ctx["curr_year"] = today.year
        ctx["curr_month"] = today.month

        if not is_admin:
            try:
                from db_utils import get_user_org_scope
                _stype, _svals = get_user_org_scope(telegram_id)
                if _stype == "shop" and _svals and len(_svals) == 1:
                    ctx["user_shop"] = _svals[0]
            except Exception:
                pass

        today_s = db.get_sales_summary(start_date=today_str, end_date=today_str) or (0, 0, 0, 0)
        month_s = db.get_sales_summary(start_date=month_str, end_date=today_str) or (0, 0, 0, 0)

        ctx["today_sales"] = int(today_s[0] or 0)
        ctx["today_revenue"] = _fmt(today_s[2], _csym)
        ctx["month_sales"] = int(month_s[0] or 0)
        ctx["month_revenue"] = _fmt(month_s[2], _csym)

        # ── Returns summary (current month) ────────────────────────────────
        try:
            _ret_kw: dict = {"start_date": month_str, "end_date": today_str}
            if ctx.get("user_shop"):
                _ret_kw["shop_name"] = ctx["user_shop"]
            ctx["month_returns"] = db.get_returns_summary(**_ret_kw) or {"count": 0, "total_qty": 0, "total_amount": 0.0}
        except Exception:
            ctx["month_returns"] = {"count": 0, "total_qty": 0, "total_amount": 0.0}

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

        # ── 30-day chart — один GROUP BY вместо 30 отдельных запросов ──────
        chart_start = (today - timedelta(days=29)).isoformat()
        daily = db.get_daily_chart_data(chart_start, today_str)
        labels, data, dates = [], [], []
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            d_str = d.isoformat()
            labels.append(d.strftime('%d.%m'))
            data.append(int(daily.get(d_str, 0)))
            dates.append(d_str)
        ctx["chart_labels"] = labels
        ctx["chart_data"] = data
        ctx["chart_dates"] = dates

        ctx["recent_sales"] = db.get_recent_sales(limit=10) or []
        _rk = org_db or "default"
        ctx["shop_ranking"] = (_rank_cached(
            f"shop:{_rk}:{month_str}", db.get_shop_ranking,
            start_date=month_str, end_date=today_str) or [])[:5]
        ctx["seller_ranking"] = (_rank_cached(
            f"seller:{_rk}:{month_str}", db.get_sales_ranking,
            start_date=month_str, end_date=today_str) or [])[:5]
        ctx["product_count"] = db.get_product_count()

        if is_admin:
            ctx["user_count"] = db.get_user_count()
            try:
                uid = db.get_user_id(telegram_id)
                conn2 = db.get_connection()
                try:
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
                finally:
                    conn2.close()
            except Exception:
                pass

            try:
                conn3 = db.get_connection()
                try:
                    cur3 = conn3.cursor()
                    cur3.execute("""
                        SELECT u.id, u.first_name, u.last_name, u.shop_name,
                               ws.start_time, ws.end_time
                        FROM work_schedule ws
                        JOIN users u ON ws.user_id = u.id
                        WHERE ws.work_date = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM absence_records ar
                              WHERE ar.user_id = u.id
                                AND ar.status = 'approved'
                                AND ar.start_date <= ?
                                AND ar.end_date >= ?
                          )
                        ORDER BY u.shop_name, u.first_name
                    """, (today_str, today_str, today_str))
                    ctx["on_shift_today"] = cur3.fetchall()
                finally:
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
        import logging
        logging.error("dashboard error tg=%s: %s", telegram_id, exc)
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    # ── AI Quota widget ──────────────────────────────────────────────────────────
    ctx["ai_quota_widget"] = None
    try:
        from web.rate_store import get_ai_enabled, get_ai_daily_usage
        from web.routes.ai_routes import _get_limits
        if get_ai_enabled() and telegram_id > 0:
            _ai_limit, _ai_is_high = _get_limits(telegram_id)
            if _ai_limit > 0:
                _ai_used = get_ai_daily_usage(telegram_id)
                ctx["ai_quota_widget"] = {
                    "used": _ai_used,
                    "limit": _ai_limit,
                    "remaining": max(0, _ai_limit - _ai_used),
                    "pct": min(100, int(_ai_used / _ai_limit * 100)) if _ai_limit > 0 else 0,
                }
    except Exception:
        pass

    # ── AI Network Insights widget (owners with ai_network_insights + ≥2 orgs) ──
    ctx["ai_network_widget"] = None
    if role == "owner":
        try:
            from billing_utils import has_extension as _has_ext
            if _has_ext(telegram_id, "ai_network_insights"):
                from web.routes.ai_insights import _get_owner_orgs, _get_cached_insights
                _orgs = _get_owner_orgs(telegram_id)
                if len(_orgs) >= 2:
                    _cached = _get_cached_insights(telegram_id)
                    ctx["ai_network_widget"] = {
                        "org_count": len(_orgs),
                        "cached": _cached,
                    }
        except Exception:
            pass

    return request.app.state.templates.TemplateResponse(request, "dashboard/index.html", ctx)
