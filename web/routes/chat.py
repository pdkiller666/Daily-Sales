import logging
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Form, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse, Response, FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_MIME_PREFIXES = ("image/", "application/pdf", "application/msword",
                         "application/vnd.", "text/plain", "text/csv")
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ
PLAN_ORDER = ["Бесплатный", "Базовый", "Стандарт", "Премиум"]

_SHOP_BOT_DB = "data/shop_bot.db"

# ── Rate limiting ─────────────────────────────────────────────────────────────
_SEND_RATE_STORE:   dict[int, list[float]] = {}   # 30 msg/min per telegram_id
_POLL_RATE_STORE:   dict[str, list[float]] = {}   # 60 req/min per IP
_TOPIC_RATE_STORE:  dict[int, list[float]] = {}   # 5 topics/hour per telegram_id
_SEARCH_RATE_STORE: dict[str, list[float]] = {}   # 30 search req/min per IP


def _rate_ok(store: dict, key, limit: int, window: float) -> bool:
    now = time.monotonic()
    times = [t for t in store.get(key, []) if now - t < window]
    if len(times) >= limit:
        store[key] = times
        return False
    times.append(now)
    store[key] = times
    return True


def _send_rate_ok(tid: int)   -> bool: return _rate_ok(_SEND_RATE_STORE,   tid, 30, 60.0)
def _poll_rate_ok(ip: str)    -> bool: return _rate_ok(_POLL_RATE_STORE,   ip,  60, 60.0)
def _topic_rate_ok(tid: int)  -> bool: return _rate_ok(_TOPIC_RATE_STORE,  tid,  5, 3600.0)
def _search_rate_ok(ip: str)  -> bool: return _rate_ok(_SEARCH_RATE_STORE, ip,  30, 60.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_chat_min_plan() -> str:
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
    """Return active plan name for this user's org.

    Priority:
    1. super_admin → Премиум always
    2. shop_bot.db subscriptions by user_id (org owner's own record)
    3. main.db organizations.subscription_plan (covers non-owner members)
    """
    try:
        from env_manager import env_manager
        if env_manager.is_super_admin(telegram_id):
            return "Премиум"
    except Exception:
        pass
    try:
        import sqlite3
        # Owner path: correct columns are user_id + plan_type
        conn = sqlite3.connect(_SHOP_BOT_DB)
        user_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if user_row:
            sub_row = conn.execute(
                "SELECT plan_type FROM subscriptions WHERE user_id = ? AND datetime(end_date) > datetime('now') ORDER BY end_date DESC LIMIT 1",
                (user_row[0],)
            ).fetchone()
            if sub_row and sub_row[0]:
                conn.close()
                return sub_row[0]
        conn.close()
    except Exception:
        pass
    try:
        import sqlite3
        # Non-owner member path: org-level subscription stored in main.db
        conn = sqlite3.connect("data/main.db")
        org_row = conn.execute(
            """SELECT o.subscription_plan, o.subscription_end
               FROM organizations o
               JOIN user_org_mapping m ON m.org_id = o.id
               WHERE m.telegram_id = ? AND m.is_active = 1
               ORDER BY o.subscription_end DESC LIMIT 1""",
            (telegram_id,)
        ).fetchone()
        conn.close()
        if (org_row and org_row[0] and org_row[0] in PLAN_ORDER
                and org_row[1] and org_row[1] >= datetime.now().strftime("%Y-%m-%d")):
            return org_row[0]
    except Exception:
        pass
    return "Бесплатный"


def _uploads_dir(org_db: str) -> str:
    base = os.path.splitext(org_db)[0]
    d = base + "_uploads/chat"
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename(original: str) -> str:
    name = os.path.basename(original)
    name = re.sub(r'[^\w.\-]', '_', name)
    return name[:120] or "file"


def _fmt_msg(row, my_db_id: int = 0, is_admin: bool = False) -> dict:
    mid, user_id, message, file_path, file_name, file_type, file_size, created_at, fn, ln, uname = row
    display = f"{fn or ''} {ln or ''}".strip() or uname or f"User#{user_id}"
    initial = (display[0] if display else "?").upper()
    has_file = bool(file_path)
    is_image = file_type.startswith("image/") if file_type else False
    raw = str(created_at or "")[:16].replace("T", " ")
    try:
        d, t = raw.split(" ")
        y, mo, day = d.split("-")
        ts = f"{day}.{mo}.{y} {t}"
    except Exception:
        ts = raw
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
        "can_delete": is_admin or (my_db_id > 0 and user_id == my_db_id),
    }


