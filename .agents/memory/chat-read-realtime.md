---
name: Chat read-state & realtime sync
description: Серверный учёт прочитанного в веб-чате, live-удаление через WS, rate-limit за прокси, курсор удалений
---

# Веб-чат: серверный read-state, live-delete, rate-limit за прокси

## Rate-limit за обратным прокси — по идентичности, не по IP
**Правило:** все rate-limit чата (poll/search/dm) ключевать по `telegram_id`, НЕ по `request.client.host`.
**Why:** на Amvera (и любом reverse-proxy) `request.client.host` — это IP прокси, общий для всех
пользователей. Лимит по нему блокирует ВСЕХ, когда один превышает порог. `_client_ip()` (X-Forwarded-For)
есть, но для anti-abuse rate-limit идентичность пользователя надёжнее.
**How to apply:** rate-helpers принимают `key`; на всех call-sites передавать `telegram_id`.

## Курсор удалений (live-delete) — >=, а не строгий >
**Правило:** `get_chat_deleted_ids_since(topic_id, since_ts)` фильтрует `deleted_at >= since_ts`.
**Why:** `now` в poll-ответе имеет точность до секунды (`%Y-%m-%d %H:%M:%S`). При строгом `>` удаление,
произошедшее в ту же секунду что и baseline, теряется навсегда. Клиентский `msgs.filter(id not in delSet)`
идемпотентен — повторная доставка уже удалённого id безвредна, поэтому `>=` безопаснее потери события.
**How to apply:** клиент хранит `_delSince = data.now` из каждого ответа и шлёт назад в `del_since`.

## Серверный учёт прочитанного вместо localStorage
**Правило:** прочитанное/непрочитанное по темам — в таблице `chat_read_state(user_id, topic_id, last_read_id)`,
upsert через `MAX(last_read_id)` (монотонно, не откатывается). НЕ localStorage.
**Why:** localStorage не синхронизируется между устройствами/браузерами; бейджи расходились.
**How to apply:** poll отдаёт `topic_unread` (ключи — str(topic_id)) и помечает текущую тему read;
`POST /chat/read` (auth+CSRF+gate) для явной пометки при переключении/отправке. Начальный рендер
(`chat_page`) кладёт `unread` в темы — фронт обязан читать `t.unread`, НЕ затирать в 0 при init.

## DM live-delete + корректный att_id во вложениях
- `dm_delete` должен быть `async` и слать WS `{"type":"delete","id":msg_id}` ОБОИМ участникам
  (`row[1]`, `row[2]` из `get_dm_message`) через `dm_manager.send_to_user(org_db, uid, payload)`;
  фронт `handleDmWsMsg` обрабатывает `type==='delete'` → filter `dmMessages`.
- WS-payload файла в `dm_send`: грузить `_load_dm_files_bulk` ДО payload и брать реальный `id` вложения
  для `/chat/dm/file/attachment/{att_id}`. id СООБЩЕНИЯ ≠ id вложения — старый код слал new_id и ломал ссылку.

## Race guard при переключении тем
`_topicReqSeq` — монотонный счётчик; `switchTopic` инкрементит и сверяет после await; `poll` запоминает
seq+topicId до fetch и отбрасывает ответ, если тема сменилась. Без этого быстрые клики мешали ленты тем.
