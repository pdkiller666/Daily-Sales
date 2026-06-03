---
name: PostgreSQL migration plan
description: Полный план поэтапной миграции DailySales с SQLite на PostgreSQL — архитектура, код, риски, порядок шагов.
---

# План миграции DailySales: SQLite → PostgreSQL

## Контекст и мотивация

**Почему SQLite — технологический потолок:**
- WAL-режим снижает lock-contention, но не устраняет его при 50+ одновременных пользователях
- Один файл на орг — нет connection pool; каждый `get_db()` открывает новое соединение
- Нет горизонтального масштабирования (несколько Replit/Amvera реплик невозможно)
- Нет нативных JSON-индексов, partial indexes, full-text search
- `asyncio.to_thread(sync_sqlite)` — временный костыль; PG имеет нативный async-драйвер

**Текущая схема хранения:**
```
data/main.db          — организации, маппинг пользователей (read: env_manager)
data/shop_bot.db      — платежи, подписки, аддоны (centralized, shared)
data/tenants/org_*.db — изолированные БД каждой орг (57 таблиц каждая)
data/fsm_storage.db   — Telegram FSM state (SQLiteStorage)
```

---

## Архитектурное решение для multi-tenancy

### Вариант A: Schema-per-org (рекомендуется)
```
PostgreSQL instance
├── schema: public        → main (orgs, user_org_mapping)
├── schema: shared        → shop_bot (payments, subscriptions, addons)
├── schema: org_huawei    → все 57 таблиц орга
├── schema: org_msk_retail → все 57 таблиц орга
└── schema: fsm           → fsm_data
```
**Плюсы:** одна БД, изоляция через search_path, единый бэкап, легко добавить орг (`CREATE SCHEMA`)
**Минусы:** миграции применяются ко всем схемам одновременно

### Вариант B: Database-per-org
```
PostgreSQL cluster
├── DB: dailysales_main
├── DB: dailysales_shared
├── DB: org_huawei
└── DB: org_msk_retail
```
**Плюсы:** полная изоляция, можно вынести на разные хосты
**Минусы:** connection pool на каждую БД, сложный management

**Решение: Вариант A (Schema-per-org)** — оптимален для текущего масштаба.

---

## Технологический стек после миграции

| Компонент | Сейчас | После |
|---|---|---|
| Драйвер | `sqlite3` (sync) + `asyncio.to_thread` | `asyncpg` (async-native) |
| ORM | Нет (raw SQL) | Нет (сохраняем raw SQL — меньше рисков) |
| Connection pool | Самописный `_get_pooled_conn` | `asyncpg.create_pool()` |
| FSM storage | `SQLiteStorage` (custom) | `aiogram-contrib` PG storage или custom asyncpg |
| Миграции | `create_tables()` inline | `Alembic` |
| ENV | `DATABASE_URL` не нужен | `DATABASE_URL=postgresql://...` в Secrets |

---

## Изменения SQL-синтаксиса SQLite → PostgreSQL

| SQLite | PostgreSQL | Файл |
|---|---|---|
| `INTEGER PRIMARY KEY` | `BIGSERIAL PRIMARY KEY` или `INTEGER GENERATED ALWAYS AS IDENTITY` | database.py |
| `AUTOINCREMENT` | убрать (SERIAL/IDENTITY) | database.py |
| `TEXT` для дат | `TIMESTAMPTZ` или `DATE` | database.py |
| `REAL` | `NUMERIC(15,2)` или `DOUBLE PRECISION` | database.py |
| `?` placeholder | `$1, $2, ...` (asyncpg) или `%s` (psycopg3) | database.py |
| `INSERT OR IGNORE` | `INSERT ... ON CONFLICT DO NOTHING` | database.py |
| `INSERT OR REPLACE` | `INSERT ... ON CONFLICT DO UPDATE` | database.py |
| `ON CONFLICT(...) DO UPDATE SET` | то же — PG поддерживает | database.py |
| `strftime('%Y-%m', ...)` | `to_char(col, 'YYYY-MM')` | database.py |
| `datetime('now')` | `NOW()` или `CURRENT_TIMESTAMP` | database.py |
| `PRAGMA journal_mode=WAL` | убрать (не нужен в PG) | db_utils.py, web/deps.py |
| `PRAGMA synchronous=NORMAL` | убрать | db_utils.py, web/deps.py |
| `json_extract(col, '$.key')` | `col->>'key'` (JSONB) | database.py |

---

## Фазы миграции

### Фаза 0: Подготовка (без изменений в prod) — 1–2 недели

**0.1 Dependency audit**
```bash
pip install asyncpg alembic psycopg2-binary
# asyncpg — основной async-драйвер
# alembic — управление миграциями схемы
# psycopg2 — для скрипта переноса данных (sync)
```

**0.2 Завести PostgreSQL**
- Replit: подключить Replit Postgres через интеграцию (см. skills/integrations)
- Prod (Amvera): managed PostgreSQL или отдельный контейнер
- Добавить `DATABASE_URL` в Replit Secrets

