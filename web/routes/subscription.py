import sqlite3
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

SHOP_BOT_DB = "data/shop_bot.db"

PLAN_ORDER = ["Бесплатный", "Базовый", "Стандарт", "Премиум"]
PLAN_PRICES = {
    "Бесплатный": 0,
    "Базовый": 500,
    "Стандарт": 1200,
    "Премиум": 4000,
}
PLAN_LABELS = {
    "Бесплатный": "Бесплатный",
    "Базовый": "Базовый",
    "Стандарт": "Стандарт",
    "Премиум": "Премиум",
}


def _get_user_id_in_shop_bot(telegram_id: int) -> int | None:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_all_plans() -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        rows = conn.execute(
            """SELECT name, price, duration_days, max_products, max_shops,
                      max_sales_per_month, can_export_reports, can_view_analytics,
                      can_use_notifications, can_use_integrations
               FROM subscription_plans WHERE is_active = 1
               ORDER BY price ASC"""
        ).fetchall()
        conn.close()
        plans = []
        for row in rows:
            name, price, days, mp, ms, msal, exp, anal, notif, integ = row

            def _lim(v):
                return "∞" if v == -1 else str(v)

            plans.append({
                "name": name,
                "label": PLAN_LABELS.get(name, name),
                "price": int(price),
                "price_fmt": (
                    f"{int(price):,}".replace(",", "\u00a0") + "\u00a0₽"
                    if int(price) > 0 else "Бесплатно"
                ),
                "duration_days": days,
                "duration_label": (
                    "навсегда" if days == 0
                    else ("30 дней" if days == 30 else f"{days} дней")
                ),
                "max_products": _lim(mp),
                "max_shops": _lim(ms),
                "max_sales": _lim(msal),
                "can_export": bool(exp),
                "can_analytics": bool(anal),
                "can_notifications": bool(notif),
                "can_integrations": bool(integ),
            })
        return plans
    except Exception:
        return []


def _get_user_subscription(user_id: int) -> dict | None:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        row = conn.execute(
            """SELECT plan_type, start_date, end_date, is_trial
               FROM subscriptions
               WHERE user_id = ? AND datetime(end_date) > datetime('now')
               ORDER BY end_date DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "plan_type": row[0],
            "start_date": str(row[1] or "")[:10],
            "end_date": str(row[2] or "")[:10],
            "is_trial": bool(row[3]),
        }
    except Exception:
        return None


def _get_payment_history(user_id: int, limit: int = 10) -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        rows = conn.execute(
            """SELECT id, plan_type, amount, status, created_at, processed_at
               FROM payment_requests
               WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        conn.close()
        STATUS_LABELS = {
            "pending": ("⏳ Ожидает", "text-amber-600 bg-amber-50"),
            "approved": ("✅ Подтверждено", "text-emerald-600 bg-emerald-50"),
            "rejected": ("❌ Отклонено", "text-red-600 bg-red-50"),
        }
        result = []
        for row in rows:
            st = row[3] or "pending"
            label, css = STATUS_LABELS.get(st, (st, "text-slate-500 bg-slate-50"))
            result.append({
                "id": row[0],
                "plan_type": row[1],
                "plan_label": PLAN_LABELS.get(row[1] or "", row[1] or "—"),
                "amount": int(row[2] or 0),
                "status": st,
                "status_label": label,
                "status_css": css,
                "created_at": str(row[4] or "")[:16].replace("T", " "),
                "processed_at": str(row[5] or "")[:16].replace("T", " ") if row[5] else "—",
            })
        return result
    except Exception:
        return []


def _has_pending_request(user_id: int) -> bool:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        row = conn.execute(
            "SELECT id FROM payment_requests WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


@router.get("/subscription")
def subscription_page(request: Request, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)

    subscription = _get_user_subscription(user_id) if user_id else None
    history = _get_payment_history(user_id) if user_id else []
    has_pending = _has_pending_request(user_id) if user_id else False
    plans = _get_all_plans()
    current_plan = subscription["plan_type"] if subscription else "Бесплатный"

    # Determine which plans are upgrades
    try:
        current_idx = PLAN_ORDER.index(current_plan)
    except ValueError:
        current_idx = 0
    for p in plans:
        try:
            p["is_current"] = p["name"] == current_plan
            p["is_upgrade"] = PLAN_ORDER.index(p["name"]) > current_idx
            p["is_downgrade"] = PLAN_ORDER.index(p["name"]) < current_idx
        except ValueError:
            p["is_current"] = False
            p["is_upgrade"] = False
            p["is_downgrade"] = False

    return request.app.state.templates.TemplateResponse(
        request,
        "subscription/index.html",
        {
            "request": request,
            "user": user,
            "subscription": subscription,
            "current_plan": current_plan,
            "plans": plans,
            "history": history,
            "has_pending": has_pending,
            "csrf_token": get_csrf_token(request),
            "msg": msg,
        },
    )


@router.post("/subscription/request")
async def subscription_request(
    request: Request,
    plan_type: str = Form(...),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    if _has_pending_request(user_id):
        return RedirectResponse(url="/subscription?msg=already_pending", status_code=303)

    if plan_type not in PLAN_PRICES:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    amount = PLAN_PRICES[plan_type]
    if amount == 0:
        return RedirectResponse(url="/subscription?msg=free_plan", status_code=303)

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        conn.execute(
            """INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id)
               VALUES (?, ?, ?, 'web_request')""",
            (user_id, plan_type, amount),
        )
        conn.commit()
        conn.close()
        return RedirectResponse(url="/subscription?msg=request_sent", status_code=303)
    except Exception as exc:
        import logging
        logging.error(f"subscription_request error: {exc}")
        return RedirectResponse(url="/subscription?msg=error", status_code=303)
