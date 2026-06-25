# Анализ безопасности DailySales

> Дата последнего аудита: 26 июня 2026  
> Охват: web-слой (FastAPI), бот (aiogram), database.py, шаблоны Jinja2  
> Статус: актуально

---

## ✅ Что реализовано на хорошем уровне

### Аутентификация
- **Пароли**: PBKDF2-SHA256, 390 000 итераций + случайная соль
- **2FA (TOTP)**: Google Authenticator / любое TOTP-приложение. Коды восстановления — SHA-256 хэши
- **Telegram Widget**: HMAC-SHA256 по токену бота с проверкой давности (max 1 ч)
- **Mini App**: HMAC-SHA256 по `b"WebAppData"` — отдельный ключ от Widget
- **Magic-link / code**: одноразовые коды, хранятся в памяти бота, TTL
- **Сессии**: JWT HS256, 7 дней, cookie `HttpOnly + Secure + SameSite=Lax`
- **WEB_SECRET_KEY**: независимый от BOT_TOKEN секрет (если задан); иначе domain-bound HMAC
- **consent_at**: фиксируется при email-регистрации И при всех трёх путях Telegram-входа (Widget / magic-link / code-form) — _(исправлено 2026-06-26)_

### Защита от типовых атак
- **CSRF**: `nonce.HMAC(secret, jwt+nonce)[:32]` — сессионно-привязан, per-request. Проверяется на всех ~120 POST-эндпоинтах (form-data). JSON API-эндпоинты (AI-ассистент, push/subscribe) защищены SameSite=Lax + CORS preflight — CSRF-токен не требуется
- **SQL-инъекции**: все запросы параметризованы; f-строки с SQL используются только для безопасных серверных значений (имена таблиц, `sys_filter` из булева аргумента)
- **XSS**: Jinja2 auto-escape по умолчанию; `| safe` используется только для SVG (с ручным html.escape) и pre-serialized числовых данных. Пользовательские имена в Chart.js переданы как Python-списки через `| tojson` — _(исправлено 2026-06-26)_
- **Open redirects**: `_safe_next()` — принимает только `/path` без `//` и внешних схем
- **Path traversal**: доказательства оплаты проверяются по абсолютному prefix-у

### Заголовки безопасности
```
Content-Security-Policy: default-src 'self' + строгий allow-list (+ unsafe-eval для Alpine)
Strict-Transport-Security: 31536000 (HSTS)
X-Frame-Options: SAMEORIGIN
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(self)
```

### Изоляция клиентов (мультитенантность)
- Каждая организация — отдельный SQLite-файл `data/tenants/org_*.db`
- JWT содержит путь к конкретной БД организации
- Нет cross-org доступа: `org_db` берётся только из проверенного JWT

### Мониторинг и аудит
- Вход с нового IP → уведомление в Telegram
- `admin_audit_log` — действия супер-админа с IP и деталью; охватывает веб-панель И бота — _(бот добавлен 2026-06-26)_
- Fail-closed rate limiter для AI-эндпоинтов (`check_and_increment_ai` → `return False` при сбое БД)
- Persistent rate limiter (SQLite) для auth-эндпоинтов: выживает рестарты

### Платежи
- Никаких PAN/CVV — карточные данные не хранятся
- Доказательства оплаты — рандомизированные имена файлов
- Идемпотентность выдачи по `payment_request_id` (UNIQUE INDEX + pre-check)

---

## ⚠️ Реальные риски — по приоритету

### 🔴 Критично для корпоративных клиентов

**1. Нет шифрования данных на диске (Encryption at Rest)**

Все SQLite-файлы (`data/*.db`) хранятся открытым текстом:
- Компрометация хостинга → все данные всех организаций читаемы без пароля
- Google OAuth токены в `integration_connections` — plaintext в БД
- Бэкапы зашифрованы AES-256 ✅ — но live-БД нет

Для ФЗ-152 регулируемых отраслей — де-факто блокер.

**2. ✅ Механизм «право на забвение» реализован (ФЗ-152 / GDPR)** _(2026-06-25)_

Реализовано: `Database.erase_user_pii()` + `erase_user_globally()` в `tenant_manager.py`:
- Анонимизация ПДн (ФИО, телефон, email, username, фото) во всех org-базах пользователя
- Удаление `web_credentials` и `login_ips` из shop_bot.db
- Анонимизация текста chat/DM-сообщений (структура reply-chain сохраняется)
- Удаление `absence_records`, `work_schedule`, `notification_history/settings`
- Удаление файла фото профиля с диска
- Сохраняются: sales, salary_adjustments (бизнес-записи для бухгалтерии)
- Веб-интерфейс: `/admin/users` → кнопка «🗑 ПДн» → страница подтверждения `/admin/users/{id}/erase`
- Аудит-лог каждого стирания в `admin_audit_log`

Остаётся: журнал передачи ПДн третьим лицам; настраиваемый retention переписки.

---

### 🟡 Важно, но решаемо

~~**3. Auth rate limiter: внешний wrapper fail-open**~~ ✅ _Исправлено 2026-06-25_

~~**4. `unsafe-eval` в CSP**~~ — `unsafe-eval` остаётся (Alpine.js 3 требует), **устранено** как риск через нонсы:

