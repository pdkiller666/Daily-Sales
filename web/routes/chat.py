import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse, Response, FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_MIME_PREFIXES = ("image/", "application/pdf", "application/msword",
                         "application/vnd.", "text/plain", "text/csv")
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ
PLAN_ORDER = ["Бесплатный", "Базовый", "Стандарт", "Премиум"]

_SHOP_BOT_DB = "data/shop_bot.db"

# Rate limiting: /chat/send — 30 msg/min per telegram_id
_SEND_RATE_STORE: dict[int, list[float]] = {}
_SEND_RATE_LIMIT = 30
_SEND_RATE_WINDOW = 60.0

# Rate limiting: /chat/poll — 60 req/min per IP
_POLL_RATE_STORE: dict[str, list[float]] = {}
_POLL_RATE_LIMIT = 60
_POLL_RATE_WINDOW = 60.0


def _send_rate_ok(telegram_id: int) -> bool:
    import time
    now = time.monotonic()
    times = [t for t in _SEND_RATE_STORE.get(telegram_id, []) if now - t < _SEND_RATE_WINDOW]
    if len(times) >= _SEND_RATE_LIMIT:
        _SEND_RATE_STORE[telegram_id] = times
        return False
    times.append(now)
    _SEND_RATE_STORE[telegram_id] = times
    return True


def _poll_rate_ok(ip: str) -> bool:
    import time
    now = time.monotonic()
    times = [t for t in _POLL_RATE_STORE.get(ip, []) if now - t < _POLL_RATE_WINDOW]
    if len(times) >= _POLL_RATE_LIMIT:
        _POLL_RATE_STORE[ip] = times
        return False
    times.append(now)
    _POLL_RATE_STORE[ip] = times
    return True


def _get_chat_min_plan() -> str:
    """Читает минимальный тариф для чата из shop_bot.db. Дефолт — Базовый."""
    try:
        import sqlite3
        conn = sqlite3.connect(_SHOP_BOT_DB)
        row = conn.execute(
            "SELECT value FROM payment_settings WHERE key='chat_min_plan'"
        ).fetchone()
        conn.close()
        if row and row[0] in PLAN_ORDER:
            return row[0]
        return "Базовый"
    except Exception:
        return "Базовый"


def _plan_allowed(org_plan: str, min_plan: str) -> bool:
    """Проверяет, достаточно ли тариф org_plan для доступа к чату."""
    if min_plan == "Отключён":
        return False
    try:
        return PLAN_ORDER.index(org_plan) >= PLAN_ORDER.index(min_plan)
    except ValueError:
        return False


