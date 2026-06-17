import html as _html
import json
import logging
import os
import sqlite3
import threading
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
        f"👤 <b>Пользователь:</b> {_html.escape(str(user_display))}\n"
        f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
        f"📦 <b>Позиция:</b> {_html.escape(label)}\n"
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


def _notify_admin_async(plan_type: str, amount: int, user_display: str, telegram_id: int) -> None:
    """Fire-and-forget wrapper: отправляет уведомление в daemon-thread."""
    t = threading.Thread(
        target=_notify_admin_new_request,
        args=(plan_type, amount, user_display, telegram_id),
        daemon=True,
    )
    t.start()


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
    return f"Тариф: {plan_type}"


def _get_user_id_in_shop_bot(telegram_id: int) -> int | None:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _strip_leading_emoji(name: str, icon: str) -> str:
    """Возвращает name без ведущего эмодзи+пробел, если name начинается с icon."""
    if icon and name and name.startswith(icon):
        return name[len(icon):].lstrip()
    return name


def _get_all_billing_modules() -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                """SELECT key, name, icon, description, price_monthly, features_json
                   FROM billing_modules WHERE is_active=1
                   ORDER BY sort_order, id"""
            ).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            key, name, icon, desc, price, feats_json = r
            try:
                features = json.loads(feats_json or "[]")
            except Exception:
                features = []
            _icon = icon or "🔧"
            result.append({
                "key": key,
                "name": name,
                "name_display": _strip_leading_emoji(name, _icon),
                "icon": _icon,
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
        try:
            rows = conn.execute(
                """SELECT module_key, key, name, icon, description, price_monthly
                   FROM billing_extensions WHERE is_active=1
                   ORDER BY module_key, sort_order, id"""
            ).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            module_key, key, name, icon, desc, price = r
            _icon = icon or "🔧"
            result.append({
                "module_key": module_key,
                "key": key,
                "name": name,
                "name_display": _strip_leading_emoji(name, _icon),
                "icon": _icon,
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
    result = {m["key"]: {"name": m["name"], "name_display": m.get("name_display", m["name"]), "icon": m["icon"]} for m in modules}
    if extensions:
        for e in extensions:
            result[e["key"]] = {"name": e["name"], "name_display": e.get("name_display", e["name"]), "icon": e["icon"]}
    return result


def _get_all_billing_bundles() -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                """SELECT key, name, icon, description, includes_json, price_monthly
                   FROM billing_bundles WHERE is_active=1
                   ORDER BY sort_order, id"""
            ).fetchall()
        finally:
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
        try:
            rows = conn.execute(
                """SELECT item_key, item_type, end_date FROM billing_module_subs
                   WHERE user_telegram_id=? AND is_active=1
                     AND (end_date IS NULL OR end_date > datetime('now'))""",
                (telegram_id,)
            ).fetchall()
        finally:
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
        try:
            row = conn.execute(
                """SELECT end_date FROM subscriptions
                   WHERE user_id=? AND is_trial=1 AND end_date > datetime('now')
                   ORDER BY end_date DESC LIMIT 1""",
                (user_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return {"end_date": str(row[0] or "")[:10], "is_trial": True}
        return None
    except Exception:
        return None


def _get_payment_history(user_id: int, limit: int = 10) -> tuple[list[dict], bool]:
    """Returns (history_rows, has_more). has_more=True when total records > limit."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM payment_requests WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT id, plan_type, amount, status, created_at, processed_at
                   FROM payment_requests
                   WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        finally:
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
        return result, total > limit
    except Exception:
        return [], False


def _has_pending_request(user_id: int) -> bool:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT id FROM payment_requests WHERE user_id = ? AND status = 'pending'",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


def _fmt_cap(value) -> str:
    """Форматирует лимит: -1 → ∞, иначе число с неразрывным пробелом."""
    try:
        v = int(value)
    except Exception:
        return "—"
    if v < 0:
        return "∞"
    return f"{v:,}".replace(",", "\u00a0")


def _get_tariff_overview(telegram_id: int) -> dict | None:
    """Текущий тариф (ось «объём»): план, лимиты объёма и фактическое использование.
    Не меняет логику гейтинга — только читает данные для отображения."""
    try:
        import subscription_utils as su
    except Exception:
        return None

    try:
        org_plan = su._get_org_plan_for_user(telegram_id)
        plan_name = org_plan if org_plan is not None else su._get_personal_plan(telegram_id)
        limits = su.get_plan_limits(telegram_id)
        days_remaining = su.get_subscription_days_remaining(telegram_id)
        addons = su._get_addon_totals_for_user(telegram_id)
    except Exception:
        return None

    extra_products = int(addons.get("extra_products", 0) or 0) * 100
    extra_shops = int(addons.get("extra_shops", 0) or 0)

    def _eff(base, extra):
        try:
            b = int(base)
        except Exception:
            return base
        return b if b < 0 else b + extra

    max_products = _eff(limits.get("max_products", 0), extra_products)
    max_shops = _eff(limits.get("max_shops", 0), extra_shops)
    max_sales = limits.get("max_sales_per_month", 0)

    used_products = used_shops = used_sales = None
    try:
        from tenant_manager import tenant_manager
        from database import Database
        from datetime import datetime as _dt
        db_path = tenant_manager.get_user_db_path(telegram_id)
        db = Database(db_path)
        used_products = len(db.get_all_products())
        used_shops = len(db.get_all_shops())
        month_start = _dt.now().strftime("%Y-%m-01")
        conn = db.get_connection()
        try:
            used_sales = conn.execute(
                "SELECT COUNT(*) FROM sales WHERE sale_date >= ?", (month_start,)
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        pass

    def _pct(used, cap):
        try:
            c = int(cap)
            u = int(used)
        except Exception:
            return None
        if c <= 0:
            return None
        return min(100, round(u * 100 / c))

    return {
        "plan_name": plan_name or "Бесплатный",
        "is_trial": bool(su._has_active_trial(telegram_id)),
        "days_remaining": days_remaining,
        "addon_products": extra_products,
        "addon_shops": extra_shops,
        "rows": [
            {"icon": "🛍", "label": "Товары", "used": used_products,
             "cap": max_products, "cap_fmt": _fmt_cap(max_products),
             "pct": _pct(used_products, max_products)},
            {"icon": "🏪", "label": "Магазины", "used": used_shops,
             "cap": max_shops, "cap_fmt": _fmt_cap(max_shops),
             "pct": _pct(used_shops, max_shops)},
            {"icon": "🧾", "label": "Продажи в месяц", "used": used_sales,
             "cap": max_sales, "cap_fmt": _fmt_cap(max_sales),
             "pct": _pct(used_sales, max_sales)},
        ],
    }


def _get_tariff_plans(current_plan_name: str | None, user_id: int | None = None) -> list[dict]:
    """Доступные тарифы (ось «объём») из subscription_plans для смены прямо из веба.
    Только активные платные планы; текущий помечается is_current.

    Понижение тарифа определяется канонической логикой бота
    (Database.check_subscription_downgrade) — без дублирования enforcement:
    помеченные is_downgrade тарифы предупреждают владельца, что остаток
    текущей подписки сгорит при немедленной замене."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                """SELECT id, name, duration_days, price, description,
                          max_products, max_shops, max_sales_per_month
                   FROM subscription_plans
                   WHERE is_active=1 AND price > 0
                   ORDER BY price"""
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    # Канонический downgrade-чек из бота (Database.check_subscription_downgrade).
    _dg_db = None
    if user_id:
        try:
            from database import Database
            _dg_db = Database(SHOP_BOT_DB)
        except Exception:
            _dg_db = None

    result = []
    for r in rows:
        pid, name, duration, price, desc, mp, ms, msl = r
        _price = int(price or 0)
        is_downgrade = False
        dg_current_plan = ""
        dg_end_date = ""
        dg_days_left = None
        if _dg_db is not None:
            try:
                _is_dg, _info = _dg_db.check_subscription_downgrade(user_id, name)
                if _is_dg and _info:
                    from datetime import datetime as _dt
                    is_downgrade = True
                    dg_current_plan = _info.get("current_plan") or ""
                    _end = _info.get("end_datetime")
                    if _end is not None:
                        dg_end_date = _end.strftime("%d.%m.%Y")
                        dg_days_left = max(0, (_end - _dt.now()).days)
            except Exception:
                pass
        result.append({
            "id": pid,
            "name": name,
            "duration_days": int(duration or 30),
            "price": _price,
            "price_fmt": f"{_price:,}".replace(",", "\u00a0") + "\u00a0₽",
            "description": desc or "",
            "max_products_fmt": _fmt_cap(mp),
            "max_shops_fmt": _fmt_cap(ms),
            "max_sales_fmt": _fmt_cap(msl),
            "is_current": bool(current_plan_name and name == current_plan_name),
            "is_downgrade": is_downgrade,
            "downgrade_current_plan": dg_current_plan,
            "downgrade_end_date": dg_end_date,
            "downgrade_days_left": dg_days_left,
        })
    return result


def _get_item_price(plan_type: str) -> int | None:
    """Возвращает реальную цену из БД по plan_type (module_/bundle_/extension_).
    Используется вместо клиентского amount, чтобы пользователь не мог подделать сумму."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            price = None
            if plan_type.startswith("module_"):
                key = plan_type[7:]
                row = conn.execute(
                    "SELECT price_monthly FROM billing_modules WHERE key=? AND is_active=1 LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    price = int(row[0] or 0)
            elif plan_type.startswith("bundle_"):
                key = plan_type[7:]
                row = conn.execute(
                    "SELECT price_monthly FROM billing_bundles WHERE key=? AND is_active=1 LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    price = int(row[0] or 0)
            elif plan_type.startswith("extension_"):
                key = plan_type[10:]
                row = conn.execute(
                    "SELECT price_monthly FROM billing_extensions WHERE key=? AND is_active=1 LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    price = int(row[0] or 0)
        finally:
            conn.close()
        return price
    except Exception:
        return None


def _get_tariff_plan_by_name(name: str) -> dict | None:
    """Валидация: активный платный тариф с таким именем (для заявки из веба)."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                """SELECT name, price FROM subscription_plans
                   WHERE name=? AND is_active=1 AND price > 0""",
                (name,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return {"name": row[0], "price": int(row[1] or 0)}
        return None
    except Exception:
        return None


def _get_payment_requisites() -> str:
    """Возвращает реквизиты оплаты из payment_settings."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT value FROM payment_settings WHERE key='card_number'"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


@router.get("/subscription")
def subscription_page(request: Request, msg: str = "", tab: str = "modules", need: str = ""):
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

    need = need.strip()[:64]
    need_ext = next((e for e in extensions if e["key"] == need), None) if need else None

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
                "tariff": None,
                "tariff_plans": [],
                "need_ext": need_ext,
            },
        )

    user_id = _get_user_id_in_shop_bot(telegram_id)
    trial = _get_active_trial(user_id)
    tariff = _get_tariff_overview(telegram_id)
    tariff_plans = _get_tariff_plans(tariff.get("plan_name") if tariff else None, user_id)
    history, history_has_more = _get_payment_history(user_id) if user_id else ([], False)
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
            "history_has_more": history_has_more,
            "requisites": requisites,
            "tariff": tariff,
            "tariff_plans": tariff_plans,
            "need_ext": need_ext,
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
            f"👤 <b>Пользователь:</b> {_html.escape(str(user_display))}\n"
            f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
            f"📦 <b>Позиция:</b> {_html.escape(safe_name)}\n\n"
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

    amount = _get_item_price(plan_type)
    if amount is None:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            conn.execute(
                """INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id)
                   VALUES (?, ?, ?, 'web_module_request')""",
                (user_id, plan_type, amount),
            )
            conn.commit()
        finally:
            conn.close()
        _notify_admin_async(
            plan_type, amount,
            user.get("first_name", user.get("email", "—")),
            telegram_id,
        )
        return RedirectResponse(url="/subscription?msg=module_request_sent", status_code=303)
    except Exception as exc:
        logging.error("subscription_module_request error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)


@router.post("/subscription/tariff-request")
def subscription_tariff_request(
    request: Request,
    plan_name: str = Form(...),
    csrf_token: str = Form(default=""),
):
    """Клиентская заявка на смену тарифа (ось «объём»).
    Использует тот же механизм, что и модули: создаёт payment_requests с
    plan_type = имя тарифа. Подтверждение супер-админом (бот/веб) идёт через
    штатный confirm_payment_request → create_subscription, без дублирования логики."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    plan = _get_tariff_plan_by_name(plan_name)
    if not plan:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            conn.execute(
                """INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id)
                   VALUES (?, ?, ?, 'web_tariff_request')""",
                (user_id, plan["name"], plan["price"]),
            )
            conn.commit()
        finally:
            conn.close()
        _notify_admin_async(
            plan["name"], plan["price"],
            user.get("first_name", user.get("email", "—")),
            telegram_id,
        )
        return RedirectResponse(url="/subscription?msg=tariff_request_sent", status_code=303)
    except Exception as exc:
        logging.error("subscription_tariff_request error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)
