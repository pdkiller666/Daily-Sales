# Анализ безопасности DailySales

> **Дата последнего аудита:** 25 июня 2026  
> **Охват:** web-слой (FastAPI), бот (aiogram), database.py, шаблоны Jinja2, деплой-инфраструктура  
> **Версия:** v1.211.0 · Amvera `2052518`

---

## ✅ Что реализовано на хорошем уровне

### Аутентификация и сессии
- **Пароли**: PBKDF2-SHA256, 390 000 итераций + случайная 32-байтовая соль
- **2FA (TOTP)**: Google Authenticator / любое TOTP-приложение; коды восстановления — SHA-256 хэши
- **Telegram Widget**: HMAC-SHA256 по токену бота с проверкой давности (max 1 ч)
- **Mini App**: HMAC-SHA256 по `b"WebAppData"` — отдельный ключ от Widget
- **Magic-link / code**: одноразовые коды в памяти бота, TTL
- **Сессии**: JWT HS256, 7 дней, cookie `HttpOnly + Secure + SameSite=Lax`
- **JWT jti revocation**: каждый токен содержит уникальный `jti`; logout вносит его в blacklist (`revoked_tokens` в shop_bot.db + in-memory кэш); `get_session_user()` проверяет revocation на каждый запрос — выживает рестарт сервера _(2026-06-25)_
- **WEB_SECRET_KEY**: независимый от BOT_TOKEN секрет; при отсутствии — domain-bound HMAC (не `sha256(BOT_TOKEN)`)
- **consent_at**: фиксируется при email-регистрации И при всех трёх Telegram-путях (Widget / magic-link / code-form) _(2026-06-26)_

### Защита от типовых атак
- **CSRF**: `HMAC(secret, jwt+nonce)[:32]` — сессионно-привязан, per-request. Проверяется на всех ~120 POST-эндпоинтах (form-data). JSON API-эндпоинты защищены SameSite=Lax + CORS preflight — CSRF-токен не требуется
- **SQL-инъекции**: все запросы параметризованы; f-строки с SQL — только для безопасных серверных значений
- **XSS**: Jinja2 auto-escape по умолчанию; `| safe` — только для SVG (с ручным `html.escape`) и числовых данных; Chart.js данные через `| tojson` _(исправлено 2026-06-26)_
- **Open redirects**: `_safe_next()` — только `/path` без `//` и внешних схем
- **Path traversal**: доказательства оплаты проверяются по абсолютному prefix-у

### Заголовки безопасности (все ответы)
```
Content-Security-Policy:
  default-src 'self'
  script-src  'self' 'unsafe-eval' 'nonce-{per-request}'  ← unsafe-inline убран
  style-src   'self' 'unsafe-inline'
  frame-src   https://telegram.org https://oauth.telegram.org
  object-src  'none'  |  base-uri 'self'  |  form-action 'self'
Strict-Transport-Security: max-age=31536000; includeSubDomains  (HTTPS-only)
X-Frame-Options: SAMEORIGIN
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(self), microphone=(self), geolocation=(), payment=(self)
X-XSS-Protection: 1; mode=block
```

Per-request CSP nonce: `SecurityHeadersMiddleware` генерирует `secrets.token_urlsafe(16)` до `call_next()`; все 93 `<script>`-блока в 52 шаблонах тегированы `nonce="{{ csp_nonce(request) }}"` _(2026-06-25)_

### Rate limiting
- Auth-эндпоинты: persistent SQLite (`rate_store`), 5 req/60 s на IP; fail-**closed** при сбое БД _(2026-06-25)_
- AI-эндпоинты: `check_and_increment_ai` — fail-closed, per-org квота
- Web Push: `rate_store`-based, per-telegram_id (не per-IP — за reverse-proxy)

### Изоляция клиентов (мультитенантность)
- Каждая организация — отдельный SQLite-файл `data/tenants/org_*.db`
- JWT содержит путь к конкретной БД; cross-org доступ исключён
- Email-only пользователи изолированы через `web_credentials.org_db`

### Платежи
- Никаких PAN/CVV — карточные данные не хранятся
- Доказательства оплаты — рандомизированные имена файлов
- Идемпотентность выдачи по `payment_request_id` (UNIQUE INDEX + pre-check, атомарный `UPDATE WHERE status='pending'`)
- Веб `billing_grant` / `billing_revoke` вызывают `invalidate_plan_cache()` — кэш плана сбрасывается немедленно _(2026-06-25)_

### Мониторинг, аудит и ПДн
- Вход с нового IP → уведомление в Telegram
- `admin_audit_log`: действия супер-админа (IP, детали) — веб + бот _(2026-06-26)_
- **Право на забвение (ФЗ-152 / GDPR)**: `Database.erase_user_pii()` + `erase_user_globally()` — анонимизация ПДн во всех org-базах, удаление `web_credentials` / `login_ips`, чистка чата; веб-интерфейс `/admin/users/{id}/erase`; аудит-лог каждого стирания _(2026-06-25)_
- Бэкапы зашифрованы AES-256 _(2026-06-25)_

---

## ⚠️ Реальные риски — по приоритету

