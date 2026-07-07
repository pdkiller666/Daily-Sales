import logging
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)
router = APIRouter()

MONTH_NAMES = [
    "", "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
    "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек",
]

# Области таргетинга мотивации по оргструктуре (task #49)
SCOPE_LABELS = {
    "global": "Все продавцы",
    "trade_network": "Сеть",
    "city": "Город",
    "shop": "Магазин",
    "user": "Сотрудник",
}


def _load_scope_options(db, scope_type):
    """Список dict {value, label} для области таргетинга (для веб-формы)."""
    try:
        if scope_type == "trade_network":
            return [{"value": v, "label": v} for v in (db.get_all_trade_networks() or [])]
        if scope_type == "city":
            return [{"value": v, "label": v} for v in (db.get_all_cities() or [])]
        if scope_type == "shop":
            return [{"value": v, "label": v} for v in (db.get_all_shops(include_system=False) or [])]
        if scope_type == "user":
            opts = []
            for u in (db.get_all_users() or []):
                uid = u[0]
                name = f"{u[2] or ''} {u[3] or ''}".strip() or f"ID {uid}"
                extra = u[8] or u[9] or ""
                label = name + (f" · {extra}" if extra else "")
                opts.append({"value": str(uid), "label": label})
            return opts
    except Exception as exc:
        logger.error(f"_load_scope_options error: {exc}")
    return []


def _fmt_rate(mtype, mval) -> str:
    if mtype == "percentage":
        return f"{mval:g}%"
    try:
        v = int(float(mval))
        return f"{v:,}".replace(",", "\u00a0") + "\u00a0₽"
    except Exception:
        return str(mval)


def _month_label(y, m):
    names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
             "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    return f"{names[m - 1]} {y}"


def _offset_month(offset: int):
    """Return (year, month) shifted by offset from today."""
    today = date.today()
    m = today.month + offset
    y = today.year
    while m < 1:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    return y, m


@router.get("/motivation")
def motivation_page(request: Request, category: str = ""):
    from web.auth import get_csrf_token, get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/subscription?msg=plans_motivation_locked", status_code=302)
    org_db = user.get("org_db")

    today = date.today()
    prev_y, prev_m = _offset_month(-1)
    next_y, next_m = _offset_month(+1)
    month_labels = {
        "-1": _month_label(prev_y, prev_m),
        "0":  _month_label(today.year, today.month) + " (текущий)",
        "1":  _month_label(next_y, next_m),
    }

    ctx: dict = {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "motivations": [],
        "products": [],
        "categories": [],
        "selected_category": category,
        "error": None,
        "saved": request.query_params.get("saved") == "1",
        "removed": request.query_params.get("removed") == "1",
        "month_labels": month_labels,
        "extra_conditions": [],
        "extra_saved": request.query_params.get("extra_saved") == "1",
        "plan_coeff_enabled": False,
        "plan_coeff_cap": True,
        "coeff_saved": request.query_params.get("coeff_saved") == "1",
    }

    try:
        db = get_web_db(telegram_id, org_db)

        # All products (for set-form dropdown)
        all_products = db.get_all_products() or []
        # products: id[0] name[1] category[2] price[3]
        categories = sorted({p[2] for p in all_products if p[2]})
        ctx["categories"] = categories

        if category:
            products_filtered = [p for p in all_products if p[2] == category]
        else:
            products_filtered = all_products
        ctx["products"] = products_filtered

        # All motivations with product info
        raw = db.get_all_product_motivations() or []
        # columns: p.id[0] p.name[1] motivation_type[2] motivation_value[3]
        #          u.first_name[4] u.last_name[5] pc.created_at[6]
        motivations = []
        for row in raw:
            pid, pname, mtype, mval, fname, lname, created_at = row
            if mtype is None:
                continue  # no commission set
            if category and all_products:
                prod_cat = next((p[2] for p in all_products if p[0] == pid), "")
                if prod_cat != category:
                    continue
            motivations.append({
                "product_id": pid,
                "product_name": pname,
                "type": mtype,
                "value": mval,
                "rate_display": _fmt_rate(mtype, mval),
                "set_by": f"{fname or ''} {lname or ''}".strip() or "—",
                "created_at": str(created_at or "")[:10],
            })
        ctx["motivations"] = motivations

        # Таргетированные правила мотивации по оргструктуре (task #49)
        try:
            rules_raw = db.get_all_motivation_rules() or []
            rules = []
            for r in rules_raw:
                if category and r.get("product_id"):
                    prod_cat = next((p[2] for p in all_products if p[0] == r["product_id"]), "")
                    if prod_cat != category:
                        continue
                rules.append({
                    "id": r["id"],
                    "product_id": r["product_id"],
                    "product_name": r["product_name"],
                    "scope_type": r["scope_type"],
                    "scope_type_label": SCOPE_LABELS.get(r["scope_type"], r["scope_type"]),
                    "scope_label": r["scope_label"],
                    "rate_display": _fmt_rate(r["motivation_type"], r["motivation_value"]),
                })
            ctx["motivation_rules"] = rules
        except Exception as _exc:
            logger.error(f"motivation rules load error: {_exc}")
            ctx["motivation_rules"] = []

        try:
            raw_extra = db.get_extra_conditions_for_month(today.year, today.month) or []
            ctx["extra_conditions"] = raw_extra
        except Exception:
            ctx["extra_conditions"] = []

        try:
            raw_global = db.get_extra_conditions(active_only=True) or []
            ctx["extra_conditions_global"] = raw_global
        except Exception:
            ctx["extra_conditions_global"] = []

        # Plan coefficient settings for current user
        try:
            _conn = db.get_connection()
            try:
                _cur = _conn.cursor()
                _cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
                _row = _cur.fetchone()
            finally:
                _conn.close()
            if _row:
                ns = db.get_notification_settings(_row[0])
                ctx["plan_coeff_enabled"] = bool(ns.get("plan_coeff_enabled", False))
                ctx["plan_coeff_cap"] = bool(ns.get("plan_coeff_cap", True))
        except Exception:
            ctx["plan_coeff_enabled"] = False
            ctx["plan_coeff_cap"] = True

    except Exception as exc:
        logger.error(f"motivation_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "motivation/index.html", ctx
    )


