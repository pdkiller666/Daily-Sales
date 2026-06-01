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


@router.get("/products/{product_id}")
def product_detail(request: Request, product_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from datetime import date

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "product": None, "product_id": product_id,
        "inventory_by_shop": [], "inventory_log": [],
        "recent_sales": [], "total_stock": 0,
        "month_revenue": 0.0, "month_qty": 0,
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        product = db.get_product(product_id)
        if not product:
            return RedirectResponse(url="/products", status_code=302)

        # products: id[0] name[1] category[2] price[3] created_at[4] photo_file_id[5] description[6]
        ctx["product"] = product

        # Inventory per shop
        # get_all_inventory returns: i.id[0] product_id[1] shop_name[2] quantity[3] last_updated[4]
        #   updated_by[5] p.name[6] p.category[7] p.price[8] updated_by_name[9]
        all_inv = db.get_all_inventory() or []
        inv_by_shop = [r for r in all_inv if r[1] == product_id]
        inv_by_shop.sort(key=lambda r: -(r[3] or 0))
        total_stock = sum(int(r[3] or 0) for r in inv_by_shop)
        ctx["inventory_by_shop"] = inv_by_shop
        ctx["total_stock"] = total_stock

        # Inventory log for each shop (up to 20 latest entries across all shops)
        inv_log: list = []
        for inv_row in inv_by_shop[:5]:  # show log for top 5 shops by stock
            shop_name = inv_row[2]
            log = db.get_inventory_log(shop_name, product_id, limit=10) or []
            for entry in log:
                # id[0] old_qty[1] new_qty[2] delta[3] change_type[4]
                # change_reason[5] changed_by[6] changed_at[7] changer_name[8]
                inv_log.append({
                    "shop": shop_name,
                    "old_qty": entry[1],
                    "new_qty": entry[2],
                    "delta": entry[3],
                    "change_type": entry[4] or "",
                    "reason": entry[5] or "",
                    "changed_at": (entry[7] or "")[:16],
                    "changer": entry[8] or "—",
                })
        inv_log.sort(key=lambda x: x["changed_at"], reverse=True)
        ctx["inventory_log"] = inv_log[:30]

        # Recent sales of this product
        today = date.today()
        month_start = today.replace(day=1).isoformat()
        # get_sales_report: id[0] pid[1] shop[2] qty[3] price[4] uid[5] date[6]
        #   product_name[7] category[8] first_name[9] last_name[10]
        import sqlite3
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT s.id, s.shop_name, s.quantity_sold, s.sale_price, s.sale_date,
                      u.first_name, u.last_name, s.user_id
               FROM sales s
               LEFT JOIN users u ON u.id = s.user_id
               WHERE s.product_id = ?
               ORDER BY s.sale_date DESC LIMIT 30""",
            (product_id,)
        )
        raw_sales = cur.fetchall()
        # Month totals
        cur.execute(
            """SELECT SUM(s.quantity_sold), SUM(s.quantity_sold * s.sale_price)
               FROM sales s
               WHERE s.product_id = ? AND date(s.sale_date) >= ?""",
            (product_id, month_start)
        )
        month_row = cur.fetchone()
        conn.close()

        ctx["recent_sales"] = raw_sales
        ctx["month_qty"] = int(month_row[0] or 0) if month_row else 0
        ctx["month_revenue"] = float(month_row[1] or 0) if month_row else 0.0

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "products/detail.html", ctx
    )
