---
name: Email auth architecture
description: Ключевые решения email+пароль аутентификации — таблица, хеширование, synthetic ID, SMTP
---

## Таблица web_credentials (shop_bot.db)
Поля: `id, email, password_hash, telegram_id (nullable), synthetic_tg_id, org_db, first_name, email_verified, verify_token, verify_expires, reset_token, reset_expires, last_login, created_at`

## Password hashing
PBKDF2-SHA256, 390 000 итераций, salt_hex + ":" + key_hex. Реализация в `web/auth.py`: `hash_password()` / `verify_password()` — stdlib только, никакого bcrypt.

**Why:** bcrypt не входит в stdlib и усложняет деплой; PBKDF2-SHA256 с 390k итераций соответствует OWASP 2024 рекомендациям.

## Synthetic telegram_id
`synthetic_tg_id = -(10_000_000 + cred_id)` — отрицательные ID не конфликтуют с реальными Telegram ID (всегда положительные). Хранится в `web_credentials.synthetic_tg_id`, используется как `tg_id` в JWT. `org_db` для email-only хранится в `web_credentials.org_db`, а не в `user_org_mapping`.

**How to apply:** При получении сессии проверить: если `tg_id < 0` — это email-only пользователь; `org_db` брать из `web_credentials`, не из `user_org_mapping`.

## SMTP (Яндекс)
- `smtp.yandex.ru:465`, SSL
- Секреты: `YANDEX_EMAIL` + `YANDEX_SMTP_PASSWORD`
- `YANDEX_SMTP_PASSWORD` — **пароль приложения** (16 символов), НЕ пароль аккаунта Яндекс
- `web/email_utils.is_configured()` — проверять перед всеми SMTP-вызовами; без него `/register` и `/auth/reset` возвращают 503

## Rate limiting
`check_rate_limit(f"email_auth:{ip}", 5, 600)` → 5 попыток / 10 мин / IP. Persistent SQLite (data/rate_limits.db). Применяется к `/auth/email`, `/register`, `/auth/reset`.

## Bot command /setweblogin
Генерирует 6-символьный одноразовый код (TTL 10 мин) в `web_credentials`. Пользователь вводит его в `/settings` → поле привязки Telegram. После привязки `telegram_id` обновляется в `web_credentials`, synthetic_tg_id перестаёт использоваться.

**Why:** Привязка позволяет email-пользователю позже получить полный Telegram-функционал без потери данных сессии.
