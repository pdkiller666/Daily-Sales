import asyncio
import logging
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone

from typing import List

from fastapi import APIRouter, Form, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse, Response, FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ на файл
MAX_FILES_PER_MSG = 10            # до 10 файлов в одном сообщении


def _get_chat_min_plan() -> str:
    """Stub: chat access is now controlled by has_module('chat').
    Returns 'Базовый' (never 'Отключён') so all legacy 'disabled' checks pass through."""
    return "Базовый"


def _get_org_active_plan(telegram_id: int) -> str:
    """Stub: plan-based access replaced by has_module(). Returns empty string for display."""
    return ""

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


def _client_ip(request: Request) -> str:
    """Реальный IP клиента за прокси Amvera: X-Forwarded-For → client.host.

    За прокси request.client.host = IP прокси (один на всех) → не годится для
    rate-limit. Авторизованные эндпойнты ключуются по telegram_id, IP — фолбэк
    для неаутентифицированных путей.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _send_rate_ok(tid: int)   -> bool: return _rate_ok(_SEND_RATE_STORE,   tid, 30, 60.0)
def _poll_rate_ok(key)        -> bool: return _rate_ok(_POLL_RATE_STORE,   key, 60, 60.0)
def _topic_rate_ok(tid: int)  -> bool: return _rate_ok(_TOPIC_RATE_STORE,  tid,  5, 3600.0)
def _search_rate_ok(key)      -> bool: return _rate_ok(_SEARCH_RATE_STORE, key, 30, 60.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_user_db_id(db, telegram_id: int) -> int | None:
    try:
        conn = db.get_connection()
        row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None




def _uploads_dir(org_db: str) -> str:
    base = os.path.splitext(org_db)[0]
    d = base + "_uploads/chat"
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename(original: str) -> str:
    name = os.path.basename(original)
    name = re.sub(r'[^\w.\-]', '_', name)
    return name[:120] or "file"


def _fmt_msg(row, my_db_id: int = 0, is_admin: bool = False, files=None) -> dict:
    """Форматировать строку chat_messages.
    files=None  → использовать legacy-колонки file_path/file_name/... из row
    files=[]    → новое сообщение без вложений
    files=[...] → список dicts из chat_message_files
    """
    mid, user_id, message, file_path, file_name, file_type, file_size, created_at, fn, ln, uname = row
    if user_id == 0:
        display = "AI-ассистент"
        initial = "🤖"
    else:
        display = f"{fn or ''} {ln or ''}".strip() or uname or f"User#{user_id}"
        initial = (display[0] if display else "?").upper()
    raw = str(created_at or "")[:16].replace("T", " ")
    try:
        d, t = raw.split(" ")
        y, mo, day = d.split("-")
        ts = f"{day}.{mo}.{y} {t}"
    except Exception:
        ts = raw

    if files is not None:
        files_list = [
            {
                "id": f["id"], "file_name": f["file_name"],
                "file_type": f["file_type"], "file_size": f["file_size"],
                "is_image": (f["file_type"] or "").startswith("image/"),
                "file_url": f"/chat/file/attachment/{f['id']}",
            }
            for f in files
        ]
    elif file_path:
        files_list = [{
            "id": 0, "file_name": file_name or "",
            "file_type": file_type or "", "file_size": file_size or 0,
            "is_image": bool(file_type and file_type.startswith("image/")),
            "file_url": f"/chat/file/{mid}",
        }]
    else:
        files_list = []

    first = files_list[0] if files_list else {}
    return {
        "id": mid,
        "user_id": user_id,
        "is_ai": user_id == 0,
        "message": message or "",
        "file_name": first.get("file_name", ""),
        "file_type": first.get("file_type", ""),
        "file_size": first.get("file_size", 0),
        "has_file": bool(files_list),
        "is_image": first.get("is_image", False),
        "file_url": first.get("file_url", ""),
        "files": files_list,
        "display_name": display,
        "initial": initial,
        "created_at": ts,
        "can_delete": is_admin or (my_db_id > 0 and user_id == my_db_id),
    }


def _load_msg_files_bulk(db, message_ids: list) -> dict:
    """Батч-загрузка файлов для списка сообщений. Возвращает {msg_id: [file_dicts]}."""
    try:
        return db.get_chat_message_files_bulk(message_ids)
    except Exception:
        return {}


_AI_CHAT_DAILY_LIMIT = 50


async def _build_ai_org_context(db, user_db_id: int) -> tuple[str, str, list]:
    """Собирает обогащённый контекст организации для AI-ассистента.

    Возвращает (org_name, user_name, context_parts) — три составляющих промпта.
    Вызывается из _ai_chat_reply и _ai_dm_reply чтобы не дублировать логику.
    """
    import anyio

    sales_today    = await anyio.to_thread.run_sync(db.get_sales_summary_today)
    sales_month    = await anyio.to_thread.run_sync(db.get_sales_summary_month)
    user_row       = await anyio.to_thread.run_sync(lambda: db.get_user_by_id(user_db_id))
    org_name       = await anyio.to_thread.run_sync(db.get_org_name)
    top_products   = await anyio.to_thread.run_sync(db.get_top_products_month)
    active_sellers = await anyio.to_thread.run_sync(db.get_active_sellers_month)
    plans_progress = await anyio.to_thread.run_sync(db.get_plans_with_progress)

    user_name = ""
    if user_row:
        fn = user_row[3] if len(user_row) > 3 else ""
        ln = user_row[4] if len(user_row) > 4 else ""
        user_name = f"{fn or ''} {ln or ''}".strip() or "сотрудник"

    top_products_text = ""
    if top_products:
        lines = []
        for i, (pname, qty, rev) in enumerate(top_products, 1):
            lines.append(f"  {i}. {pname}: {rev:,.0f} руб. ({qty} шт.)")
        top_products_text = "Топ товаров за месяц:\n" + "\n".join(lines)

    sellers_text = ""
    if active_sellers:
        names = []
        for fn2, ln2, shop, rev in active_sellers:
            name = f"{fn2 or ''} {ln2 or ''}".strip() or "—"
            shop_part = f" ({shop})" if shop else ""
            names.append(f"{name}{shop_part}: {rev:,.0f} руб.")
        sellers_text = "Активные продавцы за месяц:\n" + "\n".join(f"  - {n}" for n in names)

    plans_text = ""
    if plans_progress:
        lines = []
        for p in plans_progress[:5]:
            metric_unit = "руб." if "выручка" in p["label"] else "шт."
            line = (
                f"  - {p['label']}: {p['current']:,.0f} / {p['target']:,.0f} {metric_unit} ({p['pct']}%)"
            )
            lines.append(line)
            if p.get("seller_breakdown"):
                for bd in p["seller_breakdown"]:
                    lines.append(
                        f"      • {bd['name']}: {bd['current']:,.0f} {metric_unit} ({bd['pct']}%)"
                    )
            elif p.get("seller_name"):
                lines.append(f"      • Продавец: {p['seller_name']}")
        plans_text = "Планы продаж (прогресс):\n" + "\n".join(lines)

    context_parts = [
        f"- Продажи сегодня: {sales_today}",
        f"- Продажи за месяц: {sales_month}",
    ]
    if top_products_text:
        context_parts.append(top_products_text)
    if sellers_text:
        context_parts.append(sellers_text)
    if plans_text:
        context_parts.append(plans_text)

    return org_name, user_name, context_parts


async def _ai_chat_reply(org_db: str, topic_id: int, user_db_id: int, user_text: str):
    """Асинхронно формирует и сохраняет ответ AI-ассистента в топик чата.

    Запускается через asyncio.create_task — основной /chat/send не ждёт.
    Любой сбой глотается: AI-ошибка никогда не роняет основной чат.
    """
    try:
        from billing_utils import has_extension
        from web.ai_utils import ask_llm
        from web.deps import get_web_db
        from web.rate_store import check_and_increment_ai
        import anyio

        db = await anyio.to_thread.run_sync(lambda: get_web_db(0, org_db))

        async def _post_status(text: str):
            try:
                await anyio.to_thread.run_sync(
                    lambda: db.add_chat_message(user_id=0, message=text, topic_id=topic_id)
                )
            except Exception:
                pass

        owner_tg_id = await anyio.to_thread.run_sync(db.get_org_owner_tg_id)
        if not owner_tg_id:
            return
        if not has_extension(owner_tg_id, 'ai_chat_assistant'):
            return
        if not check_and_increment_ai(owner_tg_id, _AI_CHAT_DAILY_LIMIT):
            await _post_status(
                f"🤖 Дневной лимит AI-запросов исчерпан "
                f"({_AI_CHAT_DAILY_LIMIT}/день). Попробуйте завтра."
            )
            return

        org_name, user_name, context_parts = await _build_ai_org_context(db, user_db_id)

        prompt = (
            f"Ты AI-ассистент торговой организации «{org_name}».\n"
            f"Отвечай коротко и по делу на русском языке.\n\n"
            f"Текущие данные:\n"
            + "\n".join(context_parts)
            + f"\n- Спрашивает: {user_name}\n\n"
            f"Вопрос: {user_text}\n\n"
            f"Ответь в 2-4 предложениях. Если вопрос не связан с продажами/магазином — "
            f"скажи что можешь помочь только с данными организации."
        )

        try:
            answer = await ask_llm(prompt, max_tokens=300)
        except Exception:
            answer = None
        if not answer:
            await _post_status("🤖 AI-ассистент временно недоступен, попробуйте позже.")
            return

        ai_text = f"🤖 {answer}"
        await anyio.to_thread.run_sync(
            lambda: db.add_chat_message(user_id=0, message=ai_text, topic_id=topic_id)
        )
    except Exception:
        pass


async def _ai_dm_reply(org_db: str, sender_db_id: int, user_text: str, peer_id: int = 0):
    """Асинхронно формирует и сохраняет ответ AI-ассистента в личных сообщениях.

    Запускается через asyncio.create_task — основной dm_send не ждёт.
    Любой сбой глотается: AI-ошибка никогда не роняет основной чат.
    Ответ сохраняется как DM от user_id=0 (AI) отправителю и доставляется по WS.
    peer_id — собеседник, в переписке с которым задан вопрос; записывается в
    ai_peer_id, чтобы ответ AI был виден в истории после рефреша/поллинга.
    """
    try:
        from billing_utils import has_extension
        from web.ai_utils import ask_llm
        from web.deps import get_web_db
        from web.rate_store import check_and_increment_ai
        from web.ws_manager import dm_manager
        import anyio

        db = await anyio.to_thread.run_sync(lambda: get_web_db(0, org_db))

        async def _post_ai_dm(text: str):
            """Сохранить ответ/статус AI как DM и доставить по WS отправителю."""
            new_id = await anyio.to_thread.run_sync(
                lambda: db.add_dm(from_user_id=0, to_user_id=sender_db_id,
                                  message=text, ai_peer_id=peer_id)
            )
            if not new_id:
                return
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            await dm_manager.send_to_user(org_db, sender_db_id, {
                "type": "message",
                "id": new_id,
                "from_user_id": 0,
                "to_user_id": sender_db_id,
                "ai_peer_id": peer_id,
                "message": text,
                "has_file": False,
                "created_at": now_str,
                "is_read": False,
                "is_ai": True,
            })

        owner_tg_id = await anyio.to_thread.run_sync(db.get_org_owner_tg_id)
        if not owner_tg_id:
            return
        if not has_extension(owner_tg_id, 'ai_chat_assistant'):
            return
        if not check_and_increment_ai(owner_tg_id, _AI_CHAT_DAILY_LIMIT):
            await _post_ai_dm(
                f"🤖 Дневной лимит AI-запросов исчерпан "
                f"({_AI_CHAT_DAILY_LIMIT}/день). Попробуйте завтра."
            )
            return

        org_name, user_name, context_parts = await _build_ai_org_context(db, sender_db_id)

        prompt = (
            f"Ты AI-ассистент торговой организации «{org_name}».\n"
            f"Отвечай коротко и по делу на русском языке.\n\n"
            f"Текущие данные:\n"
            + "\n".join(context_parts)
            + f"\n- Спрашивает: {user_name}\n\n"
            f"Вопрос: {user_text}\n\n"
            f"Ответь в 2-4 предложениях. Если вопрос не связан с продажами/магазином — "
            f"скажи что можешь помочь только с данными организации."
        )

        try:
            answer = await ask_llm(prompt, max_tokens=300)
        except Exception:
            answer = None
        if not answer:
            await _post_ai_dm("🤖 AI-ассистент временно недоступен, попробуйте позже.")
            return

        await _post_ai_dm(f"🤖 {answer}")
    except Exception:
        pass


def _load_dm_files_bulk(db, dm_ids: list) -> dict:
    """Батч-загрузка файлов для списка DM-сообщений. Возвращает {dm_id: [file_dicts]}."""
    try:
        return db.get_dm_files_bulk(dm_ids)
    except Exception:
        return {}


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
    base["result_type"]       = "topic"
    base["result_topic_id"]   = row[11] or 1
    base["result_topic_name"] = row[12] or "Общий"
    base["dm_peer_id"]        = None
    base["dm_peer_name"]      = None
    return base


def _fmt_search_result_dm(row, my_db_id: int = 0) -> dict:
    """Форматирует строку из search_dm_messages (15 колонок) в поисковый результат."""
    # (id, from_uid, peer_id, message, file_path, file_name, file_type, file_size,
    #  created_at, from_fname, from_lname, from_uname, peer_fname, peer_lname, peer_uname)
    msg_id   = row[0]
    from_uid = row[1]
    peer_id  = row[2]
    message  = row[3] or ""
    file_path = row[4] or ""
    file_name = row[5] or ""
    file_type = row[6] or ""
    created_at = str(row[8] or "")[:16].replace("T", " ")

    from_name = f"{row[9] or ''} {row[10] or ''}".strip() or row[11] or f"User#{from_uid}"
    peer_name = f"{row[12] or ''} {row[13] or ''}".strip() or row[14] or f"User#{peer_id}"

    return {
        "id":               msg_id,
        "result_type":      "dm",
        "user_id":          from_uid,
        "display_name":     from_name,
        "initial":          (from_name[0].upper()) if from_name else "?",
        "message":          message,
        "created_at":       created_at,
        "has_file":         bool(file_path),
        "file_name":        file_name,
        "is_image":         (file_type or "").startswith("image/"),
        "is_mine":          (from_uid == my_db_id),
        "result_topic_id":  None,
        "result_topic_name": None,
        "dm_peer_id":       peer_id,
        "dm_peer_name":     peer_name,
    }


def _chat_access_ok(telegram_id: int) -> bool:
    """Доступ к чату через модуль 'chat' в биллинг-системе."""
    try:
        from billing_utils import has_module
        return has_module(telegram_id, "chat")
    except Exception:
        return False


def _ensure_access(db, telegram_id: int) -> tuple[bool, str]:
    """Проверяет доступ к чату. Возвращает (allowed, '')."""
    return _chat_access_ok(telegram_id), ""


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
        "ai_chat_enabled": False,
    }

    if min_plan == "Отключён":
        ctx["error"] = "disabled"
        return request.app.state.templates.TemplateResponse(request, "chat/index.html", ctx)

    try:
        db = get_web_db(telegram_id, org_db)
        allowed = _chat_access_ok(telegram_id)
        ctx["chat_allowed"] = allowed
        ctx["org_plan"] = _get_org_active_plan(telegram_id)

        if allowed:
            user_db_id = _get_user_db_id(db, telegram_id)
            ctx["my_db_id"] = user_db_id or 0

            raw_topics = db.get_chat_topics()
            topics = [_fmt_topic(r) for r in raw_topics]
            if not topics:
                topics = [{"id": 1, "name": "Общий", "created_by": None, "msg_count": 0}]

            # Серверный учёт непрочитанного по темам (синхрон между устройствами)
            try:
                unread_map = db.get_chat_unread_counts(user_db_id or 0)
                for t in topics:
                    t["unread"] = int(unread_map.get(t["id"], 0))
            except Exception:
                for t in topics:
                    t["unread"] = 0
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

            # Текущая тема открыта → помечаем прочитанной + обнуляем её бейдж
            try:
                db.set_chat_read(user_db_id or 0, topic, ctx["latest_id"])
                for t in topics:
                    if t["id"] == topic:
                        t["unread"] = 0
            except Exception:
                pass

            try:
                from billing_utils import has_extension
                owner_tg_id = db.get_org_owner_tg_id() or telegram_id
                ctx["ai_chat_enabled"] = has_extension(owner_tg_id, 'ai_chat_assistant')
            except Exception:
                pass

    except Exception as exc:
        logger.error(f"chat_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "chat/index.html", ctx)


async def _save_uploaded_files(files_list, uploads_dir: str) -> list:
    """Сохранить до MAX_FILES_PER_MSG файлов на диск. Возвращает список dict."""
    saved = []
    month_dir = datetime.now().strftime("%Y-%m")
    month_path = os.path.join(uploads_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    for f in files_list:
        if not f or not f.filename:
            continue
        if len(saved) >= MAX_FILES_PER_MSG:
            break
        try:
            raw_data = await f.read()
            fsize = len(raw_data)
            if fsize == 0 or fsize > MAX_FILE_SIZE:
                continue
            mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
            safe_name = _safe_filename(f.filename)
            uid = uuid.uuid4().hex[:12]
            dest = os.path.join(month_path, f"{uid}_{safe_name}")
            with open(dest, "wb") as fout:
                fout.write(raw_data)
            saved.append({
                "file_path": dest, "file_name": f.filename[:255],
                "file_type": mime, "file_size": fsize,
            })
        except Exception as exc:
            logger.warning(f"_save_uploaded_files skip: {exc}")
    return saved


@router.post("/chat/send")
async def chat_send(
    request: Request,
    csrf_token: str = Form(default=""),
    message: str = Form(default=""),
    topic_id: int = Form(default=1),
    files: List[UploadFile] = File(default=[]),
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
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "error": "Недостаточный тариф"}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)

        valid_topics = {r[0] for r in db.get_chat_topics()}
        if not valid_topics:
            valid_topics = {1}
        if topic_id not in valid_topics:
            topic_id = 1

        text = message.strip()[:2000]
        saved_files = await _save_uploaded_files(files, _uploads_dir(org_db))

        if not text and not saved_files:
            return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)

        new_id = db.add_chat_message(user_id=user_db_id, message=text, topic_id=topic_id)
        if saved_files:
            db.add_chat_message_files(new_id, saved_files)

        # AI hook: если сообщение адресовано AI — запустить ответ асинхронно
        if text and text.lower().lstrip().startswith(('@ии', '/ai', '@ai')):
            asyncio.create_task(_ai_chat_reply(org_db, topic_id, user_db_id, text))

        # Web Push участникам организации (кроме отправителя) — общий чат
        try:
            sender_name = user.get("name") or "Сотрудник"
            preview = (text or ("📎 Вложение" if saved_files else "")).strip()[:120]
            member_tids = [
                int(u[1]) for u in (db.get_all_users() or [])
                if u[1] and int(u[1]) != telegram_id
            ]
            if member_tids and preview:
                from web.push_utils import apush_bulk
                await apush_bulk(member_tids, "💬 Новое сообщение в чате", f"{sender_name}: {preview}", "/chat")
        except Exception:
            pass

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        new_msgs = db.get_chat_messages_since(new_id - 1, topic_id=topic_id)
        msg_ids = [r[0] for r in new_msgs]
        files_map = _load_msg_files_bulk(db, msg_ids)
        result = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin, files=files_map.get(r[0])) for r in new_msgs]
        return JSONResponse({"ok": True, "messages": result, "latest_id": new_id})

    except Exception as exc:
        logger.error(f"chat_send error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


@router.get("/chat/poll")
def chat_poll(request: Request, since_id: int = 0, topic_id: int = 1, del_since: str = "", mark_read: int = 1):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "messages": [], "latest_id": since_id})

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # rate-limit по пользователю (за прокси Amvera IP общий для всех)
    if not _poll_rate_ok(telegram_id):
        return JSONResponse({"ok": True, "messages": [], "latest_id": since_id, "now": now_ts})

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": True, "messages": [], "latest_id": since_id, "now": now_ts})

        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": True, "messages": [], "latest_id": since_id, "now": now_ts})

        user_db_id = _get_user_db_id(db, telegram_id) or 0
        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        rows = db.get_chat_messages_since(since_id, topic_id=topic_id)
        msg_ids = [r[0] for r in rows]
        files_map = _load_msg_files_bulk(db, msg_ids)
        msgs = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin, files=files_map.get(r[0])) for r in rows]
        latest = msgs[-1]["id"] if msgs else since_id

        # Удаления у всех в реальном времени (с момента прошлого опроса)
        deleted_ids = db.get_chat_deleted_ids_since(topic_id, del_since)

        # Текущая тема прочитана до latest; бейджи остальных тем.
        # mark_read=0 → вкладка скрыта: НЕ помечаем прочитанным (непрочитанное копится)
        if mark_read:
            try:
                db.set_chat_read(user_db_id, topic_id, latest)
            except Exception:
                pass
        topic_unread = {}
        try:
            topic_unread = {str(k): v for k, v in db.get_chat_unread_counts(user_db_id).items()}
        except Exception:
            pass

        return JSONResponse({
            "ok": True, "messages": msgs, "latest_id": latest,
            "deleted_ids": deleted_ids, "topic_unread": topic_unread, "now": now_ts,
        })

    except Exception as exc:
        logger.error(f"chat_poll error: {exc}")
        return JSONResponse({"ok": True, "messages": [], "latest_id": since_id, "now": now_ts})


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

    if not _poll_rate_ok(telegram_id):
        return JSONResponse({"ok": True, "messages": [], "latest_id": 0})

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": False, "messages": [], "latest_id": 0})

        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "messages": [], "latest_id": 0})

        user_db_id = _get_user_db_id(db, telegram_id) or 0
        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        rows = db.get_chat_messages(limit=50, topic_id=topic_id)
        msg_ids = [r[0] for r in rows]
        files_map = _load_msg_files_bulk(db, msg_ids)
        msgs = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin, files=files_map.get(r[0])) for r in rows]
        latest = db.get_chat_latest_id(topic_id=topic_id)
        try:
            db.set_chat_read(user_db_id, topic_id, latest)
        except Exception:
            pass
        return JSONResponse({"ok": True, "messages": msgs, "latest_id": latest})

    except Exception as exc:
        logger.error(f"chat_topic_messages error: {exc}")
        return JSONResponse({"ok": True, "messages": [], "latest_id": 0})


@router.post("/chat/read")
def chat_mark_read(
    request: Request,
    topic_id: int = Form(default=0),
    last_id: int = Form(default=0),
    csrf_token: str = Form(default=""),
):
    """Отметить тему прочитанной до last_id (серверный учёт, синхрон между устройствами)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    try:
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if user_db_id and topic_id:
            db.set_chat_read(user_db_id, topic_id, last_id)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error(f"chat_mark_read error: {exc}")
        return JSONResponse({"ok": False}, status_code=500)


