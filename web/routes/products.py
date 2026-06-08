import hashlib
import io
import json
import logging
import os
import uuid as _uuid
from pathlib import Path
from typing import List
from fastapi import APIRouter, Request, File, UploadFile, Form
from fastapi.responses import RedirectResponse, JSONResponse

_PHOTO_DIR = Path("web/static/product_photos")
_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_GALLERY_MAX = 10  # максимум фото на товар


def _get_org_hash(org_db: str) -> str:
    """Короткий хэш от имени org db-файла для изоляции папок по организациям."""
    return hashlib.md5(os.path.basename(org_db or "shop_bot.db").encode()).hexdigest()[:8]


def _is_valid_image(raw: bytes) -> bool:
    """Validate image by magic bytes — prevents disguised file uploads."""
    if len(raw) < 12:
        return False
    if raw[:3] == b'\xff\xd8\xff':
        return True  # JPEG
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return True  # PNG
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return True  # WebP
    return False


def _save_product_photo(upload: UploadFile, raw: bytes, org_db: str = "") -> str:
    """Save uploaded photo bytes to static dir, return web path like /static/product_photos/org_hash/xxx.jpg."""
    ext = Path(upload.filename or "photo.jpg").suffix.lower()
    if ext not in _PHOTO_EXTS:
        ext = ".jpg"
    org_hash = _get_org_hash(org_db)
    save_dir = _PHOTO_DIR / org_hash
    save_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_uuid.uuid4().hex}{ext}"
    (save_dir / fname).write_bytes(raw)
    return f"/static/product_photos/{org_hash}/{fname}"


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
PRODUCTS_PAGE_SIZE = 50  # rows per products list page

# In-memory import session store — bound to user:
# {session_id: {"telegram_id": int, "items": [...], "skipped": int, "_ts": float}}
_import_sessions: dict[str, dict] = {}
_IMPORT_SESSION_TTL = 3600  # 1 hour


def _cleanup_import_sessions() -> None:
    """Evict import sessions older than TTL to prevent unbounded memory growth."""
    import time as _time
    now = _time.time()
    stale = [k for k, v in _import_sessions.items()
             if now - v.get("_ts", 0) > _IMPORT_SESSION_TTL]
    for k in stale:
        _import_sessions.pop(k, None)


