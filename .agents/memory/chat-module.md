---
name: Internal org chat module
description: Architecture, constraints, and audit findings for the web chat feature (web/routes/chat.py + web/templates/chat/)
---

## Key facts

- Route file: `web/routes/chat.py` (9 routes)
- Template: `web/templates/chat/index.html` (Alpine.js, polling every 4s per topic)
- DB methods in `database.py`: add/get/since/latest_id/soft_delete + get_chat_topics, add_chat_topic, rename_chat_topic, archive_chat_topic
- Files stored in: `data/tenants/org_*_uploads/chat/YYYY-MM/`
- FAB: `base.html` bottom of body, `lg:hidden`, draggable+edge-snap, badge via `/chat/poll` every 10s

## Critical: subscription lookup uses shop_bot.db

`_get_org_active_plan(telegram_id)` reads `subscriptions` and `users` from `shop_bot.db` (NOT from org_*.db).
Org DBs don't have subscriptions. Always use `_SHOP_BOT_DB = "data/shop_bot.db"` for plan checks.

**Why:** Subscriptions are centralised in shop_bot.db; org DBs only hold org-specific data.

## Context keys in chat route

The route's `ctx` dict uses:
- `my_db_id` (NOT `user.db_id`) — internal users.id for message ownership
- `topics`, `current_topic_id`, `current_topic_name` — for topics tab bar
- `min_plan`, `org_plan`, `chat_allowed`, `messages`, `latest_id`, `error`
- `csrf_token`, `user`, `request` (standard)

`my_db_id` defaults to 0; template uses `{% if msg.user_id == my_db_id %}` for own-message styling.

## Rate limiting

- `/chat/send`: 30 msg/min per telegram_id (`_SEND_RATE_STORE`, in-memory, resets on restart)
- `/chat/poll` + `/chat/topics/{id}/messages`: 60 req/min per IP (`_POLL_RATE_STORE`)
- `/chat/topics/create`: 5 topics/hour per telegram_id (`_TOPIC_RATE_STORE`)

All three share a unified `_rate_ok(store, key, limit, window)` helper.

## Settings gating

`payment_settings` table in `shop_bot.db`, key=`chat_min_plan`.
Values: `Отключён` | `Бесплатный` | `Базовый` (default) | `Стандарт` | `Премиум`.
Super_admin changes it via `POST /settings/chat-plan`.
Jinja2 global `chat_enabled()` in `web/app.py` controls sidebar link + FAB visibility.

## Topics schema

- `chat_topics` table in org_*.db: `id, name, created_by, created_at, is_archived, sort_order`
- Default topic "Общий" always `id=1`, auto-inserted via `INSERT OR IGNORE` in `create_tables()`
- `chat_messages.topic_id INTEGER DEFAULT 1` — added via ALTER TABLE migration guard
- Topic id=1 ("Общий") is protected: cannot be renamed or archived (hard-coded guard in DB methods)

## Alpine.js chatApp() state

Key state variables: `currentTopicId`, `currentTopicName`, `topicsList[]`, `dynamicMsgs[]`,
`topicSwitched` (bool — hides SSR messages once topics are switched or msg sent),
`loadingTopic`, `latestId`.

Topic switch: `GET /chat/topics/{id}/messages` → replace `dynamicMsgs`, restart polling.
No page reload — URL updated via `history.replaceState`.

Per-topic unread tracking: `localStorage ds_chat_seen_{topicId}` stores last seen msg id.

## Security

- Path traversal: `os.path.abspath` + `startswith(uploads_dir)` on file serve
- MIME allowlist: `ALLOWED_MIME_PREFIXES` tuple
- Max file size: 20 MB
- CSRF on all 5 POST routes
- Auth check on all 9 routes
- `/chat` in robots.txt Disallow
