## Overview

This project is a professional, multi-tenant Telegram bot designed for comprehensive shop management. It allows users to manage products, track sales, and handle subscriptions with isolated databases for each organization. It ships with a full FastAPI web cabinet (PWA).

> **Это краткий README.** Подробный справочник веб-слоя, полный каталог архитектурных решений и список фич — в `PROJECT_MAP.md` (раздел 10). Схемы callback-ов хендлеров и API модулей — там же.

## Run & Operate

- **Run**: `python main.py` (via workflow "Start application")
- **Deploy (GitHub + Amvera по умолчанию)**: `bash deploy.sh "commit message"`
- **Deploy только GitHub**: `bash deploy.sh "message" --no-amvera`
- **Env vars required**: `BOT_TOKEN`, `ADMIN_CHAT_ID`, `GITHUB_TOKEN` (all in Replit Secrets)
- **Google Sheets OAuth**: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (in Replit Secrets)
- **Web Push VAPID**: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_MAILTO` (in Replit Secrets)
- **Email/SMTP (optional)**: `YANDEX_EMAIL`, `YANDEX_SMTP_PASSWORD` (пароль приложения, не аккаунта)

## Stack

**Telegram Bot:** Python 3.11 · aiogram 3 · SQLite · APScheduler · openpyxl · PickleStorage (FSM)
**Web Interface:** FastAPI + Uvicorn (port 5000) · Jinja2 · Tailwind CSS CDN · HTMX · Alpine.js · Chart.js · PyJWT (HS256 session cookie)
**Landing Page:** standalone `/` route — public promo page for unauthenticated visitors; redirects to `/dashboard` when logged in
**Infra:** Amvera (production hosting) · GitHub (version control)

## Where things live (high-level)

### Bot core
- `main.py` — bot entry, router registration, APScheduler jobs
- `database.py` — `Database` class, all methods + all migrations in `create_tables()`
- `db_utils.py` — `get_db()`, `is_any_admin()`, `clear_state_keep_org()`, `org_structure_level()` — **main entry points**
- `tenant_manager.py` — multi-tenancy routing, org/role/department assignment
- `timezone_utils.py` — `get_user_time()`, `format_user_datetime()`, `get_utc_time()` (Amvera = UTC!)
- `*_handlers.py` — per-feature routers (sales, salary, reports, dashboard, plans, contests, absences, tasks…)
- `filter_utils.py` / `filter_handlers.py`, `pagination_utils.py`, `hints.py`, `notif_utils.py` — shared helpers
- `integration/` — Google Sheets (Device Flow OAuth, `trigger_export`)
- `billing_utils.py` — `has_module()`, `has_extension()` feature-gate API

### Web cabinet (`web/`)
- `web/app.py` — `create_web_app()`: FastAPI, Jinja2 globals, security headers, robots/sitemap, nav/module gating
- `web/auth.py` — sessions, CSRF, login nonce, password hashing
- `web/deps.py` — `get_web_db()` (sync `Database` + WAL); `web/rate_store.py` — persistent rate limiter
- `web/email_utils.py`, `web/push_utils.py` — email (SMTP) + Web Push VAPID
- `web/routes/` — one file per feature module (sales, products, staff, salary, admin, admin_billing, org_structure, email_auth, support…)
- `web/templates/` — per-module Jinja2; `base.html` (nav/PWA/dark-mode), `landing.html` (SEO)

### Data (SQLite, isolated per org)
- `data/main.db` — organizations, `user_org_mapping`
- `data/shop_bot.db` — personal mode + payments/subscriptions/billing (centralized)
- `data/tenants/org_*.db` — isolated per-org DBs (departments, org_roles, chat, etc.)

## Key architecture principles

Полный каталог — `PROJECT_MAP.md` §10.2. Самое нагруженное:

- **Multi-tenancy**: each org gets its own SQLite DB; `get_db()` routes automatically; migrations run on access (`create_tables()` inside `get_db()`)
- **Anchor message pattern**: FSM flows edit one message via `fsm_edit()`; `clear_state_keep_org()` AFTER `fsm_edit()`, never before
- **Role hierarchy**: `owner` → `admin`+scope → `user`; global super_admin (env_manager) separate; `is_any_admin()` — org role beats env_manager
- **Multi-scope**: `scope_value` = JSON array; `get_user_org_scope()` → `(scope_type, list[str])`; scope сети = `network`
- **Modular billing**: `billing_modules/extensions/bundles/module_subs` в `shop_bot.db`; `billing_utils.has_module()` — feature gate; priority super_admin → trial → direct → bundle; super-admin UI `/admin/billing`
- **Flexible org structure (`org_structure`, 349₽)**: кастомная роль раскладывается в существующие поля `user_org_mapping` → старый authz работает БЕЗ изменений; гейт смотрит биллинг ВЛАДЕЛЬЦА; `_nav_modules` override `deny`/`allow`(в пределах оплаты)/None
- **Salary unified formula**: `Итого = Оклад + Мотивация + Корректировки` — одинаково в боте, веб-admin, Excel; super_admin исключён везде
- **Timezone-aware**: `datetime.now()` on Amvera = UTC; all display via `timezone_utils`; stored scheduled times = UTC
- **Security**: CSRF on all POST (`verify_csrf_token`), security headers middleware, session cookies `httponly+secure+samesite`, persistent rate limiting; Telegram + email/password auth
- **PWA + Web Push**: SW v4, App Badge API, browser notifications polling, dark mode, keyboard shortcuts
- **deploy.sh** pushes GitHub + Amvera with hash verification; `--no-amvera` to skip

## Product (summary)

Sales/inventory/daily reports · multi-org with invite codes & roles · sales plans + milestone alerts · dashboards · contests · work schedules + shift templates · salary transparency · subscriptions/payments (SBP + YooKassa) + modular billing · rankings · Excel import/export · Google Sheets integration · internal org chat (topics + DM) · push & browser notifications · email/password login · flexible org structure.

Полный список фич с деталями — `PROJECT_MAP.md` §10.3.

## User Preferences

- Deploy always goes to GitHub + Amvera directly (`--no-amvera` to skip Amvera push)
- **Deploy запускать всегда напрямую**: `bash deploy.sh "сообщение"` — никаких фоновых задач, никаких Project Task для деплоя
- Amvera hash verification (`git ls-remote`) runs after every push to confirm sync
- Amvera "write outside persistenceMount" warnings are **FALSE POSITIVES** — all `data/` paths correctly resolve to `/app/data` (persistenceMount)
- No `.db`, `.pkl`, `data/tenants/` in GitHub repo — runtime data only on Amvera persistent volume

## Gotchas

1. `clear_state_keep_org(state)` **AFTER** `fsm_edit(...)`, never before — otherwise state clears before message edits
2. `plans_progress = []` must be initialized **before** try/except — NameError if exception and variable unused
3. `state.clear()` is **banned** — always use `clear_state_keep_org(state)` (preserves `selected_org_db`)
4. `he()` required for ALL user-provided strings in HTML messages; NOT needed in button text
5. `callback_data` limit 64 bytes — use `safe_cb()` / `resolve_cb_name()` for any user strings
6. `get_users_for_notifications()` → `[0]` = internal users.id, `[1]` = telegram_id — don't mix up
7. `Database.db_file` (not `.db_path`) — attribute name for DB file path
8. New router → register in `main.py`; new module → add to `test_imports.py`; new web route → add `Disallow:` to `_ROBOTS_TXT`
9. `get_sales_ranking()` returns 9 columns — 7th is `u.id` (user_db_id), 8th is `u.username`; use `row[:7]` for old 7-col unpacking
10. `get_user_org_scope()` returns `(scope_type, list[str])` NOT `(str, str)`
11. `datetime.now()` on Amvera = UTC — use `timezone_utils` for all display; `get_utc_time()` when storing user input
12. `shift_templates` weekday: 0=Пн, 6=Вс (Python `date.weekday()` convention)
13. `scheduled_notifications.scheduled_datetime` stored as UTC string
14. `sales_handlers.py` **не имеет глобального `logger`** — использует `import logging` + `logging.error()`; любой `logger.xxx()` вызовет `NameError` → outer except → удаление продаж!
15. В `complete_sale` outer `except Exception` удаляет все `processed_sales` через `delete_sale` — любой необработанный exception ПОСЛЕ `add_sale` → потеря продажи. Все вызовы внутри outer try должны быть в `try/except`.
16. **Invite back-button** — в `admin_handlers.py` кнопка «Назад» из формы редактирования инвайта ведёт на `personnel_hub` (НЕ `admin_management`)
17. **Google Sheets OAuth тип клиента**: при создании OAuth Client ID выбирать **«TVs and Limited Input devices»** — только этот тип поддерживает Device Flow. Скачанный JSON содержит ключ `"installed"` — это нормально.
18. **Email-only user identity**: `synthetic_tg_id = -(10_000_000 + cred_id)` в `web_credentials`; используется как `tg_id` в JWT; `org_db` берётся из `web_credentials.org_db`, не из `user_org_mapping`
19. **YANDEX_SMTP_PASSWORD** — это **пароль приложения** (16 символов без пробелов), НЕ пароль от аккаунта Яндекс (Яндекс ID → Безопасность → Пароли приложений)
20. **org_structure scope** — использовать `network`, НЕ `trade_network`; per-user `allow` гейтит на биллинг владельца, не безусловно
21. **JWT-библиотека — PyJWT** (`import jwt`), НЕ python-jose (убрана как уязвимая зависимость); ловить `jwt.PyJWTError`; токены jose↔PyJWT совместимы при том же HS256-секрете (живые сессии не инвалидируются)
22. **Money-grant идемпотентность** — любой путь «оплата подтверждена → выдача» (подписка/модуль/аддон) обязан быть идемпотентным по `payment_request_id` и покрыт тестом; внешний `except` в `confirm_payment_request` глотает ошибки выдачи → сверять имена колонок с живой схемой (`create_subscription_addon` пишет в `price`, не `amount_paid`)
23. **Restore бэкапа — только SQLite Backup API** (`src.backup(dest)` + retry), НЕ `shutil.copy2` поверх открытых соединений (иначе порча работающей БД)
24. **Веб event loop** — тяжёлые/блокирующие пути не держат loop: либо роут `def` (FastAPI → threadpool), либо `anyio.to_thread.run_sync`; соединения thread-safe (`check_same_thread=False` + threading.local pool)

## Pointers

- `PROJECT_MAP.md` — **главный справочник**: архитектура, схема БД, callback-схемы хендлеров, API модулей, §10 = полный веб-слой/решения/фичи
- `AGENT_HANDOFF.md` — полная история сессий, критические правила, схема БД
- Amvera config: `amvera.yml` — `persistenceMount: /app/data`, `command: python main.py`
