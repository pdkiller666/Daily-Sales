# Карта проекта: Telegram Bot для управления розничными продажами

## 1. ОБЩАЯ АРХИТЕКТУРА

```
Telegram API
     │
   main.py  ──── запускает polling, регистрирует роутеры, инициализирует планировщик
     │
   ┌─┴──────────────────────────────────────────────────────────────┐
   │                    РОУТЕРЫ (handlers)                          │
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
   └────────────────────────────────────────────────────────────────┘
     │
   ┌─┴──────────────────────────────────────────────────────────────┐
   │                  СЛОЙ ДАННЫХ                                   │
   │  db_utils.py   ← get_db(id, state), is_any_admin()            │
   │  database.py   ← класс Database (156+ методов)                │
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
| `users` | id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, timezone |
| `products` | id, name, category, price, motivation_type, motivation_value |
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

### data/shop_bot.db ТОЛЬКО (платежи всегда централизованы)

| Таблица | Описание |
|---|---|
| `subscriptions` | id, user_id, plan_type, start_date, end_date, is_trial |
| `subscription_reminder_log` | user_id, threshold, subscription_end, sent_at |
| `payment_requests` | id, user_id, plan_type, amount, payment_proof_file_id, status, created_at |
| `payment_settings` | key, value — карта, реквизиты + trial_days, trial_plan |
| `subscription_plans` | id, name, price, duration_days, max_products, max_shops, ... |
| `promocodes` | id, code, discount_percent, max_usage, current_usage, is_active |

---

## 3. КЛЮЧЕВЫЕ МОДУЛИ

### db_utils.py — точка входа к БД

```
get_db(telegram_id, state)          ← ОСНОВНАЯ функция. Всегда использовать в async handlers.
                                       super-admin + selected_org_db → Database(org_db) + create_tables()
                                       user в org                    → Database(org_*.db) + create_tables()
                                       иначе                         → Database(shop_bot.db) + create_tables()
get_db_sync(telegram_id)            ← для синхронных контекстов (APScheduler)
is_any_admin(telegram_id)           ← проверяет ADMIN_CHAT_ID ИЛИ роль owner/admin в user_org_mapping
get_user_org_role(telegram_id)      ← 'owner'/'admin'/'user'/None из user_org_mapping
get_user_org_scope(telegram_id)     ← (scope_type, list[str]) — тип и список зон доступа
get_user_full_scope(telegram_id)    ← (scope_type, list[str], custom_title)
get_role_display_label(role, scope_type, scope_values, custom_title=None) ← текст роли
clear_state_keep_org(state, extra_keys=None) ← очистка state с сохранением selected_org_db
                                              extra_keys — доп. ключи FSM для сохранения
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
```

### tenant_manager.py — мультиарендность

```
get_user_db_path(telegram_id)       ← путь к БД пользователя (main.db → user_org_mapping)
create_organization(name, owner_id) ← создаёт org + db-файл + запись в main.db
join_organization_by_invite(telegram_id, invite_code)
generate_invite_code(org_id)
delete_organization(org_id)
get_all_organizations()             ← для супер-адмна
change_user_role(telegram_id, org_id, role, scope_type, scope_value)
                                    ← scope_value: list[str] → хранится JSON в DB
set_user_title(telegram_id, org_id, custom_title)  ← None → сброс
```

### keyboards.py — клавиатуры

```
main_menu(chat_id, user_shop)       ← главное меню (адаптируется под роль; включает «📝 Мои продажи» для продавцов)
system_admin_menu()                 ← меню супер-адмна
admin_management_menu(chat_id)      ← меню управления
products_menu(), inventory_menu()
back_button(callback_data)
generate_calendar(year, month, prefix)
safe_cb(prefix, value)              ← безопасный callback_data (SHA256 при > лимита)
resolve_cb_name(raw, candidates)    ← обратный поиск по safe_cb хэшу
```

### filter_utils.py + filter_handlers.py — общий фильтр

```
ADMIN_FILTER_KEY = "admin_filter"  — ключ в FSM data

