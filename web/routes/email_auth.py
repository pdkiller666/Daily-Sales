"""Email + password authentication routes for DailySales."""
import os
import uuid
import time
import logging
import sqlite3

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()
logger = logging.getLogger(__name__)

SHOP_BOT_DB = 'data/shop_bot.db'
# Keep in sync with web/auth.py TOKEN_EXPIRE_DAYS (7 days)
# The JWT itself expires in 7 days — cookie must not outlive it
TOKEN_EXPIRE_DAYS = 7


def _rate_ok(ip: str, prefix: str = 'email_auth') -> bool:
    try:
        from web.rate_store import check_rate_limit
        return check_rate_limit(f"{prefix}:{ip}", max_requests=5, window_seconds=600)
    except Exception:
        return True


def _shop_db():
    from database import Database
    return Database(SHOP_BOT_DB)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip('/')


def _name_from_org(tg_id: int, org_db: str) -> str:
    try:
        from database import Database
        if org_db and os.path.exists(org_db):
            db = Database(org_db)
            conn = db.get_connection()
            try:
                row = conn.execute(
                    "SELECT first_name FROM users WHERE telegram_id=?", (tg_id,)
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    return "Пользователь"


def _add_org_mapping(synthetic_tg_id: int, org_id: int, role: str = 'user'):
    try:
        conn = sqlite3.connect('data/main.db')
        try:
            conn.execute(
                "INSERT OR IGNORE INTO user_org_mapping (telegram_id, org_id, role, is_active) "
                "VALUES (?, ?, ?, 1)",
                (synthetic_tg_id, org_id, role)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("_add_org_mapping: %s", exc)


def _make_2fa_pending(cred_id: int) -> str:
    """Short-lived signed token proving 'password stage passed' for cred_id."""
    import hmac, hashlib, time as _t
    from web.auth import _SECRET
    exp = int(_t.time()) + 300
    body = f"{cred_id}.{exp}"
    sig = hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def _check_2fa_pending(token: str) -> int | None:
    import hmac, hashlib, time as _t
    from web.auth import _SECRET
    try:
        cred_id, exp, sig = token.split(".")
        body = f"{cred_id}.{exp}"
        expect = hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expect, sig):
            return None
        if int(exp) < int(_t.time()):
            return None
        return int(cred_id)
    except Exception:
        return None


def _issue_session_response(request: Request, cred: dict, tg_id: int):
    """Build the authenticated session cookie + redirect for a verified cred.
    Shared by password-only login and the post-2FA path."""
    from web.auth import create_session_token, COOKIE_NAME
    from web.deps import (get_user_org_db_path, get_user_role_from_db,
                          get_first_available_org_db)
    from env_manager import env_manager

    org_db = get_user_org_db_path(tg_id)
    role = get_user_role_from_db(tg_id)

    if env_manager.is_super_admin(tg_id):
        role = 'super_admin'
        if not org_db:
            org_db = get_first_available_org_db()

    if not org_db:
        org_db = cred.get('org_db') or SHOP_BOT_DB

    first_name = cred.get('first_name') or _name_from_org(tg_id, org_db)

    try:
        _shop_db().update_web_last_login(cred['id'])
    except Exception:
        pass

    token = create_session_token(tg_id, first_name, org_db, role)

    try:
        from web.login_notif import _real_ip, check_and_record_ip, notify_new_ip
        from bot_holder import get_bot as _get_bot
        import asyncio as _aio
        _notif_ip = _real_ip(request)
        if check_and_record_ip(tg_id, _notif_ip):
            _bot = _get_bot()
            if _bot:
                _aio.create_task(notify_new_ip(_bot, tg_id, _notif_ip, first_name))
    except Exception:
        pass

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite='lax',
                        secure=True, max_age=TOKEN_EXPIRE_DAYS * 24 * 3600)
    return response


# ── POST /auth/email  (email + password login) ────────────────────────────────

