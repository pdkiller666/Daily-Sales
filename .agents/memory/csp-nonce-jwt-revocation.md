---
name: CSP nonce + JWT revocation
description: Как реализованы per-request CSP nonce и JWT jti blacklist; ключевые ловушки.
---

## CSP nonce

- `SecurityHeadersMiddleware.dispatch()` генерирует `nonce = _secrets.token_urlsafe(16)` **до** `call_next()`, кладёт в `request.state.csp_nonce`
- Jinja2 global: `templates.env.globals['csp_nonce'] = lambda request: getattr(getattr(request,'state',None),'csp_nonce','')`
- В шаблонах: `<script nonce="{{ csp_nonce(request) }}">` — 93 script-блока в 52 файлах (добавлены через `sed 's/<script>/<script nonce...>/g'`)
- CSP header строится через `_CSP_TEMPLATE.replace("{{nonce}}", nonce)` (двойные фигурные скобки чтобы `str.format` не ломал)
- `unsafe-inline` убран из script-src — современные браузеры игнорируют его при наличии нonce
- `unsafe-eval` остаётся — Alpine.js 3 требует для `new Function()` в x-* атрибутах

**Why:** `unsafe-inline` в CSP позволяет выполнять любой инжектированный `<script>`-тег. Nonce ограничивает выполнение только тегированными сервером скриптами.

## JWT jti revocation

- `create_session_token()` добавляет `jti: secrets.token_hex(16)` в payload
- `_REVOKED: dict[str,float]` — in-memory кэш (jti → expiry timestamp); выживает в рамках процесса
- `revoke_jti(jti, exp)` — пишет в память + `revoked_tokens` (shop_bot.db)
- `is_jti_revoked(jti)` — проверяет память, потом DB (DB-fallback после рестарта)
- `get_session_user()` вызывает `is_jti_revoked()` — при совпадении возвращает None (неаутентифицирован)
- Logout: extract jti из cookie → `revoke_jti()` → `delete_cookie()`
- DB таблица: `revoked_tokens(jti PK, revoked_at, expires_at)` + `idx_revoked_exp` — только в shop_bot.db

**Why:** Cookie можно скопировать; revocation позволяет немедленно инвалидировать конкретный токен без смены секрета.
