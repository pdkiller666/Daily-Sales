import io
import logging
import os
import uuid as _uuid
from pathlib import Path
from fastapi import APIRouter, Request, File, UploadFile, Form
from fastapi.responses import RedirectResponse

_PHOTO_DIR = Path("web/static/product_photos")
_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _save_product_photo(upload: UploadFile, raw: bytes) -> str:
    """Save uploaded photo bytes to static dir, return web path like /static/product_photos/xxx.jpg."""
    ext = Path(upload.filename or "photo.jpg").suffix.lower()
    if ext not in _PHOTO_EXTS:
        ext = ".jpg"
    _PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{_uuid.uuid4().hex}{ext}"
    fpath = _PHOTO_DIR / fname
    fpath.write_bytes(raw)
    return f"/static/product_photos/{fname}"


def _delete_product_photo(photo_url: str):
    """Remove a web-uploaded photo file if it lives in our static dir."""
    if not photo_url or not photo_url.startswith("/static/product_photos/"):
        return
    try:
        fpath = Path("web") / photo_url.lstrip("/")
        if fpath.exists():
            fpath.unlink()
    except Exception:
        pass

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


@router.post("/products/import/text")
async def products_import_text(
    request: Request,
    csrf_token: str = Form(default=""),
    text_lines: str = Form(default=""),
):
    """Parse pasted product list and create products. Format: Name[, Category][, Price]"""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/products/import?error=csrf", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        items = []
        for raw_line in text_lines.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            name = parts[0] if parts else ""
            if len(name) < 2:
                continue
            category = ""
            price = 0.0
            if len(parts) == 2:
                try:
                    price = float(parts[1].replace(" ", "").replace("₽", ""))
                except ValueError:
                    category = parts[1]
            elif len(parts) >= 3:
                category = parts[1]
                try:
                    price = float(parts[2].replace(" ", "").replace("₽", ""))
                except ValueError:
                    pass
            items.append({"name": name, "category": category, "price": price, "quantity": 0})

        if not items:
            return RedirectResponse(url="/products/import?error=Список+пустой+или+нет+корректных+строк", status_code=302)

        added, _ = db.add_products_bulk(items)
        return RedirectResponse(url=f"/products?imported={added}", status_code=302)
    except Exception as e:
        logging.error(f"products_import_text: {e}")
        return RedirectResponse(url=f"/products/import?error={quote(str(e))}", status_code=302)


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
async def products_create(
    request: Request,
    csrf_token: str = Form(default=""),
    name: str = Form(...),
    category: str = Form(default=""),
    price: str = Form(default="0"),
    description: str = Form(default=""),
    photo: UploadFile = File(default=None),
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
            "form_data": fd or {"name": name, "category": category, "price": price, "description": description, "photo_url": ""},
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

    # Handle photo upload
    photo_url = None
    if photo and photo.filename:
        ext = Path(photo.filename).suffix.lower()
        if ext not in _PHOTO_EXTS:
            return _re_render("Допустимые форматы фото: JPG, PNG, WebP.")
        try:
            raw = await photo.read(_PHOTO_MAX_BYTES + 1)
            if len(raw) > _PHOTO_MAX_BYTES:
                return _re_render("Фото слишком большое (максимум 5 МБ).")
            photo_url = _save_product_photo(photo, raw)
        except Exception as exc:
            logging.error(f"products_create photo save: {exc}")
            return _re_render(f"Ошибка сохранения фото: {exc}")

    try:
        new_id = db.add_product(
            name=name_clean,
            category=category.strip() or None,
            price=price_val,
            description=description.strip() or None,
            photo_file_id=photo_url,
        )
        if not new_id:
            if photo_url:
                _delete_product_photo(photo_url)
            return _re_render("Не удалось создать товар. Попробуйте ещё раз.")
        return RedirectResponse(url=f"/products/{new_id}?success=Товар+добавлен", status_code=303)
    except Exception as exc:
        if photo_url:
            _delete_product_photo(photo_url)
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

    # photo_file_id[5] — only show as photo if it's a web-uploaded local path
    photo_file_id = product[5] if len(product) > 5 else ""
    photo_url = photo_file_id if (photo_file_id and str(photo_file_id).startswith("/static/")) else ""

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
            "photo_url": photo_url,
        },
        "error": None, "is_edit": True,
        "edit_id": product_id,
        "product_name": product[1] or "Товар",
    })


