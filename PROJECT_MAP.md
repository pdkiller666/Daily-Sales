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
   │  salary_router       ← salary_handlers.py  (зарплаты)          │
   │  contests_router     ← contests_handlers.py (конкурсы)         │
   │  dashboard_router    ← dashboard_handlers.py (дашборд)         │
   └────────────────────────────────────────────────────────────────┘
     │
   ┌─┴──────────────────────────────────────────────────────────────┐
   │                  СЛОЙ ДАННЫХ                                   │
   │  db_utils.py   ← get_db(id, state), is_any_admin()            │
   │  database.py   ← класс Database (~140+ методов)               │
   │  tenant_manager.py ← маршрутизация БД по org                  │
   │  env_manager.py    ← ADMIN_CHAT_ID, BOT_TOKEN                 │
   └────────────────────────────────────────────────────────────────┘
     │
   ┌─┴──────────────────────────────────────────────────────────────┐
   │                  БАЗЫ ДАННЫХ                                   │
   │  data/main.db           ← organizations, user_org_mapping      │
   │  data/shop_bot.db       ← личный режим + платежи/подписки      │
   │  data/fsm_storage.db    ← FSM состояния (SQLiteStorage)        │
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
| `products` | id, name, category, price |
| `inventory` | id, shop_name, product_id, quantity, last_updated |
| `inventory_history` | id, shop_name, product_id, quantity_change, change_type, change_reason, user_id, timestamp |
| `sales` | id, product_id, shop_name, quantity_sold, sale_price, user_id, sale_date |
| `seller_earnings` | id, user_id, sale_id, product_id, quantity, base_amount, commission_amount, total_amount, sale_date |
| `motivation_rules` | id, product_id, commission_type (fixed/percent), commission_value |
| `notification_settings` | user_id, low_stock_enabled, daily_reports_enabled, sales_alerts_enabled, ... |
| `notification_history` | id, user_id, notification_type, message, created_at, is_read |
| `sales_plans` | id, user_id, plan_type, period_type, target_value, ... |
| `contests` | id, name, start_date, end_date, reward, status, ... |

### data/shop_bot.db ТОЛЬКО (платежи всегда централизованы)
| Таблица | Описание |
|---|---|
| `subscriptions` | id, user_id, plan_type, start_date, end_date, is_trial |
| `subscription_reminder_log` | user_id, threshold, subscription_end, sent_at — дедупликация напоминаний |
| `payment_requests` | id, user_id, plan_type, amount, payment_proof_file_id, status, created_at |
| `payment_settings` | key, value — карта, реквизиты + trial_days, trial_plan |
| `subscription_plans` | id, name, price, duration_days, max_products, max_shops, ... |
| `promocodes` | id, code, discount_percent, max_usage, current_usage, is_active |

---

## 3. КЛЮЧЕВЫЕ МОДУЛИ

### db_utils.py — точка входа к БД
```
get_db(telegram_id, state)          ← ОСНОВНАЯ функция. Всегда использовать в async handlers.
                                       Логика: super-admin + selected_org → org_db
                                               user в org → org_*.db
                                               иначе → shop_bot.db + авто-копия из main.db
get_db_sync(telegram_id)            ← для синхронных контекстов (редко)
is_any_admin(telegram_id)           ← проверяет ADMIN_CHAT_ID ИЛИ роль в user_org_mapping
get_user_org_role(telegram_id)      ← 'owner'/'admin'/'user'/None из user_org_mapping
get_user_org_scope(telegram_id)     ← (scope_type, list[str]) — тип и список зон доступа
get_user_full_scope(telegram_id)    ← (scope_type, list[str], custom_title)
get_role_display_label(role, scope_type, scope_values, custom_title=None) ← текст роли
clear_state_keep_org(state, extra_keys=None) ← очистка state с сохранением выбранной орг;
                                               extra_keys — доп. ключи FSM для сохранения
```

### env_manager.py — переменные окружения
```
is_super_admin(chat_id)  ← ID == 921098636 или первый в ADMIN_CHAT_ID
is_admin(chat_id)        ← любой ID из ADMIN_CHAT_ID  [!] не знает об org-ролях
get_admin_ids()          ← читает сначала os.environ, потом data/.env
get_bot_token()          ← BOT_TOKEN
```
**Важно:** В handlers используй `is_any_admin()` из db_utils, НЕ `env_manager.is_admin()`.

