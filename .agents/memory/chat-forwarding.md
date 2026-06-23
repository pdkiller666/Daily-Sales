---
name: Chat message forwarding
description: Пересылка сообщений в веб-чате (группы↔ЛС) — модель данных, копирование вложений, гейтинг, индексные контракты
---

# Пересылка сообщений (forwarding)

Пересылка любого сообщения: группа↔группа, группа↔ЛС, ЛС↔ЛС. POST `/chat/forward`
(CSRF + rate-limit + `_chat_access_ok` + min_plan). Источник `src_kind`/`src_id`,
назначение `dst_kind`/`dst_topic_id`/`dst_peer_id`.

## Модель данных
- Колонка `forwarded_from TEXT DEFAULT ''` в `chat_messages` И `direct_messages`
  (guarded ALTER в `create_tables()`).
- `forwarded_from` хранит **исходного автора**, а не предыдущего отправителя:
  при пересылке уже-пересланного берём `source.forwarded_from OR source.author`,
  поэтому имя оригинала переживает всю цепочку пересылок.

## Вложения копируются физически
**Правило:** вложения пересылки `shutil.copy2` в uploads-директорию НАЗНАЧЕНИЯ
(`_uploads_dir`/`_uploads_dir_dm`), НЕ переиспользуем путь оригинала.
**Why:** если бы делили путь, удаление оригинала ломало бы пересланную копию.
**How to apply:** `_copy_files_for_forward(src_files, dst_dir)` → list; копировать
ДО вставки сообщения и при `not src_text and not copied` вернуть 400 — иначе
file-only пересылка с пропавшим исходником создаёт пустое сообщение.

## Гейтинг доступа к источнику
DM-источник: пересылать может только участник переписки (`user_db_id ∈
{from_user_id, to_user_id}` из `get_dm_message_for_forward`) — иначе IDOR на чужие ЛС.
Удалённые сообщения как источник отклоняются.

## Индексные контракты (критично — легко словить off-by-one)
`forwarded_from` вставлен ПЕРЕД is_pinned, поэтому хвостовые отрицательные индексы:
- Группа (`get_chat_messages`/`_since`/`get_pinned_chat_messages`):
  forwarded=`row[-4]`, is_pinned=`row[-3]`, edited=`row[-2]`, reply=`row[-1]`.
- ЛС (`get_dm_conversation`): forwarded=`row[-3]`, edited=`row[-2]`, reply=`row[-1]`.
**Любой новый SELECT, читаемый `_fmt_msg`/`_fmt_dm`, ОБЯЗАН включать
`COALESCE(m.forwarded_from,'')` в той же позиции** — иначе `row[-4]` укажет на
`u.username` и непересланное сообщение покажет «↪ Переслано от …»
(реальный регресс: `get_pinned_chat_messages` забыли обновить).

## UI
`_fmt_msg`/`_fmt_dm` отдают поле `forwarded_from`; рендер через Alpine `x-text`
(экранируется). WS-payload (DM) несёт `forwarded_from`. Модалка выбора назначения
показывает не-AI темы + DM-контакты (id>0); при открытии из режима тем контакты
ЛС ленится подгрузить (`dmLoadContacts` если пусто).

## Не сделано осознанно
Server-side дедуп пересылок отсутствует — защита только клиентским
`forwardSending`-флагом; повтор сети может создать дубль (низкий приоритет).
