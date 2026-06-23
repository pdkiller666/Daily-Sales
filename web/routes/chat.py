import asyncio
import logging
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta

from typing import List

from fastapi import APIRouter, Form, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse, Response, FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ на файл
MAX_FILES_PER_MSG = 10            # до 10 файлов в одном сообщении

_AI_ERROR_PREFIX = "[AI_ERROR]"  # stripped at display time; triggers error bubble CSS


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
_POLL_RATE_STORE:   dict[int, list[float]] = {}   # 60 req/min per telegram_id
_TOPIC_RATE_STORE:  dict[int, list[float]] = {}   # 5 topics/hour per telegram_id
_SEARCH_RATE_STORE: dict[int, list[float]] = {}   # 30 search req/min per telegram_id


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


_REACT_RATE_STORE: dict[int, list[float]] = {}   # 60 реакций/мин на telegram_id

def _send_rate_ok(tid: int)   -> bool: return _rate_ok(_SEND_RATE_STORE,   tid, 30, 60.0)
def _poll_rate_ok(key)        -> bool: return _rate_ok(_POLL_RATE_STORE,   key, 60, 60.0)
def _topic_rate_ok(tid: int)  -> bool: return _rate_ok(_TOPIC_RATE_STORE,  tid,  5, 3600.0)
def _search_rate_ok(key)      -> bool: return _rate_ok(_SEARCH_RATE_STORE, key, 30, 60.0)
def _react_rate_ok(tid: int)  -> bool: return _rate_ok(_REACT_RATE_STORE,  tid, 60, 60.0)

