import os
import hmac
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import jwt

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set. "
        "Set it in Replit Secrets before starting the application."
    )
# Prefer an independently-configured secret so web sessions are not derivable
# from BOT_TOKEN alone. Set WEB_SECRET_KEY in Replit Secrets for max security.
# Fallback: domain-bound HMAC so the secret is not equal to SHA256(BOT_TOKEN).
_WEB_SECRET_RAW = os.getenv('WEB_SECRET_KEY', '')
if _WEB_SECRET_RAW:
    _SECRET = hashlib.sha256(_WEB_SECRET_RAW.encode()).hexdigest()
else:
    _SECRET = hmac.new(
        BOT_TOKEN.encode(),
        b"dailysales:web:sessions:v1",
        hashlib.sha256,
    ).hexdigest()

_PBKDF2_ITERS = 390_000


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-SHA256 (stdlib, no extra deps)."""
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, _PBKDF2_ITERS)
    return salt.hex() + ':' + key.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time password verification."""
    try:
        salt_hex, key_hex = stored_hash.split(':', 1)
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, _PBKDF2_ITERS)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


ALGORITHM = "HS256"
COOKIE_NAME = "web_session"
TOKEN_EXPIRE_DAYS = 7


def verify_miniapp_init_data(init_data_str: str) -> tuple[bool, int, str]:
    """Validate Telegram Mini App initData string.

    Algorithm differs from Login Widget: secret key = HMAC-SHA256(key=b"WebAppData", msg=bot_token).
    Returns (valid, telegram_id, first_name).
    """
    import json
    from urllib.parse import parse_qs

    token = os.getenv('BOT_TOKEN', '')
    if not token or not init_data_str:
        return False, 0, ''

    try:
        params = parse_qs(init_data_str, keep_blank_values=True)
        flat = {k: v[0] for k, v in params.items()}
    except Exception:
        return False, 0, ''

    received_hash = flat.pop('hash', '')
    if not received_hash:
        return False, 0, ''

    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(flat.items()))
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        return False, 0, ''

    auth_date = int(flat.get('auth_date', 0))
    if time.time() - auth_date > 86400:
        return False, 0, ''

    try:
        user_data = json.loads(flat.get('user', '{}'))
        telegram_id = int(user_data.get('id', 0))
        first_name = user_data.get('first_name', 'Пользователь')
    except Exception:
        return False, 0, ''

    if not telegram_id:
        return False, 0, ''

    return True, telegram_id, first_name


def verify_telegram_auth(data: dict) -> bool:
    token = os.getenv('BOT_TOKEN', '')
    if not token:
        return False
    received_hash = data.get('hash', '')
    if not received_hash:
        return False
    check_data = {k: v for k, v in data.items() if k != 'hash'}
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(check_data.items()))
    secret_key = hashlib.sha256(token.encode()).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return False
    auth_date = int(data.get('auth_date', 0))
    if time.time() - auth_date > 86400:
        return False
    return True


# ── JWT revocation blacklist ──────────────────────────────────────────────────
# In-memory cache: jti → expiry unix timestamp.  Populated from DB on first
# check (after restart), and written to DB on revoke for durability.
_REVOKED: dict[str, float] = {}


def revoke_jti(jti: str, exp: float) -> None:
    """Добавить jti в blacklist (память + shop_bot.db)."""
    if not jti:
        return
    _REVOKED[jti] = exp
    try:
        import sqlite3 as _sq
        from datetime import datetime as _dt
        exp_dt = _dt.utcfromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S')
        _c = _sq.connect('data/shop_bot.db', timeout=3)
        _c.execute(
            "INSERT OR REPLACE INTO revoked_tokens (jti, expires_at) VALUES (?, ?)",
            (jti, exp_dt),
        )
        _c.commit()
        _c.close()
    except Exception:
        pass


def is_jti_revoked(jti: str) -> bool:
    """Проверить, отозван ли токен по jti. Сначала память, потом DB."""
    if not jti:
        return False
    if jti in _REVOKED:
        return True
    # DB-fallback для случая после рестарта сервера
    try:
        import sqlite3 as _sq
        _c = _sq.connect('data/shop_bot.db', timeout=3)
        row = _c.execute(
            "SELECT 1 FROM revoked_tokens WHERE jti=? AND expires_at > datetime('now') LIMIT 1",
            (jti,),
        ).fetchone()
        _c.close()
        if row:
            import time as _t
            _REVOKED[jti] = _t.time() + 604800  # cache hit
            return True
    except Exception:
        pass
    return False


