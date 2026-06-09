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


def _plan_type_label(plan_type: str) -> str:
    if not plan_type:
        return "—"
    if plan_type.startswith("module_"):
        return f"Модуль: {plan_type[7:]}"
    if plan_type.startswith("bundle_"):
        return f"Пакет: {plan_type[7:]}"
    if plan_type.startswith("extension_"):
        return f"Расширение: {plan_type[10:]}"
    if plan_type.startswith("addon_"):
        parts = plan_type.split("_")
        labels = {"shops": "Доп. магазин", "products": "Доп. товары"}
        return labels.get(parts[1] if len(parts) > 1 else "", plan_type)
    return plan_type


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


def _get_all_billing_modules() -> list[dict]:
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


def _get_all_billing_extensions() -> list[dict]:
    """Расширения из billing_extensions, сгруппированные по module_key."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        rows = conn.execute(
            """SELECT module_key, key, name, icon, description, price_monthly
               FROM billing_extensions WHERE is_active=1
               ORDER BY module_key, sort_order, id"""
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            module_key, key, name, icon, desc, price = r
            result.append({
                "module_key": module_key,
                "key": key,
                "name": name,
                "icon": icon or "🔧",
                "description": desc or "",
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
            })
        return result
    except Exception:
        return []


def _modules_map(modules: list[dict], extensions: list[dict] | None = None) -> dict:
    """key → {name, icon} для отображения дружественных названий в шаблоне.
    Включает и модули, и расширения, чтобы chips в hero-карточке показывали
    friendly names для всех активных подписок."""
    result = {m["key"]: {"name": m["name"], "icon": m["icon"]} for m in modules}
    if extensions:
        for e in extensions:
            result[e["key"]] = {"name": e["name"], "icon": e["icon"]}
    return result


def _get_all_billing_bundles() -> list[dict]:
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


def _get_active_trial(user_id: int | None) -> dict | None:
    if not user_id:
        return None
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        row = conn.execute(
            """SELECT end_date FROM subscriptions
               WHERE user_id=? AND is_trial=1 AND end_date > datetime('now')
               ORDER BY end_date DESC LIMIT 1""",
            (user_id,)
        ).fetchone()
        conn.close()
        if row:
            return {"end_date": str(row[0] or "")[:10], "is_trial": True}
        return None
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


def _get_payment_requisites() -> str:
    """Возвращает реквизиты оплаты из payment_settings."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        row = conn.execute(
            "SELECT value FROM payment_settings WHERE key='card_number'"
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


@router.get("/subscription")
def subscription_page(request: Request, msg: str = "", tab: str = "modules"):
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
    extensions = _get_all_billing_extensions()
    mmap = _modules_map(modules, extensions)
    requisites = _get_payment_requisites()

    if user.get("role") == "super_admin":
        return request.app.state.templates.TemplateResponse(
            request,
            "subscription/index.html",
            {
                "request": request,
                "user": user,
                "trial": None,
                "msg": msg,
                "csrf_token": get_csrf_token(request),
                "modules": modules,
                "bundles": bundles,
                "extensions": extensions,
                "modules_map": mmap,
                "user_mod_subs": {"*": {"item_type": "all", "end_date": "∞"}},
                "active_items": {"modules": ["*"], "extensions": ["*"], "bundles": ["*"]},
                "has_pending": False,
                "history": [],
                "requisites": requisites,
            },
        )

    user_id = _get_user_id_in_shop_bot(telegram_id)
    trial = _get_active_trial(user_id)
    history = _get_payment_history(user_id) if user_id else []
    has_pending = _has_pending_request(user_id) if user_id else False
    user_mod_subs = _get_user_active_module_subs(telegram_id)
    try:
        active_items = get_active_billing_items(telegram_id)
        active_items = {k: list(v) for k, v in active_items.items()}
    except Exception:
        active_items = {"modules": [], "extensions": [], "bundles": []}

    return request.app.state.templates.TemplateResponse(
        request,
        "subscription/index.html",
        {
            "request": request,
            "user": user,
            "trial": trial,
            "msg": msg,
            "csrf_token": get_csrf_token(request),
            "modules": modules,
            "bundles": bundles,
            "extensions": extensions,
            "modules_map": mmap,
            "user_mod_subs": user_mod_subs,
            "active_items": active_items,
            "has_pending": has_pending,
            "history": history,
            "requisites": requisites,
        },
    )


@router.post("/subscription/cancel-request")
def subscription_cancel_request(
    request: Request,
    plan_type: str = Form(...),
    item_name: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Клиентская заявка на отключение модуля/расширения/пакета."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    token = os.environ.get("BOT_TOKEN", "")
    admin_id = os.environ.get("ADMIN_CHAT_ID", "")
    if token and admin_id:
        user_display = user.get("first_name", user.get("email", "—"))
        safe_name = item_name or _plan_type_label(plan_type)
        text = (
            "❌ <b>Запрос на отключение!</b>\n\n"
            f"👤 <b>Пользователь:</b> {user_display}\n"
            f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
            f"📦 <b>Позиция:</b> {safe_name}\n\n"
            "Пожалуйста, отключите доступ вручную в <b>/admin/billing/grants</b>."
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
            logging.warning("subscription_cancel_request notify: %s", exc)

    tab = "modules"
    if plan_type.startswith("bundle_"):
        tab = "bundles"
    elif plan_type.startswith("extension_"):
        tab = "extensions"
    return RedirectResponse(url=f"/subscription?tab={tab}&msg=cancel_request_sent", status_code=303)


@router.post("/subscription/module-request")
def subscription_module_request(
    request: Request,
    plan_type: str = Form(...),
    amount: int = Form(...),
    csrf_token: str = Form(default=""),
):
    """Клиентская заявка на подключение модуля, расширения или пакета."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    valid_prefixes = ("module_", "bundle_", "extension_")
    if not any(plan_type.startswith(p) for p in valid_prefixes):
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    if amount < 0 or amount > 100_000:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    if _has_pending_request(user_id):
        return RedirectResponse(url="/subscription?msg=already_pending", status_code=303)

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
        return RedirectResponse(url="/subscription?msg=module_request_sent", status_code=303)
    except Exception as exc:
        logging.error("subscription_module_request error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)
