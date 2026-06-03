"""
One-time web login codes for bot-based authentication.

Flow:
  1. User sends /weblogin to the bot
  2. Bot calls generate_code(telegram_id) and replies with the code
  3. User enters the code on /auth/code in the web interface
  4. Web calls validate_code(code) → gets telegram_id → issues JWT
"""
import sqlite3
import secrets
import string
from datetime import datetime, timedelta

_DB = 'data/main.db'
_CODE_LEN = 8
_TTL_MINUTES = 5


def _init_table():
    conn = sqlite3.connect(_DB)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS web_login_codes (
            code TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def generate_code(telegram_id: int) -> str:
    """Generate a fresh 6-digit code for the user (replaces any previous code)."""
    _init_table()
    code = ''.join(secrets.choice(string.digits) for _ in range(_CODE_LEN))
    expires_at = (datetime.utcnow() + timedelta(minutes=_TTL_MINUTES)).isoformat()
    conn = sqlite3.connect(_DB)
    # Revoke any previous code for this user
    conn.execute("DELETE FROM web_login_codes WHERE telegram_id=?", (telegram_id,))
    # Store new code — use INSERT OR REPLACE to handle rare collision
    conn.execute(
        "INSERT OR REPLACE INTO web_login_codes (code, telegram_id, expires_at) VALUES (?,?,?)",
        (code, telegram_id, expires_at)
    )
    conn.commit()
    conn.close()
    return code


def validate_code(code: str) -> int | None:
    """Return telegram_id if code is valid and not expired, else None. Single-use: deletes on success."""
    _init_table()
    code = (code or "").strip()
    if not code:
        return None
    conn = sqlite3.connect(_DB)
    row = conn.execute(
        "SELECT telegram_id, expires_at FROM web_login_codes WHERE code=?", (code,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM web_login_codes WHERE code=?", (code,))
        conn.commit()
    conn.close()
    if not row:
        return None
    telegram_id, expires_at = row
    if datetime.utcnow().isoformat() > expires_at:
        return None
    return int(telegram_id)


def cleanup_expired():
    """Remove expired codes — call from a periodic scheduler if desired."""
    try:
        _init_table()
        conn = sqlite3.connect(_DB)
        conn.execute(
            "DELETE FROM web_login_codes WHERE expires_at < ?",
            (datetime.utcnow().isoformat(),)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
