## Overview

This project is a professional, multi-tenant Telegram bot designed for comprehensive shop management. It allows users to manage products, track sales, and handle subscriptions with isolated databases for each organization.

## Run & Operate

- **Run**: `python main.py` (via workflow "Start application")
- **Deploy (GitHub + Amvera по умолчанию)**: `bash deploy.sh "commit message"`
- **Deploy только GitHub**: `bash deploy.sh "message" --no-amvera`
- **Env vars required**: `BOT_TOKEN`, `ADMIN_CHAT_ID`, `GITHUB_TOKEN` (all in Replit Secrets)
- **Google Sheets OAuth**: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (in Replit Secrets)

## Stack

**Telegram Bot:** Python 3.11 · aiogram 3 · SQLite · APScheduler · openpyxl · PickleStorage (FSM)
**Web Interface:** FastAPI + Uvicorn (port 5000) · Jinja2 · Tailwind CSS CDN · HTMX · Alpine.js · Chart.js
**Landing Page:** standalone `/` route — public promo page for unauthenticated visitors; redirects to `/dashboard` when logged in
**Infra:** Amvera (production hosting) · GitHub (version control)

## Where things live

### Bot
- `main.py` — bot entry, router registration, 10 APScheduler jobs
- `database.py` — Database class, 163+ methods, all migrations in `create_tables()`
- `db_utils.py` — `get_db()`, `is_any_admin()`, `clear_state_keep_org()` — **main entry points**
- `timezone_utils.py` — `get_user_time()`, `get_current_user_time()`, `format_user_datetime()`, `get_utc_time()`
- `dashboard_handlers.py` — `build_admin_dashboard(..., period)`, `build_user_dashboard(...)`, `_plan_summary_line()`
- `salary_handlers.py` — smeny calendar, shift templates (`slr_tmpl_`, `tmpl_*`), hour pickers (`slr_te/tw`, `tmpl_te/tw`); `salary_summary` — ФОТ-сводка (Оклад + Мотивация + Корр. на сотрудника, `asyncio.gather` для параллельного fetch мотивации)
- `pagination_utils.py` — `paginate()`, `page_nav_row()`, `PAGE_SIZE_*`
- `reports_handlers.py` — reports + rankings; `_ranking_period()`, `_period_kb()`
- `sales_plans_handlers.py` — plan wizard + edit; `_plan_summary_line()`
- `filter_utils.py` — `empty_filter()`, `get_available_filter_values()`, `merge_scope_with_filter()`, `build_filter_keyboard()`
- `filter_handlers.py` — `filter_router`: `flt_open_{back_cb}`, `ftog_s/c/n_*`, `flt_reset`
- `hints.py` — `hint_suffix()`, `maybe_send_welcome()`, `HINT_TEXTS`
- `notif_utils.py` — `add_read_btn()` adds «✅ Прочитано» to all push notifications
- `integration/` — Google Sheets integration; `integration/auth/google_oauth.py` (Device Flow); `integration/manager.py` (trigger_export)
- `data/main.db` — organizations, user_org_mapping
- `data/shop_bot.db` — personal mode + payments/subscriptions (centralized)
- `data/tenants/org_*.db` — isolated per-org DBs (Amvera: only `org_huawei.db`)

### Web Interface (`web/`)
- `web/app.py` — `create_web_app()`: FastAPI, Jinja2, router registration, Jinja2 globals; `SecurityHeadersMiddleware` (5 security headers on every response); `/robots.txt` and `/sitemap.xml` routes; `_api_rate_ok()` rate limiter (60 req/min/IP)
- `web/auth.py` — `get_session_user()`, `get_csrf_token()`, `verify_csrf_token()`, `generate_login_nonce()`, `verify_login_nonce()` (5-min HMAC stateless nonce for `/auth/code`)
- `web/rate_store.py` — SQLite-backed persistent rate limiter; `check_rate_limit(key, limit, window_sec)` → `(allowed, retry_after)`; survives restarts; used by `auth_routes.py`
- `web/deps.py` — `get_web_db(telegram_id, org_db)` → sync `Database(path)` + `_enable_wal()` (WAL+NORMAL on every call)
- `web/routes/` — 23 route files: `auth_routes`, `dashboard`, `sales`, `products`, `inventory`, `reports`, `rankings`, `staff`, `plans`, `salary`, `schedule`, `contests`, `settings`, `integration`, `payments`, `api`, `notifications`, `motivation`, `subscription`, `categories`, `promocodes`, `shops`, `pos`, `absences`, `support`
- `web/routes/support.py` — `GET /support` (форма обратной связи), `POST /support/send` (отправка через Telegram Bot API с rate limit 3 req/hour/user; `urllib.request` stdlib)
- `web/routes/pos.py` — GET `/pos`: передаёт `sales_limit_reached` + `sales_limit_msg` в шаблон; красный 🚫 баннер при достижении лимита продаж
- `web/routes/api.py` — `GET /api/sales-feed?since=ISO` (browser notification polling; rate-limited 60 req/min/IP; returns sales by other users; каждый item содержит поле `url` через `_notif_url()` для навигации по клику)
- `web/routes/notifications.py` — история уведомлений содержит поле `url` через `_notif_url(type)`; items с url — кликабельные ссылки (`<a>` в templates)
- `web/templates/base.html` — sidebar nav, PWA meta tags + manifest, swipe gestures JS, SW registration, browser notifications prompt+polling; уведомления кликабельны (mobile sheet + desktop dropdown): `@click` → navigate по `url`; иконки по типу (📦 low_stock, 📊 daily_report, 💳 payment и др.)
- `web/templates/landing.html` — public promo landing page (served at `/` for unauthenticated visitors); full SEO head: canonical, OG tags, Twitter card, JSON-LD SoftwareApplication schema, robots meta, keywords, favicon
- `web/templates/products/form.html` — product add/edit form (admin only)
- `web/templates/*/` — per-module Jinja2 templates
- `web/static/og-image.jpg` — OG social preview image 1200×630px (referenced in og:image + twitter:image meta tags)
- `web/static/og-image.png` — original PNG source of OG image

