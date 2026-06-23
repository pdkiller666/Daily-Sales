---
name: AI session-break must be respected by ALL loaders
description: «Новый диалог»/AI reset вставляет session-break маркер; каждый путь загрузки/отображения AI-сообщений обязан клампить since_id на последний break, иначе диалог «возвращается» после перезахода
---

Кнопка «Новый диалог» (AI reset) НЕ удаляет историю — она вставляет служебный маркер
`is_session_break=1` (`add_ai_dm_session_break` / `add_ai_chat_session_break`). Активной
считается только переписка ПОСЛЕ последнего маркера.

**Правило:** любой путь, который грузит/отображает AI-сообщения, обязан передавать
`since_id = get_last_ai_*_session_break_id(...)` (для polling-дельт — `max(client_since_id, break_id)`).
Иначе экран чистится только клиентски, а после выхода/перезахода вся история возвращается.

**Why:** был баг — AI-context эндпоинты (сборка контекста для ответа) клампили на break,
а display-загрузчики (инициальный рендер темы, переключение темы, polling, DM `api_dm_conversation`)
— нет. Симптом: «нажал Новый диалог → почистилось → вышел/зашёл → всё на месте».

**How to apply:** AI DM определяется `peer_id == AI_PEER_ID` (=-1); AI-тема — `topic_id == db.get_ai_topic_id()`.
Для не-AI тем/собеседников `since_id` остаётся 0 (поведение не меняется). Lookup break-id оборачивать
в try/except (fail-open по доступности), но помнить: при сбое геттера семантика reset временно нарушится.
DM-пагинация «назад» (`before_id`) + `since_id` совместимы — `get_ai_dm_conversation` применяет оба
(`id > since_id AND id < before_id`), так что пролистать за границу reset нельзя.
