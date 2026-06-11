"""
Web-маршруты модуля задач.
GET  /tasks                              — список задач
GET  /tasks/new                          — форма создания
POST /tasks/new                          — создать задачу
GET  /tasks/{task_id}                    — детальная карточка
POST /tasks/{task_id}/status             — изменить статус
POST /tasks/{task_id}/comment            — добавить комментарий
POST /tasks/{task_id}/checklist/{iid}/toggle — отметить пункт
POST /tasks/{task_id}/edit               — редактировать (admin)
POST /tasks/{task_id}/delete             — удалить (admin)
GET  /tasks/topics                       — управление темами (admin)
POST /tasks/topics/new                   — создать тему
POST /tasks/topics/{tid}/delete          — удалить тему
"""
import json
import logging
import mimetypes
import os
import re
import threading
import urllib.request
import uuid
from datetime import date, datetime
from typing import List

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import FileResponse, RedirectResponse, Response

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_TASK_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ на файл
MAX_TASK_FILES     = 10                 # до 10 файлов на задачу


def _uploads_dir_tasks(org_db: str) -> str:
    base = os.path.splitext(org_db)[0]
    d = base + "_uploads/tasks"
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename_tasks(original: str) -> str:
    name = os.path.basename(original)
    name = re.sub(r'[^\w.\-]', '_', name)
    return name[:120] or "file"