def create_session_token(telegram_id: int, first_name: str, org_db: str, role: str) -> str:
    payload = {
        'sub': str(telegram_id),
        'name': first_name,
        'org_db': org_db,
        'role': role,
        'jti': secrets.token_hex(16),   # уникальный ID токена для revocation
        'exp': datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, _SECRET, algorithm=ALGORITHM)


def decode_session_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def get_session_user(request) -> Optional[dict]:
    # Memoize per request: every Jinja nav/badge global calls this, and each call
    # re-parses the JWT. Cache the result (including None) on request.state.
    state = getattr(request, "state", None)
    if state is not None and hasattr(state, "_session_user"):
        return state._session_user
    token = request.cookies.get(COOKIE_NAME)
    user = decode_session_token(token) if token else None
    # Revocation check: отозванный jti → treat as unauthenticated
    if user and is_jti_revoked(user.get('jti', '')):
        user = None
    if state is not None:
        try:
            state._session_user = user
        except Exception:
            pass
    return user


def _legacy_csrf_token(request) -> str:
    """Old deterministic-per-session token (32 hex chars). Kept for back-compat
    so pages rendered before this deploy keep working until their next reload."""
    jwt_val = request.cookies.get(COOKIE_NAME, '')
    return hmac.new(_SECRET.encode(), jwt_val.encode(), hashlib.sha256).hexdigest()[:32]


def get_csrf_token(request) -> str:
    """Per-request CSRF token, unique on every render.

    Format: ``<nonce>.<hmac(secret, jwt + nonce)[:32]>``. The nonce makes each
    rendered token distinct, while the HMAC binds it to the current session JWT
    so it cannot be forged. Verification accepts ANY validly-signed token for the
    current session (so multiple open tabs / long-lived forms never break).
    """
    jwt_val = request.cookies.get(COOKIE_NAME, '')
    nonce = secrets.token_urlsafe(9)
    sig = hmac.new(
        _SECRET.encode(), f"{jwt_val}.{nonce}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{nonce}.{sig}"


def _verify_one(request, token: str) -> bool:
    """True if ``token`` is a valid CSRF token for the current session JWT.

    Accepts the new per-request format (``nonce.sig``) and the legacy
    deterministic 32-char format (back-compat)."""
    if not token:
        return False
    jwt_val = request.cookies.get(COOKIE_NAME, '')
    if '.' in token:
        nonce, _, sig = token.partition('.')
        if nonce and sig:
            expected = hmac.new(
                _SECRET.encode(), f"{jwt_val}.{nonce}".encode(), hashlib.sha256
            ).hexdigest()[:32]
            if hmac.compare_digest(expected, sig):
                return True
    # Legacy fallback: deterministic per-session token (no nonce).
    return hmac.compare_digest(_legacy_csrf_token(request), token)


def verify_csrf_token(request, form_token: str) -> bool:
    """Return True if a valid CSRF token is present in the form field OR the
    ``X-CSRF-Token`` header (the latter covers HTMX / fetch requests)."""
    if _verify_one(request, form_token or ''):
        return True
    try:
        header_token = request.headers.get('X-CSRF-Token', '') or ''
    except Exception:
        header_token = ''
    return _verify_one(request, header_token)


def generate_login_nonce() -> str:
    """Time-based HMAC nonce for pre-auth CSRF on /auth/code.
    Rotates every 5 minutes; verify_login_nonce accepts current + previous window (~10 min).
    """
    window = int(time.time()) // 300
    return hmac.new(_SECRET.encode(), f"login:{window}".encode(), hashlib.sha256).hexdigest()[:24]


def verify_login_nonce(nonce: str) -> bool:
    """Accept nonce from current or previous 5-minute window."""
    if not nonce:
        return False
    window = int(time.time()) // 300
    for w in (window, window - 1):
        expected = hmac.new(_SECRET.encode(), f"login:{w}".encode(), hashlib.sha256).hexdigest()[:24]
        if hmac.compare_digest(nonce, expected):
            return True
    return False
