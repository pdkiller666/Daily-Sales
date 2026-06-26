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
from timezone_utils import DEFAULT_TZ

_PHOTO_DIR = Path("web/static/product_photos")
_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_GALLERY_MAX = 10  # максимум фото на товар
_LOGO_DIR = Path("web/static/product_photos")  # логотип ценника рядом с фото товаров
_LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MB для логотипа


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


def _save_label_logo(raw: bytes, filename: str, org_db: str = "") -> str:
    """Save logo for label design; overwrites previous logo for this org.
    Returns web path like /static/product_photos/org_hash/label_logo.ext.
    """
    ext = Path(filename or "logo.png").suffix.lower()
    if ext not in _PHOTO_EXTS:
        ext = ".png"
    org_hash = _get_org_hash(org_db)
    save_dir = _LOGO_DIR / org_hash
    save_dir.mkdir(parents=True, exist_ok=True)
    for old in save_dir.glob("label_logo.*"):
        try:
            old.unlink()
        except Exception:
            pass
    fname = f"label_logo{ext}"
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

# Article import session store — bound to user:
# {session_id: {"telegram_id": int, "items": [...], "skipped": int, "_ts": float}}
_article_sessions: dict[str, dict] = {}
ARTICLE_IMPORT_MAX = 500  # max rows per article import upload


def _cleanup_import_sessions() -> None:
    """Evict import sessions older than TTL to prevent unbounded memory growth."""
    import time as _time
    now = _time.time()
    stale = [k for k, v in _import_sessions.items()
             if now - v.get("_ts", 0) > _IMPORT_SESSION_TTL]
    for k in stale:
        _import_sessions.pop(k, None)


def _cleanup_article_sessions() -> None:
    """Evict article import sessions older than TTL."""
    import time as _time
    now = _time.time()
    stale = [k for k, v in _article_sessions.items()
             if now - v.get("_ts", 0) > _IMPORT_SESSION_TTL]
    for k in stale:
        _article_sessions.pop(k, None)


def _filter_products(all_products: list, q: str = "", category: str = "") -> list:
    """Фильтрация списка товаров по категории и поисковому запросу.
    Общий источник правды для списка товаров и «печати всех по фильтру»,
    чтобы счётчик и фактический набор для печати совпадали 1:1.
    """
    q = (q or "").strip()
    category = (category or "").strip()
    products = list(all_products or [])
    if category:
        products = [p for p in products if p[2] == category]
    if q:
        ql = q.lower()
        products = [
            p for p in products
            if ql in (p[1] or "").lower()
            or ql in (p[2] or "").lower()
            or ql in (p[7] if len(p) > 7 and p[7] else "").lower()
            or ql in (p[8] if len(p) > 8 and p[8] else "").lower()
        ]
    return products


