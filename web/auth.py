import os
import hmac
import hashlib
import time
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set. "
        "Set it in Replit Secrets before starting the application."
    )
_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
ALGORITHM = "HS256"
COOKIE_NAME = "web_session"
TOKEN_EXPIRE_DAYS = 7


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


def create_session_token(telegram_id: int, first_name: str, org_db: str, role: str) -> str:
    payload = {
        'sub': str(telegram_id),
        'name': first_name,
        'org_db': org_db,
        'role': role,
        'exp': datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, _SECRET, algorithm=ALGORITHM)


def decode_session_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _SECRET, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_session_user(request) -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return decode_session_token(token)


def get_csrf_token(request) -> str:
    """Derive a CSRF token from the session JWT (deterministic per session)."""
    jwt_val = request.cookies.get(COOKIE_NAME, '')
    return hmac.new(_SECRET.encode(), jwt_val.encode(), hashlib.sha256).hexdigest()[:32]


def verify_csrf_token(request, form_token: str) -> bool:
    """Return True if the form's CSRF token matches the session-derived token."""
    expected = get_csrf_token(request)
    return bool(form_token) and hmac.compare_digest(expected, form_token)


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
