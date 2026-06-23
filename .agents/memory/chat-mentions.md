---
name: Chat @mentions
description: How @mentions work across group topics and DM in the corporate chat (parsing, targeted push, autocomplete, XSS-safe render)
---

# @упоминания в корпоративном чате

Зеркалится в группах (chat_messages, poll) и ЛС (direct_messages, WS).

- **Резолв** на сервере: `_parse_mention_tids(text, members, exclude_tid)` по `_MENTION_RE = (?:^|\s)@([\w\u0400-\u04ff]+)`; handle = username(lower, без @) иначе first_name(lower, без пробелов) через `_mention_handle()`. Участники — `db.get_chat_mention_members()`.
- **Push split (не дублировать!)**: упомянутым — адресный «📣 Вас упомянули…»; остальным членам — общий «💬 Новое сообщение…» только по `general_tids = member_tids - mention_tids`. Всё в try/except.
- **Рендер XSS-safe**: текст сообщения выводится через `x-html="formatMsg(...)"`. `formatMsg` ОБЯЗАН сперва `this.esc()` (экранировать `& < > "`), и ТОЛЬКО потом regex-обернуть `@слово` в `<span class="mention">`. Порядок критичен — иначе XSS. Alpine x-html требует `unsafe-eval` в CSP (уже есть).
- **Автокомплит**: дропдаун над обоими textarea; источник — `dmMembers` (грузится `dmLoadMembers()` в `init()`, поле `mention`); `mentionInput` ловит `@partial` до каретки, фильтр по handle/имени, до 6; клавиши ↑↓/Enter(выбор когда открыт, иначе отправка)/Esc/blur(150ms). Вставка через `_mentionStart` + `_mentionEl` (splice по каретке, работает в середине текста).

**Why:** двойной push раздражал; x-html без предварительного esc = дыра XSS.
