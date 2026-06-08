import json
import logging
from typing import List, Optional
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, Response

logger = logging.getLogger(__name__)
router = APIRouter()


def _build_plan_dict(plan_row, actual, pct):
    """Convert plan_row tuple → display dict (shared by list and detail views)."""
    target = float(plan_row[3] or 0)
    is_revenue = plan_row[2] == "turnover"
    if plan_row[4] == "seller":
        fname = (plan_row[12] or "").strip()
        lname = (plan_row[13] or "").strip()
        who = f"{fname} {lname}".strip() or f"user#{plan_row[5]}"
    else:
        who = plan_row[6] or "все магазины"
    fd = ""
    if plan_row[7] == "category" and plan_row[8]:
        try:
            cats = json.loads(plan_row[8])
            fd = "Категория: " + (", ".join(cats) if isinstance(cats, list) else plan_row[8])
        except Exception:
            fd = "Категория: " + plan_row[8]
    elif plan_row[7] == "product" and plan_row[8]:
        fd = "По выбранным товарам"
    if pct >= 100:
        bar_cls, pct_cls = "bg-emerald-500", "text-emerald-600"
    elif pct >= 70:
        bar_cls, pct_cls = "bg-blue-500", "text-blue-600"
    elif pct >= 40:
        bar_cls, pct_cls = "bg-amber-400", "text-amber-600"
    else:
        bar_cls, pct_cls = "bg-red-400", "text-red-600"
    return {
        "id": plan_row[0], "plan_type": plan_row[1], "metric_type": plan_row[2],
        "target_value": target, "target_type": plan_row[4], "user_id": plan_row[5],
        "shop_name": plan_row[6], "filter_type": plan_row[7], "filter_value": plan_row[8],
        "filter_desc": fd, "is_active": bool(plan_row[9]),
        "created_at": (plan_row[11] or "")[:10],
        "target_who": who, "actual": float(actual), "pct": min(pct, 100),
        "pct_raw": pct, "is_metric_revenue": is_revenue,
        "bar_cls": bar_cls, "pct_cls": pct_cls,
    }


PLAN_TYPE_LABELS = {"weekly": "Недельный", "monthly": "Месячный"}
METRIC_LABELS = {"turnover": "Выручка", "quantity": "Количество"}
TARGET_LABELS = {"seller": "Продавец", "shop": "Магазин"}


def _get_user_db_id(db, telegram_id: int):
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _load_form_data(db):
    """Load sellers, shops, categories, products for the plan form."""
    try:
        all_users = db.get_all_users() or []
        sellers = []
        for u in all_users:
            # users: id[0] telegram_id[1] first_name[2] last_name[3] ... shop_name[8]
            uid = u[0]
            name = f"{(u[2] or '').strip()} {(u[3] or '').strip()}".strip() or f"user#{uid}"
            shop = u[8] or ""
            sellers.append({"id": uid, "name": name, "shop": shop})
        sellers.sort(key=lambda s: s["name"])
    except Exception:
        sellers = []

    try:
        shops = sorted(db.get_inventory_shops() or [])
    except Exception:
        shops = []

    try:
        all_products = db.get_all_products() or []
        categories = sorted({p[2] for p in all_products if p[2]})
        products = [
            {"id": p[0], "name": p[1], "category": p[2] or ""}
            for p in all_products[:200]
        ]
        products.sort(key=lambda p: (p["category"], p["name"]))
    except Exception:
        categories = []
        products = []

    return sellers, shops, categories, products


@router.get("/plans/new")
def plans_new(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/plans", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    sellers, shops, categories, products = _load_form_data(db)

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "is_edit": False, "plan": None,
        "sellers": sellers, "shops": shops,
        "categories": categories, "products": products,
        "plan_type_labels": PLAN_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "target_labels": TARGET_LABELS,
        "csrf_token": get_csrf_token(request),
        "error": None, "form_data": None,
    }
    return request.app.state.templates.TemplateResponse(request, "plans/form.html", ctx)


