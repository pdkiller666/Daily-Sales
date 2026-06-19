---
name: Tasks module architecture
description: Ключевые решения модуля задач — структура БД, интеграция в систему, паттерны
---

## Tables (org_*.db)
- `task_topics` — категории (id, name, color, sort_order, created_by)
- `tasks` — задачи (assigned_to, created_by, shop_id, priority, status, deadline, linked_chat_topic_id, recurrence)
- `task_checklist` — пункты чеклиста (task_id, text, is_done, done_by, done_at)
- `task_comments` — комментарии (task_id, user_id, text)
- `task_attachments` — вложения (task_id, file_path, file_name, file_type, file_size, uploaded_by)
- `task_user_completions` — персональные отметки «Я выполнил» (task_id, user_id, status, completed_at) — UNIQUE(task_id, user_id)

## Statuses flow
new → in_progress → review → done (admin also: any → cancelled)
При 100% выполнении командной задачи → auto update_task_status(task_id, 'review')

## Assignment modes
- `assigned_to` — конкретный user (id из users)
- `assigned_shop` — по магазину (shop_name)
- `assign_all` — всей команде
- Мульти-получатель: отдельная задача для каждого (создаётся в роуте /tasks/new)

## Team progress (assign_all / assigned_shop)
- Admin видит: список всех участников + ✅/⏳ + время выполнения (completed_at)
- Сотрудник видит: тот же список (read-only, без admin guard с v1.116.0)
- `completions_map` в контексте = dict{user_id: {user_id, status, completed_at, name}}
- `team_members_for_task` = все сотрудники (assign_all) или только магазина
- Авто-переход в review: `member_ids.issubset(completed_ids)` в task_my_complete

## _is_overdue rule (v1.116.0 fix)
- Если deadline содержит время → сравнивать datetime.now(), не date.today()
- Если только дата → сравнивать date.today()
- Симптом старого бага: задача с дедлайном «сегодня 23:00» не помечалась просроченной до следующего дня

## Integration points
- `web/routes/tasks.py` — 14+ эндпоинтов; include_router в web/app.py
- `tasks_handlers.py` — бот; `tasks_bot_router` зарегистрирован в main.py
- `database.py` — 14+ методов (create_task, get_tasks, get_task, update_task_status, update_task, delete_task, add_task_comment, get_task_comments, toggle_task_checklist_item, get_open_tasks_count, get_tasks_with_deadline_today, get_overdue_tasks, create_task_topic, get_task_topics, delete_task_topic, record_task_user_completion, get_task_user_completions, get_task_user_completion)
- `web/app.py` — Jinja2 global `open_tasks_count(request)` для счётчика в сайдбаре (indigo badge)
- `base.html` — сайдбар (после Конкурсы), «Ещё» шторка, PATH_MAP, hints SECTION_HINTS, notification icons (📋)

## APScheduler (main.py)
- `check_task_deadlines` — CronTrigger(hour=9, minute=10), ежедневно

## robots.txt
- Добавлено `Disallow: /tasks` в `_ROBOTS_TXT` в web/app.py

**Why:** Задачи живут в org_*.db (не в shop_bot.db) — данные изолированы по организации. completions_map передаётся в оба view (admin + employee) для единого шаблона без дублирования.
**How to apply:** При добавлении нового маршрута /tasks/X — добавить Disallow в robots.txt. open_tasks_count вызывается на КАЖДОЙ странице — держать O(1) запрос.