# Разрешённый набор эмодзи-реакций (валидация на сервере)
ALLOWED_REACTIONS = ("👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "✅")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_user_db_id(db, telegram_id: int) -> int | None:
    try:
        conn = db.get_connection()
        try:
            row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        finally:
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


def _reply_snippet(p: dict) -> str:
    """Короткая выжимка текста родителя для цитаты (escape делает фронт через x-text)."""
    msg = (p.get("message") or "").strip()
    if msg:
        return msg[:90]
    if p.get("file_name"):
        return f"📎 {p['file_name']}"
    return "📎 Вложение"


def _resolve_reply(reply_to_id, reply_map: dict):
    """Построить объект цитаты для сообщения. None — не ответ.
    Удалённый/отсутствующий родитель → {deleted:True, snippet:'сообщение удалено'}."""
    if not reply_to_id:
        return None
    p = reply_map.get(reply_to_id)
    if not p or p.get("is_deleted"):
        return {"id": int(reply_to_id), "author": "", "snippet": "сообщение удалено", "deleted": True}
    return {"id": p["id"], "author": p["author"], "snippet": _reply_snippet(p), "deleted": False}


def _chat_reply_map(db, rows) -> dict:
    """Превью родителей для списка строк chat_messages (reply_to_id в row[-1])."""
    try:
        return db.get_chat_reply_previews([r[-1] for r in rows])
    except Exception:
        return {}


def _dm_reply_map(db, rows) -> dict:
    """Превью родителей для списка строк direct_messages (reply_to_id в row[-1])."""
    try:
        return db.get_dm_reply_previews([r[-1] for r in rows])
    except Exception:
        return {}


def _mention_handle(first_name, username) -> str:
    """Хендл участника для @упоминаний: username (без @) либо имя без пробелов, lower."""
    if username:
        return str(username).strip().lstrip("@").lower()
    if first_name:
        return "".join(str(first_name).split()).lower()
    return ""


_MENTION_RE = re.compile(r"(?:^|\s)@([\w\u0400-\u04ff]+)", re.UNICODE)


def _parse_mention_tids(text, members, exclude_tid=None) -> set:
    """text → set telegram_id упомянутых участников.
    members: rows (id, telegram_id, first_name, username)."""
    if not text or "@" not in text:
        return set()
    tokens = {t.lower() for t in _MENTION_RE.findall(text)}
    if not tokens:
        return set()
    out = set()
    for uid, tid, fn, uname in members:
        if not tid:
            continue
        h = _mention_handle(fn, uname)
        if h and h in tokens and int(tid) != int(exclude_tid or 0):
            out.add(int(tid))
    return out


def _fmt_msg(row, my_db_id: int = 0, is_admin: bool = False, files=None, reactions=None, reply=None, edited=None, pinned=None, forwarded=None) -> dict:
    """Форматировать строку chat_messages.
    files=None  → использовать legacy-колонки file_path/file_name/... из row
    files=[]    → новое сообщение без вложений
    files=[...] → список dicts из chat_message_files
    reactions   → список [{emoji,count,mine}] или None
    reply       → объект цитаты {id,author,snippet,deleted} или None
    edited      → значение edited_at (truthy → метка «изм.»)
    forwarded   → имя первоисточника (truthy → «Переслано от …»)
    """
    mid, user_id, message, file_path, file_name, file_type, file_size, created_at, fn, ln, uname, *_ = row
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
    raw_message = message or ""
    is_ai_error = raw_message.startswith(_AI_ERROR_PREFIX)
    if is_ai_error:
        raw_message = raw_message[len(_AI_ERROR_PREFIX):]
    return {
        "id": mid,
        "user_id": user_id,
        "is_ai": user_id == 0,
        "is_ai_error": is_ai_error,
        "message": raw_message,
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
        "can_edit": (user_id != 0 and my_db_id > 0 and user_id == my_db_id),
        "can_pin": is_admin or (my_db_id > 0 and user_id == my_db_id),
        "is_pinned": bool(pinned),
        "edited": bool(edited),
        "forwarded_from": forwarded or "",
        "reactions": reactions or [],
        "reply": reply,
    }


def _topic_pinned_bar(db, topic_id: int, my_db_id: int = 0, is_admin: bool = False):
    """Последнее закреплённое сообщение темы для бара вверху (или None)."""
    try:
        rows = db.get_pinned_chat_messages(topic_id, limit=1)
        if not rows:
            return None
        r = rows[0]
        files = _load_msg_files_bulk(db, [r[0]]).get(r[0])
        return _fmt_msg(r, my_db_id=my_db_id, is_admin=is_admin,
                        files=files, edited=r[-2], pinned=r[-3], forwarded=r[-4])
    except Exception as e:
        logger.error("pinned bar topic=%s: %s", topic_id, e)
        return None


def _load_msg_files_bulk(db, message_ids: list) -> dict:
    """Батч-загрузка файлов для списка сообщений. Возвращает {msg_id: [file_dicts]}."""
    try:
        return db.get_chat_message_files_bulk(message_ids)
    except Exception:
        return {}


def _get_ai_chat_daily_limit() -> int:
    try:
        from web.rate_store import get_ai_chat_daily_limit
        return get_ai_chat_daily_limit()
    except Exception:
        return 50

# Виртуальный собеседник «AI-ассистент» в личных сообщениях. Используется как
# peer_id в маршрутах/фронтенде. Значение -1 (а не 0) — потому что во фронтенде
# dmPeerId=0 означает «диалог не выбран». В хранилище AI-тред кодируется иначе:
# запрос пользователя to_user_id=0, ответ AI from_user_id=0+ai_peer_id=0.
AI_PEER_ID = -1


_HISTORY_MAX_CHARS    = 5_000  # ~1 600 токенов — держим контекст без расточительства
_SESSION_COMPRESS_AT  = 12    # триггер авто-сжатия: N сообщений в текущей сессии
_SESSION_KEEP_FRESH   = 6     # столько свежих сообщений оставляем «за бортом» сжатия


def _rows_to_history(rows: list, uid_col: int, text_col: int,
                     fname_col: int | None = None,
                     lname_col: int | None = None) -> list[dict]:
    """Конвертирует строки БД (ASC) в список сообщений для LLM.

    Оставляет самые свежие сообщения, укладывающиеся в _HISTORY_MAX_CHARS.
    user_id == 0  → role='assistant' (AI), прочие → role='user'.
    Для многопользовательских тем fname_col/lname_col добавляют «Имя: » перед текстом.
    """
    tail: list[dict] = []
    total = 0
    for row in reversed(rows):
        uid  = row[uid_col]
        text = (row[text_col] or "").strip()
        if not text:
            continue
        if uid == 0:
            role = "assistant"
        else:
            role = "user"
            if fname_col is not None:
                fname = row[fname_col] or ""
                lname = (row[lname_col] or "") if lname_col is not None else ""
                name  = f"{fname} {lname}".strip()
                if name:
                    text = f"{name}: {text}"
        total += len(text)
        if total > _HISTORY_MAX_CHARS:
            break
        tail.append({"role": role, "content": text})
    tail.reverse()
    return tail


def _ai_ext_ok(db, telegram_id: int) -> bool:
    """Доступен ли AI-ассистент: оплачено ли расширение ai_chat_assistant у владельца."""
    try:
        from billing_utils import has_extension
        owner_tg_id = db.get_org_owner_tg_id() or telegram_id
        return bool(has_extension(owner_tg_id, 'ai_chat_assistant'))
    except Exception:
        return False


async def _build_ai_system_prompt(db, user_db_id: int) -> tuple[str, str]:
    """Собирает системный промпт для AI-ассистента с инструментами.

    Возвращает (system_prompt, user_name).
    Базовый контекст (сегодня/месяц/планы) включается сразу — без вызова инструментов.
    Детальные данные (остатки, зарплата, задачи, рейтинги и т.д.) AI запрашивает
    через инструменты по мере необходимости.
    """
    import asyncio
    import anyio

    # Все 5 DB-вызовов — read-only, запускаем параллельно через asyncio.gather.
    # SQLite thread-safe (check_same_thread=False + threading.local pool в deps.py).
    results = await asyncio.gather(
        anyio.to_thread.run_sync(db.get_sales_summary_today),
        anyio.to_thread.run_sync(db.get_sales_summary_month),
        anyio.to_thread.run_sync(lambda: db.get_user_by_id(user_db_id)),
        anyio.to_thread.run_sync(db.get_org_name),
        anyio.to_thread.run_sync(db.get_plans_with_progress),
        return_exceptions=True,
    )
    sales_today    = results[0] if not isinstance(results[0], Exception) else ""
    sales_month    = results[1] if not isinstance(results[1], Exception) else ""
    user_row       = results[2] if not isinstance(results[2], Exception) else None
    org_name       = results[3] if not isinstance(results[3], Exception) else ""
    plans_progress = results[4] if not isinstance(results[4], Exception) else []

    user_name = "сотрудник"
    if user_row:
        fn = user_row[3] if len(user_row) > 3 else ""
        ln = user_row[4] if len(user_row) > 4 else ""
        user_name = f"{fn or ''} {ln or ''}".strip() or "сотрудник"

    plans_text = ""
    if plans_progress:
        lines = []
        for p in plans_progress[:5]:
            metric_unit = "руб." if "выручка" in p["label"] else "шт."
            lines.append(
                f"  - {p['label']}: {p['current']:,.0f} / {p['target']:,.0f} {metric_unit} ({p['pct']}%)"
            )
        plans_text = "\nПланы продаж (прогресс):\n" + "\n".join(lines)

    # Текущая дата для модели. Amvera = UTC; инструменты дефолтятся на
    # date.today() (тоже UTC), поэтому «сегодня» здесь совпадает с тем,
    # какой период подставят инструменты при отсутствии явного параметра.
    _RU_MONTHS = (
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    _today = datetime.now(timezone.utc).date()
    today_text = f"{_today.day} {_RU_MONTHS[_today.month]} {_today.year} года"

    # Рассчитываем опорные диапазоны дат для подсказки AI
    _monday     = _today - timedelta(days=_today.weekday())
    _lw_end     = _monday - timedelta(days=1)
    _lw_start   = _monday - timedelta(days=7)
    _prev_first = (_today.replace(day=1) - timedelta(days=1)).replace(day=1)
    _prev_last  = _today.replace(day=1) - timedelta(days=1)

    system = (
        f"Ты — AI-ассистент торговой организации «{org_name}». "
        f"Ты опытный аналитик розничных продаж и деловой советник.\n"
        f"Сегодня — {today_text}. "
        f"Числовые ориентиры для расчёта дат:\n"
        f"  • Сегодня: {_today.isoformat()}\n"
        f"  • Эта неделя (пн–сегодня): {_monday.isoformat()} — {_today.isoformat()}\n"
        f"  • Прошлая неделя (пн–вс): {_lw_start.isoformat()} — {_lw_end.isoformat()}\n"
        f"  • Прошлый месяц: {_prev_first.isoformat()} — {_prev_last.isoformat()}\n"
        f"Отвечай по-русски, кратко и по делу. Без markdown-разметки, без заголовков.\n"
        f"Опирайся только на данные из контекста и инструментов — не придумывай числа. "
        f"Для любого исторического или периодического вопроса сначала вызови нужный инструмент, "
        f"и только потом отвечай. Фраза «у меня нет данных» допустима лишь когда инструмент "
        f"вернул пустой результат.\n"
        f"Снимок данных организации (актуально на момент запроса):\n"
        f"  - Продажи сегодня: {sales_today}\n"
        f"  - Продажи за текущий месяц: {sales_month}"
        f"{plans_text}\n"
        f"Обращается: {user_name}."
    )
    return system, user_name


async def _ai_chat_reply(org_db: str, topic_id: int, user_db_id: int, user_text: str):
    """Асинхронно формирует и сохраняет ответ AI-ассистента в топик чата.

    Запускается через asyncio.create_task — основной /chat/send не ждёт.
    Любой сбой глотается: AI-ошибка никогда не роняет основной чат.
    Использует ask_llm_with_tools — AI сам запрашивает нужные данные через инструменты.
    """
    try:
        from billing_utils import has_extension
        from web.ai_utils import ask_llm_with_tools
        from web.deps import get_web_db
        from web.rate_store import check_and_increment_ai_for_org
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
        _chat_lim = _get_ai_chat_daily_limit()
        if not check_and_increment_ai_for_org(org_db, _chat_lim):
            await _post_status(
                f"{_AI_ERROR_PREFIX}Дневной лимит AI-запросов исчерпан "
                f"({_chat_lim}/день). Попробуйте завтра."
            )
            return

        system, _user_name = await _build_ai_system_prompt(db, user_db_id)

        # История диалога в теме с учётом сессионных разрывов и резюме
        history, last_break_id = await _fetch_ai_chat_history(db, topic_id, anyio)

        try:
            answer = await ask_llm_with_tools(
                user_text, system, db, max_rounds=5, max_tokens=600, history=history
            )
        except Exception:
            answer = None
        if not answer:
            await _post_status(f"{_AI_ERROR_PREFIX}AI-ассистент временно недоступен, попробуйте позже.")
            return

        await anyio.to_thread.run_sync(
            lambda: db.add_chat_message(user_id=0, message=answer, topic_id=topic_id)
        )

        # Авто-сжатие сессии (не блокирует ответ — запускаем задачей)
        asyncio.create_task(
            _maybe_compress_chat_session(db, topic_id, last_break_id, anyio, owner_tg_id)
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
        from web.ai_utils import ask_llm_with_tools
        from web.deps import get_web_db
        from web.rate_store import check_and_increment_ai_for_org
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
            _is_err = text.startswith(_AI_ERROR_PREFIX)
            _display_text = text[len(_AI_ERROR_PREFIX):] if _is_err else text
            await dm_manager.send_to_user(org_db, sender_db_id, {
                "type": "message",
                "id": new_id,
                "from_user_id": 0,
                "to_user_id": sender_db_id,
                "ai_peer_id": peer_id,
                "message": _display_text,
                "has_file": False,
                "created_at": now_str,
                "is_read": False,
                "is_ai": True,
                "is_ai_error": _is_err,
            })

        owner_tg_id = await anyio.to_thread.run_sync(db.get_org_owner_tg_id)
        if not owner_tg_id:
            return
        if not has_extension(owner_tg_id, 'ai_chat_assistant'):
            return
        _chat_lim = _get_ai_chat_daily_limit()
        if not check_and_increment_ai_for_org(org_db, _chat_lim):
            await _post_ai_dm(
                f"{_AI_ERROR_PREFIX}Дневной лимит AI-запросов исчерпан "
                f"({_chat_lim}/день). Попробуйте завтра."
            )
            return

        system, _user_name = await _build_ai_system_prompt(db, sender_db_id)

        # История личного диалога с AI с учётом сессионных разрывов и резюме
        history, last_break_id = await _fetch_ai_dm_history(db, sender_db_id, anyio)

        try:
            answer = await ask_llm_with_tools(
                user_text, system, db, max_rounds=5, max_tokens=600, history=history
            )
        except Exception:
            answer = None
        if not answer:
            await _post_ai_dm(f"{_AI_ERROR_PREFIX}AI-ассистент временно недоступен, попробуйте позже.")
            return

        await _post_ai_dm(answer)

        # Авто-сжатие сессии (не блокирует ответ — запускаем задачей)
        asyncio.create_task(
            _maybe_compress_dm_session(db, sender_db_id, last_break_id, anyio, owner_tg_id)
        )
    except Exception:
        pass


def _load_dm_files_bulk(db, dm_ids: list) -> dict:
    """Батч-загрузка файлов для списка DM-сообщений. Возвращает {dm_id: [file_dicts]}."""
    try:
        return db.get_dm_files_bulk(dm_ids)
    except Exception:
        return {}


# ── AI Session Helpers ─────────────────────────────────────────────────────────

async def _fetch_ai_dm_history(db, user_db_id: int, anyio) -> tuple[list[dict], int]:
    """Загружает историю AI DM с учётом сессионных разрывов и резюме.

    Возвращает (history, last_break_id).
    """
    last_break_id = await anyio.to_thread.run_sync(
        lambda: db.get_last_ai_dm_session_break_id(user_db_id)
    )
    summary_text = await anyio.to_thread.run_sync(
        lambda: db.get_ai_session_text_dm(user_db_id, since_id=last_break_id)
    )
    _hist_rows = await anyio.to_thread.run_sync(
        lambda: db.get_ai_dm_conversation(user_db_id, limit=21, since_id=last_break_id)
    )
    history: list[dict] = []
    if summary_text:
        history.append({"role": "user",
                        "content": f"[Краткое резюме предыдущей части этого разговора]: {summary_text}"})
        history.append({"role": "assistant",
                        "content": "Понял, продолжу с учётом этого контекста."})
    history.extend(_rows_to_history(
        _hist_rows[:-1] if _hist_rows else [],
        uid_col=1, text_col=3,
    ))
    return history, last_break_id


async def _fetch_ai_chat_history(db, topic_id: int, anyio) -> tuple[list[dict], int]:
    """Загружает историю AI chat-темы с учётом сессионных разрывов и резюме.

    Возвращает (history, last_break_id).
    """
    last_break_id = await anyio.to_thread.run_sync(
        lambda: db.get_last_ai_chat_session_break_id(topic_id)
    )
    summary_text = await anyio.to_thread.run_sync(
        lambda: db.get_ai_session_text_chat(topic_id, since_id=last_break_id)
    )
    _hist_rows = await anyio.to_thread.run_sync(
        lambda: db.get_chat_messages(limit=21, topic_id=topic_id, since_id=last_break_id)
    )
    history: list[dict] = []
    if summary_text:
        history.append({"role": "user",
                        "content": f"[Краткое резюме предыдущей части разговора в этой теме]: {summary_text}"})
        history.append({"role": "assistant",
                        "content": "Понял, продолжу с учётом контекста."})
    history.extend(_rows_to_history(
        _hist_rows[:-1] if _hist_rows else [],
        uid_col=1, text_col=2, fname_col=8, lname_col=9,
    ))
    return history, last_break_id


async def _maybe_compress_dm_session(db, user_db_id: int,
                                     last_break_id: int, anyio,
                                     owner_tg_id: int = 0) -> None:
    """Авто-сжатие AI DM сессии при превышении порога.

    Вызывается ПОСЛЕ сохранения ответа AI — основной поток не ждёт.
    Если резюме уже есть или сообщений мало — нет-оп.
    Не списывает дневной лимит пользователя — это внутренняя системная операция.
    """
    try:
        from web.ai_utils import ask_llm
        count = await anyio.to_thread.run_sync(
            lambda: db.count_ai_dm_session_msgs(user_db_id, since_id=last_break_id)
        )
        if count <= _SESSION_COMPRESS_AT:
            return
        existing = await anyio.to_thread.run_sync(
            lambda: db.get_ai_session_text_dm(user_db_id, since_id=last_break_id)
        )
        if existing:
            return
        all_rows = await anyio.to_thread.run_sync(
            lambda: db.get_ai_dm_conversation(user_db_id, limit=count, since_id=last_break_id)
        )
        to_compress = all_rows[:-_SESSION_KEEP_FRESH]
        if len(to_compress) < 4:
            return
        dialog_text = "\n".join(
            f"{'AI' if r[1] == 0 else 'Пользователь'}: {(r[3] or '').strip()}"
            for r in to_compress if (r[3] or '').strip()
        )
        if not dialog_text:
            return
        system_compress = (
            "Ты — система сжатия контекста для AI-ассистента. "
            "Кратко и точно перескажи суть диалога в 3–4 предложениях, "
            "сохраняя ключевые факты, числа и решения. Не добавляй ничего от себя."
        )
        summary = await ask_llm(
            f"Сожми следующий диалог:\n\n{dialog_text}",
            system=system_compress,
            max_tokens=300,
            feature="summary",
        )
        if summary:
            await anyio.to_thread.run_sync(
                lambda: db.add_ai_session_summary_dm(user_db_id, summary)
            )
    except Exception as _e:
        logger.error("_maybe_compress_dm_session: %s", _e)


async def _maybe_compress_chat_session(db, topic_id: int,
                                       last_break_id: int, anyio,
                                       owner_tg_id: int = 0) -> None:
    """Авто-сжатие AI chat-темы при превышении порога.

    Не списывает дневной лимит пользователя — это внутренняя системная операция.
    """
    try:
        from web.ai_utils import ask_llm
        count = await anyio.to_thread.run_sync(
            lambda: db.count_ai_chat_session_msgs(topic_id, since_id=last_break_id)
        )
        if count <= _SESSION_COMPRESS_AT:
            return
        existing = await anyio.to_thread.run_sync(
            lambda: db.get_ai_session_text_chat(topic_id, since_id=last_break_id)
        )
        if existing:
            return
        all_rows = await anyio.to_thread.run_sync(
            lambda: db.get_chat_messages(limit=count, topic_id=topic_id, since_id=last_break_id)
        )
        to_compress = all_rows[:-_SESSION_KEEP_FRESH]
        if len(to_compress) < 4:
            return
        dialog_text = "\n".join(
            f"{'AI' if r[1] == 0 else (r[8] or 'Сотрудник')}: {(r[2] or '').strip()}"
            for r in to_compress if (r[2] or '').strip()
        )
        if not dialog_text:
            return
        system_compress = (
            "Ты — система сжатия контекста. "
            "Кратко перескажи суть командного диалога в 3–4 предложениях."
        )
        summary = await ask_llm(
            f"Сожми:\n\n{dialog_text}",
            system=system_compress,
            max_tokens=300,
            feature="summary",
        )
        if summary:
            await anyio.to_thread.run_sync(
                lambda: db.add_ai_session_summary_chat(topic_id, summary)
            )
    except Exception as _e:
        logger.error("_maybe_compress_chat_session: %s", _e)


def _fmt_topic(row) -> dict:
    tid, name, created_by, created_at, sort_order, msg_count = row[:6]
    is_ai = bool(row[6]) if len(row) > 6 else False
    return {
        "id": tid,
        "name": name,
        "created_by": created_by,
        "msg_count": msg_count or 0,
        "is_ai": is_ai,
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
        "pinned": None,
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

            # AI-ассистент: оплачено расширение → гарантируем выделенную тему
            ai_topic_id = None
            try:
                if _ai_ext_ok(db, telegram_id):
                    ctx["ai_chat_enabled"] = True
                    ai_topic_id = db.ensure_ai_topic()
            except Exception:
                pass
            ctx["ai_topic_id"] = ai_topic_id

            raw_topics = db.get_chat_topics()
            topics = [_fmt_topic(r) for r in raw_topics]
            if not topics:
                topics = [{"id": 1, "name": "Общий", "created_by": None, "msg_count": 0, "is_ai": False}]

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
            _ai_since = 0
            try:
                if db.get_ai_topic_id() == topic:
                    _ai_since = db.get_last_ai_chat_session_break_id(topic)
            except Exception:
                _ai_since = 0
            rows = db.get_chat_messages(limit=50, topic_id=topic, since_id=_ai_since)
            _react_map = db.get_chat_reactions_bulk([r[0] for r in rows], user_db_id or 0)
            _reply_map = _chat_reply_map(db, rows)
            ctx["messages"] = [_fmt_msg(r, my_db_id=user_db_id or 0, is_admin=is_admin, reactions=_react_map.get(r[0]), reply=_resolve_reply(r[-1], _reply_map), edited=r[-2], pinned=r[-3], forwarded=r[-4]) for r in rows]
            ctx["latest_id"] = db.get_chat_latest_id(topic_id=topic)
            ctx["pinned"] = _topic_pinned_bar(db, topic, user_db_id or 0, is_admin)

            # Текущая тема открыта → помечаем прочитанной + обнуляем её бейдж
            try:
                db.set_chat_read(user_db_id or 0, topic, ctx["latest_id"])
                for t in topics:
                    if t["id"] == topic:
                        t["unread"] = 0
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
    reply_to_id: int = Form(default=0),
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

        # Валидация цитаты: родитель существует, не удалён и в ТОЙ ЖЕ теме
        valid_reply = 0
        if reply_to_id:
            try:
                _pm = db.get_chat_reply_previews([reply_to_id]).get(reply_to_id)
                if _pm and not _pm.get("is_deleted") and int(_pm.get("topic_id") or 0) == int(topic_id):
                    valid_reply = int(reply_to_id)
            except Exception:
                valid_reply = 0

        new_id = db.add_chat_message(user_id=user_db_id, message=text, topic_id=topic_id, reply_to_id=valid_reply)
        if saved_files:
            db.add_chat_message_files(new_id, saved_files)

        # AI hook: в выделенной AI-теме любое текстовое сообщение → ответ AI.
        # В обычных темах AI больше не вмешивается (хук @ии убран).
        if text:
            try:
                _ai_tid = db.get_ai_topic_id()
            except Exception:
                _ai_tid = None
            if _ai_tid and topic_id == _ai_tid:
                asyncio.create_task(_ai_chat_reply(org_db, topic_id, user_db_id, text))

        # Web Push участникам организации (кроме отправителя) — общий чат.
        # Упомянутые (@имя) получают адресное уведомление вместо общего.
        try:
            sender_name = user.get("name") or "Сотрудник"
            preview = (text or ("📎 Вложение" if saved_files else "")).strip()[:120]
            member_tids = [
                int(u[1]) for u in (db.get_all_users() or [])
                if u[1] and int(u[1]) != telegram_id
            ]
            mention_tids = set()
            try:
                if text:
                    mention_tids = _parse_mention_tids(
                        text, db.get_chat_mention_members(), exclude_tid=telegram_id
                    )
            except Exception:
                mention_tids = set()
            if member_tids and preview:
                from web.push_utils import apush_bulk
                general_tids = [t for t in member_tids if t not in mention_tids]
                if general_tids:
                    await apush_bulk(general_tids, "💬 Новое сообщение в чате", f"{sender_name}: {preview}", "/chat")
            if mention_tids:
                from web.push_utils import apush_bulk
                await apush_bulk(list(mention_tids), "📣 Вас упомянули в чате", f"{sender_name}: {preview}", "/chat")
        except Exception:
            pass

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        new_msgs = db.get_chat_messages_since(new_id - 1, topic_id=topic_id)
        msg_ids = [r[0] for r in new_msgs]
        files_map = _load_msg_files_bulk(db, msg_ids)
        reply_map = _chat_reply_map(db, new_msgs)
        result = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin, files=files_map.get(r[0]), reply=_resolve_reply(r[-1], reply_map), edited=r[-2], pinned=r[-3], forwarded=r[-4]) for r in new_msgs]

        # Live-доставка в тему: будим клиентов в этой теме (кроме отправителя)
        # сразу опросить сервер, без ожидания 4-сек поллинга. Poll — фоллбэк.
        try:
            from web.ws_manager import topic_manager
            await topic_manager.broadcast(
                org_db, topic_id,
                {"type": "new", "topic_id": topic_id, "latest_id": new_id},
                exclude_user=user_db_id,
            )
        except Exception:
            pass

        return JSONResponse({"ok": True, "messages": result, "latest_id": new_id})

    except Exception as exc:
        logger.error(f"chat_send error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


def _copy_files_for_forward(src_files: list, uploads_dir: str) -> list:
    """Физически копирует вложения источника в каталог назначения и возвращает
    список dict'ов для add_*_files (file_path/file_name/file_type/file_size).

    Копируем файлы, а не переиспользуем пути, чтобы удаление оригинала не
    ломало пересланную копию (общий путь → битая ссылка)."""
    import shutil
    out = []
    if not src_files:
        return out
    month_dir = datetime.now(timezone.utc).strftime("%Y-%m")
    month_path = os.path.join(uploads_dir, month_dir)
    try:
        os.makedirs(month_path, exist_ok=True)
    except OSError as e:
        logger.warning("forward mkdir: %s", e)
        return out
    for f in src_files[:MAX_FILES_PER_MSG]:
        sp = f.get("file_path") or ""
        if not sp or not os.path.isfile(sp):
            continue
        try:
            safe = _safe_filename(f.get("file_name") or os.path.basename(sp))
            dest = os.path.join(month_path, f"{uuid.uuid4().hex[:12]}_{safe}")
            shutil.copy2(sp, dest)
            out.append({
                "file_path": dest,
                "file_name": f.get("file_name", "") or safe,
                "file_type": f.get("file_type", "") or "",
                "file_size": f.get("file_size", 0) or 0,
            })
        except Exception as e:
            logger.warning("forward copy skip: %s", e)
    return out


@router.post("/chat/forward")
async def chat_forward(
    request: Request,
    csrf_token: str = Form(default=""),
    src_kind: str = Form(default=""),      # "topic" | "dm"
    src_id: int = Form(default=0),
    dst_kind: str = Form(default=""),      # "topic" | "dm"
    dst_topic_id: int = Form(default=0),
    dst_peer_id: int = Form(default=0),
):
    """Переслать сообщение (тему/ЛС → тему/ЛС). Сохраняет первоисточник в
    forwarded_from (цепочка пересылок указывает на оригинального автора),
    физически копирует вложения."""
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

    if src_kind not in ("topic", "dm") or dst_kind not in ("topic", "dm") or not src_id:
        return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)

    try:
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "error": "Недостаточный тариф"}, status_code=403)

        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)

        # ── Загрузка источника ────────────────────────────────────────────────
        if src_kind == "topic":
            src = db.get_chat_message_for_forward(src_id)
            if not src or src[8]:  # is_deleted
                return JSONResponse({"ok": False, "error": "Сообщение недоступно"}, status_code=400)
            src_text = src[2] or ""
            src_forwarded = src[7] or ""
            src_author = src[10] or ""
            src_files = db.get_chat_message_files_bulk([src_id]).get(src_id) or []
            if not src_files and src[3]:  # legacy single-file
                src_files = [{"file_path": src[3], "file_name": src[4],
                              "file_type": src[5], "file_size": src[6]}]
        else:  # dm
            src = db.get_dm_message_for_forward(src_id)
            if not src or src[9]:  # is_deleted
                return JSONResponse({"ok": False, "error": "Сообщение недоступно"}, status_code=400)
            # доступ к источнику ЛС — только участник переписки
            if user_db_id not in (src[1], src[2]):
                return JSONResponse({"ok": False, "error": "Нет доступа к сообщению"}, status_code=403)
            src_text = src[3] or ""
            src_forwarded = src[8] or ""
            src_author = src[10] or ""
            src_files = db.get_dm_files_bulk([src_id]).get(src_id) or []
            if not src_files and src[4]:  # legacy single-file
                src_files = [{"file_path": src[4], "file_name": src[5],
                              "file_type": src[6], "file_size": src[7]}]

        # Цепочка пересылок сохраняет оригинального автора
        fwd_name = (src_forwarded or src_author or "").strip()[:120]
        if not fwd_name:
            return JSONResponse({"ok": False, "error": "Неизвестный автор"}, status_code=400)
        if not src_text and not src_files:
            return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)

        is_admin = user.get("role") in ("owner", "admin", "super_admin")

        # ── Доставка в ТЕМУ ───────────────────────────────────────────────────
        if dst_kind == "topic":
            valid_topics = {r[0] for r in db.get_chat_topics()}
            if not valid_topics:
                valid_topics = {1}
            tid = dst_topic_id if dst_topic_id in valid_topics else 1
            # Копируем вложения ДО вставки: если источник был только файлами, а
            # все копии не удались — не создаём пустое сообщение.
            copied = _copy_files_for_forward(src_files, _uploads_dir(org_db))
            if not src_text and not copied:
                return JSONResponse({"ok": False, "error": "Не удалось переслать вложение"}, status_code=400)
            new_id = db.add_chat_message(user_id=user_db_id, message=src_text,
                                         topic_id=tid, forwarded_from=fwd_name)
            if copied:
                db.add_chat_message_files(new_id, copied)

            new_msgs = db.get_chat_messages_since(new_id - 1, topic_id=tid)
            files_map = _load_msg_files_bulk(db, [r[0] for r in new_msgs])
            reply_map = _chat_reply_map(db, new_msgs)
            result = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin,
                               files=files_map.get(r[0]),
                               reply=_resolve_reply(r[-1], reply_map),
                               edited=r[-2], pinned=r[-3], forwarded=r[-4]) for r in new_msgs]
            try:
                from web.ws_manager import topic_manager
                await topic_manager.broadcast(
                    org_db, tid,
                    {"type": "new", "topic_id": tid, "latest_id": new_id},
                    exclude_user=user_db_id,
                )
            except Exception:
                pass
            return JSONResponse({"ok": True, "messages": result,
                                 "latest_id": new_id, "topic_id": tid})

        # ── Доставка в ЛС ─────────────────────────────────────────────────────
        if dst_peer_id <= 0:
            return JSONResponse({"ok": False, "error": "Получатель не указан"}, status_code=400)

        # Копируем вложения ДО вставки: если источник был только файлами, а все
        # копии не удались — не создаём пустое сообщение.
        copied = _copy_files_for_forward(src_files, _uploads_dir_dm(org_db))
        if not src_text and not copied:
            return JSONResponse({"ok": False, "error": "Не удалось переслать вложение"}, status_code=400)
        new_id = db.add_dm(user_db_id, dst_peer_id, src_text, "", "", "", 0,
                           forwarded_from=fwd_name)
        if not new_id:
            return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)
        if copied:
            db.add_dm_files(new_id, copied)

        fresh_files = _load_dm_files_bulk(db, [new_id]).get(new_id, []) if copied else []
        first_file = fresh_files[0] if fresh_files else {}
        file_name = first_file.get("file_name", "")
        file_type = first_file.get("file_type", "")
        file_size = first_file.get("file_size", 0)
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
        first_url = ws_files[0]["file_url"] if ws_files else ""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        payload = {
            "type": "message",
            "id": new_id,
            "from_user_id": user_db_id,
            "to_user_id": dst_peer_id,
            "message": src_text,
            "file_name": file_name,
            "file_type": file_type,
            "file_size": file_size,
            "has_file": bool(fresh_files),
            "is_image": file_type.startswith("image/") if file_type else False,
            "file_url": first_url,
            "files": ws_files,
            "created_at": now_str,
            "is_read": False,
            "reply": None,
            "forwarded_from": fwd_name,
        }
        try:
            from web.ws_manager import dm_manager
            await dm_manager.send_to_user(org_db, dst_peer_id, payload)
        except Exception:
            pass
        try:
            first_name = user.get("name", "Кто-то")
            preview = src_text or (f"📎 {file_name}" if file_name else "")
            dm_msg = f"↪ {first_name}: {preview[:80]}"
            db.add_notification_to_history(user_id=dst_peer_id, notification_type="dm", message=dm_msg)
            try:
                conn = db.get_connection()
                try:
                    _row = conn.execute("SELECT telegram_id FROM users WHERE id=?", (dst_peer_id,)).fetchone()
                finally:
                    conn.close()
                if _row and _row[0]:
                    from web.push_utils import apush
                    await apush(int(_row[0]), "💬 Новое сообщение", dm_msg, "/chat/dm")
            except Exception:
                pass
        except Exception:
            pass

        msg = _fmt_dm(
            (new_id, user_db_id, dst_peer_id, src_text, "", "", "", 0, now_str, 0,
             user.get("name", ""), "", ""),
            my_db_id=user_db_id, files=fresh_files, forwarded=fwd_name,
        )
        return JSONResponse({"ok": True, "message": msg, "peer_id": dst_peer_id})

    except Exception as exc:
        logger.error(f"chat_forward error: {exc}")
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
        eff_since = since_id
        try:
            if db.get_ai_topic_id() == topic_id:
                eff_since = max(since_id, db.get_last_ai_chat_session_break_id(topic_id))
        except Exception:
            eff_since = since_id
        rows = db.get_chat_messages_since(eff_since, topic_id=topic_id)
        msg_ids = [r[0] for r in rows]
        files_map = _load_msg_files_bulk(db, msg_ids)
        reply_map = _chat_reply_map(db, rows)
        msgs = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin, files=files_map.get(r[0]), reply=_resolve_reply(r[-1], reply_map), edited=r[-2], pinned=r[-3], forwarded=r[-4]) for r in rows]
        latest = msgs[-1]["id"] if msgs else since_id

        # Удаления у всех в реальном времени (с момента прошлого опроса)
        deleted_ids = db.get_chat_deleted_ids_since(topic_id, del_since)

        # Редактирования у всех в реальном времени (тот же курсор-таймстамп)
        try:
            edited_msgs = [
                {"id": e[0], "message": e[1] or ""}
                for e in db.get_chat_edited_since(topic_id, del_since)
            ]
        except Exception:
            edited_msgs = []

        # Реакции последних сообщений темы — live-синхронизация чипов
        try:
            reactions_map = {str(k): v for k, v in
                             db.get_recent_chat_reactions(topic_id, user_db_id).items()}
        except Exception:
            reactions_map = {}

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
            "deleted_ids": deleted_ids, "edited": edited_msgs,
            "topic_unread": topic_unread, "now": now_ts,
            "reactions": reactions_map,
            "pinned": _topic_pinned_bar(db, topic_id, user_db_id, is_admin),
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
        _ai_since = 0
        try:
            if db.get_ai_topic_id() == topic_id:
                _ai_since = db.get_last_ai_chat_session_break_id(topic_id)
        except Exception:
            _ai_since = 0
        rows = db.get_chat_messages(limit=50, topic_id=topic_id, since_id=_ai_since)
        msg_ids = [r[0] for r in rows]
        files_map = _load_msg_files_bulk(db, msg_ids)
        react_map = db.get_chat_reactions_bulk(msg_ids, user_db_id)
        reply_map = _chat_reply_map(db, rows)
        msgs = [_fmt_msg(r, my_db_id=user_db_id, is_admin=is_admin, files=files_map.get(r[0]), reactions=react_map.get(r[0]), reply=_resolve_reply(r[-1], reply_map), edited=r[-2], pinned=r[-3], forwarded=r[-4]) for r in rows]
        latest = db.get_chat_latest_id(topic_id=topic_id)
        try:
            db.set_chat_read(user_db_id, topic_id, latest)
        except Exception:
            pass
        return JSONResponse({
            "ok": True, "messages": msgs, "latest_id": latest,
            "pinned": _topic_pinned_bar(db, topic_id, user_db_id, is_admin),
        })

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
        try:
            row = conn.execute(
                "SELECT file_path, file_name, file_type FROM chat_messages WHERE id = ? AND is_deleted = 0",
                (msg_id,)
            ).fetchone()
        finally:
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
        try:
            msg_row = conn.execute(
                "SELECT file_path FROM chat_messages WHERE id = ? AND is_deleted = 0",
                (msg_id,)
            ).fetchone()
        finally:
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


