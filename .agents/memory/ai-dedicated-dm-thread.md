---
name: Dedicated AI assistant DM thread
description: How the standalone "AI-ассистent" peer in web DMs is encoded and routed, vs legacy in-dialog AI replies
---

# Выделенный AI-собеседник в личных сообщениях (веб-чат)

AI вынесен из реальных ЛС в отдельного виртуального собеседника. Старые AI-ответы
внутри реальных диалогов (`ai_peer_id >= 1`) остаются как есть — их не трогаем.

## Кодировка peer id (важно — два разных пространства)
- **Routing/frontend peer id = -1** (`AI_PEER_ID` в `web/routes/chat.py`).
  Нельзя 0: во фронтенде `dmPeerId=0` означает «диалог не выбран» (truthiness).
- **Storage**: запрос пользователя = `add_dm(from=user, to=0)`; ответ AI =
  `add_dm(from=0, to=user, ai_peer_id=0)`. Т.е. в БД AI-peer кодируется через
  `to=0` / `from=0+ai_peer_id=0`, НЕ через -1.

**Why:** `ai_peer_id=0` — это дефолт `add_dm`, поэтому сам по себе он НЕ уникален;
выделенный AI-тред распознаётся комбинацией `from=0 AND ai_peer_id=0` (ответ) или
`to=0` (запрос). Legacy in-dialog AI имеет `ai_peer_id = id реального собеседника`.

## Изоляция / границы
- Отдельные DB-методы `get_ai_dm_conversation` / `mark_ai_dm_read` /
  `get_ai_dm_summary` — НЕ трогают рабочие `get_dm_conversation`/`mark_dm_read`
  (низкий риск регрессий).
- `get_dm_contacts` естественно отбрасывает peer 0 (INNER JOIN users) → фантом-контакта
  AI нет; AI-контакт инъектится отдельно через `_build_ai_contact` (id=-1) когда оплачено.
- **Гейт доступа** обязателен на ВСЕХ AI-путях (send/conversation/read) — `_ai_ext_ok()`
  (has_extension владельца `ai_chat_assistant`). Иначе прямыми запросами можно
  читать/слать в AI-тред мимо скрытого контакта.
- **Валидация peer**: в DM API/WS запрещать `peer_id <= 0` кроме `-1`; `peer_id=0`
  (технический storage-маркер) не должен открываться как «диалог».
- Frontend: для `dmPeerId===-1` WS fast-path форсится на HTTP (ws_dm не умеет AI);
  AI-ответ (`from_user_id===0`) аппендится только если открыт AI-тред (dmPeerId===-1),
  иначе bump AI-контакта (-1), не текущего диалога.

**How to apply:** при любом изменении DM-потока (новые роуты, WS, счётчики) учитывать
оба пространства id и не забывать `_ai_ext_ok` на новых AI-путях.