@router.post("/auth/email")
async def email_login(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    login_nonce: str = Form(default=""),
):
    from web.auth import (verify_login_nonce, generate_login_nonce,
                          create_session_token, COOKIE_NAME, verify_password)
    from web.deps import get_user_org_db_path, get_user_role_from_db, get_first_available_org_db
    from env_manager import env_manager
    import bot_holder

    templates = request.app.state.templates

    def _err(msg: str):
        bot_username = bot_holder.get_username() or os.getenv('BOT_USERNAME', '')
        return templates.TemplateResponse(request, "auth/login.html", {
            "bot_username": bot_username,
            "auth_url": f"{_base_url(request)}/auth/telegram/callback",
            "error": msg,
            "show_email_form": True,
            "email_value": email.strip(),
            "login_nonce": generate_login_nonce(),
        })

    if not verify_login_nonce(login_nonce):
        return _err("Форма устарела. Обновите страницу и попробуйте снова.")

    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip):
        return _err("Слишком много попыток. Подождите 10 минут.")

    if not email or not password:
        return _err("Введите email и пароль.")

    try:
        db = _shop_db()
        cred = db.get_web_credential_by_email(email.strip().lower())
    except Exception as exc:
        logger.error("email_login db: %s", exc)
        return _err("Ошибка сервера. Попробуйте позже.")

    if not cred or not verify_password(password, cred['password_hash']):
        return _err("Неверный email или пароль.")

    tg_id = cred.get('telegram_id') or cred.get('synthetic_tg_id')
    if not tg_id:
        return _err("Аккаунт не привязан к организации. Обратитесь к администратору.")

    # Двухфакторная аутентификация (TOTP): пароль верен — спрашиваем код
    if cred.get('totp_enabled') and cred.get('totp_secret'):
        return templates.TemplateResponse(request, "auth/twofa.html", {
            "pending": _make_2fa_pending(cred['id']),
            "login_nonce": generate_login_nonce(),
            "error": "",
        })

    return _issue_session_response(request, cred, tg_id)


# ── POST /auth/2fa  (второй фактор после пароля) ──────────────────────────────

@router.post("/auth/2fa")
async def email_login_2fa(
    request: Request,
    code: str = Form(default=""),
    pending: str = Form(default=""),
    login_nonce: str = Form(default=""),
):
    from web.auth import verify_login_nonce, generate_login_nonce
    from web import totp_utils

    templates = request.app.state.templates

    def _err(msg: str, keep_pending: str = ""):
        return templates.TemplateResponse(request, "auth/twofa.html", {
            "pending": keep_pending or _make_2fa_pending(_check_2fa_pending(pending) or 0),
            "login_nonce": generate_login_nonce(),
            "error": msg,
        })

    if not verify_login_nonce(login_nonce):
        return RedirectResponse(url="/login", status_code=302)

    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip, prefix='email_2fa'):
        return _err("Слишком много попыток. Подождите 10 минут.")

    cred_id = _check_2fa_pending(pending)
    if not cred_id:
        return RedirectResponse(url="/login", status_code=302)

    try:
        db = _shop_db()
        cred = db.get_web_credential_by_id(cred_id)
    except Exception as exc:
        logger.error("email_2fa db: %s", exc)
        return _err("Ошибка сервера. Попробуйте позже.")

    if not cred or not cred.get('totp_enabled'):
        return RedirectResponse(url="/login", status_code=302)

    tg_id = cred.get('telegram_id') or cred.get('synthetic_tg_id')
    if not tg_id:
        return RedirectResponse(url="/login", status_code=302)

    code = (code or "").strip()
    ok = totp_utils.verify_code(cred.get('totp_secret') or "", code)
    if not ok:
        # Попытка использовать код восстановления
        used, new_json = totp_utils.consume_recovery_code(cred.get('totp_recovery'), code)
        if used:
            try:
                db.set_web_totp(cred_id, cred.get('totp_secret'), 1, new_json)
            except Exception:
                pass
            ok = True

    if not ok:
        return _err("Неверный код. Попробуйте ещё раз.", keep_pending=_make_2fa_pending(cred_id))

    return _issue_session_response(request, cred, tg_id)


# ── GET /register  (Phase 2 — new user without Telegram) ──────────────────────

def _email_registration_enabled() -> bool:
    """Check payment_settings.email_registration_enabled (default: True)."""
    try:
        import sqlite3 as _sl3
        _c = _sl3.connect("data/shop_bot.db")
        try:
            _r = _c.execute(
                "SELECT value FROM payment_settings WHERE key='email_registration_enabled'"
            ).fetchone()
        finally:
            _c.close()
        return _r is None or _r[0] != "0"
    except Exception:
        return True


@router.get("/register")
async def register_page(request: Request):
    from web.auth import get_session_user, generate_login_nonce
    if get_session_user(request):
        return RedirectResponse(url="/dashboard", status_code=302)
    templates = request.app.state.templates
    if not _email_registration_enabled():
        return templates.TemplateResponse(request, "auth/register.html", {
            "error": "Регистрация через email временно отключена администратором.",
            "login_nonce": generate_login_nonce(),
        }, status_code=403)
    return templates.TemplateResponse(request, "auth/register.html", {
        "error": request.query_params.get("error"),
        "login_nonce": generate_login_nonce(),
    })


