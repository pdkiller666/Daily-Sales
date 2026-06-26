---
name: CSP nonce + JWT revocation
description: Как реализованы per-request CSP nonce и JWT jti blacklist; ключевые ловушки.
---

## CSP — nonce ОТКАЧЕН, используется 'unsafe-inline' (важно!)

- ТЕКУЩЕЕ состояние `_CSP_TEMPLATE` (web/app.py): `script-src 'self' 'unsafe-eval' 'unsafe-inline' https://telegram.org` — БЕЗ nonce.
- `unsafe-eval` обязателен — Alpine.js 3 (`new Function()` в x-* атрибутах).
- `SecurityHeadersMiddleware` всё ещё генерит nonce и делает `.replace("{{nonce}}", nonce)` — теперь это **no-op** (в шаблоне нет `{{nonce}}`); `csp_nonce` Jinja-global и `nonce=` на `<script>` тоже безвредно игнорируются. Мёртвый код, чистить отдельным cleanup-коммитом.

**Правило (НЕ переводить обратно на nonce без полного рефактора):** интерфейс массово (250+) использует inline event-handler атрибуты (`onclick`/`onchange`/`onsubmit`). По CSP3 наличие nonce в `script-src` **отключает** `'unsafe-inline'`, а на inline-обработчики nonce повесить НЕЛЬЗЯ → все они молча умирают (тема-тогл, канбан-кнопки, переход в карточку товара, push/тема в шторке «Ещё»). `<script>`-блоки с nonce и Alpine `@click` при этом продолжают работать — отсюда обманчивая картина «ломается только часть». Строгий nonce-CSP возможен ТОЛЬКО после миграции всех inline-обработчиков на delegated listeners/Alpine.

**Why:** strict nonce-CSP — это XSS-харднинг, но он несовместим с текущей архитектурой шаблонов. Прошлая сессия добавила nonce + убрала `unsafe-inline` и сломала весь UI на inline-обработчиках; откат `unsafe-inline` — осознанный trade-off (слабее XSS-защита) ради работоспособности.

## JWT jti revocation

- `create_session_token()` добавляет `jti: secrets.token_hex(16)` в payload
- `_REVOKED: dict[str,float]` — in-memory кэш (jti → expiry timestamp); выживает в рамках процесса
- `revoke_jti(jti, exp)` — пишет в память + `revoked_tokens` (shop_bot.db)
- `is_jti_revoked(jti)` — проверяет память, потом DB (DB-fallback после рестарта)
- `get_session_user()` вызывает `is_jti_revoked()` — при совпадении возвращает None (неаутентифицирован)
- Logout: extract jti из cookie → `revoke_jti()` → `delete_cookie()`
- DB таблица: `revoked_tokens(jti PK, revoked_at, expires_at)` + `idx_revoked_exp` — только в shop_bot.db

**Why:** Cookie можно скопировать; revocation позволяет немедленно инвалидировать конкретный токен без смены секрета.
