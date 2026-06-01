# AGENT HANDOFF — Daily Sales Telegram Bot
> Последнее обновление: 2026-06-01 (сессия 247)
> Файл находится в корне проекта: `AGENT_HANDOFF.md` — пушится на GitHub, не деплоится на Amvera, не попадает в .local.
> Документ для агента, принимающего разработку. Содержит всё необходимое для немедленного продолжения работы.

---

## 0. БЫСТРЫЙ СТАРТ

```
Бот: @BotCraftAi_Test_3_bot  (тестовый, Replit)
Продакшн (Amvera): https://git.msk0.amvera.ru/pdkiller666/dailysalesdeploy
GitHub: https://github.com/pdkiller666/Daily-Sales.git
Super-admin Telegram ID: 921098636
Workflow: "Start application" → python main.py

Деплой (по умолчанию — GitHub + Amvera напрямую):
  bash deploy.sh "Сообщение коммита"

Только GitHub (без Amvera):
  bash deploy.sh "Сообщение" --no-amvera
```

**Секреты в Replit Secrets (никогда не хардкодить):**
- `BOT_TOKEN` — токен тестового бота
- `GITHUB_TOKEN` — токен для push на GitHub
- `ADMIN_CHAT_ID` — ID супер-администратора

**Последний деплой:** GitHub `31f3ba5` · Amvera `db84810` (2026-06-01, сессия 247). Оба хэша верифицированы через `git ls-remote`.

**Сессии 234–237 (2026-05-31) — «Системный» shop fix + filter panel + perf:**
- 234-235: `get_all_shops()` / `get_inventory_shops()` — параметр `include_system=False`; суперадмин передаёт `include_system=True`
- 236: Пресет инвайта — UNION с inventory (ТЦ Бум теперь виден)
- 237: `filter_utils.py` — shops из users+shops+inventory; `_low_stock_count` — shops из users+shops; `asyncio.gather` в commission_handlers (3 места) и sales_plans_handlers

**Сессия 224 (2026-05-27) — INVITE SYSTEM IMPROVEMENTS (A–E):**
- **А) Deep-link**: `/start CODE` через `Command("start")` filter в `handlers.py`; парсит аргумент инвайт-кода и автоматически привязывает к орге без ручного ввода
- **Б) Ротация кода**: `rotate_invite_code(org_id)` в `tenant_manager.py`; кнопка «🔄 Сбросить код» на экране инвайта → новый код генерируется и обновляется в БД
- **В) Уведомление owner/admin**: `_notify_org_join()` в `handlers.py` — вызывается в `process_city` через `asyncio.create_task()`; получает список admin telegram_ids из `get_org_admin_telegram_ids()`
- **Г) Имя из Telegram**: `_show_name_prefill_fsm()` в `handlers.py` при join-режиме предлагает кнопки «✅ Использовать имя из профиля» / «✏️ Ввести вручную»; callbacks `name_tg_use` / `name_tg_manual`
- **Д) Пресет роли и магазина**: `invite_preset_role`, `invite_preset_shop` в таблице `organizations`; `set_invite_preset()`, `get_invite_preset_by_code()`, `get_invite_preset_by_org()` в tenant_manager; экран настройки через `invite_preset_start_N` → `ipr_role_X_N` → `ipr_shop_TOKEN_N`; при регистрации по инвайту пресет из БД пропускает шаги выбора роли/магазина
- **Экран инвайта**: `_show_invite_screen()` — universal helper в `admin_handlers.py`; показывает код + deep-link URL + пресет + кнопки «🔄 Сбросить» / «⚙️ Пресет» / «◀️ Назад»
- **bot_holder.py**: добавлены `set_username()` / `get_username()`; в `main.py` username бота сохраняется при старте через `get_me()`
- **README.md**: полная перепись с разделом про invite-систему
- **Тесты**: 45 импортов OK, 702 сценария OK

**Сессия 200 (2026-05-26) — фичи и фиксы:**
- **Фильтры получателей уведомлений** (`notifications_handlers.py`): после ввода текста → выбор «👥 Всем / 🏪 По магазину / 🎭 По роли»; превью с числом получателей; запланированные уведомления сохраняют фильтр в `recipients_list` (JSON); `check_scheduled_notifications` применяет фильтр при отправке. Супер-admin: только «Всем».
- **Google Sheets `invalid_grant` fix** (`integration/auth/google_oauth.py`, `integration/manager.py`, `sales_handlers.py`, `integration_handlers.py`): `OAuthTokenRevokedException` класс; `_normalize_gs_error()` конвертирует `google.auth.RefreshError` в дружелюбное сообщение; автоматически отключает подключение (`enabled=0`); в продаже показывает «🔑 Требуется переподключение»; в sync_motivation — инструкция по переподключению.
- **Перенос экспортов между подключениями** (`integration_handlers.py`, `database.py`): кнопка «📤 Перенести экспорты» на экране подключения (только если есть экспорты); выбор целевого подключения → экран подтверждения с превью экспортов → одна кнопка «✅ Перенести»; `move_exports_to_connection(from_id, to_id)` в database.py — UPDATE connection_id одной транзакцией; все псевдонимы и настройки сохраняются.

**Дополнительные секреты (Google Sheets):**
- `GOOGLE_OAUTH_CLIENT_ID` — OAuth client_id из Google Cloud Console
- `GOOGLE_OAUTH_CLIENT_SECRET` — OAuth client_secret

**Верификация Amvera:** После каждого пуша `deploy.sh` автоматически проверяет `git ls-remote` и печатает:
`Amvera verify: ✅ remote hash совпадает (hash)` или `⚠️ расхождение!`

---

## 1. АРХИТЕКТУРА ПРОЕКТА

```
Telegram API
    ↓
main.py  — polling, регистрация роутеров, APScheduler (9 задач)
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  21 РОУТЕР (handlers)                                            │
│  router               ← handlers.py         (старт, профиль)    │
│  admin_router         ← admin_handlers.py   (орг, юзеры)        │
│  sales_router         ← sales_handlers.py   (продажи)           │
│  products_router      ← products_handlers.py (каталог, остатки) │
│  inventory_router     ← inventory_handlers.py (склад)           │
│  reports_router       ← reports_handlers.py  (отчёты+рейтинги)  │
│  commission_router    ← commission_handlers.py (мотивация)      │
│  earnings_router      ← earnings_handlers.py  (заработок)       │
│  sales_plans_router   ← sales_plans_handlers.py (планы продаж)  │
│  salary_router        ← salary_handlers.py    (зарплата/смены)  │
│  contacts_router      ← contacts_handlers.py  (контакты)        │
│  notifications_router ← notifications_handlers.py               │
│  payment_admin_router ← payment_admin_handlers.py               │
│  payment_system_router← payment_system_admin.py                 │
│  subscription_router  ← subscription_router.py                  │
│  backup_router        ← backup_handlers.py                      │
│  contests_router      ← contests_handlers.py  (конкурсы)        │
│  dashboard_router     ← dashboard_handlers.py (дашборд-сводка)  │
│  filter_router        ← filter_handlers.py    (общий фильтр)    │
│  integration_router   ← integration_handlers.py (Google Sheets) │
│  referral_router      ← referral_handlers.py  (реф. программа)  │
│  addon_router         ← addon_handlers.py     (надстройки)      │
└──────────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  СЛОЙ ДАННЫХ                                                     │
│  db_utils.py        — get_db(), is_any_admin() [ГЛАВНЫЙ]        │
│  database.py        — класс Database (170+ методов)             │
│  tenant_manager.py  — маршрутизация БД по организации           │
│  env_manager.py     — BOT_TOKEN, ADMIN_CHAT_ID                  │
└──────────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  БАЗЫ ДАННЫХ                                                     │
│  data/main.db              — organizations, user_org_mapping     │
│  data/shop_bot.db          — личный режим + платежи             │
│  data/tenants/org_*.db     — изолированные БД организаций       │
│  (Amvera prod: только org_huawei.db в persistent /app/data)     │
└──────────────────────────────────────────────────────────────────┘
```

**Вспомогательные модули:**

| Файл | Назначение |
|---|---|
| `payment_provider.py` | Фабрика провайдеров: `get_active_provider(db)`, `create_yookassa_payment(...)`, `check_yookassa_payment_status(...)`, `provider_label(provider)` |
| `plan_notifications.py` | Уведомления об изменениях тарифов (lazy-импорт из payment_system_admin.py) |
| `scheduler_module.py` | Синглтон APScheduler — set_scheduler() / get_scheduler() |
| `timezone_utils.py` | get_user_time(), get_current_user_time(), format_user_datetime(), get_utc_time() |
| `reports_access_control.py` | Проверка доступа к отчётам по подписке |
| `subscription_utils.py` | Лимиты подписки: `get_plan_limits(tg_id)` → dict; `_has_active_trial()` → bool (триал = `_UNLIMITED`); `check_integrations_permission()` / `check_export_permission()` / `check_analytics_permission()` / `check_notifications_permission()` / `check_product_limit()` / `check_sales_limit()` / `check_shop_limit()`; `get_subscription_warning_message()` показывает сравнение тарифов |
| `backup_manager.py` | Резервное копирование и восстановление всех БД |
| `restart_manager.py` | Управление перезапуском бота; restart_data_file = "data/restart_data.json" |
| `message_utils.py` | fsm_edit(), safe_edit_message() — anchor message pattern |
| `utils.py` | he(), escape_md(), format_currency(), format_price(), generate_excel_report() |
| `pickle_storage.py` | FSM persistence через PickleStorage (data/fsm_storage.pkl) |
| `filter_utils.py` | empty_filter(), get_available_filter_values(), merge_scope_with_filter(), build_filter_keyboard() |
| `filter_handlers.py` | filter_router: flt_open_{back_cb}, ftog_s/c/n_*, flt_reset |
| `hints.py` | hint_suffix(), maybe_send_welcome() — onboarding и inline-подсказки |
| `notif_utils.py` | add_read_btn() — добавляет «✅ Прочитано» ко всем push-уведомлениям |
| `pagination_utils.py` | paginate(), page_nav_row(), PAGE_SIZE_DEFAULT/SALES/USERS/ORGS |
| `scripts/post-merge.sh` | post-merge setup: `pip install -r requirements.txt`; зарегистрирован в `.replit [postMerge]`, таймаут 60s — запускается автоматически после каждого мержа задачи-агента |
| `integration/auth/google_oauth.py` | Device Flow OAuth 2.0: `get_client_credentials()` ← env vars; `initiate_device_flow()` → {device_code, user_code, verification_url}; `poll_for_token(device_code)` → access_token; `refresh_access_token(refresh_token)` → новый access_token |
| `integration/manager.py` | `integration_manager` синглтон; `trigger_export(db, export_type, event_data)` — вызывается из `complete_sale` для типа `'sales'`; читает `integration_connections`+`integration_exports` из той же org DB |
| `integration_handlers.py` | `integration_router`: настройка подключений GS, авторизация через Device Flow, просмотр/удаление связей |
| `referral_handlers.py` | `referral_router`: экран реф. программы (`subscription_referral`); статистика рефералов; deep-link `/start ref_TELEGRAMID` |
| `addon_handlers.py` | `addon_router`: надстройки к подписке (`subscription_addons`); покупка доп. магазинов (150₽/30д) и товаров (100₽/30д); `AddonStates.entering_qty`; маршрутизирует оплату в `confirm_payment_request` с prefix `addon_` |
| `pdf_utils.py` | `generate_pdf_report(org_name, start_date, end_date, sales_rows, summary, top_sellers, top_products)` → path\|None; `generate_pdf_for_report(db, scope, ...)` → path\|None — фасад для handlers; требует `reportlab` |

