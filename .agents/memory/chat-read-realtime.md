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

## Persistent DM WS — жить во всех режимах, не рвать при переключении
**Правило:** DM WebSocket поднимать в `init()` (а не при входе в режим ЛС) и держать открытым в режиме
«Общий» тоже; `switchToGroup` НЕ закрывает сокет. Reconnect — всегда, кроме осознанного закрытия
(флаг `_dmWsClosing`, ставится только в `dmWsDisconnect`). На `onopen` делать catch-up: `dmLoadContacts()`
+ перечитывание открытого диалога (`_dmFetchMessages(0)`) — иначе сообщения, пришедшие пока сокет был
закрыт, теряются.
**Why:** иначе входящие ЛС/бейджи приходят только когда пользователь уже в нужном диалоге; в режиме тем
человек не видит, что ему написали в личку.
**How to apply:** входящее не-AI в неоткрытый диалог (в т.ч. group-режим) поднимает `dmTotalUnread` и
обновляет строку контакта; если контакта нет в списке — `dmLoadContacts()`.

## client_id для надёжного сопоставления своих сообщений
**Правило:** при WS-отправке слать `client_id` (уникальный tmp-id), бэкенд `ws_dm` эхо-возвращает его в
`confirmed`-payload; фронт заменяет временный пузырь по `client_id` (fallback на `startsWith('tmp_')`).
**Why:** при быстрой отправке нескольких сообщений подряд сопоставление «первый tmp» путало пузыри.

## Отметка прочтения ЛС: WS + HTTP-фолбэк, но без дублей
**Правило:** `_dmMarkRead(peerId)` = WS `{type:'read'}` + `POST /chat/dm/{peer_id}/read` — для надёжной
серверной отметки при ОТКРЫТИИ диалога (WS мог ещё не подняться). НО для входящего сообщения в уже
открытом диалоге слать ТОЛЬКО WS-read: сокет заведомо открыт (только что приняли по нему), HTTP избыточен.
**Why:** HTTP на каждое входящее = лишний write/трафик при активной переписке.

## Не помечать тему прочитанной при скрытой вкладке
**Правило:** `chat_poll(mark_read:int=1)`; фронт при `document.hidden` шлёт `&mark_read=0` и не зовёт
`markTopicRead`; на `visibilitychange→visible` помечает прочитанным. Текущая тема копит непрочитанное пока
вкладка скрыта.
**Why:** иначе фоновый poll «съедал» непрочитанное — пользователь не видел, что пришло, пока его не было.

## Layout чата: высота строго между шапкой и нижним меню
**Правило:** `#chat-wrapper` height = CSS-переменная `--chat-h`, считается в JS `_fitHeight()` =
`visualViewport.height − wrapper.top − navH` (моб.: высота `nav.fixed.bottom-0` ~64px; desktop navH=0).
Слушатели `resize`/`orientationchange`/`visualViewport.resize` в `init()`. Скроллится только внутренний
список (`flex-1 overflow-y-auto`), обёртка `overflow:hidden`.
**Why:** старый расчёт вычитал только высоту шапки (3.5rem), игнорируя нижнее меню и бета-баннер →
композер уезжал под нав-меню, вся страница «двигалась». `wrapper.top` через getBoundingClientRect
автоматически учитывает бета-баннер.
**How to apply:** страница чата — полный reload (base.html без hx-boost/#content-свапа), поэтому
window-слушатели снимаются браузером сами; teardown не нужен.