@router.get("/chat/file/attachment/{att_id}")
def chat_file_attachment(request: Request, att_id: int):
    """Отдать файл из chat_message_files (новый мультифайловый маршрут)."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        db = get_web_db(telegram_id, org_db)
        row = db.get_chat_message_file(att_id)
        if not row or not row[0]:
            return Response(content="Файл не найден", status_code=404)

        fpath, fname, ftype, _ = row
        if not os.path.isfile(fpath):
            return Response(content="Файл не найден на диске", status_code=404)

        exp_uploads = os.path.abspath(_uploads_dir(org_db))
        real_fpath = os.path.abspath(fpath)
        if not real_fpath.startswith(exp_uploads):
            return Response(content="Доступ запрещён", status_code=403)

        return FileResponse(fpath, media_type=ftype or "application/octet-stream",
                            filename=fname or os.path.basename(fpath))
    except Exception as exc:
        logger.error(f"chat_file_attachment error: {exc}")
        return Response(content="Ошибка", status_code=500)


@router.get("/chat/file/{msg_id}")
def chat_file(request: Request, msg_id: int):
    """Legacy: отдать одиночный файл из chat_messages.file_path (старый формат)."""
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

        if ok:
            exp_uploads = os.path.abspath(_uploads_dir(org_db))
            # Удаляем legacy single-file
            if msg_row and msg_row[0]:
                fpath = msg_row[0]
                real_fpath = os.path.abspath(fpath)
                if real_fpath.startswith(exp_uploads) and os.path.isfile(real_fpath):
                    try:
                        os.remove(real_fpath)
                    except OSError as e:
                        logger.warning(f"chat_delete legacy file: {e}")
            # Удаляем новые multi-file вложения
            for fpath in db.delete_chat_message_files(msg_id):
                real_fpath = os.path.abspath(fpath)
                if real_fpath.startswith(exp_uploads) and os.path.isfile(real_fpath):
                    try:
                        os.remove(real_fpath)
                    except OSError as e:
                        logger.warning(f"chat_delete new file: {e}")

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
        if not _chat_access_ok(telegram_id):
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
    Rate limit: 30 req/min на пользователя (telegram_id; за прокси IP общий).
    """
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "results": []}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    if not _search_rate_ok(telegram_id):
        return JSONResponse({"ok": False, "results": [], "error": "Слишком много запросов"}, status_code=429)

    q = q.strip()[:100]
    if len(q) < 2:
        return JSONResponse({"ok": True, "results": [], "scope": "topic" if topic_id else "global"})

    min_plan = _get_chat_min_plan()
    if min_plan == "Отключён":
        return JSONResponse({"ok": False, "results": []})

    try:
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "results": []}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id) or 0
        is_admin = user.get("role") in ("owner", "admin", "super_admin")

        if topic_id:
            # Поиск только в конкретной теме
            rows = db.search_chat_messages(query=q, topic_id=topic_id, limit=25)
            results = [_fmt_search_result(r, my_db_id=user_db_id, is_admin=is_admin) for r in rows]
        else:
            # Глобальный поиск: темы + личные сообщения
            rows = db.search_chat_messages(query=q, topic_id=None, limit=20)
            results = [_fmt_search_result(r, my_db_id=user_db_id, is_admin=is_admin) for r in rows]
            if user_db_id:
                dm_rows = db.search_dm_messages(query=q, user_id=user_db_id, limit=10)
                dm_results = [_fmt_search_result_dm(r, my_db_id=user_db_id) for r in dm_rows]
                results = results + dm_results
                results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                results = results[:30]

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
def _dm_poll_ok(key)       -> bool: return _rate_ok(_DM_POLL_RATE, key, 60, 60.0)


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