---

## 2. КРИТИЧЕСКИЕ ПРАВИЛА (нарушение → баги)

### 2.1 Доступ к БД — ТОЛЬКО через get_db()

```python
# ✅ ПРАВИЛЬНО — все обычные handlers (AsyncDatabase, все методы через await):
from db_utils import get_db
current_db = await get_db(callback.from_user.id, state)
result = await current_db.some_method()        # ← await обязателен!
items  = await current_db.get_all_products()

# ✅ ПРАВИЛЬНО — payment/subscription handlers (всегда shop_bot.db):
db = wrap_db(Database('data/shop_bot.db'))      # wrap_db() из db_utils!

# ✅ ПРАВИЛЬНО — APScheduler (sync контекст, не async):
db = get_db_sync(telegram_id)                  # возвращает Database, без await
result = db.some_method()                      # без await

# ✅ ПРАВИЛЬНО — parallel queries (asyncio.gather):
r1, r2, r3 = await asyncio.gather(
    current_db.get_sales_summary(...),
    current_db.get_plans_progress(),
    current_db.get_contests(status='active'),
    return_exceptions=True,
)

# ❌ НЕПРАВИЛЬНО — глобальный db в начале обычного файла:
db = Database('data/shop_bot.db')  # создаёт утечку изоляции между орг

# ❌ НЕПРАВИЛЬНО — вызов без await:
result = current_db.some_method()  # возвращает coroutine, не данные!
```

**get_db() логика (db_utils.py):**
```
super-admin + selected_org_db в state → AsyncDatabase(Database(selected_db))
user в org (tenant_manager)            → AsyncDatabase(Database(org_*.db))
иначе                                  → AsyncDatabase(Database(shop_bot.db))
```

**AsyncDatabase (db_utils.py):**
```python
class AsyncDatabase:
    # __getattr__ оборачивает ВСЕ вызываемые атрибуты Database в asyncio.to_thread()
    # db_file — explicit @property (без asyncio.to_thread)
    # sync доступ к внутреннему объекту: getattr(db, '_db', db) → Database
    wrap_db(db: Database) → AsyncDatabase   # явная обёртка
```

### 2.2 Проверка прав — ТОЛЬКО через is_any_admin()

```python
# ✅ ПРАВИЛЬНО:
from db_utils import is_any_admin
if is_any_admin(telegram_id):  # проверяет ADMIN_CHAT_ID ИЛИ роль owner/admin в org

# ❌ НЕПРАВИЛЬНО (не знает об org-ролях):
from env_manager import env_manager
if env_manager.is_admin(chat_id):
```

### 2.3 Anchor Message Pattern — порядок КРИТИЧЕН

```python
# ✅ ПРАВИЛЬНО — clear_state_keep_org ПОСЛЕ fsm_edit:
await fsm_edit(callback, state, text, keyboard)
await clear_state_keep_org(state)

# ❌ НЕПРАВИЛЬНО — clear ПЕРЕД edit (сбрасывает anchor_msg_id до его использования):
await clear_state_keep_org(state)
await fsm_edit(callback, state, text, keyboard)
```

### 2.4 Очистка state — ТОЛЬКО через clear_state_keep_org()

```python
# ✅ ПРАВИЛЬНО:
from db_utils import clear_state_keep_org
await clear_state_keep_org(state)

# ✅ С доп. ключами (например Excel):
await clear_state_keep_org(state, extra_keys=["excel_start", "excel_end", "excel_shop"])

# ❌ ЗАПРЕЩЕНО — теряет контекст орг у супер-админа:
await state.clear()
```

### 2.5 callback_data — КРИТИЧЕСКИЙ ЛИМИТ 64 байта

```python
# ✅ ПРАВИЛЬНО — для user-provided строк:
from keyboards import safe_cb, resolve_cb_name
builder.button(text=shop, callback_data=safe_cb("sale_shop_", shop))
shop = resolve_cb_name(raw, current_db.get_all_shops() or [])

# ❌ НЕПРАВИЛЬНО — кириллица >= 22 символов ломает API:
callback_data=f"sale_shop_{shop_name}"
```

### 2.6 HTML в сообщениях — ТОЛЬКО через he() для user-строк

```python
# ✅ ПРАВИЛЬНО:
from utils import he
text = f"Товар: <b>{he(product_name)}</b>\nМагазин: <b>{he(shop_name)}</b>"

# ✅ ИСКЛЮЧЕНИЕ — тексты кнопок Telegram НЕ парсит как HTML:
InlineKeyboardButton(text=f"🏪 {shop_name}")  # he() не нужен

# ❌ НЕПРАВИЛЬНО — < > & → TelegramBadRequest:
text = f"Товар: <b>{product_name}</b>"
```

### 2.7 Инициализация переменных перед try/except

```python
# ✅ ПРАВИЛЬНО:
plans_progress = []
try:
    plans_progress = current_db.get_plans_progress()
except Exception:
    pass

# ❌ НЕПРАВИЛЬНО — NameError если try бросит исключение:
try:
    plans_progress = current_db.get_plans_progress()
except Exception:
    pass
if plans_progress:  # NameError!
```

### 2.8 Timezone — отображение времени пользователю

```python
# ✅ ПРАВИЛЬНО — все отображения времени через timezone_utils:
from timezone_utils import format_user_datetime, get_current_user_time
user_tz = current_db.get_user_timezone(user_id)
now_str = get_current_user_time(user_tz).strftime('%d.%m.%Y · %H:%M')
time_str = format_user_datetime(raw_iso_str, user_tz, '%H:%M')

# ✅ ПРАВИЛЬНО — сохранять время в UTC:
from timezone_utils import get_utc_time
utc_dt = get_utc_time(naive_local_dt, admin_tz)  # admin вводит → конвертировать в UTC

# ❌ НЕПРАВИЛЬНО — datetime.now() на Amvera это UTC, не Москва:
now_str = datetime.now().strftime('%H:%M')
```

---

## 3. ДЕПЛОЙ И AMVERA

### deploy.sh — механика

- Синхронизирует файлы workspace → `/tmp/github-deploy` и `/tmp/amvera-deploy`
- **Исключает**: `.git`, `.local`, `__pycache__`, `data/backup`, `data/tenants`, `*.db`, `*.pkl`, `*.log`
- `AGENT_HANDOFF.md`, `replit.md`, `PROJECT_MAP.md`, `README.md` → только в GitHub, не в Amvera
- GitHub: `git push --force origin HEAD:main`
- Amvera: `git push amvera HEAD:master --force` (ВСЕГДА по умолчанию)
- После пуша: `git ls-remote amvera` → верификация хэша
- Флаг `--no-amvera` — только GitHub

### Amvera warnings ("вне persistenceMount") — ВСЕ ЛОЖНЫЕ

Amvera статически сканирует `sqlite3.connect('data/...')` → предупреждает. Но рабочая директория на Amvera = `/app`, поэтому `data/` = `/app/data/` = persistenceMount ✅.

### ⚠️ Amvera: битые сборки и "Internal server error"

**Симптомы:** Amvera запускает старый контейнер несмотря на успешный `push` + `git ls-remote ✅`.

**Причина:** `deploy.sh` проверяет только что код **дошёл** до Amvera git-репозитория. Это НЕ гарантирует, что сборка прошла. Amvera строит контейнер отдельно — если сборка падает, продолжает крутить последний успешный образ.

**Диагностика:** В разделе "Логи" → "Контроль версий" (Amvera UI) смотреть колонку "Используется":
- ✅ зелёный = задеплоен и работает
- ✅ хранится + ❌ не используется = собрался, но не задеплоился  
- отсутствует в списке = сборка упала с "Internal server error" и не была записана

**"Internal server error"** — инфраструктурная ошибка Amvera, не ошибка кода. Лечится повторным пушем (иногда несколько попыток). Если повторяется >3 раз — писать в поддержку Amvera.

**Форсированный перепуш** (триггер пересборки без изменений кода):
```bash
# Добавить комментарий с датой в requirements.txt, затем:
bash deploy.sh "chore: trigger rebuild YYYY-MM-DD"
```

**❌ ЗАПРЕЩЕНО в amvera.yml** — поля которых НЕ СУЩЕСТВУЕТ:
```yaml
# НЕ ДОБАВЛЯТЬ — вызывает "Configuration error: unknown fields":
build:
  quickBuild: false   # ← НЕ СУЩЕСТВУЕТ в Amvera
```
Правильная минимальная конфигурация — только `meta` + `run` (см. раздел выше).

**Два инстанса бота при рестарте** — норма: при перезапуске контейнера старый и новый процесс ~7 секунд работают параллельно → `TelegramConflictError`. APScheduler-задачи могут выполниться дважды. Само проходит, не баг.

### Amvera persistent data (production)

- Persistent mount: `/app/data`
- Файлы: `main.db`, `shop_bot.db`, `fsm_storage.pkl`, `tenants/org_huawei.db`, `backup/`
- `org_huawei.db` — единственная тенант-БД в production. `create_tables()` авто-применяет миграции.

---

## 4. БАЗЫ ДАННЫХ

### data/main.db — только через tenant_manager

| Таблица | Ключевые колонки |
|---|---|
| `organizations` | id, name, owner_id, invite_code, db_path, subscription_plan, is_active, invite_preset_role (NULL/'admin'/'user'), invite_preset_shop (NULL/str) |
| `user_org_mapping` | telegram_id, org_id, role (owner/admin/user), scope_type, scope_value (JSON array), custom_title |

