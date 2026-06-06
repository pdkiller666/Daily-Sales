# Карта проекта: Telegram Bot для управления розничными продажами
> Последнее обновление: 2026-06-06 · 51 модуль · GitHub `13e53dc` · Amvera `e6c49da`

## 1. ОБЩАЯ АРХИТЕКТУРА

```
Telegram API
     │
   main.py  ──── запускает polling, регистрирует роутеры, инициализирует планировщик
     │
   ┌─┴──────────────────────────────────────────────────────────────┐
   │                    РОУТЕРЫ (23 штуки)                          │
   │  router              ← handlers.py         (старт, профиль)   │
   │  admin_router        ← admin_handlers.py   (орг, юзеры)       │
   │  sales_router        ← sales_handlers.py   (продажи)          │
   │  products_router     ← products_handlers.py (каталог товаров)  │
   │  inventory_router    ← inventory_handlers.py (склад)           │
   │  reports_router      ← reports_handlers.py (отчёты, рейтинги)  │
   │  commission_router   ← commission_handlers.py (мотивация)      │
   │  earnings_router     ← earnings_handlers.py (заработок)        │
   │  contacts_router     ← contacts_handlers.py (контакты)         │
   │  notifications_router← notifications_handlers.py (уведомления) │
   │  payment_admin_router← payment_admin_handlers.py (оплата юзер) │
   │  payment_system_router← payment_system_admin.py (оплата адм)   │
   │  subscription_router ← subscription_handlers.py (подписки)     │
   │  backup_router       ← backup_handlers.py  (бэкапы)            │
   │  sales_plans_router  ← sales_plans_handlers.py (планы продаж)  │
   │  salary_router       ← salary_handlers.py  (зарплаты/смены)    │
   │  contests_router     ← contests_handlers.py (конкурсы)         │
   │  dashboard_router    ← dashboard_handlers.py (дашборд)         │
   │  filter_router       ← filter_handlers.py  (общий фильтр)      │
   │  referral_router     ← referral_handlers.py (реф. программа)   │
   │  addon_router        ← addon_handlers.py   (надстройки)        │
   │  absence_router      ← absence_handlers.py (отсутствия)        │
   │  web_auth_router     ← web_auth_handlers.py (/setweblogin)     │
   └────────────────────────────────────────────────────────────────┘
     │
   ┌─┴──────────────────────────────────────────────────────────────┐
   │                  СЛОЙ ДАННЫХ                                   │
   │  db_utils.py   ← get_db(id, state), is_any_admin()            │
   │  database.py   ← класс Database (170+ методов)                │
   │  tenant_manager.py ← маршрутизация БД по org                  │
   │  env_manager.py    ← ADMIN_CHAT_ID, BOT_TOKEN                 │
   └────────────────────────────────────────────────────────────────┘
     │
   ┌─┴──────────────────────────────────────────────────────────────┐
   │                  БАЗЫ ДАННЫХ                                   │
   │  data/main.db           ← organizations, user_org_mapping      │
   │  data/shop_bot.db       ← личный режим + платежи/подписки      │
   │  data/fsm_storage.pkl   ← FSM состояния (PickleStorage)        │
   │  data/tenants/org_*.db  ← изолированные БД организаций         │
   └────────────────────────────────────────────────────────────────┘
```

---

## 2. БАЗЫ ДАННЫХ — СТРУКТУРА И ТАБЛИЦЫ

### data/main.db  (центральная, только tenant_manager)

| Таблица | Описание |
|---|---|
| `organizations` | id, name, owner_id, invite_code, db_path, subscription_plan, is_active |
| `user_org_mapping` | telegram_id, org_id, role (owner/admin/user), scope_type, scope_value (JSON array), custom_title |

### data/shop_bot.db  и  data/tenants/org_*.db  (идентичная схема)

| Таблица | Описание |
|---|---|
| `users` | id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, timezone, **username** (индекс 12) |
| `products` | id, name, category, price, motivation_type, motivation_value, **photo_file_id** TEXT (Telegram file_id), **description** TEXT — добавлены ALTER TABLE миграцией |
| `inventory` | id, shop_name, product_id, quantity, last_updated |
| `inventory_history` | id, shop_name, product_id, quantity_change, change_type, change_reason, user_id, timestamp |
| `sales` | id, product_id, shop_name, quantity_sold, sale_price, user_id, sale_date |
| `seller_earnings` | id, user_id, sale_id, product_id, quantity, base_amount, commission_amount, total_amount, sale_date |
| `motivation_rules` | id, product_id, commission_type (fixed/percent), commission_value, admin_id |
| `motivation_conditions` | условия мотивации (сверхплан, коэф. смены, фильтр категорий) |
| `motivation_extra_conditions` | доп. условия (создана миграцией в create_tables) |
| `salary_settings` | user_id, daily_rate, updated_by |
| `work_schedule` | user_id, work_date, **start_time**, **end_time**, marked_by — UNIQUE(user_id, work_date) |
| `shift_templates` | user_id, weekday (0=Пн..6=Вс), start_time, end_time — UNIQUE(user_id, weekday) |
| `notification_settings` | user_id, low_stock_enabled, daily_reports_enabled, sales_alerts_enabled, ... |
| `notification_history` | id, user_id, notification_type, message, created_at, is_read |
| `scheduled_notifications` | id, job_id(UUID), created_by(FK→users.id!), notification_text, recipients_type, scheduled_datetime(**UTC**), status |
| `sales_plans` | id, plan_type, metric_type, target_value, target_type, user_id, shop_name, filter_type, filter_value, is_active, created_by |
| `plan_milestone_alerts` | user_id, plan_id, milestone (50/75/100), period_start — UNIQUE(…, period_start) |
| `contests` | id, title, contest_type, scope, metric, target_value, reward_type, reward_value, start_date, end_date, status, winner_user_id |
| `user_hints_seen` | user_id, hint_key, seen_at |
| `user_product_favorites` | user_id, product_id |
| `user_product_recent` | user_id, product_id, last_used |
| `absence_type_settings` | id, type (vacation/sick/compensatory/absence/other), is_paid, annual_limit, penalty_mode, penalty_amount, updated_at — UNIQUE(type); 5 записей по умолчанию |
| `absence_records` | id, user_id, type, start_date, end_date, status (pending/approved/rejected/cancelled), is_paid, comment, admin_comment, created_by, reviewed_by, created_at, reviewed_at |

### data/shop_bot.db ТОЛЬКО (платежи всегда централизованы)

