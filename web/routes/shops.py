import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()


def _admin_guard(user):
    return user and user.get("role") in ("owner", "admin", "super_admin")


@router.get("/shops")
def shops_page(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not _admin_guard(user):
        return RedirectResponse(url="/inventory", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request,
        "user": user,
        "is_admin": True,
        "shops": [],
        "error": None,
        "csrf_token": get_csrf_token(request),
    }

    try:
        db = get_web_db(telegram_id, org_db)
        ctx["shops"] = db.get_shops_with_stats() or []
    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "shops/index.html", ctx
    )


@router.post("/shops/create")
def shops_create(
    request: Request,
    csrf_token: str = Form(default=""),
    name: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not _admin_guard(user):
        return RedirectResponse(url="/shops", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(
            url="/shops?error=Недействительный+CSRF-токен", status_code=302
        )

    name_clean = name.strip()
    if not name_clean or len(name_clean) < 2:
        return RedirectResponse(
            url=f"/shops?error={quote('Название должно содержать не менее 2 символов.')}",
            status_code=302,
        )
    if len(name_clean) > 60:
        return RedirectResponse(
            url=f"/shops?error={quote('Название не должно превышать 60 символов.')}",
            status_code=302,
        )

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)

        # Subscription limit: enforce shop cap in web layer (mirrors bot check_shop_limit)
        try:
            from subscription_utils import check_shop_limit
            _ok, _msg = check_shop_limit(telegram_id)
            if not _ok:
                from urllib.parse import quote as _q
                return RedirectResponse(url=f"/shops?error={_q(_msg or 'Достигнут лимит магазинов по тарифу')}", status_code=302)
        except Exception:
            pass

        ok = db.add_shop(name_clean)
        if not ok:
            return RedirectResponse(
                url=f"/shops?error={quote('Магазин с таким названием уже существует.')}",
                status_code=302,
            )
        from urllib.parse import quote as _q
        return RedirectResponse(
            url=f"/shops/{_q(name_clean)}/stock?new=1",
            status_code=303,
        )
    except Exception as exc:
        logging.error(f"shops_create error: {exc}")
        return RedirectResponse(
            url=f"/shops?error={quote(str(exc))}", status_code=302
        )


@router.get("/shops/{shop_name}/stock")
def shop_stock_page(request: Request, shop_name: str, new: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db
    from urllib.parse import unquote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not _admin_guard(user):
        return RedirectResponse(url="/shops", status_code=302)

    shop_name = unquote(shop_name)
    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request,
        "user": user,
        "is_admin": True,
        "shop_name": shop_name,
        "is_new": new == "1",
        "products_by_category": {},
        "current_stock": {},
        "error": None,
        "csrf_token": get_csrf_token(request),
    }

    try:
        db = get_web_db(telegram_id, org_db)

        # Verify shop exists
        all_shops = db.get_all_shops() or []
        if shop_name not in all_shops:
            from urllib.parse import quote
            return RedirectResponse(
                url=f"/shops?error={quote('Магазин не найден.')}",
                status_code=302,
            )

        # Load all products grouped by category
        products = db.get_all_products() or []
        # products: id[0] name[1] category[2] price[3] ...
        products_by_cat = {}
        for p in products:
            cat = p[2] or "Без категории"
            products_by_cat.setdefault(cat, []).append(p)
        ctx["products_by_category"] = dict(sorted(products_by_cat.items()))

        # Load existing inventory for this shop
        inv = db.get_all_inventory(shop_name=shop_name) or []
        # inv: id[0] product_id[1] shop_name[2] quantity[3] ...
        ctx["current_stock"] = {row[1]: int(row[3] or 0) for row in inv}

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "shops/stock.html", ctx
    )


