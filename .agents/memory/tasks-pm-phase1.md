---
name: Tasks Phase 1 PM foundation
description: Subtask hierarchy, task dependencies/blockers, saved views, inline edit — key gotchas
---

# Tasks Phase 1 — project management foundation

## Subtask hierarchy
- `parent_task_id` added via guarded ALTER (not in CREATE TABLE)
- `create_task()` accepts `parent_task_id: int | None = None` → inserted into SQL
- `get_subtask_progress(parent_id)` returns `(done, total)` — order is done FIRST, total SECOND
  - Template: `subtask_progress[0]` = done, `subtask_progress[1]` = total
  - `{% if subtask_progress[1] > 0 %}` checks total > 0

**Why:** First impl had (total, done) order; template was written for (done, total) → off-by-one progress bar.

## Task dependencies (blockers)
- Table: `task_dependencies(blocker_id, blocked_id)` — the blocker BLOCKS the blocked task
- `add_task_blocker(blocker_id, blocked_id)`: blocker_id is the UPSTREAM task
- `get_task_blockers(task_id)` → tasks that block THIS task (td.blocked_id = task_id)
- `get_task_blocking(task_id)` → tasks that THIS task blocks (td.blocker_id = task_id)
- `is_blocked` flag in `get_tasks()`: EXISTS(SELECT 1 FROM task_dependencies WHERE blocked_id = t.id)

**How to apply:** When calling `add_task_blocker(A, B)` = "A blocks B". To find what blocks a task: `get_task_blockers(task_id)`.

## Saved views
- `task_saved_views(user_id, name, filters_json)` in org_*.db
- `create_task_saved_view(user_id, name, filters_json)` returns view_id
- `delete_task_saved_view(view_id, user_id)` — user_id guard prevents deletion of others' views
- `GET /tasks?view_id=N` loads saved filters; `POST /tasks/views/save` creates; `POST /tasks/views/{id}/delete` removes
- `Disallow: /tasks/views/` added to robots.txt

## Inline editing (index.html)
- `data-inline-title="{{ t.id }}"` on task title span
- dblclick listener in nonce JS block replaces span with input, POSTs to `/tasks/{id}/inline-edit` (JSONResponse)
- `_CSRF_INDEX = {{ csrf_token | tojson | forceescape }}` — forceescape required for Alpine attrs
- `showSaveView`/`closeSaveView` are window-level functions called by ds-delegate.js via data-action

## Quick-add form
- `POST /tasks/quick-add` — requires title; optional topic_id, priority; 303 redirect to /tasks?msg=created_1
- Form placed ABOVE the task list, BELOW the bulk action bar `{% endif %}`
- topic_id pre-filled via hidden field if topic_filter is active

## Parent task in new task form
- GET /tasks/new?parent_id=N → ctx["prefill_parent_task_id"]=N, ctx["prefill_parent_task"]=db.get_task(N)
- form.html: hidden input + banner "Подзадача для: <parent.title>"
- POST /tasks/new: parent_task_id Form param → _parent_task_id → create_task(parent_task_id=...)
- "Добавить подзадачу" link in detail.html goes to /tasks/new?parent_id={{ task.id }}