| Таблица | Описание |
|---|---|
| `subscriptions` | id, user_id, plan_type, start_date, end_date, is_trial |
| `subscription_reminder_log` | user_id, threshold, subscription_end, sent_at |
| `payment_requests` | id, user_id, plan_type, amount, payment_proof_file_id, status, created_at, promocode_id |
| `payment_settings` | key, value — карта, реквизиты, trial_days, trial_plan, **payment_provider** ('sbp'/'yookassa'), **yookassa_shop_id**, **yookassa_secret_key**, **yookassa_return_url** |
| `subscription_plans` | id, name, price, duration_days, max_products, max_shops, features (JSON), **can_use_integrations** (0 для Базового!) |
| `promocodes` | id, code, discount_percent, max_usage, current_usage, is_active |
| `yookassa_payments` | id, yookassa_payment_id UNIQUE, user_id, plan_type, amount, status, promocode_id, is_scheduled, schedule_date |
| `referrals` | id, referrer_id (telegram_id), referred_id (telegram_id), created_at, bonus_applied (0/1) |
| `subscription_addons` | id, user_id, addon_type ('extra_shops'/'extra_products'), quantity, expires_at, created_at |
| `web_credentials` | id, email UNIQUE, password_hash (PBKDF2-SHA256), telegram_id (nullable FK), synthetic_tg_id, org_db, first_name, email_verified (0/1), verify_token, verify_expires (unix ts), reset_token, reset_expires (unix ts), last_login, created_at — **email+пароль аутентификация** |
| `push_subscriptions` | id, telegram_id, endpoint, p256dh, auth, created_at — UNIQUE(telegram_id, endpoint) — **Web Push VAPID подписки** |

### Индексы (create_tables, все IF NOT EXISTS)

```
idx_sales_user_date, idx_sales_shop_date, idx_inventory_shop_prod,
idx_work_schedule_date, idx_seller_earnings_sale,
idx_users_shop_name, idx_users_telegram_id,
idx_absence_user (absence_records · user_id+start_date),
idx_absence_status (absence_records · status)
```

---

## 3. КЛЮЧЕВЫЕ МОДУЛИ

### db_utils.py — точка входа к БД

```
get_db(telegram_id, state)          ← ОСНОВНАЯ функция (async). Возвращает AsyncDatabase.
                                       super-admin + selected_org_db → AsyncDatabase(Database(org_db))
                                       user в org                    → AsyncDatabase(Database(org_*.db))
                                       иначе                         → AsyncDatabase(Database(shop_bot.db))
get_db_sync(telegram_id)            ← для синхронных контекстов (APScheduler) → Database (sync)
wrap_db(db: Database)               ← явная обёртка inline Database() → AsyncDatabase
is_any_admin(telegram_id)           ← проверяет ADMIN_CHAT_ID ИЛИ роль owner/admin в user_org_mapping
get_user_org_role(telegram_id)      ← 'owner'/'admin'/'user'/None из user_org_mapping
get_user_org_scope(telegram_id)     ← (scope_type, list[str]) — тип и список зон доступа
get_user_full_scope(telegram_id)    ← (scope_type, list[str], custom_title)
get_role_display_label(role, scope_type, scope_values, custom_title=None) ← текст роли
clear_state_keep_org(state, extra_keys=None) ← очистка state с сохранением selected_org_db
                                              extra_keys — доп. ключи FSM для сохранения
```

**AsyncDatabase (db_utils.py):**
```python
# __getattr__: callable атрибуты → asyncio.to_thread(fn, *args, **kwargs)
# db_file: explicit @property (без to_thread) → прямое обращение
# sync доступ: getattr(db, '_db', db) → Database (для hints.py, username sync)
# wrap_db(Database(...)) → AsyncDatabase  (использовать вместо inline Database())

# Параллельные запросы — asyncio.gather():
results = await asyncio.gather(
    db.method_a(), db.method_b(), db.method_c(),
    return_exceptions=True,
)
# Проверка: not isinstance(results[i], Exception)
```

**Пул соединений (database.py):**
```python
# threading.local() пул: один поток = одно соединение на db_file
# _PooledConn.close() = rollback (не разрывает соединение)
# get_connection() → _PooledConn — используется во ВСЕХ методах Database
# Все методы Database используют self.get_connection() (не sqlite3.connect напрямую)
```

### env_manager.py — переменные окружения

```
is_super_admin(chat_id)  ← ID == 921098636 или первый в ADMIN_CHAT_ID
is_admin(chat_id)        ← любой ID из ADMIN_CHAT_ID  [!] не знает об org-ролях
get_admin_ids()          ← читает os.environ, затем data/.env
get_bot_token()          ← BOT_TOKEN
```

**Важно:** В handlers используй `is_any_admin()` из db_utils, НЕ `env_manager.is_admin()`.

### timezone_utils.py — работа с часовыми поясами

```python
get_user_time(naive_dt, tz_name)         → datetime  — naive UTC → tz-aware local
get_current_user_time(tz_name)           → datetime  — текущее время в TZ пользователя
format_user_datetime(raw_str, tz, fmt)   → str       — ISO UTC строка → форматированное локальное
get_utc_time(naive_local_dt, tz_name)    → datetime  — local naive → UTC

# ВАЖНО: datetime.now() на Amvera = UTC. Для отображения всегда используй timezone_utils.
# raw_str может быть ISO datetime или "HH:MM" — оба обрабатываются.
```

### utils.py — вспомогательные функции

```python
he(text)                      → str   — html.escape для user-строк в HTML-сообщениях
escape_md(text)               → str   — экранирование для MarkdownV2 (устаревший, использовать he())
format_currency(amount)       → str   — форматирование суммы (1 234,56 ₽)
format_price(price)           → str   — форматирование цены
generate_excel_report(sales, title, start_date, end_date) → Workbook
  # 4 листа:
  # 1. «Детальный отчёт» — все строки (до 50000), колонка «Продавец», ИТОГО
  # 2. «По категориям» — сводка + BarChart
  # 3. «По продавцам» — если len(row) >= 11 (get_sales_report, 11 cols)
  # 4. «По дням» — если >1 дата, с BarChart
```

### tenant_manager.py — мультиарендность

