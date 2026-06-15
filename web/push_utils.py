"""Web Push (VAPID) utility for DailySales.

send_web_push(tg_id, ...)          — sends to all devices of one user (sync).
send_web_push_bulk(tg_ids, ...)    — sends the same notification to many users (sync).
apush / apush_bulk                 — async wrappers (run sync send off the event loop).

All functions are non-blocking in effect: failures are caught and logged, stale
(404/410 Gone) subscriptions are auto-removed.
"""
import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

_SHOP_BOT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "shop_bot.db")


def _log_delivery(tg_id: int, title: str) -> None:
    """Write a push delivery record to push_delivery_log (fire-and-forget)."""
    try:
        import sqlite3
        conn = sqlite3.connect(_SHOP_BOT_DB)
        conn.execute(
            "INSERT INTO push_delivery_log (user_id, title) VALUES (?, ?)",
            (tg_id, title or ""),
        )
        conn.execute(
            """DELETE FROM push_delivery_log
               WHERE user_id = ?
                 AND id NOT IN (
                     SELECT id FROM push_delivery_log
                     WHERE user_id = ?
                     ORDER BY sent_at DESC
                     LIMIT 20
                 )""",
            (tg_id, tg_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("push_utils._log_delivery: %s", e)

def _normalize_vapid_key(raw: str) -> str:
    """Normalize VAPID private key to PKCS8 PEM that works with current cryptography.

    Handles common pasting issues:
      1. Collapsed PEM lines (no line-breaks in base64 body) → re-wrap at 64 chars
      2. Explicit EC parameters (deprecated in newer cryptography) → re-export as named-curve PKCS8
      3. Raw url-safe base64 EC private key scalar (py_vapid 1.x format) → wrap in PKCS8 PEM
    """
    if not raw:
        return raw

    # --- step 0: basic cleanup ---
    key = raw.replace("\\n", "\n").strip().strip('"').strip("'").strip()

    if not key:
        return key

    # --- step 1: re-wrap collapsed PEM base64 body ---
    # Use regex so it works even when header/body/footer are all on one line
    # (Amvera and some env UIs strip newlines from multiline values).
    if "BEGIN" in key and "KEY" in key:
        import re as _re
        _m = _re.match(r"(-----BEGIN[^-]+-{5})([\s\S]*?)(-----END[^-]+-{5})", key)
        if _m:
            _hdr = _m.group(1).strip()
            _body = _re.sub(r"[\s]", "", _m.group(2))
            _ftr = _m.group(3).strip()
            if _body:
                _wrapped = "\n".join(_body[i:i+64] for i in range(0, len(_body), 64))
                key = f"{_hdr}\n{_wrapped}\n{_ftr}"

    # --- step 2: try to load with cryptography and re-export as PKCS8 PEM ---
    # This fixes explicit EC parameters and other deprecated formats.
    if "BEGIN" in key and "KEY" in key:
        try:
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key, Encoding, PrivateFormat, NoEncryption,
            )
            loaded = load_pem_private_key(key.encode(), password=None)
            key = loaded.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            ).decode().strip()
            logger.debug("push_utils: VAPID key normalized to PKCS8 PEM via re-export")
            return key
        except Exception as _e:
            logger.debug("push_utils: PKCS8 re-export failed (%s), trying openssl", _e)

        # --- step 2b: openssl subprocess conversion ---
        # cryptography ≥ 42 refuses to load "explicit parameters" EC keys
        # (common in old VAPID tools). openssl handles any EC format → PKCS8 named-curve.
        try:
            import subprocess, tempfile, os as _os
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as _f:
                _f.write(key)
                _tmp = _f.name
            try:
                _res = subprocess.run(
                    ["openssl", "pkcs8", "-topk8", "-nocrypt",
                     "-in", _tmp, "-outform", "PEM"],
                    capture_output=True, text=True, timeout=10,
                )
            finally:
                _os.unlink(_tmp)
            if _res.returncode == 0 and "BEGIN" in _res.stdout:
                key = _res.stdout.strip()
                logger.info("push_utils: VAPID key converted via openssl pkcs8 (explicit-params fix)")
                return key
            else:
                logger.debug("push_utils: openssl pkcs8 failed: %s", _res.stderr[:200])
        except Exception as _e2:
            logger.debug("push_utils: openssl conversion error: %s", _e2)

    # --- step 3: single-line DER base64 (PKCS8 DER encoded as url-safe base64) ---
    # This is our preferred env-safe format: 184 chars, no newlines.
    # Also handles raw EC private key scalar (py_vapid 1.x, ~43 chars).
    compact = key.replace("\n", "").replace("=", "").strip()
    if "BEGIN" not in compact and " " not in compact and len(compact) >= 40:
        try:
            import base64 as _b64
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PrivateFormat, NoEncryption, load_der_private_key,
            )
            der_bytes = _b64.urlsafe_b64decode(compact + "==")
            # DER PKCS8 key for P-256 is typically 138-150 bytes
            if len(der_bytes) >= 64:
                priv = load_der_private_key(der_bytes, password=None)
                key = priv.private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
                ).decode().strip()
                logger.debug("push_utils: VAPID key converted from DER base64 to PKCS8 PEM")
                return key
        except Exception as _e:
            logger.debug("push_utils: DER base64 conversion failed: %s", _e)

        # sub-step: raw EC private key scalar (py_vapid 1.x) — 32 bytes
        try:
            import base64 as _b64
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PrivateFormat, NoEncryption,
            )
            d_bytes = _b64.urlsafe_b64decode(compact + "==")
            if len(d_bytes) == 32:
                priv = ec.derive_private_key(int.from_bytes(d_bytes, "big"), ec.SECP256R1())
                key = priv.private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
                ).decode().strip()
                logger.debug("push_utils: VAPID key converted from raw scalar to PKCS8 PEM")
                return key
        except Exception as _e:
            logger.debug("push_utils: raw scalar conversion failed: %s", _e)

    return key