CSP теперь использует `nonce-{random}` вместо `unsafe-inline`. Все 93 `<script>`-блока в 52 шаблонах получили `nonce="{{ csp_nonce(request) }}"`. Современные браузеры игнорируют `unsafe-inline` при наличии нонса → только тегированные скрипты выполняются. `unsafe-eval` остаётся нужным для Alpine.js — убирается при переходе на Alpine CSP-build (требует бандлер).

~~**5. JWT без server-side revocation**~~ ✅ _Исправлено 2026-06-25_

JWT теперь включает `jti` (уникальный ID токена). При logout jti вносится в `revoked_tokens` (shop_bot.db) и in-memory кэш. `get_session_user()` проверяет revocation при каждом запросе. Выживает рестарт сервера.

~~**6. Расхождение путей подтверждения оплаты (веб vs бот)**~~ ✅ _Исправлено 2026-06-25_

Веб-маршруты `/admin/billing/grant` и `/admin/billing/revoke` теперь вызывают `invalidate_plan_cache()` после успешного действия — устраняет задержку активации/деактивации из-за кэша плана.

---

### 🟢 Закрытые риски (история)

| Риск | Статус | Дата |
|---|---|---|
| Бэкапы plaintext | ✅ AES-256 шифрование | 2026-06-25 |
| Удаление организации без аудита | ✅ Аудит-лог (веб + бот) | 2026-06-25 / 2026-06-26 |
| Telegram-логин без consent_at | ✅ Все 3 пути записывают consent_at | 2026-06-26 |
| Rate limiter fail-open (auth) | ✅ Persistent SQLite store | 2026-06-25 |
| Auth rate limiter wrapper fail-open | ✅ `return False` при исключении | 2026-06-25 |
| CSRF не проверялся в admin.py (8 маршрутов) | ✅ Исправлено (audit 2026-06) | 2026-06-17 |
| Хранение подписки без идемпотентности | ✅ UNIQUE INDEX + pre-check | 2026-06-26 |
| XSS в tasks/analytics (json.dumps + \| safe) | ✅ Исправлено на \| tojson | 2026-06-26 |
| `unsafe-inline` в CSP | ✅ Заменён per-request nonce (93 script-блока) | 2026-06-25 |
| JWT без server-side revocation | ✅ jti blacklist (память + shop_bot.db) | 2026-06-25 |
| Расхождение billing grant/revoke (веб vs бот) | ✅ `invalidate_plan_cache` добавлен в веб | 2026-06-25 |

---

### 🟢 Осознанные компромиссы

| Решение | Почему так |
|---|---|
| SQLite вместо PostgreSQL | Простота деплоя, изоляция per-org. Потолок ~30 платящих орг. |
| YooKassa без webhook | Работает, но +latency при проверке статуса |
| `unsafe-eval` в CSP | Alpine.js 3 требует. Без него вся JS-интерактивность мертва |
| Google OAuth токены plaintext | Server-side only, нет публичного access path |
| AI JSON-API без CSRF-токена | Защищены SameSite=Lax cookie + CORS preflight браузера |

---

## 📋 Вердикт по сегментам клиентов

| Сегмент | Статус | Главный стоппер |
|---|---|---|
| МСБ без строгих требований | ✅ Можно продавать | — |
| Сети / Франшизы (50–200 чел) | ✅ С оговорками | Нет шифрования at-rest; уведомить письменно |
| Компании с требованиями ФЗ-152 | ⚠️ Нужна доработка | Шифрование at-rest + механизм «право на забвение» |
| Госструктуры / медицина | ❌ Не готово | Шифрование, сертификация, on-premise |
| Международные (GDPR) | ❌ Не готово | Право на забвение, DPA, локализация данных |

---

## 🛠️ Дорожная карта устранения

### Минимум (выполнено)
1. ✅ Шифрование бэкапов AES-256 _(2026-06-25)_
2. ✅ Удаление организации с полным аудит-логом (веб + бот) _(2026-06-25 / 2026-06-26)_
3. ✅ consent_at при регистрации и при всех Telegram-входах _(2026-06-26)_
4. ✅ Persistent rate limiter для auth-эндпоинтов _(2026-06-25)_
5. ✅ XSS в tasks/analytics — `| tojson` вместо `json.dumps + | safe` _(2026-06-26)_

### Следующий уровень (приоритизированный)
1. ✅ Auth rate limiter outer wrapper → fail-closed _(2026-06-25)_
2. ✅ Унификация подтверждения оплаты: `invalidate_plan_cache` в веб-маршрутах _(2026-06-25)_
3. ✅ **API «удалить данные пользователя» (right-to-erasure) реализован** _(2026-06-25)_
4. ✅ `unsafe-inline` заменён per-request nonce; `unsafe-eval` остаётся (Alpine.js) _(2026-06-25)_
5. ✅ JWT server-side revocation через jti blacklist _(2026-06-25)_

### Полноценно (2–3 месяца, enterprise-уровень)
- SQLCipher для шифрования баз данных at-rest
- Переход на PostgreSQL с row-level security (при >30 платящих орг)
- Настраиваемый retention переписки и данных продаж по пользователю
- On-premise / self-hosted вариант поставки
- SIEM-интеграция для корпоративного аудита