```
get_user_db_path(telegram_id)       ← путь к БД пользователя (main.db → user_org_mapping)
create_organization(name, owner_id) ← создаёт org + db-файл + запись в main.db
join_organization_by_invite(telegram_id, invite_code)
generate_invite_code(org_id)        ← создаёт/перезаписывает invite_code
rotate_invite_code(org_id)          ← Б) генерирует новый код, сохраняет в БД, возвращает str
set_invite_preset(org_id, role, shop)← Д) записывает invite_preset_role + invite_preset_shop
get_invite_preset_by_code(code)     ← Д) → dict {preset_role, preset_shop} по инвайт-коду
get_invite_preset_by_org(org_id)    ← Д) → dict {preset_role, preset_shop} по org_id
get_org_admin_telegram_ids(org_id)  ← В) → list[int] telegram_id всех owner+admin орги
get_user_org_id(telegram_id)        ← → int|None текущий org_id пользователя
delete_organization(org_id)
get_all_organizations()             ← для супер-адмна
change_user_role(telegram_id, org_id, role, scope_type, scope_value)
                                    ← scope_value: list[str] → хранится JSON в DB
set_user_title(telegram_id, org_id, custom_title)  ← None → сброс
```

### keyboards.py — клавиатуры

```
main_menu(chat_id, user_shop)       ← главное меню (адаптируется под роль)
system_admin_menu()                 ← меню супер-адмна
admin_management_menu(chat_id)      ← меню управления
products_menu(), inventory_menu()
back_button(callback_data)
generate_calendar(year, month, prefix)
safe_cb(prefix, value)              ← безопасный callback_data ≤64 байта (SHA256 при превышении)
resolve_cb_name(raw, candidates)    ← обратный поиск по safe_cb хэшу
```

### payment_provider.py — фабрика провайдеров оплаты

```python
get_active_provider(db)             → str   — 'sbp' или 'yookassa'
provider_label(provider)            → str   — «СБП» / «ЮKassa»
create_yookassa_payment(db, user_id, amount, plan_type, promocode_id=None, return_url=None)
                                    → dict | None  — {'payment_id', 'confirmation_url'} или None
check_yookassa_payment_status(db, payment_id) → str | None  — 'pending'/'succeeded'/'canceled'/None
# lazy import yookassa — не ломает бота при отсутствии библиотеки
```

### filter_utils.py + filter_handlers.py — общий фильтр

```
ADMIN_FILTER_KEY = "admin_filter"  — ключ в FSM data

empty_filter()                     → dict {shops:[], cities:[], networks:[]}
get_available_filter_values(db, scope_type, scope_values) → dict
  # Магазины берутся из ТРЁХ источников: users.shop_name + shops.name + inventory.shop_name
  # Каждый источник в отдельном try/except — магазины без сотрудников (ТЦ Бум) тоже видны
  # TTL-кеш 60 сек по db_path; invalidate_filter_values_cache(db_path) при изменении магазинов
merge_scope_with_filter(scope_type, scope_values, active_filter) → dict  — scope=потолок, filter=пол
build_filter_keyboard(available, active, back_cb) → InlineKeyboardMarkup
filter_button_text(active_filter)  → str  — «🔍 Фильтр» / «🔍 Фильтр ✅»
is_filter_active(active_filter)    → bool

Callbacks (filter_router):
  flt_open_{back_cb}  — открыть панель (back_cb = callback «✅ Применить»)
  ftog_s_{val}        — переключить магазин (через safe_cb/resolve_cb_name)
  ftog_c_{val}        — переключить город
  ftog_n_{val}        — переключить торговую сеть
  flt_reset           — сбросить
```

### hints.py — onboarding и подсказки

```
HINT_TEXTS: dict — ключи: 'sales','reports','products','dashboard','plans','contests','rankings'

hint_suffix(db, user_id, hint_key) → str
  — при первом визите добавляет текст подсказки к сообщению
  — кнопка «✅ Понятно!» → hint_dismiss:{hint_key} → удаляет попап

maybe_send_welcome(message_or_callback, db, user_id, is_admin)
  — отправляет popup при первом входе (роль-зависимый текст)
  — однократно: хранится в user_hints_seen ('welcome' key)
```

### notif_utils.py — уведомления

```
add_read_btn(existing_markup=None) → InlineKeyboardMarkup
  — добавляет кнопку «✅ Прочитано» (callback_data="notif_read") ко ВСЕМ push-уведомлениям
  — notif_read handler в notifications_handlers.py → удаляет сообщение из чата
```

### pagination_utils.py — пагинация

```
PAGE_SIZE_DEFAULT = 8   (продукты, конкурсы)
PAGE_SIZE_USERS   = 10  (пользователи)
PAGE_SIZE_ORGS    = 8   (организации)
PAGE_SIZE_SALES   = 8   (продажи для редактирования)

paginate(items, page=0, per_page=PAGE_SIZE_DEFAULT) → (page_items, total_pages)
page_nav_row(page, total_pages, prefix) → list[InlineKeyboardButton]
  — возвращает [«‹»] [«›»] кнопки для навигации
```

### states.py — FSM состояния

| StatesGroup | Файл | Назначение |
|---|---|---|
| `UserRegistrationStates` | handlers.py | Регистрация нового пользователя |
| `UserProfileStates` | handlers.py | Редактирование профиля |
| `AdminUserStates` | admin_handlers.py | Редактирование пользователя; `waiting_for_admin_title` |
| `SaleStates` | sales_handlers.py | Флоу продажи (категория→товар→кол-во→цена→подтверждение) |
| `QuickSaleStates` | sales_handlers.py | Быстрый поиск: `searching_product` |
| `ProductStates` | products_handlers.py | Создание/редактирование товара |
| `InventoryStates` | inventory_handlers.py | Корректировка остатков |
| `NotificationStates` | notifications_handlers.py | Создание рассылки, ввод даты/времени |
| `SalaryStates` | salary_handlers.py | Ввод ставки сотрудника |
| `SalesPlanStates` | sales_plans_handlers.py | Мастер создания плана |
| `ContestStates` | contests_handlers.py | Мастер создания конкурса |
| `BackupStates` | backup_handlers.py | Восстановление из бэкапа |
| `PaymentSystemStates` | payment_system_admin.py | Настройка ЮKassa (shop_id, secret_key, return_url) |
| `AddonStates` | addon_handlers.py | `entering_qty` — ввод кол-ва надстроек |
| `AbsenceStates` | absence_handlers.py | `new_start_date`, `new_end_date`, `new_comment`, `reject_comment` — флоу заявки/отклонения |

---

## 4. HANDLERS — КЛЮЧЕВЫЕ CALLBACK-СХЕМЫ

### handlers.py (router)

```
start / registration flow
profile_menu            "profile_menu"
edit_profile_*          "edit_profile_*"
prof_shop_pick_*        safe_cb("prof_shop_pick_", shop)
prof_net_pick_*         safe_cb("prof_net_pick_", network)
usage_mode_*            "usage_mode_personal" / "usage_mode_corporate"
join_org_*              "join_org"
hint_dismiss:{key}      удаление onboarding-попапа
```