def _fmt_topic(row) -> dict:
    tid, name, created_by, created_at, sort_order, msg_count = row
    return {
        "id": tid,
        "name": name,
        "created_by": created_by,
        "msg_count": msg_count or 0,
    }


def _fmt_search_result(row, my_db_id: int = 0, is_admin: bool = False) -> dict:
    """Как _fmt_msg, но строка содержит 13 колонок (добавлены topic_id, topic_name)."""
    base = _fmt_msg(row[:11], my_db_id=my_db_id, is_admin=is_admin)
    base["result_topic_id"]   = row[11] or 1
    base["result_topic_name"] = row[12] or "Общий"
    return base


def _ensure_access(db, telegram_id: int) -> tuple[bool, str]:
    """Проверяет тариф. Возвращает (allowed, org_plan)."""
    min_plan = _get_chat_min_plan()
    if min_plan == "Отключён":
        return False, ""
    org_plan = _get_org_active_plan(telegram_id)
    return _plan_allowed(org_plan, min_plan), org_plan


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/chat")
def chat_page(request: Request, topic: int = 1):
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
        "topics": [],
        "current_topic_id": topic,
        "current_topic_name": "Общий",
        "latest_id": 0,
        "chat_allowed": False,
        "min_plan": min_plan,
        "my_db_id": 0,
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

            raw_topics = db.get_chat_topics()
            topics = [_fmt_topic(r) for r in raw_topics]
            if not topics:
                topics = [{"id": 1, "name": "Общий", "created_by": None, "msg_count": 0}]
            ctx["topics"] = topics

            # Проверяем что выбранная тема существует
            valid_ids = {t["id"] for t in topics}
            if topic not in valid_ids:
                topic = topics[0]["id"]
                ctx["current_topic_id"] = topic

            ctx["current_topic_name"] = next(
                (t["name"] for t in topics if t["id"] == topic), "Общий"
            )

            is_admin = user.get("role") in ("owner", "admin", "super_admin")
            rows = db.get_chat_messages(limit=50, topic_id=topic)
            ctx["messages"] = [_fmt_msg(r, my_db_id=user_db_id or 0, is_admin=is_admin) for r in rows]
            ctx["latest_id"] = db.get_chat_latest_id(topic_id=topic)

    except Exception as exc:
        logger.error(f"chat_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "chat/index.html", ctx)


@router.post("/chat/send")
async def chat_send(
    request: Request,
    csrf_token: str = Form(default=""),
    message: str = Form(default=""),
    topic_id: int = Form(default=1),
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

        # Проверяем что тема существует
        valid_topics = {r[0] for r in db.get_chat_topics()}
        if not valid_topics:
            valid_topics = {1}
        if topic_id not in valid_topics:
            topic_id = 1

        text = message.strip()[:2000]
        file_path = file_name = file_type = ""
        file_size = 0

        if file and file.filename:
            raw_data = await file.read()
            fsize = len(raw_data)
            if fsize > MAX_FILE_SIZE:
                return JSONResponse({"ok": False, "error": "Файл слишком большой (макс. 20 МБ)"}, status_code=400)

            mime = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
            if not any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES):
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
            topic_id=topic_id,
        )

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        new_msgs = db.get_chat_messages_since(new_id - 1, topic_id=topic_id)
        result = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin) for r in new_msgs]
        return JSONResponse({"ok": True, "messages": result, "latest_id": new_id})

    except Exception as exc:
        logger.error(f"chat_send error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


@router.get("/chat/poll")
def chat_poll(request: Request, since_id: int = 0, topic_id: int = 1):
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

        user_db_id = _get_user_db_id(db, telegram_id) or 0
        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        rows = db.get_chat_messages_since(since_id, topic_id=topic_id)
        msgs = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin) for r in rows]
        latest = msgs[-1]["id"] if msgs else since_id
        return JSONResponse({"ok": True, "messages": msgs, "latest_id": latest})

    except Exception as exc:
        logger.error(f"chat_poll error: {exc}")
        return JSONResponse({"ok": True, "messages": [], "latest_id": since_id})


