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
import html as _html
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
        try:
            row = conn.execute(
                "SELECT id, telegram_id FROM users WHERE id = ?", (user_db_id,)
            ).fetchone()
        finally:
            conn.close()
        logger.info("_get_user_tg_id: uid=%s row=%s", user_db_id, row)
        if row and row[1] is not None:
            return row[1]
        # Фоллбэк: возможно id не совпадает, пробуем поиск среди всех пользователей
        # (защита на случай несоответствия id в staff_list и users table)
        logger.warning("_get_user_tg_id: tg_id is None for uid=%s, fallback to all-users scan",
                       user_db_id)
        conn2 = db.get_connection()
        try:
            all_rows = conn2.execute(
                "SELECT id, telegram_id FROM users WHERE telegram_id IS NOT NULL"
            ).fetchall()
        finally:
            conn2.close()
        logger.warning("_get_user_tg_id: all users with tg_id: %s",
                       [(r[0], r[1]) for r in all_rows])
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
        try:
            rows = conn.execute(
                "SELECT DISTINCT shop_name FROM users "
                "WHERE shop_name IS NOT NULL AND shop_name != '' "
                "ORDER BY shop_name"
            ).fetchall()
        finally:
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
        try:
            rows = conn.execute(
                "SELECT id, telegram_id FROM users "
                "WHERE shop_name = ? AND telegram_id IS NOT NULL",
                (shop_name,)
            ).fetchall()
        finally:
            conn.close()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def _get_all_members_tg_ids(db) -> list[tuple]:
    """Возвращает [(users.id, telegram_id)] всех сотрудников орга."""
    try:
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT id, telegram_id FROM users "
                "WHERE telegram_id IS NOT NULL"
            ).fetchall()
        finally:
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
        has_time = len(deadline) >= 13 and ("T" in deadline or " " in deadline[10:])
        if has_time:
            dl_dt = datetime.fromisoformat(deadline[:16].replace("T", " "))
            return dl_dt < datetime.now()
        dl = date.fromisoformat(deadline[:10])
        return dl < date.today()
    except Exception:
        return False


# ─── LIST ────────────────────────────────────────────────────────────────────

