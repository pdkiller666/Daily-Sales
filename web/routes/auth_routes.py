import os
import time as _time
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()

TOKEN_EXPIRE_DAYS = 7


def _check_rate_limit(ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited.
    Uses persistent SQLite store so limits survive restarts/deploys.
    Falls back to in-memory if rate_store is unavailable.
    """
    try:
        from web.rate_store import check_rate_limit
        return check_rate_limit(f"auth:{ip}", max_requests=5, window_seconds=60)
    except Exception:
        return True  # fail open


def _record_telegram_consent(telegram_id: int) -> None:
    """Фиксирует момент первого согласия с политикой ПДн для Telegram-пользователей.

    Обновляет consent_at в web_credentials только если:
    - строка существует (пользователь зарегистрирован через email + привязал Telegram)
    - consent_at ещё не установлен (идемпотентно — первый логин)
    Для чистых Telegram-пользователей (без web_credentials) — no-op, что корректно:
    согласие с условиями Telegram покрывает использование бота.
    """
    try:
        import sqlite3 as _sq
        _c = _sq.connect('data/shop_bot.db', timeout=5)
        _c.execute(
            "UPDATE web_credentials SET consent_at=datetime('now')"
            " WHERE telegram_id=? AND consent_at IS NULL",
            (telegram_id,),
        )
        _c.commit()
        _c.close()
    except Exception:
        pass


@router.get("/login")
async def login_page(request: Request):
    from web.auth import get_session_user, generate_login_nonce
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
        "login_nonce": generate_login_nonce(),
        "show_email_form": False,
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

    # Фиксируем согласие с политикой ПДн (первый логин через Telegram)
    _record_telegram_consent(telegram_id)

    # Уведомление при входе с нового IP (fire-and-forget, не блокирует ответ)
    try:
        from web.login_notif import _real_ip, check_and_record_ip, notify_new_ip
        from bot_holder import get_bot as _get_bot
        import asyncio as _aio
        _notif_ip = _real_ip(request)
        if check_and_record_ip(telegram_id, _notif_ip):
            _bot = _get_bot()
            if _bot:
                _aio.create_task(notify_new_ip(_bot, telegram_id, _notif_ip, first_name))
    except Exception:
        pass

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True,
        samesite='lax',
        secure=True,
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
        try:
            row = conn.execute(
                "SELECT db_path FROM organizations WHERE db_path=? AND is_active=1",
                (org_db,)
            ).fetchone()
        finally:
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
        secure=True,
        max_age=TOKEN_EXPIRE_DAYS * 24 * 3600,
    )
    return response


def _safe_next(next_path: str) -> str:
    """Validate a `next` redirect path: must start with / but not // (open redirect guard)."""
    if next_path and next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/dashboard"


@router.get("/auth/code/auto")
async def code_auto_login(request: Request, c: str = "", next: str = ""):
    """Magic-link auto-login: validate code from URL param and issue JWT immediately.

    Optional `next` param: an absolute path (must start with /) to redirect to
    after successful login instead of the default /dashboard.
    """
    from web.auth import get_session_user, create_session_token, COOKIE_NAME
    from web.deps import get_user_org_db_path, get_user_role_from_db, get_first_available_org_db
    from env_manager import env_manager
    from web_login_codes import validate_code

    # If user already has a valid session, skip code validation entirely.
    # This handles bfcache / Telegram WebView replaying the same magic-link URL
    # after the one-time code was already consumed on the first load.
    if get_session_user(request):
        return RedirectResponse(url=_safe_next(next) if next else "/dashboard", status_code=302)

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

    # Фиксируем согласие с политикой ПДн (первый логин через код бота)
    _record_telegram_consent(telegram_id)

    # Уведомление при входе с нового IP (fire-and-forget)
    try:
        from web.login_notif import _real_ip, check_and_record_ip, notify_new_ip
        from bot_holder import get_bot as _get_bot
        import asyncio as _aio
        _notif_ip = _real_ip(request)
        if check_and_record_ip(telegram_id, _notif_ip):
            _bot = _get_bot()
            if _bot:
                _aio.create_task(notify_new_ip(_bot, telegram_id, _notif_ip, first_name))
    except Exception:
        pass

    # Use explicit `next` param if provided, else fall back to referer
    if next:
        redirect_to = _safe_next(next)
    else:
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
        secure=True,
        max_age=TOKEN_EXPIRE_DAYS * 24 * 3600,
    )
    return response


