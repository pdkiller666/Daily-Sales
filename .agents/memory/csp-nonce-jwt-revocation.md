---
name: CSP nonce + JWT revocation
description: Как реализованы per-request CSP nonce и JWT jti blacklist; ключевые ловушки.
---

## CSP — строгий nonce ВКЛЮЧЁН (без 'unsafe-inline' в script-src)

- ТЕКУЩЕЕ состояние `_CSP_TEMPLATE` (web/app.py): `script-src 'self' 'unsafe-eval' 'nonce-{{nonce}}' https://telegram.org` — БЕЗ `unsafe-inline`.
- `unsafe-eval` обязателен — Alpine.js 3 (`new Function()` в x-* атрибутах); убрать его нельзя.
- `SecurityHeadersMiddleware` генерит per-request nonce ДО `call_next`, кладёт в `request.state.csp_nonce`, шаблоны читают через `csp_nonce(request)` Jinja-global; в конце `.replace("{{nonce}}", nonce)` на заголовке. Все `<script>` ОБЯЗАНЫ нести `nonce="{{ csp_nonce(request) }}"`.

**Как это стало возможным:** ВСЕ inline event-handler атрибуты (изначально ~251 `on*`: `onclick`/`onchange`/`onsubmit`/`ondblclick`/`ondragstart`/…) убраны из ~50 шаблонов. По CSP3 nonce в `script-src` **отключает** `'unsafe-inline'`, а на inline-обработчик nonce повесить НЕЛЬЗЯ → если хоть один останется, он молча умрёт. Поэтому правило: **НЕ добавлять inline `on*` ни в шаблон, ни через `setAttribute('on…')`/`cell.setAttribute('onclick',…)`** — это тоже блокируется CSP.

**Делегирование — `web/static/ds-delegate.js`** (грузится в base.html + standalone-шаблонах landing/auth/*). Document-level делегатор: `data-href`, `data-action`(+`data-arg`/`data-arg2`, числа коэрсятся, передаёт `el,event`), `data-modal-open/close`, `data-backdrop-close/action`, `data-toggle-dark`, `data-print`, `data-reload`, `data-confirm`(click+submit), `data-copy`(+`data-copy-feedback`), `data-submit-form`, `data-submit`(form→`fn(event,arg,form)`), `data-once`, `data-stop`, `data-autosubmit`, `data-change`, `data-set-value`, `data-input-transform`(upper|upper-code|int-min0|autoheight). Хелперы `window.dsOnce/dsSetLs/dsRmLs`.

**Ловушка — ds-delegate покрывает только click/submit/change/input.** События `dragstart/dragover/drop/dblclick` он НЕ делегирует. Для них нужен СВОЙ document-level listener в nonce-скрипте страницы, читающий `data-*` с `e.target.closest('[data-…]')`: канбан (`data-drop-status` на колонке, `data-task-id`+`data-status` на карточке → dragstart/drop), products-цена (`data-price-edit`+`data-pid`+`data-price` → dblclick). Эти drag/dblclick события всплывают, делегирование работает.

**Why:** strict nonce-CSP — XSS-харднинг. Прошлая сессия откатывала его на `unsafe-inline`, т.к. UI висел на inline-обработчиках; теперь они мигрированы → можно держать строгий CSP. **Любой новый inline `on*` сломает strict-CSP молча** — всегда через data-* + ds-delegate (или per-page listener для drag/dblclick).

## JWT jti revocation

- `create_session_token()` добавляет `jti: secrets.token_hex(16)` в payload
- `_REVOKED: dict[str,float]` — in-memory кэш (jti → expiry timestamp); выживает в рамках процесса
- `revoke_jti(jti, exp)` — пишет в память + `revoked_tokens` (shop_bot.db)
- `is_jti_revoked(jti)` — проверяет память, потом DB (DB-fallback после рестарта)
- `get_session_user()` вызывает `is_jti_revoked()` — при совпадении возвращает None (неаутентифицирован)
- Logout: extract jti из cookie → `revoke_jti()` → `delete_cookie()`
- DB таблица: `revoked_tokens(jti PK, revoked_at, expires_at)` + `idx_revoked_exp` — только в shop_bot.db

**Why:** Cookie можно скопировать; revocation позволяет немедленно инвалидировать конкретный токен без смены секрета.