def _fmt_dm(row, my_db_id: int = 0, files=None) -> dict:
    """Форматировать строку direct_messages.
    files=None  → legacy single-file из колонок
    files=[]    → нет вложений
    files=[...] → список dicts из dm_message_files
    """
    (mid, from_id, to_id, message, file_path, file_name,
     file_type, file_size, created_at, is_read,
     fn, ln, uname) = row
    display = f"{fn or ''} {ln or ''}".strip() or uname or f"User#{from_id}"
    is_mine = (from_id == my_db_id)

    if files is not None:
        files_list = [
            {
                "id": f["id"], "file_name": f["file_name"],
                "file_type": f["file_type"], "file_size": f["file_size"],
                "is_image": (f["file_type"] or "").startswith("image/"),
                "file_url": f"/chat/dm/file/attachment/{f['id']}",
            }
            for f in files
        ]
    elif file_path:
        files_list = [{
            "id": 0, "file_name": file_name or "",
            "file_type": file_type or "", "file_size": file_size or 0,
            "is_image": bool(file_type and file_type.startswith("image/")),
            "file_url": f"/chat/dm/file/{mid}",
        }]
    else:
        files_list = []

    first = files_list[0] if files_list else {}
    return {
        "id": mid,
        "from_user_id": from_id,
        "to_user_id": to_id,
        "message": message or "",
        "file_name": first.get("file_name", ""),
        "file_type": first.get("file_type", ""),
        "file_size": first.get("file_size", 0),
        "has_file": bool(files_list),
        "is_image": first.get("is_image", False),
        "file_url": first.get("file_url", ""),
        "files": files_list,
        "created_at": _fmt_ts(created_at),
        "is_read": bool(is_read),
        "is_mine": is_mine,
        "is_ai": (from_id == 0),
        "display_name": display,
        "can_delete": is_mine,
    }


