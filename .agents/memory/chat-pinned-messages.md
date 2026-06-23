---
name: Chat pinned messages
description: Закреплённые сообщения в групповом чате — где живут колонки, почему только группы, конвенция порядка колонок в feed-запросах
---

# Закреплённые сообщения чата

ТОЛЬКО групповые темы (`chat_messages`). `direct_messages` НЕ имеет pin-колонок → DM-закреп намеренно отложен.

## Конвенция порядка колонок (durable gotcha)
`get_chat_messages` и `get_chat_messages_since` оба заканчиваются на `... m.is_pinned, m.edited_at, m.reply_to_id`.
→ В `_fmt_msg` хвост читается ТОЛЬКО отрицательными индексами: `pinned=row[-3]`, `edited=row[-2]`, `reply=row[-1]`.

**Why:** две feed-функции исторически расходились в середине SELECT; AI-windowing (`_fetch_ai_chat_history`, `_maybe_compress_chat_session`) и `_fmt_search_result(row[:11])` индексируют ТОЛЬКО положительные колонки 0–10. Любую новую хвостовую колонку добавлять ПЕРЕД `edited_at` в ОБОИХ запросах, чтобы отрицательные индексы остались стабильны и положительные потребители не сломались.

**How to apply:** новая колонка в ленту → вставлять в оба SELECT перед `m.edited_at`; сдвигать существующие `row[-N]` в `_fmt_msg`; НЕ трогать положительные индексы AI-путей.

## Права и realtime
- pin-роут: admin/owner ИЛИ автор; CSRF; `pinned ∈ {0,1}`.
- Бар закрепа отдаётся в ctx страницы + `chat_poll`/`chat_topic_messages` JSON (`_topic_pinned_bar`, hydrate файлов через `_load_msg_files_bulk`).
- Sync-роут (как edit/delete/react) → другие видят закреп/откреп в течение 4-сек poll, без WS-броадкаста.
- Клиент: `pinnedMsg` синхронизируется в `poll()` и `switchTopic()` через ЛОКАЛЬНУЮ переменную (не shared `this._*`) — иначе race при быстром переключении тем покажет чужой закреп.