@router.get("/products")
def products_page(
    request: Request,
    q: str = "",
    category: str = "",
    page: int = 1,
    sort_col: str = "name",
    sort_order: str = "asc",
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    _cleanup_import_sessions()
    _cleanup_article_sessions()

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    _VALID_PROD_COLS = ("name", "category", "price", "stock")
    sort_col = sort_col if sort_col in _VALID_PROD_COLS else "name"
    sort_order = sort_order if sort_order in ("asc", "desc") else "asc"

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "products": [], "categories": [],
        "selected_category": category, "q": q,
        "sort_col": sort_col, "sort_order": sort_order,
        "stock": {}, "total_count": 0, "error": None,
        "page": 1, "total_pages": 1, "base_url": "/products",
        "abc_grades": {},
    }

    try:
        db = get_web_db(telegram_id, org_db)

        all_products = db.get_all_products() or []
        stock: dict[int, int] = db.get_stock_totals()

        categories = sorted({p[2] for p in all_products if p[2]})

        # products: id[0] name[1] category[2] price[3] created_at[4]
        products = _filter_products(all_products, q=q, category=category)

        # Apply user-requested sort
        rev = (sort_order == "desc")
        if sort_col == "category":
            products.sort(key=lambda p: ((p[2] or "").lower(), (p[1] or "").lower()), reverse=rev)
        elif sort_col == "price":
            products.sort(key=lambda p: float(p[3] or 0), reverse=rev)
        elif sort_col == "stock":
            products.sort(key=lambda p: stock.get(p[0], 0), reverse=rev)
        else:  # name (default)
            products.sort(key=lambda p: (p[1] or "").lower(), reverse=rev)

        total = len(products)
        total_pages = max(1, (total + PRODUCTS_PAGE_SIZE - 1) // PRODUCTS_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PRODUCTS_PAGE_SIZE

        # Build base_url for pagination links (preserves filters + sort)
        parts = []
        if q:
            from urllib.parse import quote
            parts.append(f"q={quote(q)}")
        if category:
            from urllib.parse import quote
            parts.append(f"category={quote(category)}")
        parts.append(f"sort_col={sort_col}&sort_order={sort_order}")
        base_url = "/products?" + "&".join(parts)

        ctx["products"] = products[start: start + PRODUCTS_PAGE_SIZE]
        ctx["categories"] = categories
        ctx["stock"] = stock
        ctx["total_count"] = total
        ctx["page"] = page
        ctx["total_pages"] = total_pages
        ctx["base_url"] = base_url
        ctx["is_owner"] = user.get("role") in ("owner", "super_admin")
        ctx["label_settings"] = _get_label_settings_safe(db)
        ctx["label_size_options"] = _label_size_options()
        ctx["first_product_id"] = all_products[0][0] if all_products else None
        try:
            from billing_utils import is_extension_denied as _ied, has_module as _hm
            ctx["barcode_locked"] = _ied(telegram_id, "barcodes")
            ctx["labels_locked"] = _ied(telegram_id, "labels")
            ctx["ai_assistant_ok"] = _hm(telegram_id, "ai_assistant")
        except Exception:
            ctx["barcode_locked"] = False
            ctx["labels_locked"] = False
            ctx["ai_assistant_ok"] = False

        # ── ABC-анализ: выручка по товарам за 90 дней ────────────────────────
        try:
            from datetime import date as _date, timedelta as _td
            _start90 = (_date.today() - _td(days=90)).isoformat()
            _conn = db.get_connection()
            try:
                _rows = _conn.execute("""
                    SELECT product_id, SUM(quantity_sold * sale_price) AS revenue
                    FROM sales
                    WHERE date(sale_date) >= ?
                    GROUP BY product_id
                    HAVING revenue > 0
                    ORDER BY revenue DESC
                """, (_start90,)).fetchall()
            finally:
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
    try:
        _nets = db.get_all_trade_networks() or []
    except Exception:
        _nets = []
    network_variants = (
        [{"network": n, "article": "", "barcode": ""} for n in _nets]
        if len(_nets) >= 2 else []
    )

    return request.app.state.templates.TemplateResponse(request, "products/form.html", {
        "request": request, "user": user, "is_admin": True,
        "csrf_token": get_csrf_token(request),
        "categories": categories,
        "network_variants": network_variants,
        "form_data": None, "error": None, "is_edit": False,
    })


@router.post("/products/create")
async def products_create(
    request: Request,
    csrf_token: str = Form(default=""),
    name: str = Form(...),
    category: str = Form(default=""),
    price: str = Form(default="0"),
    old_price: str = Form(default=""),
    description: str = Form(default=""),
    article: str = Form(default=""),
    barcode: str = Form(default=""),
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

    # Варианты по торговым сетям из формы (сохраняем ввод при ре-рендере ошибки)
    try:
        _allowed_nets = set(db.get_all_trade_networks() or [])
    except Exception:
        _allowed_nets = set()
    _form = await request.form()
    _variant_inputs: list[dict] = []
    for nk in sorted(k for k in _form.keys() if k.startswith("var_net__")):
        idx = nk[len("var_net__"):]
        net_name = (_form.get(nk) or "").strip()
        if not net_name or net_name not in _allowed_nets:
            continue
        _variant_inputs.append({
            "network": net_name,
            "article": (_form.get(f"var_article__{idx}") or "").strip(),
            "barcode": (_form.get(f"var_barcode__{idx}") or "").strip(),
        })

    def _re_render(err, fd=None):
        return request.app.state.templates.TemplateResponse(request, "products/form.html", {
            "request": request, "user": user, "is_admin": True,
            "csrf_token": get_csrf_token(request),
            "categories": categories,
            "network_variants": _variant_inputs,
            "form_data": fd or {"name": name, "category": category, "price": price,
                                "old_price": old_price,
                                "description": description, "article": article,
                                "barcode": barcode, "existing_photos": []},
            "error": err, "is_edit": False,
        })

    # Subscription limit
    try:
        from subscription_utils import check_product_limit
        _ok, _msg = check_product_limit(telegram_id)
        if not _ok:
            return _re_render(_msg or "Достигнут лимит товаров по вашему тарифу.")
    except Exception as _lim_err:
        logging.error(f"check_product_limit failed (fail-open): {_lim_err}")

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
        article_clean = article.strip().upper() if article and article.strip() else None
        barcode_clean = barcode.strip() or None
        try:
            old_price_val = float(old_price.replace(",", ".").strip()) if old_price.strip() else None
            if old_price_val is not None and old_price_val <= 0:
                old_price_val = None
        except ValueError:
            old_price_val = None
        new_id = db.add_product(
            name=name_clean,
            category=category.strip() or None,
            price=price_val,
            description=description.strip() or None,
            photo_file_id=first_photo,
            article=article_clean,
            barcode=barcode_clean,
            old_price=old_price_val,
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
        # Коды по торговым сетям (варианты товара)
        _vconf: list[str] = []
        for vi in _variant_inputs:
            if not (vi["article"] or vi["barcode"]):
                continue
            try:
                res = db.set_product_variant(new_id, vi["network"], vi["article"], vi["barcode"])
                if not res.get("ok") and res.get("error") == "barcode_conflict":
                    _vconf.append(vi["network"])
            except Exception as _ve:
                logging.error(f"products_create variants: {_ve}")
        if _vconf:
            _m = "Товар+добавлен,+но+штрихкод+занят+в+сетях:+" + ",+".join(_vconf)
            return RedirectResponse(url=f"/products/{new_id}?success={_m}", status_code=303)
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
    try:
        networks = db.get_all_trade_networks() or []
    except Exception:
        networks = []
    variants_map = {}
    if len(networks) >= 2:
        try:
            variants_map = db.get_product_variants_map(product_id) or {}
        except Exception:
            variants_map = {}
    network_variants = [
        {
            "network": net,
            "article": (variants_map.get(net) or {}).get("article") or "",
            "barcode": (variants_map.get(net) or {}).get("barcode") or "",
        }
        for net in networks
    ] if len(networks) >= 2 else []
    return request.app.state.templates.TemplateResponse(request, "products/form.html", {
        "request": request, "user": user, "is_admin": True,
        "csrf_token": get_csrf_token(request),
        "categories": categories,
        "form_data": {
            "name": product[1] or "",
            "category": product[2] or "",
            "price": str(int(product[3]) if product[3] == int(product[3]) else product[3]),
            "old_price": ("" if (len(product) <= 9 or product[9] in (None, 0))
                          else str(int(product[9]) if product[9] == int(product[9]) else product[9])),
            "description": product[6] if len(product) > 6 else "",
            "article": product[7] if len(product) > 7 else "",
            "barcode": product[8] if len(product) > 8 else "",
            "existing_photos": existing_photos,
        },
        "network_variants": network_variants,
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
    old_price: str = Form(default=""),
    description: str = Form(default=""),
    article: str = Form(default=""),
    barcode: str = Form(default=""),
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

    # Варианты по торговым сетям из формы (сохраняем ввод при ре-рендере ошибки)
    try:
        _allowed_nets = set(db.get_all_trade_networks() or [])
    except Exception:
        _allowed_nets = set()
    _form = await request.form()
    _variant_inputs: list[dict] = []
    for nk in sorted(k for k in _form.keys() if k.startswith("var_net__")):
        idx = nk[len("var_net__"):]
        net_name = (_form.get(nk) or "").strip()
        if not net_name or net_name not in _allowed_nets:
            continue
        _variant_inputs.append({
            "network": net_name,
            "article": (_form.get(f"var_article__{idx}") or "").strip(),
            "barcode": (_form.get(f"var_barcode__{idx}") or "").strip(),
        })

    def _re_render(err):
        return request.app.state.templates.TemplateResponse(request, "products/form.html", {
            "request": request, "user": user, "is_admin": True,
            "csrf_token": get_csrf_token(request),
            "categories": categories,
            "network_variants": _variant_inputs,
            "form_data": {"name": name, "category": category, "price": price, "old_price": old_price,
                          "description": description,
                          "article": article, "barcode": barcode, "existing_photos": existing_photos},
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
        article_clean = article.strip().upper() if article and article.strip() else ""
        barcode_clean = barcode.strip() or ""
        try:
            old_price_val = float(old_price.replace(",", ".").strip()) if old_price.strip() else 0
        except ValueError:
            old_price_val = 0
        kwargs: dict = dict(
            name=name_clean,
            category=category.strip() or "",
            price=price_val,
            description=description.strip() or "",
            article=article_clean or None,
            barcode=barcode_clean or None,
            old_price=old_price_val,
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
        # Коды по торговым сетям (варианты товара)
        variant_conflicts: list[str] = []
        for vi in _variant_inputs:
            try:
                res = db.set_product_variant(product_id, vi["network"], vi["article"], vi["barcode"])
                if not res.get("ok") and res.get("error") == "barcode_conflict":
                    variant_conflicts.append(vi["network"])
            except Exception as _ve:
                logging.error(f"products_update variants: {_ve}")
        if variant_conflicts:
            _msg = "Сохранено,+но+штрихкод+занят+в+сетях:+" + ",+".join(variant_conflicts)
            return RedirectResponse(url=f"/products/{product_id}?success={_msg}", status_code=303)
        return RedirectResponse(url=f"/products/{product_id}?success=Сохранено", status_code=303)
    except Exception as exc:
        for u in saved_urls:
            _delete_product_photo(u)
        logging.error(f"products_update db error: {exc}")
        return _re_render("Не удалось сохранить товар. Попробуйте ещё раз.")


@router.post("/products/bulk-assign-articles")
async def bulk_assign_articles(request: Request):
    """Присвоить авто-артикулы всем товарам без артикула (только admin/owner)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import is_extension_denied
    user = get_session_user(request)
    if not user or user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if is_extension_denied(int(user["sub"]), "barcodes"):
        return JSONResponse({"ok": False, "error": "subscription_required"}, status_code=403)
    try:
        body = await request.json()
        csrf = body.get("csrf_token", "")
    except Exception:
        csrf = ""
    if not verify_csrf_token(request, csrf):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)
    db = get_web_db(int(user["sub"]), user.get("org_db") or "")
    try:
        count = db.bulk_assign_articles()
        return JSONResponse({"ok": True, "count": count})
    except Exception as exc:
        logging.error(f"bulk_assign_articles: {exc}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера."}, status_code=500)


@router.get("/products/import-articles")
def products_import_articles_page(
    request: Request,
    session_id: str = "",
    page: int = 1,
    error: str = "",
):
    """Show article import upload form or preview."""
    from web.auth import get_session_user, get_csrf_token
    from billing_utils import is_extension_denied

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)
    if is_extension_denied(int(user["sub"]), "barcodes"):
        return RedirectResponse(url="/subscription?need=barcodes", status_code=302)

    telegram_id = int(user["sub"])

    ctx: dict = {
        "request": request, "user": user, "is_admin": True,
        "max_items": ARTICLE_IMPORT_MAX,
        "csrf_token": get_csrf_token(request),
        "preview": None, "error": error or None,
        "session_id": "", "total": 0, "skipped": 0,
        "page": 1, "page_count": 1, "has_prev": False, "has_next": False,
    }

    if session_id:
        sess = _article_sessions.get(session_id)
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
        request, "products/import_articles.html", ctx,
    )


@router.post("/products/import-articles")
async def products_import_articles_upload(
    request: Request,
    file: UploadFile = File(...),
    csrf_token: str = Form(default=""),
):
    """Parse xlsx with (Article, Product Name) columns, store session, redirect to preview."""
    from web.auth import get_session_user, verify_csrf_token
    from urllib.parse import quote
    from billing_utils import is_extension_denied

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/products/import-articles", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)
    if is_extension_denied(int(user["sub"]), "barcodes"):
        return RedirectResponse(url="/subscription?need=barcodes", status_code=302)

    telegram_id = int(user["sub"])

    def _err(msg: str):
        return RedirectResponse(
            url=f"/products/import-articles?error={quote(msg)}",
            status_code=302,
        )

    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return _err("Принимаются только файлы .xlsx (Excel 2007+).")

    try:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
    except Exception as e:
        logging.error(f"products_import_articles_upload read error: {e}")
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
            if not row or len(row) < 2:
                skipped += 1
                continue
            article_raw = str(row[0]).strip() if row[0] is not None else ""
            name_raw = str(row[1]).strip() if row[1] is not None else ""
            if not article_raw or not name_raw or len(name_raw) < 2:
                skipped += 1
                continue
            article_up = article_raw.upper()
            if article_up in ("АРТИКУЛ", "ARTICLE", "BARCODE", "ШТРИХКОД", "КОД"):
                skipped += 1
                continue
            if name_raw.lower() in ("название", "наименование", "product name", "name", "товар"):
                skipped += 1
                continue
            valid.append({"article": article_up, "name": name_raw[:80]})
            if len(valid) >= ARTICLE_IMPORT_MAX:
                break
        wb.close()
    except Exception as e:
        logging.error(f"products_import_articles_upload parse error: {e}")
        return _err("Не удалось разобрать файл. Убедитесь, что это корректный .xlsx файл Excel 2007+.")

    if not valid:
        return _err(
            "Файл не содержит подходящих строк. "
            "Убедитесь, что столбцы: A=Артикул, B=Название товара."
        )

    _cleanup_article_sessions()
    import time as _time
    session_id = str(_uuid.uuid4())
    _article_sessions[session_id] = {
        "telegram_id": telegram_id,
        "items": valid,
        "skipped": skipped,
        "_ts": _time.time(),
    }

    return RedirectResponse(
        url=f"/products/import-articles?session_id={session_id}&page=1",
        status_code=302,
    )


@router.post("/products/import-articles/confirm")
def products_import_articles_confirm(
    request: Request,
    session_id: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Apply article assignments from session."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote
    from billing_utils import is_extension_denied

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/products/import-articles", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)
    if is_extension_denied(int(user["sub"]), "barcodes"):
        return RedirectResponse(url="/subscription?need=barcodes", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    sess = _article_sessions.pop(session_id, None)
    if not sess or sess.get("telegram_id") != telegram_id:
        return RedirectResponse(
            url="/products/import-articles?error=Сессия+не+найдена+или+устарела.+Загрузите+файл+снова.",
            status_code=302,
        )

    try:
        db = get_web_db(telegram_id, org_db)
        result = db.import_articles_bulk(sess["items"])
    except Exception as e:
        logging.error(f"products_import_articles_confirm: {e}")
        return RedirectResponse(
            url="/products/import-articles?error=Ошибка+сохранения.+Попробуйте+ещё+раз.",
            status_code=302,
        )

    updated = result.get("updated", 0)
    not_found = result.get("not_found", [])
    conflicts = result.get("conflicts", [])
    nf = len(not_found)
    cf = len(conflicts)

    params = f"articles_imported={updated}"
    if nf:
        params += f"&nf={nf}"
    if cf:
        params += f"&cf={cf}"
    return RedirectResponse(url=f"/products?{params}", status_code=302)


@router.get("/api/products/by-article")
def api_product_by_article(request: Request, q: str = ""):
    """JSON: найти товар по артикулу (точное совпадение, без учёта регистра).
    Требует активной сессии. Используется сканером штрих-кодов.
    """
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import is_extension_denied
    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if is_extension_denied(int(user["sub"]), "barcodes"):
        return JSONResponse({"ok": False, "error": "subscription_required"}, status_code=403)
    if not q or not q.strip():
        return JSONResponse({"ok": False, "error": "q required"}, status_code=400)
    db = get_web_db(int(user["sub"]), user.get("org_db") or "")
    q_clean = q.strip()
    row = db.get_product_by_article(q_clean)
    if not row:
        row = db.get_product_by_barcode(q_clean)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({
        "ok": True,
        "product": {
            "id": row[0],
            "name": row[1],
            "category": row[2],
            "price": row[3],
            "article": row[7] if len(row) > 7 else None,
        }
    })


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
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера."}, status_code=500)


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
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера."}, status_code=500)


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


def _make_qr_b64(data: str) -> str:
    """Generate a QR code PNG as a base64 string. Returns '' on failure."""
    try:
        import qrcode as _qr
        qr = _qr.QRCode(version=1, error_correction=_qr.constants.ERROR_CORRECT_M,
                        box_size=8, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return __import__("base64").b64encode(buf.getvalue()).decode()
    except Exception as exc:
        logging.warning(f"_make_qr_b64 failed: {exc}")
        return ""


_DEFAULT_LABEL_SETTINGS = {
    'bg_color': '#ffffff', 'text_color': '#000000',
    'price_color': '#000000', 'logo_path': '', 'font_size': 'medium',
    'org_logo_path': '',
    'font_family': 'Arial, Helvetica, sans-serif',
    'border_color': '#cccccc', 'border_width': '1',
    'label_theme': 'standard',
    'element_order': ["logo","badge","name","price","sep","qr","barcode","article","category","description"],
    'visible_elements': {"logo":True,"badge":False,"name":True,"price":True,
                         "sep":True,"qr":True,"barcode":False,"article":True,
                         "category":False,"description":False},
    'sale_badge': '',
    'qr_content': '',
}


def _build_qr_payload(template: str, *, name: str, price, article: str,
                      barcode: str, product_id) -> str:
    """Подставляет плейсхолдеры в шаблон содержимого QR.
    Поддерживает {article} {barcode} {name} {price} {id}; пустой шаблон → ''."""
    t = (template or "").strip()
    if not t:
        return ""
    repl = {
        "{article}": str(article or ""),
        "{barcode}": str(barcode or ""),
        "{name}":    str(name or ""),
        "{price}":   str(price if price is not None else ""),
        "{id}":      str(product_id if product_id is not None else ""),
    }
    for k, v in repl.items():
        t = t.replace(k, v)
    return t.strip()


def _build_label_ctx(product, network: str = None, db=None, copies: int = 1,
                     qr_content: str = "") -> dict:
    """Build context dict for a single product label.
    If network is given and db is provided, resolves effective article/barcode
    from product_network_variants (variant-aware ценники).
    qr_content (если задан) — шаблон содержимого QR с плейсхолдерами; иначе QR
    кодирует артикул или штрихкод (прежнее поведение).
    """
    base_article = product[7] if len(product) > 7 else ""
    base_barcode = product[8] if len(product) > 8 else ""
    article = base_article
    barcode = base_barcode
    if network and db:
        try:
            codes = db.get_effective_product_codes(product[0], network)
            article = codes.get("article") or base_article
            barcode = codes.get("barcode") or base_barcode
        except Exception:
            pass
    name = product[1] or ""
    price_val = int(product[3]) if product[3] is not None else 0
    if (qr_content or "").strip():
        qr_code = _build_qr_payload(qr_content, name=name, price=price_val,
                                    article=article, barcode=barcode,
                                    product_id=product[0])
    else:
        qr_code = article or barcode
    qr_b64 = _make_qr_b64(qr_code) if qr_code else ""
    category = product[2] or "" if len(product) > 2 else ""
    description = (product[6] or "")[:80] if len(product) > 6 else ""
    raw_old = product[9] if len(product) > 9 else None
    old_price_val = int(raw_old) if (raw_old not in (None, 0) and raw_old > price_val) else 0
    return {
        "id":          product[0],
        "name":        product[1] or "",
        "price":       price_val,
        "old_price":   old_price_val,
        "article":     article or "",
        "barcode":     barcode or "",
        "qr_b64":      qr_b64,
        "category":    category,
        "description": description,
        "copies":      max(1, int(copies)),
    }


# Единый каталог размеров ценников.
# value: (w_mm, h_mm, h_gap_mm, v_gap_mm, margin_mm, подпись, подсказка)
# Термоэтикетки тайлятся по A4; "a4-*" — готовые раскладки для лазерных
# листов самоклейки (число столбцов/строк выводится из размеров автоматически).
_LABEL_SIZE_DEFS: dict[str, tuple] = {
    "30x20": (30, 20, 2, 2, 8, "30×20 мм", "Мини"),
    "40x30": (40, 30, 3, 3, 10, "40×30 мм", "Маленький"),
    "58x40": (58, 40, 4, 4, 10, "58×40 мм", "Стандарт"),
    "60x40": (60, 40, 4, 4, 10, "60×40 мм", "Крупный"),
    "a6":    (105, 74, 0, 5, 0, "A6 (105×148 мм)", "Большой"),
    "a4-24": (63.5, 33.9, 2, 0, 6, "A4 · 24 шт/лист", "Лазерный лист"),
    "a4-65": (38, 21.2, 2, 0, 5, "A4 · 65 шт/лист", "Лазерный лист"),
}

_VALID_LABEL_SIZES = set(_LABEL_SIZE_DEFS)

# Per-size PDF layout params: (label_w_mm, label_h_mm, h_gap_mm, v_gap_mm, margin_mm)
_LABEL_PDF_PARAMS: dict[str, tuple] = {
    k: v[:5] for k, v in _LABEL_SIZE_DEFS.items()
}


def _label_size_options() -> list[tuple]:
    """Список (value, подпись, подсказка) для UI-переключателей размера."""
    return [(k, v[5], v[6]) for k, v in _LABEL_SIZE_DEFS.items()]


def _grid_for_size(size: str) -> tuple[int, int, int]:
    """Сколько ценников данного размера помещается на A4: (cols, rows, per_page).
    Та же формула упаковки, что и в _generate_labels_pdf — единый источник,
    чтобы UI/тесты и реальная генерация не расходились."""
    # A4 в мм; работаем в мм (масштаб одинаков с точками reportlab).
    page_w, page_h = 210.0, 297.0
    lw, lh, hg, vg, mg = _LABEL_PDF_PARAMS.get(size, _LABEL_PDF_PARAMS["58x40"])
    denom = (lw + hg) if (lw + hg) > 0 else lw
    cols = max(1, int((page_w - 2 * mg + hg) / denom))
    rows = max(1, int((page_h - 2 * mg + vg) / (lh + vg)))
    return cols, rows, cols * rows


def _hex_to_rgb_color(hex_color: str):
    """Convert #rrggbb hex to reportlab Color. Returns black on error."""
    from reportlab.lib import colors as _rlc
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
        return _rlc.Color(r, g, b)
    except Exception:
        return _rlc.black


def _ean_checksum_ok(code: str) -> bool:
    """Validate the EAN check digit (last digit) for full-length codes.
    Weights alternate 3,1 from the rightmost body digit (works for EAN-13 and
    EAN-8). Only meaningful for 8- or 13-digit numeric codes."""
    d = (code or "").strip()
    if not d.isdigit() or len(d) not in (8, 13):
        return False
    digits = [int(x) for x in d]
    body, check = digits[:-1], digits[-1]
    s = 0
    for i, dig in enumerate(body):
        from_right = len(body) - 1 - i
        s += dig * (3 if from_right % 2 == 0 else 1)
    return (10 - (s % 10)) % 10 == check


def _barcode_format(code: str) -> str:
    """Pick a barcode symbology from the value: retail EAN-13/EAN-8 for pure
    digit codes of the right length, otherwise generic Code-128. Full-length
    codes (13/8 digits) must pass the EAN check digit, else fall back to
    Code-128 (python-barcode would silently recompute a wrong check digit)."""
    d = (code or "").strip()
    if d.isdigit():
        if len(d) == 12:                     # check digit auto-computed
            return "ean13"
        if len(d) == 13:
            return "ean13" if _ean_checksum_ok(d) else "code128"
        if len(d) == 7:                      # check digit auto-computed
            return "ean8"
        if len(d) == 8:
            return "ean8" if _ean_checksum_ok(d) else "code128"
    return "code128"


def _make_barcode_img(code: str) -> "io.BytesIO | None":
    """Generate a barcode PNG (EAN-13/EAN-8 when valid, else Code-128).
    Falls back to Code-128 if EAN validation (checksum/length) fails."""
    if not code:
        return None
    try:
        import barcode as _bc
        from barcode.writer import ImageWriter as _IW
        opts = {"write_text": False, "module_height": 6.0,
                "quiet_zone": 2.0, "font_size": 0}
        fmt = _barcode_format(code)
        val = code.strip() if fmt != "code128" else code
        try:
            buf = io.BytesIO()
            _bc.get(fmt, val, writer=_IW()).write(buf, options=opts)
        except Exception:
            # invalid EAN (bad checksum/length) → generic Code-128
            buf = io.BytesIO()
            _bc.get("code128", code, writer=_IW()).write(buf, options=opts)
        buf.seek(0)
        return buf
    except Exception:
        return None


_PDF_FONTS_READY = False


def _ensure_pdf_fonts() -> None:
    """Register DejaVu TTF fonts (with Cyrillic) once for reportlab.
    reportlab's built-in Helvetica/Courier have NO Cyrillic glyphs, so Russian
    text renders blank without these TTFs."""
    global _PDF_FONTS_READY
    if _PDF_FONTS_READY:
        return
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        base = Path("web/static/fonts")
        files = {
            "DejaVuSans":      "DejaVuSans.ttf",
            "DejaVuSans-Bold": "DejaVuSans-Bold.ttf",
            "DejaVuSerif":     "DejaVuSerif.ttf",
            "DejaVuSerif-Bold": "DejaVuSerif-Bold.ttf",
            "DejaVuSansMono":  "DejaVuSansMono.ttf",
        }
        registered = set(pdfmetrics.getRegisteredFontNames())
        for name, fn in files.items():
            if name in registered:
                continue
            fp = base / fn
            if fp.exists():
                pdfmetrics.registerFont(TTFont(name, str(fp)))
        # Mark ready only when the core Cyrillic pair is available, so a
        # transient/partial failure is retried on the next call.
        now = set(pdfmetrics.getRegisteredFontNames())
        if "DejaVuSans" in now and "DejaVuSans-Bold" in now:
            _PDF_FONTS_READY = True
    except Exception:
        pass


def _resolve_pdf_fonts(font_family: str) -> tuple:
    """Map a stored CSS font-family to (regular, bold) registered PDF fonts.
    Falls back to Helvetica if the Cyrillic TTF isn't registered."""
    fam = (font_family or "").lower().strip()
    if "georgia" in fam or "times" in fam or fam == "serif":
        reg, bold = "DejaVuSerif", "DejaVuSerif-Bold"
    elif "courier" in fam or "mono" in fam:
        reg, bold = "DejaVuSansMono", "DejaVuSansMono"
    else:
        reg, bold = "DejaVuSans", "DejaVuSans-Bold"
    try:
        from reportlab.pdfbase import pdfmetrics
        names = set(pdfmetrics.getRegisteredFontNames())
        if reg not in names:
            reg = "Helvetica"
        if bold not in names:
            bold = "Helvetica-Bold"
    except Exception:
        reg, bold = "Helvetica", "Helvetica-Bold"
    return reg, bold


def _generate_labels_pdf(labels: list, size: str = "58x40",
                          label_settings: dict = None) -> bytes:
    """Generate a PDF with price labels on A4 using reportlab.
    Applies bg/text/price colors and logo from label_settings.
    Column count is auto-derived so physical dimensions are exact.
    """
    import base64 as _b64
    from reportlab.pdfgen import canvas as _canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader

    ls = label_settings or {}
    bg_color    = _hex_to_rgb_color(ls.get("bg_color", "#ffffff"))
    text_color  = _hex_to_rgb_color(ls.get("text_color", "#000000"))
    price_color = _hex_to_rgb_color(ls.get("price_color", "#000000"))
    border_color = _hex_to_rgb_color(ls.get("border_color", "#cccccc"))
    try:
        border_w = max(0.1, min(3.0, float(ls.get("border_width", "1"))))
    except Exception:
        border_w = 0.5
    vis = ls.get("visible_elements") or {}
    sale_badge = ls.get("sale_badge", "")

    lw_mm, lh_mm, hg_mm, vg_mm, mg_mm = _LABEL_PDF_PARAMS.get(
        size, _LABEL_PDF_PARAMS["58x40"]
    )

    buf = io.BytesIO()
    page_w, page_h = A4

    label_w = lw_mm * mm
    label_h = lh_mm * mm
    h_gap   = hg_mm * mm
    v_gap   = vg_mm * mm
    margin  = mg_mm * mm

    cols, rows_per_page, labels_per_page = _grid_for_size(size)
    grid_w = cols * label_w + (cols - 1) * h_gap
    left_margin = (page_w - grid_w) / 2

    _ensure_pdf_fonts()
    reg_font, bold_font = _resolve_pdf_fonts(ls.get("font_family", ""))
    fscale = {"small": 0.85, "medium": 1.0, "large": 1.15}.get(
        ls.get("font_size", "medium"), 1.0)

    name_pt  = max(5, min(13, lw_mm * 0.12 * fscale))
    price_pt = max(8, min(26, lw_mm * 0.22 * fscale))
    art_pt   = max(4, min(9,  lw_mm * 0.09 * fscale))
    qr_mm_v  = max(8, min(34, lw_mm * 0.38))
    badge_pt = max(4, min(10, lw_mm * 0.10 * fscale))

    logo_path = ls.get("logo_path") or ls.get("org_logo_path") or ""
    logo_img = None
    if logo_path and vis.get("logo", True):
        try:
            fpath = Path("web") / logo_path.lstrip("/")
            if fpath.exists():
                logo_img = ImageReader(str(fpath))
        except Exception:
            pass

    # Expand copies
    expanded = []
    for lb in labels:
        for _ in range(max(1, int(lb.get("copies", 1)))):
            expanded.append(lb)

    c = _canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Ценники — DailySales")

    for idx, lb in enumerate(expanded):
        if idx > 0 and idx % labels_per_page == 0:
            c.showPage()

        col = idx % cols
        row_on_page = (idx // cols) % rows_per_page
        x = left_margin + col * (label_w + h_gap)
        y = page_h - margin - (row_on_page + 1) * label_h - row_on_page * v_gap

        # Background fill
        c.setFillColor(bg_color)
        c.roundRect(x, y, label_w, label_h, min(2 * mm, label_w * 0.04), fill=1, stroke=0)
        c.setStrokeColor(border_color)
        c.setLineWidth(border_w * 0.5)
        c.roundRect(x, y, label_w, label_h, min(2 * mm, label_w * 0.04), fill=0, stroke=1)

        inner_x = x + 2 * mm
        inner_w = label_w - 4 * mm
        cx = x + label_w / 2
        st = {"y": y + label_h - 2 * mm}

        def draw_badge():
            if not (sale_badge and vis.get("badge", False)):
                return
            c.setFillColor(colors.Color(0.9, 0.1, 0.1))
            badge_h = badge_pt * 1.6
            c.roundRect(x + 1 * mm, st["y"] - badge_h, label_w - 2 * mm, badge_h, 1 * mm, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont(bold_font, badge_pt)
            c.drawCentredString(cx, st["y"] - badge_h + badge_pt * 0.3, sale_badge)
            st["y"] -= badge_h + 1 * mm

        def draw_logo():
            if not (logo_img and vis.get("logo", True)):
                return
            try:
                logo_h = min(8 * mm, label_h * 0.2)
                logo_w = min(label_w - 4 * mm, logo_h * 3)
                c.drawImage(logo_img, x + (label_w - logo_w) / 2,
                            st["y"] - logo_h, logo_w, logo_h,
                            preserveAspectRatio=True, mask="auto")
                st["y"] -= logo_h + 1 * mm
            except Exception:
                pass

        def draw_category():
            if not vis.get("category", False):
                return
            cat = (lb.get("category") or "")[:40]
            if not cat:
                return
            c.setFillColor(colors.Color(0.5, 0.5, 0.5))
            c.setFont(reg_font, max(4, art_pt - 1))
            c.drawCentredString(cx, st["y"] - art_pt, cat.upper())
            st["y"] -= art_pt + 1 * mm

        def draw_name():
            if not vis.get("name", True):
                return
            name = (lb.get("name") or "")[:60]
            c.setFillColor(text_color)
            c.setFont(bold_font, name_pt)
            words = name.split()
            line1, line2 = "", ""
            for w in words:
                test = (line1 + " " + w).strip()
                if c.stringWidth(test, bold_font, name_pt) <= inner_w:
                    line1 = test
                else:
                    if not line2:
                        line2 = w
                    else:
                        test2 = (line2 + " " + w).strip()
                        if c.stringWidth(test2, bold_font, name_pt) <= inner_w:
                            line2 = test2
            lh_pt = name_pt * 1.3
            lines_h = (lh_pt if line1 else 0) + (lh_pt if line2 else 0) + 0.5 * mm
            name_top = st["y"] - 0.5 * mm
            if line1:
                c.drawCentredString(cx, name_top - lh_pt, line1)
            if line2:
                c.drawCentredString(cx, name_top - lh_pt - lh_pt, line2)
            st["y"] -= lines_h

        def draw_price():
            if not vis.get("price", True):
                return
            price = lb.get("price", 0)
            old_price = lb.get("old_price", 0)
            if old_price and old_price > price:
                old_pt = max(6, price_pt * 0.6)
                old_str = f"{int(old_price):,}".replace(",", "\u202f") + " \u20bd"
                c.setFillColor(text_color)
                c.setFont(reg_font, old_pt)
                old_y = st["y"] - old_pt - 0.3 * mm
                c.drawCentredString(cx, old_y, old_str)
                ow = c.stringWidth(old_str, reg_font, old_pt)
                c.setLineWidth(max(0.4, old_pt * 0.07))
                c.line(cx - ow / 2, old_y + old_pt * 0.32,
                       cx + ow / 2, old_y + old_pt * 0.32)
                st["y"] -= old_pt + 1.0 * mm
            price_str = f"{int(price):,}".replace(",", "\u202f") + " \u20bd"
            c.setFillColor(price_color)
            c.setFont(bold_font, price_pt)
            c.drawCentredString(cx, st["y"] - price_pt - 0.5 * mm, price_str)
            st["y"] -= price_pt + 2 * mm

        def draw_sep():
            if not vis.get("sep", True):
                return
            c.setStrokeColor(colors.Color(0.8, 0.8, 0.8))
            c.setLineWidth(0.3)
            c.line(inner_x, st["y"], inner_x + inner_w, st["y"])
            st["y"] -= 1.5 * mm

        def draw_qr():
            qr_b64 = lb.get("qr_b64") or ""
            if not (qr_b64 and vis.get("qr", True)):
                return
            if st["y"] - y < 9 * mm:  # not enough room — skip to avoid overflow
                return
            try:
                qr_bytes = _b64.b64decode(qr_b64)
                qr_size = min(qr_mm_v * mm, st["y"] - y - 3 * mm)
                qr_size = max(6 * mm, qr_size)
                qr_img = ImageReader(io.BytesIO(qr_bytes))
                c.drawImage(qr_img, x + (label_w - qr_size) / 2,
                            st["y"] - qr_size, qr_size, qr_size, preserveAspectRatio=True)
                st["y"] -= qr_size + 1 * mm
            except Exception:
                pass

        def draw_barcode():
            barcode_val = lb.get("barcode") or lb.get("article") or ""
            if not (barcode_val and vis.get("barcode", False)):
                return
            if st["y"] - y < 7 * mm:  # not enough room — skip to avoid overflow
                return
            bc_buf = _make_barcode_img(barcode_val)
            if not bc_buf:
                return
            try:
                bc_img = ImageReader(bc_buf)
                bc_h = min(8 * mm, st["y"] - y - 2 * mm)
                bc_h = max(5 * mm, bc_h)
                c.drawImage(bc_img, inner_x, st["y"] - bc_h,
                            inner_w, bc_h, preserveAspectRatio=True)
                st["y"] -= bc_h + 0.5 * mm
            except Exception:
                pass

        def draw_article():
            if not vis.get("article", True):
                return
            article = lb.get("article") or ""
            c.setFont(reg_font, art_pt)
            if article:
                c.setFillColor(text_color)
                c.drawCentredString(cx, st["y"] - art_pt, article)
            else:
                c.setFillColor(colors.Color(0.7, 0.7, 0.7))
                c.drawCentredString(cx, st["y"] - art_pt,
                                    "\u2014 \u0430\u0440\u0442\u0438\u043a\u0443\u043b \u043d\u0435 \u0437\u0430\u0434\u0430\u043d \u2014")
            st["y"] -= art_pt + 1 * mm

        def draw_description():
            if not vis.get("description", False):
                return
            desc = (lb.get("description") or "")[:60]
            if not desc:
                return
            c.setFillColor(colors.Color(0.4, 0.4, 0.4))
            c.setFont(reg_font, max(4, art_pt - 1))
            c.drawCentredString(cx, st["y"] - art_pt, desc)
            st["y"] -= art_pt + 1 * mm

        drawers = {
            "badge": draw_badge, "logo": draw_logo, "category": draw_category,
            "name": draw_name, "price": draw_price, "sep": draw_sep,
            "qr": draw_qr, "barcode": draw_barcode,
            "article": draw_article, "description": draw_description,
        }
        order = ls.get("element_order") or [
            "logo", "badge", "name", "price", "sep", "qr",
            "barcode", "article", "category", "description"]
        seen = set()
        for key in order:
            fn = drawers.get(key)
            if fn and key not in seen:
                seen.add(key)
                fn()
        # Draw any element missing from a malformed/partial order
        for key, fn in drawers.items():
            if key not in seen:
                fn()

    c.save()
    return buf.getvalue()


def _get_label_settings_safe(db) -> dict:
    """Fetch label settings, returning defaults on any error."""
    try:
        return db.get_label_settings()
    except Exception:
        return dict(_DEFAULT_LABEL_SETTINGS)


def _list_label_presets_safe(db, user) -> list:
    """Return saved presets for owners only; [] for everyone else or on error."""
    try:
        if user.get("role") not in ("owner", "super_admin"):
            return []
        return db.list_label_presets()
    except Exception:
        return []


def _effective_logo(label_settings: dict) -> str:
    """Return the logo to actually show on labels: label-specific logo, then org logo fallback."""
    return label_settings.get("logo_path") or label_settings.get("org_logo_path") or ""


@router.get("/products/label-design")
def products_label_design(request: Request):
    """Shortcut: redirect owner to label design page using their first product."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/products", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        prods = db.get_all_products() or []
        if prods:
            return RedirectResponse(url=f"/products/{prods[0][0]}/label?design=1", status_code=302)
    except Exception:
        pass
    return RedirectResponse(url="/products", status_code=302)


def _get_user_label_size(telegram_id: int) -> str:
    """Read per-user label size preference from main.db."""
    try:
        import sqlite3
        c = sqlite3.connect("data/main.db", timeout=5)
        try:
            c.execute(
                "CREATE TABLE IF NOT EXISTS user_label_prefs "
                "(telegram_id INTEGER PRIMARY KEY, label_size TEXT DEFAULT '58x40', updated_at TEXT)"
            )
            row = c.execute(
                "SELECT label_size FROM user_label_prefs WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            c.commit()
        finally:
            c.close()
        size = row[0] if row else "58x40"
        return size if size in _VALID_LABEL_SIZES else "58x40"
    except Exception:
        return "58x40"


def _save_user_label_size(telegram_id: int, size: str) -> None:
    """Persist per-user label size preference to main.db."""
    try:
        import sqlite3
        if size not in _VALID_LABEL_SIZES:
            return
        c = sqlite3.connect("data/main.db", timeout=5)
        try:
            c.execute(
                "CREATE TABLE IF NOT EXISTS user_label_prefs "
                "(telegram_id INTEGER PRIMARY KEY, label_size TEXT DEFAULT '58x40', updated_at TEXT)"
            )
            c.execute(
                "INSERT INTO user_label_prefs (telegram_id, label_size, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(telegram_id) DO UPDATE SET label_size=excluded.label_size, updated_at=excluded.updated_at",
                (telegram_id, size),
            )
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


@router.post("/api/label-size")
async def api_save_label_size(request: Request):
    """Save user's preferred label size to server. Fire-and-forget from JS."""
    from web.auth import get_session_user, verify_csrf_token
    user = get_session_user(request)
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False}, status_code=401)
    try:
        form = await request.form()
        size = str(form.get("size", "58x40")).strip()
        csrf = str(form.get("csrf_token", ""))
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False}, status_code=400)
    if not verify_csrf_token(request, csrf):
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False}, status_code=403)
    _save_user_label_size(int(user["sub"]), size)
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": True, "size": size})


@router.get("/products/{product_id}/label")
def product_label(request: Request, product_id: int, print: str = "",
                  size: str = "58x40", format: str = "", network: str = ""):
    """Render a print-friendly price label for a single product.
    ?format=pdf returns a downloadable PDF; ?size=58x40|40x30|a6 sets label size.
    ?network=<name> resolves network-specific codes.
    """
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db
    from billing_utils import is_extension_denied

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/products/{product_id}", status_code=302)
    if is_extension_denied(int(user["sub"]), "labels"):
        return RedirectResponse(url="/subscription?need=labels", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    product = db.get_product(product_id)
    if not product:
        return RedirectResponse(url="/products", status_code=302)

    url_size = request.query_params.get("size")
    if url_size and url_size in _VALID_LABEL_SIZES:
        size = url_size
        _save_user_label_size(telegram_id, size)
    else:
        size = _get_user_label_size(telegram_id)

    url_network = request.query_params.get("network", "").strip()
    network = url_network or network

    trade_networks: list[str] = []
    try:
        trade_networks = db.get_all_trade_networks() or []
    except Exception:
        pass

    label_settings = _get_label_settings_safe(db)
    label = _build_label_ctx(product, network=network or None, db=db,
                             qr_content=label_settings.get("qr_content", ""))

    if format == "pdf":
        from fastapi.responses import Response
        try:
            pdf_bytes = _generate_labels_pdf([label], size=size, label_settings=label_settings)
        except Exception as exc:
            logging.error(f"PDF generation failed: {exc}")
            return Response(content="PDF generation error", status_code=500)
        safe_name = (label["name"] or "label")[:40].replace(" ", "_")
        # Content-Disposition кодируется latin-1: имя с кириллицей в простом
        # filename= уронит ответ (500). ASCII-fallback + RFC 5987 filename* для
        # юникода — браузеры берут filename*, остальные — ASCII-вариант.
        from urllib.parse import quote as _q
        ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "label"
        disposition = (
            f"attachment; filename=\"{ascii_name}.pdf\"; "
            f"filename*=UTF-8''{_q(safe_name)}.pdf"
        )
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": disposition},
        )

    return request.app.state.templates.TemplateResponse(
        request, "products/label.html", {
            "request": request,
            "labels": [label],
            "auto_print": bool(print),
            "initial_size": size,
            "label_settings": label_settings,
            "effective_logo": _effective_logo(label_settings),
            "is_owner": user.get("role") in ("owner", "super_admin"),
            "csrf_token": get_csrf_token(request),
            "pdf_url": f"/products/{product_id}/label?format=pdf",
            "trade_networks": trade_networks,
            "selected_network": network,
            "single_product_id": product_id,
            "label_presets": _list_label_presets_safe(db, user),
            "label_size_options": _label_size_options(),
        }
    )


@router.post("/products/labels")
async def products_labels_bulk(request: Request):
    """Return a print page (or PDF) with labels for multiple products.
    JSON body: {product_ids: [...], copies_map: {id: n}, network: "...",
                csrf_token: "...", format: "pdf"|"", size: "58x40"}
    """
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web.deps import get_web_db
    from billing_utils import is_extension_denied

    user = get_session_user(request)
    if not user:
        from fastapi.responses import Response
        return Response(content="Unauthorized", status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        from fastapi.responses import Response
        return Response(content="Forbidden", status_code=403)
    if is_extension_denied(int(user["sub"]), "labels"):
        from fastapi.responses import Response
        return Response(content="Subscription required", status_code=403)

    fmt_qp = request.query_params.get("format", "")
    try:
        body = await request.json()
        csrf = body.get("csrf_token", "")
        product_ids = [int(x) for x in body.get("product_ids", [])]
        size = body.get("size", "58x40")
        fmt = fmt_qp or body.get("format", "")
        network = str(body.get("network", "") or "").strip()
        copies_map: dict = body.get("copies_map") or {}
        all_filtered = body.get("all_filtered") is True
        flt_q = str(body.get("q", "") or "").strip()
        flt_category = str(body.get("category", "") or "").strip()
    except Exception:
        from fastapi.responses import Response
        return Response(content="Bad request", status_code=400)

    _uid = int(user["sub"])
    if size and size in _VALID_LABEL_SIZES:
        _save_user_label_size(_uid, size)
    else:
        size = _get_user_label_size(_uid)

    if not verify_csrf_token(request, csrf):
        from fastapi.responses import Response
        return Response(content="CSRF error", status_code=403)

    if not product_ids and not all_filtered:
        from fastapi.responses import Response
        return Response(content="No products selected", status_code=400)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    label_settings = _get_label_settings_safe(db)

    # «Печать всех по фильтру»: резолвим id серверно той же фильтрацией, что и
    # список товаров (категория + поиск по названию/категории/артикулу/штрихкоду).
    if all_filtered:
        try:
            matched = _filter_products(db.get_all_products(), q=flt_q, category=flt_category)
            matched.sort(key=lambda p: (p[1] or "").lower())
            product_ids = [p[0] for p in matched]
        except Exception:
            product_ids = []

    trade_networks: list[str] = []
    try:
        trade_networks = db.get_all_trade_networks() or []
    except Exception:
        pass

    labels = []
    for pid in product_ids[:200]:
        try:
            product = db.get_product(pid)
            if product:
                copies = max(1, min(99, int(copies_map.get(str(pid), 1))))
                labels.append(_build_label_ctx(
                    product, network=network or None, db=db, copies=copies,
                    qr_content=label_settings.get("qr_content", "")))
        except Exception:
            pass

    if not labels:
        from fastapi.responses import Response
        return Response(content="No valid products", status_code=400)

    if fmt == "pdf":
        from fastapi.responses import Response
        try:
            pdf_bytes = _generate_labels_pdf(labels, size=size, label_settings=label_settings)
        except Exception as exc:
            logging.error(f"Bulk PDF generation failed: {exc}")
            return Response(content="PDF generation error", status_code=500)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="labels.pdf"'},
        )

    return request.app.state.templates.TemplateResponse(
        request, "products/label.html", {
            "request": request,
            "labels": labels,
            "auto_print": True,
            "initial_size": size,
            "label_settings": label_settings,
            "effective_logo": _effective_logo(label_settings),
            "is_owner": user.get("role") in ("owner", "super_admin"),
            "csrf_token": get_csrf_token(request),
            "bulk_product_ids": product_ids,
            "trade_networks": trade_networks,
            "selected_network": network,
            "label_presets": _list_label_presets_safe(db, user),
            "label_size_options": _label_size_options(),
        }
    )


@router.post("/products/label-settings")
async def save_label_settings(
    request: Request,
    bg_color: str = Form("#ffffff"),
    text_color: str = Form("#000000"),
    price_color: str = Form("#000000"),
    font_size: str = Form("medium"),
    clear_logo: str = Form(""),
    logo: UploadFile = File(None),
    font_family: str = Form(""),
    border_color: str = Form("#cccccc"),
    border_width: str = Form("1"),
    label_theme: str = Form("standard"),
    element_order: str = Form(""),
    visible_elements: str = Form(""),
    sale_badge: str = Form(""),
    qr_content: str = Form(""),
):
    """Save label design settings (owner only). Accepts multipart/form-data."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from fastapi.responses import Response
    from billing_utils import is_extension_denied
    import json as _json

    user = get_session_user(request)
    if not user:
        return Response(content="Unauthorized", status_code=401)
    if user.get("role") not in ("owner", "super_admin"):
        return Response(content="Forbidden", status_code=403)
    if is_extension_denied(int(user["sub"]), "labels"):
        return Response(content="Subscription required", status_code=403)

    form = await request.form()
    csrf = form.get("csrf_token", "")
    if not verify_csrf_token(request, csrf):
        return Response(content="CSRF error", status_code=403)

    import re as _re
    _color_re = _re.compile(r'^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$')
    bg_color     = bg_color     if _color_re.match(bg_color)     else "#ffffff"
    text_color   = text_color   if _color_re.match(text_color)   else "#000000"
    price_color  = price_color  if _color_re.match(price_color)  else "#000000"
    border_color = border_color if _color_re.match(border_color) else "#cccccc"

    _VALID_FONT_SIZES = {"small", "medium", "large"}
    if font_size not in _VALID_FONT_SIZES:
        font_size = "medium"
    _VALID_THEMES = {"standard", "dark", "accent", "minimal"}
    if label_theme not in _VALID_THEMES:
        label_theme = "standard"
    try:
        bw = max(0, min(5, int(border_width)))
        border_width = str(bw)
    except Exception:
        border_width = "1"

    # Validate JSON strings (element_order / visible_elements)
    def _safe_json(raw: str) -> str:
        try:
            _json.loads(raw)
            return raw
        except Exception:
            return ""

    element_order    = _safe_json(element_order)
    visible_elements = _safe_json(visible_elements)
    sale_badge = sale_badge[:30].strip() if sale_badge else ""

    # Allowed font families whitelist
    _FONT_MAP = {
        "sans": "Arial, Helvetica, sans-serif",
        "serif": "Georgia, 'Times New Roman', serif",
        "mono": "'Courier New', Courier, monospace",
        "rounded": "'Trebuchet MS', Verdana, sans-serif",
    }
    font_family = _FONT_MAP.get(font_family, font_family or "Arial, Helvetica, sans-serif")

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)

    logo_path = None
    if clear_logo == "1":
        existing = _get_label_settings_safe(db).get("logo_path", "")
        if existing and existing.startswith("/static/product_photos/"):
            try:
                fpath = Path("web") / existing.lstrip("/")
                if fpath.exists():
                    fpath.unlink()
            except Exception:
                pass
        logo_path = ""
    elif logo and logo.filename:
        try:
            raw = await logo.read()
            if raw and len(raw) <= _LOGO_MAX_BYTES and _is_valid_image(raw):
                logo_path = _save_label_logo(raw, logo.filename, org_db or "")
        except Exception as exc:
            logging.warning(f"label logo upload failed: {exc}")

    db.save_label_settings(
        bg_color, text_color, price_color, logo_path, font_size,
        font_family=font_family, border_color=border_color,
        border_width=border_width, label_theme=label_theme,
        element_order=element_order, visible_elements=visible_elements,
        sale_badge=sale_badge, qr_content=_clean_qr_content(qr_content),
    )
    return JSONResponse({"ok": True})


def _clean_label_design(raw: dict) -> dict:
    """Validate/normalise a label design dict (shared by preset routes)."""
    import re as _re
    import json as _json
    _color_re = _re.compile(r'^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$')

    def _color(v, dflt):
        v = (v or "").strip()
        return v if _color_re.match(v) else dflt

    def _safe_json(v):
        v = v or ""
        try:
            _json.loads(v)
            return v
        except Exception:
            return ""

    try:
        bw = str(max(0, min(5, int(raw.get("border_width", "1")))))
    except Exception:
        bw = "1"

    font_size = raw.get("font_size", "medium")
    if font_size not in {"small", "medium", "large"}:
        font_size = "medium"
    label_theme = raw.get("label_theme", "standard")
    if label_theme not in {"standard", "dark", "accent", "minimal"}:
        label_theme = "standard"

    _FONT_MAP = {
        "sans": "Arial, Helvetica, sans-serif",
        "serif": "Georgia, 'Times New Roman', serif",
        "mono": "'Courier New', Courier, monospace",
        "rounded": "'Trebuchet MS', Verdana, sans-serif",
    }
    ff = raw.get("font_family", "") or ""
    ff = _FONT_MAP.get(ff, ff or "Arial, Helvetica, sans-serif")

    return {
        "bg_color":         _color(raw.get("bg_color"), "#ffffff"),
        "text_color":       _color(raw.get("text_color"), "#000000"),
        "price_color":      _color(raw.get("price_color"), "#000000"),
        "logo_path":        (raw.get("logo_path") or "")[:300],
        "font_size":        font_size,
        "font_family":      ff,
        "border_color":     _color(raw.get("border_color"), "#cccccc"),
        "border_width":     bw,
        "label_theme":      label_theme,
        "element_order":    _safe_json(raw.get("element_order")),
        "visible_elements": _safe_json(raw.get("visible_elements")),
        "sale_badge":       (raw.get("sale_badge") or "")[:30].strip(),
        "qr_content":       _clean_qr_content(raw.get("qr_content")),
    }


def _clean_qr_content(raw: str) -> str:
    """Нормализует шаблон содержимого QR: режет длину, убирает переводы строк."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return s[:300]


def _label_presets_guard(request: Request):
    """Shared auth/CSRF/billing guard for preset routes.
    Returns (db, telegram_id, form) on success or a Response on failure."""
    from web.auth import get_session_user
    from fastapi.responses import Response
    from billing_utils import is_extension_denied
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return Response(content="Unauthorized", status_code=401)
    if user.get("role") not in ("owner", "super_admin"):
        return Response(content="Forbidden", status_code=403)
    if is_extension_denied(int(user["sub"]), "labels"):
        return Response(content="Subscription required", status_code=403)
    telegram_id = int(user["sub"])
    db = get_web_db(telegram_id, user.get("org_db"))
    return db, telegram_id, user


@router.post("/products/label-presets")
async def create_label_preset(request: Request):
    """Save the current design as a new named preset (owner only)."""
    from web.auth import verify_csrf_token
    from fastapi.responses import Response

    guard = _label_presets_guard(request)
    if isinstance(guard, Response):
        return guard
    db, telegram_id, _user = guard

    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token", "")):
        return Response(content="CSRF error", status_code=403)

    name = (form.get("name") or "").strip()[:60]
    if not name:
        return JSONResponse({"ok": False, "error": "empty_name"}, status_code=400)

    if len(db.list_label_presets()) >= 30:
        return JSONResponse({"ok": False, "error": "limit"}, status_code=400)

    design = _clean_label_design(dict(form))
    # The design form omits logo_path (it is an uploaded server path), so the
    # preset captures whatever logo is active at save time → lossless roundtrip.
    if not design.get("logo_path"):
        try:
            design["logo_path"] = _get_label_settings_safe(db).get("logo_path", "") or ""
        except Exception:
            design["logo_path"] = ""
    new_id = db.create_label_preset(name, design)
    if not new_id:
        return JSONResponse({"ok": False, "error": "db"}, status_code=500)
    return JSONResponse({"ok": True, "id": new_id, "name": name})


@router.get("/products/label-presets/{preset_id}")
async def get_label_preset_detail(request: Request, preset_id: int):
    """Return a preset's full design for client-side preview (owner only)."""
    from fastapi.responses import Response

    guard = _label_presets_guard(request)
    if isinstance(guard, Response):
        return guard
    db, _telegram_id, _user = guard

    preset = db.get_label_preset(preset_id)
    if not preset:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, "preset": preset})


@router.post("/products/label-presets/{preset_id}/apply")
async def apply_label_preset(request: Request, preset_id: int):
    """Load a preset into the active label settings (owner only)."""
    from web.auth import verify_csrf_token
    from fastapi.responses import Response

    guard = _label_presets_guard(request)
    if isinstance(guard, Response):
        return guard
    db, telegram_id, _user = guard

    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token", "")):
        return Response(content="CSRF error", status_code=403)

    preset = db.get_label_preset(preset_id)
    if not preset:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    db.save_label_settings(
        preset["bg_color"], preset["text_color"], preset["price_color"],
        preset.get("logo_path") or "", preset["font_size"],
        font_family=preset["font_family"], border_color=preset["border_color"],
        border_width=preset["border_width"], label_theme=preset["label_theme"],
        element_order=preset["element_order"], visible_elements=preset["visible_elements"],
        sale_badge=preset["sale_badge"], qr_content=preset.get("qr_content") or "",
    )
    return JSONResponse({"ok": True})


@router.post("/products/label-presets/{preset_id}/rename")
async def rename_label_preset(request: Request, preset_id: int):
    """Rename a preset (owner only)."""
    from web.auth import verify_csrf_token
    from fastapi.responses import Response

    guard = _label_presets_guard(request)
    if isinstance(guard, Response):
        return guard
    db, telegram_id, _user = guard

    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token", "")):
        return Response(content="CSRF error", status_code=403)

    name = (form.get("name") or "").strip()[:60]
    if not name:
        return JSONResponse({"ok": False, "error": "empty_name"}, status_code=400)
    if not db.get_label_preset(preset_id):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    db.rename_label_preset(preset_id, name)
    return JSONResponse({"ok": True, "name": name})


@router.post("/products/label-presets/{preset_id}/delete")
async def delete_label_preset(request: Request, preset_id: int):
    """Delete a preset (owner only)."""
    from web.auth import verify_csrf_token
    from fastapi.responses import Response

    guard = _label_presets_guard(request)
    if isinstance(guard, Response):
        return guard
    db, telegram_id, _user = guard

    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token", "")):
        return Response(content="CSRF error", status_code=403)

    db.delete_label_preset(preset_id)
    return JSONResponse({"ok": True})


@router.post("/products/org-logo")
async def save_org_logo(
    request: Request,
    clear_org_logo: str = Form(""),
    org_logo: UploadFile = File(None),
):
    """Save or clear the organisation logo used as fallback on price labels (owner only)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from fastapi.responses import Response
    from billing_utils import is_extension_denied

    user = get_session_user(request)
    if not user:
        return Response(content="Unauthorized", status_code=401)
    if user.get("role") not in ("owner", "super_admin"):
        return Response(content="Forbidden", status_code=403)
    if is_extension_denied(int(user["sub"]), "labels"):
        return Response(content="Subscription required", status_code=403)

    form = await request.form()
    csrf = form.get("csrf_token", "")
    if not verify_csrf_token(request, csrf):
        return Response(content="CSRF error", status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)

    if clear_org_logo == "1":
        existing = _get_label_settings_safe(db).get("org_logo_path", "")
        if existing and existing.startswith("/static/product_photos/"):
            try:
                fpath = Path("web") / existing.lstrip("/")
                if fpath.exists():
                    fpath.unlink()
            except Exception:
                pass
        db.save_org_logo("")
    elif org_logo and org_logo.filename:
        try:
            raw = await org_logo.read()
            if raw and len(raw) <= _LOGO_MAX_BYTES and _is_valid_image(raw):
                org_hash = _get_org_hash(org_db or "")
                save_dir = _LOGO_DIR / org_hash
                save_dir.mkdir(parents=True, exist_ok=True)
                for old in save_dir.glob("org_logo.*"):
                    try:
                        old.unlink()
                    except Exception:
                        pass
                ext = Path(org_logo.filename or "logo.png").suffix.lower()
                if ext not in _PHOTO_EXTS:
                    ext = ".png"
                fname = f"org_logo{ext}"
                (save_dir / fname).write_bytes(raw)
                path = f"/static/product_photos/{org_hash}/{fname}"
                db.save_org_logo(path)
        except Exception as exc:
            logging.warning(f"org logo upload failed: {exc}")
            return JSONResponse({"ok": False, "error": "Upload failed"}, status_code=400)

    return JSONResponse({"ok": True})


@router.get("/products/{product_id}")
def product_detail(request: Request, product_id: int, year: int = 0, month: int = 0):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from datetime import date
    import calendar

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    # Resolve the scope month — use provided year/month or fall back to current
    today = date.today()
    if year and month and 1 <= month <= 12 and year >= 2000:
        scoped_year = year
        scoped_month = month
    else:
        scoped_year = today.year
        scoped_month = today.month
        year = 0  # treat as "no scope" so template shows default label
        month = 0

    _month_start = date(scoped_year, scoped_month, 1).isoformat()
    _last_day = calendar.monthrange(scoped_year, scoped_month)[1]
    _month_end = date(scoped_year, scoped_month, _last_day).isoformat()

    _MONTH_RU = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                 "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    scoped_label = f"{_MONTH_RU[scoped_month]} {scoped_year}" if (year and month) else "месяц"

    # Prev/next month for navigation
    if scoped_month == 1:
        prev_year, prev_month = scoped_year - 1, 12
    else:
        prev_year, prev_month = scoped_year, scoped_month - 1
    if scoped_month == 12:
        next_year, next_month = scoped_year + 1, 1
    else:
        next_year, next_month = scoped_year, scoped_month + 1

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "product": None, "product_id": product_id,
        "inventory_by_shop": [], "inventory_log": [],
        "recent_sales": [], "total_stock": 0,
        "month_revenue": 0.0, "month_qty": 0,
        "chart_labels": [], "chart_data": [],
        "error": None,
        "user_tz": DEFAULT_TZ,
        "product_history": [],
        "scoped_year": scoped_year,
        "scoped_month": scoped_month,
        "scoped_label": scoped_label,
        "is_scoped": bool(year and month),
        "prev_year": prev_year, "prev_month": prev_month,
        "next_year": next_year, "next_month": next_month,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        try:
            ctx["user_tz"] = db.get_user_timezone(telegram_id) or DEFAULT_TZ
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

        # Product change history (price etc.)
        try:
            ctx["product_history"] = db.get_product_history(product_id, limit=20) or []
        except Exception:
            pass

        # Recent sales of this product (scoped to the selected month)
        # get_sales_report: id[0] pid[1] shop[2] qty[3] price[4] uid[5] date[6]
        #   product_name[7] category[8] first_name[9] last_name[10]
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT s.id, s.shop_name, s.quantity_sold, s.sale_price, s.sale_date,
                          u.first_name, u.last_name, s.user_id
                   FROM sales s
                   LEFT JOIN users u ON u.id = s.user_id
                   WHERE s.product_id = ?
                     AND date(s.sale_date) BETWEEN ? AND ?
                   ORDER BY s.sale_date DESC LIMIT 30""",
                (product_id, _month_start, _month_end)
            )
            raw_sales = cur.fetchall()
            # Month totals (same window)
            cur.execute(
                """SELECT SUM(s.quantity_sold), SUM(s.quantity_sold * s.sale_price)
                   FROM sales s
                   WHERE s.product_id = ? AND date(s.sale_date) BETWEEN ? AND ?""",
                (product_id, _month_start, _month_end)
            )
            month_row = cur.fetchone()
        finally:
            conn.close()

        ctx["recent_sales"] = raw_sales
        ctx["month_qty"] = int(month_row[0] or 0) if month_row else 0
        ctx["month_revenue"] = float(month_row[1] or 0) if month_row else 0.0

        # 7-day chart data (always last 7 days regardless of scope, for trend context)
        try:
            from datetime import timedelta
            chart_labels = []
            chart_data = []
            conn2 = db.get_connection()
            try:
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
            finally:
                conn2.close()
            ctx["chart_labels"] = chart_labels
            ctx["chart_data"] = chart_data
        except Exception:
            ctx["chart_labels"] = []
            ctx["chart_data"] = []

        try:
            from billing_utils import has_module as _hm
            ctx["ai_assistant_ok"] = _hm(telegram_id, "ai_assistant")
        except Exception:
            ctx["ai_assistant_ok"] = False

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "products/detail.html", ctx
    )


