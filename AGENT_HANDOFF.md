# AGENT HANDOFF — Daily Sales Telegram Bot
> Последнее обновление: 2026-05-06 (сессия 35)
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

**Последний деплой:** GitHub + Amvera — сессия 35 (2026-05-06)

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
│  18 РОУТЕРОВ (handlers)                                          │
│  router               ← handlers.py         (старт, профиль)    │
│  admin_router         ← admin_handlers.py   (орг, юзеры)        │
│  sales_router         ← sales_handlers.py   (продажи)           │
│  products_router      ← products_handlers.py (каталог, остатки) │
│  inventory_router     ← inventory_handlers.py (склад)           │
│  reports_router       ← reports_handlers.py  (отчёты+дашборд)   │
│  commission_router    ← commission_handlers.py (мотивация)      │
│  earnings_router      ← earnings_handlers.py  (заработок)       │
│  sales_plans_router   ← sales_plans_handlers.py (планы продаж)  │
│  salary_router        ← salary_handlers.py    (зарплата/смены)  │
│  contacts_router      ← contacts_handlers.py  (контакты)        │
│  notifications_router ← notifications_handlers.py               │
│  payment_admin_router ← payment_admin_handlers.py               │
│  payment_system_router← payment_system_admin.py (~90 функций)   │
│  subscription_router  ← subscription_router.py (агрегатор)      │
│  backup_router        ← backup_handlers.py                      │
│  contests_router      ← contests_handlers.py  (конкурсы)        │
│  dashboard_router     ← dashboard_handlers.py (дашборд-сводка)  │
└──────────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  СЛОЙ ДАННЫХ                                                     │
│  db_utils.py        — get_db(), is_any_admin() [ГЛАВНЫЙ]        │
│  database.py        — класс Database (133+ методов)             │
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
| `plan_notifications.py` | Уведомления об изменениях тарифов (lazy-импорт из payment_system_admin.py) |
| `scheduler_module.py` | Синглтон APScheduler — set_scheduler() / get_scheduler() |
| `timezone_utils.py` | Работа с часовыми поясами пользователей |
| `reports_access_control.py` | Проверка доступа к отчётам по подписке |
| `subscription_utils.py` | Лимиты подписки, get_plan_limits() |
| `backup_manager.py` | Резервное копирование и восстановление всех БД |
| `restart_manager.py` | Управление перезапуском бота; restart_data_file = "data/restart_data.json" |
| `message_utils.py` | fsm_edit(), safe_edit_message() — anchor message pattern |
| `utils.py` | he(), escape_md(), format_currency(), format_price(), generate_excel_report() |
| `pickle_storage.py` | FSM persistence через PickleStorage (data/fsm_storage.pkl) |

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
super-admin + selected_org_db в state → Database(selected_db) + create_tables()  ← ВАЖНО: create_tables() теперь вызывается!
user в org (tenant_manager)            → Database(org_*.db)   + create_tables()
иначе                                  → Database(shop_bot.db) + create_tables()
```

### 2.2 Проверка прав — ТОЛЬКО через is_any_admin()

```python
# ✅ ПРАВИЛЬНО:
from db_utils import is_any_admin
if is_any_admin(telegram_id):  # проверяет ADMIN_CHAT_ID ИЛИ роль в org

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
# ✅ ПРАВИЛЬНО — сохраняет selected_org_db для супер-админа:
from db_utils import clear_state_keep_org
await clear_state_keep_org(state)

# ❌ НЕПРАВИЛЬНО — теряет контекст орг у супер-админа:
await state.clear()
```

### 2.5 callback_data — КРИТИЧЕСКИЙ ЛИМИТ 64 байта (aiogram)

```python
# ✅ ПРАВИЛЬНО — для любых user-provided строк:
from keyboards import safe_cb, resolve_cb_name
builder.button(text=shop, callback_data=safe_cb("sale_shop_", shop))
# В handler'е:
shop_raw = callback.data.replace("sale_shop_", "")
shop = resolve_cb_name(shop_raw, current_db.get_all_shops() or [])

