"""TOTP-2FA helpers for web cabinet owners.

Wraps pyotp + qrcode. Recovery codes are stored hashed (sha256) as a JSON list
so a DB leak does not expose usable codes.
"""
import base64
import hashlib
import hmac
import io
import json
import secrets as _secrets
from typing import Optional

import pyotp

_ISSUER = "DailySales"


def generate_secret() -> str:
    """Random base32 TOTP secret."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str) -> str:
    """otpauth:// URI for authenticator apps."""
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=account or "user", issuer_name=_ISSUER
    )


def verify_code(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code (±1 step window for clock drift)."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False


def qr_data_uri(uri: str) -> str:
    """Render the provisioning URI as a base64 PNG data: URI for an <img> tag."""
    try:
        import qrcode
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def generate_recovery_codes(n: int = 8) -> tuple[list[str], str]:
    """Return (plain_codes, hashed_json). Show plain codes to the user ONCE."""
    plain = []
    for _ in range(n):
        raw = _secrets.token_hex(5)  # 10 hex chars
        plain.append(f"{raw[:5]}-{raw[5:]}")
    hashed = json.dumps([_hash_code(c) for c in plain])
    return plain, hashed


def consume_recovery_code(recovery_json: Optional[str], code: str) -> tuple[bool, Optional[str]]:
    """If ``code`` matches a stored recovery hash, remove it and return
    (True, new_json). Otherwise (False, None)."""
    if not recovery_json or not code:
        return False, None
    code = code.strip().lower().replace(" ", "")
    try:
        hashes = json.loads(recovery_json)
    except Exception:
        return False, None
    target = _hash_code(code)
    for h in hashes:
        if hmac.compare_digest(h, target):
            remaining = [x for x in hashes if not hmac.compare_digest(x, target)]
            return True, json.dumps(remaining)
    return False, None