@router.post("/products/{product_id}/update")
async def products_update(
    request: Request,
    product_id: int,
    csrf_token: str = Form(default=""),
    name: str = Form(...),
    category: str = Form(default=""),
    price: str = Form(default="0"),
    description: str = Form(default=""),
    photo: UploadFile = File(default=None),
    remove_photo: str = Form(default=""),
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

    # Fetch current photo to allow deletion / replacement
    existing = db.get_product(product_id)
    cur_photo = (existing[5] if existing and len(existing) > 5 else "") or ""
    cur_photo_url = cur_photo if cur_photo.startswith("/static/") else ""

    def _re_render(err):
        return request.app.state.templates.TemplateResponse(request, "products/form.html", {
            "request": request, "user": user, "is_admin": True,
            "csrf_token": get_csrf_token(request),
            "categories": categories,
            "form_data": {"name": name, "category": category, "price": price, "description": description, "photo_url": cur_photo_url},
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

    # Resolve new photo_file_id value
    new_photo_url: str | None = None  # None = don't change; "" = remove; "/static/..." = new file
    if photo and photo.filename:
        ext = Path(photo.filename).suffix.lower()
        if ext not in _PHOTO_EXTS:
            return _re_render("Допустимые форматы фото: JPG, PNG, WebP.")
        try:
            raw = await photo.read(_PHOTO_MAX_BYTES + 1)
            if len(raw) > _PHOTO_MAX_BYTES:
                return _re_render("Фото слишком большое (максимум 5 МБ).")
            new_photo_url = _save_product_photo(photo, raw)
            _delete_product_photo(cur_photo_url)  # remove old if it was web-uploaded
        except Exception as exc:
            logging.error(f"products_update photo save: {exc}")
            return _re_render(f"Ошибка сохранения фото: {exc}")
    elif remove_photo == "1" and cur_photo_url:
        _delete_product_photo(cur_photo_url)
        new_photo_url = ""

    try:
        kwargs: dict = dict(
            name=name_clean,
            category=category.strip() or "",
            price=price_val,
            description=description.strip() or "",
        )
        if new_photo_url is not None:
            kwargs["photo_file_id"] = new_photo_url
        db.update_product(product_id, **kwargs)
        return RedirectResponse(url=f"/products/{product_id}?success=Сохранено", status_code=303)
    except Exception as exc:
        if new_photo_url and new_photo_url.startswith("/static/"):
            _delete_product_photo(new_photo_url)
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
        "chart_labels": [], "chart_data": [],
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

        # 7-day chart data
        try:
            from datetime import timedelta
            chart_labels = []
            chart_data = []
            conn2 = db.get_connection()
            cur2 = conn2.cursor()
            for i in range(6, -1, -1):
                d = today - timedelta(days=i)
                ds = d.isoformat()
                cur2.execute(
                    "SELECT COALESCE(SUM(quantity_sold * sale_price),0) FROM sales WHERE product_id=? AND date(sale_date)=?",
                    (product_id, ds)
                )
                val = cur2.fetchone()
                chart_labels.append(d.strftime('%d.%m'))
                chart_data.append(int(float((val[0] if val else 0) or 0)))
            conn2.close()
            ctx["chart_labels"] = chart_labels
            ctx["chart_data"] = chart_data
        except Exception:
            ctx["chart_labels"] = []
            ctx["chart_data"] = []

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "products/detail.html", ctx
    )