### sqlite_storage.py — FSM хранилище
```
SQLiteStorage("data/fsm_storage.db")  ← заменяет PickleStorage
  Реализует BaseStorage через sqlite3 + run_in_executor (без внешних зависимостей)
  WAL-режим: параллельные чтения, каждая запись — отдельная строка
```

### tenant_manager.py — мультиарендность
```
get_user_db_path(telegram_id)       ← путь к БД пользователя (main.db → user_org_mapping)
create_organization(name, owner_id) ← создаёт org + db-файл + запись в main.db
join_organization_by_invite(telegram_id, invite_code) ← вступление по коду
generate_invite_code(org_id)        ← создаёт/обновляет код приглашения
delete_organization(org_id)         ← удаляет org + db-файл + маппинги
get_all_organizations()             ← список всех орг для супер-адмна
change_user_role(telegram_id, org_id, role, scope_type, scope_value)
                                    ← scope_value: list[str] → хранится JSON в DB
set_user_title(telegram_id, org_id, custom_title) ← название должности (None → сброс)
```

### keyboards.py — клавиатуры
```
main_menu(chat_id, user_shop)          ← главное меню, адаптируется под роль
system_admin_menu()                    ← меню супер-адмна
admin_management_menu(chat_id)         ← меню управления
products_menu()                        ← меню каталога
inventory_menu()                       ← меню склада
back_button(callback_data)             ← кнопка "Назад"
generate_calendar(year, month, prefix) ← генератор календаря
usage_mode_keyboard()                  ← выбор режима при регистрации
```

### states.py — FSM состояния
| StatesGroup | Файл | Назначение |
|---|---|---|
| `UserRegistrationStates` | handlers.py | Регистрация нового пользователя |
| `UserProfileStates` | handlers.py | Редактирование профиля |
| `AdminUserStates` | admin_handlers.py | Редактирование пользователя администратором; `waiting_for_admin_title` — ввод должности |
| `AdminManagementStates` | admin_handlers.py | Управление организацией/admins |
| `ProductStates` | products_handlers.py | Создание/редактирование товара |
| `InventoryStates` | inventory_handlers.py | Управление складом |
| `SaleStates` | sales_handlers.py | Создание продажи |
| `MultipleSaleStates` | sales_handlers.py | Корзина продаж |
| `EditSaleStates` | sales_handlers.py | Редактирование продажи |
| `ReportStates` | reports_handlers.py | Выбор периода отчёта |
| `RankingStates` | reports_handlers.py | Свой период в рейтингах (rkcal_ prefix) |
| `SubscriptionStates` | subscription_handlers.py | Оплата подписки |
| `NotificationStates` | notifications_handlers.py | Настройка уведомлений |
| `AdminNotificationStates` | notifications_handlers.py | Рассылка от адмна |
| `PaymentSystemStates` | payment_system_admin.py | Тарифы, промокоды, пробный период |
| `MotivationStates` | commission_handlers.py | Настройка мотивации |
| `EarningsStates` | earnings_handlers.py | Просмотр заработка |
| `SalesPlanStates` | sales_plans_handlers.py | Создание/редактирование планов продаж |
| `ContestStates` | contests_handlers.py | Создание конкурсов |

---

## 4. КАРТА МОДУЛЕЙ — ФУНКЦИОНАЛЬНОСТЬ И ВЗАИМОСВЯЗИ

### handlers.py (router)
**Назначение:** регистрация, главное меню, профиль пользователя
```
/start, /menu
  → режим: личный / создать орг / вступить по коду
  → регистрация: ФИО → телефон → email → сеть → магазин → город
  → при завершении: автоматически выдаётся пробный период (personal/corporate)
main_menu_callback         "main_menu"
user_profile_menu          "user_profile"
edit_profile_menu          "edit_profile"
start_profile_edit         "prof_edit_*"
process_profile_edit       [UserProfileStates.*]
process_profile_timezone   "set_timezone_*"
```