### data/shop_bot.db и data/tenants/org_*.db — идентичная схема

| Таблица | Назначение |
|---|---|
| `users` | telegram_id, first_name, last_name, phone, email, trade_network, shop_name, city, timezone, **username** (индекс 12) |
| `products` | id, name, category, price, motivation_type, motivation_value, **photo_file_id** (Telegram file_id фото), **description** (TEXT, описание товара) — добавлены миграцией ALTER TABLE |
| `inventory` | shop_name, product_id, quantity, last_updated |
| `inventory_history` | shop_name, product_id, quantity_change, change_type, change_reason, user_id, timestamp |
| `sales` | product_id, shop_name, quantity_sold, sale_price, user_id, sale_date |
| `seller_earnings` | user_id, sale_id, product_id, quantity, base_amount, commission_amount, total_amount, sale_date |
| `motivation_rules` | product_id, commission_type, commission_value, admin_id |
| `motivation_conditions` | условия мотивации (сверхплан, коэф. смены, фильтр категорий) |
| `motivation_extra_conditions` | доп. условия (миграция в create_tables) |
| `salary_settings` | user_id, daily_rate, updated_by |
| `work_schedule` | user_id, work_date, **start_time**, **end_time**, marked_by — UNIQUE(user_id, work_date) |
| `shift_templates` | user_id, weekday (0=Пн..6=Вс), start_time, end_time — UNIQUE(user_id, weekday) |
| `sales_plans` | id, plan_type, metric_type, target_value, target_type, user_id, shop_name, filter_type, filter_value, is_active, created_by |
| `plan_milestone_alerts` | user_id, plan_id, milestone (50/75/100), period_start — UNIQUE(user_id, plan_id, milestone, period_start) |
| `notification_settings` | low_stock_alerts, daily_reports, sales_alerts, payment_alerts, admin_notifications, **shift_sale_alerts** (индекс 11, добавлен миграцией) |
| `integration_connections` | id, name, provider ('google_sheets'), config (JSON), enabled, created_at, updated_at |
| `integration_exports` | id, connection_id (FK), export_type, enabled, schedule, target_sheet, operation, mapping (JSON), lookup_config (JSON), extra (JSON), last_run |
| `integration_log` | id, connection_id, export_id, status, message, created_at |
| `gs_bonus_cache` | id, connection_id, model_name, chain, bonus, rrp, synced_at — UNIQUE(connection_id, model_name, chain) |
| `notification_history` | id, user_id, notification_type, message, created_at, is_read |
| `scheduled_notifications` | id, job_id(UUID), created_by(FK→users.id!), notification_text, recipients_type, scheduled_datetime(**UTC**), status |
| `contests` | id, title, contest_type, scope, metric, target_value, reward_type, reward_value, start_date, end_date, status, winner_user_id |
| `user_hints_seen` | user_id, hint_key, seen_at |
| `user_product_favorites` | user_id, product_id |
| `user_product_recent` | user_id, product_id, last_used |

### data/shop_bot.db ТОЛЬКО (платежи централизованы):

| Таблица | Назначение |
|---|---|
| `subscriptions` | user_id, plan_type, start_date, end_date, is_active |
| `subscription_reminder_log` | user_id, threshold, subscription_end, sent_at |
| `payment_requests` | заявки на оплату + file_id чека + promocode_id |
| `payment_settings` | card_number, recipient_name, bank_name, trial_days, trial_plan, **payment_provider** ('sbp'/'yookassa'), **yookassa_shop_id**, **yookassa_secret_key**, **yookassa_return_url** |
| `subscription_plans` | name, price, duration_days, max_products, max_shops, max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications, **can_use_integrations** (0 для Базового!), is_active |
| `promocodes` | code, discount_percent, max_usage, current_usage, is_active |
| `yookassa_payments` | yookassa_payment_id (UNIQUE), user_id, plan_type, amount, status, promocode_id, is_scheduled, schedule_date |
| `referrals` | id, referrer_id (telegram_id), referred_id (telegram_id), created_at, bonus_applied (0/1) — реф. программа |
| `subscription_addons` | id, user_id, addon_type ('extra_shops'/'extra_products'), quantity, expires_at, created_at — надстройки |

---

## 5. КЛЮЧЕВЫЕ ФУНКЦИИ

### db_utils.py

```
get_db(telegram_id, state)            → AsyncDatabase  — ОСНОВНАЯ функция (async, await обязателен)
get_db_sync(telegram_id)              → Database       — для синхронных контекстов (APScheduler)
wrap_db(db: Database)                 → AsyncDatabase  — явная обёртка для inline Database()
is_any_admin(telegram_id)             → bool           — ADMIN_CHAT_ID ИЛИ роль owner/admin в org
get_user_org_role(telegram_id)        → str|None       — 'owner'/'admin'/'user'/None
get_user_org_scope(telegram_id)       → (scope_type, list[str])
get_user_full_scope(telegram_id)      → (scope_type, list[str], custom_title)
get_role_display_label(role, scope_type, scope_values, custom_title=None) → str
clear_state_keep_org(state, extra_keys=None) → None
```

**AsyncDatabase:**
```python
# db_utils.py — класс AsyncDatabase
# __init__: object.__setattr__(self, '_db', db)
# __getattr__: если атрибут callable → asyncio.to_thread(fn, *args, **kwargs)
#              если не callable → прямой return (числа, строки, None)
# db_file: explicit @property → self._db.db_file (без to_thread)
# sync доступ к внутреннему объекту: getattr(async_db, '_db', async_db) → Database
# Используется в hints.py, maybe_refresh_username для sync-совместимости
```

**Пул соединений (database.py):**
```python
# _conn_pool = threading.local()  — thread-local хранилище
# _get_pooled_conn(db_file)       — создаёт/возвращает existing conn для потока
#   check_same_thread=False, PRAGMA WAL/cache/temp/mmap при первом создании
# _PooledConn(conn)               — обёртка: close() = rollback (не disconnect!)
#   все другие методы/атрибуты → proxy к raw connection
# get_connection() → _PooledConn  — используется во всех 200+ методах Database
# Эффект: один поток = одно соединение = нет overhead открытия/закрытия
```

### timezone_utils.py

```python
get_user_time(naive_dt, tz_name)         → datetime  — naive UTC → tz-aware local
get_current_user_time(tz_name)           → datetime  — текущее время в TZ пользователя
format_user_datetime(raw_str, tz, fmt)   → str       — ISO UTC-строка → форматированное локальное
get_utc_time(naive_local_dt, tz_name)    → datetime  — local naive → UTC datetime

# raw_str в format_user_datetime может быть ISO datetime или "HH:MM" — оба обрабатываются
```

### utils.py — generate_excel_report()

```python
generate_excel_report(sales, title, start_date, end_date) → openpyxl.Workbook

# 4 листа:
# 1. «Детальный отчёт» — все строки (до 50000), столбец «Продавец», числовые форматы, ИТОГО
# 2. «По категориям» — сводка + BarChart
# 3. «По продавцам» — если данные есть (len(row) >= 11), BarChart
# 4. «По дням» — если >1 дата, BarChart

# Определение seller data: len(row) >= 11 → get_sales_report (11 col); len(row) == 9 → get_user_sales
```

### reports_handlers.py — Excel download handlers

```
_translit_filename(text) → str       — транслитерация для имён файлов
download_excel_full      — полный отчёт; scope-фильтр, лимит 50000
download_excel_period    — период из FSM (excel_start/excel_end/excel_shop)
download_excel_user      — отчёт пользователя
download_excel_shop      — отчёт по магазину
download_excel_city      — отчёт по городу (один SQL-запрос для city)
download_excel_my        — "Мои продажи" (текущий месяц)
```

### reports_handlers.py — отображение товаров в сообщениях

```
report_today            — все товары (MAX_ADMIN_LINES=25 для admin; пользователь — все)
report_full             — все категории и магазины (guard > 3500/3700 символов)
report_shop_generate    — все товары по категориям (guard > 3600)
report_my_shop          — все товары (guard > 3700 — УБРАН [:3])
generate_period_report  — все товары (guard > 3700 — УБРАН [:3])
```

**Единый принцип guard'ов:**
- `> 3600` на уровне магазинов → «скачайте Excel для полной детализации»
- `> 3700` на уровне товаров → «остальные товары в Excel»
- `> 3800` сплошная обрезка (report_today)

### dashboard_handlers.py

```python
build_admin_dashboard(current_db, today, now_str, user_id=0, telegram_id=0) → str
build_user_dashboard(current_db, user_id, telegram_id, today, now_str)      → str
_on_shift_details(db_file, today)     → list[(first_name, last_name, shop_name)]
_today_total_earnings(db_file, today) → float
_plan_summary_line(plan, actual, percent) → str  — канонический формат плана
# now_str вычисляется через get_current_user_time(user_tz) в вызывающем коде
```

### salary_handlers.py — callbacks смен и шаблонов

```
Callback prefixes:
  slr_rates              — ставки сотрудников
  slr_set_{uid}          — редактировать ставку
  slr_scheds             — список графиков
  slr_cal_{uid}_{y}_{m}  — календарь смен (admin)
  slr_tog_{uid}_{date}   — переключить день (добавить/снять, применяет шаблон при добавлении)
  slr_day_{uid}_{date}   — под-экран управления конкретным днём
  slr_rm_{uid}_{date}    — снять смену
  slr_ets_{uid}_{date}   — изменить время: пикер часов начала
  slr_etw_{uid}_{date}   — пикер часов конца
  slr_te_{h}_{uid}_{date}  — выбрать час начала → сразу к slr_etw_
  slr_tw_{h}_{uid}_{date}  — выбрать час конца → сохранить + slr_day_
  slr_tmpl_{uid}_{y}_{m}   — шаблон смен (⏰ Расписание смен)
  tmpl_day_{uid}_{wd}       — день шаблона (0=Пн..6=Вс)
  tmpl_off_{uid}_{wd}       — отметить выходным
  tmpl_te_{uid}_{wd}        — пикер начала шаблона
  tmpl_tw_{uid}_{wd}        — пикер конца шаблона
  tmpl_te_h_{h}_{uid}_{wd}  — выбрать час начала шаблона
  tmpl_tw_h_{h}_{uid}_{wd}  — выбрать час конца → сохранить
  my_schedule               — своё расписание (user)
  my_d_{date}               — детали дня (user, editable=False)
  slr_sum_{y}_{m}           — зарплатный итог за месяц
```

### filter_utils.py + filter_handlers.py