# ❌ НЕПРАВИЛЬНО — кириллица >= 22 символов ломает API:
callback_data=f"sale_shop_{shop_name}"
```

### 2.6 HTML в сообщениях — ТОЛЬКО через he() для пользовательских строк

```python
# ✅ ПРАВИЛЬНО:
from utils import he
text = f"Товар: <b>{he(product_name)}</b>\nМагазин: <b>{he(shop_name)}</b>"

# ❌ НЕПРАВИЛЬНО — если в имени есть < > & → TelegramBadRequest:
text = f"Товар: <b>{product_name}</b>"
```

### 2.7 Инициализация переменных перед try/except

```python
# ✅ ПРАВИЛЬНО — переменная доступна даже при исключении:
plans_progress = []
try:
    plans_progress = current_db.get_plans_progress()
except Exception:
    pass
if plans_progress:   # не NameError

# ❌ НЕПРАВИЛЬНО — NameError если try бросит исключение:
try:
    plans_progress = current_db.get_plans_progress()
except Exception:
    pass
if plans_progress:   # NameError!
```

### 2.8 get_all_shops() → list[str], НЕ list[tuple]!

```python
# ✅ ПРАВИЛЬНО:
shops = current_db.get_all_shops()  # ['Магазин 1', 'Магазин 2']
for shop in shops:
    builder.button(text=shop, callback_data=safe_cb("prefix_", shop))

