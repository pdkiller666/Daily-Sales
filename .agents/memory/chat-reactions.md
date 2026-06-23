---
name: Chat message reactions
description: Emoji reactions design for group topics (poll) + DM (WebSocket) in the corporate chat
---

# Реакции на сообщения чата

Эмодзи-реакции зеркалятся в двух поверхностях: групповые темы (`chat_messages`, sync через HTTP polling `/chat/poll`) и ЛС (`direct_messages`, sync через WebSocket `dm_manager`). Таблицы `message_reactions` / `dm_reactions` в каждой `org_*.db` с `UNIQUE(message/dm, user, emoji)`.

## Ключевые решения
- **Снимок реакций в poll, а не дельта.** `get_recent_chat_reactions` возвращает ВСЕ id сообщений из недавнего окна темы, включая пустой `[]`. Клиент в `poll()` перезаписывает `m.reactions` ТОЛЬКО для id, присутствующих в map (`hasOwnProperty`), и не трогает остальные. **Why:** снятие реакции — это исчезновение строки; без явного `[]` для затронутых сообщений клиент не узнал бы об удалении. Строгий «снимок окна» проще и идемпотентен.
- **DM realtime через WS `{type:'reaction', id, reactions}`** обоим участникам (`row[1]` from, `row[2]` to), причём `mine` пересчитывается per-recipient (у каждого свой флаг). Группы realtime не имеют (WS тем — задача T5), поэтому опираются на poll.
- **Toggle идемпотентен и защищён от гонки.** `toggle_message_reaction`/`toggle_dm_reaction`: сначала проверка существования НЕ-удалённого сообщения (`COALESCE(is_deleted,0)=0`), затем select-then-insert/delete; INSERT в `try/except sqlite3.IntegrityError` → при гонке (двойной клик/мультивкладка) UNIQUE-нарушение трактуется как «уже стоит». **Why:** read-then-write гонка иначе → 500.
- **Whitelist эмодзи** `ALLOWED_REACTIONS` в chat.py + rate-limit `_react_rate_ok` (60/min, ключ по telegram_id как и остальной чат за прокси). CSRF на обоих POST.
- **dm.html — deprecated `_legacy` шаблон**; primary `/chat/dm*` редиректят на `/chat?dm=1` (index.html). Новые чат-фичи делать в `index.html`, legacy не трогать.

## Формат
`_fmt_msg`/`_fmt_dm` отдают `reactions: [{emoji, count, mine}]` (bulk-загрузка как у файлов). Новые сообщения из poll/WS должны иметь дефолт (UI терпит `undefined` через `m.reactions && m.reactions.length`).