**0.3 Создать `db_pg.py` — абстракция над asyncpg**
```python
# db_pg.py — новый модуль, не трогаем database.py до Фазы 2
import asyncpg, os
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            os.environ['DATABASE_URL'],
            min_size=5, max_size=20,
            command_timeout=30
        )
    return _pool

async def execute(sql: str, *args, schema: str = 'public'):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f'SET search_path TO {schema},public')
        return await conn.execute(sql, *args)

async def fetch(sql: str, *args, schema: str = 'public'):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f'SET search_path TO {schema},public')
        return await conn.fetch(sql, *args)
```

**0.4 Настроить Alembic**
```bash
alembic init alembic
# alembic/env.py — настроить на DATABASE_URL
# alembic/versions/ — первая ревизия: создание всех схем + таблиц
```

---

### Фаза 1: Параллельная запись (dual-write) — 2–4 недели

Цель: данные пишутся в обе БД, читаем из SQLite. Позволяет накопить данные и проверить корректность.

**1.1 Создать декоратор dual-write:**
```python
# В database.py: обернуть write-методы
async def _dual_write(method_name, *args, **kwargs):
    # Пишем в SQLite (основное)
    result = getattr(current_db, method_name)(*args, **kwargs)
    # Пишем в PG (вторичное, не блокируем)
    try:
        await pg_equivalent(method_name, *args, **kwargs)
    except Exception as e:
        logger.warning(f"PG dual-write failed: {e}")
    return result
```

**1.2 Начать с самых частых методов:**
- `add_sale()` / `delete_sale()`
- `add_user()` / `update_user()`
- `toggle_work_day()`

**1.3 Мониторинг расхождений:**
```python
# Ежечасный cron: сравниваем count(*) в SQLite vs PG
# Логируем в dual_write_stats таблицу
```

---

### Фаза 2: Рефакторинг Database класса — 4–8 недель

Цель: переписать `database.py` на asyncpg, сохраняя публичный API.

**2.1 Стратегия рефакторинга методов:**

```python
# БЫЛО (database.py):
def get_all_sales(self, shop_name=None, limit=50):
    with self.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sales WHERE ...", ...)
        return cursor.fetchall()

# СТАЛО (database_pg.py):
async def get_all_sales(self, shop_name=None, limit=50):
    sql = "SELECT * FROM sales WHERE $1 IS NULL OR shop_name=$1 LIMIT $2"
    rows = await fetch(sql, shop_name, limit, schema=self.schema)
    return [tuple(r) for r in rows]  # сохраняем tuple-совместимость
```

**2.2 Порядок переноса методов (по приоритету):**
1. CRUD продажи (самые частые)
2. Пользователи и орг
3. Инвентарь
4. Зарплата и мотивация
5. Планы и конкурсы
6. Уведомления
7. Интеграции
8. Отсутствия
9. Чат

**2.3 Tuple-совместимость:**
Весь код хендлеров обращается к строкам как `row[0]`, `row[3]` и т.д.
asyncpg возвращает `Record`, а не `tuple`. Нужен адаптер:
```python
def to_tuple(row):
    return tuple(row) if row else None

def to_tuples(rows):
    return [tuple(r) for r in rows]
```
Либо добавить `__getitem__` в Record-обёртку (не нужно — asyncpg.Record уже поддерживает `row[n]`).

**2.4 Placeholder-замена:**
SQLite: `?`, psycopg3: `%s`, asyncpg: `$1, $2, ...`
Написать утилиту конвертации:
```python
def to_pg_placeholders(sql: str) -> str:
    """Заменяет ? на $1, $2, ... по порядку"""
    counter = 0
    def replace(m):
        nonlocal counter
        counter += 1
        return f'${counter}'
    return re.sub(r'\?', replace, sql)
```

---

### Фаза 3: Миграция FSM storage — 1 неделя

```python
# sqlite_storage.py → pg_storage.py
# Использовать aiogram-contrib или написать свой на asyncpg

class PostgresStorage(BaseStorage):
    """FSM storage через asyncpg."""
    async def set_state(self, key, state=None):
        await execute(
            "INSERT INTO fsm.fsm_data(bot_id,chat_id,user_id,destiny,state) "
            "VALUES($1,$2,$3,$4,$5) ON CONFLICT(bot_id,chat_id,user_id,destiny) "
            "DO UPDATE SET state=$5",
            str(key.bot_id), key.chat_id, key.user_id, key.destiny, state
        )
    # ... get_state, set_data, get_data, close
```

---

### Фаза 4: Миграция данных — 1–2 дня downtime или zero-downtime

**Zero-downtime вариант:**

