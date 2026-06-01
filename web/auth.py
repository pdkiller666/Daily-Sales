import os
import hmac
import hashlib
import time
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest() if BOT_TOKEN else 'dev_secret_shopbot_change_me'
ALGORITHM = "HS256"
COOKIE_NAME = "web_session"
TOKEN_EXPIRE_DAYS = 30


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
