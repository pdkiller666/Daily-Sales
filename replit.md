## Overview

This project is a professional, multi-tenant Telegram bot designed for comprehensive shop management. It allows users to manage products, track sales, and handle subscriptions with isolated databases for each organization.

## Run & Operate

- **Run**: `python main.py` (via workflow "Start application")
- **Deploy (GitHub + Amvera по умолчанию)**: `bash deploy.sh "commit message"`
- **Deploy только GitHub**: `bash deploy.sh "message" --no-amvera`
- **Env vars required**: `BOT_TOKEN`, `ADMIN_CHAT_ID`, `GITHUB_TOKEN` (all in Replit Secrets)

## Stack

- Python 3.11 · aiogram 3 · SQLite · APScheduler · openpyxl · PickleStorage (FSM)
- Amvera (production hosting) · GitHub (version control)

## Where things live

- `main.py` — bot entry, router registration, 7 APScheduler jobs
- `database.py` — Database class, 133+ methods, all migrations in `create_tables()`
- `db_utils.py` — `get_db()`, `is_any_admin()`, `clear_state_keep_org()` — **main entry points**
- `dashboard_handlers.py` — `build_admin_dashboard(..., period='today'/'week'/'month')`, `build_user_dashboard(..., period=...)`; helpers: `_on_shift_details()`, `_today_total_earnings()`
- `pagination_utils.py` — `paginate()`, `page_nav_row()`, `PAGE_SIZE_DEFAULT/SALES/USERS/ORGS`
- `reports_handlers.py` — all reports + rankings; helpers: `_ranking_period()`, `_period_kb()`
- `sales_plans_handlers.py` — plan wizard + edit + прогресс планов; `_plan_summary_line()` for shared display format
- `filter_utils.py` — `empty_filter()`, `get_available_filter_values()`, `merge_scope_with_filter()`, `build_filter_keyboard()` — shared filter infra
- `filter_handlers.py` — `filter_router`: `flt_open_{back_cb}`, `ftog_s/c/n_*`, `flt_reset` handlers
- `data/main.db` — organizations, user_org_mapping
- `data/shop_bot.db` — personal mode + payments/subscriptions (centralized)
- `data/tenants/org_*.db` — isolated per-org DBs (Amvera: only `org_huawei.db`)

## Architecture decisions

- **Multi-tenancy**: each org gets its own SQLite DB; `get_db()` routes automatically
- **Anchor message pattern**: all FSM flows edit one message via `fsm_edit()`; `clear_state_keep_org()` MUST be called AFTER `fsm_edit()`, never before
- **Migrations run on access**: `create_tables()` is called inside `get_db()` for every DB path — this auto-creates missing tables on first access, including super-admin org context
- **deploy.sh always pushes to both GitHub + Amvera** (default); use `--no-amvera` to skip Amvera; hash verification runs after every Amvera push; `clean_dst()` wipes dst before sync — no stale files; `AMVERA_ONLY_EXCLUDE_FILES` excludes AGENT_HANDOFF.md, replit.md, PROJECT_MAP.md, README.md from Amvera
- **Filter system**: `ADMIN_FILTER_KEY="admin_filter"` in FSM data; `flt_open_{back_cb}` opens panel; toggles `ftog_s/c/n_*` update state immediately; "✅ Применить" = back_cb button; `flt_reset` clears; scope is ceiling — filter is floor (merge_scope_with_filter); `admin_filter` preserved by `clear_state_keep_org()`
- **plan_milestone_alerts**: UNIQUE(user_id, plan_id, milestone, **period_start**) — milestone fires once per period (week/month start date). Migration auto-recreates table if `period_start` column missing.
- **is_any_admin() priority**: org role in `user_org_mapping` takes precedence over `env_manager` — prevents join-mode users from getting admin rights
- **Role hierarchy** (org-level): `owner` (Директор) → `admin`+scope (Зам/Магазин/Город/Сеть) → `user` (Сотрудник); global super_admin (env_manager) is separate. `scope_type`/`scope_value`/`custom_title` columns in `user_org_mapping`. Migration: `super_admin` → `owner` on every startup. Dashboard/reports/rankings filter by scope automatically.
- **Multi-scope**: `scope_value` stores JSON array `["val1","val2"]`; `get_user_org_scope()` returns `(scope_type, list[str])`; DB methods accept `shop_names/cities/trade_networks` list params with IN clause. UI: toggle multi-select (`adm_t_s_*`, `adm_t_c_*`, `adm_t_n_*`) + `adm_scope_submit`.
- **Custom role titles**: `custom_title` column in `user_org_mapping`; set via `tenant_manager.set_user_title()`; `get_role_display_label(..., custom_title)` uses it over computed label; `AdminUserStates.waiting_for_admin_title` FSM state; accessible from user card (🏷️) and role menu.