_raw_vapid_key = os.environ.get("VAPID_PRIVATE_KEY", "")
# Normalize the key at import time — handles collapsed PEM, explicit EC params,
# and the old py_vapid 1.x raw-scalar format used by some key generators.
_VAPID_PRIVATE = _normalize_vapid_key(_raw_vapid_key)

_VAPID_MAILTO = (os.environ.get("VAPID_MAILTO", "") or "").strip()
if not _VAPID_MAILTO:
    logger.warning(
        "push_utils: VAPID_MAILTO not set — using fallback mailto:admin@dailysales.app; "
        "set VAPID_MAILTO env to a real contact for reliable delivery (Apple/Mozilla may throttle)."
    )
    _VAPID_MAILTO = "mailto:admin@dailysales.app"
elif not _VAPID_MAILTO.startswith("mailto:"):
    _VAPID_MAILTO = "mailto:" + _VAPID_MAILTO


def _vapid_claims() -> dict:
    """Fresh claims dict per send — pywebpush mutates it (adds aud/exp)."""
    return {"sub": _VAPID_MAILTO}


def is_configured() -> bool:
    """Public helper — returns True when VAPID private key is present and valid."""
    return _is_configured()


def _is_configured() -> bool:
    # Accept both PEM (PKCS8/SEC1 "-----BEGIN ... KEY-----") and raw url-safe
    # base64 application-server keys. Be lenient about exact prefix/whitespace.
    if not _VAPID_PRIVATE:
        return False
    if "BEGIN" in _VAPID_PRIVATE and "KEY" in _VAPID_PRIVATE:
        return True
    # raw base64 VAPID private key (no PEM wrapper) — typically ~43 chars
    compact = _VAPID_PRIVATE.replace("\n", "").replace("=", "")
    return len(compact) >= 20 and " " not in compact


def generate_vapid_keypair() -> dict:
    """Generate a fresh VAPID (EC P-256) keypair for Web Push.

    Returns:
      - private_pem:    PKCS8 PEM string (multiline) — fallback format.
      - private_single: PKCS8 DER encoded as url-safe base64 (single line, 184 chars)
                        → preferred format for VAPID_PRIVATE_KEY env var (no multiline issues).
      - public_b64:     raw uncompressed point, url-safe base64 без padding →
                        env var VAPID_PUBLIC_KEY (он же applicationServerKey в браузере).

    Nothing is persisted — the private key is returned once for the admin to copy.
    """
    import base64 as _b64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8").strip()
    # Single-line DER base64: safer for env vars (no multiline/newline issues)
    private_der = priv.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_single = _b64.urlsafe_b64encode(private_der).rstrip(b"=").decode("utf-8")
    raw_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = _b64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("utf-8")
    return {"private_pem": private_pem, "private_single": private_single, "public_b64": public_b64}


def _get_subscriptions(tg_id: int) -> list[dict]:
    """Return all push_subscriptions for a telegram_id from shop_bot.db."""
    try:
        import sqlite3
        conn = sqlite3.connect(_SHOP_BOT_DB)
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (tg_id,),
        ).fetchall()
        conn.close()
        return [{"endpoint": r[0], "keys": {"p256dh": r[1], "auth": r[2]}} for r in rows]
    except Exception as e:
        logger.warning("push_utils._get_subscriptions: %s", e)
        return []


