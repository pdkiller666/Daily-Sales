import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from typing import Annotated

router = APIRouter()


def _get_internal_uid(db, telegram_id: int):
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_user_allowed_shops(telegram_id: int, db) -> list:
    from db_utils import get_user_org_scope
    all_shops = db.get_all_shops() or []
    try:
        scope_type, scope_values = get_user_org_scope(telegram_id)
        if not scope_type or scope_type == "all":
            return all_shops
        if scope_type == "shop":
            filtered = [s for s in all_shops if s in scope_values]
            if filtered:
                return filtered
        elif scope_type in ("city", "network", "trade_network"):
            col = "city" if scope_type == "city" else "trade_network"
            placeholders = ",".join("?" * len(scope_values))
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute(
                f"SELECT DISTINCT shop_name FROM users WHERE {col} IN ({placeholders}) AND shop_name IS NOT NULL",
                scope_values,
            )
            allowed = {row[0] for row in cur.fetchall()}
            conn.close()
            filtered = [s for s in all_shops if s in allowed]
            if filtered:
                return filtered
    except Exception:
        pass
    # Fallback: user's shop from org DB
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT shop_name FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] and row[0] in all_shops:
            return [row[0]]
    except Exception:
        pass
    return all_shops


@router.get("/pos")
def pos_page(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "shops": [],
        "all_shops": [],
        "csrf_token": get_csrf_token(request),
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        ctx["shops"] = _get_user_allowed_shops(telegram_id, db)
        ctx["all_shops"] = db.get_all_shops() or []
    except Exception as e:
        ctx["error"] = str(e)

    return request.app.state.templates.TemplateResponse(request, "pos/index.html", ctx)


@router.get("/api/pos/products")
def api_pos_products(request: Request, shop: str = ""):
    """Return all products with stock grouped by category for POS."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized", "products": []}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        allowed_shops = _get_user_allowed_shops(telegram_id, db)
        if shop and shop not in allowed_shops:
            if user.get("role") not in ("owner", "admin", "super_admin"):
                return JSONResponse({"error": "Forbidden", "products": []}, status_code=403)

        raw = db.get_all_inventory(shop_name=shop if shop else None) or []
        # id[0] product_id[1] shop_name[2] quantity[3] … name[6] category[7] price[8]
        products = []
        for r in raw:
            qty = int(r[3] or 0)
            if qty <= 0:
                continue
            products.append({
                "id": r[1],
                "name": r[6] or "",
                "category": r[7] or "Без категории",
                "price": float(r[8] or 0),
                "stock": qty,
            })

        # Sort: by category then by name
        products.sort(key=lambda p: (p["category"].lower(), p["name"].lower()))

        # Group by category
        categories: dict = {}
        for p in products:
            cat = p["category"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(p)

        return JSONResponse({"products": products, "categories": categories})
    except Exception as e:
        return JSONResponse({"error": str(e), "products": [], "categories": {}})


@router.post("/pos/checkout")
def pos_checkout(
    request: Request,
    items_json: Annotated[str, Form()],
    shop_name: Annotated[str, Form()],
    csrf_token: str = Form(default=""),
):
    """Process a POS cart checkout — records multiple sales at once."""
    import json
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF error"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        items = json.loads(items_json)
        if not items:
            return JSONResponse({"ok": False, "error": "Корзина пуста"}, status_code=400)

        db = get_web_db(telegram_id, org_db)
        allowed_shops = _get_user_allowed_shops(telegram_id, db)

        if shop_name not in allowed_shops:
            if user.get("role") not in ("owner", "admin", "super_admin"):
                return JSONResponse({"ok": False, "error": "Магазин недоступен"}, status_code=403)
            all_shops = db.get_all_shops() or []
            if shop_name not in all_shops:
                return JSONResponse({"ok": False, "error": "Магазин не найден"}, status_code=400)

        internal_uid = _get_internal_uid(db, telegram_id)
        if not internal_uid:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)

        sold = []
        errors = []
        for item in items:
            product_id = int(item.get("id", 0))
            qty = int(item.get("qty", 1))
            price = float(item.get("price", 0))
            name = item.get("name", "")

            if qty < 1 or product_id < 1:
                continue

            try:
                result = db.add_sale(
                    product_id=product_id,
                    shop_name=shop_name,
                    quantity_sold=qty,
                    user_id=internal_uid,
                    sale_price=price,
                )
                if result is None:
                    errors.append(f"«{name}»: недостаточно на складе")
                else:
                    sold.append(name)
            except Exception as e:
                errors.append(f"«{name}»: {e}")

        if not sold:
            return JSONResponse({"ok": False, "error": "; ".join(errors) or "Ошибка записи"}, status_code=400)

        msg = f"Продано {len(sold)} поз."
        if errors:
            msg += f" Ошибки: {'; '.join(errors)}"
        return JSONResponse({"ok": True, "message": msg, "sold_count": len(sold), "errors": errors})

    except Exception as e:
        logging.error(f"pos_checkout error: {e}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)
