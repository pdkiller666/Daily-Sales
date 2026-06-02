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
        ok = db.add_shop(name_clean)
        if not ok:
            return RedirectResponse(
                url=f"/shops?error={quote('Магазин с таким названием уже существует.')}",
                status_code=302,
            )
        return RedirectResponse(
            url=f"/shops?success={quote('Магазин «' + name_clean + '» создан.')}",
            status_code=303,
        )
    except Exception as exc:
        logging.error(f"shops_create error: {exc}")
        return RedirectResponse(
            url=f"/shops?error={quote(str(exc))}", status_code=302
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