def _get_user_db_id(db, telegram_id: int) -> int | None:
    try:
        conn = db.get_connection()
        row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_org_active_plan(telegram_id: int) -> str:
    """Возвращает название активного тарифа из shop_bot.db (с учётом триала)."""
    try:
        import sqlite3
        conn = sqlite3.connect(_SHOP_BOT_DB)
        row = conn.execute(
            "SELECT plan_name FROM subscriptions WHERE telegram_id = ? AND end_date >= date('now') ORDER BY end_date DESC LIMIT 1",
            (telegram_id,)
        ).fetchone()
        if row:
            conn.close()
            return row[0]
        row2 = conn.execute(
            "SELECT trial_plan, trial_end FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()
        conn.close()
        if row2 and row2[1] and row2[1] >= datetime.now().strftime("%Y-%m-%d"):
            return row2[0] or "Премиум"
        return "Бесплатный"
    except Exception:
        return "Бесплатный"


def _uploads_dir(org_db: str) -> str:
    """data/tenants/org_xxx_uploads/chat/ из пути к org_db."""
    base = os.path.splitext(org_db)[0]
    d = base + "_uploads/chat"
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename(original: str) -> str:
    name = os.path.basename(original)
    name = re.sub(r'[^\w.\-]', '_', name)
    return name[:120] or "file"


def _fmt_msg(row) -> dict:
    mid, user_id, message, file_path, file_name, file_type, file_size, created_at, fn, ln, uname = row
    display = f"{fn or ''} {ln or ''}".strip() or uname or f"User#{user_id}"
    initial = (display[0] if display else "?").upper()
    has_file = bool(file_path)
    is_image = file_type.startswith("image/") if file_type else False
    ts = str(created_at or "")[:16].replace("T", " ")
    return {
        "id": mid,
        "user_id": user_id,
        "message": message or "",
        "file_name": file_name or "",
        "file_type": file_type or "",
        "file_size": file_size or 0,
        "has_file": has_file,
        "is_image": is_image,
        "file_url": f"/chat/file/{mid}" if has_file else "",
        "display_name": display,
        "initial": initial,
        "created_at": ts,
    }


@router.get("/chat")
def chat_page(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    min_plan = _get_chat_min_plan()

    ctx = {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "messages": [],
        "latest_id": 0,
        "chat_allowed": False,
        "min_plan": min_plan,
        "error": None,
    }

    if min_plan == "Отключён":
        ctx["error"] = "disabled"
        return request.app.state.templates.TemplateResponse(request, "chat/index.html", ctx)

    try:
        db = get_web_db(telegram_id, org_db)
        org_plan = _get_org_active_plan(telegram_id)
        allowed = _plan_allowed(org_plan, min_plan)
        ctx["chat_allowed"] = allowed
        ctx["org_plan"] = org_plan

        if allowed:
            user_db_id = _get_user_db_id(db, telegram_id)
            ctx["my_db_id"] = user_db_id or 0
            rows = db.get_chat_messages(limit=50)
            ctx["messages"] = [_fmt_msg(r) for r in rows]
            ctx["latest_id"] = db.get_chat_latest_id()
    except Exception as exc:
        logger.error(f"chat_page error: {exc}")
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(request, "chat/index.html", ctx)


@router.post("/chat/send")
async def chat_send(
    request: Request,
    csrf_token: str = Form(default=""),
    message: str = Form(default=""),
    file: UploadFile = File(default=None),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    if not _send_rate_ok(telegram_id):
        return JSONResponse({"ok": False, "error": "Слишком много сообщений, подождите немного"}, status_code=429)

    min_plan = _get_chat_min_plan()
    if min_plan == "Отключён":
        return JSONResponse({"ok": False, "error": "Чат отключён"}, status_code=403)

    try:
        db = get_web_db(telegram_id, org_db)
        org_plan = _get_org_active_plan(telegram_id)
        if not _plan_allowed(org_plan, min_plan):
            return JSONResponse({"ok": False, "error": "Недостаточный тариф"}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)

        text = message.strip()[:2000]

        file_path = ""
        file_name = ""
        file_type = ""
        file_size = 0

        if file and file.filename:
            raw_data = await file.read()
            fsize = len(raw_data)
            if fsize > MAX_FILE_SIZE:
                return JSONResponse({"ok": False, "error": "Файл слишком большой (макс. 20 МБ)"}, status_code=400)

            mime = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
            allowed_mime = any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES)
            if not allowed_mime:
                return JSONResponse({"ok": False, "error": "Тип файла не разрешён"}, status_code=400)

            safe_name = _safe_filename(file.filename)
            uid = uuid.uuid4().hex[:12]
            month_dir = datetime.now().strftime("%Y-%m")
            uploads = _uploads_dir(org_db)
            month_path = os.path.join(uploads, month_dir)
            os.makedirs(month_path, exist_ok=True)
            dest = os.path.join(month_path, f"{uid}_{safe_name}")

            with open(dest, "wb") as f_out:
                f_out.write(raw_data)

            file_path = dest
            file_name = file.filename[:255]
            file_type = mime
            file_size = fsize

        if not text and not file_path:
            return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)

        new_id = db.add_chat_message(
            user_id=user_db_id,
            message=text,
            file_path=file_path,
            file_name=file_name,
            file_type=file_type,
            file_size=file_size,
        )

        new_msgs = db.get_chat_messages_since(new_id - 1)
        result = [_fmt_msg(r) for r in new_msgs]
        return JSONResponse({"ok": True, "messages": result, "latest_id": new_id})

    except Exception as exc:
        logger.error(f"chat_send error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


@router.get("/chat/poll")
def chat_poll(request: Request, since_id: int = 0):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "messages": [], "latest_id": since_id})

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    ip = request.client.host if request.client else "unknown"

    if not _poll_rate_ok(ip):
        return JSONResponse({"ok": True, "messages": [], "latest_id": since_id})

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": True, "messages": [], "latest_id": since_id})

        db = get_web_db(telegram_id, org_db)
        org_plan = _get_org_active_plan(telegram_id)
        if not _plan_allowed(org_plan, min_plan):
            return JSONResponse({"ok": True, "messages": [], "latest_id": since_id})

        rows = db.get_chat_messages_since(since_id)
        msgs = [_fmt_msg(r) for r in rows]
        latest = msgs[-1]["id"] if msgs else since_id
        return JSONResponse({"ok": True, "messages": msgs, "latest_id": latest})
    except Exception as exc:
        logger.error(f"chat_poll error: {exc}")
        return JSONResponse({"ok": True, "messages": [], "latest_id": since_id})


@router.get("/chat/file/{msg_id}")
def chat_file(request: Request, msg_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        row = conn.execute(
            "SELECT file_path, file_name, file_type FROM chat_messages WHERE id = ? AND is_deleted = 0",
            (msg_id,)
        ).fetchone()
        conn.close()

        if not row or not row[0]:
            return Response(content="Файл не найден", status_code=404)

        fpath, fname, ftype = row
        if not os.path.isfile(fpath):
            return Response(content="Файл не найден на диске", status_code=404)

        exp_uploads = os.path.abspath(_uploads_dir(org_db))
        real_fpath = os.path.abspath(fpath)
        if not real_fpath.startswith(exp_uploads):
            return Response(content="Доступ запрещён", status_code=403)

        return FileResponse(
            fpath,
            media_type=ftype or "application/octet-stream",
            filename=fname or os.path.basename(fpath),
        )
    except Exception as exc:
        logger.error(f"chat_file error: {exc}")
        return Response(content="Ошибка", status_code=500)


@router.post("/chat/message/{msg_id}/delete")
def chat_delete_message(
    request: Request,
    msg_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id)
        ok = db.soft_delete_chat_message(msg_id, user_db_id or 0, is_admin)
        return JSONResponse({"ok": ok})
    except Exception as exc:
        logger.error(f"chat_delete error: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