@router.post("/products/bulk")
def products_bulk_action(
    request: Request,
    action: str = Form(...),
    ids: str = Form(default=""),
    value: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Bulk-операции с товарами (delete / set_category / adjust_price)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from fastapi.responses import RedirectResponse

    user = get_session_user(request)
    if not user or user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/products?error=access", status_code=303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/products?error=csrf", status_code=303)

    # Parse IDs
    try:
        product_ids = [int(x.strip()) for x in ids.split(",") if x.strip().lstrip("-").isdigit()]
    except Exception:
        product_ids = []
    if not product_ids:
        return RedirectResponse("/products?error=no_ids", status_code=303)

    db = get_web_db(int(user["sub"]), user.get("org_db"))
    changed = 0

    if action == "delete":
        for pid in product_ids:
            try:
                db.delete_product(pid)
                changed += 1
            except Exception:
                pass
        return RedirectResponse(f"/products?bulk=deleted&n={changed}", status_code=303)

    elif action == "set_category":
        new_cat = value.strip()[:100]
        if not new_cat:
            return RedirectResponse("/products?error=empty_cat", status_code=303)
        try:
            conn = db.get_connection()
            try:
                for pid in product_ids:
                    conn.execute("UPDATE products SET category = ? WHERE id = ?", (new_cat, pid))
                    changed += 1
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return RedirectResponse(f"/products?bulk=category&n={changed}", status_code=303)

    elif action == "adjust_price":
        try:
            pct = float(value)
            if not (-99 <= pct <= 999):
                raise ValueError("out of range")
        except ValueError:
            return RedirectResponse("/products?error=bad_pct", status_code=303)
        try:
            first_name = user.get("first_name") or str(user.get("sub", ""))
            conn = db.get_connection()
            try:
                for pid in product_ids:
                    row = conn.execute("SELECT price FROM products WHERE id = ?", (pid,)).fetchone()
                    if not row:
                        continue
                    old_price = int(row[0] or 0)
                    new_price = max(0, round(old_price * (1 + pct / 100)))
                    conn.execute("UPDATE products SET price = ? WHERE id = ?", (new_price, pid))
                    try:
                        db.add_product_history(pid, "price", old_price, new_price,
                                               changed_by=int(user["sub"]), changed_by_name=first_name)
                    except Exception:
                        pass
                    changed += 1
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return RedirectResponse(f"/products?bulk=price&n={changed}", status_code=303)

    return RedirectResponse("/products", status_code=303)


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
        try:
            # Read old price BEFORE updating
            old_row = conn.execute("SELECT price FROM products WHERE id = ?", (product_id,)).fetchone()
            if not old_row:
                return JSONResponse({"ok": False, "error": "Товар не найден"}, status_code=404)
            old_price = int(old_row[0]) if old_row[0] is not None else None
            conn.execute("UPDATE products SET price = ? WHERE id = ?", (price, product_id))
            conn.commit()
        finally:
            conn.close()
        # Log price change
        try:
            first_name = user.get("first_name") or str(user.get("sub", ""))
            db.add_product_history(product_id, "price", old_price, price,
                                   changed_by=int(user["sub"]), changed_by_name=first_name)
        except Exception:
            pass
        price_fmt = f"{price:,}".replace(",", "\u00a0") + "\u00a0₽"
        return JSONResponse({"ok": True, "price_fmt": price_fmt})
    except Exception:
        return JSONResponse({"ok": False, "error": "Ошибка сохранения"}, status_code=500)