@router.post("/shops/{shop_name}/stock")
async def shop_stock_save(request: Request, shop_name: str):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import unquote, quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not _admin_guard(user):
        return RedirectResponse(url="/shops", status_code=302)

    shop_name = unquote(shop_name)

    form = await request.form()

    csrf_token = form.get("csrf_token", "")
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(
            url=f"/shops/{quote(shop_name)}/stock?error=csrf",
            status_code=302,
        )

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)

        # Load current inventory for this shop to compute deltas
        inv = db.get_all_inventory(shop_name=shop_name) or []
        current_stock = {row[1]: int(row[3] or 0) for row in inv}

        updated = 0
        for key in form.keys():
            if not key.startswith("qty_"):
                continue
            try:
                product_id = int(key[4:])
                new_qty = int(form[key] or 0)
            except (ValueError, TypeError):
                continue
            if new_qty < 0:
                new_qty = 0
            current = current_stock.get(product_id, 0)
            delta = new_qty - current
            if delta != 0:
                db.update_inventory(
                    shop_name=shop_name,
                    product_id=product_id,
                    delta=delta,
                    user_id=telegram_id,
                    change_type="initial_stock",
                    change_reason="Начальные остатки (веб)",
                )
                updated += 1

        msg = f"Остатки для «{shop_name}» сохранены."
        if updated:
            msg += f" Обновлено позиций: {updated}."
        return RedirectResponse(
            url=f"/shops?success={quote(msg)}",
            status_code=303,
        )
    except Exception as exc:
        logging.error(f"shop_stock_save error: {exc}")
        return RedirectResponse(
            url=f"/shops/{quote(shop_name)}/stock?error={quote(str(exc))}",
            status_code=302,
        )


@router.post("/shops/rename")
def shops_rename(
    request: Request,
    csrf_token: str = Form(default=""),
    old_name: str = Form(default=""),
    new_name: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not _admin_guard(user):
        return RedirectResponse(url="/shops", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(
            url="/shops?error=Недействительный+CSRF-токен", status_code=302
        )

    old_clean = old_name.strip()
    new_clean = new_name.strip()

    if not old_clean:
        return RedirectResponse(
            url=f"/shops?error={quote('Не указано текущее название.')}", status_code=302
        )
    if not new_clean or len(new_clean) < 2:
        return RedirectResponse(
            url=f"/shops?error={quote('Новое название должно содержать не менее 2 символов.')}",
            status_code=302,
        )
    if len(new_clean) > 60:
        return RedirectResponse(
            url=f"/shops?error={quote('Название не должно превышать 60 символов.')}",
            status_code=302,
        )
    if old_clean == new_clean:
        return RedirectResponse(url="/shops", status_code=302)

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        existing = db.get_all_shops()
        if new_clean in existing:
            return RedirectResponse(
                url=f"/shops?error={quote('Магазин с таким названием уже существует.')}",
                status_code=302,
            )
        db.rename_shop_everywhere(old_clean, new_clean)
        return RedirectResponse(
            url=f"/shops?success={quote('Магазин переименован в «' + new_clean + '».')}",
            status_code=303,
        )
    except Exception as exc:
        logging.error(f"shops_rename error: {exc}")
        return RedirectResponse(
            url=f"/shops?error={quote(str(exc))}", status_code=302
        )


@router.post("/shops/delete")
def shops_delete(
    request: Request,
    csrf_token: str = Form(default=""),
    name: str = Form(default=""),
    confirm: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not _admin_guard(user):
        return RedirectResponse(url="/shops", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(
            url="/shops?error=Недействительный+CSRF-токен", status_code=302
        )

    name_clean = name.strip()
    if not name_clean:
        return RedirectResponse(
            url=f"/shops?error={quote('Не указано название магазина.')}", status_code=302
        )
    if confirm != "yes":
        return RedirectResponse(
            url=f"/shops?error={quote('Удаление не подтверждено.')}", status_code=302
        )

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        affected = db.delete_shop_everywhere(name_clean)
        msg = f"Магазин «{name_clean}» удалён."
        if affected:
            msg += f" У {affected} сотр. сброшен магазин."
        return RedirectResponse(
            url=f"/shops?success={quote(msg)}", status_code=303
        )
    except Exception as exc:
        logging.error(f"shops_delete error: {exc}")
        return RedirectResponse(
            url=f"/shops?error={quote(str(exc))}", status_code=302
        )
