---
name: Task automation & SLA engine (Phase 2)
description: How the Tasks-module automation rules + SLA escalation engine fires idempotently and where every creation/status hook must live.
---

# Task automation + SLA (module Задачи, Фаза 2)

`task_automation.py` is the single engine: `run_rules(db, event, task, actor_id)` + `sweep_sla_for_db(db)`.
Events: `status_changed`, `task_created`, `task_assigned`, `deadline_approaching`, `deadline_passed`.
Actions: `notify(target)`, `set_status`, `set_priority`, `add_comment`. Targets: assignee/creator/admins(manager)/team.

## Idempotency rules (test-covered)
- Time-based events (`deadline_*`) are deduped per `task_rule_firings.fire_key = "{rule_id}:{task_id}:{event}"` via `record_rule_firing` (INSERT OR IGNORE, rowcount==1 = first fire). Instant events (status/created/assigned) are NOT firing-keyed — they fire each real action.
- SLA escalation claimed atomically: `claim_task_escalation` (`UPDATE ... WHERE COALESCE(sla_escalated,0)=0`, rowcount==1). Sweep escalates once.
- `notify_recipients` dedups by `tg_id` (per-call `seen` set).

## Hooks — every task creation/status point MUST call run_rules
**Why:** the engine is only as complete as its call sites. Bot creation flows were initially missed → bot-created tasks silently skipped `task_created`/`task_assigned` while web worked.
**How to apply:** when adding ANY new task-create or status-change path (web route, bot handler, import, API), mirror the web pattern: after `create_task`, `db.get_task(task_id)` then `run_rules('task_created')` always + `run_rules('task_assigned')` when assigned_to/assigned_shop/assign_all. Status paths: build dict with `_old_status` + new `status`, call `run_rules('status_changed')` (skip when old==new). Easy-to-miss paths beyond the obvious edit/status routes: quick-add, bulk status, "my complete" auto-advance to review, bot reopen→in_progress. A regression test in `test_imports.py` greps each handler's source for `run_rules` — keep it updated when you add a handler. Find all sites: `rg "run_rules" web/routes/tasks.py tasks_handlers.py`.

## Recurring task spawn — single canonical helper at EVERY done-path
**Why:** spawning the next recurring task lived inline only in the web status-route + one bot path; closing a recurring task via kanban-drag, bulk action, or SLA `set_status` silently failed to create the next occurrence. The inline code also re-spawned on every re-close (no `old!=done` guard).
**How to apply:** never inline recurrence math. Call `task_automation.spawn_recurring_if_done(db, task, old_status, new_status)` — it gates on `old!=done && new==done`, computes the next date, copies checklist, and is idempotent per source task via `db.claim_recurrence_spawn(task_id)` (atomic `UPDATE ... WHERE COALESCE(recurrence_spawned,0)=0`, rowcount==1). The `recurrence_spawned` column is a guarded ALTER on `tasks`. Helper returns `assigned_to` (or None); web wraps notification in `_notify_recurring_spawn(db, assigned_to, title)`. Any NEW close-to-done path (web route, kanban, bulk, bot, automation) MUST call it. `task` here is the pre-update row, so `task['status']`/`old_status` is the OLD status. Regression test: "recurring spawn idempotency" in `test_imports.py`.

## Async-safety
In bot/scheduler `db` is the SYNC `Database` (no await on `db.get_task`/`update_task_status`). Web uses sync `db` too inside `def` routes. Web Push goes through `_push_send` → daemon thread (never sync `send_web_push` on the loop); scheduler calls sweep via `asyncio.to_thread`.

## Admin UI / robots
`/tasks/automation*` gated `has_module(...,'tasks_pro')` + role + CSRF; added to `_ROBOTS_TXT` Disallow. Templates use `data-confirm` (ds-delegate.js), no inline on* (strict CSP). HTMX live filters: `_list_fragment.html` returned when `HX-Request==true`.
