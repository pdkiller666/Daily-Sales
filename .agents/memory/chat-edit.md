---
name: Chat message editing (T4)
description: How edit-message works in corporate chat — author-only, edited marker, group-via-poll vs DM-via-WS realtime sync
---

# Редактирование сообщений в корпоративном чате

Реализовано в обеих лентах: групповые темы (`chat_messages`, HTTP polling) и ЛС (`direct_messages`, WebSocket).

## Ключевые решения / гочи

- **`edited_at` хранится как `row[-2]`** в трёх SELECT-ах (`get_chat_messages`, `get_chat_messages_since`, `get_dm_conversation`); `reply_to_id` остаётся `row[-1]`. При добавлении новых колонок в эти SELECT-ы сверять порядок — `_fmt_msg`/`_fmt_dm` и list-callers распаковывают по отрицательным индексам.
  **Why:** добавление в конец сместило бы reply_to_id и сломало T2.

- **Группа синхронизирует правки через poll, тем же курсором-таймстампом, что и удаления** (`del_since` / `_delSince`, серверный `data.now`). `get_chat_edited_since(topic_id, since_ts)` использует `edited_at >= since_ts` (не строгий `>`, иначе теряются правки в ту же секунду; клиентский merge идемпотентен).
  **Why:** в группах нет per-message WS — только poll. Отдельный курсор не нужен.

- **ЛС синхронизируют правки через WS** `{type:'edit', id, message}`, broadcast обоим эндпоинтам (`get_dm_message` → `row[1]`=from, `row[2]`=to) через `dm_manager.send_to_user`.

- **Author-only на уровне SQL**: `edit_chat_message`/`edit_dm` фильтруют `WHERE id=? AND from_user_id/user_id=? AND is_deleted=0`. `can_edit` в `_fmt_*` = `is_mine and from_id != 0` (AI-сообщения from_user_id=0 редактировать нельзя).

- **Клиентский guard от затирания при активной правке**: poll-merge и WS-edit пропускают сообщение, если `editId/dmEditId === msg.id` (пользователь сейчас его редактирует).

- **XSS**: отредактированный текст ре-рендерится тем же путём `x-html="formatMsg(...)"` (esc перед mention-regex), как обычные сообщения — безопасно.

- **Метку «изм.» рисовать в ОБЕИХ лентах** — легко забыть про ЛС: в группе это `<span x-show="msg.edited">`, в ЛС meta был `<p>` только с таймстампом, пришлось обернуть в span + добавить `· изм.`.
