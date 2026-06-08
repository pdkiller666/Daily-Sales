import json
import logging
import os
import sqlite3
import urllib.request
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

SHOP_BOT_DB = "data/shop_bot.db"


def _notify_admin_new_request(plan_type: str, amount: int, user_display: str, telegram_id: int) -> None:
    """Уведомить супер-администратора в Telegram о новой заявке (web-запрос)."""
    token = os.environ.get("BOT_TOKEN", "")
    admin_id = os.environ.get("ADMIN_CHAT_ID", "")
    if not token or not admin_id:
        return
    label = _plan_type_label(plan_type)
    text = (
        "🔔 <b>Новая заявка из веб-кабинета!</b>\n\n"
        f"👤 <b>Пользователь:</b> {user_display}\n"
        f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
        f"📦 <b>Позиция:</b> {label}\n"
        f"💰 <b>Сумма:</b> {amount}\u00a0₽\n\n"
        "⏰ Заявка ожидает рассмотрения в боте."
    )
    payload = json.dumps({"chat_id": admin_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        logging.warning("_notify_admin_new_request: %s", exc)

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

# ── Friendly labels for module/bundle plan_types in payment history ────────────
def _plan_type_label(plan_type: str) -> str:
    if not plan_type:
        return "—"
    if plan_type.startswith("module_"):
        key = plan_type[7:]
        return f"Модуль: {key}"
    if plan_type.startswith("bundle_"):
        key = plan_type[7:]
        return f"Пакет: {key}"
    if plan_type.startswith("addon_"):
        parts = plan_type.split("_")
        labels = {"shops": "Доп. магазин", "products": "Доп. товары"}
        return labels.get(parts[1] if len(parts) > 1 else "", plan_type)
    return PLAN_LABELS.get(plan_type, plan_type)


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


def _get_all_billing_modules() -> list[dict]:
    """Все активные модули биллинга для клиентского маркетплейса."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        rows = conn.execute(
            """SELECT key, name, icon, description, price_monthly, features_json
               FROM billing_modules WHERE is_active=1
               ORDER BY sort_order, id"""
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            key, name, icon, desc, price, feats_json = r
            try:
                features = json.loads(feats_json or "[]")
            except Exception:
                features = []
            result.append({
                "key": key,
                "name": name,
                "icon": icon or "🔧",
                "description": desc or "",
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
                "features": features,
            })
        return result
    except Exception:
        return []


def _get_all_billing_bundles() -> list[dict]:
    """Все активные пакеты биллинга для клиентского маркетплейса."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        rows = conn.execute(
            """SELECT key, name, icon, description, includes_json, price_monthly
               FROM billing_bundles WHERE is_active=1
               ORDER BY sort_order, id"""
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            key, name, icon, desc, inc_json, price = r
            try:
                includes = json.loads(inc_json or '{"modules":[],"extensions":[]}')
            except Exception:
                includes = {"modules": [], "extensions": []}
            result.append({
                "key": key,
                "name": name,
                "icon": icon or "📦",
                "description": desc or "",
                "includes": includes,
                "modules_count": len(includes.get("modules", [])),
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
            })
        return result
    except Exception:
        return []


def _get_user_active_module_subs(telegram_id: int) -> dict:
    """Возвращает {item_key: {end_date, item_type}} для активных модульных подписок пользователя."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        rows = conn.execute(
            """SELECT item_key, item_type, end_date FROM billing_module_subs
               WHERE user_telegram_id=? AND is_active=1
                 AND (end_date IS NULL OR end_date > datetime('now'))""",
            (telegram_id,)
        ).fetchall()
        conn.close()
        return {
            r[0]: {"item_type": r[1], "end_date": str(r[2] or "")[:10] or "∞"}
            for r in rows
        }
    except Exception:
        return {}


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
                "plan_label": _plan_type_label(row[1] or ""),
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


def _build_ctx(request, user, subscription, current_plan, plans, history,
               has_pending, csrf_token, msg, modules, bundles, user_mod_subs,
               active_items):
    """Build full template context."""
    return {
        "request": request,
        "user": user,
        "subscription": subscription,
        "current_plan": current_plan,
        "plans": plans,
        "history": history,
        "has_pending": has_pending,
        "csrf_token": csrf_token,
        "msg": msg,
        "modules": modules,
        "bundles": bundles,
        "user_mod_subs": user_mod_subs,
        "active_items": active_items,
    }


@router.get("/subscription")
def subscription_page(request: Request, msg: str = "", tab: str = "plan"):
    from web.auth import get_session_user, get_csrf_token
    from billing_utils import get_active_billing_items

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    modules = _get_all_billing_modules()
    bundles = _get_all_billing_bundles()

    # Super-admin has unconditional Премиум access — no real subscription record
    if user.get("role") == "super_admin":
        plans = _get_all_plans()
        for p in plans:
            p["is_current"] = p["name"] == "Премиум"
            p["is_upgrade"] = False
            p["is_downgrade"] = (
                PLAN_ORDER.index(p["name"]) < PLAN_ORDER.index("Премиум")
                if p["name"] in PLAN_ORDER else False
            )
        return request.app.state.templates.TemplateResponse(
            request,
            "subscription/index.html",
            _build_ctx(
                request, user,
                {"plan_type": "Премиум", "start_date": "—", "end_date": "—", "is_trial": False},
                "Премиум", plans, [], False,
                get_csrf_token(request), msg,
                modules, bundles,
                user_mod_subs={"*": {"item_type": "all", "end_date": "∞"}},
                active_items={"modules": {"*"}, "extensions": {"*"}, "bundles": {"*"}},
            ),
        )

    user_id = _get_user_id_in_shop_bot(telegram_id)

    subscription = _get_user_subscription(user_id) if user_id else None
    history = _get_payment_history(user_id) if user_id else []
    has_pending = _has_pending_request(user_id) if user_id else False
    plans = _get_all_plans()
    current_plan = subscription["plan_type"] if subscription else "Бесплатный"
    user_mod_subs = _get_user_active_module_subs(telegram_id)
    try:
        active_items = get_active_billing_items(telegram_id)
        # Convert sets to lists for JSON serialisation in Jinja2
        active_items = {k: list(v) for k, v in active_items.items()}
    except Exception:
        active_items = {"modules": [], "extensions": [], "bundles": []}

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
        _build_ctx(
            request, user, subscription, current_plan, plans, history,
            has_pending, get_csrf_token(request), msg,
            modules, bundles, user_mod_subs, active_items,
        ),
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
        _notify_admin_new_request(
            plan_type, amount,
            user.get("first_name", user.get("email", "—")),
            telegram_id,
        )
        return RedirectResponse(url="/subscription?msg=request_sent&tab=plan", status_code=303)
    except Exception as exc:
        logging.error("subscription_request error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)


@router.post("/subscription/module-request")
def subscription_module_request(
    request: Request,
    plan_type: str = Form(...),
    amount: int = Form(...),
    csrf_token: str = Form(default=""),
):
    """Клиентская заявка на подключение модуля или пакета."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?tab=modules&msg=csrf_error", status_code=303)

    # plan_type must be module_* or bundle_*
    if not (plan_type.startswith("module_") or plan_type.startswith("bundle_")):
        return RedirectResponse(url="/subscription?tab=modules&msg=invalid_plan", status_code=303)

    if amount < 0 or amount > 100_000:
        return RedirectResponse(url="/subscription?tab=modules&msg=invalid_plan", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?tab=modules&msg=user_not_found", status_code=303)

    if _has_pending_request(user_id):
        return RedirectResponse(url="/subscription?tab=modules&msg=already_pending", status_code=303)

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        conn.execute(
            """INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id)
               VALUES (?, ?, ?, 'web_module_request')""",
            (user_id, plan_type, amount),
        )
        conn.commit()
        conn.close()
        _notify_admin_new_request(
            plan_type, amount,
            user.get("first_name", user.get("email", "—")),
            telegram_id,
        )
        return RedirectResponse(url="/subscription?tab=modules&msg=module_request_sent", status_code=303)
    except Exception as exc:
        logging.error("subscription_module_request error: %s", exc)
        return RedirectResponse(url="/subscription?tab=modules&msg=error", status_code=303)