### admin_handlers.py (admin_router)

```
admin_users_menu        "admin_users"
admin_user_details      "admin_user_{id}"
change_user_role        "admin_edit_role_{id}"
adm_scope_toggle        "adm_t_s_{val}" / "adm_t_c_{val}" / "adm_t_n_{val}"  — мульти-выбор
adm_scope_submit        "adm_scope_submit"
adm_title_start         "adm_title_start"  → AdminUserStates.waiting_for_admin_title
adm_shop_pick_*         safe_cb("adm_shop_pick_", shop)
adm_net_pick_*          safe_cb("adm_net_pick_", network)
change_org_context      "change_org_context"  — переключение орг для супер-адмна
select_org_*            "select_org_{id}"
generate_invite         "generate_invite"
reset_invite            "reset_invite_{org_id}"           ← Б) ротация кода
invite_preset_start     "invite_preset_start_{org_id}"    ← Д) выбор роли пресета
ipr_role_X              "ipr_role_{role}_{org_id}"        ← Д) user/admin/none
ipr_shop_TOKEN          "ipr_shop_{token}_{org_id}"       ← Д) выбор магазина / NONE
name_tg_use             "name_tg_use"                     ← Г) использовать имя из TG
name_tg_manual          "name_tg_manual"                  ← Г) ввести имя вручную
```

### sales_handlers.py (sales_router)

```
start_sale              "start_sale"
_show_sale_categories   — категории + «🔍 Найти товар» (quick_search)
sale_cat_{cat}          — выбрать категорию
sale_product_{id}       — выбрать товар → _make_qty_keyboard(max_qty)
sq_qty_{n}              — выбрать кол-во кнопкой (SaleStates.entering_quantity)
sq_qty_manual           — ввести вручную
sale_quick_search       — QuickSaleStates.searching_product
quick_confirm_sale      — ⚡ «Продать сейчас» (первый товар в корзине)
edit_sales_list         — esl_pg_{page} — пагинация
edit_sale_{id}          — редактировать конкретную продажу
delete_sale_{id}        — удалить продажу
cross_shop_switch       — «🔄 Сменить магазин» (торговые сети)
```

### reports_handlers.py (reports_router)

```
reports_menu            "reports_menu"
report_today            "report_today"                — все транзакции (admin) / все товары (user)
report_full             "report_full"                  — все категории и магазины (guard > 3500/3700)
report_my_shop          "report_my_shop"               — все товары в магазинах пользователя (guard > 3700)
period_report_*         — выбор периода через календарь → generate_period_report (guard > 3700)
report_my_month         "report_my_month"              — сотрудник: с 1-го по сегодня

ranking_sellers         "ranking_sellers"
ranking_shops           "ranking_shops"
ranking_cities          "ranking_cities"
rank_sel/shp/cty_month  — период: этот месяц (default)
rank_sel/shp/cty_7d     — 7 дней
rank_sel/shp/cty_prev   — прошлый месяц
rkcal_{y}_{m}_{d}       — «📆 Свой период» через rk-календарь

download_excel_full     "download_excel_full"          — полный; scope-фильтр, лимит 50000
download_excel_period   "download_excel_period"        — из FSM (excel_start/excel_end/excel_shop)
download_excel_user     "download_excel_user"          — отчёт пользователя
download_excel_shop     "download_excel_shop"          — по магазину (из FSM shop_name)
download_excel_city     "download_excel_city"          — по городу (один SQL-запрос)
download_excel_my       "download_excel_my"            — «Мои продажи» (текущий месяц)

download_pdf_full       "download_pdf_full"            — PDF полного отчёта (pdf_utils.py)
download_pdf_period     "download_pdf_period"          — PDF за период (из FSM)
download_pdf_shop       "download_pdf_shop"            — PDF по магазину
download_pdf_user       "download_pdf_user"            — PDF отчёт пользователя

_translit_filename(text) → str                         — транслитерация имён файлов .xlsx
```

**Принцип guard'ов в сообщениях:**
- `> 3600` уровень магазинов → `<i>··· ещё магазины скрыты — скачайте Excel</i>`
- `> 3700` уровень товаров → `<i>··· остальные товары в Excel</i>`
- `> 3800` сплошная обрезка + `<i>··· список обрезан</i>`

### dashboard_handlers.py (dashboard_router)

```
build_admin_dashboard(db, today, now_str, user_id, telegram_id) → str
  now_str = get_current_user_time(user_tz).strftime('%d.%m.%Y · %H:%M')
  Показывает: зарплата, продажи+мотивация сегодня, все планы, конкурсы, список смены

build_user_dashboard(db, user_id, telegram_id, today, now_str) → str
  Показывает: зарплата, мотивация, продажи сегодня, планы, призы конкурсов

_on_shift_details(db_file, today) → list[(first_name, last_name, shop_name)]
_today_total_earnings(db_file, today) → float
_plan_summary_line(plan, actual, percent) → str  — единый формат для всех планов
```

### salary_handlers.py (salary_router)

```
slr_rates               — ставки сотрудников
slr_set_{uid}           — редактировать ставку
slr_scheds              — список графиков
slr_cal_{uid}_{y}_{m}   — календарь смен (admin)
slr_tog_{uid}_{date}    — переключить день (добавить с шаблоном / снять)
slr_day_{uid}_{date}    — под-экран конкретного дня
slr_rm_{uid}_{date}     — снять смену
slr_ets_{uid}_{date}    — пикер часов начала
slr_etw_{uid}_{date}    — пикер часов конца
slr_te_{h}_{uid}_{date} — выбрать час начала → сразу slr_etw_
slr_tw_{h}_{uid}_{date} — выбрать час конца → сохранить + slr_day_
slr_tmpl_{uid}_{y}_{m}  — ⏰ Расписание смен (шаблон 7 дней)
tmpl_day_{uid}_{wd}     — день шаблона (0=Пн..6=Вс)
tmpl_off_{uid}_{wd}     — выходной
tmpl_te/tmpl_tw_{uid}_{wd}          — пикеры начала/конца шаблона
tmpl_te_h/tmpl_tw_h_{h}_{uid}_{wd} — выбор часа
my_schedule             — своё расписание (user, editable=False)
my_d_{date}             — детали дня (время из work_schedule или шаблон-fallback)
slr_sum_{y}_{m}         — зарплатный итог за месяц
```

### sales_plans_handlers.py (sales_plans_router)