## Architecture decisions

- **Multi-tenancy**: each org gets its own SQLite DB; `get_db()` routes automatically
- **Anchor message pattern**: all FSM flows edit one message via `fsm_edit()`; `clear_state_keep_org()` MUST be called AFTER `fsm_edit()`, never before
- **Migrations run on access**: `create_tables()` is called inside `get_db()` for every DB path — auto-creates missing tables including `shift_templates`, `start_time`/`end_time` in `work_schedule`
- **Subscription tiers**: Бесплатный (0₽, 50 products/1 shop/100 sales, no features) → Базовый (500₽/30d, 200/3/500, export+analytics+notifications, NO integrations) → Стандарт (1200₽/90d, 500/10/1500, + Google Sheets) → Премиум (4000₽/365d, unlimited all). Migration always enforces `can_use_integrations=0` for Базовый. Default `trial_plan='Премиум'`.
- **deploy.sh always pushes to both GitHub + Amvera** (default); `--no-amvera` to skip; hash verification runs after every Amvera push; `AMVERA_ONLY_EXCLUDE_FILES` excludes AGENT_HANDOFF.md, replit.md, PROJECT_MAP.md, README.md from Amvera
- **Login nonce (stateless CSRF for `/auth/code`)**: `generate_login_nonce()` создаёт HMAC-SHA256(secret, timestamp//300); `verify_login_nonce()` принимает nonce ±1 окно (10 мин tolerance); не хранится в БД — чистый stateless
- **Persistent rate limiting**: `web/rate_store.py` — SQLite `data/rate_limits.db`; `check_rate_limit(key, limit, window_sec)` — выдерживает рестарты; используется в `auth_routes.py`; `_api_rate_ok()` (in-memory) остаётся для `/api/*`
- **FSM cleanup job**: `sqlite_storage.py` таблица `fsm_data` имеет колонку `updated_at TEXT`; еженедельный job `cleanup_fsm_storage` (воскресенье 04:30) удаляет записи старше 30 дней → 10-й APScheduler job
- **Timezone-aware**: `datetime.now()` on Amvera = UTC; all display uses `timezone_utils`; scheduled_notifications stored as UTC (`get_utc_time()` on write, `format_user_datetime()` on display)
- **Filter system**: `ADMIN_FILTER_KEY="admin_filter"` in FSM data; `flt_open_{back_cb}` opens panel; scope = ceiling, filter = floor (`merge_scope_with_filter`); `admin_filter` preserved by `clear_state_keep_org()`
- **plan_milestone_alerts**: UNIQUE(user_id, plan_id, milestone, period_start) — fires once per period
- **is_any_admin() priority**: org role in `user_org_mapping` takes precedence over `env_manager`
- **Role hierarchy**: `owner` → `admin`+scope → `user`; global super_admin (env_manager) is separate
- **Multi-scope**: `scope_value` stores JSON array; `get_user_org_scope()` → `(scope_type, list[str])`
- **Custom role titles**: `custom_title` in `user_org_mapping`; `get_role_display_label(..., custom_title)` uses it over computed label
- **Shift templates**: `shift_templates` table UNIQUE(user_id, weekday 0=Пн..6=Вс); `work_schedule` has `start_time`/`end_time`; slr_tog auto-applies template
- **Salary unified formula**: `Итого = Оклад (смены+оплач.отсутствия × ставка) + Мотивация (get_seller_total_earnings) + Корректировки (get_salary_adjustments_sum)`; применяется одинаково в боте `salary_summary`, веб admin `GET /salary`, Excel-экспорте; super_admin всегда исключается из всех зарплатных списков; detail-панель admin показывает построчные комиссии (`get_seller_earnings`); страница сотрудника `/salary/earnings` показывает построчные корректировки (`get_salary_adjustments`)
- **PWA**: `web/static/manifest.json` + SW at `/sw.js` (served via FastAPI route with `Service-Worker-Allowed: /`); SW caches `/static/` assets cache-first, authenticated routes network-only
- **SQLite WAL (web layer)**: `_enable_wal()` in `web/deps.py` sets `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` on every `get_web_db()` call — reduces bot/web lock contention
- **Browser notifications**: polling `/api/sales-feed?since=ISO` every 60s; permission prompt in «Ещё» sheet; `localStorage.ds_notif_since` checkpoint; fires only when `document.hidden`
- **Dark mode**: early script in `<head>` sets `.dark` on `<html>` from `localStorage.ds_dark` (no flash); `tailwind.config={darkMode:'class'}`; 80+ CSS overrides in `<style>` for all UI regions; 🌙/☀️ toggle in topbar + CSS toggle switch in More sheet; `dsToggleDark()` persists to localStorage; `Alt+D` keyboard shortcut
- **Keyboard shortcuts**: `Alt+D` dark mode; `Alt+N` primary action; `/` focus search; `Escape` close sheet; `?` show hint overlay (3.5s)
- **Security headers**: `SecurityHeadersMiddleware` in `web/app.py` adds to every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, `X-XSS-Protection: 1; mode=block`; HSTS (`Strict-Transport-Security: max-age=31536000`) fires only when `request.url.scheme == "https"` — safe for both dev and prod
- **Session cookie security**: `httponly=True`, `secure=True`, `samesite='lax'`, `max_age=7d`; secret derived from `SHA256(BOT_TOKEN)` — rotates automatically if BOT_TOKEN changes
- **CSRF**: all POST routes use `verify_csrf_token(request, form_token)` — token is HMAC-SHA256 of session JWT, deterministic per session, checked with `hmac.compare_digest`; GET `/auth/code` serves `login_nonce`; POST verifies via `verify_login_nonce()`
- **Telegram auth**: `verify_telegram_auth()` checks HMAC against BOT_TOKEN + rejects `auth_date` older than 24h
- **Rate limiting**: auth routes — 5 req/60s/IP (`check_rate_limit` из `web/rate_store.py`, **persistent** SQLite); `/api/*` endpoints — 60 req/60s/IP (`_api_rate_ok`, in-memory); `/support/send` — 3 req/60min/telegram_id (`_rate_store` in `support.py`, in-memory)
- **Clickable notifications**: `_notif_url(notification_type)` в `api.py` и `notifications.py` роутит тип → URL (`/sales`, `/products`, `/dashboard` и др.); `base.html` mobile sheet и desktop dropdown рендерят `<div @click>` с навигацией; `notifications/index.html` history items с url — `<a>` теги
- **robots.txt**: `/robots.txt` route in `web/app.py` — allows only `/$` and `/static/`; disallows all 25 app routes (`/dashboard`, `/sales`, `/api/`, `/support`, `/absences`, etc.) to prevent crawl budget waste and structure leakage; `Sitemap:` pointer included; **when adding a new route — always add `Disallow:` to `_ROBOTS_TXT` in `web/app.py`**
- **sitemap.xml**: `/sitemap.xml` route — single entry `https://dailysales.app/` with `priority=1.0`, `changefreq=weekly`; submit to Google Search Console + Яндекс.Вебмастер for fast indexing
- **SEO meta (landing.html)**: canonical `https://dailysales.app/`; `og:image/url/locale(ru_RU)/site_name`; `twitter:card=summary_large_image`; JSON-LD `SoftwareApplication` with offers (0–4000₽), aggregateRating, featureList; `<link rel="icon">` for favicon in browser tab; `keywords` meta; `robots: index, follow`
- **OG image**: `web/static/og-image.jpg` (118KB, 1408×768 → served as 1200×630 crop by social platforms); source PNG at `web/static/og-image.png` (824KB); both in git repo (not excluded)

## Product

- Sales management: products, inventory, sales recording, daily reports
- Quick search: «🔍 Найти товар» — case-insensitive search among in-stock products; qty buttons [1,2,3,5,10,20,50]
- Cross-shop sale: сотрудник торговой сети может продавать/списывать с другого магазина
- Multi-org: invite codes, role system (super-admin / owner / admin / user), isolated data
- Sales plans: per-seller or per-shop, weekly/monthly, turnover/quantity, category/product filter, milestone alerts
- Dashboard: period toggle (Сегодня/Неделя/Месяц); admin sees staff + earnings + plans; user sees salary + period sales + plans
- Contests: create, auto-finish, winner calculation via APScheduler; archive clear with confirmation
- Work schedule: admin marks days, hour-level time pickers, shift templates (⏰ Расписание смен) per weekday
- Salary transparency: все три вида — бот ФОТ-сводка, веб admin `/salary`, веб user `/salary/earnings` — показывают Оклад + Мотивация + Корр. = Итого; admin detail-панель раскрывает комиссии по каждой продаже; пользователь видит каждую корректировку с комментарием; Excel-экспорт содержит колонку Мотивация (7 колонок итого)
- Subscriptions/payments: tariff plans (Бесплатный/Базовый/Стандарт/Премиум), promocodes, Excel export; SBP (manual screenshot) + YooKassa (auto); trial 14d = Премиум-level access via `_has_active_trial()` → `_UNLIMITED`
- Rankings: sellers / shops / cities; period toggle (7д / месяц / прошлый); user's own position if outside top-10
- Favorites & Recent: ⭐ Избранное + 🔄 Недавние in product selection
- Excel import: 📊 Импорт из Excel; openpyxl parse + preview + confirm
- Pagination: продукты, продажи, пользователи, орги, конкурсы
- Manual filters: 🔍 Фильтр in Reports, Rankings, Users, Plans progress; scope=ceiling, filter=floor
- Push notifications: «✅ Прочитано» on all notifications; scheduled notifications with UTC-correct timing
- Onboarding & hints: welcome popup (once, role-aware); section hints (once per section); «✅ Понятно!»
- Google Sheets integration: экспорт продаж в таблицу; авторизация через Device Flow (OAuth 2.0); триггер `trigger_export(db, 'sales', event)` в `complete_sale`; таблицы `integration_connections`, `integration_exports`, `integration_log`, `gs_bonus_cache` в org_*.db; **только тариф Стандарт+** (can_use_integrations=0 для Базового)
- Shift sale alerts: push-уведомление коллегам в магазине при каждой продаже (если стоит смена + включена настройка `shift_sale_alerts`)
- Trial upsell: `send_trial_expired_upsell` (ежечасно :05) + reminders at 14/7/3/1d (`send_payment_alerts`); dedup via `subscription_reminder_log` (threshold -1 = expired)
- Auto-reject stale payments: `auto_reject_stale_payments` (ежедневно 10:15) отклоняет pending СБП-заявки >72ч и уведомляет пользователя
- Web feedback form: `/support` — форма обратной связи в веб-кабинете; категория + тема + сообщение; отправляет супер-админу в Telegram через Bot API; rate limit 3/час/user; доступна всем авторизованным пользователям через шторку «Ещё»

## User Preferences

- Deploy always goes to GitHub + Amvera directly (`--no-amvera` to skip Amvera push)
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
8. New router → register in `main.py`; new module → add to `test_imports.py`
9. `get_sales_ranking()` returns 9 columns — 7th is `u.id` (user_db_id), 8th is `u.username`; use `row[:7]` for old 7-col unpacking
10. `get_user_org_scope()` returns `(scope_type, list[str])` NOT `(str, str)`
11. `datetime.now()` on Amvera = UTC — use `timezone_utils` for all display; `get_utc_time()` when storing user input
12. `shift_templates` weekday: 0=Пн, 6=Вс (Python `date.weekday()` convention)
13. `scheduled_notifications.scheduled_datetime` stored as UTC string
14. `sales_handlers.py` **не имеет глобального `logger`** — использует `import logging` + `logging.error()`; любой `logger.xxx()` вызовет `NameError` → outer except → удаление продаж!
15. В `complete_sale` outer `except Exception` удаляет все `processed_sales` через `delete_sale` — любой необработанный exception ПОСЛЕ `add_sale` → потеря продажи. Все вызовы внутри outer try должны быть в `try/except`.
16. **Invite back-button** — в `admin_handlers.py` кнопка «Назад» из формы редактирования инвайта ведёт на `personnel_hub` (НЕ `admin_management`); при изменениях в этом блоке проверять `back_button("personnel_hub")`
17. **Google Sheets OAuth тип клиента**: при создании OAuth Client ID в Google Cloud Console выбирать **«TVs and Limited Input devices»** — только этот тип поддерживает Device Flow (`https://oauth2.googleapis.com/device/code`). Скачанный JSON содержит ключ `"installed"` — это нормально. `client_id` и `client_secret` → в Replit Secrets как `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.

## Pointers

- See `AGENT_HANDOFF.md` for full session history, critical rules, and DB schema
- See `PROJECT_MAP.md` for handler callback schemes and module API reference
- Amvera config: `amvera.yml` — `persistenceMount: /app/data`, `command: python main.py`
