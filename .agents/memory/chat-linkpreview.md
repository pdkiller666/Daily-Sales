---
name: Chat OpenGraph link previews
description: Phase-4 of messenger upgrade — how link previews are fetched (SSRF-safe), cached, gated, and rendered in both bubble types
---

# Превью ссылок (OpenGraph) в корпоративном чате

Декаплед-подход: отдельный backend-модуль + GET-API + ленивый фронт-фетч. Текст сообщений хранится сырым — превью не трогает хранение/отправку.

## Backend — `web/link_preview.py` (self-contained)
- **SSRF-защита обязательна и проверяется на КАЖДОМ редиректе** (не только на исходном URL): только `http`/`https`; резолв host → отклонять private/loopback/link-local/reserved IP. `allow_redirects=False` + ручной цикл ≤3 редиректов. Body cap 512KB, таймаут 8с.
- **Парсинг — regex**, НЕ bs4 (bs4 в стеке нет). Тянет og:/twitter:/обычные title/description/image/site_name.
- **Кэш — собственный sqlite** `data/link_cache.db` (паттерн как у `rate_store.py`: свой файл + once-per-process ensure). `OK_TTL=7д`, `FAIL_TTL=1ч` (чтобы битый URL не дёргать каждый раз).
- Экспорт: `extract_first_url(text)` (sync) и `get_preview(url)` (async).
- **Возврат `get_preview`**: на успех `{"ok": True, "title","description","image","site_name","url"}`; на отказ/блокировку — либо `{"ok": False}`, либо `None`. **Эндпоинт обязан проверять `if not data or not data.get("ok")`** — `{"ok": False}` это truthy dict, голый `if not data` его пропустит.

## API — `web/routes/chat.py`
- `GET /api/chat/link-preview?url=` — `async def`. Auth `get_session_user`, chat-гейт (`_get_chat_min_plan() != 'Отключён'` + `_chat_access_ok(tg_id)`), rate-limit `_rate_ok(_LINK_PREVIEW_RATE, telegram_id, 40, 60.0)` — **ключ по telegram_id, не IP** (за прокси Amvera IP общий, см. chat-read-realtime.md).

## Frontend — `web/templates/chat/index.html`
- Alpine state `linkPreviews: {}` — карта `key → preview|false`. `undefined`=ещё не пробовали, `false`=в процессе/пусто, объект=есть превью (идемпотентность: `loadLinkPreview` выходит если `!== undefined`).
- Ключ: **`'g'+msg.id` для групп, `'d'+m.id` для ЛС** (id-последовательности групп и ЛС независимы и могут пересекаться численно — префикс обязателен).
- Триггер — `x-init="loadLinkPreview(key, text)"` на текстовом `<span>/<p>`; при пересоздании элемента (poll заменяет массив) re-run безвреден (guard).
- Карточка рендерится в ОБОИХ пузырях: title/site/description через `x-text` (НЕ x-html — XSS), `:href`/`:src` из server-derived URL, `referrerpolicy="no-referrer"` на img.
- **Инвалидация при правке**: `saveEdit`/`saveDmEdit` делают `delete this.linkPreviews[key]` → новый текст перечитает превью (иначе edited-in ссылка не получит карточку до перезагрузки — это был единственный gap architect-ревью).

**Why:** decoupled API + кэш — единственный путь, который работает одинаково в group (poll) и DM (WS) пузырях без дублирования логики и без блокировки отправки сетевым фетчем.