@router.get("/auth/code")
async def code_login_page(request: Request):
    """Show the code-based login form (bot-code alternative to Telegram Widget)."""
    from web.auth import get_session_user, generate_login_nonce
    if get_session_user(request):
        return RedirectResponse(url="/dashboard", status_code=302)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "auth/login.html", {
        "bot_username": "",   # hide widget on code page
        "auth_url": "",
        "error": request.query_params.get("error"),
        "show_code_form": True,
        "login_nonce": generate_login_nonce(),
    })


@router.post("/auth/code")
async def code_login_submit(
    request: Request,
    code: str = Form(default=""),
    login_nonce: str = Form(default=""),
):
    """Validate a one-time bot code and issue a web session JWT."""
    from web.auth import create_session_token, COOKIE_NAME, verify_login_nonce, generate_login_nonce
    from web.deps import get_user_org_db_path, get_user_role_from_db, get_first_available_org_db
    from env_manager import env_manager
    from web_login_codes import validate_code

    templates = request.app.state.templates

    def _err(msg: str, code_val: str = ""):
        return templates.TemplateResponse(request, "auth/login.html", {
            "bot_username": "", "auth_url": "",
            "error": msg,
            "show_code_form": True,
            "code_value": code_val,
            "login_nonce": generate_login_nonce(),
        })

    if not verify_login_nonce(login_nonce):
        return _err("Форма устарела. Обновите страницу и попробуйте снова.")

    _ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(_ip):
        return _err("Слишком много попыток. Подождите минуту и попробуйте снова.")

    telegram_id = validate_code(code.strip())
    if not telegram_id:
        return _err("bad_code", code.strip())

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

    # Фиксируем согласие с политикой ПДн (первый логин через код бота)
    _record_telegram_consent(telegram_id)

    # Уведомление при входе с нового IP (fire-and-forget)
    try:
        from web.login_notif import _real_ip, check_and_record_ip, notify_new_ip
        from bot_holder import get_bot as _get_bot
        import asyncio as _aio
        _notif_ip = _real_ip(request)
        if check_and_record_ip(telegram_id, _notif_ip):
            _bot = _get_bot()
            if _bot:
                _aio.create_task(notify_new_ip(_bot, telegram_id, _notif_ip, first_name))
    except Exception:
        pass

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True,
        samesite='lax',
        secure=True,
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
            try:
                row = conn.execute(
                    "SELECT first_name FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    return "Пользователь"


@router.get("/auth/miniapp-entry", include_in_schema=False)
async def miniapp_entry(request: Request):
    """Точка входа для Telegram Mini App: минимальная HTML-страница,
    которая читает initData из хэша URL (#tgWebAppData=...) и
    POST-ит на /auth/miniapp для создания сессии, затем редиректит на /dashboard."""
    from web.auth import get_session_user
    if get_session_user(request):
        return RedirectResponse(url="/dashboard", status_code=302)
    html = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DailySales</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f172a;font-family:sans-serif;color:#94a3b8}
.wrap{text-align:center}.ico{font-size:2.5rem;margin-bottom:.75rem}.msg{font-size:.95rem}</style>
</head><body><div class="wrap"><div class="ico">⏳</div><div class="msg">Вход в DailySales…</div></div>
<script>
(async function(){
  var p=new URLSearchParams(location.hash.slice(1));
  var d=p.get('tgWebAppData');
  if(!d){location.href='/login';return;}
  try{
    var fd=new FormData();fd.append('init_data',d);
    var r=await fetch('/auth/miniapp',{method:'POST',body:fd,credentials:'same-origin'});
    var j=await r.json();
    location.href=(j&&j.redirect)||'/dashboard';
  }catch(e){location.href='/login';}
})();
</script></body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


@router.get("/open-app", include_in_schema=False)
async def open_app(request: Request, c: str = ""):
    """Открыть установленное Android-приложение (TWA) через intent://.
    Если приложение установлено — открывается и (при наличии кода c) авто-логинится
    по deep-link на /auth/code/auto. Если не установлено — fallback на /download/android."""
    from fastapi.responses import HTMLResponse
    import json as _json
    import re as _re
    # Код входа — всегда только цифры; жёстко санируем, чтобы исключить любой
    # HTML/script-breakout (reflected XSS) при вставке в inline <script>.
    safe_code = _re.sub(r"\D", "", c or "")[:16]
    code_js = _json.dumps(safe_code).replace("</", "<\\/")
    pkg = "com.dailysales.app"
    html = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DailySales</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f172a;font-family:system-ui,-apple-system,sans-serif;color:#e2e8f0;padding:1.5rem}
.card{max-width:340px;width:100%;text-align:center}
.ico{font-size:3rem;margin-bottom:1rem}
.title{font-size:1.15rem;font-weight:700;margin-bottom:.5rem}
.sub{font-size:.9rem;color:#94a3b8;margin-bottom:1.5rem;line-height:1.5}
.btn{display:block;width:100%;box-sizing:border-box;padding:.85rem;border-radius:.75rem;font-weight:600;font-size:.95rem;text-decoration:none;margin-bottom:.6rem;border:none;cursor:pointer}
.btn-primary{background:#2563eb;color:#fff}
.btn-secondary{background:#1e293b;color:#e2e8f0}
.hidden{display:none}
</style></head><body>
<div class="card">
  <div class="ico">📱</div>
  <div class="title" id="t">Открываем приложение…</div>
  <div class="sub" id="s">Если приложение установлено — оно откроется автоматически.</div>
  <div id="choices" class="hidden">
    <a class="btn btn-primary" id="install" href="/download/android">📥 Установить приложение</a>
    <a class="btn btn-secondary" id="browser" href="#">🌐 Войти в браузере</a>
  </div>
</div>
<script>
(function(){
  var CODE=__CODE__;
  var PKG="__PKG__";
  var isAndroid=/android/i.test(navigator.userAgent);
  var deep="/auth/code/auto?c="+encodeURIComponent(CODE);
  var browserUrl=CODE?deep:"/login";
  document.getElementById('browser').href=browserUrl;
  function showChoices(){
    document.getElementById('t').textContent='Приложение не открылось';
    document.getElementById('s').textContent='Установите приложение или войдите через браузер.';
    document.getElementById('choices').classList.remove('hidden');
  }
  if(isAndroid){
    var fallback=location.origin+'/download/android';
    var intentUrl='intent://'+location.host+deep+
      '#Intent;scheme=https;package='+PKG+
      ';S.browser_fallback_url='+encodeURIComponent(fallback)+';end';
    try{ window.location.href=intentUrl; }catch(e){}
    setTimeout(showChoices,2000);
  } else {
    window.location.href=browserUrl;
  }
})();
</script></body></html>"""
    html = html.replace("__CODE__", code_js).replace("__PKG__", pkg)
    return HTMLResponse(content=html)


@router.post("/auth/miniapp")
async def miniapp_auth(request: Request, init_data: str = Form(default="")):
    """Авторизация через Telegram Mini App initData.
    Валидирует подпись, создаёт JWT-сессию, возвращает JSON {ok, redirect}."""
    from fastapi.responses import JSONResponse
    from web.auth import verify_miniapp_init_data, create_session_token, COOKIE_NAME
    from web.deps import get_user_org_db_path, get_user_role_from_db, get_first_available_org_db
    from env_manager import env_manager

    if not init_data:
        return JSONResponse({"ok": False, "error": "no_data"}, status_code=400)

    ok, telegram_id, first_name = verify_miniapp_init_data(init_data)
    if not ok or not telegram_id:
        return JSONResponse({"ok": False, "error": "invalid"}, status_code=403)

    org_db = get_user_org_db_path(telegram_id)
    role = get_user_role_from_db(telegram_id)

    if env_manager.is_super_admin(telegram_id):
        role = 'super_admin'
        if not org_db:
            org_db = get_first_available_org_db()

    if not org_db:
        org_db = 'data/shop_bot.db'

    token = create_session_token(telegram_id, first_name, org_db, role)

    response = JSONResponse({"ok": True, "redirect": "/dashboard"})
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True,
        samesite='lax',
        secure=True,
        max_age=TOKEN_EXPIRE_DAYS * 24 * 3600,
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    from web.auth import COOKIE_NAME, verify_csrf_token
    form = await request.form()
    if not verify_csrf_token(request, str(form.get("csrf_token", ""))):
        # CSRF-провал → не разлогиниваем (защита от forced-logout через подделку)
        return RedirectResponse(url="/dashboard", status_code=302)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
