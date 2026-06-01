from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/inventory")
def inventory_page(request: Request, shop: str = ""):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "shops": [], "selected_shop": shop,
        "inventory": [], "total_items": 0,
        "out_of_stock": 0, "low_stock": 0, "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        shops = db.get_inventory_shops() or []
        if not shop or shop not in shops:
            shop = shops[0] if shops else ""

        ctx["shops"] = shops
        ctx["selected_shop"] = shop

        # inv: id[0] product_id[1] shop_name[2] quantity[3] updated_at[4]
        #       updated_by[5] name[6] category[7] price[8] updated_by_name[9]
        raw = db.get_all_inventory(shop_name=shop if shop else None) or []

        # Sort: out of stock first (qty ≤ 0), then by qty asc, then name
        def _sort_key(r):
            qty = int(r[3] or 0)
            return (1 if qty > 0 else 0, qty, (r[6] or "").lower())

        inventory = sorted(raw, key=_sort_key)

        ctx["inventory"] = inventory
        ctx["total_items"] = len(inventory)
        ctx["out_of_stock"] = sum(1 for r in inventory if int(r[3] or 0) <= 0)
        ctx["low_stock"] = sum(1 for r in inventory if 0 < int(r[3] or 0) <= 5)

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "inventory/index.html", ctx
    )