@router.post("/motivation/plan_coeff")
def motivation_plan_coeff(
    request: Request,
    csrf_token: str = Form(default=""),
    plan_coeff_enabled: str = Form(default=""),
    plan_coeff_cap: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    telegram_id = int(user["sub"])
    from billing_utils import has_module, has_extension
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/subscription?msg=plans_motivation_locked", status_code=302)
    if not has_extension(telegram_id, "plan_coefficients"):
        return RedirectResponse(
            url="/motivation?error=Расширение+«Коэффициент+плана»+не+подключено.+Перейдите+в+Подписка+→+Расширения.",
            status_code=303,
        )
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        _conn = db.get_connection()
        try:
            _cur = _conn.cursor()
            _cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            _row = _cur.fetchone()
        finally:
            _conn.close()
        if _row:
            db.update_notification_settings(
                _row[0],
                plan_coeff_enabled=1 if plan_coeff_enabled == "on" else 0,
                plan_coeff_cap=1 if plan_coeff_cap == "on" else 0,
            )
    except Exception as exc:
        from urllib.parse import quote as _q
        import logging as _log; _log.error(f"motivation_coeff_save: {exc}")
        return RedirectResponse(url=f"/motivation?error={_q('Ошибка сохранения настроек. Попробуйте позже.')}", status_code=303)

    return RedirectResponse(url="/motivation?coeff_saved=1", status_code=303)


@router.get("/motivation/scope_values")
def motivation_scope_values(request: Request, scope_type: str = ""):
    """HTMX: вернуть <option>-ы значений для выбранной области таргетинга."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return Response(content="", status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return Response(content="", status_code=403)

    if scope_type not in ("trade_network", "city", "shop", "user"):
        return Response(content="", media_type="text/html")

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        opts = _load_scope_options(db, scope_type)
    except Exception as exc:
        logger.error(f"motivation_scope_values error: {exc}")
        opts = []

    from markupsafe import escape
    html = "".join(
        f'<option value="{escape(o["value"])}">{escape(o["label"])}</option>'
        for o in opts
    )
    if not html:
        html = '<option value="" disabled>Нет данных — заполните оргструктуру</option>'
    return Response(content=html, media_type="text/html")


@router.post("/motivation/set")
def motivation_set(
    request: Request,
    csrf_token: str = Form(default=""),
    product_id: int = Form(...),
    motivation_type: str = Form(default="percentage"),
    motivation_value: str = Form(default=""),
    month_offset: int = Form(default=0),
    scope_type: str = Form(default="global"),
    scope_value: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    try:
        val = float(motivation_value.replace(",", ".").strip())
        if val <= 0:
            raise ValueError("Ставка должна быть больше 0")
        if motivation_type == "percentage" and val > 100:
            raise ValueError("Процент не может превышать 100")
    except ValueError as exc:
        from urllib.parse import quote as _q
        return RedirectResponse(url=f"/motivation?error={_q(str(exc))}", status_code=303)

    if scope_type not in ("global", "trade_network", "city", "shop", "user"):
        scope_type = "global"
    if scope_type != "global" and not scope_value.strip():
        return RedirectResponse(
            url="/motivation?error=Выберите+значение+для+таргетинга.", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        if scope_type != "global":
            # Таргетированная ставка по оргструктуре (без месячного расписания)
            db.set_product_motivation(
                product_id, motivation_type, val, telegram_id,
                scope_type=scope_type, scope_value=scope_value.strip(),
            )
        elif month_offset == 0:
            db.set_product_motivation(product_id, motivation_type, val, telegram_id)
        else:
            ty, tm = _offset_month(month_offset)
            db.set_motivation_for_month(product_id, ty, tm, motivation_type, val, telegram_id)
            try:
                db.recalculate_month_earnings(product_id, ty, tm)
            except Exception:
                pass
        logger.info(f"Motivation set: product={product_id} type={motivation_type} val={val} offset={month_offset} scope={scope_type}:{scope_value} by={telegram_id}")
    except Exception as exc:
        logger.error(f"motivation_set error: {exc}")
        return RedirectResponse(url="/motivation?error=Ошибка+сохранения.+Попробуйте+позже.", status_code=303)

    return RedirectResponse(url="/motivation?saved=1", status_code=303)


@router.post("/motivation/set_extra")
def motivation_set_extra(
    request: Request,
    csrf_token: str = Form(default=""),
    min_sellers: str = Form(default=""),
    coefficient: str = Form(default=""),
    shop_name: str = Form(default=""),
    include_transferred: str | None = Form(default=None),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    today = date.today()
    telegram_id = int(user["sub"])
    from billing_utils import has_module, has_extension
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/subscription?msg=plans_motivation_locked", status_code=302)
    if not has_extension(telegram_id, "joint_motivation"):
        return RedirectResponse(
            url="/motivation?error=Расширение+«Совместная+мотивация»+не+подключено.+Перейдите+в+Подписка+→+Расширения.",
            status_code=303,
        )
    org_db = user.get("org_db")

    try:
        ms = int(min_sellers.strip()) if min_sellers.strip() else None
        coeff = float(coefficient.replace(",", ".").strip()) if coefficient.strip() else None
        if coeff is not None and not (0.01 <= coeff <= 10.0):
            raise ValueError("Коэффициент должен быть от 0.01 до 10")
        if ms is not None and ms < 0:
            raise ValueError("Минимум продавцов не может быть отрицательным")

        db = get_web_db(telegram_id, org_db)
        db.set_extra_condition_for_month(
            condition_type="multi_seller_coeff",
            calc_mode="joint",
            year=today.year,
            month=today.month,
            shop_name=shop_name.strip() or None,
            min_sellers=ms,
            coefficient=coeff,
            user_id=telegram_id,
            include_transferred=(include_transferred is not None),
        )
    except Exception as exc:
        logger.error(f"motivation_set_extra error: {exc}")
        return RedirectResponse(url="/motivation?error=Ошибка+сохранения.+Попробуйте+позже.", status_code=303)

    return RedirectResponse(url="/motivation?extra_saved=1", status_code=303)


@router.post("/motivation/set_category")
def motivation_set_category(
    request: Request,
    csrf_token: str = Form(default=""),
    category_name: str = Form(...),
    motivation_type: str = Form(default="percentage"),
    motivation_value: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/motivation?error=CSRF", status_code=303)

    try:
        val = float(motivation_value.replace(",", ".").strip())
        if val <= 0:
            raise ValueError("Ставка должна быть больше 0")
        if motivation_type == "percentage" and val > 100:
            raise ValueError("Процент не может превышать 100")
    except ValueError as exc:
        from urllib.parse import quote as _q
        return RedirectResponse(url=f"/motivation?error={_q(str(exc))}&category={_q(category_name)}", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        products = db.get_products_by_category(category_name) or []
        count = 0
        for product in products:
            db.set_product_motivation(product[0], motivation_type, val, telegram_id)
            count += 1
        logger.info(f"Category motivation set: category={category_name} type={motivation_type} val={val} products={count} by={telegram_id}")
    except Exception as exc:
        logger.error(f"motivation_set_category error: {exc}")
        return RedirectResponse(url="/motivation?error=Ошибка+сохранения.+Попробуйте+позже.", status_code=303)

    from urllib.parse import quote
    return RedirectResponse(url=f"/motivation?saved_category={count}&category={quote(category_name)}", status_code=303)


@router.post("/motivation/remove/{product_id}")
def motivation_remove(
    request: Request,
    product_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        removed = db.remove_product_motivation(product_id)
        if removed:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "Мотивация не найдена"}, status_code=404)
    except Exception as exc:
        logger.error(f"motivation_remove error: {exc}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)


@router.post("/motivation/rule/remove/{rule_id}")
def motivation_rule_remove(
    request: Request,
    rule_id: int,
    csrf_token: str = Form(default=""),
):
    """Удалить одно таргетированное правило мотивации (task #49)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        removed = db.remove_motivation_rule(rule_id)
        if removed:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "Правило не найдено"}, status_code=404)
    except Exception as exc:
        logger.error(f"motivation_rule_remove error: {exc}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)