def _delete_subscription(tg_id: int, endpoint: str):
    """Remove a stale subscription (410 Gone from push server)."""
    try:
        import sqlite3
        conn = sqlite3.connect(_SHOP_BOT_DB)
        conn.execute(
            "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
            (tg_id, endpoint),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("push_utils._delete_subscription: %s", e)


def _build_payload(title: str, body: str, url: str, badge: int) -> bytes:
    # Map URL prefix → notification tag so different types don't overwrite each other
    _TAG_MAP = {
        '/sales':     'ds-sale',
        '/reports':   'ds-report',
        '/inventory': 'ds-stock',
        '/tasks':     'ds-task',
        '/chat':      'ds-chat',
        '/absences':  'ds-absence',
        '/settings':  'ds-sub',
        '/pos':       'ds-sale',
    }
    tag = next((v for k, v in _TAG_MAP.items() if url.startswith(k)), 'ds-default')
    return json.dumps({
        "title": title,
        "body": body if body and body.strip() else title,  # fallback: never empty body
        "url": url,
        "badge": badge,
        "tag": tag,
    }).encode()


def _push_to_user(tg_id: int, payload: bytes, webpush, WebPushException) -> int:
    """Send a prepared payload to all devices of one user. Returns devices reached."""
    sent = 0
    for sub in _get_subscriptions(tg_id):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=_VAPID_PRIVATE,
                vapid_claims=_vapid_claims(),
            )
            sent += 1
        except Exception as exc:
            # 404/410 Gone → subscription expired, remove it
            gone = False
            try:
                if isinstance(exc, WebPushException) and exc.response is not None:
                    gone = exc.response.status_code in (404, 410)
            except Exception:
                pass
            if gone:
                _delete_subscription(tg_id, sub["endpoint"])
            else:
                logger.warning("push_utils push tg_id=%s: %s", tg_id, exc)
    return sent


def send_web_push(
    tg_id: int, title: str, body: str, url: str = "/dashboard", badge: int = 1
) -> dict:
    """Send a Web Push notification to all browser subscriptions of tg_id.

    Returns a dict: {"sent": int, "failed": int, "gone": int, "subs_found": int,
                     "vapid": bool, "code": str}
    Code values: "ok" | "vapid_missing" | "no_subscriptions" | "import_error"
    """
    if not _is_configured():
        return {"sent": 0, "failed": 0, "gone": 0, "subs_found": 0,
                "vapid": False, "code": "vapid_missing"}
    try:
        tg_id = int(tg_id)
    except (ValueError, TypeError):
        return {"sent": 0, "failed": 0, "gone": 0, "subs_found": 0,
                "vapid": True, "code": "no_subscriptions"}
    subs = _get_subscriptions(tg_id)
    if not subs:
        return {"sent": 0, "failed": 0, "gone": 0, "subs_found": 0,
                "vapid": True, "code": "no_subscriptions"}
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("push_utils: pywebpush not installed")
        return {"sent": 0, "failed": 0, "gone": 0, "subs_found": len(subs),
                "vapid": True, "code": "import_error"}
    sent = failed = gone = 0
    errors: list = []
    payload = _build_payload(title, body, url, badge)
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=_VAPID_PRIVATE,
                vapid_claims=_vapid_claims(),
            )
            sent += 1
        except Exception as exc:
            _gone = False
            err_detail = f"{type(exc).__name__}: {exc}"
            try:
                if isinstance(exc, WebPushException) and exc.response is not None:
                    status = exc.response.status_code
                    _gone = status in (404, 410)
                    err_detail = f"HTTP {status}: {exc.response.text[:300]}"
            except Exception:
                pass
            if _gone:
                _delete_subscription(tg_id, sub["endpoint"])
                gone += 1
            else:
                logger.warning("push_utils push tg_id=%s: %s", tg_id, err_detail)
                failed += 1
                if err_detail not in errors:
                    errors.append(err_detail)
    if sent > 0:
        _log_delivery(tg_id, title)
    return {"sent": sent, "failed": failed, "gone": gone, "subs_found": len(subs),
            "vapid": True, "code": "ok" if sent > 0 else "send_failed",
            "errors": errors}


def send_web_push_bulk(tg_ids, title: str, body: str, url: str = "/dashboard", badge: int = 1) -> int:
    """Send the same Web Push to many users efficiently. Returns devices reached.

    Dedups telegram_ids, loads VAPID config + pywebpush once, and isolates every
    per-user/per-device failure so one bad subscription can't abort the batch.
    """
    if not _is_configured():
        return 0
    seen: set = set()
    ids: list = []
    for t in tg_ids:
        try:
            ti = int(t)
        except (ValueError, TypeError):
            continue
        if ti in seen:
            continue
        seen.add(ti)
        ids.append(ti)
    if not ids:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("push_utils: pywebpush not installed")
        return 0
    payload = _build_payload(title, body, url, badge)
    sent = 0
    for ti in ids:
        sent += _push_to_user(ti, payload, webpush, WebPushException)
    return sent


async def apush(tg_id: int, title: str, body: str, url: str = "/dashboard", badge: int = 1):
    """Async wrapper: send a single push off the event loop."""
    await asyncio.to_thread(send_web_push, tg_id, title, body, url, badge)


async def apush_bulk(tg_ids, title: str, body: str, url: str = "/dashboard", badge: int = 1) -> int:
    """Async wrapper: send a bulk push off the event loop."""
    return await asyncio.to_thread(send_web_push_bulk, list(tg_ids), title, body, url, badge)