```
ADMIN_FILTER_KEY = "admin_filter"  — ключ в FSM data, сохраняется между экранами

empty_filter()                     → dict  — пустой фильтр {shops:[], cities:[], networks:[]}
get_available_filter_values(db, scope_type, scope_values) → dict
merge_scope_with_filter(scope_type, scope_values, active_filter) → dict  — scope потолок, фильтр пол
build_filter_keyboard(available, active, back_cb) → InlineKeyboardMarkup

Callbacks (filter_router):
  flt_open_{back_cb}  — открыть панель (back_cb = callback кнопки «✅ Применить»)
  ftog_s_{val}        — переключить магазин (safe_cb/resolve_cb_name)
  ftog_c_{val}        — переключить город
  ftog_n_{val}        — переключить торговую сеть
  flt_reset           — сбросить фильтр
```

### hints.py + notif_utils.py + pagination_utils.py

```python
# hints.py
hint_suffix(db, user_id, hint_key) → str   — подсказка при первом визите раздела
maybe_send_welcome(msg_or_cb, db, user_id, is_admin)  — popup при первом входе
# Ключи HINT_TEXTS: 'sales','reports','products','dashboard','plans','contests','rankings'
# hint_dismiss:{hint_key} → удаляет попап (registered in main.py)

# notif_utils.py
add_read_btn(existing_markup=None) → InlineKeyboardMarkup
# Добавляет «✅ Прочитано» (callback notif_read) ко всем push-уведомлениям

# pagination_utils.py
PAGE_SIZE_DEFAULT=8, PAGE_SIZE_USERS=10, PAGE_SIZE_ORGS=8, PAGE_SIZE_SALES=8
paginate(items, page=0, per_page) → (page_items, total_pages)
page_nav_row(page, total_pages, prefix) → list[InlineKeyboardButton]
```

### main.py — APScheduler (9 задач)

| ID задачи | Расписание | Назначение |
|---|---|---|
| `send_sales_alerts` | каждую минуту (сек 0) | уведомления о дневных целях продаж |
| `send_payment_alerts` | каждую минуту (сек 12) | напоминания об окончании подписки + trial reminders 14/7/3/1d |
| `send_daily_reports` | каждую минуту (сек 24) | ежедневные отчёты |
| `send_personalized_notifications` | каждую минуту (сек 36) | персонализированные уведомления |
| `check_scheduled_notifications` | каждую минуту (сек 48) | запланированные рассылки (UTC) |
| `send_trial_expired_upsell` | каждый час в :05 | upsell-пуш при истечении триала; dedup threshold=-1 |
| `auto_finish_contests` | каждый час в :00 | автозавершение конкурсов |
| `auto_reject_stale_payments` | 10:15 ежедневно | авто-отклонение pending СБП-заявок >72ч |
| `backup_job` | 03:00 ежедневно | авто-бэкап всех БД (retention 30 дней) |

**APScheduler config:** `misfire_grace_time=60`, `coalesce=True`, `max_instances=1` — никакого параллельного запуска, пропущенные таски схлопываются.

**Timezone в APScheduler:** все задачи используют `datetime.now()` (UTC на Amvera), конвертируют через `.astimezone(user_tz)` для сравнения с настроенным временем.

**Scheduled notifications:** admin вводит время → `get_utc_time(naive, admin_tz)` → хранится UTC → `check_scheduled_notifications` сравнивает `datetime.now().isoformat()` (UTC) с UTC → корректно.

---

## 6. ИСТОРИЯ СЕССИЙ

**Сессии 1–10:** базовая архитектура, multi-tenancy, роли, FSM flows, меню.

**Сессии 11–15:** PickleStorage, кеш путей БД, bulk import, полный audit callback.answer() (58 хендлеров).

**Сессии 16–23:** callback.answer(); org-admin баги; рейтинги (date() vs ISO); escape_md().

**Сессии 24–25:** мотивационные условия (сверхплан, коэф.); планы продаж; зарплата и смены.

**Сессия 199:** cross-org broadcast баг; check_notifications_permission получал DB-id вместо telegram_id; JOIN sn.created_by = u.id; добавлена задача check_scheduled_notifications.

**Сессия 199:** итоговый аудит. HTML-инъекция устранена в 44 местах / 9 файлах (he()). 0 bare except, 0 print().

**Сессия 199:** аудит сессий 24–25. Критический баг salary_handlers (fsm_edit вместо edit_text). he() в commission_handlers и sales_plans_handlers.

**Сессия 199:** `is_any_admin()` переписан — роль в user_org_mapping приоритетнее ADMIN_CHAT_ID. role='user' всегда False.

**Сессия 199:** конкурсы (полный жизненный цикл); дашборд перенесён в Отчёты; инвайт для орг-admin.

**Сессии 31–32:** multi-select категорий в plan wizard; редактирование планов; дашборд пользователя (все планы, призы конкурсов).

**Сессия 199:**
1. Anchor message fix — `clear_state_keep_org` ПОСЛЕ `fsm_edit`.
2. Edit plan: все поля (Получатель/Период/Метрика/Фильтр). Callback prefixes: `epwho_*`, `epperiod_*`, `epmetric_*`, `epfilter_*`.
3. Admin dashboard: все планы через `_plan_summary_line()`.
4. Баг `plans_progress` NameError исправлен.
5. `create_tables()` теперь вызывается для супер-адмна в `get_db()`.
6. `deploy.sh exclusions` — исключены `.db`, `.pkl`, `data/tenants/` из GitHub.

**Сессия 199:**
1. Профиль пользователя — роль вверху, дата в DD.MM.YYYY, he() на всех полях.
2. Очистка архива конкурсов с подтверждением.
3. `is_any_admin()` приоритет: org-роль > env_manager.
4. Admin dashboard: `_on_shift_details()` + `_today_total_earnings()`.
5. Reports: «По городу» внутри «За период»; сотрудник — «Мои продажи» + «Текущий месяц».
6. Rankings: 8 колонок в `get_sales_ranking()`; `_ranking_period()`; `_period_kb()`; позиция вне топ-10.
7. `deploy.sh` — `--no-amvera` флаг; верификация через `git ls-remote`.

**Сессия 199:**
1. he() аудит: contacts_handlers, payment_admin_handlers, main.py, plan_notifications, payment_system_admin.
2. `clear_state_keep_org(state, extra_keys=[...])` — сохранение доп. ключей FSM.
3. Excel download → FSM (избавился от длинных callback_data).
4. `safe_cb()` / `resolve_cb_name()` в handlers.py и admin_handlers.py.
5. **Multi-scope**: scope_value = JSON array; toggle UI `adm_t_s/c/n_*` + `adm_scope_submit`.
6. **Custom title**: custom_title в user_org_mapping; `set_user_title()`; 🏷️ в карточке пользователя.

**Сессия 199:**
1. **QuickSaleStates** (`searching_product = State()`).
2. **`_make_qty_keyboard(max_qty)`** — кнопки [1,2,3,5,10,20,50] + «✏️ Ввести вручную».
3. **Быстрый поиск** — «🔍 Найти товар» в экране категорий; поиск по ненулевому остатку.
4. **`sq_qty_*`** — выбор количества без ввода текста.

**Сессия 199:**
1. **`shift_templates`** таблица — UNIQUE(user_id, weekday), weekday 0=Пн..6=Вс.
2. **`work_schedule`** — добавлены колонки `start_time`, `end_time`; миграция авто.
3. **6 новых методов database.py**: `set_shift_template`, `get_shift_templates`, `get_work_day_time`, `add_work_day`, `remove_work_day`, `set_work_day_time`.
4. **salary_handlers.py** (~796 строк): пикер часов `_hour_picker_kb()`; цепочки slr_te/slr_tw; при slr_tog → применяет шаблон дня недели.
5. **⏰ Расписание смен** — кнопка `slr_tmpl_` в календаре; экран просмотра/редактирования 7 дней.
6. **my_day_detail** — сотрудник: время из work_schedule или шаблона (fallback).

**Сессия 199:**
1. **«📝 Мои продажи»** восстановлена в `main_menu()` для обычных продавцов (keyboards.py).
2. **«query is too old»** TelegramBadRequest → DEBUG (не засоряет логи).
3. **Timezone earnings**: время продажи в «Моём заработке» → `format_user_datetime(sale_date, user_tz, '%H:%M')`.
4. **now_str**: дашборд и отчёты → `get_current_user_time(user_tz).strftime(...)`.
5. **Scheduled notifications UTC fix**: ввод → `get_utc_time(naive, admin_tz)` → хранить; список → `format_user_datetime(raw_utc, admin_tz)`.
6. **Аудит итог**: 34/34 модулей · 279/279 тестов · 0 кириллицы в callback_data.

**Сессия 199:**
1. **«Команда сегодня»** дашборд: `_staff_by_shop_with_names()` — для 'wide'/'org' scale показывает «ТЦ Лето: 3 (Иванов, Петров, Сидоров)».
2. **Reports Markdown→HTML**: `report_full`, `report_shop_generate`, `report_city_generate` — HTML + `he()`.
3. **Perf: 7 индексов в `create_tables()`**: idx_sales_user_date, idx_sales_shop_date, idx_inventory_shop_prod, idx_work_schedule_date, idx_seller_earnings_sale, idx_users_shop_name, idx_users_telegram_id — все `CREATE INDEX IF NOT EXISTS`.
4. **`busy_timeout=10000`** в `create_tables()` (ранее только в `get_connection()`).
5. **«message is not modified»** → тихий `answer()` без логирования.

**Сессия 199 (2026-05-07):**
1. **`earnings_handlers.py` — Markdown→HTML** (10 мест): все `parse_mode="Markdown"` → HTML.
2. **`earnings_handlers.py` — Русские названия месяцев**: `_MONTHS_RU[]` вместо `calendar.month_name[]`.
3. **`salary_handlers.py` — Markdown→HTML** (24 места): `escape_md()` → `he()`.
4. **`keyboards.py` — утечка соединения SQLite** в `main_menu()`: добавлен `try/finally` вокруг `conn.close()`.

**Сессия 199 (2026-05-07) — ЮKASSA:**
1. **`payment_provider.py`** (новый): `get_active_provider(db)`, `create_yookassa_payment(...)`, `check_yookassa_payment_status(...)`.
2. **`database.py`** — таблица `yookassa_payments`; 7 методов: `get/set_payment_provider`, `get/set_yookassa_config`, `create/get/update_yookassa_payment_*`.
3. **`payment_system_admin.py`** — «🔀 Провайдер оплаты»; экраны выбора/настройки ЮKassa.
4. **`subscription_handlers.py`** — маршрутизация по провайдеру: СБП = скриншот; ЮKassa = URL-оплата + «✅ Я оплатил — проверить» → auto-confirm.
5. **`states.py`** — `PaymentSystemStates`: waiting_yookassa_shop_id/secret_key/return_url.
6. **559 тестов ✅** (добавлены новые сценарии).