@router.post("/plans/create")
def plans_create(
    request: Request,
    csrf_token: str = Form(default=""),
    target_type: str = Form(...),
    seller_id: Optional[str] = Form(default=None),
    shop_name_val: Optional[str] = Form(default=None),
    plan_type: str = Form(...),
    metric_type: str = Form(...),
    filter_type: str = Form(default="all"),
    filter_categories: List[str] = Form(default=[]),
    filter_products: List[str] = Form(default=[]),
    target_value: str = Form(...),
):
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/plans", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен. Обновите страницу и попробуйте снова.",
                        status_code=403)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    sellers, shops, categories, products = _load_form_data(db)
    error = None

    try:
        tv = float(target_value.replace(",", ".").strip()) if target_value else 0
        if tv <= 0:
            error = "Целевое значение должно быть больше нуля."
        if target_type not in ("seller", "shop"):
            error = "Выберите тип цели."
        if plan_type not in ("weekly", "monthly"):
            error = "Выберите период."
        if metric_type not in ("turnover", "quantity"):
            error = "Выберите метрику."
        if filter_type not in ("all", "category", "product"):
            filter_type = "all"
        if filter_type != "all":
            from billing_utils import has_extension as _hex
            if not _hex(telegram_id, "plan_filters"):
                filter_type = "all"
                error = "Фильтрация по категории/товару требует расширения «Фильтры планов». Подключите его в Подписка → Расширения."

        uid = None
        shop = None
        if target_type == "seller":
            if not seller_id:
                error = "Выберите продавца."
            else:
                uid = int(seller_id)
        else:
            if not shop_name_val:
                error = "Выберите магазин."
            else:
                shop = shop_name_val

        fv = None
        if filter_type == "category":
            if not filter_categories:
                error = "Выберите хотя бы одну категорию."
            else:
                fv = json.dumps(filter_categories, ensure_ascii=False)
        elif filter_type == "product":
            if not filter_products:
                error = "Выберите хотя бы один товар."
            else:
                fv = json.dumps([int(p) for p in filter_products])

        if error:
            raise ValueError(error)

        created_by = _get_user_db_id(db, telegram_id)
        db.add_sales_plan(
            plan_type=plan_type, metric_type=metric_type,
            target_value=tv, target_type=target_type,
            user_id=uid, shop_name=shop,
            filter_type=filter_type, filter_value=fv,
            created_by=created_by,
        )
        return RedirectResponse(url="/plans", status_code=303)

    except ValueError as e:
        error = str(e)   # controlled validation message — safe to show
    except Exception as e:
        logger.error(f"plans_create error: {e}")
        error = "Ошибка при создании плана. Попробуйте ещё раз."

    ctx = {
        "request": request, "user": user,
        "is_admin": True, "is_edit": False, "plan": None,
        "sellers": sellers, "shops": shops,
        "categories": categories, "products": products,
        "plan_type_labels": PLAN_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "target_labels": TARGET_LABELS,
        "csrf_token": get_csrf_token(request),
        "error": error,
        "form_data": {
            "target_type": target_type, "seller_id": seller_id,
            "shop_name_val": shop_name_val, "plan_type": plan_type,
            "metric_type": metric_type, "filter_type": filter_type,
            "filter_categories": filter_categories,
            "filter_products": [str(p) for p in filter_products],
            "target_value": target_value,
        },
    }
    return request.app.state.templates.TemplateResponse(request, "plans/form.html", ctx)


