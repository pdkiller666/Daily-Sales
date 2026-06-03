import sqlite3
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

SHOP_BOT_DB = "data/shop_bot.db"

PLAN_LABELS = {
    "Базовый": "Базовый",
    "Стандарт": "Стандарт",
    "Премиум": "Премиум",
}


def _get_db():
    from database import Database
    return Database(SHOP_BOT_DB)


def _fmt_promo(row) -> dict:
    """cols: id[0] code[1] discount_percent[2] discount_type[3] usage_count[4]
             max_usage[5] is_active[6] expires_at[7] allowed_plans[8]
             last_used_at[9] created_at[10]"""
    dtype = row[3] or "percent"
    dval = int(row[2] or 0)
    if dtype == "percent":
        discount_label = f"{dval}%"
    else:
        discount_label = f"{dval:,}".replace(",", "\u00a0") + "\u00a0₽"

    usage = int(row[4] or 0)
    max_u = int(row[5] or 0)
    used_pct = round(usage / max_u * 100) if max_u > 0 else 0

    allowed = row[8]
    if allowed:
        try:
            import json
            plans = json.loads(allowed)
            allowed_label = ", ".join(PLAN_LABELS.get(p, p) for p in plans)
        except Exception:
            allowed_label = allowed
    else:
        allowed_label = "Все тарифы"

    expires = str(row[7] or "")[:10] if row[7] else None
    return {
        "id": row[0],
        "code": row[1] or "—",
        "discount_label": discount_label,
        "discount_type": dtype,
        "discount_value": dval,
        "usage": usage,
        "max_usage": max_u,
        "used_pct": used_pct,
        "is_active": bool(row[6]),
        "expires_at": expires,
        "allowed_label": allowed_label,
        "last_used": str(row[9] or "")[:10] if row[9] else None,
        "created_at": str(row[10] or "")[:10],
    }


@router.get("/promocodes")
def promocodes_page(request: Request, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    ctx: dict = {
        "request": request, "user": user,
        "csrf_token": get_csrf_token(request),
        "promocodes": [], "msg": msg, "error": None,
        "created_codes": [],
    }

    try:
        db = _get_db()
        rows = db.get_all_promocodes(include_inactive=True)
        ctx["promocodes"] = [_fmt_promo(r) for r in rows]
    except Exception as exc:
        import logging
        logging.error(f"promocodes_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "promocodes/index.html", ctx
    )


@router.post("/promocodes/create")
async def promocodes_create(
    request: Request,
    code: str = Form(default=""),
    discount_value: int = Form(default=10),
    discount_type: str = Form(default="percent"),
    max_usage: int = Form(default=1),
    expires_at: str = Form(default=""),
    allowed_plans: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/promocodes?msg=csrf_error", status_code=303)

    code = code.strip().upper()
    if not code or len(code) > 30:
        return RedirectResponse(url="/promocodes?msg=invalid_code", status_code=303)
    if discount_value <= 0 or (discount_type == "percent" and discount_value > 100):
        return RedirectResponse(url="/promocodes?msg=invalid_discount", status_code=303)
    if max_usage < 1 or max_usage > 100000:
        return RedirectResponse(url="/promocodes?msg=invalid_usage", status_code=303)

    expires = expires_at.strip() if expires_at.strip() else None
    plans_json = None
    if allowed_plans.strip():
        import json
        plans = [p.strip() for p in allowed_plans.split(",") if p.strip()]
        plans_json = json.dumps(plans) if plans else None

    try:
        db = _get_db()
        db.create_promocode(
            code=code,
            discount_percent=discount_value,
            discount_type=discount_type,
            max_usage=max_usage,
            expires_at=expires,
            allowed_plans=plans_json,
        )
        return RedirectResponse(url="/promocodes?msg=created", status_code=303)
    except Exception as exc:
        import logging
        logging.error(f"promocodes_create error: {exc}")
        if "UNIQUE" in str(exc):
            return RedirectResponse(url="/promocodes?msg=duplicate", status_code=303)
        return RedirectResponse(url="/promocodes?msg=error", status_code=303)


@router.post("/promocodes/batch")
async def promocodes_batch(
    request: Request,
    prefix: str = Form(default=""),
    discount_value: int = Form(default=10),
    discount_type: str = Form(default="percent"),
    count: int = Form(default=5),
    expires_at: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/promocodes?msg=csrf_error", status_code=303)

    if count < 1 or count > 200:
        return RedirectResponse(url="/promocodes?msg=invalid_count", status_code=303)

    expires = expires_at.strip() if expires_at.strip() else None
    try:
        db = _get_db()
        codes = db.create_promocodes_batch(
            prefix=prefix.strip().upper(),
            discount_percent=discount_value,
            discount_type=discount_type,
            count=count,
            expires_at=expires,
        )
        # Store codes in session flash via query param (simplified: redirect with count)
        return RedirectResponse(url=f"/promocodes?msg=batch_{len(codes)}", status_code=303)
    except Exception as exc:
        import logging
        logging.error(f"promocodes_batch error: {exc}")
        return RedirectResponse(url="/promocodes?msg=error", status_code=303)


@router.post("/promocodes/{promo_id}/toggle")
async def promocodes_toggle(
    request: Request,
    promo_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/promocodes?msg=csrf_error", status_code=303)

    try:
        db = _get_db()
        row = db.get_promocode_by_id(promo_id)
        if row:
            new_state = 0 if bool(row[6]) else 1
            db.update_promocode(promo_id, is_active=new_state)
        return RedirectResponse(url="/promocodes?msg=toggled", status_code=303)
    except Exception as exc:
        import logging
        logging.error(f"promocodes_toggle error: {exc}")
        return RedirectResponse(url="/promocodes?msg=error", status_code=303)


@router.post("/promocodes/{promo_id}/delete")
async def promocodes_delete(
    request: Request,
    promo_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/promocodes?msg=csrf_error", status_code=303)

    try:
        db = _get_db()
        db.delete_promocode(promo_id)
        return RedirectResponse(url="/promocodes?msg=deleted", status_code=303)
    except Exception as exc:
        import logging
        logging.error(f"promocodes_delete error: {exc}")
        return RedirectResponse(url="/promocodes?msg=error", status_code=303)