def _fmt_filesize(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size // 1024} КБ"
    return f"{size / 1024 / 1024:.1f} МБ"


PRIORITY_LABELS = {
    'low':    '🟢 Низкий',
    'normal': '🔵 Обычный',
    'high':   '🟡 Высокий',
    'urgent': '🔴 Срочно',
}
PRIORITY_CSS = {
    'low':    'bg-emerald-50 text-emerald-700 border-emerald-200',
    'normal': 'bg-blue-50 text-blue-700 border-blue-200',
    'high':   'bg-amber-50 text-amber-700 border-amber-200',
    'urgent': 'bg-red-50 text-red-700 border-red-200',
}
STATUS_LABELS = {
    'new':         '🆕 Новая',
    'in_progress': '🔄 В работе',
    'review':      '🔍 На проверке',
    'done':        '✅ Выполнена',
    'cancelled':   '🚫 Отменена',
}
STATUS_CSS = {
    'new':         'bg-slate-100 text-slate-600 border-slate-300',
    'in_progress': 'bg-blue-50 text-blue-700 border-blue-200',
    'review':      'bg-amber-50 text-amber-700 border-amber-200',
    'done':        'bg-emerald-50 text-emerald-700 border-emerald-200',
    'cancelled':   'bg-red-50 text-red-500 border-red-200',
}
TOPIC_COLORS = {
    'blue':   'bg-blue-100 text-blue-700 border-blue-200',
    'green':  'bg-emerald-100 text-emerald-700 border-emerald-200',
    'amber':  'bg-amber-100 text-amber-700 border-amber-200',
    'red':    'bg-red-100 text-red-700 border-red-200',
    'purple': 'bg-purple-100 text-purple-700 border-purple-200',
    'slate':  'bg-slate-100 text-slate-600 border-slate-300',
}


def _send_tg_task_notify(telegram_id: int, text: str) -> None:
    """Push-уведомление в Telegram пользователю (fire-and-forget с логированием ошибок)."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token or not telegram_id:
        logger.warning("task notify SKIP: token=%s tg_id=%s", bool(token), telegram_id)
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": telegram_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[{"text": "✅ Прочитано", "callback_data": "notif_read"}]]
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )

        def _do_send():
            try:
                resp = urllib.request.urlopen(req, timeout=10)
                body = resp.read(512)
                logger.info("task notify OK tg_id=%s status=%s body=%s",
                            telegram_id, resp.status, body[:200])
            except Exception as err:
                logger.error("task notify FAIL tg_id=%s: %s", telegram_id, err)

        threading.Thread(target=_do_send, daemon=True).start()
    except Exception as e:
        logger.error("task web notify setup: %s", e)


def _get_user_tg_id(db, user_db_id: int) -> int | None:
    """Получить telegram_id сотрудника по его users.id."""
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT id, telegram_id FROM users WHERE id = ?", (user_db_id,)
        ).fetchone()
        logger.info("_get_user_tg_id: uid=%s row=%s", user_db_id, row)
        conn.close()
        if row and row[1] is not None:
            return row[1]
        # Фоллбэк: возможно id не совпадает, пробуем поиск среди всех пользователей
        # (защита на случай несоответствия id в staff_list и users table)
        logger.warning("_get_user_tg_id: tg_id is None for uid=%s, fallback to all-users scan",
                       user_db_id)
        conn2 = db.get_connection()
        all_rows = conn2.execute(
            "SELECT id, telegram_id FROM users WHERE telegram_id IS NOT NULL"
        ).fetchall()
        logger.warning("_get_user_tg_id: all users with tg_id: %s",
                       [(r[0], r[1]) for r in all_rows])
        conn2.close()
        return None
    except Exception as e:
        logger.error("_get_user_tg_id error uid=%s: %s", user_db_id, e)
        return None


def _get_staff_list(db) -> list:
    """Список сотрудников org для выбора исполнителя."""
    try:
        rows = db.get_all_users()
        result = []
        for r in rows:
            # SELECT * → id(0) telegram_id(1) first_name(2) last_name(3) middle_name(4)
            #             phone(5) email(6) trade_network(7) shop_name(8) city(9)
            #             timezone(10) created_at(11) username(12)
            uid = r[0]
            name = f"{r[2] or ''} {r[3] or ''}".strip() or r[12] or f"User#{uid}"
            shop = r[8] or ""
            result.append({"id": uid, "name": name, "shop": shop})
        return result
    except Exception:
        return []


def _get_shops_list(db) -> list:
    """Список магазинов из users.shop_name (distinct)."""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT shop_name FROM users "
            "WHERE shop_name IS NOT NULL AND shop_name != '' "
            "ORDER BY shop_name"
        ).fetchall()
        conn.close()
        return [{"id": r[0], "name": r[0]} for r in rows]
    except Exception:
        return []


def _build_search_items_json(staff_list: list, shops_list: list) -> str:
    """JSON-массив для динамического поиска получателей задачи."""
    items = []
    for s in staff_list:
        label = s['name'] + (f" ({s['shop']})" if s.get('shop') else '')
        items.append({"type": "user", "id": s['id'], "name": s['name'],
                      "label": label, "key": f"u{s['id']}"})
    for sh in shops_list:
        items.append({"type": "shop", "id": None, "name": sh['name'],
                      "label": f"🏪 {sh['name']}", "key": f"s{sh['name']}"})
    return json.dumps(items, ensure_ascii=False)


def _build_init_selected_json(task: dict | None) -> tuple[str, str]:
    """Возвращает (init_selected_json, init_assign_all) для формы редактирования."""
    if not task:
        return "[]", "false"
    if task.get('assign_all'):
        return "[]", "true"
    if task.get('assigned_to') and task.get('assigned_name'):
        name = task['assigned_name']
        uid = task['assigned_to']
        return json.dumps([{"type": "user", "id": uid, "name": name,
                            "label": name, "key": f"u{uid}"}], ensure_ascii=False), "false"
    if task.get('assigned_shop'):
        shop = task['assigned_shop']
        return json.dumps([{"type": "shop", "id": None, "name": shop,
                            "label": f"🏪 {shop}", "key": f"s{shop}"}], ensure_ascii=False), "false"
    return "[]", "false"


def _get_shop_members_tg_ids(db, shop_name: str) -> list[tuple]:
    """Возвращает [(users.id, telegram_id)] всех активных сотрудников магазина."""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, telegram_id FROM users "
            "WHERE shop_name = ? AND telegram_id IS NOT NULL",
            (shop_name,)
        ).fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def _get_all_members_tg_ids(db) -> list[tuple]:
    """Возвращает [(users.id, telegram_id)] всех сотрудников орга."""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, telegram_id FROM users "
            "WHERE telegram_id IS NOT NULL"
        ).fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def _fmt_deadline(d: str | None) -> str:
    if not d:
        return ""
    try:
        s = d[:16].replace("T", " ")
        has_time = len(d) >= 13 and ("T" in d or " " in d[10:])
        dt = datetime.fromisoformat(s) if has_time else date.fromisoformat(d[:10])
        if has_time:
            return dt.strftime("%d.%m.%Y %H:%M")
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return d


def _is_overdue(deadline: str | None, status: str) -> bool:
    if not deadline or status in ('done', 'cancelled'):
        return False
    try:
        dl = date.fromisoformat(deadline[:10])
        return dl < date.today()
    except Exception:
        return False


# ─── LIST ────────────────────────────────────────────────────────────────────

@router.get("/tasks")
def tasks_list(request: Request, status: str = "", topic_id: int = 0,
               assigned_filter: int = 0, shop_filter: str = "", msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    ctx = {
        "request": request, "user": user, "is_admin": is_admin,
        "tasks": [], "topics": [], "staff_list": [], "shops_list": [],
        "status_filter": status, "topic_filter": topic_id,
        "assigned_filter": assigned_filter, "shop_filter": shop_filter,
        "status_labels": STATUS_LABELS, "status_css": STATUS_CSS,
        "priority_labels": PRIORITY_LABELS, "priority_css": PRIORITY_CSS,
        "topic_colors": TOPIC_COLORS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
        "fmt_deadline": _fmt_deadline, "is_overdue": _is_overdue,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = (my_row[1] or "") if my_row else ""

        ctx["topics"] = db.get_task_topics()

        tasks = db.get_tasks(
            status=status or None,
            topic_id=topic_id or None,
            assigned_to=assigned_filter if assigned_filter else None,
            shop_filter=shop_filter or None,
            is_admin=is_admin,
            my_user_id=my_db_id,
            my_shop=my_shop or None,
        )
        ctx["tasks"] = tasks
        if is_admin:
            ctx["staff_list"] = _get_staff_list(db)
            ctx["shops_list"] = _get_shops_list(db)
        ctx["my_db_id"] = my_db_id
    except Exception as e:
        logger.error("tasks_list: %s", e)
        ctx["error"] = "Ошибка загрузки задач."

    return request.app.state.templates.TemplateResponse(
        request, "tasks/index.html", ctx
    )


# ─── NEW FORM ────────────────────────────────────────────────────────────────

@router.get("/tasks/new")
def tasks_new_form(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "topics": [], "staff_list": [], "shops_list": [],
        "priority_labels": PRIORITY_LABELS,
        "csrf_token": get_csrf_token(request),
        "edit_task": None, "error": None,
        "search_items_json": "[]", "init_selected_json": "[]", "init_assign_all": "false",
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["topics"] = db.get_task_topics()
        ctx["staff_list"] = _get_staff_list(db)
        ctx["shops_list"] = _get_shops_list(db)
        ctx["search_items_json"] = _build_search_items_json(ctx["staff_list"], ctx["shops_list"])
        ctx["init_selected_json"] = "[]"
        ctx["init_assign_all"] = "false"
    except Exception as e:
        logger.error("tasks_new_form: %s", e)

    return request.app.state.templates.TemplateResponse(
        request, "tasks/form.html", ctx
    )


@router.post("/tasks/new")
async def tasks_new_post(
    request: Request,
    csrf_token: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    topic_id: str = Form(""),
    assign_mode: str = Form("person"),
    assigned_to: str = Form(""),
    assigned_shop: str = Form(""),
    priority: str = Form("normal"),
    deadline: str = Form(""),
    recurrence: str = Form("none"),
    create_chat_topic: str = Form(""),
    checklist_items: str = Form(""),
    recipients_json: str = Form(""),
    files: List[UploadFile] = File(default=[]),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks/new?msg=csrf_error", status_code=303)

    title = title.strip()
    if not title:
        return RedirectResponse(url="/tasks/new?msg=no_title", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        _topic_id = int(topic_id) if topic_id.isdigit() else None
        _deadline = deadline.strip() or None
        if priority not in PRIORITY_LABELS:
            priority = 'normal'

        # Resolve assignment based on mode
        _assigned_to = None
        _assigned_shop = None
        _assign_all = 0
        if assign_mode == "person":
            _assigned_to = int(assigned_to) if assigned_to.isdigit() else None
        elif assign_mode == "shop":
            _assigned_shop = assigned_shop.strip() or None
        elif assign_mode == "all":
            _assign_all = 1
        elif assign_mode == "multi":
            # Multiple recipients — create one task per recipient, return early
            _recip_list = []
            if recipients_json:
                try:
                    _recip_list = json.loads(recipients_json)
                except Exception:
                    pass
            if _recip_list:
                _items = [s.strip() for s in checklist_items.split("\n") if s.strip()]
                for _rec in _recip_list:
                    _r_to = _rec.get('id') if _rec.get('type') == 'user' else None
                    _r_shop = _rec.get('name') if _rec.get('type') == 'shop' else None
                    _rtid = db.create_task(
                        title=title,
                        description=description.strip(),
                        topic_id=_topic_id,
                        created_by=my_db_id,
                        assigned_to=_r_to,
                        assigned_shop=_r_shop,
                        assign_all=0,
                        priority=priority,
                        deadline=_deadline,
                        linked_chat_topic_id=None,
                        checklist=_items,
                        recurrence=recurrence if recurrence not in ('none', '') else None,
                    )
                    if _r_to and _rtid:
                        try:
                            db.add_notification_to_history(
                                _r_to, "task_assign",
                                f"📋 Назначена задача: {title}"
                            )
                        except Exception:
                            pass
                return RedirectResponse(url="/tasks?msg=created", status_code=303)

        _linked_chat_topic_id = None
        if create_chat_topic == "1" and title:
            try:
                chat_topic_id = db.add_chat_topic(title, my_db_id)
                _linked_chat_topic_id = chat_topic_id
            except Exception as ce:
                logger.warning("tasks: add_chat_topic failed: %s", ce)

        items = [s.strip() for s in checklist_items.split("\n") if s.strip()]

        task_id = db.create_task(
            title=title,
            description=description.strip(),
            topic_id=_topic_id,
            created_by=my_db_id,
            assigned_to=_assigned_to,
            assigned_shop=_assigned_shop,
            assign_all=_assign_all,
            priority=priority,
            deadline=_deadline,
            linked_chat_topic_id=_linked_chat_topic_id,
            checklist=items,
            recurrence=recurrence if recurrence not in ('none', '') else None,
        )

        # Send notifications
        deadline_str = f"\n📅 Срок: {_fmt_deadline(_deadline)}" if _deadline else ""
        prio = PRIORITY_LABELS.get(priority, "")
        notify_text = (
            f"📋 <b>Вам назначена задача</b>\n\n"
            f"<b>{title}</b>\n"
            f"{prio}{deadline_str}\n\n"
            f"🌐 Откройте веб-кабинет для подробностей."
        )
        notif_msg = f"📋 Назначена задача: {title}"

        def _safe_add_notif(uid: int, ntype: str, msg: str):
            try:
                db.add_notification_to_history(uid, ntype, msg)
            except Exception as _ne:
                logger.warning("tasks: add_notification_to_history uid=%s: %s", uid, _ne)

        logger.info("tasks notify: mode=%s assigned_to=%s my_db_id=%s",
                    assign_mode, _assigned_to, my_db_id)
        if assign_mode == "person" and _assigned_to:
            tg_id = _get_user_tg_id(db, _assigned_to)
            logger.info("tasks notify: person uid=%s tg_id=%s type=%s",
                        _assigned_to, tg_id, type(tg_id).__name__)
            _safe_add_notif(_assigned_to, "task_assigned", notif_msg)
            if tg_id:
                _send_tg_task_notify(tg_id, notify_text)
                try:
                    from web.push_utils import apush
                    await apush(tg_id, "📋 Новая задача", notif_msg, "/tasks")
                except Exception:
                    pass
            else:
                logger.warning("tasks notify: no tg_id for uid=%s — checking DB directly",
                               _assigned_to)
                try:
                    _c = db.get_connection()
                    _r = _c.execute("SELECT id, telegram_id FROM users WHERE id=?",
                                    (_assigned_to,)).fetchone()
                    logger.warning("tasks notify: raw DB row for uid=%s → %s", _assigned_to, _r)
                except Exception as _de:
                    logger.error("tasks notify: DB check failed: %s", _de)
            # Уведомление создателю в историю (если он не тот же, кто исполнитель)
            if my_db_id and my_db_id != _assigned_to:
                assignee_name = next(
                    (s["name"] for s in _get_staff_list(db) if s["id"] == _assigned_to),
                    f"id={_assigned_to}"
                )
                creator_msg = f"📋 Задача создана: «{title}» → {assignee_name}"
                _safe_add_notif(my_db_id, "task_assigned", creator_msg)
        elif assign_mode == "shop" and _assigned_shop:
            members = _get_shop_members_tg_ids(db, _assigned_shop)
            logger.info("tasks notify: shop=%s members=%s", _assigned_shop, len(members))
            for uid, tg_id in members:
                _safe_add_notif(uid, "task_assigned", notif_msg)
                if tg_id:
                    _send_tg_task_notify(tg_id, notify_text)
                    try:
                        from web.push_utils import apush
                        await apush(tg_id, "📋 Новая задача", notif_msg, "/tasks")
                    except Exception:
                        pass
            # Notify creator even if they're not in that shop
            if my_db_id and not any(uid == my_db_id for uid, _ in members):
                _safe_add_notif(my_db_id, "task_assigned",
                                f"📋 Задача создана: «{title}» → магазин {_assigned_shop}")
        elif assign_mode == "all":
            members = _get_all_members_tg_ids(db)
            logger.info("tasks notify: all members=%s", len(members))
            for uid, tg_id in members:
                _safe_add_notif(uid, "task_assigned", notif_msg)
                if tg_id:
                    _send_tg_task_notify(tg_id, notify_text)
                    try:
                        from web.push_utils import apush
                        await apush(tg_id, "📋 Новая задача", notif_msg, "/tasks")
                    except Exception:
                        pass
            # Notify creator if somehow not in members list
            if my_db_id and not any(uid == my_db_id for uid, _ in members):
                _safe_add_notif(my_db_id, "task_assigned",
                                f"📋 Задача создана: «{title}» → вся команда")

        # Вложения при создании задачи
        if files:
            uploads_dir = _uploads_dir_tasks(org_db)
            month_dir = datetime.now().strftime("%Y-%m")
            month_path = os.path.join(uploads_dir, month_dir)
            os.makedirs(month_path, exist_ok=True)
            saved = []
            for f in files:
                if not f or not f.filename:
                    continue
                if len(saved) >= MAX_TASK_FILES:
                    break
                try:
                    raw_data = await f.read()
                    if len(raw_data) == 0 or len(raw_data) > MAX_TASK_FILE_SIZE:
                        continue
                    mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
                    safe_name = _safe_filename_tasks(f.filename)
                    uid = uuid.uuid4().hex[:12]
                    dest = os.path.join(month_path, f"{uid}_{safe_name}")
                    with open(dest, "wb") as fout:
                        fout.write(raw_data)
                    saved.append({
                        "file_path": dest, "file_name": f.filename[:255],
                        "file_type": mime, "file_size": len(raw_data),
                        "uploaded_by": my_db_id,
                    })
                except Exception as fe:
                    logger.warning("tasks_new_post: file skip: %s", fe)
            if saved:
                db.add_task_attachments(task_id, my_db_id, saved)

        return RedirectResponse(url=f"/tasks/{task_id}?msg=created", status_code=303)
    except Exception as e:
        logger.error("tasks_new_post: %s", e)
        return RedirectResponse(url="/tasks/new?msg=error", status_code=303)


# ─── TOPICS (must be BEFORE /{task_id} to avoid route shadowing) ─────────────

@router.get("/tasks/topics")
def tasks_topics(request: Request, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "topics": [], "topic_colors": TOPIC_COLORS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
        "colors": [("blue","bg-blue-500","Синий"), ("green","bg-emerald-500","Зелёный"),
                   ("amber","bg-amber-500","Жёлтый"), ("red","bg-red-500","Красный"),
                   ("purple","bg-purple-500","Фиолетовый"), ("slate","bg-slate-400","Серый")],
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["topics"] = db.get_task_topics()
    except Exception as e:
        logger.error("tasks_topics: %s", e)

    return request.app.state.templates.TemplateResponse(
        request, "tasks/topics.html", ctx
    )


@router.post("/tasks/topics/new")
def tasks_topics_new(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(""),
    color: str = Form("blue"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks/topics", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks/topics?msg=csrf_error", status_code=303)

    name = name.strip()
    if not name:
        return RedirectResponse(url="/tasks/topics?msg=no_name", status_code=303)
    if color not in TOPIC_COLORS:
        color = "blue"

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        db.create_task_topic(name, color, my_db_id)
    except Exception as e:
        logger.error("tasks_topics_new: %s", e)
        return RedirectResponse(url="/tasks/topics?msg=error", status_code=303)

    return RedirectResponse(url="/tasks/topics?msg=created", status_code=303)


@router.post("/tasks/topics/{tid}/edit")
def tasks_topics_edit(
    request: Request,
    tid: int,
    csrf_token: str = Form(""),
    name: str = Form(""),
    color: str = Form("blue"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks/topics", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks/topics?msg=csrf_error", status_code=303)

    name = name.strip()
    if not name:
        return RedirectResponse(url="/tasks/topics?msg=no_name", status_code=303)
    if color not in TOPIC_COLORS:
        color = "blue"

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.update_task_topic(tid, name, color)
    except Exception as e:
        logger.error("tasks_topics_edit: %s", e)
        return RedirectResponse(url="/tasks/topics?msg=error", status_code=303)

    return RedirectResponse(url="/tasks/topics?msg=saved", status_code=303)


@router.post("/tasks/topics/{tid}/delete")
def tasks_topics_delete(
    request: Request,
    tid: int,
    csrf_token: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks/topics", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks/topics?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_task_topic(tid)
    except Exception as e:
        logger.error("tasks_topics_delete: %s", e)
        return RedirectResponse(url="/tasks/topics?msg=error", status_code=303)

    return RedirectResponse(url="/tasks/topics?msg=deleted", status_code=303)


# ─── DETAIL ──────────────────────────────────────────────────────────────────

@router.get("/tasks/{task_id}")
def task_detail(request: Request, task_id: int, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    ctx = {
        "request": request, "user": user, "is_admin": is_admin,
        "task": None, "comments": [], "my_db_id": 0,
        "status_labels": STATUS_LABELS, "status_css": STATUS_CSS,
        "priority_labels": PRIORITY_LABELS, "priority_css": PRIORITY_CSS,
        "topic_colors": TOPIC_COLORS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
        "fmt_deadline": _fmt_deadline, "is_overdue": _is_overdue,
        "team_completions": [], "team_members_for_task": [], "completed_user_ids": [],
        "my_completion": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = (my_row[1] or "") if my_row else ""
        ctx["my_db_id"] = my_db_id
        ctx["fmt_filesize"] = _fmt_filesize

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=302)

        if not is_admin:
            allowed = (
                task.get("assigned_to") == my_db_id
                or task.get("created_by") == my_db_id
                or task.get("assign_all")
                or (task.get("assigned_shop") and my_shop and task["assigned_shop"] == my_shop)
            )
            if not allowed:
                return RedirectResponse(url="/tasks", status_code=302)

        ctx["task"] = task
        ctx["my_shop"] = my_shop
        ctx["comments"] = db.get_task_comments(task_id)
        try:
            ctx["attachments"] = db.get_task_attachments(task_id)
        except Exception:
            ctx["attachments"] = []

        # Team completion tracking (admin + assign_all/shop tasks)
        _assign_all = task.get('assign_all', False)
        _assigned_shop = task.get('assigned_shop', '')
        if is_admin and (_assign_all or _assigned_shop):
            try:
                completions = db.get_task_user_completions(task_id)
                completed_ids = [c['user_id'] for c in completions]
                all_staff = _get_staff_list(db)
                if _assign_all:
                    task_members = all_staff
                else:
                    task_members = [s for s in all_staff if s.get('shop') == _assigned_shop]
                ctx["team_completions"] = completions
                ctx["team_members_for_task"] = task_members
                ctx["completed_user_ids"] = completed_ids
            except Exception as _te:
                logger.error("task_detail team_completions: %s", _te)
                ctx["team_completions"] = []
                ctx["team_members_for_task"] = []
                ctx["completed_user_ids"] = []
        else:
            ctx["team_completions"] = []
            ctx["team_members_for_task"] = []
            ctx["completed_user_ids"] = []

        # My personal completion for team tasks (for employees)
        if not is_admin and (_assign_all or _assigned_shop):
            try:
                ctx["my_completion"] = db.get_task_user_completion(task_id, my_db_id)
            except Exception:
                ctx["my_completion"] = None
        else:
            ctx["my_completion"] = None

    except Exception as e:
        logger.error("task_detail: %s", e)
        ctx["error"] = "Ошибка загрузки задачи."

    return request.app.state.templates.TemplateResponse(
        request, "tasks/detail.html", ctx
    )


# ─── TASK ATTACHMENTS ─────────────────────────────────────────────────────────

@router.post("/tasks/{task_id}/upload")
async def task_upload_attachment(
    request: Request,
    task_id: int,
    csrf_token: str = Form(default=""),
    files: List[UploadFile] = File(default=[]),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    try:
        db = get_web_db(telegram_id, org_db)
        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        if not is_admin and task.get("assigned_to") != my_db_id and not task.get("assign_all"):
            return RedirectResponse(url=f"/tasks/{task_id}?msg=access_denied", status_code=303)

        existing_count = db.get_task_attachments_count(task_id)
        if existing_count >= MAX_TASK_FILES:
            return RedirectResponse(url=f"/tasks/{task_id}?msg=too_many_files", status_code=303)

        uploads_dir = _uploads_dir_tasks(org_db)
        month_dir = datetime.now().strftime("%Y-%m")
        month_path = os.path.join(uploads_dir, month_dir)
        os.makedirs(month_path, exist_ok=True)
        saved = []
        for f in files:
            if not f or not f.filename:
                continue
            if existing_count + len(saved) >= MAX_TASK_FILES:
                break
            try:
                raw_data = await f.read()
                if len(raw_data) == 0 or len(raw_data) > MAX_TASK_FILE_SIZE:
                    continue
                mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
                safe_name = _safe_filename_tasks(f.filename)
                uid = uuid.uuid4().hex[:12]
                dest = os.path.join(month_path, f"{uid}_{safe_name}")
                with open(dest, "wb") as fout:
                    fout.write(raw_data)
                saved.append({
                    "file_path": dest, "file_name": f.filename[:255],
                    "file_type": mime, "file_size": len(raw_data),
                    "uploaded_by": my_db_id,
                })
            except Exception as exc:
                logger.warning(f"task_upload skip: {exc}")

        if saved:
            db.add_task_attachments(task_id, my_db_id, saved)

    except Exception as e:
        logger.error("task_upload_attachment: %s", e)

    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@router.get("/tasks/attachment/{att_id}")
def task_serve_attachment(request: Request, att_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    try:
        db = get_web_db(telegram_id, org_db)
        row = db.get_task_attachment(att_id)
        if not row:
            return Response(content="Файл не найден", status_code=404)

        # row = (file_path, file_name, file_type, task_id, user_id)
        fpath, fname, ftype, task_id, _ = row

        if not is_admin:
            task = db.get_task(task_id)
            conn = db.get_connection()
            my_row = conn.execute(
                "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            conn.close()
            my_db_id = my_row[0] if my_row else 0
            my_shop = (my_row[1] or "") if my_row else ""
            shop_ok = bool(my_shop and task and task.get("assigned_shop") == my_shop)
            if not task or (
                task.get("assigned_to") != my_db_id
                and not task.get("assign_all")
                and not shop_ok
            ):
                return Response(content="Доступ запрещён", status_code=403)

        if not os.path.isfile(fpath):
            return Response(content="Файл не найден на диске", status_code=404)

        exp_uploads = os.path.abspath(_uploads_dir_tasks(org_db))
        real_fpath = os.path.abspath(fpath)
        if not real_fpath.startswith(exp_uploads):
            return Response(content="Доступ запрещён", status_code=403)

        return FileResponse(fpath, media_type=ftype or "application/octet-stream",
                            filename=fname or os.path.basename(fpath))
    except Exception as exc:
        logger.error(f"task_serve_attachment error: {exc}")
        return Response(content="Ошибка", status_code=500)


@router.post("/tasks/attachment/{att_id}/delete")
def task_delete_attachment(
    request: Request,
    att_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    task_id = 0

    try:
        db = get_web_db(telegram_id, org_db)
        row = db.get_task_attachment(att_id)
        if not row:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        # row = (file_path, file_name, file_type, task_id, user_id)
        _, _, _, task_id, _ = row

        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        # delete_task_attachment returns (success, file_path)
        ok, fpath = db.delete_task_attachment(att_id, my_db_id, is_admin=is_admin)
        if ok and fpath and os.path.isfile(fpath):
            exp_uploads = os.path.abspath(_uploads_dir_tasks(org_db))
            real_fpath = os.path.abspath(fpath)
            if real_fpath.startswith(exp_uploads):
                try:
                    os.remove(fpath)
                except OSError as e:
                    logger.warning(f"task_delete_attachment fs: {e}")

        return RedirectResponse(url=f"/tasks/{task_id or ''}", status_code=303)
    except Exception as exc:
        logger.error(f"task_delete_attachment: {exc}")
        return RedirectResponse(url=f"/tasks/{task_id or ''}", status_code=303)


# ─── CHANGE STATUS ───────────────────────────────────────────────────────────

@router.post("/tasks/{task_id}/status")
def task_change_status(
    request: Request,
    task_id: int,
    csrf_token: str = Form(""),
    status: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    if status not in STATUS_LABELS:
        return RedirectResponse(url=f"/tasks/{task_id}?msg=bad_status", status_code=303)

    # Non-admin cannot cancel tasks
    if not is_admin and status == 'cancelled':
        return RedirectResponse(url=f"/tasks/{task_id}?msg=bad_status", status_code=303)

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        # Deny unresolved users
        if not my_db_id:
            return RedirectResponse(url="/tasks?msg=error", status_code=303)

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        if not is_admin and task.get("assigned_to") != my_db_id:
            return RedirectResponse(url="/tasks", status_code=303)

        # Server-side enforce valid transition for non-admin
        _VALID_NEXT = {'new': 'in_progress', 'in_progress': 'review', 'review': 'done'}
        if not is_admin:
            allowed = _VALID_NEXT.get(task.get('status', ''))
            if status != allowed:
                return RedirectResponse(url=f"/tasks/{task_id}?msg=bad_status", status_code=303)

        db.update_task_status(task_id, status)

        # Record per-user completion for team tasks
        if status in ('done', 'review') and (task.get('assign_all') or task.get('assigned_shop')):
            try:
                db.record_task_user_completion(task_id, my_db_id, status)
            except Exception:
                pass

        # Spawn next recurring task when done
        if status == 'done':
            try:
                from datetime import date, timedelta
                _recurrence = task.get('recurrence') or ''
                if _recurrence and _recurrence not in ('none', ''):
                    _intervals = {'daily': 1, 'weekly': 7, 'monthly': 30}
                    _days = _intervals.get(_recurrence)
                    if _days:
                        _old_dl = task.get('deadline')
                        _base = date.fromisoformat(_old_dl[:10]) if _old_dl else date.today()
                        _new_dl = (_base + timedelta(days=_days)).isoformat()
                        db.create_task(
                            title=task['title'], description=task.get('description', ''),
                            topic_id=task.get('topic_id'), created_by=task.get('created_by', 0),
                            assigned_to=task.get('assigned_to'), assigned_shop=task.get('assigned_shop'),
                            assign_all=1 if task.get('assign_all') else 0,
                            priority=task.get('priority', 'normal'), deadline=_new_dl,
                            recurrence=_recurrence,
                        )
            except Exception as _re:
                logger.warning("task_change_status spawn recurring: %s", _re)

        creator_id = task.get("created_by")
        if creator_id and creator_id != my_db_id:
            tg_id = _get_user_tg_id(db, creator_id)
            if tg_id:
                status_text = STATUS_LABELS.get(status, status)
                db.add_notification_to_history(
                    creator_id, "task_status",
                    f"📋 Задача «{task['title']}»: {status_text}"
                )
                _send_tg_task_notify(
                    tg_id,
                    f"📋 <b>Статус задачи изменён</b>\n\n"
                    f"<b>{task['title']}</b>\n"
                    f"Новый статус: {status_text}"
                )
                try:
                    from web.push_utils import send_web_push
                    send_web_push(tg_id, "📋 Задача обновлена", f"«{task['title']}»: {status_text}", "/tasks")
                except Exception:
                    pass

        if status == 'done' and task.get("assigned_to") and task["assigned_to"] != my_db_id:
            assigned_tg_id = _get_user_tg_id(db, task["assigned_to"])
            if assigned_tg_id:
                db.add_notification_to_history(
                    task["assigned_to"], "task_status",
                    f"✅ Задача выполнена: {task['title']}"
                )
                _send_tg_task_notify(
                    assigned_tg_id,
                    f"✅ <b>Задача выполнена</b>\n\n<b>{task['title']}</b>"
                )
                try:
                    from web.push_utils import send_web_push
                    send_web_push(assigned_tg_id, "✅ Задача выполнена", task['title'], "/tasks")
                except Exception:
                    pass

    except Exception as e:
        logger.error("task_change_status: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)

    return RedirectResponse(url=f"/tasks/{task_id}?msg=status_updated", status_code=303)


# ─── COMMENT ─────────────────────────────────────────────────────────────────

@router.post("/tasks/{task_id}/comment")
def task_add_comment(
    request: Request,
    task_id: int,
    csrf_token: str = Form(""),
    text: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    text = text.strip()
    if not text:
        return RedirectResponse(url=f"/tasks/{task_id}?msg=no_text", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)
        if not is_admin and task.get("assigned_to") != my_db_id and task.get("created_by") != my_db_id:
            return RedirectResponse(url="/tasks", status_code=303)

        db.add_task_comment(task_id, my_db_id, text)

        other_id = task.get("created_by") if task.get("assigned_to") == my_db_id else task.get("assigned_to")
        if other_id and other_id != my_db_id:
            tg_id = _get_user_tg_id(db, other_id)
            if tg_id:
                db.add_notification_to_history(
                    other_id, "task_status",
                    f"💬 Новый комментарий к задаче «{task['title']}»"
                )
                _send_tg_task_notify(
                    tg_id,
                    f"💬 <b>Новый комментарий</b>\n\nЗадача: <b>{task['title']}</b>"
                )
                try:
                    from web.push_utils import send_web_push
                    send_web_push(tg_id, "💬 Новый комментарий", f"Задача: {task['title']}", "/tasks")
                except Exception:
                    pass

    except Exception as e:
        logger.error("task_add_comment: %s", e)

    return RedirectResponse(url=f"/tasks/{task_id}?msg=commented", status_code=303)


# ─── CHECKLIST TOGGLE ────────────────────────────────────────────────────────

@router.post("/tasks/{task_id}/checklist/{item_id}/toggle")
def task_toggle_checklist(
    request: Request,
    task_id: int,
    item_id: int,
    csrf_token: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        # Deny unresolved users
        if not my_db_id:
            return RedirectResponse(url="/tasks?msg=error", status_code=303)

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)
        if not is_admin and task.get("assigned_to") != my_db_id:
            return RedirectResponse(url="/tasks", status_code=303)

        # IDOR guard: verify item_id belongs to this task_id
        item_conn = db.get_connection()
        item_row = item_conn.execute(
            "SELECT task_id FROM task_checklist WHERE id = ?", (item_id,)
        ).fetchone()
        item_conn.close()
        if not item_row or item_row[0] != task_id:
            return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)

        # Block checklist edits on closed tasks for non-admin
        if not is_admin and task.get("status") in ('done', 'cancelled'):
            return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

        db.toggle_task_checklist_item(item_id, my_db_id)
    except Exception as e:
        logger.error("task_toggle_checklist: %s", e)

    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


# ─── EDIT ────────────────────────────────────────────────────────────────────

@router.get("/tasks/{task_id}/edit")
def task_edit_form(request: Request, task_id: int):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "topics": [], "staff_list": [], "shops_list": [],
        "priority_labels": PRIORITY_LABELS,
        "csrf_token": get_csrf_token(request),
        "edit_task": None, "error": None,
        "search_items_json": "[]", "init_selected_json": "[]", "init_assign_all": "false",
    }
    try:
        db = get_web_db(telegram_id, org_db)
        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=302)
        ctx["edit_task"] = task
        ctx["topics"] = db.get_task_topics()
        ctx["staff_list"] = _get_staff_list(db)
        ctx["shops_list"] = _get_shops_list(db)
        ctx["search_items_json"] = _build_search_items_json(ctx["staff_list"], ctx["shops_list"])
        init_sel, init_all = _build_init_selected_json(task)
        ctx["init_selected_json"] = init_sel
        ctx["init_assign_all"] = init_all
    except Exception as e:
        logger.error("task_edit_form: %s", e)

    return request.app.state.templates.TemplateResponse(
        request, "tasks/form.html", ctx
    )


@router.post("/tasks/{task_id}/edit")
def task_edit_post(
    request: Request,
    task_id: int,
    csrf_token: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    topic_id: str = Form(""),
    assign_mode: str = Form("person"),
    assigned_to: str = Form(""),
    assigned_shop: str = Form(""),
    priority: str = Form("normal"),
    deadline: str = Form(""),
    recurrence: str = Form("none"),
    recipients_json: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}/edit?msg=csrf_error", status_code=303)

    title = title.strip()
    if not title:
        return RedirectResponse(url=f"/tasks/{task_id}/edit?msg=no_title", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        old_task = db.get_task(task_id)
        if not old_task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        _topic_id = int(topic_id) if topic_id.isdigit() else None
        _deadline = deadline.strip() or None
        if priority not in PRIORITY_LABELS:
            priority = 'normal'

        _assigned_to = None
        _assigned_shop_val = None
        _assign_all = 0
        if assign_mode == "person":
            _assigned_to = int(assigned_to) if assigned_to.isdigit() else None
        elif assign_mode == "shop":
            _assigned_shop_val = assigned_shop.strip() or None
        elif assign_mode == "all":
            _assign_all = 1

        db.update_task(task_id, title, description.strip(), _topic_id,
                       _assigned_to, None, priority, _deadline,
                       assigned_shop=_assigned_shop_val, assign_all=_assign_all,
                       recurrence=recurrence if recurrence not in ('none', '') else None)

        # Notify if new person assigned
        if assign_mode == "person" and _assigned_to and _assigned_to != old_task.get("assigned_to"):
            tg_id = _get_user_tg_id(db, _assigned_to)
            if tg_id:
                deadline_str = f"\n📅 Срок: {_fmt_deadline(_deadline)}" if _deadline else ""
                try:
                    db.add_notification_to_history(
                        _assigned_to, "task_assigned", f"📋 Назначена задача: {title}"
                    )
                except Exception:
                    pass
                _send_tg_task_notify(
                    tg_id,
                    f"📋 <b>Вам назначена задача</b>\n\n"
                    f"<b>{title}</b>"
                    f"{deadline_str}\n\n"
                    f"🌐 Откройте веб-кабинет для подробностей."
                )
                try:
                    from web.push_utils import send_web_push
                    send_web_push(tg_id, "📋 Назначена задача", title, "/tasks")
                except Exception:
                    pass
    except Exception as e:
        logger.error("task_edit_post: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}/edit?msg=error", status_code=303)

    return RedirectResponse(url=f"/tasks/{task_id}?msg=saved", status_code=303)


# ─── DELETE ──────────────────────────────────────────────────────────────────

@router.post("/tasks/{task_id}/delete")
def task_delete(
    request: Request,
    task_id: int,
    csrf_token: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_task(task_id)
    except Exception as e:
        logger.error("task_delete: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)

    return RedirectResponse(url="/tasks?msg=deleted", status_code=303)


@router.post("/tasks/{task_id}/my_complete")
def task_my_complete(
    request: Request,
    task_id: int,
    csrf_token: str = Form(""),
):
    """Сотрудник помечает командную задачу как выполненную со своей стороны."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        if not my_db_id:
            return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        # Only allowed on assign_all or shop tasks
        if not (task.get('assign_all') or task.get('assigned_shop')):
            return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

        db.record_task_user_completion(task_id, my_db_id, 'done')
    except Exception as e:
        logger.error("task_my_complete: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)

    return RedirectResponse(url=f"/tasks/{task_id}?msg=done", status_code=303)