```python
# migrate_data.py — скрипт переноса
import sqlite3, asyncpg, asyncio, json

ORGS = ['org_huawei', 'org_msk_retail']  # список из main.db

async def migrate_org(org_name: str, sqlite_path: str, pg_schema: str):
    src = sqlite3.connect(sqlite_path)
    pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with pool.acquire() as dst:
        await dst.execute(f'CREATE SCHEMA IF NOT EXISTS {pg_schema}')
        await dst.execute(f'SET search_path TO {pg_schema}')
        
        for table in TABLE_ORDER:  # порядок с учётом FK
            rows = src.execute(f'SELECT * FROM {table}').fetchall()
            cols = [d[0] for d in src.execute(f'SELECT * FROM {table} LIMIT 0').description]
            
            if rows:
                placeholders = ','.join(f'${i+1}' for i in range(len(cols)))
                await dst.executemany(
                    f'INSERT INTO {table}({",".join(cols)}) VALUES({placeholders}) '
                    f'ON CONFLICT DO NOTHING',
                    [tuple(r) for r in rows]
                )
                print(f'  {table}: {len(rows)} rows migrated')
    
    src.close()

TABLE_ORDER = [
    'users', 'products', 'inventory', 'inventory_log', 'sales',
    'salary_settings', 'work_schedule', 'shift_templates',
    'seller_earnings', 'sales_plans', 'plan_milestone_alerts',
    'product_motivations', 'motivation_extra_conditions',
    'contests', 'contest_salary_payouts',
    'subscriptions', 'payment_requests',
    'notification_settings', 'notification_history',
    'scheduled_notifications', 'user_hints_seen',
    'absence_type_settings', 'absence_records',
    'salary_adjustments', 'referrals', 'subscription_addons',
    'integration_connections', 'integration_exports', 'integration_log',
    'chat_messages', 'chat_topics',
    # ... все 57 таблиц
]
```

---

### Фаза 5: Переключение prod → PG — 1 день

```
1. Включить maintenance mode (бот отвечает "⏳ Обновление системы")
2. Финальная sync: migrate_data.py --incremental (только новые строки)
3. Поменять DATABASE_URL → PostgreSQL
4. Деплой новой версии
5. Smoke-test: 10 min мониторинг логов
6. Выключить maintenance mode
7. Мониторить 48h: ошибки, latency, lock-waits
```

---

### Фаза 6: Cleanup — 1 неделя

```
- Удалить sqlite3 зависимости из database.py
- Удалить _get_pooled_conn, ConnectionWrapper
- Удалить PRAGMA WAL из db_utils.py и web/deps.py
- Удалить data/tenants/*.db из деплоя (оставить бэкап)
- Настроить pg_dump для автобэкапа
- Добавить PG-специфичные индексы (GIN для JSON, partial indexes)
```

---

## Риски и митигация

| Риск | Вероятность | Митигация |
|---|---|---|
| Row-tuple breaking (index access) | Высокая | Тесты на каждый метод перед переключением |
| Placeholder mismatch (`?` vs `$1`) | Высокая | `to_pg_placeholders()` + CI-проверка |
| Потеря данных при миграции | Средняя | Dual-write + row-count verification |
| FSM state loss при переключении | Низкая | Migrate FSM DB отдельно, users просто нажмут /start |
| PG connection limit exceeded | Средняя | `max_size=20` в пуле, pg_bouncer при росте |
| Prod downtime >1h | Низкая | Zero-downtime подход через dual-write |
| asyncpg несовместимость с sync-кодом | Высокая | Вся web-layer уже использует `await`, bot-handlers тоже |

---

## Оценка трудозатрат

| Фаза | Оценка | Критический путь |
|---|---|---|
| 0: Подготовка | 1–2 нед | Настройка Alembic, pg_pool, env |
| 1: Dual-write | 2–4 нед | Покрытие write-методов |
| 2: Рефакторинг (279 методов) | 4–8 нед | Самая большая фаза |
| 3: FSM storage | 1 нед | |
| 4: Миграция данных | 1–2 дня | Downtime window |
| 5: Переключение | 1 день | |
| 6: Cleanup | 1 нед | |
| **Итого** | **~12–18 нед** | При работе одного разработчика full-time |

---

## Не-очевидные gotchas

1. **`asyncpg.Record` vs `tuple`**: `row[0]` работает, но `row[:-1]` — нет. Нужно `tuple(row)[:-1]`.
2. **`asyncpg` не принимает `None` для `IN (?)`**: нужен `ANY($1::int[])` вместо `IN (?)`.
3. **Transaction isolation**: SQLite implicit commit vs PG explicit `async with conn.transaction()`.
4. **`RETURNING id`**: в SQLite — `cursor.lastrowid`; в PG — `INSERT ... RETURNING id` в fetchval.
5. **Schema search_path в пуле**: при connection pool search_path не персистируется между acquire(); нужно устанавливать в каждом запросе или в pool `server_settings`.
6. **Amvera**: PostgreSQL как отдельный сервис (не persistenceMount SQLite); нужен managed PG или docker-compose.

---

**Why:**
SQLite работает отлично до ~50 одновременных пользователей. При масштабировании на > 50 активных пользователей / множество оргов начинается lock-contention и невозможность горизонтального масштабирования. PostgreSQL снимает этот потолок полностью.

**How to apply:**
Начинать Фазу 0 только после достижения ≥30 платящих организаций или появления реальных симптомов lock-contention в логах. Не мигрировать преждевременно.
