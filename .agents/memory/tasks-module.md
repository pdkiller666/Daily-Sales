---
name: Tasks module architecture
description: Ключевые решения модуля задач — структура БД, интеграция в систему, паттерны
---

## Tables (org_*.db)
- `task_topics` — категории (id, name, color, sort_order, created_by)
- `tasks` — задачи (assigned_to, created_by, shop_id, priority, status, deadline, linked_chat_topic_id)
- `task_checklist` — пункты чеклиста (task_id, text, is_done, done_by, done_at)
- `task_comments` — комментарии (task_id, user_id, text)

## Statuses flow
new → in_progress → review → done (admin also: any → cancelled)

## Integration points
- `web/routes/tasks.py` — 12 эндпоинтов; include_router в web/app.py
- `tasks_handlers.py` — бот; `tasks_bot_router` зарегистрирован в main.py
- `database.py` — 14 методов (create_task, get_tasks, get_task, update_task_status, update_task, delete_task, add_task_comment, get_task_comments, toggle_task_checklist_item, get_open_tasks_count, get_tasks_with_deadline_today, get_overdue_tasks, create_task_topic, get_task_topics, delete_task_topic)
- `web/app.py` — Jinja2 global `open_tasks_count(request)` для счётчика в сайдбаре (indigo badge)
- `web/routes/api.py` — `_notif_url`: task_assigned/task_status/task_deadline/task_overdue → `/tasks`
- `base.html` — сайдбар (после Конкурсы), «Ещё» шторка, PATH_MAP, hints SECTION_HINTS, notification icons (📋)

## APScheduler (main.py)
- `check_task_deadlines` — CronTrigger(hour=9, minute=10), ежедневно; итого 11 джобов

## robots.txt
- Добавлено `Disallow: /tasks` в `_ROBOTS_TXT` в web/app.py

**Why:** Задачи живут в org_*.db (не в shop_bot.db) — данные изолированы по организации, как и остальные рабочие данные.
**How to apply:** При добавлении нового маршрута /tasks/X — добавить Disallow в robots.txt. open_tasks_count вызывается на КАЖДОЙ странице — держать O(1) запрос.
