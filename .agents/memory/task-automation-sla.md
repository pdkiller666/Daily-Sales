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
**How to apply:** when adding ANY new task-create or status-change path (web route, bot handler, import, API), mirror the web pattern: after `create_task`, `db.get_task(task_id)` then `run_rules('task_created')` always + `run_rules('task_assigned')` when assigned_to/assigned_shop/assign_all. Status paths: build dict with `_old_status` + new `status`, call `run_rules('status_changed')`.
Known call sites: web `tasks_new_post`/status routes; bot `tsk_c_ok`, `tsk_ai_ok` (create), status handler (~line 624). Scheduler `sweep_sla_for_db` fires deadline_* + escalation.

## Async-safety
In bot/scheduler `db` is the SYNC `Database` (no await on `db.get_task`/`update_task_status`). Web uses sync `db` too inside `def` routes. Web Push goes through `_push_send` → daemon thread (never sync `send_web_push` on the loop); scheduler calls sweep via `asyncio.to_thread`.

## Admin UI / robots
`/tasks/automation*` gated `has_module(...,'tasks_pro')` + role + CSRF; added to `_ROBOTS_TXT` Disallow. Templates use `data-confirm` (ds-delegate.js), no inline on* (strict CSP). HTMX live filters: `_list_fragment.html` returned when `HX-Request==true`.
