"""Task automation & SLA engine (Phase 2).

Pure, side-effecting helpers shared by the web layer, the bot and the
APScheduler jobs. Everything here is synchronous (the SQLite layer is sync);
Telegram and Web Push are dispatched on daemon threads so this module is safe
to call from async code without ever blocking the event loop or calling a sync
Web Push on the loop.

Events:  status_changed, task_created, task_assigned,
         deadline_approaching, deadline_passed
Actions: notify, set_status, set_priority, add_comment

Idempotency: time-based events (deadline_*) record a firing row keyed by
``rule_id:task_id:event`` so a rule never re-fires on every scheduler tick.
SLA escalation is claimed atomically via ``claim_task_escalation``.
"""
from __future__ import annotations

import os
import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

VALID_EVENTS = (
    'status_changed', 'task_created', 'task_assigned',
    'deadline_approaching', 'deadline_passed',
)
_TIME_EVENTS = ('deadline_approaching', 'deadline_passed')

VALID_ACTIONS = ('notify', 'set_status', 'set_priority', 'add_comment')

_PRIORITIES = ('urgent', 'high', 'normal', 'low')
_STATUSES = ('new', 'in_progress', 'review', 'done', 'cancelled')

_SLA_WARN_RATIO = 0.8


# ─── time helpers ────────────────────────────────────────────────────────────
def _utc_now() -> datetime:
    """Naive UTC now — matches SQLite datetime('now') stored strings."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(s: str | None) -> datetime | None:
    """Parse a stored UTC datetime/date string to naive datetime."""
    if not s:
        return None
    try:
        txt = str(s).strip()
        has_time = len(txt) >= 13 and ("T" in txt or " " in txt[10:])
        if has_time:
            return datetime.fromisoformat(txt[:19].replace("T", " "))
        return datetime.fromisoformat(txt[:10])
    except Exception:
        return None


# ─── SLA computation ─────────────────────────────────────────────────────────
def policies_by_priority(policies: list) -> dict:
    """[{priority, react_hours, resolve_hours, is_active}] → {priority: policy}."""
    out = {}
    for p in policies or []:
        if p.get("is_active", True):
            out[p.get("priority")] = p
    return out


def compute_sla(task: dict, policies_map: dict, now: datetime | None = None) -> str:
    """Return SLA state: 'none' | 'ok' | 'warning' | 'breached'.

    react window: applies while task is still 'new' (no reaction yet).
    resolve window: applies while task is not terminal.
    Both measured from created_at. Worst severity wins.
    """
    status = task.get("status") or "new"
    if status in ("done", "cancelled"):
        return "none"
    pol = policies_map.get(task.get("priority") or "normal")
    if not pol:
        return "none"
    created = _parse_dt(task.get("created_at"))
    if not created:
        return "none"
    now = now or _utc_now()
    elapsed_h = (now - created).total_seconds() / 3600.0
    if elapsed_h < 0:
        elapsed_h = 0.0

    severity = 0  # 0 none, 1 ok-active, 2 warning, 3 breached

    def _check(limit):
        nonlocal severity
        if not limit or limit <= 0:
            return
        if elapsed_h >= limit:
            severity = max(severity, 3)
        elif elapsed_h >= limit * _SLA_WARN_RATIO:
            severity = max(severity, 2)
        else:
            severity = max(severity, 1)

    if status == "new":
        _check(pol.get("react_hours"))
    _check(pol.get("resolve_hours"))

    return {0: "none", 1: "ok", 2: "warning", 3: "breached"}[severity]


# ─── condition matching ──────────────────────────────────────────────────────
def _load_json(s, default):
    try:
        v = json.loads(s) if isinstance(s, str) else s
        return v if v is not None else default
    except Exception:
        return default


def match_conditions(conditions: dict, task: dict) -> bool:
    """All listed conditions must hold (AND). Empty → always matches.

    Supported keys: topic_id, priority, from_status, to_status.
    from_status/to_status are checked against task['_old_status']/task['status'].
    """
    if not conditions:
        return True
    try:
        if conditions.get("topic_id"):
            if str(task.get("topic_id") or "") != str(conditions["topic_id"]):
                return False
        if conditions.get("priority"):
            if (task.get("priority") or "normal") != conditions["priority"]:
                return False
        if conditions.get("from_status"):
            if (task.get("_old_status") or "") != conditions["from_status"]:
                return False
        if conditions.get("to_status"):
            if (task.get("status") or "") != conditions["to_status"]:
                return False
        return True
    except Exception as e:
        logger.error("match_conditions: %s", e)
        return False


# ─── notification dispatch (dedup, thread-based, async-safe) ─────────────────
def _tg_send(tg_id: int, text: str) -> None:
    token = os.environ.get("BOT_TOKEN", "")
    if not token or not tg_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": tg_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[{"text": "✅ Прочитано", "callback_data": "notif_read"}]]
            },
        }).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"})
        threading.Thread(
            target=lambda: urllib.request.urlopen(req, timeout=10), daemon=True
        ).start()
    except Exception:
        pass


def _push_send(tg_id: int, title: str, body: str) -> None:
    """Web Push on a daemon thread — never blocks an event loop, never runs a
    sync push directly on the loop."""
    if not tg_id:
        return
    try:
        from web.push_utils import send_web_push
        threading.Thread(
            target=send_web_push,
            args=(int(tg_id), title, body, "/tasks"),
            daemon=True,
        ).start()
    except Exception:
        pass


def notify_recipients(db, recipients, tg_text: str, push_title: str,
                      push_body: str, bell_type: str, bell_text: str) -> int:
    """Send Telegram + bell history + Web Push to recipients, deduped by tg_id.

    recipients: iterable of (user_id|None, tg_id|None).
    Returns count of unique tg_ids actually notified.
    """
    seen = set()
    sent = 0
    for user_id, tg_id in recipients:
        try:
            tg_id = int(tg_id) if tg_id else 0
        except Exception:
            tg_id = 0
        if not tg_id or tg_id in seen:
            continue
        seen.add(tg_id)
        _tg_send(tg_id, tg_text)
        _push_send(tg_id, push_title, push_body)
        if user_id:
            try:
                db.add_notification_to_history(user_id, bell_type, bell_text)
            except Exception:
                pass
        sent += 1
    return sent


def _resolve_user_by_tg(db, tg_id):
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (tg_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _tg_for_user(db, user_id):
    if not user_id:
        return None
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT telegram_id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _recipients_for_target(db, target: str, task: dict) -> list:
    """Build [(user_id, tg_id)] for a notify action target."""
    out = []
    if target == "assignee":
        uid = task.get("assigned_to")
        if uid:
            out.append((uid, _tg_for_user(db, uid)))
    elif target == "creator":
        uid = task.get("created_by")
        if uid:
            out.append((uid, _tg_for_user(db, uid)))
    elif target in ("admins", "manager"):
        owner_tg = None
        try:
            owner_tg = db.get_org_owner_tg_id()
        except Exception:
            owner_tg = None
        if owner_tg:
            out.append((_resolve_user_by_tg(db, owner_tg), owner_tg))
        cuid = task.get("created_by")
        if cuid:
            out.append((cuid, _tg_for_user(db, cuid)))
    elif target == "team":
        try:
            if task.get("assign_all"):
                tg_ids = db.get_org_all_member_tg_ids()
            elif task.get("assigned_shop"):
                tg_ids = db.get_org_shop_member_tg_ids(task["assigned_shop"])
            else:
                tg_ids = []
            for tg in tg_ids:
                out.append((_resolve_user_by_tg(db, tg), tg))
        except Exception:
            pass
    return out


# ─── action execution ────────────────────────────────────────────────────────
def _next_recurrence_date(base, recurrence: str):
    """Следующая дата для повторяющейся задачи (без сторонних зависимостей)."""
    import calendar
    if recurrence == 'daily':
        return base + timedelta(days=1)
    if recurrence == 'weekly':
        return base + timedelta(weeks=1)
    if recurrence == 'monthly':
        month = base.month + 1
        year = base.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        max_day = calendar.monthrange(year, month)[1]
        return base.replace(year=year, month=month, day=min(base.day, max_day))
    return None


def spawn_recurring_if_done(db, task: dict, old_status: str, new_status: str):
    """Создать следующую повторяющуюся задачу при переходе задачи в ``done``.

    Единая точка для ВСЕХ путей закрытия (веб-маршрут статуса, канбан, массовое
    действие, бот, SLA-автоматизация). Идемпотентна: спавн происходит ровно один
    раз на задачу-источник (атомарный ``claim_recurrence_spawn``) и только на
    реальном переходе ``old != done && new == done``.

    Возвращает ``assigned_to`` созданной задачи (для уведомления) или ``None``.
    """
    from datetime import date
    if new_status != 'done' or old_status == 'done':
        return None
    recurrence = task.get('recurrence') or ''
    if not recurrence or recurrence in ('none', ''):
        return None

    task_id = task.get('id')
    # Идемпотентность: застолбить спавн ровно один раз на задачу-источник.
    if task_id is not None:
        try:
            if not db.claim_recurrence_spawn(task_id):
                return None
        except Exception as e:
            logger.warning("spawn_recurring_if_done claim: %s", e)
            return None

    old_deadline = task.get('deadline') or ''
    try:
        base = date.fromisoformat(old_deadline[:10]) if old_deadline else date.today()
    except Exception:
        base = date.today()

    new_date = _next_recurrence_date(base, recurrence)
    if new_date is None:
        return None

    if old_deadline and len(old_deadline) >= 13 and ("T" in old_deadline or " " in old_deadline[10:]):
        new_deadline = new_date.isoformat() + old_deadline[10:16]
    else:
        new_deadline = new_date.isoformat()

    checklist_items = None
    old_checklist = task.get('checklist') or []
    if old_checklist:
        checklist_items = [i.get('text', '') for i in old_checklist if i.get('text', '').strip()]

    assigned_to = task.get('assigned_to')
    try:
        db.create_task(
            title=task['title'],
            description=task.get('description', ''),
            topic_id=task.get('topic_id'),
            created_by=task.get('created_by', 0),
            assigned_to=assigned_to,
            assigned_shop=task.get('assigned_shop'),
            assign_all=1 if task.get('assign_all') else 0,
            priority=task.get('priority', 'normal'),
            deadline=new_deadline,
            recurrence=recurrence,
            checklist=checklist_items,
        )
    except Exception as e:
        logger.error("spawn_recurring_if_done create_task: %s", e)
        return None
    logger.info("spawn_recurring_if_done: '%s' → %s", task.get('title'), new_deadline)
    return assigned_to


def _apply_action(db, action: dict, task: dict, rule_name: str) -> str | None:
    """Execute one action. Returns a short summary for history, or None."""
    atype = action.get("type")
    task_id = task.get("id")
    if atype == "notify":
        target = action.get("target", "assignee")
        title = (task.get("title") or "—")
        custom = (action.get("text") or "").strip()
        body = custom or "Сработала автоматизация по задаче."
        tg_text = (f"🤖 <b>Автоматизация</b>\n<b>{_esc(title)}</b>\n\n{_esc(body)}")
        recips = _recipients_for_target(db, target, task)
        notify_recipients(
            db, recips, tg_text, "🤖 Автоматизация",
            f"{title}: {body}", "task_automation",
            f"🤖 {rule_name}: {title}")
        return f"уведомление → {target}"
    if atype == "set_status":
        val = action.get("value")
        if val in _STATUSES and val != task.get("status"):
            old = task.get("status")
            db.update_task_status(task_id, val)
            task["status"] = val
            try:
                db.add_task_history(task_id, None, "status", old, val)
            except Exception:
                pass
            try:
                spawn_recurring_if_done(db, task, old or "", val)
            except Exception as _re:
                logger.warning("set_status spawn recurring: %s", _re)
            return f"статус → {val}"
    if atype == "set_priority":
        val = action.get("value")
        if val in _PRIORITIES and val != task.get("priority"):
            try:
                conn = db.get_connection()
                try:
                    conn.execute(
                        "UPDATE tasks SET priority = ? WHERE id = ?", (val, task_id))
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                return None
            old = task.get("priority")
            task["priority"] = val
            try:
                db.add_task_history(task_id, None, "priority", old, val)
            except Exception:
                pass
            return f"приоритет → {val}"
    if atype == "add_comment":
        txt = (action.get("text") or "").strip()
        if txt:
            try:
                db.add_task_comment(task_id, 0, txt)
            except Exception:
                return None
            return "комментарий"
    return None


def _esc(s: str) -> str:
    import html as _html
    return _html.escape(str(s or ""))


# ─── public engine entry point ───────────────────────────────────────────────
def run_rules(db, event: str, task: dict, actor_user_id: int | None = None) -> int:
    """Evaluate & execute all active rules for *event* against *task*.

    For time-based events firing is idempotent (recorded per rule+task+event),
    so calling this every scheduler tick is safe. Returns number of rules fired.
    """
    if event not in VALID_EVENTS or not task or not task.get("id"):
        return 0
    fired = 0
    try:
        rules = db.get_active_rules_for_event(event)
    except Exception as e:
        logger.error("run_rules load (%s): %s", event, e)
        return 0
    for rule in rules:
        try:
            conds = _load_json(rule.get("conditions_json"), {})
            if not match_conditions(conds, task):
                continue
            if event in _TIME_EVENTS:
                fire_key = f"{rule['id']}:{task['id']}:{event}"
                if not db.record_rule_firing(rule["id"], task["id"], fire_key):
                    continue  # already fired — idempotent skip
            actions = _load_json(rule.get("actions_json"), [])
            summary = []
            for action in actions:
                if not isinstance(action, dict):
                    continue
                res = _apply_action(db, action, task, rule.get("name", "правило"))
                if res:
                    summary.append(res)
            try:
                db.add_task_history(
                    task["id"], actor_user_id, "automation",
                    rule.get("name", "правило"),
                    "; ".join(summary) if summary else "—")
            except Exception:
                pass
            fired += 1
        except Exception as e:
            logger.error("run_rules exec rule=%s: %s", rule.get("id"), e)
    return fired


# ─── SLA sweep (called by scheduler) ─────────────────────────────────────────
def sweep_sla_for_db(db, log=None) -> dict:
    """Recompute SLA status for all open tasks of one org DB; escalate newly
    breached tasks to the manager exactly once. Returns counters dict.

    Also fires deadline_approaching (within 24h) / deadline_passed rules
    idempotently via run_rules.
    """
    log = log or logger
    counters = {"checked": 0, "breached": 0, "escalated": 0, "rules": 0}
    try:
        policies = policies_by_priority(db.get_sla_policies(only_active=True))
    except Exception:
        policies = {}
    now = _utc_now()
    try:
        tasks = db.get_open_tasks_for_sla()
    except Exception as e:
        log.error("sweep_sla get tasks: %s", e)
        return counters
    for t in tasks:
        counters["checked"] += 1
        # ── deadline rule triggers ──
        try:
            dl = _parse_dt(t.get("deadline"))
            if dl:
                if now >= dl:
                    counters["rules"] += run_rules(db, "deadline_passed", dict(t))
                elif dl - now <= timedelta(hours=24):
                    counters["rules"] += run_rules(db, "deadline_approaching", dict(t))
        except Exception as e:
            log.error("sweep_sla deadline rules task=%s: %s", t.get("id"), e)
        # ── SLA status + escalation ──
        if not policies:
            continue
        try:
            new_status = compute_sla(t, policies, now)
            if new_status == "none":
                continue
            if t.get("sla_status") != new_status:
                db.set_task_sla_status(t["id"], new_status)
            if new_status == "breached":
                counters["breached"] += 1
                if int(t.get("sla_escalated") or 0) == 0 and db.claim_task_escalation(t["id"]):
                    _escalate(db, t)
                    counters["escalated"] += 1
        except Exception as e:
            log.error("sweep_sla status task=%s: %s", t.get("id"), e)
    return counters


def _escalate(db, task: dict) -> None:
    """Notify the manager (org owner + creator) that a task breached its SLA."""
    title = task.get("title") or "—"
    tg_text = (
        f"🚨 <b>Нарушение SLA</b>\n<b>{_esc(title)}</b>\n\n"
        f"Задача превысила установленный срок. Требуется вмешательство руководителя.\n"
        f"🌐 Откройте веб-кабинет для деталей."
    )
    recips = _recipients_for_target(db, "manager", task)
    # also include the assignee so they know it escalated
    uid = task.get("assigned_to")
    if uid:
        recips.append((uid, _tg_for_user(db, uid)))
    notify_recipients(
        db, recips, tg_text, "🚨 Нарушение SLA",
        f"Задача «{title}» нарушила SLA", "task_sla",
        f"🚨 SLA нарушен: {title}")
    try:
        db.add_task_history(task["id"], None, "sla_escalated", None, title)
    except Exception:
        pass
