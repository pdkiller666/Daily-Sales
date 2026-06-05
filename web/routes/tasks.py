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
import os
import threading
import urllib.request
from datetime import date, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
logger = logging.getLogger(__name__)

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
    """Push-уведомление в Telegram пользователю (fire-and-forget)."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token or not telegram_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": telegram_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        threading.Thread(
            target=lambda: urllib.request.urlopen(req, timeout=10),
            daemon=True,
        ).start()
    except Exception as e:
        logger.error("task web notify: %s", e)


def _get_user_tg_id(db, user_db_id: int) -> int | None:
    """Получить telegram_id сотрудника по его users.id."""
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT telegram_id FROM users WHERE id = ?", (user_db_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_staff_list(db) -> list:
    """Список активных сотрудников org для выбора исполнителя."""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, first_name, last_name, username, shop_name "
            "FROM users WHERE COALESCE(is_active, 1) != 0 ORDER BY first_name, last_name"
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            name = f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or f"User#{r[0]}"
            result.append({"id": r[0], "name": name, "shop": r[4] or ""})
        return result
    except Exception:
        return []


def _get_shops_list(db) -> list:
    """Список магазинов из users.shop_name (distinct)."""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT shop_name FROM users "
            "WHERE COALESCE(is_active, 1) != 0 AND shop_name IS NOT NULL AND shop_name != '' "
            "ORDER BY shop_name"
        ).fetchall()
        conn.close()
        return [{"id": r[0], "name": r[0]} for r in rows]
    except Exception:
        return []


def _get_shop_members_tg_ids(db, shop_name: str) -> list[tuple]:
    """Возвращает [(users.id, telegram_id)] всех активных сотрудников магазина."""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, telegram_id FROM users "
            "WHERE is_active = 1 AND shop_name = ? AND telegram_id IS NOT NULL",
            (shop_name,)
        ).fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def _get_all_members_tg_ids(db) -> list[tuple]:
    """Возвращает [(users.id, telegram_id)] всех активных сотрудников орга."""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, telegram_id FROM users "
            "WHERE is_active = 1 AND telegram_id IS NOT NULL"
        ).fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def _fmt_deadline(d: str | None) -> str:
    if not d:
        return ""
    try:
        dt = date.fromisoformat(d[:10])
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return d


def _is_overdue(deadline: str | None, status: str) -> bool:
    if not deadline or status in ('done', 'cancelled'):
        return False
    try:
        return date.fromisoformat(deadline[:10]) < date.today()
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
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["topics"] = db.get_task_topics()
        ctx["staff_list"] = _get_staff_list(db)
        ctx["shops_list"] = _get_shops_list(db)
    except Exception as e:
        logger.error("tasks_new_form: %s", e)

    return request.app.state.templates.TemplateResponse(
        request, "tasks/form.html", ctx
    )


@router.post("/tasks/new")
def tasks_new_post(
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
    create_chat_topic: str = Form(""),
    checklist_items: str = Form(""),
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

        _linked_chat_topic_id = None
        if create_chat_topic == "1" and title:
            try:
                chat_topic_id = db.create_chat_topic(title, my_db_id)
                _linked_chat_topic_id = chat_topic_id
            except Exception as ce:
                logger.warning("tasks: create_chat_topic failed: %s", ce)

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
        if assign_mode == "person" and _assigned_to and _assigned_to != my_db_id:
            tg_id = _get_user_tg_id(db, _assigned_to)
            if tg_id:
                try:
                    db.add_notification_to_history(
                        _assigned_to, "task_assigned", f"📋 Назначена задача: {title}"
                    )
                except Exception:
                    pass
                _send_tg_task_notify(tg_id, notify_text)
        elif assign_mode == "shop" and _assigned_shop:
            members = _get_shop_members_tg_ids(db, _assigned_shop)
            for uid, tg_id in members:
                if uid == my_db_id:
                    continue
                try:
                    db.add_notification_to_history(
                        uid, "task_assigned", f"📋 Назначена задача: {title}"
                    )
                except Exception:
                    pass
                _send_tg_task_notify(tg_id, notify_text)
        elif assign_mode == "all":
            members = _get_all_members_tg_ids(db)
            for uid, tg_id in members:
                if uid == my_db_id:
                    continue
                try:
                    db.add_notification_to_history(
                        uid, "task_assigned", f"📋 Назначена задача: {title}"
                    )
                except Exception:
                    pass
                _send_tg_task_notify(tg_id, notify_text)

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
    except Exception as e:
        logger.error("task_detail: %s", e)
        ctx["error"] = "Ошибка загрузки задачи."

    return request.app.state.templates.TemplateResponse(
        request, "tasks/detail.html", ctx
    )


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

        if status == 'done' and task.get("assigned_to") and task["assigned_to"] != my_db_id:
            assigned_tg_id = _get_user_tg_id(db, task["assigned_to"])
            if assigned_tg_id:
                db.add_notification_to_history(
                    task["assigned_to"], "task_status",
                    f"✅ Задача выполнена: {task['title']}"
                )

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
                       assigned_shop=_assigned_shop_val, assign_all=_assign_all)

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
