# AGENT HANDOFF — Daily Sales Telegram Bot
> Последнее обновление: 2026-05-18 (сессия 77)
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

**Последний деплой:** GitHub `fde07f9` · Amvera `166f7e4` (2026-05-18, сессия 77). Оба хэша верифицированы через `git ls-remote`.

**Верификация Amvera:** После каждого пуша `deploy.sh` автоматически проверяет `git ls-remote` и печатает:
`Amvera verify: ✅ remote hash совпадает (hash)` или `⚠️ расхождение!`

---

## 1. АРХИТЕКТУРА ПРОЕКТА

```
Telegram API
    ↓
main.py  — polling, регистрация роутеров, APScheduler (7 задач)
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  19 РОУТЕРОВ (handlers)                                          │
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
└──────────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  СЛОЙ ДАННЫХ                                                     │
│  db_utils.py        — get_db(), is_any_admin() [ГЛАВНЫЙ]        │
│  database.py        — класс Database (163 метода)               │
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
| `subscription_utils.py` | Лимиты подписки, get_plan_limits() |
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

---

## 2. КРИТИЧЕСКИЕ ПРАВИЛА (нарушение → баги)

### 2.1 Доступ к БД — ТОЛЬКО через get_db()

```python
# ✅ ПРАВИЛЬНО — все обычные handlers:
from db_utils import get_db
current_db = await get_db(callback.from_user.id, state)
result = current_db.some_method()

# ✅ ПРАВИЛЬНО — payment/subscription handlers (всегда shop_bot.db):
db = Database('data/shop_bot.db')   # допустимо только в этих файлах

# ❌ НЕПРАВИЛЬНО — глобальный db в начале обычного файла:
db = Database('data/shop_bot.db')  # создаёт утечку изоляции между орг
```

**get_db() логика (db_utils.py):**
```
super-admin + selected_org_db в state → Database(selected_db) + create_tables()
user в org (tenant_manager)            → Database(org_*.db)   + create_tables()
иначе                                  → Database(shop_bot.db) + create_tables()
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

### Amvera persistent data (production)

- Persistent mount: `/app/data`
- Файлы: `main.db`, `shop_bot.db`, `fsm_storage.pkl`, `tenants/org_huawei.db`, `backup/`
- `org_huawei.db` — единственная тенант-БД в production. `create_tables()` авто-применяет миграции.

---

## 4. БАЗЫ ДАННЫХ

### data/main.db — только через tenant_manager

| Таблица | Ключевые колонки |
|---|---|
| `organizations` | id, name, owner_id, invite_code, db_path, subscription_plan, is_active |
| `user_org_mapping` | telegram_id, org_id, role (owner/admin/user), scope_type, scope_value (JSON array), custom_title |

### data/shop_bot.db и data/tenants/org_*.db — идентичная схема

| Таблица | Назначение |
|---|---|
| `users` | telegram_id, first_name, last_name, phone, email, trade_network, shop_name, city, timezone, **username** (индекс 12) |
| `products` | id, name, category, price, motivation_type, motivation_value |
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
| `notification_settings` | low_stock_alerts, daily_reports, sales_alerts, payment_alerts, admin_notifications |
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
| `subscription_plans` | name, price, duration_days, max_products, max_shops, features |
| `promocodes` | code, discount_percent, max_usage, current_usage, is_active |
| `yookassa_payments` | yookassa_payment_id (UNIQUE), user_id, plan_type, amount, status, promocode_id, is_scheduled, schedule_date |

---

## 5. КЛЮЧЕВЫЕ ФУНКЦИИ

### db_utils.py