def _fmt_contact(row, my_id: int) -> dict:
    peer_id, fn, ln, uname, last_msg, last_from, last_file_name, last_at, unread = row
    display = f"{fn or ''} {ln or ''}".strip() or uname or f"User#{peer_id}"
    initial = (display[0] if display else "?").upper()
    preview = last_msg or (f"📎 {last_file_name}" if last_file_name else "")
    if last_from == 0 and preview:
        preview = "🤖 " + preview
    elif last_from == my_id and preview:
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
    return RedirectResponse(url="/chat?dm=1", status_code=301)


@router.get("/chat/dm/_legacy")
def dm_contacts_page_legacy(request: Request):
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
        "ai_chat_enabled": False,
    }

    if min_plan == "Отключён":
        ctx["error"] = "disabled"
        return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)

    try:
        db = get_web_db(telegram_id, org_db)
        allowed = _chat_access_ok(telegram_id)
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
            try:
                from billing_utils import has_extension
                owner_tg_id = db.get_org_owner_tg_id() or telegram_id
                ctx["ai_chat_enabled"] = has_extension(owner_tg_id, 'ai_chat_assistant')
            except Exception:
                pass
    except Exception as exc:
        logger.error(f"dm_contacts_page error: {exc}")
        ctx["error"] = "Внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)


