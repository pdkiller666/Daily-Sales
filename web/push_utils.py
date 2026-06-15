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

def _key_to_single_line_der(priv) -> str:
    """Export a cryptography EC private key as single-line url-safe base64 PKCS8 DER."""
    import base64 as _b64
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption,
    )
    der = priv.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    return _b64.urlsafe_b64encode(der).decode().rstrip("=")


def _normalize_vapid_key(raw: str) -> str:
    """Normalize VAPID private key to SINGLE-LINE url-safe base64 PKCS8 DER.

    CRITICAL: pywebpush hands the key string to py_vapid ``Vapid.from_string()``,
    which simply strips newlines and url-safe-base64-decodes the WHOLE string —
    it does NOT strip the PEM ``-----BEGIN/END-----`` header/footer. Feeding it a
    PEM therefore fails with
        ValueError: Could not deserialize key data ... ASN.1 parsing error: invalid length
    The only format that works is a single-line url-safe base64 DER (~184 chars,
    no newlines, no PEM armor). So we convert EVERY accepted input into a
    cryptography key object and re-export it in that exact format.

    Accepted inputs:
      * PKCS8 / SEC1 PEM (incl. lines collapsed by Amvera env UI)
      * EC keys with explicit parameters (via openssl fallback)
      * standard OR url-safe base64 DER (PKCS8)
      * raw 32-byte EC private scalar (py_vapid 1.x format)
    """
    if not raw:
        return raw

    # --- step 0: basic cleanup ---
    key = raw.replace("\\n", "\n").strip().strip('"').strip("'").strip()
    if not key:
        return key

    import base64 as _b64
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key, load_der_private_key,
    )

    priv = None

    # --- step 1: PEM input → load (re-wrapping collapsed bodies first) ---
    if "BEGIN" in key and "KEY" in key:
        import re as _re
        pem_text = key
        _m = _re.match(r"(-----BEGIN[^-]+-{5})([\s\S]*?)(-----END[^-]+-{5})", key)
        if _m:
            _hdr = _m.group(1).strip()
            _body = _re.sub(r"\s", "", _m.group(2))
            _ftr = _m.group(3).strip()
            if _body:
                _wrapped = "\n".join(_body[i:i+64] for i in range(0, len(_body), 64))
                pem_text = f"{_hdr}\n{_wrapped}\n{_ftr}"
        try:
            priv = load_pem_private_key(pem_text.encode(), password=None)
        except Exception as _e:
            logger.debug("push_utils: load_pem failed (%s), trying openssl", _e)
            # openssl fallback: cryptography ≥ 42 refuses "explicit parameters"
            # EC keys; openssl re-encodes to named-curve PKCS8 DER.
            try:
                import subprocess, tempfile, os as _os
                with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as _f:
                    _f.write(pem_text)
                    _tmp = _f.name
                try:
                    _res = subprocess.run(
                        ["openssl", "pkcs8", "-topk8", "-nocrypt",
                         "-in", _tmp, "-outform", "DER"],
                        capture_output=True, timeout=10,
                    )
                finally:
                    _os.unlink(_tmp)
                if _res.returncode == 0 and _res.stdout:
                    priv = load_der_private_key(_res.stdout, password=None)
                    logger.info("push_utils: VAPID key converted via openssl (explicit-params fix)")
                else:
                    logger.debug("push_utils: openssl pkcs8 failed: %s", _res.stderr[:200])
            except Exception as _e2:
                logger.debug("push_utils: openssl conversion error: %s", _e2)

    # --- step 2: base64 DER / raw scalar input ---
    if priv is None:
        compact = key.replace("\n", "").replace("=", "").strip()
        if "BEGIN" not in compact and " " not in compact and len(compact) >= 40:
            _pad = "=" * (-len(compact) % 4)
            der_bytes = None
            for _dec in (_b64.urlsafe_b64decode, _b64.b64decode):
                try:
                    der_bytes = _dec(compact + _pad)
                    break
                except Exception:
                    der_bytes = None
            if der_bytes:
                if len(der_bytes) >= 64:
                    # PKCS8 DER (P-256 key ~138 bytes)
                    try:
                        priv = load_der_private_key(der_bytes, password=None)
                    except Exception as _e:
                        logger.debug("push_utils: DER load failed: %s", _e)
                elif len(der_bytes) == 32:
                    # raw EC private scalar (py_vapid 1.x)
                    try:
                        from cryptography.hazmat.primitives.asymmetric import ec
                        priv = ec.derive_private_key(int.from_bytes(der_bytes, "big"), ec.SECP256R1())
                    except Exception as _e:
                        logger.debug("push_utils: raw scalar load failed: %s", _e)

    # --- step 3: export in the format py_vapid.from_string() accepts ---
    if priv is not None:
        try:
            single = _key_to_single_line_der(priv)
            logger.info("push_utils: VAPID key normalized to single-line url-safe DER (%d chars)", len(single))
            return single
        except Exception as _e:
            logger.error("push_utils: failed to export VAPID key to DER: %s", _e)

    logger.error(
        "push_utils: VAPID_PRIVATE_KEY could not be parsed into a usable key "
        "(len=%d) — push will fail until a valid key is set", len(key)
    )
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
