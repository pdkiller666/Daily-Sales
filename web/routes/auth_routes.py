import os
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    from web.auth import get_session_user
    if get_session_user(request):
        return RedirectResponse(url="/dashboard", status_code=302)
    templates = request.app.state.templates
    import bot_holder
    bot_username = bot_holder.get_username() or os.getenv('BOT_USERNAME', '')
    base_url = str(request.base_url).rstrip('/')
    return templates.TemplateResponse(request, "auth/login.html", {
        "bot_username": bot_username,
        "auth_url": f"{base_url}/auth/telegram/callback",
        "error": request.query_params.get("error"),
    })


@router.get("/auth/telegram/callback")
async def telegram_callback(request: Request):
    from web.auth import verify_telegram_auth, create_session_token, COOKIE_NAME
    from web.deps import get_user_org_db_path, get_user_role_from_db
    from env_manager import env_manager

    params = dict(request.query_params)
    if not params.get('hash'):
        return RedirectResponse(url="/login?error=no_hash", status_code=302)

    if not verify_telegram_auth(dict(params)):
        return RedirectResponse(url="/login?error=invalid", status_code=302)

    telegram_id = int(params.get('id', 0))
    first_name = params.get('first_name', 'Пользователь')

    org_db = get_user_org_db_path(telegram_id)
    role = get_user_role_from_db(telegram_id)
    if env_manager.is_super_admin(telegram_id):
        role = 'super_admin'

    token = create_session_token(telegram_id, first_name, org_db, role)

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True,
        samesite='lax',
        secure=False,
        max_age=TOKEN_EXPIRE_DAYS * 24 * 3600,
    )
    return response


TOKEN_EXPIRE_DAYS = 30


@router.get("/logout")
@router.post("/logout")
async def logout(request: Request):
    from web.auth import COOKIE_NAME
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
