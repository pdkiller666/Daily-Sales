---
name: Internal org chat module
description: Architecture, constraints, and audit findings for the web chat feature (web/routes/chat.py + web/templates/chat/)
---

## Key facts

- Route file: `web/routes/chat.py` (5 routes)
- Template: `web/templates/chat/index.html` (Alpine.js, polling every 4s)
- DB methods in `database.py`: `add_chat_message`, `get_chat_messages`, `get_chat_messages_since`, `get_chat_latest_id`, `soft_delete_chat_message`
- Files stored in: `data/tenants/org_*_uploads/chat/YYYY-MM/`
- FAB: `base.html` bottom of body, `lg:hidden`, draggable+edge-snap, badge via `/chat/poll` every 10s

## Critical: subscription lookup uses shop_bot.db

`_get_org_active_plan(telegram_id)` reads `subscriptions` and `users` from `shop_bot.db` (NOT from org_*.db).
Org DBs don't have subscriptions. Always use `_SHOP_BOT_DB = "data/shop_bot.db"` for plan checks.

**Why:** Subscriptions are centralised in shop_bot.db; org DBs only hold org-specific data.

## Context keys in chat route

The route's `ctx` dict uses:
- `my_db_id` (NOT `user.db_id`) — internal users.id for message ownership
- `min_plan`, `org_plan`, `chat_allowed`, `messages`, `latest_id`, `error`
- `csrf_token`, `user`, `request` (standard)

`my_db_id` defaults to 0; template uses `{% if msg.user_id == my_db_id %}` for own-message styling.

## Rate limiting

- `/chat/send`: 30 msg/min per telegram_id (`_SEND_RATE_STORE`, in-memory, resets on restart)
- `/chat/poll`: 60 req/min per IP (`_POLL_RATE_STORE`, in-memory)

## Settings gating

`payment_settings` table in `shop_bot.db`, key=`chat_min_plan`.
Values: `Отключён` | `Бесплатный` | `Базовый` (default) | `Стандарт` | `Премиум`.
Super_admin changes it via `POST /settings/chat-plan`.
Jinja2 global `chat_enabled()` in `web/app.py` controls sidebar link + FAB visibility.

## Security

- Path traversal: `os.path.abspath` + `startswith(uploads_dir)` on file serve
- MIME allowlist: `ALLOWED_MIME_PREFIXES` tuple
- Max file size: 20 MB
- CSRF on both POST routes
- Auth check on all 5 routes

## Topics feature (planned)

Next enhancement: `chat_topics` table + `topic_id` in `chat_messages`.
Default topic "Общий" (id=1) auto-created. UI: horizontal tabs above message area.
