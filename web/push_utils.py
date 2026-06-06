"""Web Push (VAPID) utility for DailySales.

send_web_push(tg_id, title, body, url) — sends to all subscriptions of the user.
Silently ignores missing VAPID config or send failures (log only).
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_SHOP_BOT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "shop_bot.db")

_VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
_VAPID_CLAIMS  = {"sub": os.environ.get("VAPID_MAILTO", "mailto:admin@dailysales.app")}


def _is_configured() -> bool:
    return bool(_VAPID_PRIVATE and _VAPID_PRIVATE.startswith("-----BEGIN"))


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


def send_web_push(tg_id: int, title: str, body: str, url: str = "/dashboard", badge: int = 1):
    """Send a Web Push notification to all browser subscriptions of tg_id.

    Non-blocking: catches all exceptions internally.
    badge — numeric hint for App Badge in SW (optional).
    """
    if not _is_configured():
        return

    subs = _get_subscriptions(tg_id)
    if not subs:
        return

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
        "badge": badge,
        "tag": "dailysales-push",
    }).encode()

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("push_utils: pywebpush not installed")
        return

    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=_VAPID_PRIVATE,
                vapid_claims=_VAPID_CLAIMS,
            )
        except Exception as exc:
            # 410 Gone → subscription expired, remove it
            gone = False
            try:
                from pywebpush import WebPushException
                if isinstance(exc, WebPushException) and exc.response is not None:
                    gone = exc.response.status_code in (404, 410)
            except Exception:
                pass
            if gone:
                _delete_subscription(tg_id, sub["endpoint"])
            else:
                logger.warning("push_utils.send_web_push tg_id=%s: %s", tg_id, exc)
