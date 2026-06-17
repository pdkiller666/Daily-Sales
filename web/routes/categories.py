from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/categories")
def categories_page(request: Request, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "categories": [],
        "msg": msg,
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        rows = conn.execute(
            """SELECT p.category, COUNT(*) as cnt,
                      COUNT(CASE WHEN COALESCE(inv.qty, 0) > 0 THEN 1 END) as in_stock
               FROM products p
               LEFT JOIN (
                   SELECT product_id, SUM(quantity) as qty
                   FROM inventory
                   GROUP BY product_id
               ) inv ON inv.product_id = p.id
               GROUP BY p.category
               ORDER BY p.category"""
        ).fetchall()
        conn.close()
        ctx["categories"] = [
            {
                "name": row[0] or "—",
                "count": row[1],
                "in_stock": row[2],
            }
            for row in rows
        ]
    except Exception as exc:
        import logging
        logging.error(f"categories_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "categories/index.html", ctx
    )


@router.post("/categories/rename")
async def categories_rename(
    request: Request,
    old_name: str = Form(...),
    new_name: str = Form(...),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/categories?msg=csrf_error", status_code=303)

    new_name = new_name.strip()
    if not new_name or len(new_name) > 80:
        return RedirectResponse(url="/categories?msg=invalid_name", status_code=303)
    if new_name == old_name:
        return RedirectResponse(url="/categories?msg=same_name", status_code=303)

    try:
        db = get_web_db(int(user["sub"]), user.get("org_db"))
        conn = db.get_connection()
        try:
            dup = conn.execute(
                "SELECT COUNT(*) FROM products WHERE category = ?", (new_name,)
            ).fetchone()[0]
            if dup > 0:
                return RedirectResponse(url="/categories?msg=duplicate", status_code=303)
            conn.execute(
                "UPDATE products SET category = ? WHERE category = ?",
                (new_name, old_name),
            )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse(url="/categories?msg=renamed", status_code=303)
    except Exception as exc:
        import logging
        logging.error(f"categories_rename error: {exc}")
        return RedirectResponse(url="/categories?msg=error", status_code=303)


@router.post("/categories/delete")
async def categories_delete(
    request: Request,
    name: str = Form(...),
    move_to: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/categories?msg=csrf_error", status_code=303)

    try:
        db = get_web_db(int(user["sub"]), user.get("org_db"))
        conn = db.get_connection()
        try:
            if move_to and move_to.strip() and move_to.strip() != name:
                conn.execute(
                    "UPDATE products SET category = ? WHERE category = ?",
                    (move_to.strip(), name),
                )
            else:
                # Move to "Без категории" placeholder
                conn.execute(
                    "UPDATE products SET category = 'Без категории' WHERE category = ?",
                    (name,),
                )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse(url="/categories?msg=deleted", status_code=303)
    except Exception as exc:
        import logging
        logging.error(f"categories_delete error: {exc}")
        return RedirectResponse(url="/categories?msg=error", status_code=303)


@router.post("/categories/create")
async def categories_create(
    request: Request,
    name: str = Form(...),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/categories?msg=csrf_error", status_code=303)

    name = name.strip()
    if not name or len(name) > 80:
        return RedirectResponse(url="/categories?msg=invalid_name", status_code=303)

    try:
        db = get_web_db(int(user["sub"]), user.get("org_db"))
        conn = db.get_connection()
        exists = conn.execute(
            "SELECT COUNT(*) FROM products WHERE category = ?", (name,)
        ).fetchone()[0]
        conn.close()
        if exists > 0:
            return RedirectResponse(url="/categories?msg=duplicate", status_code=303)
        # Category only exists when a product uses it — redirect to create product with preset
        return RedirectResponse(
            url=f"/products/new?category={name}",
            status_code=303,
        )
    except Exception as exc:
        import logging
        logging.error(f"categories_create error: {exc}")
        return RedirectResponse(url="/categories?msg=error", status_code=303)