## Product

- Sales management: products, inventory, sales recording, daily reports
- Multi-org: invite codes, role system (super-admin / admin / user), isolated data
- Sales plans: per-seller or per-shop, weekly/monthly, turnover/quantity, category/product filter
- Dashboard (Reports): period toggle (Сегодня/Неделя/Месяц); admin sees staff + earnings + plans; user sees salary + period sales + plans
- Contests: create, auto-finish, winner calculation via APScheduler; archive clear with confirmation
- Subscriptions/payments: tariff plans, promocodes, Excel export
- Rankings: sellers / shops / cities; period toggle (7д / месяц / прошлый); user's own position if outside top-10
- Favorites & Recent: ⭐ Избранное + 🔄 Недавние блоки в начале экрана выбора товара (sale_product_{id})
- Quick confirm: ⚡ Продать сейчас — кнопка при первом товаре в корзине
- Plan milestones: 50%/75%/100% уведомления; таблица `plan_milestone_alerts` исключает дубли
- Excel import: 📊 Импорт из Excel в меню Товары; openpyxl-парсинг + превью + подтверждение
- Pagination everywhere: продукты (prodl_pg_), продажи для редактирования (esl_pg_), пользователи (au_pg_), орги (orgs_pg_), конкурсы (cal_pg_/car_pg_)
- Manual filters (scope-aware): кнопка 🔍 Фильтр в Отчётах, Рейтингах, Сотрудниках, Прогрессе планов; scope роли — потолок, ручной фильтр — пол; глобальный ключ FSM `admin_filter`; `flt_open_{back_cb}` pattern

## User Preferences

- Deploy always goes to GitHub + Amvera directly (`--no-amvera` to skip Amvera push)
- Amvera hash verification (`git ls-remote`) runs after every push to confirm sync
- Amvera "write outside persistenceMount" warnings are **FALSE POSITIVES** — all `data/` paths correctly resolve to `/app/data` (persistenceMount)
- No `.db`, `.pkl`, `data/tenants/` in GitHub repo — runtime data only on Amvera persistent volume

## Gotchas

1. `clear_state_keep_org(state)` **AFTER** `fsm_edit(...)`, never before — otherwise state clears before message edits
2. `plans_progress = []` must be initialized **before** try/except — NameError if exception and variable unused
3. `state.clear()` is **banned** — always use `clear_state_keep_org(state)` (preserves `selected_org_db`)
4. `he()` required for ALL user-provided strings in HTML messages (shop_name, first_name, product_name, etc.)
5. `callback_data` limit 64 bytes — use `safe_cb()` / `resolve_cb_name()` for any user strings
6. `get_users_for_notifications()` → `[0]` = internal users.id, `[1]` = telegram_id — don't mix up
7. `Database.db_file` (not `.db_path`) — attribute name for DB file path
8. New router → register in `main.py`; new module → add to `test_imports.py`
9. `get_sales_ranking()` returns 8 columns — 8th is `u.id` (user_db_id); use `row[:7]` for old 7-col unpacking
10. `get_user_org_scope()` returns `(scope_type, list[str])` NOT `(str, str)` — scope_values is always a list (empty = full access)
11. `data/` is **excluded from both repos** — runtime DBs live on Amvera persistenceMount only; never commit DB files

## Pointers

- See `AGENT_HANDOFF.md` for full session history, critical rules, and DB schema
- Amvera config: `amvera.yml` — `persistenceMount: /app/data`, `command: python main.py`