# ── POST /register ────────────────────────────────────────────────────────────

@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    password2: str = Form(default=""),
    first_name: str = Form(default=""),
    invite_code: str = Form(default=""),
    login_nonce: str = Form(default=""),
    consent: str = Form(default=""),
):
    from web.auth import (verify_login_nonce, generate_login_nonce,
                          create_session_token, COOKIE_NAME, hash_password)
    from web.email_utils import send_verification_email, is_configured as email_ok

    templates = request.app.state.templates

    def _err(msg: str):
        return templates.TemplateResponse(request, "auth/register.html", {
            "error": msg,
            "login_nonce": generate_login_nonce(),
            "email_value": email,
            "first_name_value": first_name,
            "invite_code_value": invite_code,
        })

    if not _email_registration_enabled():
        return _err("Регистрация через email временно отключена администратором.")

    if not verify_login_nonce(login_nonce):
        return _err("Форма устарела. Обновите страницу.")

    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip, 'register'):
        return _err("Слишком много попыток. Подождите 10 минут.")

    email = email.strip().lower()
    first_name = first_name.strip()[:64]
    invite_code = invite_code.strip().upper()

    if not email or '@' not in email or '.' not in email.split('@')[-1]:
        return _err("Введите корректный email-адрес.")
    if not password or len(password) < 8:
        return _err("Пароль должен содержать не менее 8 символов.")
    if password != password2:
        return _err("Пароли не совпадают.")
    if not first_name:
        return _err("Введите ваше имя.")
    if not invite_code:
        return _err("Введите инвайт-код организации.")
    if not consent:
        return _err("Необходимо согласиться с политикой обработки персональных данных.")

    try:
        conn = sqlite3.connect('data/main.db')
        try:
            org_row = conn.execute(
                "SELECT id, name, db_path FROM organizations WHERE invite_code=? AND is_active=1",
                (invite_code,)
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("register main.db: %s", exc)
        return _err("Ошибка проверки инвайт-кода. Попробуйте позже.")

    if not org_row:
        return _err("Инвайт-код не найден или недействителен.")

    org_id, org_name, org_db = org_row

    try:
        db = _shop_db()

        if db.get_web_credential_by_email(email):
            return _err("Этот email уже зарегистрирован.")

        pw_hash = hash_password(password)
        cred_id = db.create_web_credential(email, pw_hash, telegram_id=None)
        if not cred_id:
            return _err("Ошибка создания аккаунта. Попробуйте позже.")

        synthetic_tg_id = -(10_000_000 + cred_id)
        db.set_web_synthetic_tg_id(cred_id, synthetic_tg_id, org_db, first_name)
        # Записываем момент получения согласия с политикой ПДн
        try:
            import sqlite3 as _sqlite3
            _sc = _sqlite3.connect('data/shop_bot.db')
            _sc.execute(
                "UPDATE web_credentials SET consent_at=datetime('now') WHERE id=?",
                (cred_id,)
            )
            _sc.commit()
            _sc.close()
        except Exception:
            pass

        from database import Database
        if os.path.exists(org_db):
            org_db_obj = Database(org_db)
            org_db_obj.add_user(
                telegram_id=synthetic_tg_id,
                first_name=first_name,
                last_name='',
                email=email,
            )

        _add_org_mapping(synthetic_tg_id, org_id, role='user')

        if email_ok():
            tok = str(uuid.uuid4())
            db.set_web_verify_token(cred_id, tok, int(time.time()) + 86400)
            send_verification_email(email, f"{_base_url(request)}/auth/verify?t={tok}")

    except Exception as exc:
        logger.error("register: %s", exc)
        return _err("Ошибка регистрации. Попробуйте позже.")

    token = create_session_token(synthetic_tg_id, first_name, org_db, 'user')
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite='lax',
                        secure=True, max_age=TOKEN_EXPIRE_DAYS * 24 * 3600)
    return response


# ── GET /auth/verify ──────────────────────────────────────────────────────────

@router.get("/auth/verify")
async def verify_email(request: Request, t: str = ""):
    templates = request.app.state.templates
    try:
        cred_id = _shop_db().verify_web_email_token(t) if t else None
    except Exception:
        cred_id = None
    status = "ok" if cred_id else "error"
    msg = ("Email успешно подтверждён! Теперь вы можете войти."
           if cred_id else "Ссылка устарела или уже использована.")
    return templates.TemplateResponse(request, "auth/verify_sent.html",
                                      {"status": status, "msg": msg})


# ── GET + POST /auth/reset ────────────────────────────────────────────────────

