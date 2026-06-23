"""Super-admin audit logging helper.

Records sensitive super-admin actions to admin_audit_log (shop_bot.db).
Best-effort: never raises into the calling route.
"""
import logging

logger = logging.getLogger("web.audit")

SHOP_BOT_DB = 'data/shop_bot.db'


def _real_ip(request) -> str:
    try:
        xff = request.headers.get('x-forwarded-for', '')
        if xff:
            return xff.split(',')[0].strip()
        return request.client.host if request.client else ''
    except Exception:
        return ''


def log_admin_action(request, user, action: str,
                     target: str = "", details: str = "") -> None:
    """Write an audit entry for a super-admin action. Safe to call anywhere."""
    try:
        actor_tg = None
        actor_name = ""
        if isinstance(user, dict):
            try:
                actor_tg = int(user.get('sub')) if user.get('sub') is not None else None
            except (TypeError, ValueError):
                actor_tg = None
            actor_name = user.get('name') or ""
        from database import Database
        Database(SHOP_BOT_DB).add_admin_audit(
            actor_tg_id=actor_tg, actor_name=actor_name, action=action,
            target=target, details=details, ip=_real_ip(request)
        )
    except Exception as exc:
        logger.warning("log_admin_action failed: %s", exc)