@router.get("/products")
def products_page(request: Request, q: str = "", category: str = "", page: int = 1):
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
        "page": 1, "total_pages": 1, "base_url": "/products",
        "abc_grades": {},
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

        total = len(products)
        total_pages = max(1, (total + PRODUCTS_PAGE_SIZE - 1) // PRODUCTS_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PRODUCTS_PAGE_SIZE

        # Build base_url for pagination links (preserves filters)
        parts = []
        if q:
            from urllib.parse import quote
            parts.append(f"q={quote(q)}")
        if category:
            from urllib.parse import quote
            parts.append(f"category={quote(category)}")
        base_url = "/products?" + "&".join(parts) if parts else "/products?"

        ctx["products"] = products[start: start + PRODUCTS_PAGE_SIZE]
        ctx["categories"] = categories
        ctx["stock"] = stock
        ctx["total_count"] = total
        ctx["page"] = page
        ctx["total_pages"] = total_pages
        ctx["base_url"] = base_url

        # ── ABC-анализ: выручка по товарам за 90 дней ────────────────────────
        try:
            from datetime import date as _date, timedelta as _td
            _start90 = (_date.today() - _td(days=90)).isoformat()
            _conn = db.get_connection()
            _rows = _conn.execute("""
                SELECT product_id, SUM(quantity_sold * sale_price) AS revenue
                FROM sales
                WHERE date(sale_date) >= ?
                GROUP BY product_id
                HAVING revenue > 0
                ORDER BY revenue DESC
            """, (_start90,)).fetchall()
            _conn.close()
            if _rows:
                _total = sum(r[1] for r in _rows)
                _cumul = 0.0
                _abc: dict[int, str] = {}
                for _r in _rows:
                    _cumul += _r[1]
                    _pct = _cumul / _total
                    _abc[_r[0]] = 'A' if _pct <= 0.80 else ('B' if _pct <= 0.95 else 'C')
                ctx["abc_grades"] = _abc
        except Exception:
            pass

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

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
        logging.error(f"products_import_upload read error: {e}")
        return _err("Не удалось прочитать файл. Убедитесь, что файл не повреждён.")

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
        return _err("Не удалось разобрать файл. Убедитесь, что это корректный .xlsx файл Excel 2007+.")

    if not valid:
        return _err(
            "Файл не содержит подходящих строк. "
            "Убедитесь, что столбцы: A=Название, B=Категория, C=Цена (число > 0)."
        )

    _cleanup_import_sessions()  # evict stale sessions before creating a new one
    import time as _time
    session_id = str(_uuid.uuid4())
    # Bind session to the authenticated user's telegram_id
    _import_sessions[session_id] = {
        "telegram_id": telegram_id,
        "items": valid,
        "skipped": skipped,
        "_ts": _time.time(),
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
        return RedirectResponse(url="/products/import?error=Ошибка+импорта.+Попробуйте+ещё+раз.", status_code=302)


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
            url="/products/import?error=Ошибка+сохранения+товаров.+Попробуйте+ещё+раз.",
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
    photos: List[UploadFile] = File(default=[]),
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
    org_db = user.get("org_db") or ""
    db = get_web_db(telegram_id, org_db)
    categories = sorted({p[2] for p in (db.get_all_products() or []) if p[2]})

    def _re_render(err, fd=None):
        return request.app.state.templates.TemplateResponse(request, "products/form.html", {
            "request": request, "user": user, "is_admin": True,
            "csrf_token": get_csrf_token(request),
            "categories": categories,
            "form_data": fd or {"name": name, "category": category, "price": price, "description": description, "existing_photos": []},
            "error": err, "is_edit": False,
        })

    # Subscription limit
    try:
        from subscription_utils import check_product_limit
        _ok, _msg = check_product_limit(telegram_id)
        if not _ok:
            return _re_render(_msg or "Достигнут лимит товаров по вашему тарифу.")
    except Exception:
        pass

    name_clean = name.strip()
    if not name_clean:
        return _re_render("Введите название товара.")
    try:
        price_val = float(price.replace(",", ".").strip() or "0")
        if price_val < 0:
            raise ValueError
    except ValueError:
        return _re_render("Цена должна быть числом ≥ 0.")

    # Handle multiple photo uploads
    valid_photos = [p for p in (photos or []) if p and p.filename]
    if len(valid_photos) > _GALLERY_MAX:
        return _re_render(f"Максимум {_GALLERY_MAX} фотографий.")

    saved_urls: list[str] = []
    for upload in valid_photos:
        ext = Path(upload.filename).suffix.lower()
        if ext not in _PHOTO_EXTS:
            for u in saved_urls:
                _delete_product_photo(u)
            return _re_render("Допустимые форматы фото: JPG, PNG, WebP.")
        try:
            raw = await upload.read(_PHOTO_MAX_BYTES + 1)
            if len(raw) > _PHOTO_MAX_BYTES:
                for u in saved_urls:
                    _delete_product_photo(u)
                return _re_render("Одно из фото слишком большое (максимум 5 МБ).")
            if not _is_valid_image(raw):
                for u in saved_urls:
                    _delete_product_photo(u)
                return _re_render("Один из файлов не является изображением. Загрузите JPG, PNG или WebP.")
            saved_urls.append(_save_product_photo(upload, raw, org_db))
        except Exception as exc:
            logging.error(f"products_create photo save: {exc}")
            for u in saved_urls:
                _delete_product_photo(u)
            return _re_render("Не удалось сохранить фото. Попробуйте ещё раз.")

    first_photo = saved_urls[0] if saved_urls else None
    try:
        new_id = db.add_product(
            name=name_clean,
            category=category.strip() or None,
            price=price_val,
            description=description.strip() or None,
            photo_file_id=first_photo,
        )
        if not new_id:
            for u in saved_urls:
                _delete_product_photo(u)
            return _re_render("Не удалось создать товар. Попробуйте ещё раз.")
        for url in saved_urls:
            try:
                db.add_product_photo(new_id, url, source='web')
            except Exception:
                pass
        return RedirectResponse(url=f"/products/{new_id}?success=Товар+добавлен", status_code=303)
    except Exception as exc:
        for u in saved_urls:
            _delete_product_photo(u)
        logging.error(f"products_create db error: {exc}")
        return _re_render("Не удалось создать товар. Попробуйте ещё раз.")


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
    existing_photos = db.get_product_photos(product_id) or []
    return request.app.state.templates.TemplateResponse(request, "products/form.html", {
        "request": request, "user": user, "is_admin": True,
        "csrf_token": get_csrf_token(request),
        "categories": categories,
        "form_data": {
            "name": product[1] or "",
            "category": product[2] or "",
            "price": str(int(product[3]) if product[3] == int(product[3]) else product[3]),
            "description": product[6] if len(product) > 6 else "",
            "existing_photos": existing_photos,
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
    photos: List[UploadFile] = File(default=[]),
    delete_photo_ids: str = Form(default=""),
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
    org_db = user.get("org_db") or ""
    db = get_web_db(telegram_id, org_db)
    categories = sorted({p[2] for p in (db.get_all_products() or []) if p[2]})
    existing_photos = db.get_product_photos(product_id) or []

    def _re_render(err):
        return request.app.state.templates.TemplateResponse(request, "products/form.html", {
            "request": request, "user": user, "is_admin": True,
            "csrf_token": get_csrf_token(request),
            "categories": categories,
            "form_data": {"name": name, "category": category, "price": price, "description": description,
                          "existing_photos": existing_photos},
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

    # Delete marked photos
    to_delete_ids: list[int] = []
    try:
        to_delete_ids = [int(x) for x in delete_photo_ids.split(",") if x.strip().isdigit()]
    except Exception:
        pass
    for pid in to_delete_ids:
        try:
            url = db.delete_product_photo(pid)
            if url:
                _delete_product_photo(url)
        except Exception:
            pass

    # Upload new photos
    valid_photos = [p for p in (photos or []) if p and p.filename]
    remaining_count = len(db.get_product_photos(product_id) or [])
    if remaining_count + len(valid_photos) > _GALLERY_MAX:
        return _re_render(f"Максимум {_GALLERY_MAX} фотографий на товар.")

    saved_urls: list[str] = []
    for upload in valid_photos:
        ext = Path(upload.filename).suffix.lower()
        if ext not in _PHOTO_EXTS:
            for u in saved_urls:
                _delete_product_photo(u)
            return _re_render("Допустимые форматы фото: JPG, PNG, WebP.")
        try:
            raw = await upload.read(_PHOTO_MAX_BYTES + 1)
            if len(raw) > _PHOTO_MAX_BYTES:
                for u in saved_urls:
                    _delete_product_photo(u)
                return _re_render("Одно из фото слишком большое (максимум 5 МБ).")
            if not _is_valid_image(raw):
                for u in saved_urls:
                    _delete_product_photo(u)
                return _re_render("Один из файлов не является изображением. Загрузите JPG, PNG или WebP.")
            saved_urls.append(_save_product_photo(upload, raw, org_db))
        except Exception as exc:
            logging.error(f"products_update photo save: {exc}")
            for u in saved_urls:
                _delete_product_photo(u)
            return _re_render("Не удалось сохранить фото. Попробуйте ещё раз.")

    try:
        # Update product fields
        all_photos = db.get_product_photos(product_id) or []
        first_photo_url = all_photos[0][2] if all_photos else (saved_urls[0] if saved_urls else None)
        kwargs: dict = dict(
            name=name_clean,
            category=category.strip() or "",
            price=price_val,
            description=description.strip() or "",
        )
        if first_photo_url is not None:
            kwargs["photo_file_id"] = first_photo_url
        elif not all_photos and not saved_urls:
            kwargs["photo_file_id"] = None
        db.update_product(product_id, **kwargs)
        for url in saved_urls:
            try:
                db.add_product_photo(product_id, url, source='web')
            except Exception:
                pass
        return RedirectResponse(url=f"/products/{product_id}?success=Сохранено", status_code=303)
    except Exception as exc:
        for u in saved_urls:
            _delete_product_photo(u)
        logging.error(f"products_update db error: {exc}")
        return _re_render("Не удалось сохранить товар. Попробуйте ещё раз.")


@router.post("/products/{product_id}/photos/{photo_id}/delete")
async def product_photo_delete(request: Request, product_id: int, photo_id: int):
    """AJAX: удалить одно фото товара."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    user = get_session_user(request)
    if not user or user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        csrf = body.get("csrf_token", "")
    except Exception:
        csrf = ""
    if not verify_csrf_token(request, csrf):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)
    db = get_web_db(int(user["sub"]), user.get("org_db") or "")
    try:
        url = db.delete_product_photo(photo_id)
        if url:
            _delete_product_photo(url)
        # Update products.photo_file_id to first remaining or None
        remaining = db.get_product_photos(product_id) or []
        db.update_product(product_id, photo_file_id=remaining[0][2] if remaining else None)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logging.error(f"product_photo_delete: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/products/{product_id}/photos/reorder")
async def product_photos_reorder(request: Request, product_id: int):
    """AJAX: изменить порядок фотографий."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    user = get_session_user(request)
    if not user or user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        csrf = body.get("csrf_token", "")
        photo_ids = [int(x) for x in body.get("photo_ids", [])]
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    if not verify_csrf_token(request, csrf):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)
    db = get_web_db(int(user["sub"]), user.get("org_db") or "")
    try:
        db.reorder_product_photos(photo_ids)
        remaining = db.get_product_photos(product_id) or []
        if remaining:
            db.update_product(product_id, photo_file_id=remaining[0][2])
        return JSONResponse({"ok": True})
    except Exception as exc:
        logging.error(f"product_photos_reorder: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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
        "user_tz": "Europe/Moscow",
    }

    try:
        db = get_web_db(telegram_id, org_db)
        try:
            ctx["user_tz"] = db.get_user_timezone(telegram_id) or "Europe/Moscow"
        except Exception:
            pass

        product = db.get_product(product_id)
        if not product:
            return RedirectResponse(url="/products", status_code=302)

        # products: id[0] name[1] category[2] price[3] created_at[4] photo_file_id[5] description[6]
        ctx["product"] = product
        ctx["product_photos"] = db.get_product_photos(product_id) or []

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
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "products/detail.html", ctx
    )


@router.post("/api/products/{product_id}/price")
def api_update_product_price(
    request: Request, product_id: int,
    price: int = Form(...),
    csrf_token: str = Form(default=""),
):
    """Inline price edit — admin-only AJAX endpoint."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user or user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)
    if price < 0:
        return JSONResponse({"ok": False, "error": "Цена не может быть отрицательной"})
    try:
        db = get_web_db(int(user["sub"]), user.get("org_db"))
        conn = db.get_connection()
        updated = conn.execute(
            "UPDATE products SET price = ? WHERE id = ?", (price, product_id)
        ).rowcount
        conn.commit()
        conn.close()
        if not updated:
            return JSONResponse({"ok": False, "error": "Товар не найден"}, status_code=404)
        price_fmt = f"{price:,}".replace(",", "\u00a0") + "\u00a0₽"
        return JSONResponse({"ok": True, "price_fmt": price_fmt})
    except Exception:
        return JSONResponse({"ok": False, "error": "Ошибка сохранения"}, status_code=500)