empty_filter()                     → dict {shops:[], cities:[], networks:[]}
get_available_filter_values(db, scope_type, scope_values) → dict
merge_scope_with_filter(scope_type, scope_values, active_filter) → dict  — scope=потолок, filter=пол
build_filter_keyboard(available, active, back_cb) → InlineKeyboardMarkup
filter_button_text(active_filter)  → str  — текст кнопки «🔍 Фильтр» / «🔍 Фильтр ✅»
is_filter_active(active_filter)    → bool

Callbacks (filter_router):
  flt_open_{back_cb}  — открыть панель (back_cb = callback «✅ Применить»)
  ftog_s_{val}        — переключить магазин
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
report_today            "report_today"
report_full             "report_full" / "report_full_{page}"
period_report_*         выбор периода через календарь
report_my_month         "report_my_month"  — сотрудник: с 1-го по сегодня

ranking_sellers         "ranking_sellers"
ranking_shops           "ranking_shops"
ranking_cities          "ranking_cities"
rank_sel/shp/cty_month  — период: этот месяц (default)
rank_sel/shp/cty_7d     — 7 дней
rank_sel/shp/cty_prev   — прошлый месяц
rkcal_{y}_{m}_{d}       — «📆 Свой период» через rk-календарь

download_excel_period   "download_excel_period"  — из FSM (excel_start/excel_end/excel_shop)
```

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
admin_send_notification     — создание рассылки
view_scheduled_notifications — список (время отображается в TZ admin через format_user_datetime)
del_sched_notif_{id}        — удалить запланированное

# UTC fix: ввод time → get_utc_time(naive, admin_tz) → хранить в scheduled_datetime
# check_scheduled_notifications: now = datetime.now().isoformat() (UTC) vs scheduled_datetime (UTC)
```

### salary_handlers.py — работа с timezone

```python
# ❌ БЫЛО: now_str = datetime.now().strftime(...)  — UTC на Amvera
# ✅ СТАЛО: get_current_user_time(user_tz).strftime(...)
```

---

## 5. ПЛАНИРОВЩИК (main.py + scheduler_module.py)

```
APScheduler → запускается в main() → schedule: cron[minute='*']

send_payment_alerts()            ← напоминания об истечении подписки (14/7/3/1 день)
                                    дедупликация: subscription_reminder_log
send_sales_alerts()              ← проверяет дневные цели → уведомляет продавцов
send_daily_reports()             ← ежедневные отчёты пользователям
send_personalized_notifications() ← персонализированные уведомления
check_scheduled_notifications()  ← запланированные рассылки (UTC vs UTC)
daily_backup_task()              ← авто-бэкап в 03:00 (cron[hour=3, minute=0])
auto_finish_contests()           ← завершение конкурсов (cron[minute='*/30'])
```

**Timezone:** все задачи сравнивают `datetime.now()` (UTC на Amvera) с настроенным временем пользователя через `.astimezone(user_tz)`. Scheduled notifications: хранятся в UTC, сравниваются UTC vs UTC.

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
# ПРАВИЛЬНО — все обычные handlers:
current_db = await get_db(callback.from_user.id, state)
data = current_db.get_something()

# ПРАВИЛЬНО — payment/subscription (всегда централизованно):
db = Database('data/shop_bot.db')

# НЕПРАВИЛЬНО — устаревший паттерн:
db = Database('data/shop_bot.db')  # без get_db() — нарушает multi-tenancy
```

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

---

## 9. МОНЕТИЗАЦИЯ

```
Тарифы (subscription_plans): Бесплатный / Базовый / Стандарт / Премиум / Бизнес
Пробный период: настраивается в payment_system_admin → "Пробный период"
  → trial_days (по умолчанию 14), trial_plan (по умолчанию Бизнес)
  → выдаётся при регистрации (personal/corporate режимы)
Оплата: ручное подтверждение скриншота → confirm_payment_request → clear_reminders
Промокоды: discount_percent, max_usage, current_usage
```