@router.get("/chat/topics/{topic_id}/messages")
def chat_topic_messages(request: Request, topic_id: int):
    """Последние 50 сообщений темы — для клиентского переключения тем без перезагрузки."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "messages": [], "latest_id": 0}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    ip = request.client.host if request.client else "unknown"

    if not _poll_rate_ok(ip):
        return JSONResponse({"ok": True, "messages": [], "latest_id": 0})

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": False, "messages": [], "latest_id": 0})

        db = get_web_db(telegram_id, org_db)
        if not _plan_allowed(_get_org_active_plan(telegram_id), min_plan):
            return JSONResponse({"ok": False, "messages": [], "latest_id": 0})

        user_db_id = _get_user_db_id(db, telegram_id) or 0
        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        rows = db.get_chat_messages(limit=50, topic_id=topic_id)
        msgs = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin) for r in rows]
        latest = db.get_chat_latest_id(topic_id=topic_id)
        return JSONResponse({"ok": True, "messages": msgs, "latest_id": latest})

    except Exception as exc:
        logger.error(f"chat_topic_messages error: {exc}")
        return JSONResponse({"ok": True, "messages": [], "latest_id": 0})


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

        # Получаем file_path до удаления, чтобы потом удалить файл с диска
        conn = db.get_connection()
        msg_row = conn.execute(
            "SELECT file_path FROM chat_messages WHERE id = ? AND is_deleted = 0",
            (msg_id,)
        ).fetchone()
        conn.close()

        ok = db.soft_delete_chat_message(msg_id, user_db_id or 0, is_admin)

        # Физически удаляем файл если сообщение успешно удалено
        if ok and msg_row and msg_row[0]:
            fpath = msg_row[0]
            exp_uploads = os.path.abspath(_uploads_dir(org_db))
            real_fpath = os.path.abspath(fpath)
            if real_fpath.startswith(exp_uploads) and os.path.isfile(real_fpath):
                try:
                    os.remove(real_fpath)
                except OSError as e:
                    logger.warning(f"chat_delete: не удалось удалить файл {real_fpath}: {e}")

        return JSONResponse({"ok": ok, "error": None if ok else "Нет доступа или сообщение не найдено"})
    except Exception as exc:
        logger.error(f"chat_delete error: {exc}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)


@router.post("/chat/topics/create")
def chat_topic_create(
    request: Request,
    csrf_token: str = Form(default=""),
    name: str = Form(default=""),
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

    name = name.strip()[:64]
    if not name:
        return JSONResponse({"ok": False, "error": "Название темы не может быть пустым"}, status_code=400)

    if not _topic_rate_ok(telegram_id):
        return JSONResponse({"ok": False, "error": "Лимит создания тем: 5 в час"}, status_code=429)

    min_plan = _get_chat_min_plan()
    if min_plan == "Отключён":
        return JSONResponse({"ok": False, "error": "Чат отключён"}, status_code=403)

    try:
        db = get_web_db(telegram_id, org_db)
        if not _plan_allowed(_get_org_active_plan(telegram_id), min_plan):
            return JSONResponse({"ok": False, "error": "Недостаточный тариф"}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id) or 0
        new_id = db.add_chat_topic(name=name, created_by=user_db_id)
        return JSONResponse({"ok": True, "topic": {"id": new_id, "name": name}})

    except Exception as exc:
        logger.error(f"chat_topic_create error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


@router.post("/chat/topics/{topic_id}/rename")
def chat_topic_rename(
    request: Request,
    topic_id: int,
    csrf_token: str = Form(default=""),
    name: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    if topic_id == 1:
        return JSONResponse({"ok": False, "error": "Тему «Общий» нельзя переименовать"}, status_code=400)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    name = name.strip()[:64]
    if not name:
        return JSONResponse({"ok": False, "error": "Название не может быть пустым"}, status_code=400)

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        ok = db.rename_chat_topic(topic_id, name, user_db_id, is_admin)
        return JSONResponse({"ok": ok, "name": name if ok else ""})

    except Exception as exc:
        logger.error(f"chat_topic_rename error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


@router.post("/chat/topics/{topic_id}/archive")
def chat_topic_archive(
    request: Request,
    topic_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    if topic_id == 1:
        return JSONResponse({"ok": False, "error": "Тему «Общий» нельзя архивировать"}, status_code=400)

    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    if not is_admin:
        return JSONResponse({"ok": False, "error": "Недостаточно прав"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        db = get_web_db(telegram_id, org_db)
        ok = db.archive_chat_topic(topic_id)
        return JSONResponse({"ok": ok})

    except Exception as exc:
        logger.error(f"chat_topic_archive error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


@router.get("/chat/search")
def chat_search(request: Request, q: str = "", topic_id: int = 0):
    """Поиск сообщений в чате.

    topic_id=0  → глобальный поиск по всем темам (до 30 результатов)
    topic_id>0  → поиск только в указанной теме (до 25 результатов)
    Минимальная длина запроса: 2 символа.
    Rate limit: 30 req/min per IP.
    """
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "results": []}, status_code=401)

    ip = request.client.host if request.client else "unknown"
    if not _search_rate_ok(ip):
        return JSONResponse({"ok": False, "results": [], "error": "Слишком много запросов"}, status_code=429)

    q = q.strip()[:100]
    if len(q) < 2:
        return JSONResponse({"ok": True, "results": [], "scope": "topic" if topic_id else "global"})

    min_plan = _get_chat_min_plan()
    if min_plan == "Отключён":
        return JSONResponse({"ok": False, "results": []})

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        db = get_web_db(telegram_id, org_db)
        org_plan = _get_org_active_plan(telegram_id)
        if not _plan_allowed(org_plan, min_plan):
            return JSONResponse({"ok": False, "results": []}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id) or 0
        is_admin = user.get("role") in ("owner", "admin", "super_admin")

        limit = 25 if topic_id else 30
        rows = db.search_chat_messages(
            query=q,
            topic_id=topic_id if topic_id else None,
            limit=limit,
        )
        results = [_fmt_search_result(r, my_db_id=user_db_id, is_admin=is_admin) for r in rows]
        return JSONResponse({
            "ok": True,
            "results": results,
            "total": len(results),
            "scope": "topic" if topic_id else "global",
        })

    except Exception as exc:
        logger.error(f"chat_search error: {exc}")
        return JSONResponse({"ok": False, "results": [], "error": "Ошибка сервера"}, status_code=500)


# ══════════════════════════════════════════════════════════════════════════════
# DIRECT MESSAGES — routes, WebSocket, file serving
# ══════════════════════════════════════════════════════════════════════════════

_DM_SEND_RATE:   dict[int, list[float]] = {}
_DM_POLL_RATE:   dict[str, list[float]] = {}


def _dm_send_ok(tid: int)  -> bool: return _rate_ok(_DM_SEND_RATE, tid, 30, 60.0)
def _dm_poll_ok(ip: str)   -> bool: return _rate_ok(_DM_POLL_RATE, ip,  60, 60.0)


def _uploads_dir_dm(org_db: str) -> str:
    base = os.path.splitext(org_db)[0]
    d = base + "_uploads/dm"
    os.makedirs(d, exist_ok=True)
    return d


def _fmt_ts(raw) -> str:
    s = str(raw or "")[:16].replace("T", " ")
    try:
        d, t = s.split(" ")
        y, mo, day = d.split("-")
        return f"{day}.{mo}.{y} {t}"
    except Exception:
        return s


def _fmt_dm(row, my_db_id: int = 0) -> dict:
    (mid, from_id, to_id, message, file_path, file_name,
     file_type, file_size, created_at, is_read,
     fn, ln, uname) = row
    display = f"{fn or ''} {ln or ''}".strip() or uname or f"User#{from_id}"
    is_mine = (from_id == my_db_id)
    has_file = bool(file_path)
    is_image = (file_type or "").startswith("image/")
    return {
        "id": mid,
        "from_user_id": from_id,
        "to_user_id": to_id,
        "message": message or "",
        "file_name": file_name or "",
        "file_type": file_type or "",
        "file_size": file_size or 0,
        "has_file": has_file,
        "is_image": is_image,
        "file_url": f"/chat/dm/file/{mid}" if has_file else "",
        "created_at": _fmt_ts(created_at),
        "is_read": bool(is_read),
        "is_mine": is_mine,
        "display_name": display,
        "can_delete": is_mine,
    }


def _fmt_contact(row, my_id: int) -> dict:
    peer_id, fn, ln, uname, last_msg, last_from, last_file_name, last_at, unread = row
    display = f"{fn or ''} {ln or ''}".strip() or uname or f"User#{peer_id}"
    initial = (display[0] if display else "?").upper()
    preview = last_msg or (f"📎 {last_file_name}" if last_file_name else "")
    if last_from == my_id and preview:
        preview = "Вы: " + preview
    return {
        "id": peer_id,
        "display_name": display,
        "initial": initial,
        "last_msg": (preview or "")[:80],
        "last_at": _fmt_ts(last_at),
        "unread": int(unread or 0),
    }


# ── Page: contacts list ───────────────────────────────────────────────────────

@router.get("/chat/dm")
def dm_contacts_page(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    min_plan = _get_chat_min_plan()

    ctx = {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "contacts": [],
        "members": [],
        "chat_allowed": False,
        "min_plan": min_plan,
        "my_db_id": 0,
        "error": None,
    }

    if min_plan == "Отключён":
        ctx["error"] = "disabled"
        return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)

    try:
        db = get_web_db(telegram_id, org_db)
        org_plan = _get_org_active_plan(telegram_id)
        allowed = _plan_allowed(org_plan, min_plan)
        ctx["chat_allowed"] = allowed

        if allowed:
            user_db_id = _get_user_db_id(db, telegram_id) or 0
            ctx["my_db_id"] = user_db_id
            if user_db_id:
                raw_contacts = db.get_dm_contacts(user_db_id)
                ctx["contacts"] = [_fmt_contact(r, user_db_id) for r in raw_contacts]
                raw_members = db.get_dm_org_members(exclude_user_id=user_db_id)
                existing_ids = {c["id"] for c in ctx["contacts"]}
                ctx["members"] = [
                    {
                        "id": r[0],
                        "display_name": f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or f"User#{r[0]}",
                        "initial": ((f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or "?")[0]).upper(),
                        "shop_name": r[4] or "",
                    }
                    for r in raw_members if r[0] not in existing_ids
                ]
    except Exception as exc:
        logger.error(f"dm_contacts_page error: {exc}")
        ctx["error"] = "Внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)


# ── Page: conversation ────────────────────────────────────────────────────────

@router.get("/chat/dm/{peer_id}")
def dm_conversation_page(request: Request, peer_id: int):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    min_plan = _get_chat_min_plan()

    ctx = {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "contacts": [],
        "members": [],
        "peer": None,
        "messages": [],
        "chat_allowed": False,
        "min_plan": min_plan,
        "my_db_id": 0,
        "peer_id": peer_id,
        "error": None,
    }

    if min_plan == "Отключён":
        ctx["error"] = "disabled"
        return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)

    try:
        db = get_web_db(telegram_id, org_db)
        org_plan = _get_org_active_plan(telegram_id)
        allowed = _plan_allowed(org_plan, min_plan)
        ctx["chat_allowed"] = allowed

        if allowed:
            user_db_id = _get_user_db_id(db, telegram_id) or 0
            ctx["my_db_id"] = user_db_id
            if user_db_id:
                raw_contacts = db.get_dm_contacts(user_db_id)
                ctx["contacts"] = [_fmt_contact(r, user_db_id) for r in raw_contacts]
                raw_members = db.get_dm_org_members(exclude_user_id=user_db_id)
                existing_ids = {c["id"] for c in ctx["contacts"]}
                ctx["members"] = [
                    {
                        "id": r[0],
                        "display_name": f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or f"User#{r[0]}",
                        "initial": ((f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or "?")[0]).upper(),
                        "shop_name": r[4] or "",
                    }
                    for r in raw_members if r[0] not in existing_ids
                ]
                conn = db.get_connection()
                peer_row = conn.execute(
                    "SELECT id, first_name, last_name, username FROM users WHERE id = ?",
                    (peer_id,)
                ).fetchone()
                conn.close()
                if peer_row:
                    pname = f"{peer_row[1] or ''} {peer_row[2] or ''}".strip() or peer_row[3] or f"User#{peer_id}"
                    ctx["peer"] = {"id": peer_id, "display_name": pname, "initial": pname[0].upper()}
                    rows = db.get_dm_conversation(user_db_id, peer_id, limit=50)
                    ctx["messages"] = [_fmt_dm(r, my_db_id=user_db_id) for r in rows]
                    db.mark_dm_read(user_db_id, peer_id)
    except Exception as exc:
        logger.error(f"dm_conversation_page error: {exc}")
        ctx["error"] = "Внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)


# ── API: contacts (JSON) ──────────────────────────────────────────────────────

@router.get("/api/dm/contacts")
def api_dm_contacts(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "contacts": [], "unread_total": 0}, status_code=401)

    ip = request.client.host if request.client else "unknown"
    if not _dm_poll_ok(ip):
        return JSONResponse({"ok": True, "contacts": [], "unread_total": 0})

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": True, "contacts": [], "unread_total": 0})
        db = get_web_db(telegram_id, org_db)
        if not _plan_allowed(_get_org_active_plan(telegram_id), min_plan):
            return JSONResponse({"ok": True, "contacts": [], "unread_total": 0})
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if not user_db_id:
            return JSONResponse({"ok": True, "contacts": [], "unread_total": 0})
        raw = db.get_dm_contacts(user_db_id)
        contacts = [_fmt_contact(r, user_db_id) for r in raw]
        unread_total = sum(c["unread"] for c in contacts)
        return JSONResponse({"ok": True, "contacts": contacts, "unread_total": unread_total})
    except Exception as exc:
        logger.error(f"api_dm_contacts error: {exc}")
        return JSONResponse({"ok": False, "contacts": [], "unread_total": 0})


# ── API: conversation history (JSON, paged) ───────────────────────────────────

@router.get("/api/dm/conversation/{peer_id}")
def api_dm_conversation(request: Request, peer_id: int, before_id: int = 0):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "messages": [], "has_more": False}, status_code=401)

    ip = request.client.host if request.client else "unknown"
    if not _dm_poll_ok(ip):
        return JSONResponse({"ok": True, "messages": [], "has_more": False})

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": False, "messages": [], "has_more": False})
        db = get_web_db(telegram_id, org_db)
        if not _plan_allowed(_get_org_active_plan(telegram_id), min_plan):
            return JSONResponse({"ok": False, "messages": [], "has_more": False}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if not user_db_id:
            return JSONResponse({"ok": False, "messages": [], "has_more": False}, status_code=400)
        rows = db.get_dm_conversation(user_db_id, peer_id, limit=51, before_id=before_id)
        has_more = len(rows) > 50
        msgs = [_fmt_dm(r, my_db_id=user_db_id) for r in rows[:50]]
        return JSONResponse({"ok": True, "messages": msgs, "has_more": has_more})
    except Exception as exc:
        logger.error(f"api_dm_conversation error: {exc}")
        return JSONResponse({"ok": False, "messages": [], "has_more": False})


# ── HTTP send (with optional file) ────────────────────────────────────────────

@router.post("/chat/dm/send")
async def dm_send(
    request: Request,
    csrf_token: str = Form(default=""),
    to_user_id: int = Form(default=0),
    message: str = Form(default=""),
    file: UploadFile = File(default=None),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from web.ws_manager import dm_manager

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    if not _dm_send_ok(telegram_id):
        return JSONResponse({"ok": False, "error": "Слишком много сообщений"}, status_code=429)

    min_plan = _get_chat_min_plan()
    if min_plan == "Отключён":
        return JSONResponse({"ok": False, "error": "Чат отключён"}, status_code=403)

    if not to_user_id:
        return JSONResponse({"ok": False, "error": "Получатель не указан"}, status_code=400)

    try:
        db = get_web_db(telegram_id, org_db)
        org_plan = _get_org_active_plan(telegram_id)
        if not _plan_allowed(org_plan, min_plan):
            return JSONResponse({"ok": False, "error": "Недостаточный тариф"}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)

        text = message.strip()[:2000]
        file_path = file_name = file_type = ""
        file_size = 0

        if file and file.filename:
            raw_data = await file.read()
            fsize = len(raw_data)
            if fsize > MAX_FILE_SIZE:
                return JSONResponse({"ok": False, "error": "Файл слишком большой (макс. 20 МБ)"}, status_code=400)
            mime = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
            if not any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES):
                return JSONResponse({"ok": False, "error": "Тип файла не разрешён"}, status_code=400)
            safe_name = _safe_filename(file.filename)
            uid = uuid.uuid4().hex[:12]
            month_dir = datetime.now().strftime("%Y-%m")
            uploads = _uploads_dir_dm(org_db)
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

        new_id = db.add_dm(user_db_id, to_user_id, text, file_path, file_name, file_type, file_size)
        if not new_id:
            return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        payload = {
            "type": "message",
            "id": new_id,
            "from_user_id": user_db_id,
            "to_user_id": to_user_id,
            "message": text,
            "file_name": file_name,
            "file_type": file_type,
            "file_size": file_size,
            "has_file": bool(file_path),
            "is_image": file_type.startswith("image/") if file_type else False,
            "file_url": f"/chat/dm/file/{new_id}" if file_path else "",
            "created_at": now_str,
            "is_read": False,
        }
        await dm_manager.send_to_user(org_db, to_user_id, payload)
        try:
            first_name = user.get("name", "Кто-то")
            preview = text or (f"📎 {file_name}" if file_name else "")
            db.add_notification_to_history(
                user_id=to_user_id,
                notification_type="dm",
                message=f"💬 {first_name}: {preview[:80]}",
            )
        except Exception:
            pass

        msg = _fmt_dm(
            (new_id, user_db_id, to_user_id, text, file_path, file_name,
             file_type, file_size, now_str, 0,
             user.get("name", ""), "", ""),
            my_db_id=user_db_id,
        )
        return JSONResponse({"ok": True, "message": msg})

    except Exception as exc:
        logger.error(f"dm_send error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


# ── Delete DM ─────────────────────────────────────────────────────────────────

@router.post("/chat/dm/{msg_id}/delete")
def dm_delete(
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
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        row = db.get_dm_message(msg_id)
        ok = db.soft_delete_dm(msg_id, user_db_id, is_admin)
        if ok and row and row[4]:
            fpath = row[4]
            exp_uploads = os.path.abspath(_uploads_dir_dm(org_db))
            real_fpath = os.path.abspath(fpath)
            if real_fpath.startswith(exp_uploads) and os.path.isfile(real_fpath):
                try:
                    os.remove(real_fpath)
                except OSError as e:
                    logger.warning(f"dm_delete: не удалось удалить файл {real_fpath}: {e}")
        return JSONResponse({"ok": ok, "error": None if ok else "Нет доступа или сообщение не найдено"})
    except Exception as exc:
        logger.error(f"dm_delete error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


# ── File download ─────────────────────────────────────────────────────────────

@router.get("/chat/dm/file/{msg_id}")
def dm_file(request: Request, msg_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        row = db.get_dm_message(msg_id)
        if not row or row[10]:
            return Response(content="Файл не найден", status_code=404)
        _, from_id, to_id, _, fpath, fname, ftype, _, _, _, _ = row
        if not fpath or user_db_id not in (from_id, to_id):
            return Response(content="Доступ запрещён", status_code=403)
        if not os.path.isfile(fpath):
            return Response(content="Файл не найден на диске", status_code=404)
        exp_uploads = os.path.abspath(_uploads_dir_dm(org_db))
        if not os.path.abspath(fpath).startswith(exp_uploads):
            return Response(content="Доступ запрещён", status_code=403)
        return FileResponse(fpath, media_type=ftype or "application/octet-stream",
                            filename=fname or os.path.basename(fpath))
    except Exception as exc:
        logger.error(f"dm_file error: {exc}")
        return Response(content="Ошибка", status_code=500)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws/dm")
async def ws_dm(websocket: WebSocket):
    from web.auth import decode_session_token, COOKIE_NAME
    from web.deps import get_web_db
    from web.ws_manager import dm_manager

    token = websocket.cookies.get(COOKIE_NAME, "")
    user = decode_session_token(token) if token else None
    if not user:
        await websocket.close(code=4001)
        return

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    min_plan = _get_chat_min_plan()
    if min_plan == "Отключён" or not _plan_allowed(_get_org_active_plan(telegram_id), min_plan):
        await websocket.close(code=4003)
        return

    try:
        db = get_web_db(telegram_id, org_db)
    except Exception:
        await websocket.close(code=4004)
        return

    user_db_id = _get_user_db_id(db, telegram_id)
    if not user_db_id:
        await websocket.close(code=4004)
        return

    await dm_manager.connect(org_db, user_db_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "message":
                to_id = int(data.get("to_user_id", 0))
                text = str(data.get("message", "")).strip()[:2000]
                if not to_id or not text:
                    continue
                if not _dm_send_ok(telegram_id):
                    await websocket.send_json({"type": "error", "message": "Слишком много сообщений"})
                    continue
                new_id = db.add_dm(user_db_id, to_id, text)
                if not new_id:
                    await websocket.send_json({"type": "error", "message": "Ошибка сервера"})
                    continue
                now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
                payload = {
                    "type": "message",
                    "id": new_id,
                    "from_user_id": user_db_id,
                    "to_user_id": to_id,
                    "message": text,
                    "has_file": False,
                    "created_at": now_str,
                    "is_read": False,
                }
                await websocket.send_json({**payload, "confirmed": True})
                await dm_manager.send_to_user(org_db, to_id, payload)
                try:
                    first_name = user.get("name", "Кто-то")
                    db.add_notification_to_history(
                        user_id=to_id,
                        notification_type="dm",
                        message=f"💬 {first_name}: {text[:80]}",
                    )
                except Exception:
                    pass

            elif msg_type == "typing":
                to_id = int(data.get("to_user_id", 0))
                if to_id:
                    await dm_manager.send_to_user(org_db, to_id, {
                        "type": "typing",
                        "from_user_id": user_db_id,
                    })

            elif msg_type == "read":
                peer_id = int(data.get("peer_id", 0))
                if peer_id:
                    db.mark_dm_read(user_db_id, peer_id)
                    await dm_manager.send_to_user(org_db, peer_id, {
                        "type": "read",
                        "by_user_id": user_db_id,
                    })

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("ws_dm error uid=%s: %s", user_db_id, e)
    finally:
        dm_manager.disconnect(org_db, user_db_id)