### 🔴 Критично для корпоративных клиентов

**Нет шифрования данных на диске (Encryption at Rest)**

SQLite-файлы (`data/*.db`) хранятся открытым текстом:
- Компрометация хостинга → все данные всех организаций читаемы без пароля
- Google OAuth-токены в `integration_connections` — plaintext в БД
- Бэкапы зашифрованы ✅, но live-БД — нет

Для регулируемых отраслей — блокер.  
**Путь решения:** SQLCipher (at-rest encryption) или PostgreSQL с шифрованием тома.

---

### 🟡 Остаточные ограничения (осознанные компромиссы)

| Ограничение | Почему так | Путь решения |
|---|---|---|
| `unsafe-eval` в CSP | Alpine.js 3 требует `new Function()` для вычисления `x-*` выражений; без него вся JS-интерактивность мертва | Alpine CSP-build (требует бандлер/Vite) или миграция на Preact |
| SQLite вместо PostgreSQL | Простота деплоя, изоляция per-org; потолок ~30 платящих орг | Поэтапная миграция при росте (план в `postgres-migration.md`) |
| YooKassa без webhook | +latency при проверке статуса; polling-based подтверждение | Регистрация webhook-endpoint в кабинете ЮKassa |
| Google OAuth токены plaintext | Только server-side, нет публичного пути чтения | Шифрование at-rest (SQLCipher) решит и это |
| AI JSON-API без CSRF-токена | SameSite=Lax + CORS preflight достаточно для браузерного контекста | — |

---

### 🟢 История закрытых рисков

| Риск | Статус | Дата |
|---|---|---|
| Бэкапы plaintext | ✅ AES-256 шифрование | 2026-06-25 |
| Удаление организации без аудита | ✅ Аудит-лог (веб + бот) | 2026-06-25 / 2026-06-26 |
| Telegram-логин без consent_at | ✅ Все 3 пути записывают consent_at | 2026-06-26 |
| Rate limiter fail-open (persistent store) | ✅ SQLite-based, выживает рестарт | 2026-06-25 |
| Auth rate limiter wrapper fail-open | ✅ `return False` при исключении | 2026-06-25 |
| CSRF не проверялся в admin.py (8 маршрутов) | ✅ Исправлено (audit 2026-06) | 2026-06-17 |
| Хранение подписки без идемпотентности | ✅ UNIQUE INDEX + pre-check + атомарный UPDATE | 2026-06-26 |
| XSS в tasks/analytics (json.dumps + `| safe`) | ✅ Заменено на `| tojson` | 2026-06-26 |
| `unsafe-inline` в CSP | ✅ Заменён per-request nonce (93 блока, 52 шаблона) | 2026-06-25 |
| JWT без server-side revocation | ✅ jti blacklist (in-memory + shop_bot.db) | 2026-06-25 |
| Billing grant/revoke без invalidate_plan_cache | ✅ Добавлен в оба веб-маршрута | 2026-06-25 |
| Право на забвение (ФЗ-152 / GDPR) | ✅ erase_user_pii() + веб-интерфейс + аудит | 2026-06-25 |

---

## 📋 Вердикт по сегментам клиентов

| Сегмент | Статус | Главный стоппер |
|---|---|---|
| МСБ без строгих требований | ✅ Можно продавать | — |
| Сети / Франшизы (50–200 чел) | ✅ С оговорками | Нет шифрования at-rest; уведомить письменно |
| Компании с требованиями ФЗ-152 | ⚠️ Ограниченно | Шифрование at-rest (right-to-erasure уже ✅) |
| Госструктуры / медицина | ❌ Не готово | Шифрование, сертификация ФСТЭК, on-premise |
| Международные (GDPR) | ❌ Не готово | DPA, retention policy, локализация данных |

---

## 🛠️ Дорожная карта

### Выполнено ✅
1. Шифрование бэкапов AES-256
2. Удаление организации с полным аудит-логом (веб + бот)
3. consent_at при регистрации и при всех Telegram-входах
4. Persistent rate limiter для auth-эндпоинтов (fail-closed)
5. XSS-фикс в tasks/analytics (`| tojson`)
6. Auth rate limiter wrapper → fail-closed
7. Унификация billing: `invalidate_plan_cache` в веб-маршрутах
8. Право на забвение — `erase_user_pii()` + веб-интерфейс + аудит
9. `unsafe-inline` → per-request nonce (93 script-блока)
10. JWT server-side revocation — jti blacklist (память + DB)

### Следующий приоритет
- **[Высокий]** SQLCipher — шифрование live-БД at-rest (требует миграцию, downtime)
- **[Средний]** YooKassa webhook — убрать polling-подтверждение
- **[Средний]** Alpine CSP-build — убрать `unsafe-eval` (требует бандлер)
- **[Низкий]** Настраиваемый retention переписки и данных продаж по пользователю
- **[Низкий]** DPA / Privacy Policy для GDPR-клиентов

### Enterprise (2–3 месяца)
- PostgreSQL с row-level security (при >30 платящих орг)
- On-premise / self-hosted вариант поставки
- SIEM-интеграция для корпоративного аудита
- ФСТЭК / сертификация для госсектора