# ── Page: conversation ────────────────────────────────────────────────────────

@router.get("/chat/dm/{peer_id}")
def dm_conversation_page(request: Request, peer_id: int):
    return RedirectResponse(url=f"/chat?dm=1&peer={peer_id}", status_code=301)


@router.get("/chat/dm/_legacy/{peer_id}")
def dm_conversation_page_legacy(request: Request, peer_id: int):
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
        "ai_chat_enabled": False,
    }

    if min_plan == "Отключён":
        ctx["error"] = "disabled"
        return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)

    try:
        db = get_web_db(telegram_id, org_db)
        allowed = _chat_access_ok(telegram_id)
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
                    dm_ids = [r[0] for r in rows]
                    dm_files_map = _load_dm_files_bulk(db, dm_ids)
                    ctx["messages"] = [_fmt_dm(r, my_db_id=user_db_id, files=dm_files_map.get(r[0])) for r in rows]
                    db.mark_dm_read(user_db_id, peer_id)
            try:
                from billing_utils import has_extension
                owner_tg_id = db.get_org_owner_tg_id() or telegram_id
                ctx["ai_chat_enabled"] = has_extension(owner_tg_id, 'ai_chat_assistant')
            except Exception:
                pass
    except Exception as exc:
        logger.error(f"dm_conversation_page error: {exc}")
        ctx["error"] = "Внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(request, "chat/dm.html", ctx)


