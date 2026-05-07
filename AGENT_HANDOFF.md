# AGENT HANDOFF — Daily Sales Telegram Bot
> Последнее обновление: 2026-05-07 (сессия 41)
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

**Последний деплой:** GitHub + Amvera — сессия 41 audit (2026-05-07), commit `1c53b06`

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
│  database.py        — класс Database (156+ методов)             │
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
| `users` | telegram_id, first_name, last_name, phone, email, trade_network, shop_name, city, timezone |
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
| `send_sales_alerts` | каждую минуту | уведомления о дневных целях продаж |
| `send_payment_alerts` | каждую минуту | напоминания об окончании подписки |
| `send_daily_reports` | каждую минуту | ежедневные отчёты |
| `send_personalized_notifications` | каждую минуту | персонализированные уведомления |
| `check_scheduled_notifications` | каждую минуту | запланированные рассылки (UTC) |
| `auto_finish_contests` | каждые 30 минут | автозавершение конкурсов |
| `backup_job` | 03:00 ежедневно | авто-бэкап всех БД |

**Timezone в APScheduler:** все задачи используют `datetime.now()` (UTC на Amvera), конвертируют через `.astimezone(user_tz)` для сравнения с настроенным временем.

**Scheduled notifications:** admin вводит время → `get_utc_time(naive, admin_tz)` → хранится UTC → `check_scheduled_notifications` сравнивает `datetime.now().isoformat()` (UTC) с UTC → корректно. Список показывается через `format_user_datetime(raw_utc, admin_tz)`.

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
5. Тесты: 11 новых → итого **279/279 ✅**.

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
1. **«Команда сегодня»** дашборд: добавлен `_staff_by_shop_with_names()` — для 'wide'/'org' scale показывает сгруппированный вид «ТЦ Лето: 3 (Иванов, Петров, Сидоров)». Commit `52d4d2d`.
2. **Reports Markdown→HTML**: `report_full`, `report_shop_generate`, `report_city_generate` — переведены на HTML + `he()`. Commit `6b77c93`.
3. **Perf: 7 индексов в `create_tables()`**: `idx_sales_user_date`, `idx_sales_shop_date`, `idx_inventory_shop_prod`, `idx_work_schedule_date`, `idx_seller_earnings_sale`, `idx_users_shop_name`, `idx_users_telegram_id` — все `CREATE INDEX IF NOT EXISTS`, применяются при каждом открытии БД.
4. **`busy_timeout=10000`** в `create_tables()` (ранее только в `get_connection()`).
5. **«message is not modified»** → тихий `answer()` без логирования (аналог «query is too old»). Commit `d13cbb7`.
6. **Filter/dashboard investigation (read-only)**: дашборд и ручной фильтр (🔍 Фильтр) полностью независимы — дашборд читает только `get_user_org_scope()`, никогда не читает `ADMIN_FILTER_KEY` из FSM. Ручной фильтр влияет только на отчёты/рейтинги. Потенциальный gap: `get_plans_progress()` в дашборде (строка 529) не фильтрует по scope для 'wide'/'org' — показывает ВСЕ планы орга. Для single-shop scope фильтрация есть (строки 530-535). Поведение, видимо, намеренное (owner/org-level обзор планов).

**Сессия 41 (2026-05-07) — ФУНДАМЕНТ ЮKASSA:**
1. **`payment_provider.py`** (новый модуль): `get_active_provider(db)`, `create_yookassa_payment(...)`, `check_yookassa_payment_status(...)`, `provider_label(provider)` — lazy import yookassa; при ошибке API возвращает None/error, не ломает бота.
2. **`database.py`** — новая таблица `yookassa_payments`; 7 новых методов: `get_payment_provider`, `set_payment_provider`, `get_yookassa_config`, `set_yookassa_config`, `create_yookassa_payment_record`, `get_yookassa_payment_by_payment_id`, `update_yookassa_payment_status`. Ключи в `payment_settings`: `payment_provider`, `yookassa_shop_id`, `yookassa_secret_key`, `yookassa_return_url`.
3. **`payment_system_admin.py`** — в «💳 Настройки оплаты» добавлена кнопка «🔀 Провайдер оплаты»; экран выбора СБП/ЮKassa; экран настройки ЮKassa (Shop ID, секретный ключ с маскированием, Return URL); переключение на ЮKassa требует заполненных Shop ID и Secret Key; все строки через `he()`.
4. **`subscription_handlers.py`** — `proceed_to_payment` маршрутизируется по провайдеру: СБП = старый поток (скриншот), ЮKassa = создаёт платёж → кнопка «💳 Перейти к оплате» (URL) + «✅ Я оплатил — проверить»; новый хэндлер `check_yookassa_payment` — проверяет статус в API → auto-confirm при 'succeeded' (create_payment_request + confirm_payment_request), уведомляет супер-администратора.
5. **`states.py`** — `PaymentSystemStates`: `waiting_yookassa_shop_id`, `waiting_yookassa_secret_key`, `waiting_yookassa_return_url`.
6. **`subscription_router.py`** — зарегистрирован `check_yookassa_payment` (`yk_check_*`).
7. **`test_imports.py`** — добавлен `payment_provider` в список модулей.
8. Тесты: **279/279 ✅**. Бот запущен чисто.

