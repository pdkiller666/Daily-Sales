## Overview

This project is a professional, multi-tenant Telegram bot designed for comprehensive shop management. It allows users to manage products, track sales, and handle subscriptions with isolated databases for each organization.

## Run & Operate

- **Run**: `python main.py` (via workflow "Start application")
- **Deploy (GitHub + Amvera по умолчанию)**: `bash deploy.sh "commit message"`
- **Deploy только GitHub**: `bash deploy.sh "message" --no-amvera`
- **Env vars required**: `BOT_TOKEN`, `ADMIN_CHAT_ID`, `GITHUB_TOKEN` (all in Replit Secrets)
- **Google Sheets OAuth**: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (in Replit Secrets)

## Stack

- Python 3.11 · aiogram 3 · SQLite · APScheduler · openpyxl · PickleStorage (FSM)
- Amvera (production hosting) · GitHub (version control)

## Where things live

- `main.py` — bot entry, router registration, 7 APScheduler jobs
- `database.py` — Database class, 163+ methods, all migrations in `create_tables()`
- `db_utils.py` — `get_db()`, `is_any_admin()`, `clear_state_keep_org()` — **main entry points**
- `timezone_utils.py` — `get_user_time()`, `get_current_user_time()`, `format_user_datetime()`, `get_utc_time()`
- `dashboard_handlers.py` — `build_admin_dashboard(..., period)`, `build_user_dashboard(...)`, `_plan_summary_line()`
- `salary_handlers.py` — smeny calendar, shift templates (`slr_tmpl_`, `tmpl_*`), hour pickers (`slr_te/tw`, `tmpl_te/tw`)
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

## Architecture decisions

- **Multi-tenancy**: each org gets its own SQLite DB; `get_db()` routes automatically
- **Anchor message pattern**: all FSM flows edit one message via `fsm_edit()`; `clear_state_keep_org()` MUST be called AFTER `fsm_edit()`, never before
- **Migrations run on access**: `create_tables()` is called inside `get_db()` for every DB path — auto-creates missing tables including `shift_templates`, `start_time`/`end_time` in `work_schedule`
- **deploy.sh always pushes to both GitHub + Amvera** (default); `--no-amvera` to skip; hash verification runs after every Amvera push; `AMVERA_ONLY_EXCLUDE_FILES` excludes AGENT_HANDOFF.md, replit.md, PROJECT_MAP.md, README.md from Amvera
- **Timezone-aware**: `datetime.now()` on Amvera = UTC; all display uses `timezone_utils`; scheduled_notifications stored as UTC (`get_utc_time()` on write, `format_user_datetime()` on display)
- **Filter system**: `ADMIN_FILTER_KEY="admin_filter"` in FSM data; `flt_open_{back_cb}` opens panel; scope = ceiling, filter = floor (`merge_scope_with_filter`); `admin_filter` preserved by `clear_state_keep_org()`
- **plan_milestone_alerts**: UNIQUE(user_id, plan_id, milestone, period_start) — fires once per period
- **is_any_admin() priority**: org role in `user_org_mapping` takes precedence over `env_manager`
- **Role hierarchy**: `owner` → `admin`+scope → `user`; global super_admin (env_manager) is separate
- **Multi-scope**: `scope_value` stores JSON array; `get_user_org_scope()` → `(scope_type, list[str])`
- **Custom role titles**: `custom_title` in `user_org_mapping`; `get_role_display_label(..., custom_title)` uses it over computed label
- **Shift templates**: `shift_templates` table UNIQUE(user_id, weekday 0=Пн..6=Вс); `work_schedule` has `start_time`/`end_time`; slr_tog auto-applies template

## Product

- Sales management: products, inventory, sales recording, daily reports
- Quick search: «🔍 Найти товар» — case-insensitive search among in-stock products; qty buttons [1,2,3,5,10,20,50]
- Cross-shop sale: сотрудник торговой сети может продавать/списывать с другого магазина
- Multi-org: invite codes, role system (super-admin / owner / admin / user), isolated data
- Sales plans: per-seller or per-shop, weekly/monthly, turnover/quantity, category/product filter, milestone alerts
- Dashboard: period toggle (Сегодня/Неделя/Месяц); admin sees staff + earnings + plans; user sees salary + period sales + plans
- Contests: create, auto-finish, winner calculation via APScheduler; archive clear with confirmation
- Work schedule: admin marks days, hour-level time pickers, shift templates (⏰ Расписание смен) per weekday
- Subscriptions/payments: tariff plans, promocodes, Excel export
- Rankings: sellers / shops / cities; period toggle (7д / месяц / прошлый); user's own position if outside top-10
- Favorites & Recent: ⭐ Избранное + 🔄 Недавние in product selection
- Excel import: 📊 Импорт из Excel; openpyxl parse + preview + confirm
- Pagination: продукты, продажи, пользователи, орги, конкурсы
- Manual filters: 🔍 Фильтр in Reports, Rankings, Users, Plans progress; scope=ceiling, filter=floor
- Push notifications: «✅ Прочитано» on all notifications; scheduled notifications with UTC-correct timing
- Onboarding & hints: welcome popup (once, role-aware); section hints (once per section); «✅ Понятно!»
- Google Sheets integration: экспорт продаж в таблицу; авторизация через Device Flow (OAuth 2.0); триггер `trigger_export(db, 'sales', event)` в `complete_sale`; таблицы `integration_connections`, `integration_exports`, `integration_log`, `gs_bonus_cache` в org_*.db
- Shift sale alerts: push-уведомление коллегам в магазине при каждой продаже (если стоит смена + включена настройка `shift_sale_alerts`)

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
9. `get_sales_ranking()` returns 8 columns — 8th is `u.id` (user_db_id); use `row[:7]` for old 7-col unpacking
10. `get_user_org_scope()` returns `(scope_type, list[str])` NOT `(str, str)`
11. `datetime.now()` on Amvera = UTC — use `timezone_utils` for all display; `get_utc_time()` when storing user input
12. `shift_templates` weekday: 0=Пн, 6=Вс (Python `date.weekday()` convention)
13. `scheduled_notifications.scheduled_datetime` stored as UTC string
14. `sales_handlers.py` **не имеет глобального `logger`** — использует `import logging` + `logging.error()`; любой `logger.xxx()` вызовет `NameError` → outer except → удаление продаж!
15. В `complete_sale` outer `except Exception` удаляет все `processed_sales` через `delete_sale` — любой необработанный exception ПОСЛЕ `add_sale` → потеря продажи. Все вызовы внутри outer try должны быть в `try/except`.
16. **Google Sheets OAuth тип клиента**: при создании OAuth Client ID в Google Cloud Console выбирать **«TVs and Limited Input devices»** — только этот тип поддерживает Device Flow (`https://oauth2.googleapis.com/device/code`). Скачанный JSON содержит ключ `"installed"` — это нормально. `client_id` и `client_secret` → в Replit Secrets как `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.

## Pointers

- See `AGENT_HANDOFF.md` for full session history, critical rules, and DB schema
- See `PROJECT_MAP.md` for handler callback schemes and module API reference
- Amvera config: `amvera.yml` — `persistenceMount: /app/data`, `command: python main.py`
