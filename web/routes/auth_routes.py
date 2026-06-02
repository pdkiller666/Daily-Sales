import os
import time as _time
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()

TOKEN_EXPIRE_DAYS = 7

# Rate limiting for /auth/code/auto — 5 attempts per 60 s per IP
_code_attempt_log: dict = {}
_RATE_WINDOW = 60
_RATE_MAX = 5


def _check_rate_limit(ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = _time.time()
    hits = [t for t in _code_attempt_log.get(ip, []) if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_MAX:
        return False
    hits.append(now)
    _code_attempt_log[ip] = hits
    return True


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
    from web.deps import get_user_org_db_path, get_user_role_from_db, get_first_available_org_db
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
        # Super_admin may not be in any org mapping — find the first available org
        if not org_db:
            org_db = get_first_available_org_db()

    # Final fallback (shouldn't happen in normal operation)
    if not org_db:
        org_db = 'data/shop_bot.db'

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


@router.post("/switch_org")
async def switch_org(
    request: Request,
    org_db: str = Form(...),
    csrf_token: str = Form(default=""),
):
    """Super_admin org switcher — re-issue JWT with the selected org's DB path."""
    from web.auth import get_session_user, verify_csrf_token, create_session_token, COOKIE_NAME

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/dashboard", status_code=302)

    # Validate: must be a real active org with an existing file
    import sqlite3
    try:
        conn = sqlite3.connect("data/main.db")
        row = conn.execute(
            "SELECT db_path FROM organizations WHERE db_path=? AND is_active=1",
            (org_db,)
        ).fetchone()
        conn.close()
    except Exception:
        row = None

    if not row or not os.path.exists(org_db):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    first_name = user.get("name", "")
    role = user.get("role", "super_admin")

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


@router.get("/auth/code/auto")
async def code_auto_login(request: Request, c: str = ""):
    """Magic-link auto-login: validate code from URL param and issue JWT immediately."""
    from web.auth import create_session_token, COOKIE_NAME
    from web.deps import get_user_org_db_path, get_user_role_from_db, get_first_available_org_db
    from env_manager import env_manager
    from web_login_codes import validate_code

    _ip = (request.client.host if request.client else "unknown")
    if not _check_rate_limit(_ip):
        return RedirectResponse(url="/auth/code?error=Слишком+много+попыток%2C+подождите+минуту", status_code=302)

    telegram_id = validate_code(c.strip())
    if not telegram_id:
        return RedirectResponse(url="/auth/code?error=Неверный+или+просроченный+код", status_code=302)

    org_db = get_user_org_db_path(telegram_id)
    role = get_user_role_from_db(telegram_id)

    if env_manager.is_super_admin(telegram_id):
        role = 'super_admin'
        if not org_db:
            org_db = get_first_available_org_db()

    if not org_db:
        org_db = 'data/shop_bot.db'

    first_name = _get_display_name(telegram_id, org_db)
    token = create_session_token(telegram_id, first_name, org_db, role)
    # Stay on current page if referer is one of our own pages
    referer = request.headers.get("referer", "")
    try:
        from urllib.parse import urlparse
        ref_path = urlparse(referer).path or "/dashboard"
        _safe = ("/dashboard", "/sales", "/products", "/inventory", "/reports",
                 "/rankings", "/staff", "/plans", "/salary", "/schedule",
                 "/settings", "/integration", "/contests")
        redirect_to = ref_path if any(ref_path.startswith(p) for p in _safe) else "/dashboard"
    except Exception:
        redirect_to = "/dashboard"
    response = RedirectResponse(url=redirect_to, status_code=302)
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True,
        samesite='lax',
        secure=False,
        max_age=TOKEN_EXPIRE_DAYS * 24 * 3600,
    )
    return response


@router.get("/auth/code")
async def code_login_page(request: Request):
    """Show the code-based login form (bot-code alternative to Telegram Widget)."""
    from web.auth import get_session_user
    if get_session_user(request):
        return RedirectResponse(url="/dashboard", status_code=302)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "auth/login.html", {
        "bot_username": "",   # hide widget on code page
        "auth_url": "",
        "error": request.query_params.get("error"),
        "show_code_form": True,
    })


@router.post("/auth/code")
async def code_login_submit(
    request: Request,
    code: str = Form(default=""),
):
    """Validate a one-time bot code and issue a web session JWT."""
    from web.auth import create_session_token, COOKIE_NAME
    from web.deps import get_user_org_db_path, get_user_role_from_db, get_first_available_org_db
    from env_manager import env_manager
    from web_login_codes import validate_code

    _ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(_ip):
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "auth/login.html", {
            "bot_username": "", "auth_url": "",
            "error": "Слишком много попыток. Подождите минуту и попробуйте снова.",
            "show_code_form": True, "code_value": "",
        })

    telegram_id = validate_code(code.strip())
    if not telegram_id:
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "auth/login.html", {
            "bot_username": "",
            "auth_url": "",
            "error": "bad_code",
            "show_code_form": True,
            "code_value": code.strip(),
        })

    org_db = get_user_org_db_path(telegram_id)
    role = get_user_role_from_db(telegram_id)

    if env_manager.is_super_admin(telegram_id):
        role = 'super_admin'
        if not org_db:
            org_db = get_first_available_org_db()

    if not org_db:
        org_db = 'data/shop_bot.db'

    # Get display name from main.db (user_org_mapping → org DB → users)
    first_name = _get_display_name(telegram_id, org_db)

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


def _get_display_name(telegram_id: int, org_db: str) -> str:
    """Try to fetch first_name for the user from their org DB or fallback."""
    try:
        from database import Database
        if org_db and org_db != 'data/shop_bot.db' and os.path.exists(org_db):
            db = Database(org_db)
            conn = db.get_connection()
            row = conn.execute(
                "SELECT first_name FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    return "Пользователь"


@router.get("/logout")
@router.post("/logout")
async def logout(request: Request):
    from web.auth import COOKIE_NAME
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