# ── API: org members (for "new conversation" list) ───────────────────────────

@router.get("/api/dm/members")
def api_dm_members(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "members": []}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": True, "members": []})
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": True, "members": []})
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if not user_db_id:
            return JSONResponse({"ok": True, "members": []})
        rows = db.get_dm_org_members(exclude_user_id=user_db_id)
        members = [
            {
                "id": r[0],
                "display_name": f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or f"User#{r[0]}",
                "initial": ((f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or "?")[0]).upper(),
                "shop_name": r[4] or "",
            }
            for r in rows
        ]
        return JSONResponse({"ok": True, "members": members})
    except Exception as exc:
        logger.error(f"api_dm_members error: {exc}")
        return JSONResponse({"ok": False, "members": []})


# ── API: contacts (JSON) ──────────────────────────────────────────────────────

@router.get("/api/dm/contacts")
def api_dm_contacts(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "contacts": [], "unread_total": 0}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    if not _dm_poll_ok(telegram_id):
        return JSONResponse({"ok": True, "contacts": [], "unread_total": 0})

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": True, "contacts": [], "unread_total": 0})
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
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

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    if not _dm_poll_ok(telegram_id):
        return JSONResponse({"ok": True, "messages": [], "has_more": False})

    try:
        min_plan = _get_chat_min_plan()
        if min_plan == "Отключён":
            return JSONResponse({"ok": False, "messages": [], "has_more": False})
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "messages": [], "has_more": False}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if not user_db_id:
            return JSONResponse({"ok": False, "messages": [], "has_more": False}, status_code=400)
        rows = db.get_dm_conversation(user_db_id, peer_id, limit=51, before_id=before_id)
        has_more = len(rows) > 50
        page = rows[:50]
        dm_ids = [r[0] for r in page]
        dm_files_map = _load_dm_files_bulk(db, dm_ids)
        msgs = [_fmt_dm(r, my_db_id=user_db_id, files=dm_files_map.get(r[0])) for r in page]
        return JSONResponse({"ok": True, "messages": msgs, "has_more": has_more})
    except Exception as exc:
        logger.error(f"api_dm_conversation error: {exc}")
        return JSONResponse({"ok": False, "messages": [], "has_more": False})