**Сессия 40 (2026-05-07) — ПОЛНЫЙ ОБЗОР ПРОЕКТА + ИСПРАВЛЕНИЯ:**
1. **`earnings_handlers.py` — Markdown→HTML** (10 мест): все `parse_mode="Markdown"` → `parse_mode="HTML"`, `*жирный*` → `<b>жирный</b>`. Commit `f3fa6ec`.
2. **`earnings_handlers.py` — Английские названия месяцев**: `calendar.month_name[x]` (возвращал "May", "January") → `_MONTHS_RU[x]` (локальный список, "Май", "Январь"). Был баг на всех экранах истории заработка.
3. **`earnings_handlers.py` — Неэкранированный `shop_name`**: в детальном виде дня (`earnings_day_details`) `shop_name` попадал в Markdown без `escape_md()` — при имени магазина с `_` или `*` рендеринг ломался. Теперь `he(shop_name)` в HTML.
4. **`salary_handlers.py` — Markdown→HTML** (24 места): все `parse_mode="Markdown"` → `parse_mode="HTML"`, все `escape_md(name)` → `he(name)`, имена магазинов (`shop_str`) тоже через `he()`. Включая `_my_schedule_text()`, `_refresh_admin_calendar()`, `salary_summary()` и все hour-picker экраны.
5. **`keyboards.py` — утечка соединения SQLite**: в `main_menu()` `conn.close()` стоял без `try/finally` — при исключении между `connect()` и `close()` соединение утекало. Исправлено добавлением `try/finally`.
6. **Полный аудит архитектуры** (read-only): прочитаны все 25+ модулей. Найдены и задокументированы ниже.

**Архитектурные наблюдения (не баги, но важно знать):**
- `handlers.py` line 37: `db = Database('data/shop_bot.db')` — module-level instance. Не стале (bot перезапускается), но создаёт соединение при импорте.
- `subscription_handlers.py` `_get_db()` создаёт новый `Database` на каждый вызов (намеренно — для изоляции).
- Dashboard `get_plans_progress()` без scope-фильтра для org/wide — намеренно (owner видит все планы орга).
- `create_tables()` вызывается при каждом `get_db()` — 7 CREATE INDEX IF NOT EXISTS на каждый запрос. На практике быстро (SQLite проверяет наличие), но есть overhead.

---

## 7. ТИПИЧНЫЕ ЛОВУШКИ

1. **`clear_state_keep_org` ПОСЛЕ `fsm_edit`** — не до! Иначе anchor_msg_id теряется.
2. **Инициализировать переменные перед try/except** — если используются снаружи блока.
3. **`state.clear()` запрещён** → только `clear_state_keep_org(state)`.
4. **Пользовательские строки в HTML** → всегда `he(var)`. Тексты кнопок — не нужен.
5. **`get_users_for_notifications()`** → `[0]` = внутренний users.id, `[1]` = telegram_id.
6. **`scheduled_notifications.created_by`** хранит users.id (не telegram_id). JOIN = `ON sn.created_by = u.id`.
7. **Новый роутер** → зарегистрировать в main.py.
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

---

## 8. ЧЕКЛИСТ ПЕРЕД ДЕПЛОЕМ

```bash
# 1. Полный импорт-аудит (34 модуля):
python3 -c "
errors=[]
for m in ['database','db_utils','tenant_manager','env_manager','keyboards','states','utils',
          'message_utils','timezone_utils','handlers','admin_handlers','dashboard_handlers',
          'reports_handlers','sales_plans_handlers','payment_admin_handlers','subscription_handlers',
          'commission_handlers','contests_handlers','salary_handlers','notifications_handlers',
          'earnings_handlers','sales_handlers','products_handlers','inventory_handlers',
          'backup_handlers','contacts_handlers','plan_notifications','payment_system_admin','main',
          'filter_handlers','filter_utils','hints','notif_utils','pagination_utils']:
    try: __import__(m); print(f'  ✅ {m}')
    except Exception as e: print(f'  ❌ {m}: {e}'); errors.append(m)
print(f'Итог: {34-len(errors)} OK, {len(errors)} ошибок')
"

# 2. Тесты (279 сценариев):
python test_scenarios.py

# 3. Деплой (GitHub + Amvera):
bash deploy.sh "commit message"

# Только GitHub:
bash deploy.sh "commit message" --no-amvera
```
