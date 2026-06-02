import json
import logging
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)
router = APIRouter()

_rate_store: dict[int, list[float]] = {}
_RATE_LIMIT = 3
_RATE_WINDOW = 3600

CATEGORIES = {
    "question": "❓ Вопрос",
    "problem":  "🐛 Проблема",
    "idea":     "💡 Предложение",
    "other":    "📝 Другое",
}


def _rate_ok(telegram_id: int) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    times = [t for t in _rate_store.get(telegram_id, []) if now - t < _RATE_WINDOW]
    if len(times) >= _RATE_LIMIT:
        return False
    times.append(now)
    _rate_store[telegram_id] = times
    return True


def _send_tg(bot_token: str, chat_id: int, text: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error("support _send_tg error: %s", e)
        return False


def _get_user_info(telegram_id: int, org_db: str | None) -> dict:
    info = {"name": "", "username": "", "shop": "", "org": ""}
    try:
        import sqlite3
        if org_db:
            conn = sqlite3.connect(org_db)
            row = conn.execute(
                "SELECT first_name, last_name, username, shop_name FROM users WHERE telegram_id=?",
                (telegram_id,)
            ).fetchone()
            conn.close()
            if row:
                fn, ln, uname, shop = row
                info["name"] = f"{fn or ''} {ln or ''}".strip()
                info["username"] = uname or ""
                info["shop"] = shop or ""
        row2 = None
        conn2 = sqlite3.connect("data/main.db")
        row2 = conn2.execute(
            "SELECT org_name FROM organizations o "
            "JOIN user_org_mapping m ON m.org_id=o.id "
            "WHERE m.telegram_id=? AND m.is_active=1",
            (telegram_id,)
        ).fetchone()
        conn2.close()
        if row2:
            info["org"] = row2[0] or ""
    except Exception as e:
        logger.error("support _get_user_info error: %s", e)
    return info


@router.get("/support")
def support_page(request: Request, sent: str = "", error: str = ""):
    from web.auth import get_session_user, get_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    ctx = {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "sent": sent == "1",
        "error": error,
        "categories": CATEGORIES,
    }
    return request.app.state.templates.TemplateResponse(request, "support/index.html", ctx)


@router.post("/support/send")
def support_send(
    request: Request,
    csrf_token: str = Form(""),
    category: str = Form(""),
    subject: str = Form(""),
    message: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/support?error=csrf", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    subject = subject.strip()[:200]
    message = message.strip()[:2000]
    category = category if category in CATEGORIES else "other"

    if len(message) < 10:
        return RedirectResponse(url="/support?error=short", status_code=303)

    if not _rate_ok(telegram_id):
        return RedirectResponse(url="/support?error=rate", status_code=303)

    try:
        import env_manager as _env
        bot_token = _env.get_bot_token()
        admin_id = _env.get_main_admin_id()
    except Exception:
        bot_token = None
        admin_id = None

    if not bot_token or not admin_id:
        return RedirectResponse(url="/support?error=cfg", status_code=303)

    info = _get_user_info(telegram_id, org_db)
    cat_label = CATEGORIES.get(category, "Другое")
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    name_part = info["name"] or user.get("name", "—")
    uname_part = f" (@{info['username']})" if info["username"] else ""
    org_part = info["org"] or "—"
    shop_part = f" · {info['shop']}" if info["shop"] else ""

    text = (
        f"📨 <b>Обратная связь из веб-кабинета</b>\n\n"
        f"👤 {name_part}{uname_part}\n"
        f"🏢 {org_part}{shop_part}\n"
        f"📋 Категория: {cat_label}\n"
    )
    if subject:
        text += f"📌 Тема: {subject}\n"
    text += (
        f"\n💬 <b>Сообщение:</b>\n{message}\n\n"
        f"⏰ {now_str}"
    )

    ok = _send_tg(bot_token, admin_id, text)
    if not ok:
        return RedirectResponse(url="/support?error=send", status_code=303)

    return RedirectResponse(url="/support?sent=1", status_code=303)