```
Создание планов: тип → период → метрика → фильтр → целевое значение
epwho_*/epwhousr_*/epwhoshp_*/epwhotgt_  — редактирование получателя
epperiod_*    — редактирование периода
epmetric_*    — редактирование метрики
epfilter_*    — редактирование фильтра (all/category/product)
plnflt_cat_*  — мультивыбор категорий

_plan_summary_line(plan, actual, percent) → str  — канонический формат
```

### notifications_handlers.py (notifications_router)

```
notifications_menu          — меню
notification_settings_menu  — настройки
admin_send_notification     — создание рассылки (asyncio.sleep(0.05) = 20 msg/sec)
view_scheduled_notifications — список (время в TZ admin через format_user_datetime)
del_sched_notif_{id}        — удалить запланированное

# UTC fix: ввод time → get_utc_time(naive, admin_tz) → scheduled_datetime (UTC)
# check_scheduled_notifications: now = datetime.now().isoformat() (UTC) vs scheduled_datetime (UTC)
# ⚠️ Techdebt: 3 строки "BROADCAST DEBUG" logging.info() — minor, стоит убрать
```

### subscription_handlers.py (subscription_router)

```
pay_{plan}              — начало оплаты; маршрутизация по провайдеру
proceed_to_payment      — СБП: экран с реквизитами; ЮKassa: создать платёж + URL
check_yookassa_payment  "yk_check_{payment_id}" — проверить статус → auto-confirm при 'succeeded'
upload_payment_proof    — загрузка скриншота (СБП)
subscription_menu       — меню подписки (содержит кнопки «🔗 Реферальная» + «➕ Надстройки»)
```

### referral_handlers.py (referral_router)

```
subscription_referral   — экран реф. программы: ссылка + статистика (total/applied/bonus_days)
                          Deep-link разбирается в handlers.py:
                          /start ref_TELEGRAMID → select_city (callback) / process_city (message)
                          → create_referral(referrer_id, referred_id) сразу;
                          → apply_referral_bonus(referrer_id) при create_organization()
```

### addon_handlers.py (addon_router)

```
subscription_addons         — меню надстроек; get_addon_totals() → суммарные активные
addon_buy_shops_1           — экран покупки +1 магазин (150₽/30д)
addon_buy_products_1        — экран покупки +100 товаров (100₽/30д)
addon_confirm_{type}        — подтверждение; redirect → confirm_payment_request с plan_type 'addon_shops_1'/'addon_products_1'
```

### absence_handlers.py (absence_router)

```
abs_my              — экран «Мои отсутствия»: сводка по типам + кнопка «➕ Подать заявку»
abs_hist_{year}     — история за год (список записей)
abs_new             — выбор типа заявки
abs_nt_{type}       — выбран тип → FSM: AbsenceStates.new_start_date
  → AbsenceStates.new_end_date → AbsenceStates.new_comment → создание absence_record
abs_admin           — экран admin: сводка pending + кнопки «🕐 На рассмотрении» / «📋 Все»
abs_pnd             — список pending заявок
abs_rv_{id}         — карточка заявки (admin): Одобрить / Отклонить
abs_ok_{id}         — одобрить (status → approved)
abs_rj_{id}         — начало отклонения → FSM: AbsenceStates.reject_comment
  → abs_reject_do (message) → status → rejected
abs_del_{id}        — удалить запись (только pending/создатель)

Типы: vacation (Отпуск), sick (Больничный), compensatory (Отгул),
      absence (Прогул), other (Другое)
Статусы: pending → approved / rejected / cancelled
```

### pdf_utils.py (без роутера)

```python
generate_pdf_report(
    org_name, start_date, end_date,
    sales_rows,       # [(date, product, shop, qty, price, total, seller), ...]
    summary,          # {'total_sales': N, 'total_revenue': F, 'avg_sale': F}
    top_sellers,      # [(name, total_revenue, sales_count), ...]
    top_products,     # [(name, qty_sold, total_revenue), ...]
    output_path=None  # None → tempfile
) → str | None

generate_pdf_for_report(db, scope, start_date, end_date, scope_value=None) → str | None
  — фасад: делает SQL-запросы, собирает summary/tops, вызывает generate_pdf_report
  — scope: 'full' / 'period' / 'shop' / 'user'
  — требует reportlab; при ImportError → None без краша
```

---

## 5. ПЛАНИРОВЩИК (main.py + scheduler_module.py)

```
APScheduler (AsyncIOScheduler)
  misfire_grace_time=60  — до 60с опоздания — всё равно запустить
  coalesce=True          — пропущенные повторы схлопываются в один
  max_instances=1        — никакого параллельного запуска одного задания

Задачи (10 штук):
  send_sales_alerts()               cron(minute='*', second=0)   — дневные цели продаж
  send_payment_alerts()             cron(minute='*', second=12)  — напоминания подписки (14/7/3/1 день) + trial reminders
  send_daily_reports()              cron(minute='*', second=24)  — ежедневные отчёты (async)
  send_personalized_notifications() cron(minute='*', second=36)  — персонализированные
  check_scheduled_notifications()   cron(minute='*', second=48)  — запланированные рассылки (UTC)
  send_trial_expired_upsell()       cron(hour='*', minute=5)     — upsell при истечении триала; dedup threshold=-1
  auto_finish_contests()            cron(hour='*', minute=0)     — завершение конкурсов
  auto_reject_stale_payments()      cron(hour=10, minute=15)     — отклонение pending СБП >72ч
  backup_job()                      cron(hour=3, minute=0)       — авто-бэкап (retention 30 дней)
  cleanup_fsm_storage()             cron(day_of_week='sun', hour=4, minute=30) — удаление FSM-записей старше 30 дней

_get_scheduler_db_paths()  → list[str]  — TTL-кеш 5 мин, все tenant БД + shop_bot.db
```

**Timezone:** все задачи сравнивают `datetime.now()` (UTC на Amvera) с настроенным временем через `.astimezone(user_tz)`.
**Async в APScheduler:** `send_daily_reports` — async функция, запускается через `asyncio.run_coroutine_threadsafe`. Остальные задачи — sync, используют `get_db_sync()`.

---

## 6. РОЛИ И ПРАВА ДОСТУПА

| Роль | Определяется | Права |
|---|---|---|
| **super_admin** | ID == 921098636 или первый в ADMIN_CHAT_ID | Всё, переключение контекста орг |
| **owner** | role='owner' в user_org_mapping | Директор; управление всей орг, назначение ролей |
| **admin** | role='admin' + scope в user_org_mapping | Зам/Магазин/Город/Сеть; отчёты по своей зоне |
| **user (org)** | role='user' в user_org_mapping | Продажи, склад своего магазина, заработок |
| **user (personal)** | в shop_bot.db, не в маппинге | Только свои данные |

