---
name: AI DM replies break unread counter
description: AI-ответы в ЛС (from_user_id=0) ломают счётчик непрочитанных, если mark-read фильтрует по from_user_id
---

# AI-ответы в ЛС и счётчик непрочитанных

AI-ассистент пишет ответ в личке как DM с `from_user_id=0`, `to_user_id=<юзер>`,
`ai_peer_id=<собеседник>`, `is_read=0`.

**Правило:** любой путь, отмечающий ЛС прочитанными или считающий непрочитанное
по собеседнику, ОБЯЗАН трактовать AI-ответ (`from_user_id=0 AND ai_peer_id=peer`)
как часть переписки с `peer` — симметрично `get_dm_conversation`.

**Why:** `get_dm_unread_count` считает все `to_user_id=me AND is_read=0` (включая
AI). Старый `mark_dm_read(me, peer)` фильтровал только `from_user_id=peer`, поэтому
AI-ответы (`from_user_id=0`) НИКОГДА не помечались прочитанными → счётчик ЛС висел
вечно после введения AI в личных сообщениях. Аналогично `get_dm_contacts`
группировал непрочитанное по `from_user_id`, и AI (0) не попадал ни в одну строку
контакта (рассинхрон с общим счётчиком).

**How to apply:** в WHERE/GROUP таких запросов AI-сообщение приводить к peer через
`CASE WHEN from_user_id=0 THEN ai_peer_id ELSE from_user_id END`. Сохранять изоляцию
по собеседнику: отметка прочитанным одной переписки не должна закрывать AI-ответы
другой (`ai_peer_id` другого peer). Регресс покрыт функциональным тестом read-state.
