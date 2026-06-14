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

_raw_vapid_key = os.environ.get("VAPID_PRIVATE_KEY", "")
# Strip surrounding whitespace/quotes that often sneak in when pasting a
# multi-line PEM into a hosting env-var UI (e.g. Amvera) — a leading newline
# would otherwise break the "-----BEGIN" check and silently disable Web Push.
_VAPID_PRIVATE = _raw_vapid_key.replace("\\n", "\n").strip().strip('"').strip("'").strip()

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
      - private_pem: PKCS8 PEM string → paste into env var VAPID_PRIVATE_KEY.
      - public_b64:  raw uncompressed point, url-safe base64 без padding →
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
    raw_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = _b64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("utf-8")
    return {"private_pem": private_pem, "public_b64": public_b64}


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


def send_web_push(tg_id: int, title: str, body: str, url: str = "/dashboard", badge: int = 1):
    """Send a Web Push notification to all browser subscriptions of tg_id."""
    if not _is_configured():
        return
    try:
        tg_id = int(tg_id)
    except (ValueError, TypeError):
        return
    if not _get_subscriptions(tg_id):
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("push_utils: pywebpush not installed")
        return
    _push_to_user(tg_id, _build_payload(title, body, url, badge), webpush, WebPushException)


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