**Зоны доступа (scope):** `scope_type` = 'shop'/'city'/'network'/None; `scope_value` = JSON array `["Магазин А","Магазин Б"]`. Пустой список = полный доступ.

**Кастомные должности:** `custom_title` в `user_org_mapping`; приоритет над стандартным ярлыком роли.

**is_any_admin():** owner/admin в org → True; user → False (даже если env_manager.is_admin() = True).

---

## 7. ПАТТЕРН ДОСТУПА К БД

```python
# ПРАВИЛЬНО — все обычные handlers (AsyncDatabase):
current_db = await get_db(callback.from_user.id, state)
data = await current_db.get_something()               # ← await обязателен!

# ПРАВИЛЬНО — параллельные запросы (3+ независимых):
r1, r2, r3 = await asyncio.gather(
    current_db.get_sales_summary(...),
    current_db.get_plans_progress(),
    current_db.get_contests(status='active'),
    return_exceptions=True,
)

# ПРАВИЛЬНО — payment/subscription (всегда централизованно):
db = wrap_db(Database('data/shop_bot.db'))            # wrap_db() обязателен!

# ПРАВИЛЬНО — APScheduler sync контекст:
db = get_db_sync(telegram_id)                         # Database (не AsyncDatabase)
data = db.get_something()                             # без await

# НЕПРАВИЛЬНО — устаревший паттерн:
db = Database('data/shop_bot.db')                     # без wrap_db() — методы не async!
data = current_db.get_something()                     # без await — получишь coroutine!
```

**SQLite concurrency и производительность:**
- `get_connection()` → `_PooledConn` — thread-local пул (одно соединение на поток)
- `PRAGMA journal_mode=WAL` — параллельные читатели без блокировок
- `PRAGMA synchronous=NORMAL` — баланс надёжность/скорость
- `PRAGMA cache_size=-8000` — 8 MB page cache
- `PRAGMA temp_store=MEMORY` + `mmap_size=134217728` — in-memory tmp + 128 MB mmap
- `busy_timeout=10000` — 10 с ожидания при блокировке

---

## 8. КЛЮЧЕВЫЕ ПРАВИЛА (GOTCHAS)

1. `clear_state_keep_org(state)` **ПОСЛЕ** `fsm_edit(...)`, никогда до
2. `state.clear()` **запрещён** — только `clear_state_keep_org(state)`
3. `he()` для ВСЕХ пользовательских строк в HTML-сообщениях (в button.text НЕ нужен)
4. `safe_cb()` / `resolve_cb_name()` для callback_data с user-строками (лимит 64 байта)
5. `get_users_for_notifications()` → `[0]` = users.id, `[1]` = telegram_id — не путать
6. `Database.db_file` (не `.db_path`) — атрибут пути к файлу БД
7. Новый роутер → зарегистрировать в `main.py`; новый модуль → добавить в `test_imports.py`
8. `get_sales_ranking()` возвращает 8 колонок; 8-я = `u.id`; `row[:7]` для старого unpacking
9. `get_user_org_scope()` → `(scope_type, list[str])` НЕ `(str, str)`
10. `clear_state_keep_org(state, extra_keys=[...])` — для сохранения доп. ключей FSM
11. `data/` исключены из обоих репо — runtime БД только на Amvera persistenceMount
12. `datetime.now()` на Amvera = UTC → для отображения всегда `timezone_utils`
13. `shift_templates` weekday: 0=Пн, 6=Вс (стандарт Python `date.weekday()`)
14. `scheduled_notifications.scheduled_datetime` хранится в UTC — `get_utc_time()` при записи
15. Excel: `generate_excel_report(sales, ...)` — seller detection: `len(row) >= 11`; лимит 50000 строк
16. Telegram лимит сообщения 4096 символов: guard'ы на уровне магазинов (`>3600`) и товаров (`>3700`)
17. **`get_db()` возвращает `AsyncDatabase`** — все вызовы `await`. Без await → coroutine, не данные
18. **`wrap_db(Database(...))`** обязателен для inline Database() в обычных handlers
19. **APScheduler** → `get_db_sync()` (не `get_db()`), методы без await
20. **Новый метод в `Database`** → использовать `self.get_connection()`, НЕ `sqlite3.connect(self.db_file)`
21. **`asyncio.gather()` + `return_exceptions=True`** → всегда проверять `isinstance(r, Exception)`
22. **`sales_handlers.py`** не имеет глобального `logger` — только `import logging` + `logging.error()`
23. **`confirm_payment_request`**: plan_type начинающийся с `'addon_'` → `create_subscription_addon()` (не `create_subscription()`). Формат: `addon_shops_1` / `addon_products_1`
24. **Реф. deep-link**: аргумент `/start ref_TELEGRAMID` → после `.upper()` сохраняется как `REF_{ID}` в FSM. Бонус применяется в **обоих** путях создания орги: `select_city` (callback) и `process_city` (message-handler) в `handlers.py`
25. **`products` колонки `photo_file_id` + `description`** — добавлены ALTER TABLE миграцией в `create_tables()`. При SELECT всех полей — индексы: photo_file_id=6, description=7. Старый код с `row[0:6]` unpacking не сломается, но новые данные не получит
26. **`pdf_utils.py`** требует `reportlab`. При `ImportError` возвращает `None` без краша — обработать в handler и уведомить пользователя
27. **`get_absence_days_map(year, month, user_id=None)`** → `{user_id: {day_num: {type, status, id}}}` — возвращает вложенный dict с ВНЕШНИМ ключом user_id. Вызывающий код должен делать `.get(user_id, {})` для извлечения дня-карты. Без этого распаковка даст пустой dict или KeyError.
28. **Callback handlers (не message-handlers) НЕ используют `fsm_edit(callback, text, markup)`** — это функция только для message-handlers (принимает `message`). В callback-хендлерах: `await callback.answer()` + `await callback.message.edit_text(text, markup=markup, parse_mode=...)`.

---

## 9. МОНЕТИЗАЦИЯ

