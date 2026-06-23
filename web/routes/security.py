"""Security routes: TOTP-2FA management (per-user) + super-admin audit log."""
import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
logger = logging.getLogger(__name__)

SHOP_BOT_DB = 'data/shop_bot.db'


def _db():
    from database import Database
    return Database(SHOP_BOT_DB)


def _tg_id(user) -> int | None:
    try:
        return int(user.get('sub'))
    except (TypeError, ValueError, AttributeError):
        return None


def _cred_for(db, tg_id: int):
    """Web credential by real telegram_id, falling back to synthetic id."""
    cred = db.get_web_credential_by_telegram_id(tg_id)
    if cred:
        return cred
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT * FROM web_credentials WHERE synthetic_tg_id=? LIMIT 1", (tg_id,)
        ).fetchone()
        return db._wc_row(row)
    except Exception:
        return None


# ── POST /settings/2fa/init — generate secret + show QR & recovery codes ──────
@router.post("/settings/2fa/init")
async def twofa_init(request: Request, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web import totp_utils

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings", status_code=303)

    tg_id = _tg_id(user)
    db = _db()
    cred = _cred_for(db, tg_id) if tg_id is not None else None
    if not cred:
        return RedirectResponse(url="/settings?twofa_err=no_cred", status_code=303)
    if cred.get('totp_enabled'):
        return RedirectResponse(url="/settings?twofa_err=already_on", status_code=303)

    secret = totp_utils.generate_secret()
    plain_codes, hashed_json = totp_utils.generate_recovery_codes()
    # Store secret as pending (enabled=0) + recovery codes
    db.set_web_totp(cred['id'], secret, 0, hashed_json)

    uri = totp_utils.provisioning_uri(secret, cred.get('email') or "user")
    qr = totp_utils.qr_data_uri(uri)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "settings/twofa_setup.html", {
        "user": user,
        "secret": secret,
        "qr": qr,
        "recovery_codes": plain_codes,
        "csrf_token": get_csrf_token(request),
        "_p": "/settings",
    })


# ── POST /settings/2fa/enable — confirm with a code, activate ─────────────────
@router.post("/settings/2fa/enable")
async def twofa_enable(request: Request, csrf_token: str = Form(""),
                       code: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web import totp_utils

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings", status_code=303)

    tg_id = _tg_id(user)
    db = _db()
    cred = _cred_for(db, tg_id) if tg_id is not None else None
    if not cred or not cred.get('totp_secret'):
        return RedirectResponse(url="/settings?twofa_err=no_setup", status_code=303)

    if not totp_utils.verify_code(cred['totp_secret'], code):
        return RedirectResponse(url="/settings?twofa_err=bad_code", status_code=303)

    db.set_web_totp(cred['id'], cred['totp_secret'], 1, cred.get('totp_recovery'))
    return RedirectResponse(url="/settings?twofa=on", status_code=303)


# ── POST /settings/2fa/disable — turn off (verify a current code) ─────────────
@router.post("/settings/2fa/disable")
async def twofa_disable(request: Request, csrf_token: str = Form(""),
                        code: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web import totp_utils

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings", status_code=303)

    tg_id = _tg_id(user)
    db = _db()
    cred = _cred_for(db, tg_id) if tg_id is not None else None
    if not cred or not cred.get('totp_enabled'):
        return RedirectResponse(url="/settings", status_code=303)

    ok = totp_utils.verify_code(cred.get('totp_secret') or "", code)
    if not ok:
        used, _ = totp_utils.consume_recovery_code(cred.get('totp_recovery'), code)
        ok = used
    if not ok:
        return RedirectResponse(url="/settings?twofa_err=bad_code", status_code=303)

    db.set_web_totp(cred['id'], None, 0, None)
    return RedirectResponse(url="/settings?twofa=off", status_code=303)


# ── GET /admin/audit — super-admin audit log viewer ───────────────────────────
@router.get("/admin/audit")
async def admin_audit_page(request: Request, page: int = 0):
    from web.auth import get_session_user, get_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    role = user.get('role') if isinstance(user, dict) else None
    if role != 'super_admin':
        return RedirectResponse(url="/dashboard", status_code=303)

    page = max(0, int(page or 0))
    per = 100
    entries = _db().get_admin_audit(limit=per, offset=page * per)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "admin/audit.html", {
        "user": user,
        "entries": entries,
        "page": page,
        "has_next": len(entries) == per,
        "csrf_token": get_csrf_token(request),
        "_p": "/admin/audit",
    })
