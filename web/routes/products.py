from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/products")
def products_page(request: Request, q: str = "", category: str = ""):
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
        "products": [], "categories": [],
        "selected_category": category, "q": q,
        "stock": {}, "total_count": 0, "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        all_products = db.get_all_products() or []
        all_inv = db.get_all_inventory() or []

        # inv: id[0] product_id[1] shop_name[2] quantity[3] ...
        stock: dict[int, int] = {}
        for row in all_inv:
            pid = row[1]
            stock[pid] = stock.get(pid, 0) + int(row[3] or 0)

        categories = sorted({p[2] for p in all_products if p[2]})

        # products: id[0] name[1] category[2] price[3] created_at[4]
        products = list(all_products)
        if category:
            products = [p for p in products if p[2] == category]
        if q:
            ql = q.lower()
            products = [
                p for p in products
                if ql in (p[1] or "").lower() or ql in (p[2] or "").lower()
            ]

        products.sort(key=lambda p: ((p[2] or ""), (p[1] or "").lower()))

        ctx["products"] = products
        ctx["categories"] = categories
        ctx["stock"] = stock
        ctx["total_count"] = len(products)

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "products/index.html", ctx
    )
