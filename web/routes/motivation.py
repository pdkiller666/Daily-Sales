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

        try:
            raw_extra = db.get_extra_conditions_for_month(today.year, today.month) or []
            ctx["extra_conditions"] = raw_extra
        except Exception:
            ctx["extra_conditions"] = []

    except Exception as exc:
        logger.error(f"motivation_page error: {exc}")
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "motivation/index.html", ctx
    )


@router.post("/motivation/set")
def motivation_set(
    request: Request,
    csrf_token: str = Form(default=""),
    product_id: int = Form(...),
    motivation_type: str = Form(default="percentage"),
    motivation_value: str = Form(default=""),
    month_offset: int = Form(default=0),
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
    except (ValueError, AttributeError) as exc:
        return RedirectResponse(url=f"/motivation?error={exc}", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        if month_offset == 0:
            db.set_product_motivation(product_id, motivation_type, val, telegram_id)
        else:
            ty, tm = _offset_month(month_offset)
            db.set_motivation_for_month(product_id, ty, tm, motivation_type, val, telegram_id)
            try:
                db.recalculate_month_earnings(product_id, ty, tm)
            except Exception:
                pass
        logger.info(f"Motivation set: product={product_id} type={motivation_type} val={val} offset={month_offset} by={telegram_id}")
    except Exception as exc:
        logger.error(f"motivation_set error: {exc}")
        return RedirectResponse(url=f"/motivation?error={exc}", status_code=303)

    return RedirectResponse(url="/motivation?saved=1", status_code=303)


@router.post("/motivation/set_extra")
def motivation_set_extra(
    request: Request,
    csrf_token: str = Form(default=""),
    min_sellers: str = Form(default=""),
    coefficient: str = Form(default=""),
    shop_name: str = Form(default=""),
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
            condition_type="global",
            year=today.year,
            month=today.month,
            shop_name=shop_name.strip() or None,
            min_sellers=ms,
            coefficient=coeff,
            user_id=telegram_id,
        )
    except Exception as exc:
        logger.error(f"motivation_set_extra error: {exc}")
        return RedirectResponse(url=f"/motivation?error={exc}", status_code=303)

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
    except (ValueError, AttributeError) as exc:
        return RedirectResponse(url=f"/motivation?error={exc}&category={category_name}", status_code=303)

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
        return RedirectResponse(url=f"/motivation?error={exc}", status_code=303)

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
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
