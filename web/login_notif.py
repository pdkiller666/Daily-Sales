"""
Уведомление о входе с нового IP-адреса.

При каждом успешном логине проверяет, видели ли мы этот IP для данного пользователя.
Если нет — записывает его и отправляет уведомление в Telegram (fire-and-forget).

Применяется только к реальным tg_id (> 0). Email-only пользователи (tg_id < 0)
и системные адреса (localhost, неизвестный) пропускаются.
"""
import logging
import sqlite3
from datetime import datetime

_logger = logging.getLogger(__name__)
_SHOP_BOT_DB = "data/shop_bot.db"

_PRIVATE_PREFIXES = (
    "127.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
    "192.168.", "::1", "localhost",
)


def _is_private(ip: str) -> bool:
    return not ip or ip == "unknown" or any(ip.startswith(p) for p in _PRIVATE_PREFIXES)


def _real_ip(request) -> str:
    """Extract real client IP, respecting X-Forwarded-For if present."""
    try:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


def check_and_record_ip(telegram_id: int, ip: str) -> bool:
    """Return True if this IP is new for the user (and record it).
    Uses INSERT OR IGNORE — race-safe via UNIQUE constraint.
    Returns False for private/loopback addresses and email-only accounts."""
    if telegram_id <= 0 or _is_private(ip):
        return False
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB)
        cur = conn.execute(
            "INSERT OR IGNORE INTO login_ips (telegram_id, ip, first_seen) VALUES (?, ?, ?)",
            (telegram_id, ip, datetime.now().isoformat(timespec='seconds'))
        )
        conn.commit()
        is_new = cur.rowcount > 0
        conn.close()
        return is_new
    except Exception as exc:
        _logger.debug("login_notif.check_and_record_ip: %s", exc)
        return False


async def notify_new_ip(bot, telegram_id: int, ip: str, first_name: str) -> None:
    """Send a Telegram notification about a new login IP. Never raises."""
    try:
        name_esc = first_name.replace("<", "&lt;").replace(">", "&gt;") if first_name else "пользователь"
        text = (
            f"🔐 <b>Вход с нового устройства</b>\n\n"
            f"Привет, {name_esc}! Зафиксирован вход в веб-кабинет DailySales "
            f"с нового IP-адреса: <code>{ip}</code>\n\n"
            f"Если это были вы — всё в порядке.\n"
            f"Если нет — немедленно смените пароль в разделе «⚙️ Настройки»."
        )
        await bot.send_message(telegram_id, text, parse_mode="HTML")
    except Exception as exc:
        _logger.debug("login_notif.notify_new_ip tg_id=%s: %s", telegram_id, exc)