@router.post("/chat/message/{msg_id}/edit")
def chat_edit_message(
    request: Request,
    msg_id: int,
    message: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Редактировать своё сообщение в групповой теме (только автор)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    text = (message or "").strip()[:2000]
    if not text:
        return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        ok = db.edit_chat_message(msg_id, user_db_id, text)
        return JSONResponse({
            "ok": ok, "message": text if ok else None,
            "error": None if ok else "Нет доступа или сообщение не найдено",
        })
    except Exception as exc:
        logger.error(f"chat_edit error: {exc}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)


@router.post("/chat/message/{msg_id}/pin")
def chat_pin_message(
    request: Request,
    msg_id: int,
    pinned: int = Form(default=1),
    csrf_token: str = Form(default=""),
):
    """Закрепить/открепить сообщение в групповой теме (админ/владелец или автор)."""
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
    if pinned not in (0, 1):
        return JSONResponse({"ok": False, "error": "Некорректный параметр"}, status_code=400)
    want = bool(pinned)
    try:
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id) or 0

        # Автор + тема сообщения для проверки прав и формирования бара
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT user_id, topic_id FROM chat_messages WHERE id = ? AND is_deleted = 0",
                (msg_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return JSONResponse({"ok": False, "error": "Сообщение не найдено"}, status_code=404)
        author_id, topic_id = row[0], row[1]
        if not (is_admin or (user_db_id > 0 and author_id == user_db_id)):
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

        ok = db.set_chat_message_pinned(msg_id, want)
        return JSONResponse({
            "ok": ok,
            "pinned": _topic_pinned_bar(db, topic_id, user_db_id, is_admin),
            "error": None if ok else "Сообщение не найдено",
        })
    except Exception as exc:
        logger.error(f"chat_pin error: {exc}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)


# ── Реакции: групповая тема ──────────────────────────────────────────────────

@router.post("/chat/message/{msg_id}/react")
def chat_react_message(
    request: Request,
    msg_id: int,
    emoji: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)
    emoji = (emoji or "").strip()
    if emoji not in ALLOWED_REACTIONS:
        return JSONResponse({"ok": False, "error": "Недопустимая реакция"}, status_code=400)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    if not _react_rate_ok(telegram_id):
        return JSONResponse({"ok": False, "error": "Слишком часто"}, status_code=429)
    try:
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if not user_db_id:
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        mine = db.toggle_message_reaction(msg_id, user_db_id, emoji)
        reactions = db.get_chat_reactions_bulk([msg_id], user_db_id).get(msg_id, [])
        return JSONResponse({"ok": True, "mine": mine, "reactions": reactions})
    except Exception as exc:
        logger.error(f"chat_react error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


# ── Реакции: личные сообщения (с WS-распространением обоим) ───────────────────

@router.post("/chat/dm/{msg_id}/react")
async def dm_react_message(
    request: Request,
    msg_id: int,
    emoji: str = Form(default=""),
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
    emoji = (emoji or "").strip()
    if emoji not in ALLOWED_REACTIONS:
        return JSONResponse({"ok": False, "error": "Недопустимая реакция"}, status_code=400)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    if not _react_rate_ok(telegram_id):
        return JSONResponse({"ok": False, "error": "Слишком часто"}, status_code=429)
    try:
        db = get_web_db(telegram_id, org_db)
        if not _chat_access_ok(telegram_id):
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        if not user_db_id:
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        row = db.get_dm_message(msg_id)
        if not row:
            return JSONResponse({"ok": False, "error": "Сообщение не найдено"}, status_code=404)
        # Реагировать может только участник переписки (row: id, from, to, ...)
        if user_db_id not in {row[1], row[2]}:
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        mine = db.toggle_dm_reaction(msg_id, user_db_id, emoji)
        my_reactions = db.get_dm_reactions_bulk([msg_id], user_db_id).get(msg_id, [])
        # WS обоим участникам — у каждого свой признак mine
        for uid in {row[1], row[2]}:
            if not uid:
                continue
            uid_reacts = db.get_dm_reactions_bulk([msg_id], uid).get(msg_id, [])
            try:
                await dm_manager.send_to_user(org_db, uid, {
                    "type": "reaction", "id": msg_id, "reactions": uid_reacts,
                })
            except Exception:
                pass
        return JSONResponse({"ok": True, "mine": mine, "reactions": my_reactions})
    except Exception as exc:
        logger.error(f"dm_react error: {exc}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)


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


def _fmt_dm(row, my_db_id: int = 0, files=None, reactions=None, reply=None, edited=None, forwarded=None) -> dict:
    """Форматировать строку direct_messages.
    files=None  → legacy single-file из колонок
    files=[]    → нет вложений
    files=[...] → список dicts из dm_message_files
    reactions   → список [{emoji,count,mine}] или None
    reply       → объект цитаты {id,author,snippet,deleted} или None
    edited      → значение edited_at (truthy → метка «изм.»)
    forwarded   → имя первоисточника (truthy → «Переслано от …»)
    """
    (mid, from_id, to_id, message, file_path, file_name,
     file_type, file_size, created_at, is_read,
     fn, ln, uname, *_) = row
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
    raw_message = message or ""
    is_ai_error = raw_message.startswith(_AI_ERROR_PREFIX)
    if is_ai_error:
        raw_message = raw_message[len(_AI_ERROR_PREFIX):]
    return {
        "id": mid,
        "from_user_id": from_id,
        "to_user_id": to_id,
        "message": raw_message,
        "is_ai_error": is_ai_error,
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
        "can_edit": (is_mine and from_id != 0),
        "edited": bool(edited),
        "forwarded_from": forwarded or "",
        "reactions": reactions or [],
        "reply": reply,
    }


def _fmt_contact(row, my_id: int) -> dict:
    peer_id, fn, ln, uname, photo_url, last_msg, last_from, last_file_name, last_at, unread = row
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
        "photo_url": photo_url or "",
        "last_msg": (preview or "")[:80],
        "last_at": _fmt_ts(last_at),
        "unread": int(unread or 0),
    }


def _build_ai_contact(db, user_db_id: int, telegram_id: int):
    """Закреплённый контакт «AI-ассистент» для списка ЛС.

    Возвращает dict или None, если расширение ai_chat_assistant не оплачено.
    """
    if not _ai_ext_ok(db, telegram_id):
        return None
    try:
        last_msg, last_from, last_at, unread = db.get_ai_dm_summary(user_db_id)
    except Exception:
        last_msg, last_from, last_at, unread = ('', 0, '', 0)
    if last_msg:
        preview = ("🤖 " if last_from == 0 else "Вы: ") + last_msg
    else:
        preview = "Спросите о продажах, планах, товарах"
    return {
        "id": AI_PEER_ID,
        "display_name": "AI-ассистент",
        "initial": "🤖",
        "last_msg": preview[:80],
        "last_at": _fmt_ts(last_at) if last_at else "",
        "unread": int(unread or 0),
        "is_ai": True,
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
                try:
                    peer_row = conn.execute(
                        "SELECT id, first_name, last_name, username, profile_photo FROM users WHERE id = ?",
                        (peer_id,)
                    ).fetchone()
                finally:
                    conn.close()
                if peer_row:
                    pname = f"{peer_row[1] or ''} {peer_row[2] or ''}".strip() or peer_row[3] or f"User#{peer_id}"
                    ctx["peer"] = {"id": peer_id, "display_name": pname, "initial": pname[0].upper(), "photo_url": (peer_row[4] if len(peer_row) > 4 else "") or ""}
                    rows = db.get_dm_conversation(user_db_id, peer_id, limit=50)
                    dm_ids = [r[0] for r in rows]
                    dm_files_map = _load_dm_files_bulk(db, dm_ids)
                    dm_react_map = db.get_dm_reactions_bulk(dm_ids, user_db_id)
                    dm_reply_map = _dm_reply_map(db, rows)
                    ctx["messages"] = [_fmt_dm(r, my_db_id=user_db_id, files=dm_files_map.get(r[0]), reactions=dm_react_map.get(r[0]), reply=_resolve_reply(r[-1], dm_reply_map), edited=r[-2], forwarded=r[-3]) for r in rows]
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
                "mention": _mention_handle(r[1], r[3]),
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
        # AI-ассистент — закреплённый контакт сверху (если расширение оплачено)
        ai_contact = _build_ai_contact(db, user_db_id, telegram_id)
        if ai_contact:
            contacts.insert(0, ai_contact)
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
        if peer_id == AI_PEER_ID:
            if not _ai_ext_ok(db, telegram_id):
                return JSONResponse({"ok": False, "messages": [], "has_more": False}, status_code=403)
            ai_break_id = db.get_last_ai_dm_session_break_id(user_db_id)
            rows = db.get_ai_dm_conversation(user_db_id, limit=51, before_id=before_id, since_id=ai_break_id)
        elif peer_id <= 0:
            # peer_id=0 — технический «AI-маркер» в хранилище, не реальный диалог.
            return JSONResponse({"ok": False, "messages": [], "has_more": False}, status_code=400)
        else:
            rows = db.get_dm_conversation(user_db_id, peer_id, limit=51, before_id=before_id)
        has_more = len(rows) > 50
        page = rows[:50]
        dm_ids = [r[0] for r in page]
        dm_files_map = _load_dm_files_bulk(db, dm_ids)
        dm_react_map = db.get_dm_reactions_bulk(dm_ids, user_db_id)
        dm_reply_map = _dm_reply_map(db, page)
        msgs = [_fmt_dm(r, my_db_id=user_db_id, files=dm_files_map.get(r[0]), reactions=dm_react_map.get(r[0]), reply=_resolve_reply(r[-1], dm_reply_map), edited=r[-2], forwarded=r[-3]) for r in page]
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
    reply_to_id: int = Form(default=0),
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

        # ── AI-ассистент: выделенный личный тред (peer = AI_PEER_ID) ──────────
        if to_user_id == AI_PEER_ID:
            if not _ai_ext_ok(db, telegram_id):
                return JSONResponse({"ok": False, "error": "AI-ассистент недоступен"}, status_code=403)
            text = message.strip()[:2000]
            if not text:
                return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)
            # Запрос пользователя: from=user, to=0 (метка AI-треда)
            new_id = db.add_dm(user_db_id, 0, text)
            if not new_id:
                return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)
            # Ответ AI формируется асинхронно (from=0, to=user, ai_peer_id=0) и
            # доставляется по WS — основной запрос не ждёт.
            asyncio.create_task(_ai_dm_reply(org_db, user_db_id, text, peer_id=0))
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            msg = _fmt_dm(
                (new_id, user_db_id, 0, text, "", "", "", 0, now_str, 0,
                 user.get("name", ""), "", ""),
                my_db_id=user_db_id, files=[],
            )
            return JSONResponse({"ok": True, "message": msg})

        # Прочие неположительные peer (например -2) — невалидны (0 отсечён выше).
        if to_user_id < 0:
            return JSONResponse({"ok": False, "error": "Некорректный получатель"}, status_code=400)

        text = message.strip()[:2000]
        saved_files = await _save_uploaded_files(files, _uploads_dir_dm(org_db))

        if not text and not saved_files:
            return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)

        # Валидация цитаты: родитель существует, не удалён и в этом диалоге
        valid_reply = 0
        reply_obj = None
        if reply_to_id:
            try:
                _pm = db.get_dm_reply_previews([reply_to_id]).get(reply_to_id)
                _pf = int(_pm.get("from_user_id", -999)) if _pm else -999
                _pt = int(_pm.get("to_user_id", -999)) if _pm else -999
                _conv = (user_db_id, to_user_id)
                if _pm and not _pm.get("is_deleted") and _pf in _conv and _pt in _conv:
                    valid_reply = int(reply_to_id)
                    reply_obj = _resolve_reply(valid_reply, {valid_reply: _pm})
            except Exception:
                valid_reply = 0
                reply_obj = None

        # Для DM legacy-колонки оставляем пустыми, файлы идут в dm_message_files
        new_id = db.add_dm(user_db_id, to_user_id, text, "", "", "", 0, reply_to_id=valid_reply)
        if not new_id:
            return JSONResponse({"ok": False, "error": "Ошибка сервера"}, status_code=500)

        if saved_files:
            db.add_dm_files(new_id, saved_files)

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
            "reply": reply_obj,
        }
        try:
            await dm_manager.send_to_user(org_db, to_user_id, payload)
        except Exception:
            pass
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
                try:
                    _row = conn.execute("SELECT telegram_id FROM users WHERE id=?", (to_user_id,)).fetchone()
                finally:
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
            reply=reply_obj,
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


@router.post("/chat/dm/{msg_id}/edit")
async def dm_edit(
    request: Request,
    msg_id: int,
    message: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Редактировать своё личное сообщение (только автор). Live-обновление по WS."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from web.ws_manager import dm_manager

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    text = (message or "").strip()[:2000]
    if not text:
        return JSONResponse({"ok": False, "error": "Пустое сообщение"}, status_code=400)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id) or 0
        row = db.get_dm_message(msg_id)
        ok = db.edit_dm(msg_id, user_db_id, text)
        if ok and row:
            payload = {"type": "edit", "id": msg_id, "message": text}
            for uid in {row[1], row[2]}:
                if uid:
                    try:
                        await dm_manager.send_to_user(org_db, uid, payload)
                    except Exception:
                        pass
        return JSONResponse({
            "ok": ok, "message": text if ok else None,
            "error": None if ok else "Нет доступа или сообщение не найдено",
        })
    except Exception as exc:
        logger.error(f"dm_edit error: {exc}")
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
        if peer_id == AI_PEER_ID:
            if not _ai_ext_ok(db, telegram_id):
                return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
            db.mark_ai_dm_read(user_db_id)
            return JSONResponse({"ok": True})
        if peer_id < 0:
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
                # to_id <= 0 невалиден: 0 = «нет», -1 = AI (только по HTTP), прочие — мусор.
                if to_id <= 0 or not text:
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
                        try:
                            _row = conn.execute("SELECT telegram_id FROM users WHERE id=?", (to_id,)).fetchone()
                        finally:
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


@router.websocket("/ws/topic")
async def ws_topic(websocket: WebSocket):
    """Live-доставка групповых тем: сервер «будит» клиентов комнаты темы
    (broadcast {type:'new'}), клиент сразу делает poll. Само сообщение
    форматируется per-user в HTTP-поллинге; poll — фоллбэк при разрыве WS."""
    from web.auth import decode_session_token, COOKIE_NAME
    from web.deps import get_web_db
    from web.ws_manager import topic_manager

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

    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")
            if mtype == "join":
                try:
                    tid = int(data.get("topic_id", 0))
                except (TypeError, ValueError):
                    tid = 0
                if tid > 0:
                    topic_manager.join(org_db, tid, user_db_id, websocket)
            elif mtype == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("ws_topic error uid=%s: %s", user_db_id, e)
    finally:
        topic_manager.leave(websocket)


# ── AI Session Reset Routes ────────────────────────────────────────────────────

@router.post("/chat/ai/reset")
async def ai_dm_reset_session(
    request: Request,
    csrf_token: str = Form(""),
):
    """Вставить маркер разрыва сессии в личный AI-тред (кнопка «Новый диалог»)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "Invalid CSRF"}, status_code=403)
    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    if not _ai_ext_ok(get_web_db(telegram_id, org_db), telegram_id):
        return JSONResponse({"ok": False, "error": "AI недоступен"}, status_code=403)
    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return JSONResponse({"ok": False}, status_code=400)
        new_id = db.add_ai_dm_session_break(user_db_id)
        return JSONResponse({"ok": bool(new_id)})
    except Exception as e:
        logger.error("ai_dm_reset_session: %s", e)
        return JSONResponse({"ok": False}, status_code=500)


@router.post("/chat/topic/ai/reset")
async def ai_topic_reset_session(
    request: Request,
    csrf_token: str = Form(""),
    topic_id: int = Form(0),
):
    """Вставить маркер разрыва сессии в AI-тему (кнопка «Новый диалог» в теме)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "Invalid CSRF"}, status_code=403)
    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    try:
        db = get_web_db(telegram_id, org_db)
        if not _ai_ext_ok(db, telegram_id):
            return JSONResponse({"ok": False, "error": "AI недоступен"}, status_code=403)
        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id or not topic_id:
            return JSONResponse({"ok": False}, status_code=400)
        ai_tid = db.get_ai_topic_id()
        if not ai_tid or ai_tid != topic_id:
            return JSONResponse({"ok": False, "error": "Не AI-тема"}, status_code=400)
        new_id = db.add_ai_chat_session_break(user_db_id, topic_id)
        return JSONResponse({"ok": bool(new_id), "latest_id": new_id})
    except Exception as e:
        logger.error("ai_topic_reset_session: %s", e)
        return JSONResponse({"ok": False}, status_code=500)