# ── HTTP send (with optional file) ────────────────────────────────────────────

@router.get("/chat/dm/file/attachment/{att_id}")
def dm_file_attachment(request: Request, att_id: int):
    """Отдать файл из dm_message_files (новый мультифайловый маршрут для DM)."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""

    try:
        db = get_web_db(telegram_id, org_db)
        row = db.get_dm_file(att_id)
        if not row or not row[0]:
            return Response(content="Файл не найден", status_code=404)

        fpath, fname, ftype, dm_id = row
        if not os.path.isfile(fpath):
            return Response(content="Файл не найден на диске", status_code=404)

        exp_uploads = os.path.abspath(_uploads_dir_dm(org_db))
        real_fpath = os.path.abspath(fpath)
        if not real_fpath.startswith(exp_uploads):
            return Response(content="Доступ запрещён", status_code=403)

        return FileResponse(fpath, media_type=ftype or "application/octet-stream",
                            filename=fname or os.path.basename(fpath))
    except Exception as exc:
        logger.error(f"dm_file_attachment error: {exc}")
        return Response(content="Ошибка", status_code=500)


@router.post("/chat/dm/send")
async def dm_send(
    request: Request,
    csrf_token: str = Form(default=""),
    to_user_id: int = Form(default=0),
    message: str = Form(default=""),
    files: List[UploadFile] = File(default=[]),
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
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "error": "Недостаточный тариф"}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)

        text = message.strip()[:2000]
        saved_files = await _save_uploaded_files(files, _uploads_dir_dm(org_db))

        if not text and not saved_files:
            return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)

        # Для DM legacy-колонки оставляем пустыми, файлы идут в dm_message_files
        new_id = db.add_dm(user_db_id, to_user_id, text, "", "", "", 0)
        if not new_id:
            return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)

        if saved_files:
            db.add_dm_files(new_id, saved_files)

        # AI hook: если сообщение адресовано AI — запустить ответ асинхронно
        if text and text.lower().lstrip().startswith(('@ии', '/ai', '@ai')):
            asyncio.create_task(_ai_dm_reply(org_db, user_db_id, text, peer_id=to_user_id))

        # Загружаем только что сохранённые файлы, чтобы получить реальные att_id
        # (id вложения, НЕ id сообщения) — нужны и для WS-payload, и для ответа.
        fresh_files = _load_dm_files_bulk(db, [new_id]).get(new_id, []) if saved_files else []

        first_file = fresh_files[0] if fresh_files else {}
        file_name = first_file.get("file_name", "")
        file_type = first_file.get("file_type", "")
        file_size = first_file.get("file_size", 0)

        # Список файлов для WS-payload — с корректными att_id и file_url
        ws_files = [
            {
                "id": f.get("id", 0),
                "file_name": f.get("file_name", ""), "file_type": f.get("file_type", ""),
                "file_size": f.get("file_size", 0),
                "is_image": (f.get("file_type") or "").startswith("image/"),
                "file_url": f"/chat/dm/file/attachment/{f.get('id', 0)}",
            }
            for f in fresh_files
        ]
        # file_url первого файла для превью (правильный att_id вложения)
        first_url = ws_files[0]["file_url"] if ws_files else ""

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        payload = {
            "type": "message",
            "id": new_id,
            "from_user_id": user_db_id,
            "to_user_id": to_user_id,
            "message": text,
            "file_name": file_name,
            "file_type": file_type,
            "file_size": file_size,
            "has_file": bool(fresh_files),
            "is_image": file_type.startswith("image/") if file_type else False,
            "file_url": first_url,
            "files": ws_files,
            "created_at": now_str,
            "is_read": False,
        }
        await dm_manager.send_to_user(org_db, to_user_id, payload)
        try:
            first_name = user.get("name", "Кто-то")
            preview = text or (f"📎 {file_name}" if file_name else "")
            dm_msg = f"💬 {first_name}: {preview[:80]}"
            db.add_notification_to_history(
                user_id=to_user_id,
                notification_type="dm",
                message=dm_msg,
            )
            # Web Push для DM
            try:
                conn = db.get_connection()
                _row = conn.execute("SELECT telegram_id FROM users WHERE id=?", (to_user_id,)).fetchone()
                conn.close()
                if _row and _row[0]:
                    from web.push_utils import apush
                    await apush(int(_row[0]), "💬 Новое сообщение", dm_msg, "/chat/dm")
            except Exception:
                pass
        except Exception:
            pass

        # fresh_files уже загружены выше (att_id) — переиспользуем для ответа
        msg = _fmt_dm(
            (new_id, user_db_id, to_user_id, text, "", "",
             "", 0, now_str, 0,
             user.get("name", ""), "", ""),
            my_db_id=user_db_id,
            files=fresh_files,
        )
        return JSONResponse({"ok": True, "message": msg})

    except Exception as exc:
        logger.error(f"dm_send error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


# ── Delete DM ─────────────────────────────────────────────────────────────────

@router.post("/chat/dm/{msg_id}/delete")
async def dm_delete(
    request: Request,
    msg_id: int,
    csrf_token: str = Form(default=""),
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
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        row = db.get_dm_message(msg_id)
        ok = db.soft_delete_dm(msg_id, user_db_id, is_admin)
        if ok:
            exp_uploads = os.path.abspath(_uploads_dir_dm(org_db))
            # Legacy single-file
            if row and row[4]:
                fpath = row[4]
                real_fpath = os.path.abspath(fpath)
                if real_fpath.startswith(exp_uploads) and os.path.isfile(real_fpath):
                    try:
                        os.remove(real_fpath)
                    except OSError as e:
                        logger.warning(f"dm_delete legacy file: {e}")
            # Новые multi-file вложения
            for fpath in db.delete_dm_files(msg_id):
                real_fpath = os.path.abspath(fpath)
                if real_fpath.startswith(exp_uploads) and os.path.isfile(real_fpath):
                    try:
                        os.remove(real_fpath)
                    except OSError as e:
                        logger.warning(f"dm_delete new file: {e}")
            # Live-удаление у обоих участников переписки (row: id,from,to,...)
            if row:
                payload = {"type": "delete", "id": msg_id}
                for uid in {row[1], row[2]}:
                    if uid:
                        try:
                            await dm_manager.send_to_user(org_db, uid, payload)
                        except Exception:
                            pass
        return JSONResponse({"ok": ok, "error": None if ok else "Нет доступа или сообщение не найдено"})
    except Exception as exc:
        logger.error(f"dm_delete error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


# ── Mark DM read (HTTP fallback) ────────────────────────────────────────────────

@router.post("/chat/dm/{peer_id}/read")
async def dm_mark_read(
    request: Request,
    peer_id: int,
    csrf_token: str = Form(default=""),
):
    """Надёжная серверная отметка диалога прочитанным (фолбэк к WS).

    Помечает все входящие от peer_id прочитанными и шлёт собеседнику
    WS-квитанцию 'read', даже если у клиента WS в этот момент не открыт.
    """
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
    try:
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if not user_db_id or not peer_id:
            return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)
        db.mark_dm_read(user_db_id, peer_id)
        try:
            await dm_manager.send_to_user(org_db, peer_id, {
                "type": "read",
                "by_user_id": user_db_id,
            })
        except Exception:
            pass
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error(f"dm_mark_read error: {exc}")
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

    if not _chat_access_ok(telegram_id):
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
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                client_id = data.get("client_id")
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
                await websocket.send_json({**payload, "confirmed": True, "client_id": client_id})
                await dm_manager.send_to_user(org_db, to_id, payload)
                # AI hook: если сообщение адресовано AI — запустить ответ асинхронно
                if text and text.lower().lstrip().startswith(('@ии', '/ai', '@ai')):
                    asyncio.create_task(_ai_dm_reply(org_db, user_db_id, text, peer_id=to_id))
                try:
                    first_name = user.get("name", "Кто-то")
                    _dm_msg = f"💬 {first_name}: {text[:80]}"
                    db.add_notification_to_history(
                        user_id=to_id,
                        notification_type="dm",
                        message=_dm_msg,
                    )
                    try:
                        conn = db.get_connection()
                        _row = conn.execute("SELECT telegram_id FROM users WHERE id=?", (to_id,)).fetchone()
                        conn.close()
                        if _row and _row[0]:
                            from web.push_utils import apush
                            await apush(int(_row[0]), "💬 Новое сообщение", _dm_msg, "/chat/dm")
                    except Exception:
                        pass
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
