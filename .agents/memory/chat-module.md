---
name: Internal org chat module
description: Architecture, constraints, and audit findings for the web chat feature (web/routes/chat.py + web/templates/chat/) including Direct Messages (DM)
---

## Key facts

- Route file: `web/routes/chat.py` (~27 routes total: topics + DM + search + files + WS)
- Template: `web/templates/chat/index.html` (Alpine.js, unified `chatApp()` for both topics and DM)
- DB methods: topic (add/get/since/latest_id/soft_delete + get/add/rename/archive topics) + DM (see below)
- Files stored in: `data/tenants/org_*_uploads/chat/YYYY-MM/` (shared for both topics and DM)
- FAB: `base.html`, badge = `topicNew + dmUnread` (see FAB section below)

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

## Direct Messages (DM)

### DB schema
`direct_messages` table in org_*.db:
`id, from_user_id, to_user_id, message, file_path, file_name, file_type, file_size, created_at, is_read, is_deleted`

Peer identity: `CASE WHEN from_user_id = me THEN to_user_id ELSE from_user_id END AS peer_id`

### DM DB methods (9 core + 4 file)
- `add_dm(from_user_id, to_user_id, message, ...)` → dm_id
- `get_dm_conversation(user_a, user_b, limit, before_id)` → list[tuple] (15 cols)
- `get_dm_contacts(user_id)` → list with unread count per peer
- `get_dm_org_members(exclude_user_id)` → all org members except self
- `mark_dm_read(viewer_id, from_user_id)` → marks is_read=1
- `get_dm_unread_count(user_id)` → total unread DM count
- `get_dm_message(msg_id)` → single row or None
- `soft_delete_dm(msg_id, user_id, is_admin)` → bool
- `search_dm_messages(query, user_id, limit)` → list (both sides of conversation)
- Files: `add_dm_files`, `get_dm_files_bulk`, `get_dm_file`, `delete_dm_files`

### DM routes in chat.py
- `GET /chat/dm` — contacts page (SSR, shows all DM contacts)
- `GET /chat/dm/{peer_id}` — conversation page (SSR)
- `GET /api/dm/contacts` — JSON contacts list with unread counts
- `GET /api/dm/members` — JSON org members for "new DM" picker
- `GET /api/dm/conversation/{peer_id}` — JSON message history (paginated)
- `POST /chat/dm/send` — send DM (CSRF, rate limited)
- `POST /chat/dm/{msg_id}/delete` — soft-delete (CSRF)
- `GET /chat/dm/file/{msg_id}` — serve DM file
- `GET /chat/dm/file/attachment/{att_id}` — serve DM file attachment
- `WS /ws/chat/dm` — WebSocket for read-receipts + real-time messages

### Alpine.js DM state
Key variables: `dmMode` (bool), `dmContacts[]`, `dmMembers[]`, `dmMessages[]`,
`dmPeerId`, `dmPeerName`, `dmTotalUnread`, `dmWs` (WebSocket).

Methods: `switchToDm()` / `switchToGroup()` (toggle mode),
`openDmConversation(contact)` (load history + connect WS),
`dmSend()` (POST + optimistic append), `fetchDmContacts()`.

**goToResult(r) for DM search**: switches to DM mode, waits for contacts load, calls `openDmConversation` matching peer_id.

## Chat search (global mode — topic_id=0)

When `topic_id=0`, `chat_search` route queries both topic messages and DM messages,
merges results sorted by `created_at DESC`, returns up to 30 items.

- Topic results: `result_type: 'topic'`, shown with `#ТемаName` badge (blue)
- DM results: `result_type: 'dm'`, `dm_peer_id`, `dm_peer_name` — shown with «💬 Личное» badge (purple)

`search_dm_messages()` uses `LOWER(message) LIKE LOWER(?)` — same case-insensitive approach as topic search.

## FAB badge (base.html)

Two independent counters combined: `topicNew` (from `/chat/poll`) + `dmUnread` (from `/api/unread-count .dms`).
- `fetchDmUnread()` called on page load + every 60s
- `_maybeClearChatBadge()` runs on `DOMContentLoaded`: if `location.pathname.startsWith('/chat')`, resets both to 0
- Badge shows `topicNew + dmUnread`; hidden when both are 0

## Security

- Path traversal: `os.path.abspath` + `startswith(uploads_dir)` on file serve
- MIME allowlist: `ALLOWED_MIME_PREFIXES` tuple
- Max file size: 20 MB
- CSRF on all POST routes (topic and DM)
- Auth check on all routes
- `/chat` and `/chat/dm` in robots.txt Disallow
