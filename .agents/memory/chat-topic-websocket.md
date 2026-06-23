---
name: Group-chat topic WebSocket (live delivery)
description: Why group topics use a WS "poke" + poll instead of broadcasting formatted messages
---

# Group-chat topic live delivery (poke-and-poll)

Group topics get near-instant delivery via a topic WebSocket, but the WS only
carries a lightweight **poke** `{type:'new', topic_id, latest_id}` — never the
formatted message. The receiving client triggers an immediate (debounced ~120ms)
`poll()` which returns the messages.

**Why not broadcast the message itself:** the poll/`_fmt_msg` path formats
messages PER-USER (`is_mine`/`can_delete`/`can_edit` depend on the recipient's
`user_db_id`, and admins can delete others'). Broadcasting the sender-formatted
message would give every recipient the sender's flags (e.g. `is_mine=true`).
Re-formatting per recipient in the broadcast loop is the alternative, but poking
+ reusing the access-controlled poll path is simpler and avoids per-recipient bugs.

**How to apply:**
- `TopicConnectionManager` (web/ws_manager.py): rooms keyed `(org_db, topic_id)`,
  `join()` moves a socket between rooms (one socket per user), `broadcast(exclude_user)`.
- Only the **async** `chat_send` route pokes. Group edit/delete/react routes are
  sync `def` (no event loop to await a broadcast) → they intentionally stay on the
  4s poll. If you ever need instant edit/delete/react, convert those routes to
  `async` first, then add a poke.
- Client (chat/index.html): `topicWsConnect()` joins current topic on open and does
  an immediate poll on (re)connect to catch messages missed during a WS drop; poll
  remains the 4s fallback. `switchTopic()` must call `topicJoin(newTopicId)`.
- `handleTopicWsMsg` ignores pokes when `dmMode` or for a non-current topic.

DM uses a different model (full message over WS, `dm_manager`) — don't conflate.