@router.get("/auth/reset")
async def reset_page(request: Request):
    from web.auth import generate_login_nonce
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "auth/reset_request.html", {
        "login_nonce": generate_login_nonce(),
        "sent": request.query_params.get("sent") == "1",
        "error": request.query_params.get("error"),
    })


@router.post("/auth/reset")
async def reset_submit(
    request: Request,
    email: str = Form(default=""),
    login_nonce: str = Form(default=""),
):
    from web.auth import verify_login_nonce, generate_login_nonce
    from web.email_utils import send_reset_email, is_configured as email_ok
    templates = request.app.state.templates

    def _err(msg: str):
        return templates.TemplateResponse(request, "auth/reset_request.html", {
            "login_nonce": generate_login_nonce(), "error": msg, "email_value": email,
        })

    if not verify_login_nonce(login_nonce):
        return _err("Форма устарела. Обновите страницу.")

    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip, 'reset'):
        return _err("Слишком много попыток. Подождите 10 минут.")

    email = email.strip().lower()
    if not email or '@' not in email:
        return _err("Введите корректный email-адрес.")

    if not email_ok():
        return _err("Отправка писем временно недоступна. Обратитесь к администратору.")

    try:
        db = _shop_db()
        cred = db.get_web_credential_by_email(email)
        if cred:
            tok = str(uuid.uuid4())
            db.set_web_reset_token(email, tok, int(time.time()) + 3600)
            send_reset_email(email, f"{_base_url(request)}/auth/reset/confirm?t={tok}")
    except Exception as exc:
        logger.error("reset_submit: %s", exc)

    return RedirectResponse(url="/auth/reset?sent=1", status_code=302)


# ── GET + POST /auth/reset/confirm ────────────────────────────────────────────

@router.get("/auth/reset/confirm")
async def reset_confirm_page(request: Request, t: str = ""):
    from web.auth import generate_login_nonce
    templates = request.app.state.templates
    if not t:
        return RedirectResponse(url="/auth/reset?error=invalid", status_code=302)
    try:
        cred = _shop_db().verify_web_reset_token(t)
    except Exception:
        cred = None
    return templates.TemplateResponse(request, "auth/reset_confirm.html", {
        "token": t if cred else "",
        "login_nonce": generate_login_nonce(),
        "error": None if cred else "Ссылка устарела или недействительна.",
    })


@router.post("/auth/reset/confirm")
async def reset_confirm_submit(
    request: Request,
    token: str = Form(default=""),
    password: str = Form(default=""),
    password2: str = Form(default=""),
    login_nonce: str = Form(default=""),
):
    from web.auth import verify_login_nonce, generate_login_nonce, hash_password
    templates = request.app.state.templates

    def _err(msg: str):
        return templates.TemplateResponse(request, "auth/reset_confirm.html", {
            "error": msg, "token": token, "login_nonce": generate_login_nonce(),
        })

    if not verify_login_nonce(login_nonce):
        return _err("Форма устарела. Обновите страницу.")
    if not password or len(password) < 8:
        return _err("Пароль должен содержать не менее 8 символов.")
    if password != password2:
        return _err("Пароли не совпадают.")

    try:
        db = _shop_db()
        cred = db.verify_web_reset_token(token)
        if not cred:
            return _err("Ссылка устарела или недействительна.")
        db.reset_web_password(token, hash_password(password))
    except Exception as exc:
        logger.error("reset_confirm: %s", exc)
        return _err("Ошибка сервера. Попробуйте позже.")

    return RedirectResponse(url="/login?msg=password_reset", status_code=302)


# ── POST /settings/email-setup  (link email to existing TG session) ───────────

