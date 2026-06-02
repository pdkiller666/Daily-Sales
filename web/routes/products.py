import io
import logging
import uuid as _uuid
from fastapi import APIRouter, Request, File, UploadFile, Form
from fastapi.responses import RedirectResponse

router = APIRouter()

BULK_IMPORT_MAX = 100   # max products per upload (mirrors bot)
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
PREVIEW_PAGE_SIZE = 20  # rows per preview page

# In-memory import session store — bound to user:
# {session_id: {"telegram_id": int, "items": [...], "skipped": int}}
_import_sessions: dict[str, dict] = {}


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


@router.get("/products/import")
def products_import_page(
    request: Request,
    session_id: str = "",
    page: int = 1,
    error: str = "",
):
    from web.auth import get_session_user
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)

    telegram_id = int(user["sub"])

    # Base context for upload form
    from web.auth import get_csrf_token
    ctx: dict = {
        "request": request, "user": user, "is_admin": True,
        "max_items": BULK_IMPORT_MAX,
        "csrf_token": get_csrf_token(request),
        "preview": None, "error": error or None,
        "session_id": "", "total": 0, "skipped": 0,
        "page": 1, "page_count": 1, "has_prev": False, "has_next": False,
    }

    if session_id:
        sess = _import_sessions.get(session_id)
        # Security: bind session to authenticated user
        if not sess or sess.get("telegram_id") != telegram_id:
            ctx["error"] = "Сессия не найдена или устарела. Загрузите файл снова."
        else:
            items = sess["items"]
            total = len(items)
            page_count = max(1, (total + PREVIEW_PAGE_SIZE - 1) // PREVIEW_PAGE_SIZE)
            page = max(1, min(page, page_count))
            offset = (page - 1) * PREVIEW_PAGE_SIZE
            ctx.update({
                "preview": items[offset: offset + PREVIEW_PAGE_SIZE],
                "total": total,
                "skipped": sess.get("skipped", 0),
                "session_id": session_id,
                "page": page,
                "page_count": page_count,
                "has_prev": page > 1,
                "has_next": page < page_count,
            })

    return request.app.state.templates.TemplateResponse(
        request, "products/import.html", ctx,
    )


@router.post("/products/import")
async def products_import_upload(
    request: Request,
    file: UploadFile = File(...),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/products/import", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)

    telegram_id = int(user["sub"])

    def _err(msg: str):
        return RedirectResponse(
            url=f"/products/import?error={quote(msg)}",
            status_code=302,
        )

    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return _err("Принимаются только файлы .xlsx (Excel 2007+).")

    try:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
    except Exception as e:
        return _err(f"Ошибка чтения файла: {e}")

    if len(raw) > MAX_UPLOAD_BYTES:
        return _err("Файл слишком большой (максимум 5 МБ).")

    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        valid: list[dict] = []
        skipped = 0
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 3:
                skipped += 1
                continue
            name = str(row[0]).strip() if row[0] is not None else ""
            category = str(row[1]).strip() if row[1] is not None else "Без категории"
            try:
                price = float(str(row[2]).replace(",", ".").strip())
            except (ValueError, TypeError):
                skipped += 1
                continue
            if not name or len(name) < 2 or price <= 0:
                skipped += 1
                continue
            valid.append({
                "name": name[:50],
                "category": category[:30] or "Без категории",
                "price": price,
            })
            if len(valid) >= BULK_IMPORT_MAX:
                break
        wb.close()
    except Exception as e:
        logging.error(f"products_import_upload parse error: {e}")
        return _err(f"Ошибка разбора файла: {e}")

    if not valid:
        return _err(
            "Файл не содержит подходящих строк. "
            "Убедитесь, что столбцы: A=Название, B=Категория, C=Цена (число > 0)."
        )

    session_id = str(_uuid.uuid4())
    # Bind session to the authenticated user's telegram_id
    _import_sessions[session_id] = {
        "telegram_id": telegram_id,
        "items": valid,
        "skipped": skipped,
    }

    return RedirectResponse(
        url=f"/products/import?session_id={session_id}&page=1",
        status_code=302,
    )


@router.post("/products/import/confirm")
def products_import_confirm(
    request: Request,
    session_id: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/products/import", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    sess = _import_sessions.pop(session_id, None)
    if not sess or sess.get("telegram_id") != telegram_id:
        return RedirectResponse(
            url="/products/import?error=Сессия+не+найдена+или+устарела.+Загрузите+файл+снова.",
            status_code=302,
        )

    try:
        db = get_web_db(telegram_id, org_db)
        added, _skipped_names = db.add_products_bulk(sess["items"])
    except Exception as e:
        logging.error(f"products_import_confirm bulk insert: {e}")
        return RedirectResponse(
            url=f"/products/import?error={quote(str(e))}",
            status_code=302,
        )

    return RedirectResponse(url=f"/products?imported={added}", status_code=302)


@router.get("/products/new")
def products_new_form(request: Request):
    """Show form to create a new product (admin only)."""
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    categories = sorted({p[2] for p in (db.get_all_products() or []) if p[2]})

    return request.app.state.templates.TemplateResponse(request, "products/form.html", {
        "request": request, "user": user, "is_admin": True,
        "csrf_token": get_csrf_token(request),
        "categories": categories,
        "form_data": None, "error": None, "is_edit": False,
    })


@router.post("/products/create")
def products_create(
    request: Request,
    csrf_token: str = Form(default=""),
    name: str = Form(...),
    category: str = Form(default=""),
    price: str = Form(default="0"),
    description: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        from fastapi.responses import Response
        return Response(content="Недействительный CSRF-токен. Обновите страницу.", status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    categories = sorted({p[2] for p in (db.get_all_products() or []) if p[2]})

    def _re_render(err, fd=None):
        return request.app.state.templates.TemplateResponse(request, "products/form.html", {
            "request": request, "user": user, "is_admin": True,
            "csrf_token": get_csrf_token(request),
            "categories": categories,
            "form_data": fd or {"name": name, "category": category, "price": price, "description": description},
            "error": err, "is_edit": False,
        })

    name_clean = name.strip()
    if not name_clean:
        return _re_render("Введите название товара.")
    try:
        price_val = float(price.replace(",", ".").strip() or "0")
        if price_val < 0:
            raise ValueError
    except ValueError:
        return _re_render("Цена должна быть числом ≥ 0.")

    try:
        new_id = db.add_product(
            name=name_clean,
            category=category.strip() or None,
            price=price_val,
            description=description.strip() or None,
        )
        if not new_id:
            return _re_render("Не удалось создать товар. Попробуйте ещё раз.")
        return RedirectResponse(url=f"/products/{new_id}?success=Товар+добавлен", status_code=303)
    except Exception as exc:
        return _re_render(f"Ошибка: {exc}")


@router.get("/products/{product_id}/edit")
def products_edit_form(request: Request, product_id: int):
    """Show edit form for an existing product (admin only)."""
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/products/{product_id}", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    product = db.get_product(product_id)
    if not product:
        return RedirectResponse(url="/products", status_code=302)

    categories = sorted({p[2] for p in (db.get_all_products() or []) if p[2]})
    return request.app.state.templates.TemplateResponse(request, "products/form.html", {
        "request": request, "user": user, "is_admin": True,
        "csrf_token": get_csrf_token(request),
        "categories": categories,
        "form_data": {
            "name": product[1] or "",
            "category": product[2] or "",
            "price": str(int(product[3]) if product[3] == int(product[3]) else product[3]),
            "description": product[6] if len(product) > 6 else "",
        },
        "error": None, "is_edit": True,
        "edit_id": product_id,
        "product_name": product[1] or "Товар",
    })


@router.post("/products/{product_id}/update")
def products_update(
    request: Request,
    product_id: int,
    csrf_token: str = Form(default=""),
    name: str = Form(...),
    category: str = Form(default=""),
    price: str = Form(default="0"),
    description: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/products/{product_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        from fastapi.responses import Response
        return Response(content="Недействительный CSRF-токен. Обновите страницу.", status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    categories = sorted({p[2] for p in (db.get_all_products() or []) if p[2]})

    def _re_render(err):
        return request.app.state.templates.TemplateResponse(request, "products/form.html", {
            "request": request, "user": user, "is_admin": True,
            "csrf_token": get_csrf_token(request),
            "categories": categories,
            "form_data": {"name": name, "category": category, "price": price, "description": description},
            "error": err, "is_edit": True,
            "edit_id": product_id, "product_name": name,
        })

    name_clean = name.strip()
    if not name_clean:
        return _re_render("Введите название товара.")
    try:
        price_val = float(price.replace(",", ".").strip() or "0")
        if price_val < 0:
            raise ValueError
    except ValueError:
        return _re_render("Цена должна быть числом ≥ 0.")

    try:
        db.update_product(
            product_id,
            name=name_clean,
            category=category.strip() or "",
            price=price_val,
            description=description.strip() or "",
        )
        return RedirectResponse(url=f"/products/{product_id}?success=Сохранено", status_code=303)
    except Exception as exc:
        return _re_render(f"Ошибка: {exc}")


@router.post("/products/{product_id}/delete")
def products_delete(
    request: Request,
    product_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from fastapi.responses import Response

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/products/{product_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен. Обновите страницу.", status_code=403)

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        db.delete_product(product_id)
    except Exception as exc:
        logging.error(f"products_delete error: {exc}")

    return RedirectResponse(url="/products?success=Товар+удалён", status_code=303)


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