```
Тарифы (subscription_plans):
  Бесплатный  — 0₽, 50 товаров / 1 магазин / 100 продаж; без функций
  Базовый     — 500₽/30д, 200/3/500; экспорт+аналитика+уведомления; БЕЗ интеграций (can_use_integrations=0)
  Стандарт    — 1200₽/90д, 500/10/1500; + Google Sheets
  Премиум     — 4000₽/365д; всё безлимит

Пробный период: trial_days=14, trial_plan='Премиум' в payment_settings
  → _has_active_trial(tg_id) → True → get_plan_limits() сразу возвращает _UNLIMITED (минуя lookup)
  → dedup upsell: subscription_reminder_log threshold=-1 (для expired upsell)

Провайдеры оплаты (payment_provider в payment_settings):
  'sbp'      — ручное подтверждение скриншота чека; pending >72ч → auto_reject_stale_payments
  'yookassa' — автоматическая оплата через API ЮKassa (shop_id + secret_key + return_url)

Надстройки (subscription_addons в shop_bot.db):
  extra_shops    — 150₽/30д, +1 к лимиту магазинов; plan_type = 'addon_shops_1'
  extra_products — 100₽/30д, +100 к лимиту товаров; plan_type = 'addon_products_1'
  Несколько надстроек суммируются. get_addon_totals(user_id) → {'extra_shops': N, 'extra_products': N}
  get_plan_limits() в subscription_utils.py суммирует addon-значения с лимитами тарифа.
  confirm_payment_request: plan_type.startswith('addon_') → create_subscription_addon() (не create_subscription)

Реферальная программа (referrals в shop_bot.db):
  Deep-link /start ref_TELEGRAMID → бонус +30 дней к подписке реферера за каждого,
  кто создал организацию. apply_referral_bonus(referrer_id) → _extend_subscription_by_days(tg_id, 30).

Промокоды: discount_percent, max_usage, current_usage
Лимиты по тарифу: max_products, max_shops (-1 = безлимит)

ВАЖНО: can_use_integrations=0 для Базового — always-running migration в database.py принудительно
       держит это значение. Не менять без проверки миграции (~строка 861 в database.py).
```

---

## 10. ДЕПЛОЙ

```bash
# GitHub + Amvera (по умолчанию):
bash deploy.sh "commit message"

# Только GitHub:
bash deploy.sh "commit message" --no-amvera

# Верификация после деплоя:
# deploy.sh автоматически: git ls-remote amvera refs/heads/master
# Выводит: "Amvera verify: ✅ remote hash совпадает (hash)"
```

**Amvera config (`amvera.yml`):**
```yaml
meta:
  environment: python
  toolchain: {name: pip, version: "3.11"}
run:
  persistenceMount: /app/data
  command: python main.py
```

**Исключения из деплоя на Amvera:** `AGENT_HANDOFF.md`, `replit.md`, `PROJECT_MAP.md`, `README.md`, `*.db`, `*.pkl`, `data/tenants/`, `data/backup/`.

---

## 11. ВЕБ-ИНТЕРФЕЙС (`web/`)

### Стек
FastAPI + Uvicorn (порт 5000) · Jinja2 · Tailwind CSS CDN · HTMX · Alpine.js · Chart.js

Запускается в `main.py` через `threading.Thread`; аутентификация — Telegram Login Widget → JWT cookie `web_session` (24ч).

### Файловая структура
```
web/
  app.py              — create_web_app(); регистрация роутеров, Jinja2 globals/filters
  auth.py             — get_session_user(), get_csrf_token(), verify_csrf_token(),
                         generate_login_nonce(), verify_login_nonce() (HMAC nonce для /auth/code);
                         hash_password() / verify_password() (PBKDF2-SHA256, 390k итераций, stdlib)
  email_utils.py      — send_verification_email(), send_reset_email(), send_link_notification();
                         smtp.yandex.ru:465 SSL; secrets YANDEX_EMAIL + YANDEX_SMTP_PASSWORD;
                         is_configured() — проверять перед вызовом (503 если SMTP не настроен)
  rate_store.py       — persistent SQLite rate limiter; check_rate_limit(key, limit, window_sec)
                         → bool; хранит в data/rate_limits.db; выдерживает рестарты
  deps.py             — get_web_db(telegram_id, org_db) → Database(path)
  routes/
    auth_routes.py    — GET/POST /login, GET /logout
    email_auth.py     — GET/POST /register, GET/POST /auth/email, GET /auth/verify,
                         POST /auth/resend-verify, GET/POST /auth/reset,
                         GET/POST /auth/reset/confirm,
                         POST /settings/email-change, /settings/password-change,
                         /settings/email-unlink; rate limit 5 req/10min/IP
    dashboard.py      — GET /dashboard
    sales.py          — GET /sales, GET /sales/export.xlsx,
                         POST /sales/create (CSRF), POST /sales/{id}/delete (CSRF),
                         GET /api/products-for-shop?shop=…
    products.py       — GET /products, GET/POST /products/new, POST /products/create,
                         GET/POST /products/{id}/edit, POST /products/{id}/update,
                         POST /products/{id}/delete, GET/POST /products/import,
                         POST /products/import/confirm, GET /products/{id}
    inventory.py      — GET /inventory, GET /inventory/export.xlsx,
                         POST /inventory/adjust (JSON, CSRF)
    reports.py        — GET /reports
    rankings.py       — GET /rankings
    staff.py          — GET /staff, GET /staff/{id},
                         GET /staff/invite-code (JSON),
                         POST /staff/invite-code/rotate (CSRF),
                         POST /staff/{id}/set-role (JSON, CSRF),
                         POST /staff/{id}/remove (JSON, CSRF)
    plans.py          — GET /plans, GET /plans/new, POST /plans/create, GET /plans/{id},
                         GET /plans/{id}/edit, POST /plans/{id}/update, POST /plans/{id}/delete, POST /plans/{id}/toggle
    salary.py         — GET /salary, GET /salary/export.xlsx,
                         POST /salary/adjustment/add (CSRF),
                         POST /salary/adjustment/{id}/delete (CSRF)
    schedule.py       — GET /schedule, POST /schedule/toggle_day, /set_time, /remove_day, /fill_month, /set_template
    contests.py       — GET /contests, GET /contests/new, POST /contests/create,
                         GET /contests/{id}/edit, POST /contests/{id}/update, POST /contests/{id}/finish|cancel
    settings.py       — GET/POST /settings, POST /settings/rotate_invite, /settings/save_invite_preset
    integration.py    — GET /integration, POST /integration/create, /auth/start, /auth/poll,
                         POST /integration/{id}/toggle, /integration/{id}/delete
    payments.py       — GET /payments, POST /payments/{id}/confirm, /payments/{id}/reject
    absences.py       — GET /absences, POST /absences/add, /absences/update,
                         GET /absences/settings, POST /absences/settings/update
    chat.py           — GET /chat, POST /chat/send, GET /chat/poll,
                         GET /chat/topics/{id}/messages, GET /chat/file/{id},
                         GET /chat/file/attachment/{id},
                         POST /chat/message/{id}/delete, POST /chat/topics/create,
                         POST /chat/topics/{id}/rename, POST /chat/topics/{id}/archive,
                         GET /chat/search?q=&topic_id=  ← поиск (topic_id=0 = темы+DM)
                         ── DM (Direct Messages) ──
                         GET /chat/dm, GET /chat/dm/{peer_id},
                         GET /chat/dm/file/{msg_id}, GET /chat/dm/file/attachment/{att_id},
                         POST /chat/dm/send, POST /chat/dm/{msg_id}/delete,
                         GET /api/dm/contacts, GET /api/dm/members,
                         GET /api/dm/conversation/{peer_id},
                         WS  /ws/chat/dm  ← WebSocket (read-receipts + real-time)
    push_utils.py     — send_web_push(subscription, payload): pywebpush 2.3.0 + VAPID;
                         env: VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_MAILTO
  sale_events.py    — async post_sale_effects(org_db_path, sale_id, shop_name, telegram_id)
                       вызывается через asyncio.run_coroutine_threadsafe из sales_create;
                       3 эффекта: GSheets trigger · shift-sale push · plan milestones
  templates/
    base.html         — сайдбар, nav (включает Платежи только для super_admin + pending badge)
    auth/             — login.html (Telegram+email табы), register.html, reset_request.html,
                        reset_confirm.html, verify_sent.html
    dashboard/, sales/, products/, inventory/, reports/, rankings/,
    staff/, plans/, salary/, schedule/, contests/, settings/, integration/, payments/
    errors/403.html, errors/404.html
  static/             — (пустой, авто-создаётся)
```

