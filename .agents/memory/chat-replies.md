---
name: Chat replies / quotes
description: Reply-to/quote design for group topics (poll) + DM (WebSocket) in the corporate chat
---

# Ответы / цитирование в чате

Зеркалится в двух поверхностях: групповые темы (`chat_messages`, HTTP polling) и ЛС (`direct_messages`, WS). Колонка `reply_to_id INTEGER DEFAULT 0` в обеих таблицах (ALTER в create_tables).

## Ключевые решения / инварианты
- **Цитата-превью валидируется по доступу, не только по существованию.** Родитель должен быть в ТОЙ ЖЕ области, иначе IDOR/утечка чужого контекста:
  - Группы (`chat_send`): parent.topic_id == topic_id текущей темы + не удалён.
  - ЛС (`dm_send`): ОБА конца родителя (`from_user_id` И `to_user_id`) должны лежать в кортеже текущего диалога `(user_db_id, to_user_id)`. **Why:** проверка только `from_user_id in conv` пропускала цитирование чужого сообщения по голому id (architect нашёл при ревью T2). Поэтому `get_dm_reply_previews` отдаёт `to_user_id`, а `get_chat_reply_previews` — `topic_id` именно для валидации.
- **`reply_to_id` всегда последний столбец (`row[-1]`)** в `get_chat_messages*` / `get_dm_conversation` SELECT — все лоадеры опираются на это. При добавлении новых столбцов в эти запросы либо держать reply_to_id последним, либо переписать все callsites.
- **Удалённый родитель остаётся цитируемым.** Preview-методы включают удалённые строки (`is_deleted` в выдаче) → UI рисует «сообщение удалено», ссылка-цитата остаётся. Но НОВУЮ цитату на удалённый родитель валидация отклоняет (valid_reply=0).
- **DM WS fast-path пропускается при ответе** — отправка с reply_to_id форсит HTTP-путь, т.к. reply_obj собирается на сервере и кладётся в WS payload + ответ.
- **Сниппет НЕ экранируется на сервере** (`_reply_snippet` обрезает, не escape) — фронт рендерит цитату через `x-text` (Alpine), который экранирует. XSS закрыт на клиенте; не переносить сниппет в innerHTML/`| safe`.

## Формат
`_fmt_msg`/`_fmt_dm` отдают `reply: {id, author, snippet}` (или None). AI-пути (chat AI session formatter, AI DM) намеренно НЕ передают reply. Клик по цитате → scrollToMsg/scrollToDmMsg + flash-подсветка оригинала.