**Сессия 199 (2026-05-07) — EXCEL ОТЧЁТЫ:**
1. **`generate_excel_report()`** в `utils.py` переписан — 4 листа: «Детальный отчёт» (с колонкой «Продавец», числовые форматы, ИТОГО), «По категориям» (BarChart), «По продавцам» (если есть данные), «По дням» (если >1 день, BarChart). Определение продавца: `len(row) >= 11`.
2. **6 Excel-хендлеров** в `reports_handlers.py` обновлены: scope-фильтр, лимит 50000, `_translit_filename()`, `⏳ Формирую файл...`, подписи с числом строк.
3. **`_translit_filename(text)`** — хелпер транслитерации для имён .xlsx файлов.
4. GitHub `7a7f1a3` · Amvera `e5913d1`. 559 тестов ✅.

**Сессия 199 (2026-05-11) — Конкурсы: индивидуальные пороги (#3) + per_sale тиры (#4):**
1. **database.py**: `contests` +`reward_mode`/`individual_targets` (ALTER TABLE migration); новая таблица `contest_product_bonuses`; новые методы `save_contest_product_bonuses`, `get_contest_product_bonuses`, `get_user_plan_pct_for_contest`; `create_contest`/`update_contest` расширены; `compute_contest_results` разделён на total (учитывает `individual_targets.by_shop`) и per_sale (тиры × qty × plan_pct); `get_user_contest_rewards` читает `reward_mode`.
2. **contests_handlers.py**: новые FSM-состояния `configuring_tier_bonus`, `entering_individual_target`; новые шаги: выбор режима (`ctrm_`), wizard тиров per_sale (`cttc_`/`ctpct_`/bonus-ввод), индивидуальные пороги по магазинам (`ctind_yes/no`, `ctindval_skip`); `_show_contest_confirm` показывает тиры/инд.пороги; `contest_confirm_create` сохраняет `reward_mode`, `individual_targets`, тиры через `save_contest_product_bonuses`; `contest_view` отображает тиры/инд.пороги; `contest_results` ветвится по `reward_mode` (per_sale: бонус, plan_pct; total: победители + инд.пороги); `contest_notify_winners` — разные тексты для per_sale/total.
3. **dashboard_handlers.py**: `_contest_block` — per_sale конкурсы показывают накопленный бонус+qty; total конкурсы — прогресс по индивидуальному порогу (`individual_target` из результатов).
4. **main.py**: `auto_finish_contests` — per_sale уведомления (qty+бонус+plan_pct); total уведомления без изменений.
5. **Индексы `contests` таблицы**: `reward_mode` = col 22, `individual_targets` = col 23 (после `created_at`=21).

**Сессия 199 (2026-05-13) — БАГФИКСЫ + СОВМЕСТНАЯ МОТИВАЦИЯ:**
1. **Баг: `back_to_edit_sale` KeyError** — `data['sale_id']` → `data.get('sale_id')` с fallback к кешу продаж или `edit_sales_start` (`sales_handlers.py`).
2. **Баг: FakeCallback в тирах конкурса** — `_safe_tier_edit(callback, state, text, markup)` в `contests_handlers.py`: сначала пробует `callback.message.edit_text()`, при ошибке ищет `anchor_msg_id` из FSM и редактирует якорное сообщение. Применён в `_show_tier_bonus_step`, `_show_tier_pct_step`, `_show_period_step`.
3. **Баг: кнопка «Сменить магазин» не появлялась** для орг-пользователей без `trade_network` — `_build_cross_shop_screen` переписан: если `trade_network` пустой → использует `get_all_shops()` вместо `get_shops_by_network()`. `sale_select_network_shop` аналогично. Кнопка `allow_change=True` выставляется при 2+ магазинах в орге (`sales_handlers.py`).
4. **Фича: Совместный режим мотивации** (`commission_handlers.py`, `database.py`):
   - `motivation_extra_conditions` + колонка `calc_mode TEXT DEFAULT 'individual'`; автомиграция.
   - `ExtraConditionStates.selecting_calc_mode` — новый шаг 3/4 между min_sellers и coefficient.
   - Обработчик `coeff_calc_mode_selected` — кнопки «👤 Раздельный» / «🤝 Совместный».
   - `add_extra_condition(calc_mode=...)` — новый параметр.
   - `get_extra_conditions()` — SELECT добавлен `COALESCE(mec.calc_mode,'individual') as calc_mode` (индекс [12]).
   - `get_joint_bonus_adjustment(user_id, start_date, end_date)` — пул = SUM(комиссий всех продавцов в совместных магазинах). Математика: `joint_bonus = SUM(individual*coeff all sellers) = total_qty × motiv × coeff` — каждый получает одинаково.
   - `get_seller_total_earnings` — добавляет корректировку: `base + get_joint_bonus_adjustment(...)`.
   - `view_extra_conditions` — иконка режима 👤/🤝 рядом с каждым условием.

**Сессия 199 (2026-05-18) — БАГИ ПРОДАЖ + GOOGLE SHEETS OAUTH:**

**Корневые причины бага «продажи не сохраняются»:**
1. **`callback.answer()` без try/except** в `complete_sale` (до нашего фикса 7cbbd63): если Telegram отвечал «query is too old», outer except перехватывал исключение и удалял все `processed_sales` через `delete_sale`. Исправлено: `callback.answer()` и `get_user()` обёрнуты в try/except.
2. **`logger` не объявлен в `sales_handlers.py`**: при добавлении дебаг-логов использовали `logger.error()`, тогда как в файле объявлен только `import logging`. `NameError: name 'logger' is not defined` бросался ДО outer try, падал в aiogram error middleware. Исправлено: добавлен `import logging` в начало файла, все вызовы → `logging.error()`.
3. **Дополнительно исправлено** в той же сессии (коммит 7cbbd63): `_per_shop_breakdown` в `dashboard_handlers.py` использовал несуществующие колонки `s.quantity`/`s.total_price` → пустая сводка; `get_contest_manual_results` — ambiguous `shop_name` в JOIN.

**Диагностический метод:** временное `logging.error("[DEBUG complete_sale] ...")` в каждой ключевой точке хендлера — немедленно обнажило NameError в логах workflow.

**Google Sheets OAuth setup:**
- Тип OAuth клиента в Google Cloud Console: **«TVs and Limited Input devices»** — единственный тип, поддерживающий Device Flow (POST на `https://oauth2.googleapis.com/device/code`).
- Скачанный JSON имеет ключ `"installed"` — это нормально для этого типа.
- `client_id` и `client_secret` → Replit Secrets `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.
- `integration/auth/google_oauth.py`: `get_client_credentials()` читает из `os.environ`; `initiate_device_flow()` → user_code + verification_url; `poll_for_token()` — long-polling до подтверждения.
- GitHub `f7a76f8` · Amvera `7057da5`.

**Сессия 199 (2026-05-18) — ЗАДАЧА #9: ПОИСК ПО @USERNAME В СПИСКЕ ПОЛЬЗОВАТЕЛЕЙ + POST-MERGE SETUP:**
1. **`database.py`**: добавлена колонка `username TEXT` в `users` (CREATE TABLE + авто-миграция `ALTER TABLE`); `add_user()` и `update_user()` принимают `username=`.
2. **`admin_handlers.py`**: `_ADMIN_USERS_COLS` расширен на `username` (13-я колонка, индекс 12); поиск в `_build_admin_users_content` теперь включает `@username` в строку сравнения (с `lstrip('@')` для толерантности к вводу); кнопка в списке показывает `@username` если есть, иначе `(магазин)`.
3. **Все call-сайты `add_user`**: `handlers.py` (5 мест: super-admin, select_city callback, process_city message, shop_bot.db trial copy), `sales_handlers.py`, `reports_handlers.py`, `notifications_handlers.py`, `subscription_handlers.py`, `db_utils.py` (оба get_db и get_db_sync) — везде передаётся `username` (с guard `len > 12` для кортежей).
4. **`scripts/post-merge.sh`**: создан (`pip install -r requirements.txt`); зарегистрирован в `.replit [postMerge]` с таймаутом 60s — теперь при каждом мерже задачи-агента автоматически устанавливаются зависимости.
5. GitHub `5c7104b` · Amvera `bcd4ae2`.

**Сессия 199–72 (2026-05-18) — ЗАДАЧА #5: ПОИСК В ИЗБРАННОМ И НЕДАВНИХ (sale flow):**
1. **`states.py`**: добавлены `SearchStates.sale_favourites` и `SearchStates.sale_recent`.
2. **`sales_handlers.py`**: добавлены билдеры `_build_fav_list_content(fav_prods, cart, query="")` и `_build_recent_list_content(recent, cart, query="")` — возвращают `(text, markup)`, фильтрация по имени, кнопки 🔍/✖️/🛒/⬅️.
3. **Рефакторинг** `sale_show_favorites` и `sale_show_recent_handler`: сохраняют `anchor_msg_id` + `sale_srch_list_type` ('fav'/'recent') в FSM; используют новые билдеры.
4. **5 новых хендлеров**: `slr_fav_srch_start` / `slr_rec_srch_start` (запуск поиска), `slr_srch_cancel` (сброс, восстанавливает `MultipleSaleStates.adding_items`), `slr_fav_srch_process` / `slr_rec_srch_process` (обработка текста, `fsm_edit` + билдер).
5. GitHub `abb429c` · Amvera `a9b5b3f`.

**Сессия 199 (2026-05-18) — ЗАДАЧА #2 (смержена агентом): ПОМЕСЯЧНАЯ МОТИВАЦИЯ И АРХИВ:**
- Таблицы `motivation_schedule` (UNIQUE product_id+year+month) и `extra_conditions_schedule`.
- `get_effective_motivation_matrix(col_months)` — матрица всех товаров с мотивацией по месяцам.
- `set_motivation_for_month` / `get_motivation_for_month` (fallback на global).
- `set_extra_condition_for_month` / `get_extra_conditions_for_month`.
- `commission_handlers.py`: кнопка «📅 По месяцам», хендлеры выбора месяца, матрица (7 столбцов: 6 прошлых + следующий), ячейки `sched_cell_*`, архив `archive_months`.
- ИСПРАВЛЕН баг: `recalculate_month_earnings` теперь принимает конкретный year/month.
- GitHub (task agent) `5c7104b` (после мержа).

**Сессия 199 (2026-05-18) — ЗАДАЧА #1: ДИНАМИЧЕСКИЙ ПОИСК ВО ВСЕХ БОЛЬШИХ СПИСКАХ:**
1. **`states.py`**: добавлена группа `SearchStates` с 12 состояниями: `shop_commission`, `user_catfilt`, `shop_plans`, `user_plans`, `product_plans`, `category_plans`, `product_contests`, `category_contests`, `shop_reports`, `shop_inventory`, `product_inventory`, `shop_contacts`.
2. **`commission_handlers.py`**: поиск магазина (`coeff_srch_shop_*`, `SearchStates.shop_commission`) + поиск сотрудника в catfilt (`catfilt_srch_*`, `SearchStates.user_catfilt`).
3. **`reports_handlers.py`**: поиск магазина для периодического отчёта (`rep_srch_shop_*`, `SearchStates.shop_reports`).
4. **`contacts_handlers.py`**: поиск магазина (`contacts_srch_shop_*`, `SearchStates.shop_contacts`).
5. **`sales_plans_handlers.py`**: 4 поиска — магазин (`plnwiz_srch_shop_*`), сотрудник (`plnwiz_srch_user_*`), мультиселект категорий (`plnwiz_srch_cat_*`), мультиселект товаров (`plnwiz_srch_prd_*`). Хелперы `_show_plan_shop_list`, `_show_plan_user_list`. `_render_category_selection`/`_render_product_selection` получили параметр `query` и кнопку 🔍.
6. **`contests_handlers.py`**: поиск товара (`ct_srch_prd_*`, `SearchStates.product_contests`) + поиск категории (`ct_srch_cat_*`, `SearchStates.category_contests`). Хелперы `_show_contest_product_list`, `_show_contest_category_list`.
7. **`inventory_handlers.py`**: поиск магазина (`inv_srch_shop_*`, `SearchStates.shop_inventory`) + поиск товара (`inv_srch_prd_*`, `SearchStates.product_inventory`), хелпер `_show_inv_product_list`. Однoмагазинный путь в `add_inventory_start` тоже использует `_show_inv_product_list`.
8. **Паттерн**: везде используется `fsm_edit` + `anchor_msg_id`. После multiselect-поиска — `await state.set_state(SalesPlansStates.selecting_*)` для восстановления стейта toggle-хендлеров.
9. GitHub `98df9d8` · Amvera `0b13728`.

**Сессия 199 (2026-05-18) — ИСПРАВЛЕНИЕ КОРРЕКТИРОВОК КОНКУРСА:**
1. **Авто-значение с полными фильтрами** — добавлен метод `compute_contest_shop_auto_totals(contest_id)` в `database.py`. Использует тот же `_build_contest_sale_query()` что и `compute_contest_results` — с фильтрами по товарам/категориям/городам/пользователям. Ранее UI показывал упрощённый `SUM(sale_price * qty)` без фильтров конкурса.
2. **per_sale (тиры): корректировки недоступны** — `compute_contest_results` в ветке per_sale никогда не вызывал `get_contest_manual_results()`, корректировки молча игнорировались. Теперь при попытке открыть корректировку для per_sale конкурса — внятное сообщение с объяснением.
3. **Рефакторинг contest_manual_list / contest_manual_set_shop** — оба хендлера заменили самописный SQL на `compute_contest_shop_auto_totals()`.
4. GitHub `c2393e0` · Amvera `8350ba2`.

**Сессия 199 (2026-05-18) — КОРРЕКТИРОВКА ПОКАЗАТЕЛЕЙ КОНКУРСА В ЛЮБОЙ МОМЕНТ:**
1. **Переименование UX**: «✏️ Ручной ввод результатов» → «✏️ Скорректировать показатели».
2. **Кнопка добавлена в экран результатов** — доступна всегда, не только из карточки конкурса.
3. **contest_manual_list**: показывает авто-значение каждого магазина (🏪) или корректировку (✏️) с автором и датой.
4. **contest_manual_set_shop**: отображает «Авто (по продажам конкурса)» + текущую корректировку с историей.
5. **Удаление сообщений пользователя**: `await message.delete()` + try/except во всех 8 обработчиках текстового ввода конкурсов.
6. **Рефакторинг `_FakeCallback`**: 3 дублирующихся класса → 1 общий экземпляр в `contest_tier_bonus_entered`.
7. GitHub `3f1cf87` · Amvera `8350ba2`.

**Сессии 119–124 (2026-05-19) — МОНЕТИЗАЦИЯ: ПОЛНЫЙ ЦИКЛ:**

**Сессия 199 — `can_use_integrations` в тарифах:**
1. Добавлена колонка `can_use_integrations` в `subscription_plans` (миграция ALTER TABLE).
2. Платные планы: Базовый/Стандарт/Премиум → `can_use_integrations=1`; Бесплатный → 0.
3. `subscription_utils.py`: `check_integrations_permission(tg_id)` проверяет флаг.
4. `integration_handlers.py`: `integration_menu` закрыт через `check_integrations_permission`.
5. Все SELECT `subscription_plans` обновлены (7-й столбец = `can_use_integrations`).

**Сессия 199 — Триал = безлимит через `_has_active_trial()`:**
1. `subscription_utils.py`: `_has_active_trial(tg_id)` → True если `is_trial=1 AND end_date > now`.
2. `get_plan_limits()`: триал → немедленный return `_UNLIMITED` (минуя lookup плана 'Бизнес').
3. Устранена путаница: план триала 'Бизнес' не существует в `subscription_plans`, но `_has_active_trial()` перехватывает раньше.

**Сессия 199 — Trial upsell flow:**
1. `main.py`: `send_trial_expired_upsell(bot)` — ежечасно в :05; dedup через `subscription_reminder_log` (threshold=-1).
2. `database.py`: `get_recently_expired_trials()` — триалы истёкшие за последние 48ч.
3. `main.py`: `_TRIAL_FEATURES_LOST` — список потерянных функций; `_sub_markup()` — InlineKeyboardMarkup с «💳 Выбрать тариф» + «✅ Прочитано».
4. `send_payment_alerts`: расширены trial-specific reminders за 14/7/3/1d (список фич + кнопка).
5. GitHub `85476dc` · Amvera `ad56380`.

**Сессия 199 — Дифференциация тарифов + авто-отклонение СБП:**
1. **Тарифная сетка**: Базовый → `can_use_integrations=0` (Google Таблицы только Стандарт+).
2. `database.py`: always-running migration: `UPDATE subscription_plans SET can_use_integrations=0 WHERE name='Базовый'`.
3. **trial_plan 'Бизнес' → 'Премиум'**: `database.py` default_settings, `handlers.py` (2 места), `payment_system_admin.py` (2 места); always-running migration обновляет существующие БД.
4. `subscription_handlers.py`: добавлена строка «• Google Таблицы: ✅/❌» в статус подписки.
5. `subscription_utils.py`: `get_subscription_warning_message()` показывает сравнение Базовый/Стандарт/Премиум + для Базового без интеграций — конкретное сообщение.
6. `database.py`: `get_stale_pending_payments(hours=72)` → rows с telegram_id.
7. `main.py`: `auto_reject_stale_payments(bot)` — ежедневно 10:15; отклоняет pending СБП >72ч + уведомление юзеру через `_sub_markup()`.
8. GitHub `d3b2529` · Amvera `288b234`.

**Сессия 199 — Upsell-кнопка в интеграциях + квитанция при активации:**
1. `integration_handlers.py`: при `check_integrations_permission() = False` — редактирует сообщение с таблицей тарифов и кнопкой «💳 Выбрать тариф» (вместо `show_alert=True` без кнопок).
2. `payment_admin_handlers.py`: `confirm_payment_request` — пользователь получает полную квитанцию (тариф + сумма + дата истечения), данные тянутся из `subscription_plans`.
3. GitHub `3a9650d` · Amvera `46d94fa`.

**Сессия 199–132 — PERFORMANCE AUDIT: answer() + DB indexes + ⏳ indicators:**
1. **10 новых индексов** в `database.py` (create_tables): idx_sales_date, idx_users_city, idx_users_trade_network, idx_products_category, idx_subscriptions_user, idx_sales_plans_user, idx_notif_history_user, idx_sched_notif_dt, idx_plan_milestones — ускоряют выборки по дате/городу/пользователю.
2. **`answer("⏳ Загрузка...")` добавлен** в `_render_dashboard`, `reports_menu`, `view_ratings`, `report_today`, `report_my_shop`, `report_user_month`, `report_admin_month`, `my_plans` — тяжёлые экраны с несколькими DB-запросами.
3. **`answer()` перенесён перед DB** в 13 обработчиках: `my_schedule`, `my_schedule_nav` (salary_handlers), `my_plans`, `plnwiz_toggle_category`, `plnwiz_products_page` (sales_plans_handlers), `contest_toggle_category`, `ct_srch_shop_cancel`, `contest_toggle_shop` (contests_handlers), `_render_product_list`, `edit_product_choice`, `confirm_delete_product` (products_handlers), `user_inventory_menu` (inventory_handlers), `catfilt_toggle_category`, `catfilt_allow_all` (commission_handlers).
4. **`_show_schedule_matrix` (commission_handlers)**: перенесён pattern — answer() добавлен в `view_motivation_schedule` и `sched_page` ДО вызова хелпера; из самого хелпера `answer()` убран (дублирование).
5. **`edit_product_choice` / `confirm_delete_product`**: ошибка «не найден» изменена на `show_alert=True` (было без alert — плохой UX).
6. Принцип: `answer()` — первая строка если нет show_alert-валидации; сразу после последней валидации если есть; toggle-хендлеры — всегда первой строкой.
7. GitHub `b3c41f4` · Amvera `d04df11`. 45 импортов ✅.

**Сессия 199 — ВЕРИФИКАЦИЯ + GOOGLE SHEETS ДОКУМЕНТАЦИЯ:**
1. Верификация: все изменения за 2 дня присутствуют в коде (syntax check всех .py ✅, test_imports ✅).
2. git статус: local main = `48a148a` (Replit checkpoint); origin/main = `fb845fd` (stale tracking — нормально, deploy.sh пушит из /tmp/github-deploy отдельно). Расхождение 94 vs 50 коммитов — ожидаемо, не баг.
3. Документирован скоуп Google Sheets интеграции (см. ловушку #30 ниже).
4. Нет нового кода — только документация и верификация.

**Сессии 135–143 (2026-05-19) — PERFORMANCE OPTIMIZATIONS (3 шага):**

**Шаг 1 — PRAGMA WAL (database.py `get_connection()` + `create_tables()`):**
- `PRAGMA journal_mode=WAL` — параллельные читатели без блокировок
- `PRAGMA synchronous=NORMAL` — баланс надёжность/скорость
- `PRAGMA cache_size=-8000` — 8 MB page cache на соединение
- `PRAGMA temp_store=MEMORY` — временные таблицы в памяти
- `PRAGMA mmap_size=134217728` — 128 MB mmap для read-heavy путей
- `busy_timeout=10000` — 10 с ожидания при блокировке (без SQLITE_BUSY краша)
- GitHub `4a7f5ec` · Amvera `...`

**Шаг 2 — AsyncDatabase wrapper (db_utils.py):**
- Класс `AsyncDatabase` с `__getattr__` → `asyncio.to_thread(fn, *args, **kwargs)`
- `wrap_db(db)` — явная обёртка для inline `Database()`
- `get_db()` стал `async`, возвращает `AsyncDatabase`
- **577 `await`** добавлено во все handler-файлы (все вызовы `current_db.*`)
- 5 sync-хелперов переписаны в async: `build_admin_dashboard`, `build_user_dashboard`, `_build_sale_product_list_content`, etc.
- `hints.py` / `maybe_refresh_username` — sync-совместимость через `getattr(db, '_db', db)`
- Inline `Database()` в `admin_handlers`, `handlers`, `inventory_handlers`, `notifications_handlers` — обёрнуты через `wrap_db()`
- `main.py` `send_daily_reports` — полная async-конвертация
- 45/45 test_imports ✅
- GitHub `106222c` · Amvera `7c407f8`

**Шаг 3 — Thread-local connection pool + asyncio.gather() (database.py + dashboard_handlers.py):**
- `_conn_pool = threading.local()` в `database.py`
- `_get_pooled_conn(db_file)` — создаёт соединение с `check_same_thread=False` + все PRAGMA; переиспользует в том же потоке
- `_PooledConn` — обёртка, `close()` = только `rollback` (не разрывает соединение)
- `get_connection()` возвращает `_PooledConn`
- **187 вхождений** `sqlite3.connect(self.db_file...)` в 200+ методах заменены на `self.get_connection()`
- `build_admin_dashboard`: 7 запросов → `asyncio.gather(*_base_tasks, *_salary_tasks, return_exceptions=True)` — все параллельно (~80ms → ~10ms)
- `build_user_dashboard`: 8 запросов → `asyncio.gather(...)` — все параллельно (~80ms → ~10ms)
- 45/45 test_imports ✅ · runtime pool tests ✅ · runtime AsyncDatabase tests ✅
- GitHub `0ffc019` · Amvera `7355f9a`

**Сессия 199 (2026-05-07) — TOP-3 FIX + АУДИТ:**
1. **`report_full`** — убраны `[:3]` у категорий и магазинов; теперь все с guard `> 3500` / `> 3700`.
2. **`report_my_shop`** — убран `[:3]` у товаров; guard `> 3700` с сообщением «остальные товары в Excel».
3. **`generate_period_report`** — убран `[:3]` у товаров; аналогичный guard.
4. **Комплексный аудит** — 36 модулей, 559 тестов, 0 bare except, 0 print(), 0 hardcoded secrets, WAL+busy_timeout, APScheduler misfire/coalesce/max_instances=1, все соединения закрываются.
5. **Нераздражающий techdebt**: 3 строки `BROADCAST DEBUG` в `notifications_handlers.py` (logging.info) — не баги, но стоит убрать перед масштабированием.
6. GitHub `077ce67` · Amvera `790cabe`. 559 тестов ✅.

**Сессия 228 (2026-05-27) — РЕФЕРАЛЫ + НАДСТРОЙКИ + PDF + ФОТО/ОПИСАНИЕ ТОВАРОВ:**

**D) Реферальная программа** (`referral_handlers.py`, `database.py`):
- Новая таблица `referrals` в `shop_bot.db`: `referrer_id`, `referred_id`, `bonus_applied`.
- Deep-link: `/start ref_TELEGRAMID` → парсится в `handlers.py` (`select_city` callback + `process_city` message handler); сохраняется как `REF_{id.upper()}` в FSM.
- Бонус начисляется через `apply_referral_bonus(referrer_id)` → `_extend_subscription_by_days(tg_id, 30)` при создании организации приглашённым.
- Новые методы DB: `create_referral(referrer_id, referred_id)`, `apply_referral_bonus(referrer_id)`, `_extend_subscription_by_days(tg_id, days)`, `get_referral_stats(tg_id)` → (total, applied, bonus_days).
- Экран: `subscription_referral` callback → `referral_router`; ссылка через `get_me()` с fallback.

**E) Надстройки к подписке** (`addon_handlers.py`, `database.py`):
- Новая таблица `subscription_addons` в `shop_bot.db`: `addon_type`, `quantity`, `expires_at`.
- Типы: `extra_shops` (150₽/30д, +1 магазин), `extra_products` (100₽/30д, +100 товаров).
- `addon_router`: `subscription_addons` → меню; `addon_buy_shops_1` / `addon_buy_products_1` → экран покупки; оплата через стандартный `confirm_payment_request` с plan_type `addon_shops_1` / `addon_products_1` (prefix `addon_`).
- Новые методы DB: `create_subscription_addon(user_id, addon_type, qty, days)`, `get_active_addons(user_id)`, `get_addon_totals(user_id)` → dict {'extra_shops': N, 'extra_products': N}.
- `subscription_utils.py`: `get_plan_limits()` суммирует addon-значения с лимитами тарифа.
- `confirm_payment_request` (payment_admin_handlers.py): plan_type начинающийся с `addon_` → вызывает `create_subscription_addon` вместо `create_subscription`.

**F) PDF экспорт** (`pdf_utils.py`, `reports_handlers.py`):
- `generate_pdf_report(org_name, start_date, end_date, sales_rows, summary, top_sellers, top_products)` → path|None — строит A4 PDF с шапкой, таблицей продаж, топами.
- `generate_pdf_for_report(db, scope, start_date, end_date, scope_value)` → path|None — фасад: делает SQL-запросы, собирает данные, вызывает `generate_pdf_report`.
- Требует `reportlab` (в requirements.txt). Кириллица через системные шрифты с fallback на Helvetica.
- Кнопки `download_pdf_full`, `download_pdf_period`, `download_pdf_shop`, `download_pdf_user` в `reports_handlers.py`.

**G) Фото и описание товаров** (`products_handlers.py`, `database.py`):
- Таблица `products`: новые колонки `photo_file_id TEXT` и `description TEXT` — добавлены `ALTER TABLE IF NOT EXISTS` миграцией в `create_tables()`.
- `add_product` / `update_product` принимают `photo_file_id=None` и `description=None`.
- В карточке товара показывается фото (если есть) + описание. При продаже `sale_product_{id}` — также отображается фото/описание.
- `products_handlers.py`: `ProductStates` расширены `entering_photo` + `entering_description`; кнопка «📷 Фото» и «📝 Описание» в меню редактирования.

**Деплой:** GitHub `91ef931` · Amvera `ef1edd7` · 48/48 test_imports ✅

---

**Сессия 200 (2026-05-26) — ФИЛЬТРЫ УВЕДОМЛЕНИЙ + GOOGLE SHEETS FIXES + ПЕРЕНОС ЭКСПОРТОВ:**

**1. Фильтры получателей уведомлений** (`notifications_handlers.py`, `main.py`):
- После ввода текста → экран «👥 Выберите получателей»: Всем / По магазину / По роли.
- FSM-ключи: `ntf_rcpt_type` ('all'/'shop'/'role'), `ntf_rcpt_filter` (имя магазина или роль), `ntf_rcpt_label` (display).
- Превью с числом получателей (`_count_notif_recipients`) перед отправкой.
- Кнопка «↩️ Изменить получателей» для возврата.
- Callbacks: `ntf_rcpt_all`, `ntf_rcpt_shop`, `ntf_rcpt_s_{shop}`, `ntf_rcpt_role`, `ntf_rcpt_r_{role}`, `ntf_change_rcpt`.
- `admin_confirm_send_now`: читает `ntf_rcpt_type`/`ntf_rcpt_filter` из FSM, фильтрует `target_by_db`.
- `process_schedule_time`: сериализует фильтр в `recipients_list` JSON (`{"type":"shop","filter":"ЦУМ"}`).
- `check_scheduled_notifications` (main.py): разбирает `notif[5]` (recipients_list JSON) и применяет фильтр при отправке. Обратная совместимость: NULL → отправка всем.
- Супер-admin видит только «Всем» (кросс-орг фильтрация не реализована).

**2. Google Sheets `invalid_grant` обработка**:
- `integration/auth/google_oauth.py`: класс `OAuthTokenRevokedException`; `refresh_access_token` детектирует `error_code == 'invalid_grant'` и поднимает его вместо сырого `ValueError`.
- `integration/manager.py`: `_ensure_valid_token` перехватывает `OAuthTokenRevokedException` → `db.update_integration_connection(conn_id, enabled=0)` + лог → `ValueError` с дружелюбным текстом. Новый хелпер `_normalize_gs_error(e, db, conn_id)`: конвертирует двух-аргументный `google.auth.RefreshError` (формат `('invalid_grant:...', {...})`) → чистый `ValueError`; вызывается в `_run_export` и `_run_export_with_result`.
- `_run_export` при отозванном токене: `_notify_admins` получает «🔑 Google Sheets: требуется переподключение» вместо сырого tuple-текста.
- `sales_handlers.py`: отозванный токен → «🔑 Требуется переподключение → Управление орг. → Интеграции».
- `integration_handlers.py` sync_motivation: детект `invalid_grant` или 'Переподключите' → инструкция пользователю.

**3. Перенос экспортов между подключениями** (`integration_handlers.py`, `database.py`):
- Кнопка «📤 Перенести экспорты» на экране подключения (только если `len(exports) > 0`).
- `gs_move_exports_{from_id}` → список других подключений (с иконкой ✅/❌).
- `gs_move_exp_to_{from_id}_{to_id}` → экран подтверждения с превью экспортов (до 8 строк).
- `gs_move_exp_ok_{from_id}_{to_id}` → `db.move_exports_to_connection(from_id, to_id)` → отчёт.
- `database.py`: `move_exports_to_connection(from_conn_id, to_conn_id)` — `UPDATE integration_exports SET connection_id=? WHERE connection_id=?`, возвращает число перенесённых.
- Все псевдонимы, mapping, lookup, schedule сохраняются без изменений.
- GitHub `0983dfd` · Amvera `42fb73b`. 703 теста ✅, 0 проблем callback-анализатора.

---

**Сессии 234–235 (2026-05-31) — ИСПРАВЛЕНИЕ ВИДИМОСТИ МАГАЗИНА «СИСТЕМНЫЙ»:**
1. `database.py`: добавлен параметр `include_system=False` в `get_all_shops()`, `get_shops_with_stats()`; все UNION-части (users + shops + inventory) фильтруют «Системный»/«System» для обычных пользователей. Супер-администратор передаёт `include_system=True`.
2. `database.py`: `get_inventory_shops()` — аналогично фильтрует «Системный».
3. `admin_handlers.py`: `_get_shops_with_stats()` + все 14 точек вызова обновлены под новый параметр.
4. GitHub `974aa37` · Amvera `c496f12`.

**Сессия 236 (2026-05-31) — ПРЕСЕТ: ИСПРАВЛЕНИЕ ОТСУТСТВИЯ ТЦ БУМ:**
1. `admin_handlers.py`: `invite_preset_role_handler` и `invite_preset_shop_handler` — добавлен третий UNION-блок `inventory` к UNION-запросу. Пресет теперь показывает магазины из users + shops + inventory.
2. GitHub `f86e21d` · Amvera `f86e21d`.

**Сессия 237 (2026-05-31) — АУДИТ + ФИКС filter_utils + LOW-STOCK + asyncio.gather:**
1. **`filter_utils.py` (Bug fix)**: `get_available_filter_values()` — список магазинов объединяет три источника: `users.shop_name`, `shops.name`, `inventory.shop_name` — каждый с отдельным `try/except`. Магазины без сотрудников (типа ТЦ Бум) теперь появляются в панели 🔍 Фильтр в Отчётах / Рейтингах / Прогрессе планов.
2. **`dashboard_handlers.py` (Bug fix)**: `_low_stock_count()` для scope city/network теперь ищет магазины в `users` и в `shops` (с `try/except` fallback). Инвентарные магазины без сотрудников включены в подсчёт.
3. **`commission_handlers.py` (Оптимизация)**: добавлен `import asyncio`; три хендлера переведены на `asyncio.gather()`: `view_motivations` (motivations+products параллельно), `remove_motivation_start` (то же), `remove_motiv_cat_selected` (categories + motivations + products — все три параллельно).
4. **`sales_plans_handlers.py` (Оптимизация)**: `plnwiz_metric_selected` — `get_all_categories()` + `get_all_products()` параллельно через `asyncio.gather()`.
5. GitHub `cc5507d` · Amvera `f4792e4`. 48/48 test_imports ✅.

---

## 7. ТИПИЧНЫЕ ЛОВУШКИ

1. **`clear_state_keep_org` ПОСЛЕ `fsm_edit`** — не до! Иначе anchor_msg_id теряется.
2. **Инициализировать переменные перед try/except** — если используются снаружи блока.
3. **`state.clear()` запрещён** → только `clear_state_keep_org(state)`.
4. **Пользовательские строки в HTML** → всегда `he(var)`. Тексты кнопок — не нужен.
5. **`get_users_for_notifications()`** → `[0]` = внутренний users.id, `[1]` = telegram_id.
6. **`scheduled_notifications.created_by`** хранит users.id (не telegram_id). JOIN = `ON sn.created_by = u.id`.
7. **Новый роутер** → зарегистрировать в main.py; новый модуль → добавить в test_imports.py.
8. **callback_data + кириллица** → `safe_cb(prefix, value)`, не f-строка.
9. **Планировщик** → `_get_scheduler_db_paths()` для итерации всех тенантов.
10. **plan_notifications.py** — lazy-импорт (внутри функций), не на уровне модуля.
11. **Database.db_file** (не .db_path!) — атрибут пути к файлу.
12. **`get_user_org_scope()`** → `(scope_type, list[str])` НЕ `(str, str)`.
13. **`clear_state_keep_org(state, extra_keys=[...])`** — для сохранения доп. ключей.
14. **InlineKeyboardButton.text** — Telegram НЕ парсит HTML. `he()` не нужен.
15. **`get_sales_ranking()`** → 8 колонок; 8-я = `u.id`; `row[:7]` для старого 7-кол. unpacking.
16. **Время на Amvera — UTC.** `datetime.now()` = UTC. Для отображения — `timezone_utils`.
17. **`shift_templates`**: weekday 0=Пн, 6=Вс. `date.weekday()` — та же нумерация.
18. **slr_te_/slr_tw_**: пикеры идут напрямую к slr_etw_ и slr_day_ (без промежуточного экрана).
19. **Excel лимит 50000 строк**: `get_sales_report(limit=50000)` во всех download-хендлерах.
20. **`generate_excel_report` seller detection**: `len(row) >= 11` = admin report (11 cols); `== 9` = user report.
21. **Telegram лимит сообщения 4096 символов**: guard'ы на уровне магазинов (`> 3600`) и товаров (`> 3700`), сообщение с подсказкой «скачайте Excel».
22. **`sales_handlers.py` НЕ имеет глобального `logger`** — файл использует `import logging` + `logging.error()`. Если добавить `logger.error()` без объявления `logger = logging.getLogger(...)` → `NameError` вне try/except → outer except удалит все `processed_sales`. Всегда использовать `logging.xxx()`.
23. **`complete_sale` outer except удаляет продажи** — любой необработанный exception внутри `try:` блока ПОСЛЕ `add_sale` вызовет `delete_sale(sale_id)` для каждого `processed_sales`. Все сетевые вызовы (answer, edit_text, bot.send_message, интеграции) должны быть обёрнуты в `try/except`. Текущее состояние: get_user(), callback.answer(), edit_text, GS-интеграция, notifications — все защищены.
24. **Google Sheets OAuth**: тип клиента в Google Cloud = **«TVs and Limited Input devices»** (Device Flow). Скачанный JSON будет с ключом `"installed"` — это нормально. `client_id` + `client_secret` → Replit Secrets `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`. Читаются в `integration/auth/google_oauth.py` через `get_client_credentials()` из `os.environ`.
25. **`notification_settings.shift_sale_alerts`** — индекс 11 (после created_at[9], updated_at[10]); добавлен миграцией `ALTER TABLE`. В `get_notification_settings()` читается как `bool(settings[11]) if len(settings) > 11 else True`.
26. **`subscription_plans.can_use_integrations`** — **Базовый=0, Стандарт/Премиум=1**. Always-running migration в `_initialize_default_data()` принудительно держит Базовый=0. Не менять без проверки migration-блока (строка ~861 в database.py).
27. **`trial_plan` = 'Премиум'** (не 'Бизнес' — этого плана нет в БД). Триал проверяется через `_has_active_trial()` → `_UNLIMITED`, не через lookup плана. Fallback в handlers.py и payment_system_admin.py: `settings.get('trial_plan', 'Премиум')`.
28. **`subscription_reminder_log` threshold=-1** — специальный ключ для upsell-сообщения «триал истёк». Остальные ключи: 14, 7, 3, 1 (дней до истечения). `get_recently_expired_trials()` возвращает trials с end_date в последние 48ч.
29. **`auto_reject_stale_payments`** — ежедневно в 10:15; использует `db.get_stale_pending_payments(hours=72)` и `db.reject_payment_request(req_id, admin_id=0)`. admin_id=0 означает авто-отклонение (не конкретный admin).
30. **Google Sheets — скоуп ПО ОРГАНИЗАЦИИ, не по пользователю.** `integration_connections` и `integration_exports` хранятся в `org_*.db` (не personal). Настраивает только admin/owner с тарифом Стандарт+ (`is_any_admin` + `check_integrations_permission`). После настройки ВСЕ продажи/инвентарь ОТ ЛЮБОГО пользователя орга автоматически пишутся в таблицу — через `trigger_export(current_db, 'sales', event_data)` в `complete_sale`. Рядовые пользователи (sellers) не видят меню интеграций и ничего не настраивают — их продажи попадают в GS автоматически. Поле `seller_name` в экспорте = кто сделал продажу.
31. **`get_db()` возвращает `AsyncDatabase`** — все вызовы методов требуют `await`. Без `await` получаешь coroutine, а не данные. В APScheduler-задачах использовать `get_db_sync()` → возвращает `Database` (sync, без await).
32. **`wrap_db(db)` обязателен** для inline `Database('data/shop_bot.db')` в обычных handlers — иначе методы не будут async. Исключение: payment/subscription handlers, где `Database` допустим напрямую (всегда sync контекст).
33. **Thread-local pool**: каждый рабочий поток (`asyncio.to_thread`) получает своё соединение. `_PooledConn.close()` НЕ закрывает соединение — только откатывает незакрытые транзакции. Соединение живёт пока живёт поток. При добавлении нового метода в `Database` — используй `self.get_connection()`, не `sqlite3.connect(self.db_file)`.
34. **`asyncio.gather()` с `return_exceptions=True`**: используй в dashboard и любых экранах с 3+ независимыми DB-запросами. Всегда проверяй каждый результат: `if not isinstance(result, Exception)`. Паттерн: `_r = lambda i, default=None: results[i] if not isinstance(results[i], Exception) else default`.
35. **`filter_utils.py` — магазины без сотрудников**: `get_available_filter_values()` собирает список магазинов из трёх таблиц: `users.shop_name`, `shops.name`, `inventory.shop_name`. Если новый магазин добавлен только в `inventory` (0 сотрудников) — он появится в фильтре. При изменении логики — обязательно поддерживать все три источника. Инвалидация: `invalidate_filter_values_cache(db_path)` при добавлении/удалении/переименовании магазина.
36. **`_low_stock_count()` в dashboard**: при scope city/network магазины ищутся в `users` и `shops` таблицах. Таблица `shops` может не иметь сотрудников, но хранит city/trade_network — без неё inventory-only магазины исчезают из подсчёта низких остатков. Fallback `try/except` если таблица `shops` отсутствует.

---

## 8. ЧЕКЛИСТ ПЕРЕД ДЕПЛОЕМ

```bash
# 1. Импорт-аудит (48 модулей):
python test_imports.py   # должно быть: Итог: 48 ОК, 0 ошибок

# 2. Синтаксис:
python -c "
import ast, os
errors = []
for fn in os.listdir('.'):
    if fn.endswith('.py') and not fn.startswith('test_'):
        try:
            with open(fn) as f: ast.parse(f.read())
        except SyntaxError as e:
            errors.append(f'{fn}: {e}')
print('✅ OK' if not errors else '\n'.join(errors))
"

# 3. Деплой (GitHub + Amvera):
bash deploy.sh "commit message"

# Только GitHub:
bash deploy.sh "commit message" --no-amvera
```