@router.get("/plans")
def plans_page(request: Request, active_only: str = "1"):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "plans_data": [], "grouped_plans": [], "active_only": active_only,
        "plan_type_labels": PLAN_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "target_labels": TARGET_LABELS,
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        only_active = active_only != "0"
        if only_active:
            progress_rows = db.get_plans_progress(local_today=today)
        else:
            plans = db.get_sales_plans(active_only=False) or []
            progress_rows = []
            for plan in plans:
                actual = db.calculate_plan_actual(plan, local_today=today)
                target = plan[3]
                pct = round((actual / target * 100) if target > 0 else 0.0, 1)
                progress_rows.append((plan, actual, pct))

        plans_data = []
        for plan_row, actual, pct in progress_rows:
            target = float(plan_row[3] or 0)
            is_metric_revenue = plan_row[2] == "turnover"

            if plan_row[4] == "seller":
                fname = (plan_row[12] or "").strip()
                lname = (plan_row[13] or "").strip()
                target_who = f"{fname} {lname}".strip() or f"user#{plan_row[5]}"
            else:
                target_who = plan_row[6] or "все магазины"

            filter_desc = ""
            if plan_row[7] == "category" and plan_row[8]:
                try:
                    cats = json.loads(plan_row[8])
                    filter_desc = ", ".join(cats) if isinstance(cats, list) else plan_row[8]
                except Exception:
                    filter_desc = plan_row[8]
                filter_desc = f"Категория: {filter_desc}"
            elif plan_row[7] == "product" and plan_row[8]:
                filter_desc = "По выбранным товарам"

            if pct >= 100:
                bar_cls, pct_cls = "bg-emerald-500", "text-emerald-600"
            elif pct >= 70:
                bar_cls, pct_cls = "bg-blue-500", "text-blue-600"
            elif pct >= 40:
                bar_cls, pct_cls = "bg-amber-400", "text-amber-600"
            else:
                bar_cls, pct_cls = "bg-red-400", "text-red-600"

            plans_data.append({
                "id": plan_row[0], "plan_type": plan_row[1],
                "metric_type": plan_row[2], "target_value": target,
                "target_type": plan_row[4], "target_who": target_who,
                "filter_desc": filter_desc, "is_active": bool(plan_row[9]),
                "created_at": (plan_row[11] or "")[:10],
                "actual": float(actual), "pct": min(pct, 100),
                "pct_raw": pct, "is_metric_revenue": is_metric_revenue,
                "bar_cls": bar_cls, "pct_cls": pct_cls,
            })

        plans_data.sort(key=lambda p: (0 if p["is_active"] else 1, -p["pct_raw"]))
        ctx["plans_data"] = plans_data

        # Group by target (shop or seller) — one card per entity
        groups: dict = {}
        for p in plans_data:
            key = p["target_who"]
            if key not in groups:
                groups[key] = {
                    "key": key,
                    "plans": [],
                    "target_type": p["target_type"],
                    "is_active": False,
                }
            groups[key]["plans"].append(p)
            if p["is_active"]:
                groups[key]["is_active"] = True

        grouped_plans = []
        for g in groups.values():
            g["avg_pct"] = round(
                sum(p["pct_raw"] for p in g["plans"]) / len(g["plans"]), 1
            )
            g["count"] = len(g["plans"])
            grouped_plans.append(g)
        grouped_plans.sort(key=lambda g: (0 if g["is_active"] else 1, g["avg_pct"]))
        ctx["grouped_plans"] = grouped_plans

    except Exception as exc:
        logger.error(f"plans_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "plans/index.html", ctx)


@router.get("/plans/{plan_id}/edit")
def plans_edit(request: Request, plan_id: int):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/plans/{plan_id}", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)

    all_plans = db.get_sales_plans(active_only=False) or []
    plan_row = next((p for p in all_plans if p[0] == plan_id), None)
    if not plan_row:
        return RedirectResponse(url="/plans", status_code=302)

    from timezone_utils import get_current_user_time
    tz = db.get_user_timezone(telegram_id)
    today = get_current_user_time(tz).date()
    actual = db.calculate_plan_actual(plan_row, local_today=today)
    target = float(plan_row[3] or 0)
    pct = round((actual / target * 100) if target > 0 else 0.0, 1)
    plan_dict = _build_plan_dict(plan_row, actual, pct)

    sel_cats, sel_prods = [], []
    if plan_row[7] == "category" and plan_row[8]:
        try:
            sel_cats = json.loads(plan_row[8])
        except Exception:
            sel_cats = [plan_row[8]]
    elif plan_row[7] == "product" and plan_row[8]:
        try:
            sel_prods = [str(i) for i in json.loads(plan_row[8])]
        except Exception:
            sel_prods = []

    plan_dict["sel_cats"] = sel_cats
    plan_dict["sel_prods"] = sel_prods
    plan_dict["seller_id"] = str(plan_row[5]) if plan_row[5] else ""
    plan_dict["shop_name_val"] = plan_row[6] or ""

    sellers, shops, categories, products = _load_form_data(db)

    ctx = {
        "request": request, "user": user,
        "is_admin": True, "is_edit": True,
        "plan": plan_dict,
        "sellers": sellers, "shops": shops,
        "categories": categories, "products": products,
        "plan_type_labels": PLAN_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "target_labels": TARGET_LABELS,
        "csrf_token": get_csrf_token(request),
        "error": None, "form_data": None,
    }
    return request.app.state.templates.TemplateResponse(request, "plans/form.html", ctx)


@router.post("/plans/{plan_id}/update")
def plans_update(
    request: Request,
    plan_id: int,
    csrf_token: str = Form(default=""),
    target_type: str = Form(...),
    seller_id: Optional[str] = Form(default=None),
    shop_name_val: Optional[str] = Form(default=None),
    plan_type: str = Form(...),
    metric_type: str = Form(...),
    filter_type: str = Form(default="all"),
    filter_categories: List[str] = Form(default=[]),
    filter_products: List[str] = Form(default=[]),
    target_value: str = Form(...),
):
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/plans/{plan_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен. Обновите страницу и попробуйте снова.",
                        status_code=403)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    error = None

    try:
        tv = float(target_value.replace(",", ".").strip()) if target_value else 0
        if tv <= 0:
            error = "Целевое значение должно быть больше нуля."
        if target_type not in ("seller", "shop"):
            error = "Выберите тип цели."
        if plan_type not in ("weekly", "monthly"):
            error = "Выберите период."
        if metric_type not in ("turnover", "quantity"):
            error = "Выберите метрику."
        if filter_type not in ("all", "category", "product"):
            filter_type = "all"
        if filter_type != "all":
            from billing_utils import has_extension as _hex
            if not _hex(telegram_id, "plan_filters"):
                filter_type = "all"
                error = "Фильтрация по категории/товару требует расширения «Фильтры планов». Подключите его в Подписка → Расширения."

        uid = None
        shop = None
        if target_type == "seller":
            if not seller_id:
                error = "Выберите продавца."
            else:
                uid = int(seller_id)
        else:
            if not shop_name_val:
                error = "Выберите магазин."
            else:
                shop = shop_name_val

        fv = None
        if filter_type == "category":
            if not filter_categories:
                error = "Выберите хотя бы одну категорию."
            else:
                fv = json.dumps(filter_categories, ensure_ascii=False)
        elif filter_type == "product":
            if not filter_products:
                error = "Выберите хотя бы один товар."
            else:
                fv = json.dumps([int(p) for p in filter_products])

        if error:
            raise ValueError(error)

        db.update_sales_plan(
            plan_id,
            plan_type=plan_type, metric_type=metric_type,
            target_value=tv, target_type=target_type,
            user_id=uid, shop_name=shop,
            filter_type=filter_type, filter_value=fv,
        )
        return RedirectResponse(url=f"/plans/{plan_id}", status_code=303)

    except ValueError as e:
        error = str(e)   # controlled validation message — safe to show
    except Exception as e:
        logger.error(f"plans_update {plan_id} error: {e}")
        error = "Ошибка при обновлении плана. Попробуйте ещё раз."

    all_plans = db.get_sales_plans(active_only=False) or []
    plan_row = next((p for p in all_plans if p[0] == plan_id), None)
    plan_dict = None
    if plan_row:
        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()
        actual = db.calculate_plan_actual(plan_row, local_today=today)
        target_f = float(plan_row[3] or 0)
        pct = round((actual / target_f * 100) if target_f > 0 else 0.0, 1)
        plan_dict = _build_plan_dict(plan_row, actual, pct)
        plan_dict["sel_cats"] = filter_categories
        plan_dict["sel_prods"] = [str(p) for p in filter_products]
        plan_dict["seller_id"] = seller_id or ""
        plan_dict["shop_name_val"] = shop_name_val or ""

    sellers, shops, categories, products = _load_form_data(db)

    ctx = {
        "request": request, "user": user,
        "is_admin": True, "is_edit": True,
        "plan": plan_dict,
        "sellers": sellers, "shops": shops,
        "categories": categories, "products": products,
        "plan_type_labels": PLAN_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "target_labels": TARGET_LABELS,
        "csrf_token": get_csrf_token(request),
        "error": error,
        "form_data": {
            "target_type": target_type, "seller_id": seller_id,
            "shop_name_val": shop_name_val, "plan_type": plan_type,
            "metric_type": metric_type, "filter_type": filter_type,
            "filter_categories": filter_categories,
            "filter_products": [str(p) for p in filter_products],
            "target_value": target_value,
        },
    }
    return request.app.state.templates.TemplateResponse(request, "plans/form.html", ctx)


@router.post("/plans/{plan_id}/delete")
def plans_delete(
    request: Request,
    plan_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/plans/{plan_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен. Обновите страницу и попробуйте снова.",
                        status_code=403)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)

    try:
        ok = db.delete_sales_plan(plan_id)
        if not ok:
            logger.warning(f"plans_delete: plan {plan_id} not found or already deleted")
    except Exception as e:
        logger.error(f"plans_delete {plan_id} error: {e}")
        return RedirectResponse(url=f"/plans/{plan_id}?error=delete", status_code=303)

    return RedirectResponse(url="/plans", status_code=303)


@router.post("/plans/{plan_id}/toggle")
def plans_toggle(
    request: Request,
    plan_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/plans/{plan_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен. Обновите страницу и попробуйте снова.",
                        status_code=403)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)

    try:
        all_plans = db.get_sales_plans(active_only=False) or []
        plan_row = next((p for p in all_plans if p[0] == plan_id), None)
        if plan_row:
            new_active = 0 if plan_row[9] else 1
            db.update_sales_plan(plan_id, is_active=new_active)
        else:
            logger.warning(f"plans_toggle: plan {plan_id} not found")
    except Exception as e:
        logger.error(f"plans_toggle {plan_id} error: {e}")
        return RedirectResponse(url=f"/plans/{plan_id}?error=toggle", status_code=303)

    return RedirectResponse(url=f"/plans/{plan_id}", status_code=303)


def _load_milestone_history(db, plan_id: int) -> list:
    """Return milestone alert rows for a plan, newest first, with seller names resolved."""
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pma.milestone, pma.period_start, pma.alerted_at,
                   u.first_name, u.last_name, pma.user_id
            FROM plan_milestone_alerts pma
            LEFT JOIN users u ON u.id = pma.user_id
            WHERE pma.plan_id = ?
            ORDER BY pma.alerted_at DESC
            """,
            (plan_id,),
        )
        rows = cur.fetchall()
        conn.close()
        result = []
        for row in rows:
            milestone, period_start, alerted_at, fname, lname, user_id = row
            name = f"{(fname or '').strip()} {(lname or '').strip()}".strip() or f"user#{user_id}"
            alerted_date = (alerted_at or "")[:10]
            result.append({
                "milestone": milestone,
                "period_start": (period_start or "")[:10],
                "alerted_at": alerted_date,
                "seller_name": name,
            })
        return result
    except Exception as exc:
        logger.warning(f"_load_milestone_history plan {plan_id}: {exc}")
        return []


@router.get("/plans/{plan_id}")
def plan_detail(request: Request, plan_id: int, error: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "plans_motivation"):
        return RedirectResponse(url="/dashboard?msg=module_plans_required", status_code=302)
    org_db = user.get("org_db")

    error_msg = ""
    if error == "delete":
        error_msg = "Не удалось удалить план. Попробуйте ещё раз."
    elif error == "toggle":
        error_msg = "Не удалось изменить статус плана. Попробуйте ещё раз."

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "plan": None, "sellers": [], "milestones": [], "error": error_msg or None,
        "plan_type_labels": PLAN_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "target_labels": TARGET_LABELS,
        "csrf_token": "",
    }

    try:
        db = get_web_db(telegram_id, org_db)
        ctx["csrf_token"] = get_csrf_token(request)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        all_plans = db.get_sales_plans(active_only=False) or []
        plan_row = next((p for p in all_plans if p[0] == plan_id), None)
        if not plan_row:
            return RedirectResponse(url="/plans", status_code=302)

        actual = db.calculate_plan_actual(plan_row, local_today=today)
        target = float(plan_row[3] or 0)
        pct = round((actual / target * 100) if target > 0 else 0.0, 1)
        ctx["plan"] = _build_plan_dict(plan_row, actual, pct)

        if plan_row[4] == "shop" and plan_row[6]:
            shop_users = db.get_all_users(shop_name=plan_row[6]) or []
            sellers = []
            for u_row in shop_users:
                # users: id[0] telegram_id[1] first_name[2] last_name[3]
                uid = u_row[0]
                name = f"{(u_row[2] or '').strip()} {(u_row[3] or '').strip()}".strip() or f"user#{uid}"
                fake_plan = list(plan_row)
                fake_plan[4] = "seller"
                fake_plan[5] = uid
                fake_plan[6] = None
                seller_actual = db.calculate_plan_actual(fake_plan, local_today=today)
                sellers.append({
                    "user_db_id": uid, "name": name,
                    "actual": float(seller_actual),
                    "is_revenue": plan_row[2] == "turnover",
                    "pct": round(seller_actual / target * 100, 1) if target else 0.0,
                })
            sellers.sort(key=lambda s: -s["actual"])
            ctx["sellers"] = sellers

        from billing_utils import has_extension as _hex
        if _hex(telegram_id, "milestone_alerts"):
            ctx["milestones"] = _load_milestone_history(db, plan_id)
        ctx["has_milestone_alerts"] = _hex(telegram_id, "milestone_alerts")

    except Exception as exc:
        logger.error(f"plan_detail {plan_id} error: {exc}")
        if not ctx.get("error"):
            ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "plans/detail.html", ctx)