@router.post("/settings/email-setup")
async def settings_email_setup(
    request: Request,
    action_email: str = Form(default=""),
    action_password: str = Form(default=""),
    action_password2: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token, hash_password
    from web.email_utils import send_verification_email, send_link_notification, is_configured as email_ok

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings?email_error=csrf", status_code=302)

    email = action_email.strip().lower()
    password = action_password
    password2 = action_password2

    if not email or '@' not in email:
        return RedirectResponse(url="/settings?email_error=invalid_email#security", status_code=302)
    if not password or len(password) < 8:
        return RedirectResponse(url="/settings?email_error=short_password#security", status_code=302)
    if password != password2:
        return RedirectResponse(url="/settings?email_error=passwords_mismatch#security", status_code=302)

    tg_id = int(user["sub"])
    try:
        db = _shop_db()
        existing_by_email = db.get_web_credential_by_email(email)
        existing_by_tg = db.get_web_credential_by_telegram_id(tg_id)

        if existing_by_email and existing_by_email.get('telegram_id') != tg_id:
            return RedirectResponse(url="/settings?email_error=email_taken#security", status_code=302)

        pw_hash = hash_password(password)

        if existing_by_tg:
            db.update_web_credential_email(existing_by_tg['id'], email, pw_hash)
            cred_id = existing_by_tg['id']
        else:
            cred_id = db.create_web_credential(email, pw_hash, telegram_id=tg_id)

        if email_ok():
            tok = str(uuid.uuid4())
            db.set_web_verify_token(cred_id, tok, int(time.time()) + 86400)
            send_verification_email(email, f"{_base_url(request)}/auth/verify?t={tok}")
            try:
                send_link_notification(email, user.get('name', 'Пользователь'))
            except Exception:
                pass
    except Exception as exc:
        logger.error("settings_email_setup: %s", exc)
        return RedirectResponse(url="/settings?email_error=server_error#security", status_code=302)

    return RedirectResponse(url="/settings?email_saved=1#security", status_code=302)


# ── POST /settings/email-change-password ─────────────────────────────────────

@router.post("/settings/email-change-password")
async def settings_change_password(
    request: Request,
    old_password: str = Form(default=""),
    new_password: str = Form(default=""),
    new_password2: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token, hash_password, verify_password

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings?email_error=csrf#security", status_code=302)

    if not new_password or len(new_password) < 8:
        return RedirectResponse(url="/settings?email_error=short_password#security", status_code=302)
    if new_password != new_password2:
        return RedirectResponse(url="/settings?email_error=passwords_mismatch#security", status_code=302)

    tg_id = int(user["sub"])
    # Rate-limit: эндпоинт проверяет старый пароль → защищаем от перебора (5 / 10 мин).
    try:
        from web.rate_store import check_rate_limit
        if not check_rate_limit(f"pwchange:{tg_id}", max_requests=5, window_seconds=600):
            return RedirectResponse(url="/settings?email_error=rate_limited#security", status_code=302)
    except Exception:
        pass
    try:
        db = _shop_db()
        cred = db.get_web_credential_by_telegram_id(tg_id)
        if not cred:
            return RedirectResponse(url="/settings?email_error=no_cred#security", status_code=302)
        if not verify_password(old_password, cred['password_hash']):
            return RedirectResponse(url="/settings?email_error=wrong_old_password#security", status_code=302)
        db.update_web_password(cred['id'], hash_password(new_password))
    except Exception as exc:
        logger.error("change_password: %s", exc)
        return RedirectResponse(url="/settings?email_error=server_error#security", status_code=302)

    return RedirectResponse(url="/settings?email_saved=1#security", status_code=302)


# ── POST /settings/email-unlink ──────────────────────────────────────────────

@router.post("/settings/email-unlink")
async def settings_email_unlink(request: Request, csrf_token: str = Form(default="")):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings?email_error=csrf#security", status_code=302)

    tg_id = int(user["sub"])
    if tg_id < 0:
        return RedirectResponse(url="/settings?email_error=cannot_unlink#security", status_code=302)

    try:
        db = _shop_db()
        cred = db.get_web_credential_by_telegram_id(tg_id)
        if cred:
            db.delete_web_credential_by_id(cred['id'])
    except Exception as exc:
        logger.error("settings_email_unlink: %s", exc)
        return RedirectResponse(url="/settings?email_error=server_error#security", status_code=302)

    return RedirectResponse(url="/settings?email_saved=1#security", status_code=302)


# ── POST /settings/email-resend-verify ────────────────────────────────────────

@router.post("/settings/email-resend-verify")
async def settings_resend_verify(request: Request, csrf_token: str = Form(default="")):
    from web.auth import get_session_user, verify_csrf_token
    from web.email_utils import send_verification_email, is_configured as email_ok

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings#security", status_code=302)

    tg_id = int(user["sub"])
    sent = False
    try:
        db = _shop_db()
        cred = db.get_web_credential_by_telegram_id(tg_id)
        if cred and email_ok():
            tok = str(uuid.uuid4())
            db.set_web_verify_token(cred['id'], tok, int(time.time()) + 86400)
            sent = send_verification_email(cred['email'], f"{_base_url(request)}/auth/verify?t={tok}")
    except Exception as exc:
        logger.error("resend_verify: %s", exc)

    if sent:
        return RedirectResponse(url="/settings?email_sent=1#security", status_code=302)
    return RedirectResponse(url="/settings?email_error=smtp_failed#security", status_code=302)