```
get_db(telegram_id, state)            → Database  — ОСНОВНАЯ функция
get_db_sync(telegram_id)              → Database  — для синхронных контекстов (APScheduler)
is_any_admin(telegram_id)             → bool      — ADMIN_CHAT_ID ИЛИ роль owner/admin в org
get_user_org_role(telegram_id)        → str|None  — 'owner'/'admin'/'user'/None
get_user_org_scope(telegram_id)       → (scope_type, list[str])
get_user_full_scope(telegram_id)      → (scope_type, list[str], custom_title)
get_role_display_label(role, scope_type, scope_values, custom_title=None) → str
clear_state_keep_org(state, extra_keys=None) → None
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

### main.py — APScheduler (7 задач)

| ID задачи | Расписание | Назначение |
|---|---|---|
| `send_sales_alerts` | каждую минуту (сек 0) | уведомления о дневных целях продаж |
| `send_payment_alerts` | каждую минуту (сек 12) | напоминания об окончании подписки |
| `send_daily_reports` | каждую минуту (сек 24) | ежедневные отчёты |
| `send_personalized_notifications` | каждую минуту (сек 36) | персонализированные уведомления |
| `check_scheduled_notifications` | каждую минуту (сек 48) | запланированные рассылки (UTC) |
| `auto_finish_contests` | каждые 30 минут | автозавершение конкурсов |
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

**Сессия 26:** cross-org broadcast баг; check_notifications_permission получал DB-id вместо telegram_id; JOIN sn.created_by = u.id; добавлена задача check_scheduled_notifications.

**Сессия 27:** итоговый аудит. HTML-инъекция устранена в 44 местах / 9 файлах (he()). 0 bare except, 0 print().

**Сессия 28:** аудит сессий 24–25. Критический баг salary_handlers (fsm_edit вместо edit_text). he() в commission_handlers и sales_plans_handlers.

**Сессия 29:** `is_any_admin()` переписан — роль в user_org_mapping приоритетнее ADMIN_CHAT_ID. role='user' всегда False.

**Сессия 30:** конкурсы (полный жизненный цикл); дашборд перенесён в Отчёты; инвайт для орг-admin.

**Сессии 31–32:** multi-select категорий в plan wizard; редактирование планов; дашборд пользователя (все планы, призы конкурсов).

**Сессия 33:**
1. Anchor message fix — `clear_state_keep_org` ПОСЛЕ `fsm_edit`.
2. Edit plan: все поля (Получатель/Период/Метрика/Фильтр). Callback prefixes: `epwho_*`, `epperiod_*`, `epmetric_*`, `epfilter_*`.
3. Admin dashboard: все планы через `_plan_summary_line()`.
4. Баг `plans_progress` NameError исправлен.
5. `create_tables()` теперь вызывается для супер-адмна в `get_db()`.
6. `deploy.sh exclusions` — исключены `.db`, `.pkl`, `data/tenants/` из GitHub.

**Сессия 34:**
1. Профиль пользователя — роль вверху, дата в DD.MM.YYYY, he() на всех полях.
2. Очистка архива конкурсов с подтверждением.
3. `is_any_admin()` приоритет: org-роль > env_manager.
4. Admin dashboard: `_on_shift_details()` + `_today_total_earnings()`.
5. Reports: «По городу» внутри «За период»; сотрудник — «Мои продажи» + «Текущий месяц».
6. Rankings: 8 колонок в `get_sales_ranking()`; `_ranking_period()`; `_period_kb()`; позиция вне топ-10.
7. `deploy.sh` — `--no-amvera` флаг; верификация через `git ls-remote`.

**Сессия 35:**
1. he() аудит: contacts_handlers, payment_admin_handlers, main.py, plan_notifications, payment_system_admin.
2. `clear_state_keep_org(state, extra_keys=[...])` — сохранение доп. ключей FSM.
3. Excel download → FSM (избавился от длинных callback_data).
4. `safe_cb()` / `resolve_cb_name()` в handlers.py и admin_handlers.py.
5. **Multi-scope**: scope_value = JSON array; toggle UI `adm_t_s/c/n_*` + `adm_scope_submit`.
6. **Custom title**: custom_title в user_org_mapping; `set_user_title()`; 🏷️ в карточке пользователя.

**Сессия 36:**
1. **QuickSaleStates** (`searching_product = State()`).
2. **`_make_qty_keyboard(max_qty)`** — кнопки [1,2,3,5,10,20,50] + «✏️ Ввести вручную».
3. **Быстрый поиск** — «🔍 Найти товар» в экране категорий; поиск по ненулевому остатку.
4. **`sq_qty_*`** — выбор количества без ввода текста.

**Сессия 37:**
1. **`shift_templates`** таблица — UNIQUE(user_id, weekday), weekday 0=Пн..6=Вс.
2. **`work_schedule`** — добавлены колонки `start_time`, `end_time`; миграция авто.
3. **6 новых методов database.py**: `set_shift_template`, `get_shift_templates`, `get_work_day_time`, `add_work_day`, `remove_work_day`, `set_work_day_time`.
4. **salary_handlers.py** (~796 строк): пикер часов `_hour_picker_kb()`; цепочки slr_te/slr_tw; при slr_tog → применяет шаблон дня недели.
5. **⏰ Расписание смен** — кнопка `slr_tmpl_` в календаре; экран просмотра/редактирования 7 дней.
6. **my_day_detail** — сотрудник: время из work_schedule или шаблона (fallback).

**Сессия 38:**
1. **«📝 Мои продажи»** восстановлена в `main_menu()` для обычных продавцов (keyboards.py).
2. **«query is too old»** TelegramBadRequest → DEBUG (не засоряет логи).
3. **Timezone earnings**: время продажи в «Моём заработке» → `format_user_datetime(sale_date, user_tz, '%H:%M')`.
4. **now_str**: дашборд и отчёты → `get_current_user_time(user_tz).strftime(...)`.
5. **Scheduled notifications UTC fix**: ввод → `get_utc_time(naive, admin_tz)` → хранить; список → `format_user_datetime(raw_utc, admin_tz)`.
6. **Аудит итог**: 34/34 модулей · 279/279 тестов · 0 кириллицы в callback_data.

**Сессия 39:**
1. **«Команда сегодня»** дашборд: `_staff_by_shop_with_names()` — для 'wide'/'org' scale показывает «ТЦ Лето: 3 (Иванов, Петров, Сидоров)».
2. **Reports Markdown→HTML**: `report_full`, `report_shop_generate`, `report_city_generate` — HTML + `he()`.
3. **Perf: 7 индексов в `create_tables()`**: idx_sales_user_date, idx_sales_shop_date, idx_inventory_shop_prod, idx_work_schedule_date, idx_seller_earnings_sale, idx_users_shop_name, idx_users_telegram_id — все `CREATE INDEX IF NOT EXISTS`.
4. **`busy_timeout=10000`** в `create_tables()` (ранее только в `get_connection()`).
5. **«message is not modified»** → тихий `answer()` без логирования.

**Сессия 40 (2026-05-07):**
1. **`earnings_handlers.py` — Markdown→HTML** (10 мест): все `parse_mode="Markdown"` → HTML.
2. **`earnings_handlers.py` — Русские названия месяцев**: `_MONTHS_RU[]` вместо `calendar.month_name[]`.
3. **`salary_handlers.py` — Markdown→HTML** (24 места): `escape_md()` → `he()`.
4. **`keyboards.py` — утечка соединения SQLite** в `main_menu()`: добавлен `try/finally` вокруг `conn.close()`.

**Сессия 41 (2026-05-07) — ЮKASSA:**
1. **`payment_provider.py`** (новый): `get_active_provider(db)`, `create_yookassa_payment(...)`, `check_yookassa_payment_status(...)`.
2. **`database.py`** — таблица `yookassa_payments`; 7 методов: `get/set_payment_provider`, `get/set_yookassa_config`, `create/get/update_yookassa_payment_*`.
3. **`payment_system_admin.py`** — «🔀 Провайдер оплаты»; экраны выбора/настройки ЮKassa.
4. **`subscription_handlers.py`** — маршрутизация по провайдеру: СБП = скриншот; ЮKassa = URL-оплата + «✅ Я оплатил — проверить» → auto-confirm.
5. **`states.py`** — `PaymentSystemStates`: waiting_yookassa_shop_id/secret_key/return_url.
6. **559 тестов ✅** (добавлены новые сценарии).

**Сессия 42 (2026-05-07) — EXCEL ОТЧЁТЫ:**
1. **`generate_excel_report()`** в `utils.py` переписан — 4 листа: «Детальный отчёт» (с колонкой «Продавец», числовые форматы, ИТОГО), «По категориям» (BarChart), «По продавцам» (если есть данные), «По дням» (если >1 день, BarChart). Определение продавца: `len(row) >= 11`.
2. **6 Excel-хендлеров** в `reports_handlers.py` обновлены: scope-фильтр, лимит 50000, `_translit_filename()`, `⏳ Формирую файл...`, подписи с числом строк.
3. **`_translit_filename(text)`** — хелпер транслитерации для имён .xlsx файлов.
4. GitHub `7a7f1a3` · Amvera `e5913d1`. 559 тестов ✅.

**Сессия 50 (2026-05-11) — Конкурсы: индивидуальные пороги (#3) + per_sale тиры (#4):**
1. **database.py**: `contests` +`reward_mode`/`individual_targets` (ALTER TABLE migration); новая таблица `contest_product_bonuses`; новые методы `save_contest_product_bonuses`, `get_contest_product_bonuses`, `get_user_plan_pct_for_contest`; `create_contest`/`update_contest` расширены; `compute_contest_results` разделён на total (учитывает `individual_targets.by_shop`) и per_sale (тиры × qty × plan_pct); `get_user_contest_rewards` читает `reward_mode`.
2. **contests_handlers.py**: новые FSM-состояния `configuring_tier_bonus`, `entering_individual_target`; новые шаги: выбор режима (`ctrm_`), wizard тиров per_sale (`cttc_`/`ctpct_`/bonus-ввод), индивидуальные пороги по магазинам (`ctind_yes/no`, `ctindval_skip`); `_show_contest_confirm` показывает тиры/инд.пороги; `contest_confirm_create` сохраняет `reward_mode`, `individual_targets`, тиры через `save_contest_product_bonuses`; `contest_view` отображает тиры/инд.пороги; `contest_results` ветвится по `reward_mode` (per_sale: бонус, plan_pct; total: победители + инд.пороги); `contest_notify_winners` — разные тексты для per_sale/total.
3. **dashboard_handlers.py**: `_contest_block` — per_sale конкурсы показывают накопленный бонус+qty; total конкурсы — прогресс по индивидуальному порогу (`individual_target` из результатов).
4. **main.py**: `auto_finish_contests` — per_sale уведомления (qty+бонус+plan_pct); total уведомления без изменений.
5. **Индексы `contests` таблицы**: `reward_mode` = col 22, `individual_targets` = col 23 (после `created_at`=21).

**Сессия 52 (2026-05-13) — БАГФИКСЫ + СОВМЕСТНАЯ МОТИВАЦИЯ:**
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

**Сессия 73 (2026-05-18) — ЗАДАЧА #9: ПОИСК ПО @USERNAME В СПИСКЕ ПОЛЬЗОВАТЕЛЕЙ + POST-MERGE SETUP:**
1. **`database.py`**: добавлена колонка `username TEXT` в `users` (CREATE TABLE + авто-миграция `ALTER TABLE`); `add_user()` и `update_user()` принимают `username=`.
2. **`admin_handlers.py`**: `_ADMIN_USERS_COLS` расширен на `username` (13-я колонка, индекс 12); поиск в `_build_admin_users_content` теперь включает `@username` в строку сравнения (с `lstrip('@')` для толерантности к вводу); кнопка в списке показывает `@username` если есть, иначе `(магазин)`.
3. **Все call-сайты `add_user`**: `handlers.py` (5 мест: super-admin, select_city callback, process_city message, shop_bot.db trial copy), `sales_handlers.py`, `reports_handlers.py`, `notifications_handlers.py`, `subscription_handlers.py`, `db_utils.py` (оба get_db и get_db_sync) — везде передаётся `username` (с guard `len > 12` для кортежей).
4. **`scripts/post-merge.sh`**: создан (`pip install -r requirements.txt`); зарегистрирован в `.replit [postMerge]` с таймаутом 60s — теперь при каждом мерже задачи-агента автоматически устанавливаются зависимости.
5. GitHub `5c7104b` · Amvera `bcd4ae2`.

**Сессия 71–72 (2026-05-18) — ЗАДАЧА #5: ПОИСК В ИЗБРАННОМ И НЕДАВНИХ (sale flow):**
1. **`states.py`**: добавлены `SearchStates.sale_favourites` и `SearchStates.sale_recent`.
2. **`sales_handlers.py`**: добавлены билдеры `_build_fav_list_content(fav_prods, cart, query="")` и `_build_recent_list_content(recent, cart, query="")` — возвращают `(text, markup)`, фильтрация по имени, кнопки 🔍/✖️/🛒/⬅️.
3. **Рефакторинг** `sale_show_favorites` и `sale_show_recent_handler`: сохраняют `anchor_msg_id` + `sale_srch_list_type` ('fav'/'recent') в FSM; используют новые билдеры.
4. **5 новых хендлеров**: `slr_fav_srch_start` / `slr_rec_srch_start` (запуск поиска), `slr_srch_cancel` (сброс, восстанавливает `MultipleSaleStates.adding_items`), `slr_fav_srch_process` / `slr_rec_srch_process` (обработка текста, `fsm_edit` + билдер).
5. GitHub `abb429c` · Amvera `a9b5b3f`.

**Сессия 70 (2026-05-18) — ЗАДАЧА #2 (смержена агентом): ПОМЕСЯЧНАЯ МОТИВАЦИЯ И АРХИВ:**
- Таблицы `motivation_schedule` (UNIQUE product_id+year+month) и `extra_conditions_schedule`.
- `get_effective_motivation_matrix(col_months)` — матрица всех товаров с мотивацией по месяцам.
- `set_motivation_for_month` / `get_motivation_for_month` (fallback на global).
- `set_extra_condition_for_month` / `get_extra_conditions_for_month`.
- `commission_handlers.py`: кнопка «📅 По месяцам», хендлеры выбора месяца, матрица (7 столбцов: 6 прошлых + следующий), ячейки `sched_cell_*`, архив `archive_months`.
- ИСПРАВЛЕН баг: `recalculate_month_earnings` теперь принимает конкретный year/month.
- GitHub (task agent) `5c7104b` (после мержа).

**Сессия 64 (2026-05-18) — ЗАДАЧА #1: ДИНАМИЧЕСКИЙ ПОИСК ВО ВСЕХ БОЛЬШИХ СПИСКАХ:**
1. **`states.py`**: добавлена группа `SearchStates` с 12 состояниями: `shop_commission`, `user_catfilt`, `shop_plans`, `user_plans`, `product_plans`, `category_plans`, `product_contests`, `category_contests`, `shop_reports`, `shop_inventory`, `product_inventory`, `shop_contacts`.
2. **`commission_handlers.py`**: поиск магазина (`coeff_srch_shop_*`, `SearchStates.shop_commission`) + поиск сотрудника в catfilt (`catfilt_srch_*`, `SearchStates.user_catfilt`).
3. **`reports_handlers.py`**: поиск магазина для периодического отчёта (`rep_srch_shop_*`, `SearchStates.shop_reports`).
4. **`contacts_handlers.py`**: поиск магазина (`contacts_srch_shop_*`, `SearchStates.shop_contacts`).
5. **`sales_plans_handlers.py`**: 4 поиска — магазин (`plnwiz_srch_shop_*`), сотрудник (`plnwiz_srch_user_*`), мультиселект категорий (`plnwiz_srch_cat_*`), мультиселект товаров (`plnwiz_srch_prd_*`). Хелперы `_show_plan_shop_list`, `_show_plan_user_list`. `_render_category_selection`/`_render_product_selection` получили параметр `query` и кнопку 🔍.
6. **`contests_handlers.py`**: поиск товара (`ct_srch_prd_*`, `SearchStates.product_contests`) + поиск категории (`ct_srch_cat_*`, `SearchStates.category_contests`). Хелперы `_show_contest_product_list`, `_show_contest_category_list`.
7. **`inventory_handlers.py`**: поиск магазина (`inv_srch_shop_*`, `SearchStates.shop_inventory`) + поиск товара (`inv_srch_prd_*`, `SearchStates.product_inventory`), хелпер `_show_inv_product_list`. Однoмагазинный путь в `add_inventory_start` тоже использует `_show_inv_product_list`.
8. **Паттерн**: везде используется `fsm_edit` + `anchor_msg_id`. После multiselect-поиска — `await state.set_state(SalesPlansStates.selecting_*)` для восстановления стейта toggle-хендлеров.
9. GitHub `98df9d8` · Amvera `0b13728`.

**Сессия 62 (2026-05-18) — ИСПРАВЛЕНИЕ КОРРЕКТИРОВОК КОНКУРСА:**
1. **Авто-значение с полными фильтрами** — добавлен метод `compute_contest_shop_auto_totals(contest_id)` в `database.py`. Использует тот же `_build_contest_sale_query()` что и `compute_contest_results` — с фильтрами по товарам/категориям/городам/пользователям. Ранее UI показывал упрощённый `SUM(sale_price * qty)` без фильтров конкурса.
2. **per_sale (тиры): корректировки недоступны** — `compute_contest_results` в ветке per_sale никогда не вызывал `get_contest_manual_results()`, корректировки молча игнорировались. Теперь при попытке открыть корректировку для per_sale конкурса — внятное сообщение с объяснением.
3. **Рефакторинг contest_manual_list / contest_manual_set_shop** — оба хендлера заменили самописный SQL на `compute_contest_shop_auto_totals()`.
4. GitHub `c2393e0` · Amvera `8350ba2`.

**Сессия 61 (2026-05-18) — КОРРЕКТИРОВКА ПОКАЗАТЕЛЕЙ КОНКУРСА В ЛЮБОЙ МОМЕНТ:**
1. **Переименование UX**: «✏️ Ручной ввод результатов» → «✏️ Скорректировать показатели».
2. **Кнопка добавлена в экран результатов** — доступна всегда, не только из карточки конкурса.
3. **contest_manual_list**: показывает авто-значение каждого магазина (🏪) или корректировку (✏️) с автором и датой.
4. **contest_manual_set_shop**: отображает «Авто (по продажам конкурса)» + текущую корректировку с историей.
5. **Удаление сообщений пользователя**: `await message.delete()` + try/except во всех 8 обработчиках текстового ввода конкурсов.
6. **Рефакторинг `_FakeCallback`**: 3 дублирующихся класса → 1 общий экземпляр в `contest_tier_bonus_entered`.
7. GitHub `3f1cf87` · Amvera `8350ba2`.

**Сессия 43 (2026-05-07) — TOP-3 FIX + АУДИТ:**
1. **`report_full`** — убраны `[:3]` у категорий и магазинов; теперь все с guard `> 3500` / `> 3700`.
2. **`report_my_shop`** — убран `[:3]` у товаров; guard `> 3700` с сообщением «остальные товары в Excel».
3. **`generate_period_report`** — убран `[:3]` у товаров; аналогичный guard.
4. **Комплексный аудит** — 36 модулей, 559 тестов, 0 bare except, 0 print(), 0 hardcoded secrets, WAL+busy_timeout, APScheduler misfire/coalesce/max_instances=1, все соединения закрываются.
5. **Нераздражающий techdebt**: 3 строки `BROADCAST DEBUG` в `notifications_handlers.py` (logging.info) — не баги, но стоит убрать перед масштабированием.
6. GitHub `077ce67` · Amvera `790cabe`. 559 тестов ✅.

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

---

## 8. ЧЕКЛИСТ ПЕРЕД ДЕПЛОЕМ

```bash
# 1. Импорт-аудит (36 модулей):
python test_imports.py

# 2. Тесты (559 сценариев):
python test_scenarios.py

# 3. Синтаксис:
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

# 4. Деплой (GitHub + Amvera):
bash deploy.sh "commit message"

# Только GitHub:
bash deploy.sh "commit message" --no-amvera
```