### Ключевые правила (web-специфичные)
1. **DB в веб — sync**: `get_web_db()` → `Database(path)` без await; НЕ `AsyncDatabase`
2. **TemplateResponse**: первый аргумент — `request` → `TemplateResponse(request, "tmpl.html", ctx)`
3. **POST redirect**: status_code=**303** (не 302)
4. **CSRF**: в роуте `ctx["csrf_token"] = get_csrf_token(request)`; в шаблоне `{{ csrf_token }}`; проверка `verify_csrf_token(request, form.get("csrf_token"))`
5. **Flash**: только query-параметры; нет server-side session storage
6. **Платежи**: всегда `Database("data/shop_bot.db")` напрямую (не `get_web_db`)
7. **super_admin в web**: `user.get("role") == "super_admin"` (устанавливается через `env_manager.is_super_admin()` при логине)
8. **Новый роутер**: добавить import + `app.include_router(...)` в `web/app.py`
9. **Jinja2 globals**: `bot_username()`, `pending_payments_count()` — зарегистрированы в `app.py`
10. **In-memory state**: `_import_sessions` (products.py) и `_device_flow` (integration.py) — теряются при рестарте
11. **Persistent rate limiting**: `check_rate_limit(key, limit, window_sec)` из `web/rate_store.py` — хранит состояние в `data/rate_limits.db`; используется в `auth_routes.py` и `email_auth.py`
12. **HMAC nonce для `/auth/code`**: `generate_login_nonce()` / `verify_login_nonce()` в `web/auth.py` — stateless, хранения в БД не требует
13. **Кликабельные уведомления**: `_notif_url(notification_type)` в `api.py` и `notifications.py` → URL-роутинг по типу; `base.html` mobile/desktop items навигируют по клику
14. **Email auth — synthetic_tg_id**: email-only пользователи получают `synthetic_tg_id = -(10_000_000 + cred_id)`; хранится в `web_credentials.synthetic_tg_id`; используется как `tg_id` в JWT; `org_db` для email-only берётся из `web_credentials.org_db`, НЕ из `user_org_mapping`
15. **Email auth — SMTP**: `web/email_utils.is_configured()` проверять перед каждым SMTP-вызовом; `YANDEX_SMTP_PASSWORD` = пароль приложения (16 символов), НЕ пароль аккаунта Яндекс

### Доступ по ролям
| Роль | Что видит / может делать |
|------|-----------|
| `super_admin` | Всё включая `/payments` с бейджем |
| `owner` | Всё кроме `/payments`; управляет staff (set-role, remove, rotate invite) |
| `admin` | Всё кроме `/payments` в рамках своего scope; staff: только user-цели |
| `user` | `/dashboard`, `/sales` (запись+удаление), `/products`, `/inventory` (adjust), `/rankings` |

### Веб write-actions (добавлены сессия 292)
| Маршрут | Метод | Описание | Кто |
|---------|-------|----------|-----|
| `POST /sales/create` | CSRF form | Записать продажу | user+ |
| `POST /sales/{id}/delete` | CSRF form | Удалить продажу (только своя) | user+ |
| `GET /api/products-for-shop` | JSON | Товары по scope пользователя | user+ |
| `POST /inventory/adjust` | JSON+CSRF | Изменить остаток ±N или абсолютно | user+ |
| `GET /staff/invite-code` | JSON | Текущий инвайт-код | owner/admin |
| `POST /staff/invite-code/rotate` | CSRF form | Ротация инвайт-кода | owner/admin |
| `POST /staff/{id}/set-role` | JSON+CSRF | Изменить роль сотрудника | owner+ |
| `POST /staff/{id}/remove` | JSON+CSRF | Удалить сотрудника (с иерархией) | owner+ |
| `POST /salary/adjustment/add` | CSRF form | Добавить корректировку/бонус | owner/admin |
| `POST /salary/adjustment/{id}/delete` | CSRF form | Удалить корректировку | owner/admin |

| `GET /chat/search` | JSON | Поиск сообщений по теме или глобально (дебаунс 300 мс) | user+ |
| `POST /inventory/adjust` | JSON+CSRF | Изменить остаток; возвращает `gs_status` если GSheets настроен | user+ |

**Вспомогательная функция**: `_get_user_allowed_shops(telegram_id, db)` в `web/routes/sales.py` — определяет scope пользователя через `get_user_org_scope()`: None=все, 'shop'=список, 'city'/'network'=запрос к БД.

**⚠️ Amvera: битые сборки**
- `deploy.sh` + `git ls-remote ✅` = код дошёл до репозитория. НЕ означает что сборка прошла.
- "Internal server error" в логах Amvera = инфраструктурная ошибка, не ошибка кода. Amvera продолжает крутить последний успешный образ.
- Диагностика: Amvera UI → Контроль версий → колонка "Используется".
- Форс-триггер: добавить комментарий в `requirements.txt` → `bash deploy.sh "chore: trigger rebuild"`.
- ❌ `quickBuild: false` в `amvera.yml` — НЕ существует, вызывает "Configuration error: unknown fields".