### admin_handlers.py (admin_router)
**Назначение:** управление организациями и пользователями; переключение контекста супер-адмна
```
list_all_orgs_handler      "list_all_orgs"
delete_org_confirm         "delete_org_*"
system_admin_panel_handler "system_admin_panel", "admin_menu"
admin_users_menu           "admin_users"
admin_user_details         "admin_user_*"
admin_edit_user_*          "admin_edit_*"
manage_admins_handler      "manage_admins"
change_org_context_handler "change_org_context"
select_org_handler         "select_org_*"
generate_invite_handler    "generate_invite"
adm_shop_pick / adm_net_pick "adm_shop_pick_*" / "adm_net_pick_*"  (safe_cb + resolve_cb_name)
adm_scope_toggle           "adm_t_s_*" / "adm_t_c_*" / "adm_t_n_*" — мульти-выбор зоны
adm_scope_submit           "adm_scope_submit" — подтверждение мульти-зоны
adm_title_start/entered    "adm_title_start" / [AdminUserStates.waiting_for_admin_title]
```

### sales_handlers.py (sales_router)
```
start_sale / select_shop/category/product / process_quantity / complete_sale
edit_sales_* / edit_sale_* / delete_sale_*
```

### products_handlers.py, inventory_handlers.py
```
CRUD товаров, категорий, остатков склада
```

### reports_handlers.py (reports_router)
```
reports_menu / report_today / report_full / report_period / report_city
ranking_sellers / ranking_shops / ranking_cities
  → period: 7д / этот мес / прошлый / 📆 Свой период (rkcal_ calendar)
  → продавцы: топ + конкурсные призы за период
download_excel_*
```

### dashboard_handlers.py (dashboard_router)
```
build_admin_dashboard(db, today, now_str, user_id, telegram_id)
build_user_dashboard()
_on_shift_details() / _today_total_earnings()
```

### sales_plans_handlers.py (sales_plans_router)
```
Создание планов: тип (продавец/магазин) → период (неделя/месяц) → метрика (выручка/кол-во)
  → фильтр категории/товара → целевое значение
Мои планы: прогресс за текущий период
_plan_summary_line() — общий формат отображения плана
```

### contests_handlers.py (contests_router)
```
Создание конкурсов, просмотр, авто-завершение (APScheduler hourly)
Архив с очисткой по подтверждению
```

### salary_handlers.py (salary_router)
```
Просмотр зарплатных начислений; расчёт на основе seller_earnings
```

### commission_handlers.py (commission_router)
```
admin_motivation_menu / set_motivation / view_all_motivations / remove_motivation
show_top_sellers
```

### earnings_handlers.py (earnings_router)
```
my_earnings / earnings_current_month / earnings_all_time / earnings_detailed
earnings_select_period / earnings_specific_month
```

### payment_system_admin.py (payment_system_router)
```
payment_system_admin_menu  ← главное меню (супер-адмн)
payment_settings_*         ← реквизиты карты
manage_plans / edit_plan   ← тарифные планы
payment_statistics         ← статистика платежей
manage_subscriptions       ← управление подписками пользователей
manage_promocodes / *      ← промокоды CRUD
trial_settings             ← настройки пробного периода ★NEW
  trial_edit_days          ← длительность (дней)
  trial_edit_plan          ← тарифный план
```

### subscription_handlers.py (subscription_router)
```
subscription_menu / subscription_plans / start_subscription_purchase
process_payment_proof / subscription_limits
notify_admins_about_payment_request — уведомление при новой заявке
```

### payment_admin_handlers.py (payment_admin_router)
```
pending_payments_menu / view_payment_request / confirm_payment_request
reject_payment_request
  → подтверждение: clear_reminders(user_id) сбрасывает лог напоминаний
```

### notifications_handlers.py (notifications_router)
```
notifications_menu / notification_settings_menu / toggle_*
notification_history_menu / admin_send_notification
```

### backup_handlers.py (backup_router)
```
backup_management_menu / create_backup_manual / list_backups
restore_backup_* / cleanup_old_backups
```

### contacts_handlers.py (contacts_router)
```
view_contacts_menu — личный: свой профиль; корпоративный: коллеги в орг
```

---

## 5. ПЛАНИРОВЩИК (main.py + scheduler_module.py)

```
APScheduler → запускается в main() → schedule: cron[minute='*']

send_payment_alerts()      ← напоминания об истечении подписки за 14/7/3/1 день
                              дедупликация: subscription_reminder_log в shop_bot.db
                              при продлении подписки: clear_reminders() сбрасывает лог
send_sales_alerts()        ← проверяет дневные цели → уведомляет продавцов
send_daily_reports()       ← ежедневные отчёты пользователям
send_personalized_notifications() ← персонализированные уведомления
check_scheduled_notifications() ← запланированные рассылки
daily_backup_task()        ← авто-бэкап в 03:00 (cron[hour=3, minute=0])
auto_finish_contests()     ← завершение конкурсов по расписанию (cron[minute='*/30'])
```