@router.get("/tasks")
def tasks_list(request: Request, status: str = "", topic_id: int = 0,
               assigned_filter: int = 0, shop_filter: str = "", msg: str = "",
               q: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    from billing_utils import has_module as _has_module, has_extension as _has_ext
    tasks_pro = _has_module(telegram_id, 'tasks_pro')
    tasks_ai = tasks_pro and _has_ext(telegram_id, 'tasks_ai')

    ctx = {
        "request": request, "user": user, "is_admin": is_admin,
        "tasks": [], "topics": [], "staff_list": [], "shops_list": [],
        "status_filter": status, "topic_filter": topic_id,
        "assigned_filter": assigned_filter, "shop_filter": shop_filter,
        "q": q,
        "status_labels": STATUS_LABELS, "status_css": STATUS_CSS,
        "priority_labels": PRIORITY_LABELS, "priority_css": PRIORITY_CSS,
        "topic_colors": TOPIC_COLORS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
        "fmt_deadline": _fmt_deadline, "is_overdue": _is_overdue,
        "tasks_pro": tasks_pro, "tasks_ai": tasks_ai,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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
            q=q.strip() or None,
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

def _chat_available(telegram_id: int) -> bool:
    """Доступен ли модуль чата (глобально не отключён + есть в биллинге владельца).

    Зеркалит логику chat_page: иначе задача могла создать тему в отключённом/
    неоплаченном чате, а ссылка «Обсудить в чате» вела на заблокированный модуль.
    """
    try:
        from web.routes.chat import _get_chat_min_plan, _chat_access_ok
        if _get_chat_min_plan() == "Отключён":
            return False
        return _chat_access_ok(telegram_id)
    except Exception:
        return False


# ─── TEMPLATES ───────────────────────────────────────────────────────────────

# ─── POOL (самоназначение) ────────────────────────────────────────────────────

# ─── AI DECOMPOSE ────────────────────────────────────────────────────────────

@router.get("/tasks/decompose")
def tasks_decompose_form(request: Request, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module as _has_module, has_extension as _has_ext

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    if not _has_module(telegram_id, 'tasks_pro') or not _has_ext(telegram_id, 'tasks_ai'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=302)

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "topics": [], "priority_labels": PRIORITY_LABELS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["topics"] = db.get_task_topics()
    except Exception as e:
        logger.error("tasks_decompose_form: %s", e)

    return request.app.state.templates.TemplateResponse(
        request, "tasks/decompose.html", ctx
    )


@router.post("/tasks/decompose/create")
def tasks_decompose_create(
    request: Request,
    csrf_token: str = Form(""),
    tasks_json: str = Form(""),
    topic_id: str = Form(""),
    deadline: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module as _has_module, has_extension as _has_ext
    import json as _json

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks/decompose?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    if not _has_module(telegram_id, 'tasks_pro') or not _has_ext(telegram_id, 'tasks_ai'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=303)

    try:
        tasks_data = _json.loads(tasks_json or "[]")
        if not tasks_data:
            return RedirectResponse(url="/tasks/decompose?msg=no_tasks", status_code=303)

        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0

        tid = int(topic_id) if topic_id and topic_id.isdigit() else None
        dl = deadline.strip() or None

        created_count = 0
        for t in tasks_data[:10]:
            if isinstance(t, dict) and t.get('title'):
                db.create_task(
                    title=t['title'][:200],
                    description=t.get('description', '')[:2000],
                    topic_id=tid,
                    created_by=my_db_id,
                    priority=t.get('priority', 'normal') if t.get('priority') in ('normal', 'high', 'urgent', 'low') else 'normal',
                    deadline=dl,
                )
                created_count += 1

        return RedirectResponse(url=f"/tasks?msg=created_{created_count}", status_code=303)
    except Exception as e:
        logger.error("tasks_decompose_create: %s", e)
        return RedirectResponse(url="/tasks/decompose?msg=error", status_code=303)


@router.get("/tasks/pool")
def tasks_pool(request: Request, topic_id: str = "", q: str = "", msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=302)

    ctx = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "tasks": [], "topics": [],
        "priority_labels": PRIORITY_LABELS,
        "csrf_token": get_csrf_token(request),
        "topic_filter": topic_id, "q": q, "msg": msg,
        "error": None,
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["topics"] = db.get_task_topics()
        tid = int(topic_id) if topic_id and topic_id.isdigit() else None
        ctx["tasks"] = db.get_unassigned_tasks(topic_id=tid, q=q.strip() or None)
    except Exception as e:
        logger.error("tasks_pool: %s", e)
        ctx["error"] = "Ошибка загрузки пула задач."

    return request.app.state.templates.TemplateResponse(
        request, "tasks/pool.html", ctx
    )


@router.post("/tasks/{task_id}/self_assign")
def task_self_assign(request: Request, task_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/pool?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=303)

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        finally:
            conn.close()
        if not my_row:
            return RedirectResponse(url="/tasks/pool?msg=error", status_code=303)
        ok = db.self_assign_task(task_id, my_row[0])
        if ok:
            return RedirectResponse(url=f"/tasks/{task_id}?msg=status_updated", status_code=303)
        else:
            return RedirectResponse(url="/tasks/pool?msg=already_taken", status_code=303)
    except Exception as e:
        logger.error("task_self_assign: %s", e)
        return RedirectResponse(url="/tasks/pool?msg=error", status_code=303)


@router.get("/tasks/templates")
def tasks_templates_list(request: Request, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=302)

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "templates": [], "topics": [],
        "priority_labels": PRIORITY_LABELS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["templates"] = db.get_task_templates()
        ctx["topics"] = db.get_task_topics()
    except Exception as e:
        logger.error("tasks_templates_list: %s", e)
        ctx["error"] = "Ошибка загрузки шаблонов."

    return request.app.state.templates.TemplateResponse(
        request, "tasks/templates.html", ctx
    )


@router.post("/tasks/templates/new")
def tasks_templates_new(request: Request, csrf_token: str = Form(""),
                        title: str = Form(""), description: str = Form(""),
                        priority: str = Form("normal"), topic_id: str = Form(""),
                        checklist_items: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    import json as _json

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks/templates?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=303)

    title = title.strip()
    if not title:
        return RedirectResponse(url="/tasks/templates?msg=no_title", status_code=303)

    checklist = []
    for line in (checklist_items or "").splitlines():
        line = line.strip()
        if line:
            checklist.append({"text": line, "done": False})
    checklist_json = _json.dumps(checklist, ensure_ascii=False)

    tid = int(topic_id) if topic_id and topic_id.isdigit() else None

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else None
        db.create_task_template(
            title=title, description=description.strip(),
            priority=priority, checklist_json=checklist_json,
            topic_id=tid, created_by=my_db_id
        )
        return RedirectResponse(url="/tasks/templates?msg=created", status_code=303)
    except Exception as e:
        logger.error("tasks_templates_new: %s", e)
        return RedirectResponse(url="/tasks/templates?msg=error", status_code=303)


@router.post("/tasks/templates/{tid}/delete")
def tasks_template_delete(request: Request, tid: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks/templates?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_task_template(tid)
    except Exception as e:
        logger.error("tasks_template_delete: %s", e)

    return RedirectResponse(url="/tasks/templates?msg=deleted", status_code=303)


@router.post("/tasks/{task_id}/save_as_template")
def task_save_as_template(request: Request, task_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    import json as _json

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=pro_required", status_code=303)

    try:
        db = get_web_db(telegram_id, org_db)
        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=302)

        # Build checklist from existing checklist items
        checklist_items = []
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT text FROM task_checklist WHERE task_id=? ORDER BY sort_order, id",
                (task_id,)
            ).fetchall()
            checklist_items = [{"text": r[0], "done": False} for r in rows]
        finally:
            conn.close()

        conn = db.get_connection()
        try:
            my_row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else None

        db.create_task_template(
            title=task['title'],
            description=task.get('description', ''),
            priority=task.get('priority', 'normal'),
            checklist_json=_json.dumps(checklist_items, ensure_ascii=False),
            topic_id=task.get('topic_id'),
            created_by=my_db_id,
        )
        return RedirectResponse(url=f"/tasks/{task_id}?msg=template_saved", status_code=303)
    except Exception as e:
        logger.error("task_save_as_template: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)


@router.get("/tasks/new")
def tasks_new_form(request: Request, template_id: int = 0):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module, has_extension as _has_ext
    tasks_pro = _has_module(telegram_id, 'tasks_pro')
    tasks_ai = tasks_pro and _has_ext(telegram_id, 'tasks_ai')

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "topics": [], "staff_list": [], "shops_list": [],
        "priority_labels": PRIORITY_LABELS,
        "csrf_token": get_csrf_token(request),
        "edit_task": None, "error": None,
        "chat_available": _chat_available(telegram_id),
        "search_items_json": "[]", "init_selected_json": "[]", "init_assign_all": "false",
        "tasks_pro": tasks_pro, "tasks_ai": tasks_ai, "templates": [],
        "prefill_title": "", "prefill_description": "",
        "prefill_priority": "normal", "prefill_topic_id": 0,
        "prefill_checklist": "",
        "msg": None,
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["topics"] = db.get_task_topics()
        ctx["staff_list"] = _get_staff_list(db)
        ctx["shops_list"] = _get_shops_list(db)
        ctx["search_items_json"] = _build_search_items_json(ctx["staff_list"], ctx["shops_list"])
        ctx["init_selected_json"] = "[]"
        ctx["init_assign_all"] = "false"
        if tasks_pro:
            ctx["templates"] = db.get_task_templates()
            if template_id:
                tmpl = db.get_task_template(template_id)
                if tmpl:
                    ctx["prefill_title"] = tmpl['title']
                    ctx["prefill_description"] = tmpl['description']
                    ctx["prefill_priority"] = tmpl['priority']
                    ctx["prefill_topic_id"] = tmpl['topic_id'] or 0
                    ctx["prefill_checklist"] = "\n".join(
                        item['text'] for item in tmpl.get('checklist', [])
                    )
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
    estimated_hours: str = Form(""),
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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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
                    _prio_label = PRIORITY_LABELS.get(priority, "")
                    _dl_str = f"\n📅 Срок: {_fmt_deadline(_deadline)}" if _deadline else ""
                    _multi_tg_text = (
                        f"📋 <b>Вам назначена задача</b>\n\n"
                        f"<b>{_html.escape(title)}</b>\n"
                        f"{_prio_label}{_dl_str}\n\n"
                        f"🌐 Откройте веб-кабинет для подробностей."
                    )
                    _multi_msg = f"📋 Назначена задача: {title}"
                    if _r_to and _rtid:
                        try:
                            db.add_notification_to_history(_r_to, "task_assigned", _multi_msg)
                        except Exception:
                            pass
                        _r_tg = _get_user_tg_id(db, _r_to)
                        if _r_tg:
                            _send_tg_task_notify(_r_tg, _multi_tg_text)
                            try:
                                from web.push_utils import send_web_push
                                send_web_push(_r_tg, "📋 Новая задача", title, "/tasks")
                            except Exception:
                                pass
                    elif _r_shop and _rtid:
                        for _sm_uid, _sm_tg in _get_shop_members_tg_ids(db, _r_shop):
                            try:
                                db.add_notification_to_history(_sm_uid, "task_assigned", _multi_msg)
                            except Exception:
                                pass
                            if _sm_tg:
                                _send_tg_task_notify(_sm_tg, _multi_tg_text)
                                try:
                                    from web.push_utils import send_web_push
                                    send_web_push(_sm_tg, "📋 Новая задача", title, "/tasks")
                                except Exception:
                                    pass
                return RedirectResponse(url="/tasks?msg=created", status_code=303)

        _linked_chat_topic_id = None
        if create_chat_topic == "1" and title and _chat_available(telegram_id):
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
        if task_id:
            try:
                db.add_task_history(task_id, my_db_id, 'created', None, title)
            except Exception:
                pass
            # Phase 4.3: estimated_hours
            try:
                _eh = float(estimated_hours.strip().replace(',', '.')) if estimated_hours.strip() else None
                if _eh and _eh > 0:
                    _c2 = db.get_connection()
                    try:
                        _c2.execute("UPDATE tasks SET estimated_hours=? WHERE id=?", (_eh, task_id))
                        _c2.commit()
                    finally:
                        _c2.close()
            except Exception:
                pass

        # Send notifications
        deadline_str = f"\n📅 Срок: {_fmt_deadline(_deadline)}" if _deadline else ""
        prio = PRIORITY_LABELS.get(priority, "")
        notify_text = (
            f"📋 <b>Вам назначена задача</b>\n\n"
            f"<b>{_html.escape(title)}</b>\n"
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
                    try:
                        _r = _c.execute("SELECT id, telegram_id FROM users WHERE id=?",
                                        (_assigned_to,)).fetchone()
                    finally:
                        _c.close()
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

@router.get("/tasks/kanban")
def tasks_kanban(request: Request, topic_id: int = 0, shop_filter: str = "",
                 assigned_filter: int = 0):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=302)

    ctx = {
        "request": request, "user": user, "is_admin": is_admin,
        "columns": [],
        "topics": [], "staff_list": [], "shops_list": [],
        "topic_filter": topic_id, "shop_filter": shop_filter,
        "assigned_filter": assigned_filter,
        "status_labels": STATUS_LABELS, "status_css": STATUS_CSS,
        "priority_labels": PRIORITY_LABELS, "priority_css": PRIORITY_CSS,
        "topic_colors": TOPIC_COLORS,
        "csrf_token": get_csrf_token(request),
        "fmt_deadline": _fmt_deadline, "is_overdue": _is_overdue,
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = (my_row[1] or "") if my_row else ""

        ctx["topics"] = db.get_task_topics()
        if is_admin:
            ctx["staff_list"] = _get_staff_list(db)
            ctx["shops_list"] = _get_shops_list(db)

        all_tasks = db.get_tasks(
            topic_id=topic_id or None,
            assigned_to=assigned_filter if assigned_filter else None,
            shop_filter=shop_filter or None,
            is_admin=is_admin,
            my_user_id=my_db_id,
            my_shop=my_shop or None,
        )
        ctx["my_db_id"] = my_db_id

        col_order = ['new', 'in_progress', 'review', 'done']
        col_icons = {'new': '🆕', 'in_progress': '🔄', 'review': '🔍', 'done': '✅'}
        col_names = {'new': 'Новые', 'in_progress': 'В работе',
                     'review': 'На проверке', 'done': 'Выполнены'}
        by_status = {s: [] for s in col_order}
        for t in all_tasks:
            s = t.get('status', 'new')
            if s in by_status:
                by_status[s].append(t)
        ctx["columns"] = [
            {"key": s, "icon": col_icons[s], "name": col_names[s],
             "tasks": by_status[s]}
            for s in col_order
        ]
    except Exception as e:
        logger.error("tasks_kanban: %s", e)
        ctx["error"] = "Ошибка загрузки канбан-доски."

    return request.app.state.templates.TemplateResponse(
        request, "tasks/kanban.html", ctx
    )


@router.post("/tasks/kanban/move")
def tasks_kanban_move(request: Request,
                      task_id: int = Form(...), status: str = Form(...),
                      csrf_token: str = Form(...)):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from fastapi.responses import JSONResponse

    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "not_auth"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"error": "csrf"}, status_code=403)
    if status not in STATUS_LABELS:
        return JSONResponse({"error": "bad_status"}, status_code=400)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    try:
        db = get_web_db(telegram_id, org_db)
        task = db.get_task(task_id)
        if not task:
            return JSONResponse({"error": "not_found"}, status_code=404)

        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0

        can_edit = (
            is_admin
            or task.get('created_by') == my_db_id
            or task.get('assigned_to') == my_db_id
            or task.get('assign_all')
        )
        if not can_edit:
            return JSONResponse({"error": "forbidden"}, status_code=403)

        old_status_kb = task.get('status', '')
        db.update_task_status(task_id, status)
        try:
            db.add_task_history(task_id, my_db_id, 'status',
                                STATUS_LABELS.get(old_status_kb, old_status_kb),
                                STATUS_LABELS.get(status, status))
        except Exception:
            pass
        return JSONResponse({"ok": True, "new_status": status,
                             "label": STATUS_LABELS[status]})
    except Exception as e:
        logger.error("tasks_kanban_move: %s", e)
        return JSONResponse({"error": "server_error"}, status_code=500)


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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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


# ─── BULK OPERATIONS ─────────────────────────────────────────────────────────

@router.post("/tasks/bulk")
def tasks_bulk(request: Request, action: str = Form(""),
               task_ids: list[int] = Form(default=[]),
               csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/tasks?msg=csrf_error", status_code=303)
    if not task_ids or not action:
        return RedirectResponse(url="/tasks?msg=error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=303)

    STATUS_MAP = {
        "status_new": "new",
        "status_in_progress": "in_progress",
        "status_review": "review",
        "status_done": "done",
        "status_cancelled": "cancelled",
    }

    try:
        db = get_web_db(telegram_id, org_db)

        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else None

        ok = 0
        if action in STATUS_MAP:
            new_status = STATUS_MAP[action]
            for tid in task_ids:
                try:
                    db.update_task_status(tid, new_status)
                    try:
                        db.add_task_history(tid, my_db_id, 'status', None, new_status)
                    except Exception:
                        pass
                    ok += 1
                except Exception:
                    pass
        elif action == "delete":
            for tid in task_ids:
                try:
                    db.delete_task(tid)
                    ok += 1
                except Exception:
                    pass
        else:
            return RedirectResponse(url="/tasks?msg=error", status_code=303)

        return RedirectResponse(url=f"/tasks?msg=bulk_ok_{ok}", status_code=303)
    except Exception as e:
        logger.error("tasks_bulk: %s", e)
        return RedirectResponse(url="/tasks?msg=error", status_code=303)


# ─── EXCEL EXPORT ────────────────────────────────────────────────────────────

@router.get("/tasks/export")
def tasks_export_excel(request: Request, status: str = "", topic_id: int = 0,
                       assigned_filter: int = 0, shop_filter: str = "", q: str = ""):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from fastapi.responses import StreamingResponse
    import io, datetime as _dt

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/tasks", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=302)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment

        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = (my_row[1] or "") if my_row else ""

        tasks = db.get_tasks(
            status=status or None,
            topic_id=topic_id or None,
            assigned_to=assigned_filter if assigned_filter else None,
            shop_filter=shop_filter or None,
            is_admin=True,
            my_user_id=my_db_id,
            my_shop=my_shop or None,
            q=q.strip() or None,
        )

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Задачи"

        header_fill = PatternFill("solid", fgColor="4F46E5")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)

        headers = [
            ("ID", 6), ("Название", 40), ("Статус", 14), ("Приоритет", 12),
            ("Тема", 18), ("Исполнитель", 20), ("Магазин", 16),
            ("Срок", 14), ("Создана", 14), ("Описание", 50),
        ]
        for col, (h, w) in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

        ws.row_dimensions[1].height = 22

        status_ru = {"new": "Новая", "in_progress": "В работе", "review": "Проверка",
                     "done": "Выполнена", "cancelled": "Отменена"}
        priority_ru = {"low": "Низкий", "normal": "Обычный", "high": "Высокий", "urgent": "Срочный"}

        for i, t in enumerate(tasks, 2):
            assigned = ""
            if t.get("assign_all"):
                assigned = "Вся команда"
            elif t.get("assigned_shop"):
                assigned = t["assigned_shop"]
            elif t.get("assigned_name"):
                assigned = t["assigned_name"]

            deadline_str = t.get("deadline", "") or ""
            created_str = (t.get("created_at") or "")[:10]

            ws.cell(row=i, column=1, value=t.get("id"))
            ws.cell(row=i, column=2, value=t.get("title", ""))
            ws.cell(row=i, column=3, value=status_ru.get(t.get("status", ""), t.get("status", "")))
            ws.cell(row=i, column=4, value=priority_ru.get(t.get("priority", ""), t.get("priority", "")))
            ws.cell(row=i, column=5, value=t.get("topic_name") or "")
            ws.cell(row=i, column=6, value=assigned)
            ws.cell(row=i, column=7, value=t.get("assigned_shop") or "")
            ws.cell(row=i, column=8, value=deadline_str[:10] if deadline_str else "")
            ws.cell(row=i, column=9, value=created_str)
            ws.cell(row=i, column=10, value=(t.get("description") or "")[:500])

            if i % 2 == 0:
                row_fill = PatternFill("solid", fgColor="F8F7FF")
                for col in range(1, 11):
                    ws.cell(row=i, column=col).fill = row_fill

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        fname = f"tasks_{_dt.date.today().isoformat()}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        logger.error("tasks_export_excel: %s", e)
        return RedirectResponse(url="/tasks?msg=error", status_code=303)


# ─── ANALYTICS ───────────────────────────────────────────────────────────────

@router.get("/tasks/analytics")
def tasks_analytics(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db
    import json as _json

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    from billing_utils import has_module as _has_module
    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url="/tasks?msg=pro_required", status_code=302)

    ctx = {
        "request": request, "user": user, "is_admin": is_admin,
        "stats": {}, "error": None,
        "chart_status": "{}",
        "chart_topics_labels": [], "chart_topics_data": "[]",
        "chart_assignee_labels": [], "chart_assignee_data": "[]",
        "chart_priority_labels": "[]", "chart_priority_data": "[]",
        "chart_trend_labels": "[]",
        "chart_created_data": "[]", "chart_completed_data": "[]",
    }

    try:
        db = get_web_db(telegram_id, org_db)
        stats = db.get_tasks_analytics()
        ctx["stats"] = stats

        STATUS_RU = {
            'new': 'Новые', 'in_progress': 'В работе',
            'review': 'На проверке', 'done': 'Выполнены', 'cancelled': 'Отменены'
        }
        ctx["chart_status"] = _json.dumps(
            {STATUS_RU.get(k, k): v for k, v in stats.get('by_status', {}).items()}
        )

        by_topic = stats.get('by_topic', [])
        ctx["chart_topics_labels"] = [r[0] for r in by_topic]
        ctx["chart_topics_data"] = _json.dumps([r[1] for r in by_topic])

        by_assignee = stats.get('by_assignee', [])
        ctx["chart_assignee_labels"] = [r[0] for r in by_assignee]
        ctx["chart_assignee_data"] = _json.dumps([r[1] for r in by_assignee])

        PRIORITY_RU = {'urgent': '🔴 Критичный', 'high': '🟠 Высокий',
                       'medium': '🔵 Средний', 'low': '🟢 Низкий'}
        by_priority = stats.get('by_priority', [])
        ctx["chart_priority_labels"] = _json.dumps([PRIORITY_RU.get(r[0], r[0]) for r in by_priority])
        ctx["chart_priority_data"] = _json.dumps([r[1] for r in by_priority])

        # Trend: merge created + completed, fill gaps for last 30 days
        from datetime import date, timedelta
        all_days = [(date.today() - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
        created_map = dict(stats.get('created_daily', []))
        completed_map = dict(stats.get('completed_daily', []))
        ctx["chart_trend_labels"] = _json.dumps([d[5:] for d in all_days])  # MM-DD
        ctx["chart_created_data"] = _json.dumps([created_map.get(d, 0) for d in all_days])
        ctx["chart_completed_data"] = _json.dumps([completed_map.get(d, 0) for d in all_days])

    except Exception as e:
        logger.error("tasks_analytics: %s", e)
        ctx["error"] = "Ошибка загрузки аналитики."

    return request.app.state.templates.TemplateResponse(
        request, "tasks/analytics.html", ctx
    )


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

    from billing_utils import has_module as _has_module, has_extension as _has_ext
    tasks_pro = _has_module(telegram_id, 'tasks_pro')
    tasks_ai = tasks_pro and _has_ext(telegram_id, 'tasks_ai')

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
        "chat_available": _chat_available(telegram_id),
        "task_history": [],
        "my_reminders": [],
        "tasks_pro": tasks_pro, "tasks_ai": tasks_ai,
        "comments_text": "",
        "is_watching": False, "watcher_count": 0,
        "time_total_minutes": 0, "time_logs": [],
        "mention_users": [],
    }

    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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
                ctx["completions_map"] = {c['user_id']: c for c in completions}
                ctx["team_members_for_task"] = task_members
                ctx["completed_user_ids"] = completed_ids
            except Exception as _te:
                logger.error("task_detail team_completions: %s", _te)
                ctx["team_completions"] = []
                ctx["completions_map"] = {}
                ctx["team_members_for_task"] = []
                ctx["completed_user_ids"] = []
        else:
            ctx["team_completions"] = []
            ctx["completions_map"] = {}
            ctx["team_members_for_task"] = []
            ctx["completed_user_ids"] = []

        # Для сотрудников командных задач — показываем их коллег и общий прогресс (read-only)
        if not is_admin and (task.get('assign_all') or task.get('assigned_shop')):
            try:
                _assign_all_e = task.get('assign_all', False)
                _assigned_shop_e = task.get('assigned_shop', '')
                _completions_e = db.get_task_user_completions(task_id)
                _all_staff_e = _get_staff_list(db)
                _task_members_e = _all_staff_e if _assign_all_e else [
                    s for s in _all_staff_e if s.get('shop') == _assigned_shop_e
                ]
                ctx["team_members_for_task"] = _task_members_e
                ctx["completed_user_ids"] = [c['user_id'] for c in _completions_e]
                ctx["completions_map"] = {c['user_id']: c for c in _completions_e}
            except Exception:
                pass

        # My personal completion for team tasks (for employees)
        if not is_admin and (_assign_all or _assigned_shop):
            try:
                ctx["my_completion"] = db.get_task_user_completion(task_id, my_db_id)
            except Exception:
                ctx["my_completion"] = None
        else:
            ctx["my_completion"] = None

        # Task history (admin only or task creator)
        can_see_history = is_admin or (task.get('created_by') == my_db_id)
        if can_see_history:
            try:
                ctx["task_history"] = db.get_task_history(task_id, limit=30)
            except Exception:
                ctx["task_history"] = []
        else:
            ctx["task_history"] = []

        # My active reminders for this task
        if my_db_id and task.get('status') not in ('done', 'cancelled'):
            try:
                ctx["my_reminders"] = db.get_task_reminders_for_user(task_id, my_db_id)
            except Exception:
                ctx["my_reminders"] = []

        # Phase 4.2: Task watchers
        try:
            ctx["is_watching"] = db.is_watching_task(task_id, my_db_id) if my_db_id else False
            watcher_ids = db.get_task_watcher_db_ids(task_id)
            ctx["watcher_count"] = len(watcher_ids)
        except Exception:
            ctx["is_watching"] = False
            ctx["watcher_count"] = 0

        # Phase 4.3: Time tracking (tasks_pro gate)
        if tasks_pro:
            try:
                ctx["time_total_minutes"] = db.get_task_time_total(task_id)
                ctx["time_logs"] = db.get_task_time_logs(task_id)
            except Exception:
                ctx["time_total_minutes"] = 0
                ctx["time_logs"] = []
        else:
            ctx["time_total_minutes"] = 0
            ctx["time_logs"] = []

        # Build org user list for @mention autocomplete (4.1)
        try:
            _mention_conn = db.get_connection()
            try:
                _mention_rows = _mention_conn.execute(
                    "SELECT id, first_name, last_name, username FROM users WHERE telegram_id>0 LIMIT 200"
                ).fetchall()
            finally:
                _mention_conn.close()
            ctx["mention_users"] = [
                {"db_id": r[0], "name": f"{r[1] or ''} {r[2] or ''}".strip() or r[3] or str(r[0]),
                 "username": r[3] or ""}
                for r in _mention_rows
            ]
        except Exception:
            ctx["mention_users"] = []

        # Build comments_text for AI context (existing)
        try:
            ctx["comments_text"] = " | ".join(
                c.get("text", "") for c in ctx["comments"] if c.get("text")
            )[:2000]
        except Exception:
            ctx["comments_text"] = ""

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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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
            try:
                my_row = conn.execute(
                    "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
                ).fetchone()
            finally:
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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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

        old_status = task.get('status', '')
        db.update_task_status(task_id, status)
        try:
            db.add_task_history(task_id, my_db_id, 'status',
                                STATUS_LABELS.get(old_status, old_status),
                                STATUS_LABELS.get(status, status))
        except Exception:
            pass

        # Record per-user completion for team tasks
        if status in ('done', 'review') and (task.get('assign_all') or task.get('assigned_shop')):
            try:
                db.record_task_user_completion(task_id, my_db_id, status)
            except Exception:
                pass

        # Spawn next recurring task when done
        if status == 'done':
            try:
                import calendar as _cal
                from datetime import date as _date, timedelta as _td
                _recurrence = task.get('recurrence') or ''
                if _recurrence and _recurrence not in ('none', ''):
                    _old_dl = task.get('deadline') or ''
                    try:
                        _base = _date.fromisoformat(_old_dl[:10]) if _old_dl else _date.today()
                    except Exception:
                        _base = _date.today()
                    if _recurrence == 'daily':
                        _new_date = _base + _td(days=1)
                    elif _recurrence == 'weekly':
                        _new_date = _base + _td(weeks=1)
                    elif _recurrence == 'monthly':
                        _m = _base.month + 1
                        _y = _base.year + (_m - 1) // 12
                        _m = ((_m - 1) % 12) + 1
                        _md = _cal.monthrange(_y, _m)[1]
                        _new_date = _base.replace(year=_y, month=_m, day=min(_base.day, _md))
                    else:
                        _new_date = None
                    if _new_date:
                        if _old_dl and len(_old_dl) >= 13 and ("T" in _old_dl or " " in _old_dl[10:]):
                            _new_dl = _new_date.isoformat() + _old_dl[10:16]
                        else:
                            _new_dl = _new_date.isoformat()
                        _cl_items = [i.get('text', '') for i in (task.get('checklist') or []) if i.get('text', '').strip()]
                        db.create_task(
                            title=task['title'], description=task.get('description', ''),
                            topic_id=task.get('topic_id'), created_by=task.get('created_by', 0),
                            assigned_to=task.get('assigned_to'), assigned_shop=task.get('assigned_shop'),
                            assign_all=1 if task.get('assign_all') else 0,
                            priority=task.get('priority', 'normal'), deadline=_new_dl,
                            recurrence=_recurrence,
                            checklist=_cl_items or None,
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
                    f"<b>{_html.escape(task['title'])}</b>\n"
                    f"Новый статус: {_html.escape(status_text)}"
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
                    f"✅ <b>Задача выполнена</b>\n\n<b>{_html.escape(task['title'])}</b>"
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
        try:
            my_row = conn.execute(
                "SELECT id, shop_name FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = (my_row[1] or "") if my_row and len(my_row) > 1 else ""

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)
        if not is_admin:
            _can_comment = (
                task.get("assigned_to") == my_db_id
                or task.get("created_by") == my_db_id
                or task.get("assign_all")
                or (task.get("assigned_shop") and my_shop and task["assigned_shop"] == my_shop)
            )
            if not _can_comment:
                return RedirectResponse(url="/tasks", status_code=303)

        db.add_task_comment(task_id, my_db_id, text)

        # Уведомить создателя И исполнителя (кроме комментатора) — все 3 канала
        _c_notify: set = set()
        _c_creator = task.get("created_by")
        _c_assignee = task.get("assigned_to")
        if _c_creator and _c_creator != my_db_id:
            _c_notify.add(_c_creator)
        if _c_assignee and _c_assignee != my_db_id:
            _c_notify.add(_c_assignee)

        # Phase 4.2: также уведомить наблюдателей (watchers)
        try:
            _watcher_ids = db.get_task_watcher_db_ids(task_id)
            for _wid in _watcher_ids:
                if _wid != my_db_id:
                    _c_notify.add(_wid)
        except Exception:
            pass

        for _nid in _c_notify:
            _ntg = _get_user_tg_id(db, _nid)
            if _ntg:
                try:
                    db.add_notification_to_history(
                        _nid, "task_status",
                        f"💬 Новый комментарий к задаче «{task['title']}»"
                    )
                except Exception:
                    pass
                _send_tg_task_notify(
                    _ntg,
                    f"💬 <b>Новый комментарий</b>\n\nЗадача: <b>{_html.escape(task['title'])}</b>"
                )
                try:
                    from web.push_utils import send_web_push
                    send_web_push(_ntg, "💬 Новый комментарий",
                                  f"Задача: {task['title']}", f"/tasks/{task_id}")
                except Exception:
                    pass

        # Phase 4.1: @mentions — найти @username в тексте и уведомить
        import re as _re
        _mentions = _re.findall(r'@(\w+)', text)
        if _mentions:
            try:
                _conn_m = db.get_connection()
                try:
                    _m_rows = _conn_m.execute(
                        "SELECT id, username, telegram_id FROM users WHERE telegram_id>0"
                    ).fetchall()
                finally:
                    _conn_m.close()
                _username_map = {
                    (r[1] or "").lower(): (r[0], r[2]) for r in _m_rows if r[1]
                }
                for _mention in _mentions:
                    _ml = _mention.lower()
                    if _ml in _username_map:
                        _m_db_id, _m_tg = _username_map[_ml]
                        if _m_db_id != my_db_id and _m_tg:
                            try:
                                db.add_notification_to_history(
                                    _m_db_id, "task_status",
                                    f"🔔 Вас упомянули в задаче «{task['title']}»"
                                )
                            except Exception:
                                pass
                            _send_tg_task_notify(
                                _m_tg,
                                f"🔔 <b>Вас упомянули</b> в комментарии к задаче\n\n"
                                f"«{_html.escape(task['title'])}»\n"
                                f"<i>{_html.escape(text[:200])}</i>"
                            )
                            try:
                                from web.push_utils import send_web_push
                                send_web_push(_m_tg, "🔔 Вас упомянули",
                                              f"Задача: {task['title']}", f"/tasks/{task_id}")
                            except Exception:
                                pass
            except Exception as _me:
                logger.error("task_comment mentions: %s", _me)

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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0

        # Deny unresolved users
        if not my_db_id:
            return RedirectResponse(url="/tasks?msg=error", status_code=303)

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)
        if not is_admin:
            _my_shop_cl = ""
            try:
                _cl_conn = db.get_connection()
                _cl_row = _cl_conn.execute(
                    "SELECT shop_name FROM users WHERE id = ?", (my_db_id,)
                ).fetchone()
                _cl_conn.close()
                _my_shop_cl = (_cl_row[0] or "") if _cl_row else ""
            except Exception:
                pass
            _can_toggle = (
                task.get("assigned_to") == my_db_id
                or task.get("assign_all")
                or (task.get("assigned_shop") and _my_shop_cl and task["assigned_shop"] == _my_shop_cl)
            )
            if not _can_toggle:
                return RedirectResponse(url="/tasks", status_code=303)

        # IDOR guard: verify item_id belongs to this task_id
        item_conn = db.get_connection()
        try:
            item_row = item_conn.execute(
                "SELECT task_id FROM task_checklist WHERE id = ?", (item_id,)
            ).fetchone()
        finally:
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

@router.post("/tasks/{task_id}/rate")
def task_rate(request: Request, task_id: int,
              csrf_token: str = Form(""),
              rating: int = Form(0),
              rating_comment: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)
    if rating < 1 or rating > 5:
        return RedirectResponse(url=f"/tasks/{task_id}?msg=rate_invalid", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)
        db.rate_task(task_id, rating, rating_comment)

        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else None
        try:
            db.add_task_history(task_id, my_db_id, 'rated', None,
                                f"{'⭐' * rating} ({rating}/5)" + (f": {rating_comment}" if rating_comment.strip() else ""))
        except Exception:
            pass
        return RedirectResponse(url=f"/tasks/{task_id}?msg=rated_ok", status_code=303)
    except Exception as e:
        logger.error("task_rate: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)


@router.post("/tasks/{task_id}/remind")
def task_set_reminder(request: Request, task_id: int,
                      csrf_token: str = Form(""),
                      remind_in: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    import datetime as _dt

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        hours = float(remind_in) if remind_in else 0
        if hours <= 0 or hours > 720:
            return RedirectResponse(url=f"/tasks/{task_id}?msg=remind_invalid", status_code=303)

        db = get_web_db(telegram_id, org_db)
        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        if not my_row:
            return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)
        my_db_id = my_row[0]

        remind_at = (_dt.datetime.utcnow() + _dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        db.add_task_reminder(task_id, my_db_id, remind_at)
        try:
            db.add_task_history(task_id, my_db_id, 'remind_set', None,
                                f"через {remind_in}ч ({remind_at} UTC)")
        except Exception:
            pass
        return RedirectResponse(url=f"/tasks/{task_id}?msg=remind_ok", status_code=303)
    except Exception as e:
        logger.error("task_set_reminder: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)


@router.post("/tasks/{task_id}/duplicate")
def task_duplicate(request: Request, task_id: int, csrf_token: str = Form("")):
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
        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0

        cl_items = [i.get('text', '') for i in (task.get('checklist') or []) if i.get('text', '').strip()]
        new_title = f"[Копия] {task['title']}"
        new_id = db.create_task(
            title=new_title,
            description=task.get('description', ''),
            topic_id=task.get('topic_id'),
            created_by=my_db_id,
            assigned_to=task.get('assigned_to'),
            assigned_shop=task.get('assigned_shop'),
            assign_all=task.get('assign_all', 0),
            priority=task.get('priority', 'normal'),
            deadline=None,
            checklist=cl_items,
        )
        if new_id:
            try:
                db.add_task_history(new_id, my_db_id, 'created', None, f"{new_title} (скопирована из #{task_id})")
            except Exception:
                pass
        return RedirectResponse(url=f"/tasks/{new_id}/edit", status_code=303)
    except Exception as e:
        logger.error("task_duplicate: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)


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

    from billing_utils import has_module as _has_module, has_extension as _has_ext
    _tpro = _has_module(telegram_id, 'tasks_pro')
    _tai  = _tpro and _has_ext(telegram_id, 'tasks_ai')

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "topics": [], "staff_list": [], "shops_list": [],
        "priority_labels": PRIORITY_LABELS,
        "csrf_token": get_csrf_token(request),
        "edit_task": None, "error": None,
        "chat_available": _chat_available(telegram_id),
        "search_items_json": "[]", "init_selected_json": "[]", "init_assign_all": "false",
        "tasks_pro": _tpro, "tasks_ai": _tai,
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
    estimated_hours: str = Form(""),
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
        # Phase 4.3: estimated_hours
        try:
            _eh_e = float(estimated_hours.strip().replace(',', '.')) if estimated_hours.strip() else None
            _c_eh = db.get_connection()
            try:
                _c_eh.execute("UPDATE tasks SET estimated_hours=? WHERE id=?",
                               (_eh_e if _eh_e and _eh_e > 0 else None, task_id))
                _c_eh.commit()
            finally:
                _c_eh.close()
        except Exception:
            pass
        try:
            conn_ed = db.get_connection()
            ed_row = conn_ed.execute("SELECT id FROM users WHERE telegram_id = ?",
                                    (telegram_id,)).fetchone()
            conn_ed.close()
            _ed_uid = ed_row[0] if ed_row else None
            db.add_task_history(task_id, _ed_uid, 'edit', None, title)
            if old_task.get('priority') != priority:
                db.add_task_history(task_id, _ed_uid, 'priority',
                                    PRIORITY_LABELS.get(old_task.get('priority', ''), old_task.get('priority', '')),
                                    PRIORITY_LABELS.get(priority, priority))
        except Exception:
            pass

        # Уведомить исполнителя об изменениях задачи
        _deadline_str = f"\n📅 Срок: {_fmt_deadline(_deadline)}" if _deadline else ""
        if assign_mode == "person" and _assigned_to:
            tg_id = _get_user_tg_id(db, _assigned_to)
            if tg_id:
                if _assigned_to != old_task.get("assigned_to"):
                    # Новый исполнитель
                    try:
                        db.add_notification_to_history(
                            _assigned_to, "task_assigned", f"📋 Назначена задача: {title}"
                        )
                    except Exception:
                        pass
                    _send_tg_task_notify(
                        tg_id,
                        f"📋 <b>Вам назначена задача</b>\n\n"
                        f"<b>{_html.escape(title)}</b>"
                        f"{_deadline_str}\n\n"
                        f"🌐 Откройте веб-кабинет для подробностей."
                    )
                    try:
                        from web.push_utils import send_web_push
                        send_web_push(tg_id, "📋 Назначена задача", title, "/tasks")
                    except Exception:
                        pass
                else:
                    # Тот же исполнитель — задача обновлена
                    try:
                        db.add_notification_to_history(
                            _assigned_to, "task_assigned", f"📋 Задача обновлена: {title}"
                        )
                    except Exception:
                        pass
                    _send_tg_task_notify(
                        tg_id,
                        f"📋 <b>Задача обновлена</b>\n\n"
                        f"<b>{_html.escape(title)}</b>"
                        f"{_deadline_str}\n\n"
                        f"🌐 Откройте веб-кабинет для деталей."
                    )
                    try:
                        from web.push_utils import send_web_push
                        send_web_push(tg_id, "📋 Задача обновлена", title, "/tasks")
                    except Exception:
                        pass
        elif assign_mode == "shop" and _assigned_shop_val:
            _is_new_shop = _assigned_shop_val != old_task.get("assigned_shop")
            _sh_notif_msg = f"📋 Назначена задача: {title}" if _is_new_shop else f"📋 Задача обновлена: {title}"
            _sh_tg_hdr = "Вам назначена задача" if _is_new_shop else "Задача обновлена"
            for _sm_uid, _sm_tg in _get_shop_members_tg_ids(db, _assigned_shop_val):
                try:
                    db.add_notification_to_history(_sm_uid, "task_assigned", _sh_notif_msg)
                except Exception:
                    pass
                if _sm_tg:
                    _send_tg_task_notify(
                        _sm_tg,
                        f"📋 <b>{_html.escape(_sh_tg_hdr)}</b>\n\n<b>{_html.escape(title)}</b>{_deadline_str}\n\n"
                        f"🌐 Откройте веб-кабинет."
                    )
                    try:
                        from web.push_utils import send_web_push
                        send_web_push(_sm_tg, f"📋 {_sh_tg_hdr}", title, "/tasks")
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
        # Сначала удалить файлы вложений с диска
        try:
            attachments = db.get_task_attachments(task_id)
            for att in attachments:
                att_id = att["id"] if isinstance(att, dict) else att[0]
                ok, fpath = db.delete_task_attachment(att_id, telegram_id, is_admin=True)
                if ok and fpath:
                    import os as _os
                    try:
                        _os.remove(fpath)
                    except OSError:
                        pass
        except Exception as _ae:
            logger.warning("task_delete attachments cleanup: %s", _ae)
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
    report: str = Form(""),
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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
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

        # Сохранить текстовый отчёт как комментарий
        report_text = (report or "").strip()[:1000]
        if report_text:
            try:
                db.add_task_comment(task_id, my_db_id, f"📋 Отчёт: {report_text}")
            except Exception as _re:
                logger.warning("task_my_complete save report: %s", _re)

        # Авто-переход в «На проверку» когда все участники отметили выполнение
        try:
            _assign_all = task.get('assign_all', False)
            _assigned_shop = task.get('assigned_shop', '')
            if (_assign_all or _assigned_shop) and task.get('status') not in ('done', 'cancelled', 'review'):
                all_staff = _get_staff_list(db)
                task_members = all_staff if _assign_all else [
                    s for s in all_staff if s.get('shop') == _assigned_shop
                ]
                completions_list = db.get_task_user_completions(task_id)
                completed_ids = {c['user_id'] for c in completions_list}
                member_ids = {m['id'] for m in task_members}
                if member_ids and member_ids.issubset(completed_ids):
                    db.update_task_status(task_id, 'review')
        except Exception as _ae:
            logger.error("task_my_complete auto-advance: %s", _ae)

        # Уведомить создателя задачи — все 3 канала
        _creator_id = task.get("created_by")
        if _creator_id and _creator_id != my_db_id:
            _creator_tg = _get_user_tg_id(db, _creator_id)
            if _creator_tg:
                try:
                    _nc = db.get_connection()
                    try:
                        _nr = _nc.execute(
                            "SELECT first_name FROM users WHERE telegram_id = ?", (telegram_id,)
                        ).fetchone()
                    finally:
                        _nc.close()
                    _uname = (_nr[0] or "Сотрудник") if _nr else "Сотрудник"
                except Exception:
                    _uname = "Сотрудник"
                _cmsg = f"✅ «{task['title']}» — {_uname} выполнил(а)"
                try:
                    db.add_notification_to_history(_creator_id, "task_status", _cmsg)
                except Exception:
                    pass
                _send_tg_task_notify(
                    _creator_tg,
                    f"✅ <b>Задача отмечена выполненной</b>\n\n"
                    f"<b>{_html.escape(task['title'])}</b>\n{_html.escape(_uname)}"
                )
                try:
                    from web.push_utils import send_web_push
                    send_web_push(_creator_tg, "✅ Задача выполнена",
                                  f"«{task['title']}»: {_uname}", f"/tasks/{task_id}")
                except Exception:
                    pass

    except Exception as e:
        logger.error("task_my_complete: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)

    return RedirectResponse(url=f"/tasks/{task_id}?msg=done", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# ── Phase 4.2: Наблюдатели задачи (watch / unwatch) ─────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/tasks/{task_id}/watch")
def task_watch(request: Request, task_id: int, csrf_token: str = Form("")):
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
        if not db.get_task(task_id):
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0
        if my_db_id:
            db.add_task_watcher(task_id, my_db_id)
    except Exception as e:
        logger.error("task_watch: %s", e)
    return RedirectResponse(url=f"/tasks/{task_id}?msg=watching", status_code=303)


@router.post("/tasks/{task_id}/unwatch")
def task_unwatch(request: Request, task_id: int, csrf_token: str = Form("")):
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
        if not db.get_task(task_id):
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0
        if my_db_id:
            db.remove_task_watcher(task_id, my_db_id)
    except Exception as e:
        logger.error("task_unwatch: %s", e)
    return RedirectResponse(url=f"/tasks/{task_id}?msg=unwatched", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# ── Phase 4.3: Учёт времени (log time / delete time log) ─────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/tasks/{task_id}/log_time")
def task_log_time(
    request: Request,
    task_id: int,
    csrf_token: str = Form(""),
    hours: str = Form(""),
    minutes: str = Form(""),
    note: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module as _has_module

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    if not _has_module(telegram_id, 'tasks_pro'):
        return RedirectResponse(url=f"/tasks/{task_id}?msg=pro_required", status_code=303)

    try:
        h = int(hours or 0)
        m = int(minutes or 0)
        total_minutes = h * 60 + m
        if total_minutes <= 0:
            return RedirectResponse(url=f"/tasks/{task_id}?msg=bad_time", status_code=303)

        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0

        task = db.get_task(task_id)
        if not task:
            return RedirectResponse(url="/tasks?msg=not_found", status_code=303)

        db.log_task_time(task_id, my_db_id, total_minutes, note.strip()[:500])

    except Exception as e:
        logger.error("task_log_time: %s", e)
        return RedirectResponse(url=f"/tasks/{task_id}?msg=error", status_code=303)

    return RedirectResponse(url=f"/tasks/{task_id}?msg=time_logged", status_code=303)


@router.post("/tasks/{task_id}/log_time/{log_id}/delete")
def task_delete_time_log(
    request: Request,
    task_id: int,
    log_id: int,
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
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0
        db.delete_task_time_log(log_id, my_db_id, is_admin)
    except Exception as e:
        logger.error("task_delete_time_log: %s", e)

    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)