# ❌ НЕПРАВИЛЬНО — даёт первый символ строки!:
builder.button(text=shop[0], ...)   # 'М' вместо 'Магазин 1'
```

### 2.9 Database.db_file, НЕ .db_path!

```python
current_db.db_file   # ✅ строка пути к файлу
current_db.db_path   # ❌ AttributeError
```

---

## 3. ДЕПЛОЙ И AMVERA

### deploy.sh — механика

- Синхронизирует файлы workspace → `/tmp/github-deploy` и `/tmp/amvera-deploy` (отдельные git repo)
- **Исключает**: `.git`, `.local`, `__pycache__`, `data/backup`, `data/tenants`, `*.db`, `*.pkl`, `*.log`
- `AGENT_HANDOFF.md` и `replit.md` → только в GitHub, не в Amvera
- GitHub: `git push --force origin HEAD:main`
- Amvera: `git push amvera HEAD:master --force` (ВСЕГДА по умолчанию)
- После пуша на Amvera: `git ls-remote amvera refs/heads/master` → верификация совпадения хэшей
- Флаг `--no-amvera` — пропустить Amvera, только GitHub

### Почему Amvera всегда напрямую

Amvera webhook срабатывал ненадёжно — иногда не пересобирал контейнер. Начиная с сессии 34 `deploy.sh` всегда пушит на Amvera напрямую и верифицирует хэш.

### Amvera warnings ("вне persistenceMount") — ВСЕ ЛОЖНЫЕ

Amvera статически сканирует код и видит `sqlite3.connect('data/...')` → предупреждает. Но рабочая директория на Amvera = `/app`, поэтому `data/xxx.db` = `/app/data/xxx.db` = **persistenceMount** ✅. Все файлы `data/main.db`, `data/shop_bot.db`, `data/*.json`, `data/fsm_storage.pkl` хранятся корректно.

### Amvera persistent data (production)

- Persistent mount: `/app/data` (только эта папка сохраняется между перезапусками)
- Файлы: `main.db`, `shop_bot.db`, `fsm_storage.pkl`, `tenants/org_huawei.db`, `backup/`
- Код приходит из git (Amvera repo), данные — из persistent volume (git не перезаписывает)
- `org_huawei.db` — единственная тенант-БД в production. Создана до ряда миграций, но `create_tables()` авто-применяет их при первом обращении

---

## 4. БАЗЫ ДАННЫХ

### data/main.db — только через tenant_manager

| Таблица | Ключевые колонки |
|---|---|
| `organizations` | id, name, owner_id, invite_code, db_path, subscription_plan, is_active |
| `user_org_mapping` | telegram_id, org_id, role (super_admin/admin/user) |
| `users` | зеркало профиля для быстрого поиска |

### data/shop_bot.db и data/tenants/org_*.db — идентичная схема

| Таблица | Назначение |
|---|---|
| `users` | telegram_id, first_name, last_name, phone, email, trade_network, shop_name, city, timezone, admin_notifications |
| `products` | id, name, category, price, motivation_type, motivation_value |
| `inventory` | shop_name, product_id, quantity, last_updated |
| `sales` | product_id, shop_name, quantity_sold, sale_price, user_id, sale_date |
| `seller_earnings` | user_id, sale_id, amount, earning_date |
| `motivation_rules` | product_id, commission_type, commission_value, admin_id |
| `motivation_conditions` | условия мотивации (сверхплан, коэф. смены, фильтр категорий) |
| `motivation_extra_conditions` | доп. условия (создана миграцией в create_tables) |
| `salary_settings` | user_id, daily_rate, updated_by |
| `work_schedule` | user_id, work_date, UNIQUE(user_id, work_date) |
| `sales_plans` | id, plan_type, metric_type, target_value, target_type, user_id, shop_name, filter_type, filter_value, is_active, created_by |
| `notification_settings` | low_stock_alerts, daily_reports, sales_alerts, payment_alerts, admin_notifications |
| `scheduled_notifications` | id, job_id(UUID), created_by(FK→users.id!), notification_text, recipients_type, scheduled_datetime, status |
| `contests` | id, title, contest_type, scope, metric, target_value, reward_type, reward_value, start_date, end_date, status, winner_user_id |

### data/shop_bot.db ТОЛЬКО (платежи централизованы):

| Таблица | Назначение |
|---|---|
| `subscriptions` | user_id, plan_type, start_date, end_date, is_active |
| `payment_requests` | заявки на оплату + file_id чека + promocode_id |
| `payment_settings` | card_number, recipient_name, bank_name |
| `subscription_plans` | name, price, duration_days, max_products, max_shops, features |
| `promocodes` | code, discount_percent, max_usage, current_usage, is_active |

---

## 5. КЛЮЧЕВЫЕ ФУНКЦИИ

### db_utils.py — точка входа к БД

```
get_db(telegram_id, state)     → Database  — ОСНОВНАЯ функция
  super-admin + selected_org → org_db + create_tables()  ← create_tables() ОБЯЗАТЕЛЕН
  user в org                 → tenant org_*.db + create_tables()
  иначе                      → shop_bot.db + create_tables()

get_db_sync(telegram_id)       → Database  — для синхронных контекстов
is_any_admin(telegram_id)      → bool      — ADMIN_CHAT_ID ИЛИ роль в org
get_user_org_role(telegram_id) → str|None  — 'super_admin'/'admin'/'user'/None
clear_state_keep_org(state)    → None      — очистка state БЕЗ потери selected_org_db
```

### dashboard_handlers.py — дашборд

```python
build_admin_dashboard(current_db, today, now_str, user_id=0, telegram_id=0) → str
  # Показывает: зарплатный блок, продажи + мотивация за сегодня, остатки,
  #             ВСЕ активные планы с прогресс-барами, конкурсы, список смены с ФИО
  # Планы отображаются через _plan_summary_line() — тот же формат что в "Мои планы"

build_user_dashboard(current_db, user_id, telegram_id, today, now_str) → str
  # Показывает: зарплата, мотивация, продажи сегодня, планы, призы конкурсов

_on_shift_details(db_file, today) → list[(first_name, last_name, shop_name)]
  # JOIN work_schedule + users — список сотрудников на смене сегодня

_today_total_earnings(db_file, today) → float
  # SUM(seller_earnings.commission_amount) JOIN sales WHERE sale_date = today

_plan_summary_line(plan, actual, percent) → str
  # Формат: "📋 **who** · период · метрика · кат. «...»\nbar pct%\nФакт: x / Цель: y"
```

### sales_plans_handlers.py — планы продаж

```
_plan_summary_line(plan, actual, percent) → str  — канонический формат отображения плана
_PERIOD_LABELS = {'weekly': 'Неделя', 'monthly': 'Месяц', ...}
_METRIC_LABELS = {'turnover': 'Оборот (₽)', 'quantity': 'Количество (шт)'}

Callback prefixes (редактирование плана):
  epwho_       — выбор получателя (seller/shop/all)
  epwhousr_    — выбор конкретного продавца
  epwhoshp_    — выбор конкретного магазина
  epwhotgt_    — подтверждение получателя
  epperiod_    — выбор периода
  epmetric_    — выбор метрики
  epfilter_    — выбор фильтра (all/category/product)
  epperset_    — сохранить filter=all
  epmset_      — подтверждение мультивыбора категорий/товаров
  plnflt_cat   — мультивыбор категорий (в create и edit режиме)
```

### utils.py — утилиты

```
he(text)                → str   — html.escape для Telegram HTML parse_mode
escape_md(text)         → str   — экранирование Markdown V1
format_currency(amount) → str   — '12 345₽'
format_price(price)     → str   — '12 345' (без знака валюты)
generate_excel_report(...)      — выгрузка продаж в .xlsx
```

### main.py — APScheduler (7 задач)

| ID задачи | Функция | Расписание |
|---|---|---|
| `send_sales_alerts` | send_sales_alerts | каждую минуту |
| `send_payment_alerts` | send_payment_alerts | каждую минуту |
| `send_daily_reports` | send_daily_reports | каждую минуту |
| `send_personalized_notifications` | send_personalized_notifications | каждую минуту |
| `check_scheduled_notifications` | check_scheduled_notifications | каждую минуту |
| `auto_finish_contests` | auto_finish_contests | каждую минуту |
| `backup_job` | daily_backup_task | 03:00 ежедневно |

---

## 6. ИСТОРИЯ СЕССИЙ

**Сессии 1–10:** базовая архитектура, multi-tenancy, роли, FSM flows, меню.

**Сессии 11–15:** PickleStorage, кеш путей БД, bulk import, полный audit callback.answer() (58 хендлеров).

**Сессии 16–23:** callback.answer(); org-admin баги; рейтинги (date() vs ISO); escape_md().

**Сессии 24–25:** мотивационные условия (сверхплан, коэф.); планы продаж; зарплата и смены.

**Сессия 26:** кросс-организационный broadcast баг; check_notifications_permission получал DB-id вместо telegram_id; JOIN sn.created_by = u.id (не u.telegram_id); добавлена задача check_scheduled_notifications.

**Сессия 27:** итоговый аудит. HTML-инъекция устранена в 44 местах / 9 файлах (he()). 0 bare except, 0 print(). GitHub `bcd5c2f`.

**Сессия 28:** аудит сессий 24–25. Критический баг salary_handlers (fsm_edit вместо edit_text). he() в commission_handlers и sales_plans_handlers.

**Сессия 29:** `is_any_admin()` переписан — роль в user_org_mapping приоритетнее ADMIN_CHAT_ID. role='user' всегда False.

**Сессия 30:** конкурсы (полный жизненный цикл); дашборд перенесён в Отчёты; инвайт для орг-admin; баг salary_handlers с is_admin().

**Сессии 31–32:** multi-select категорий в plan wizard; редактирование планов; дашборд пользователя (все планы, призы конкурсов); инвайт для орг-admin.

**Сессия 33:**
1. **Anchor message fix** — `clear_state_keep_org` ПОСЛЕ `fsm_edit` (не до). Файлы: `sales_plans_handlers.py`, `commission_handlers.py`.
2. **Edit plan: все поля** — расширено редактирование до Получателя/Периода/Метрики/Фильтра. Callback prefixes: `epwho_*`, `epperiod_*`, `epmetric_*`, `epfilter_*`.
3. **Admin dashboard: все планы** — `build_admin_dashboard` итерирует ВСЕ активные планы через `_plan_summary_line()`.
4. **Баг: `plans_progress` NameError** — исправлено: `plans_progress = []` до try.
5. **Баг: create_tables() не вызывался для супер-админа** — исправлено в `db_utils.py`.
6. **deploy.sh exclusions** — исключены `.db`, `.pkl`, `data/tenants/` из GitHub.
7. **Amvera webhook lag** — решение: `--with-amvera` для прямого push.

**Сессия 34:**
1. **Профиль пользователя** (`handlers.py`) — роль показывается вверху (👑/🔧/👤), дата регистрации в формате DD.MM.YYYY через таймзону пользователя, `he()` на всех полях.
2. **Очистка архива конкурсов** (`contests_handlers.py` + `database.py`) — кнопка «Очистить архив» с подтверждением; метод `clear_contests_archive()`.
3. **`is_any_admin()` приоритет** (`db_utils.py`) — роль в `user_org_mapping` теперь приоритетнее `env_manager`. Исправлено: join-mode пользователи не получают права админа.
4. **Admin dashboard** (`dashboard_handlers.py`):
   - Подпись: `build_admin_dashboard(db, today, now_str, user_id=0, telegram_id=0)` — теперь передаётся из `reports_menu` (был баг: вызов без user_id).
   - Новый хелпер `_on_shift_details()` — JOIN work_schedule+users, возвращает список `(fn, ln, shop)` вместо простого счётчика.
   - Новый хелпер `_today_total_earnings()` — суммарная мотивация за сегодня из seller_earnings+sales.
   - Блок «Продажи сегодня» показывает «Мотивация (выплачено): N ₽».
   - Блок «Команда сегодня» показывает детальный список: «— Тарасов Илья · ТЦ Лето».
5. **Reports: меню и отчёты** (`reports_handlers.py`):
   - Кнопка «🏙️ По городу» убрана из главного меню админа — она перенесена внутрь «За период».
   - «За период» для админа: `📊 Общий | 🏪 По магазину | 🏙️ По городу` (три кнопки на экране выбора дат).
   - Сотрудник — новые кнопки: `📊 Мои продажи` (бывший «Отчёт по магазину»), `📅 Текущий месяц` (быстрый отчёт с 1-го числа по сегодня), `📅 За период`.
   - `generate_period_report`: сотрудник теперь фильтрует по `user_id` (не shop_name) — только свои продажи.
   - `report_today`: Markdown → HTML, `he()` на именах товаров/магазинов.
   - Новые обработчики: `period_report_city` → список городов → `period_city_*` → отчёт; `report_my_month`.
6. **Rankings: полный рефакторинг** (`reports_handlers.py` + `database.py`):
   - Удалён дублирующий `user_rankings_menu_handler` (строка 136), который обходил меню.
   - `get_sales_ranking()` → теперь 8 колонок (добавлен `u.id as user_db_id`); все старые `row[:7]` работают.
   - Новые хелперы: `_ranking_period(period)` → `(start, end, label)`; `_period_kb(active, rtype, back_cb)` → клавиатура с тоглами периода.
   - Меню: admin = Продавцы + Магазины + Города + Очистить; user = Продавцы + Магазины.
   - Рейтинг магазинов открыт для всех (не только admin).
   - Период: `7 дней | ✅ Этот месяц | Прошлый` — тогл без возврата в меню.
   - Продавцы: если пользователь вне топ-10 — показывается его позиция внизу («📍 Ваша позиция: 15-е место»).
   - Все рейтинги: HTML вместо Markdown, единый период (текущий месяц по умолчанию).
   - Callback-схема: `rank_sel_month/7d/prev`, `rank_shp_month/7d/prev`, `rank_cty_month/7d/prev`.
7. **deploy.sh** — `--with-amvera` стал дефолтом; добавлен флаг `--no-amvera`; после каждого Amvera пуша верификация через `git ls-remote`.

**Сессия 35 — полный he() аудит + multi-scope + custom_title:**

1. **he() аудит: все оставшиеся файлы** — добавлен `from utils import he` в `contacts_handlers.py` и `payment_admin_handlers.py`; применён `he()` ко всем user-строкам в HTML-блоках:
   - `contacts_handlers.py` — all 5 handlers: my profile, support contact, shop contacts, city contacts, all contacts (first_name, last_name, middle_name, phone, email, shop, city, trade_network).
   - `main.py:286` `he(shop_name)` в уведомлении о продажах; `L292` `he(product_name)` в том же блоке; `L326` `he(shop_name)` в уведомлении об остатках.
   - `payment_admin_handlers.py:179` — `he(first_name)`, `he(last_name)`, `he(plan_name)` в caption чека.
   - `plan_notifications.py:31,78,120` — `he(first_name)` в трёх notification-функциях.
   - `payment_system_admin.py:1198` — `he(shop_name)` в активных подписках.
2. **`clear_state_keep_org` расширен** — параметр `extra_keys: list | None = None` для сохранения доп. ключей FSM (использован для Excel: `excel_start`, `excel_end`, `excel_shop`).
3. **`download_excel_period_` → FSM** — callback_data с русскими именами магазинов мог превышать 64 байта; перенесено в FSM-ключи; обработчик изменён на `F.data == "download_excel_period"`.
4. **`prof_shop_pick_` / `prof_net_pick_`** в `handlers.py` — `safe_cb()` + `resolve_cb_name()`.
5. **`adm_shop_pick_` / `adm_net_pick_`** в `admin_handlers.py` — `safe_cb()` + `resolve_cb_name()`.
6. **Multi-scope (multi-select)** — `scope_value` хранится как JSON array; UI: toggle `adm_t_s_*`/`adm_t_c_*`/`adm_t_n_*` + `adm_scope_submit`; `get_user_org_scope()` → `(scope_type, list[str])`.
7. **Custom title** — `custom_title` колонка в `user_org_mapping`; `set_user_title()` в tenant_manager; `get_role_display_label(..., custom_title)` — приоритет над вычисленным ярлыком; `AdminUserStates.waiting_for_admin_title`; кнопка 🏷️ в карточке пользователя и меню роли.
8. **Импорт-аудит** — 28 модулей проверены (26 основных + plan_notifications + payment_system_admin): 0 ошибок.

---

## 7. ТИПИЧНЫЕ ЛОВУШКИ

1. **`clear_state_keep_org` ПОСЛЕ `fsm_edit`** — не до! Иначе anchor_msg_id теряется.

2. **Инициализировать переменные перед try/except** — если используются снаружи блока.

3. **`state.clear()` запрещён** → только `clear_state_keep_org(state)`.

4. **Пользовательские строки в HTML** → всегда `he(var)`. Даже shop_name может содержать `<>&`.

5. **`get_users_for_notifications()`** → `[0]` = внутренний users.id, `[1]` = telegram_id.

6. **scheduled_notifications.created_by** хранит users.id (не telegram_id). JOIN = `ON sn.created_by = u.id`.

7. **Новый роутер** → зарегистрировать в main.py.

8. **callback_data + кириллица** → `safe_cb(prefix, value)`, не f-строка.

9. **Планировщик** → `_get_scheduler_db_paths()` для итерации всех тенантов. Никогда не хранить глобальный Database.

10. **plan_notifications.py** — lazy-импорт (внутри функций), не на уровне модуля.

11. **Database.db_file** (не .db_path!) — атрибут пути к файлу.

12. **Amvera webhook** — force-push может не триггерить rebuild. Используй `--with-amvera` если изменения не доходят.

13. **Тестовые БД не в GitHub** — `*.db`, `*.pkl`, `data/tenants/` исключены из deploy.sh. Не добавлять их обратно.

14. **`get_user_org_scope()`** → `(scope_type, list[str])` НЕ `(str, str)` — scope_values всегда список (пустой = полный доступ).

15. **`clear_state_keep_org(state, extra_keys=[...])`** — передавай ключи для сохранения. Пример: `clear_state_keep_org(state, extra_keys=["excel_start","excel_end","excel_shop"])`.

16. **InlineKeyboardButton.text** — Telegram НЕ парсит HTML в тексте кнопок. `he()` там НЕ нужен, даже если сообщение с `parse_mode="HTML"`.

---

## 8. ЧЕКЛИСТ ПЕРЕД ДЕПЛОЕМ

```bash
# 1. Полный импорт-аудит (все модули):
python3 -c "
errors=[]
for m in ['database','db_utils','tenant_manager','env_manager','keyboards','states','utils',
          'message_utils','handlers','admin_handlers','dashboard_handlers','reports_handlers',
          'sales_plans_handlers','payment_admin_handlers','subscription_handlers',
          'commission_handlers','contests_handlers','salary_handlers','notifications_handlers',
          'earnings_handlers','sales_handlers','products_handlers','inventory_handlers',
          'backup_handlers','contacts_handlers','plan_notifications','payment_system_admin','main']:
    try: __import__(m); print(f'  ✅ {m}')
    except Exception as e: print(f'  ❌ {m}: {e}'); errors.append(m)
print(f'Итог: {28-len(errors)} OK, {len(errors)} ошибок')
"

# 2. Деплой (GitHub + Amvera):
bash deploy.sh "commit message"
# Только GitHub:
bash deploy.sh "commit message" --no-amvera
```