---

## 6. РОЛИ И ПРАВА ДОСТУПА

| Роль | Определяется | Права |
|---|---|---|
| **super_admin** | ID == 921098636 или первый в ADMIN_CHAT_ID | Всё, переключение контекста орг |
| **owner** | role='owner' в user_org_mapping | Директор; управление всей орг, назначение ролей, кастомные должности |
| **admin** | role='admin' + scope_type/scope_value в user_org_mapping | Зам/Магазин/Город/Сеть; отчёты и управление по своей зоне |
| **user (org)** | role='user' в user_org_mapping | Продажи, склад своего магазина, заработок |
| **user (personal)** | в shop_bot.db, не в маппинге | Только свои данные |

**Зоны доступа (scope):** `scope_type` = 'shop'/'city'/'network'/None; `scope_value` = JSON array `["Магазин А","Магазин Б"]`. Пустой список = полный доступ.

**Кастомные должности:** `custom_title` в `user_org_mapping`; отображается вместо стандартного ярлыка.

**Проверка прав:** `is_any_admin(telegram_id)` из db_utils → проверяет ADMIN_CHAT_ID ИЛИ роль в org. Роль 'owner'/'admin' → True; 'user' → False (даже если env_manager.is_admin() = True).

---

## 7. ПАТТЕРН ДОСТУПА К БД

```python
# ПРАВИЛЬНО — все обычные handlers:
current_db = await get_db(callback.from_user.id, state)
data = current_db.get_something()

# ПРАВИЛЬНО — payment/subscription (всегда централизованно):
def _get_db():
    return Database('data/shop_bot.db')
db = _get_db()

# НЕПРАВИЛЬНО (устаревший паттерн):
db = Database('data/shop_bot.db')  # без create_tables() — риск пустой БД
```

---

## 8. КЛЮЧЕВЫЕ ПРАВИЛА (GOTCHAS)

1. `clear_state_keep_org(state)` **ПОСЛЕ** `fsm_edit(...)`, никогда до
2. `state.clear()` **запрещён** — только `clear_state_keep_org(state)`
3. `he()` для ВСЕХ пользовательских строк в HTML-сообщениях (не в button.text — Telegram не парсит)
4. `safe_cb()` / `resolve_cb_name()` для callback_data с user-строками (лимит 64 байта)
5. `get_users_for_notifications()` → `[0]` = users.id, `[1]` = telegram_id — не путать
6. `Database.db_file` (не `.db_path`) — атрибут пути к файлу БД
7. Новый роутер → зарегистрировать в `main.py`; новый модуль → добавить в `test_imports.py`
8. `get_sales_ranking()` возвращает 8 колонок — 8-я = `u.id`; `row[:7]` для старого 7-кол. unpacking
9. Пробный период: `trial_days` и `trial_plan` в `payment_settings`; `create_trial_subscription()` в database.py
10. Напоминания о подписке: `subscription_reminder_log` в shop_bot.db; `clear_reminders(user_id)` при продлении
11. FSM хранилище: PickleStorage (`data/fsm_storage.pkl`) — используется через `pickle_storage.py`
12. `get_user_org_scope()` → `(scope_type, list[str])` НЕ `(str, str)` — scope_values всегда список
13. `clear_state_keep_org(state, extra_keys=[...])` — для сохранения доп. ключей FSM через очистку (пример: excel_start/excel_end/excel_shop для Download Excel)
14. `data/` исключены из обоих репо — runtime БД только на Amvera persistenceMount

---

## 9. МОНЕТИЗАЦИЯ

```
Тарифы (subscription_plans): Бесплатный / Базовый / Стандарт / Премиум / Бизнес
Пробный период: настраивается в payment_system_admin → "Пробный период"
  → trial_days (по умолчанию 14), trial_plan (по умолчанию Бизнес)
  → выдаётся автоматически при регистрации (personal/corporate режимы)
Оплата: ручное подтверждение скриншота → confirm_payment_request → clear_reminders
Промокоды: discount_percent, max_usage, current_usage
```
