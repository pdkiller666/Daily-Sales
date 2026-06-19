"""
Модуль для работы с базой данных
"""
import sqlite3
import os
import logging
import threading
import time as _time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Сентинел для параметра username в update_user(): позволяет явно
# передать username=None (→ запись NULL) в отличие от «не передавать» (→ без изменений).
_UNSET = object()

# Кеш инициализированных БД: create_tables() выполняется только ОДИН РАЗ
# на каждый путь БД за время жизни процесса. Перезапуск сбрасывает кеш.
_INITIALIZED_DBS: set = set()

# Кеш get_user_timezone(): {(db_file, telegram_id): (tz_str, timestamp)}
# Часовой пояс меняется крайне редко, TTL 300 сек.
_tz_cache: dict = {}
_TZ_CACHE_TTL = 300

# ─── Thread-local connection pool ──────────────────────────────────────────
# asyncio.to_thread() запускает каждый вызов в пуле воркер-потоков.
# _conn_pool хранит по одному SQLite-соединению на (поток × db_file).
# Это устраняет overhead открытия нового соединения (~3-10 мс) при каждом
# вызове метода Database.
_conn_pool = threading.local()


def _get_pooled_conn(db_file: str, timeout: float = 30.0) -> sqlite3.Connection:
    """Вернуть переиспользуемое соединение для текущего потока.

    Первый вызов открывает соединение и настраивает PRAGMAs.
    Последующие вызовы из того же потока возвращают уже открытое соединение.
    """
    pool = getattr(_conn_pool, 'conns', None)
    if pool is None:
        _conn_pool.conns = {}
        pool = _conn_pool.conns

    conn = pool.get(db_file)
    if conn is None:
        conn = sqlite3.connect(db_file, timeout=timeout, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.create_function("lower_u", 1, lambda s: s.lower() if s else s)
        pool[db_file] = conn
    return conn


class _PooledConn:
    """Тонкая обёртка над sqlite3.Connection из пула.

    close() не закрывает реальное соединение — только откатывает незакрытые
    транзакции, чтобы следующий вызов из того же потока не видел «грязного» стейта.
    Все остальные методы проксируются к оригинальному объекту.
    """
    __slots__ = ('_c',)

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, '_c', conn)

    def close(self) -> None:
        try:
            c = object.__getattribute__(self, '_c')
            if c.in_transaction:
                c.rollback()
        except Exception as _exc:
            logger.debug("close: подавлено исключение: %s", _exc)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, '_c'), name)

    def __enter__(self):
        return object.__getattribute__(self, '_c').__enter__()

    def __exit__(self, *args):
        return object.__getattribute__(self, '_c').__exit__(*args)

    def cursor(self):
        return object.__getattribute__(self, '_c').cursor()

    def commit(self):
        return object.__getattribute__(self, '_c').commit()

    def rollback(self):
        return object.__getattribute__(self, '_c').rollback()

    def execute(self, *args, **kwargs):
        return object.__getattribute__(self, '_c').execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return object.__getattribute__(self, '_c').executemany(*args, **kwargs)


class Database:
    def __init__(self, db_file=None):
        self.db_file = db_file or 'data/shop_bot.db'
        # Создаем директорию, если она не существует
        db_dir = os.path.dirname(self.db_file)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
        # self.create_tables()  # Убираем автоматический вызов здесь, так как мы копируем уже готовую структуру

    def get_connection(self):
        """Получить соединение с БД из thread-local пула.

        Возвращает _PooledConn — обёртку, у которой close() не разрывает
        соединение, а только откатывает незакрытые транзакции.
        """
        raw = _get_pooled_conn(self.db_file)
        return _PooledConn(raw)

    def create_tables(self):
        """Создание таблиц в базе данных.

        Выполняется только один раз на каждый путь БД за время жизни процесса
        (кеш _INITIALIZED_DBS). Повторные вызовы — мгновенный return.
        """
        if self.db_file in _INITIALIZED_DBS:
            return
        conn = self.get_connection()
        cursor = conn.cursor()

        # Основные таблицы
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                middle_name TEXT,
                phone TEXT,
                email TEXT,
                trade_network TEXT,
                shop_name TEXT,
                city TEXT,
                timezone TEXT DEFAULT 'Europe/Moscow',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                username TEXT
            )
        ''')
        

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                price REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_name TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products (id),
                UNIQUE(shop_name, product_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS inventory_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_name TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                old_quantity INTEGER,
                new_quantity INTEGER,
                delta INTEGER,
                change_type TEXT DEFAULT 'manual',
                change_reason TEXT,
                changed_by INTEGER,
                changed_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        # Добавляем username в users если отсутствует (миграция)
        cursor.execute("PRAGMA table_info(users)")
        users_cols = [col[1] for col in cursor.fetchall()]
        if 'username' not in users_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
        if 'mobile_nav' not in users_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN mobile_nav TEXT DEFAULT NULL")

        # Добавляем недостающие столбцы в таблицу inventory если их нет
        cursor.execute("PRAGMA table_info(inventory)")
        inventory_columns = [column[1] for column in cursor.fetchall()]

        if 'updated_by' not in inventory_columns:
            cursor.execute('ALTER TABLE inventory ADD COLUMN updated_by INTEGER')
        if 'change_type' not in inventory_columns:
            cursor.execute('ALTER TABLE inventory ADD COLUMN change_type TEXT DEFAULT "manual"')
        if 'change_reason' not in inventory_columns:
            cursor.execute('ALTER TABLE inventory ADD COLUMN change_reason TEXT')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                shop_name TEXT NOT NULL,
                quantity_sold INTEGER NOT NULL,
                sale_price REAL,
                user_id INTEGER NOT NULL,
                sale_date TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products (id),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_type TEXT NOT NULL,
                start_date TEXT DEFAULT CURRENT_TIMESTAMP,
                end_date TEXT NOT NULL,
                is_trial BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Добавляем is_trial в subscriptions если отсутствует (миграция)
        cursor.execute("PRAGMA table_info(subscriptions)")
        sub_cols = [col[1] for col in cursor.fetchall()]
        if 'is_trial' not in sub_cols:
            cursor.execute('ALTER TABLE subscriptions ADD COLUMN is_trial BOOLEAN DEFAULT FALSE')

        # Таблица дедупликации напоминаний об истечении подписки
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscription_reminder_log (
                user_id INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                subscription_end TEXT NOT NULL,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, threshold, subscription_end)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_type TEXT NOT NULL,
                amount REAL NOT NULL,
                payment_proof_file_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                processed_by INTEGER,
                promocode_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                low_stock_alerts BOOLEAN DEFAULT TRUE,
                daily_reports BOOLEAN DEFAULT FALSE,
                sales_alerts BOOLEAN DEFAULT TRUE,
                payment_alerts BOOLEAN DEFAULT TRUE,
                admin_notifications BOOLEAN DEFAULT TRUE,
                stock_threshold INTEGER DEFAULT 5,
                notification_time TEXT DEFAULT '09:00',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                notification_type TEXT NOT NULL,
                message TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_hints_seen (
                user_id INTEGER NOT NULL,
                hint_key TEXT NOT NULL,
                seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, hint_key),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                changed_by_user_id INTEGER,
                old_quantity INTEGER,
                new_quantity INTEGER,
                old_price REAL,
                new_price REAL,
                changed_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL,
                field       TEXT NOT NULL,
                old_value   TEXT,
                new_value   TEXT,
                changed_by  INTEGER,
                changed_by_name TEXT,
                changed_at  TEXT DEFAULT (datetime('now'))
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_product_history_pid ON product_history(product_id, changed_at DESC)'
        )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                created_by INTEGER NOT NULL,
                notification_text TEXT NOT NULL,
                recipients_type TEXT NOT NULL,
                recipients_list TEXT,
                scheduled_datetime TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by) REFERENCES users (id)
            )
        ''')

        # Таблицы платежной системы
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payment_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS yookassa_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                yookassa_payment_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                plan_type TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                promocode_id INTEGER,
                is_scheduled INTEGER DEFAULT 0,
                schedule_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                duration_days INTEGER NOT NULL,
                price REAL NOT NULL,
                description TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Добавляем колонки лимитов если их нет
        cursor.execute("PRAGMA table_info(subscription_plans)")
        columns = [column[1] for column in cursor.fetchall()]

        if 'max_products' not in columns:
            cursor.execute('ALTER TABLE subscription_plans ADD COLUMN max_products INTEGER DEFAULT 50')
        if 'max_shops' not in columns:
            cursor.execute('ALTER TABLE subscription_plans ADD COLUMN max_shops INTEGER DEFAULT 1')
        if 'max_sales_per_month' not in columns:
            cursor.execute('ALTER TABLE subscription_plans ADD COLUMN max_sales_per_month INTEGER DEFAULT 100')
        if 'can_export_reports' not in columns:
            cursor.execute('ALTER TABLE subscription_plans ADD COLUMN can_export_reports BOOLEAN DEFAULT FALSE')
        if 'can_view_analytics' not in columns:
            cursor.execute('ALTER TABLE subscription_plans ADD COLUMN can_view_analytics BOOLEAN DEFAULT FALSE')
        if 'can_use_notifications' not in columns:
            cursor.execute('ALTER TABLE subscription_plans ADD COLUMN can_use_notifications BOOLEAN DEFAULT FALSE')
        if 'can_use_integrations' not in columns:
            cursor.execute('ALTER TABLE subscription_plans ADD COLUMN can_use_integrations BOOLEAN DEFAULT FALSE')
            # Сразу включаем для всех платных планов (Бесплатный остаётся 0)
            cursor.execute(
                "UPDATE subscription_plans SET can_use_integrations=1 WHERE name != 'Бесплатный'"
            )

        # Миграция таблицы products: фото, описание, артикул, штрихкод
        cursor.execute("PRAGMA table_info(products)")
        _prod_cols = [c[1] for c in cursor.fetchall()]
        if 'photo_file_id' not in _prod_cols:
            cursor.execute("ALTER TABLE products ADD COLUMN photo_file_id TEXT")
        if 'description' not in _prod_cols:
            cursor.execute("ALTER TABLE products ADD COLUMN description TEXT")
        if 'article' not in _prod_cols:
            cursor.execute("ALTER TABLE products ADD COLUMN article TEXT")
        if 'barcode' not in _prod_cols:
            cursor.execute("ALTER TABLE products ADD COLUMN barcode TEXT")
        if 'old_price' not in _prod_cols:
            cursor.execute("ALTER TABLE products ADD COLUMN old_price REAL")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode "
            "ON products(barcode) WHERE barcode IS NOT NULL AND barcode != ''"
        )

        # Варианты товара по торговой сети: один товар — разные артикул/штрихкод
        # в разных торговых сетях (DNS, М-Видео ...). products.article/barcode
        # остаются значениями по умолчанию (fallback) — полная обратная совместимость.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_network_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                trade_network TEXT NOT NULL,
                article TEXT,
                barcode TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pnv_product_network "
            "ON product_network_variants(product_id, trade_network)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pnv_barcode "
            "ON product_network_variants(barcode) "
            "WHERE barcode IS NOT NULL AND barcode != ''"
        )

        # Галерея фото товаров (единое хранилище бот+веб)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                photo_url TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                source TEXT DEFAULT 'web',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                discount_percent INTEGER NOT NULL,
                discount_type TEXT NOT NULL DEFAULT 'percent',
                usage_count INTEGER DEFAULT 0,
                max_usage INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                expires_at TEXT DEFAULT NULL,
                allowed_plans TEXT DEFAULT NULL,
                last_used_at TEXT DEFAULT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promocode_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                promocode_id INTEGER NOT NULL,
                used_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, promocode_id)
            )
        ''')

        # Система мотивации - мотивации по товарам
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_motivations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                motivation_type TEXT NOT NULL CHECK (motivation_type IN ('percentage', 'fixed')),
                motivation_value REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                FOREIGN KEY (product_id) REFERENCES products (id),
                FOREIGN KEY (created_by) REFERENCES users (id),
                UNIQUE(product_id)
            )
        ''')

        # Таргетированная мотивация по оргструктуре (task #49)
        # scope_type: global / trade_network / city / shop / user
        # scope_value: '' для global; имя сети/города/магазина; str(user_id) для user
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_motivation_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                scope_type TEXT NOT NULL DEFAULT 'global'
                    CHECK (scope_type IN ('global','trade_network','city','shop','user')),
                scope_value TEXT NOT NULL DEFAULT '',
                motivation_type TEXT NOT NULL CHECK (motivation_type IN ('percentage', 'fixed')),
                motivation_value REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                FOREIGN KEY (product_id) REFERENCES products (id),
                FOREIGN KEY (created_by) REFERENCES users (id),
                UNIQUE(product_id, scope_type, scope_value)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pmr_product ON product_motivation_rules(product_id)')
        # Автомиграция: старые глобальные мотивации → product_motivation_rules (scope=global)
        # INSERT OR IGNORE идемпотентен: повторный запуск при старте не перезапишет уже изменённые ставки
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO product_motivation_rules
                    (product_id, scope_type, scope_value, motivation_type, motivation_value, created_at, created_by)
                SELECT product_id, 'global', '', motivation_type, motivation_value, created_at, created_by
                FROM product_motivations
            ''')
        except Exception as _exc:
            logger.debug("create_tables: миграция product_motivations подавлена: %s", _exc)

        # Оклады: дневные ставки сотрудников
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS salary_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                daily_rate REAL NOT NULL DEFAULT 0,
                updated_by INTEGER,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        # Оклады: табель рабочих смен
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS work_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                marked_by INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, work_date)
            )
        ''')

        # Корректировки зарплат: ручные бонусы и штрафы
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS salary_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                amount REAL NOT NULL,
                comment TEXT,
                created_by INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        # Планы продаж
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_type TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                target_value REAL NOT NULL,
                target_type TEXT NOT NULL,
                user_id INTEGER,
                shop_name TEXT,
                filter_type TEXT DEFAULT 'all',
                filter_value TEXT,
                is_active INTEGER DEFAULT 1,
                created_by INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        # Дополнительные условия мотивации (коэффициенты смены и фильтры категорий)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS motivation_extra_conditions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_type TEXT NOT NULL,
                description TEXT,
                shop_name TEXT,
                min_sellers INTEGER,
                coefficient REAL,
                user_id INTEGER,
                allowed_categories TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                calc_mode TEXT DEFAULT 'individual'
            )
        ''')
        # Миграция: добавить calc_mode если отсутствует
        try:
            cursor.execute("ALTER TABLE motivation_extra_conditions ADD COLUMN calc_mode TEXT DEFAULT 'individual'")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # Миграция: добавить shift_sale_alerts в notification_settings если отсутствует
        try:
            cursor.execute("ALTER TABLE notification_settings ADD COLUMN shift_sale_alerts BOOLEAN DEFAULT TRUE")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # Миграция: зависимость мотивации от выполнения недельных планов
        try:
            cursor.execute("ALTER TABLE notification_settings ADD COLUMN plan_coeff_enabled BOOLEAN DEFAULT FALSE")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)
        try:
            cursor.execute("ALTER TABLE notification_settings ADD COLUMN plan_coeff_cap BOOLEAN DEFAULT TRUE")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # Миграция: уведомление о начале смены (opt-out, по умолчанию включено)
        try:
            cursor.execute("ALTER TABLE notification_settings ADD COLUMN shift_reminders BOOLEAN DEFAULT TRUE")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # Миграция: авто-создание задач при низком остатке (tasks_pro, opt-in)
        try:
            cursor.execute("ALTER TABLE notification_settings ADD COLUMN auto_tasks_low_stock BOOLEAN DEFAULT FALSE")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # Миграция: контекстные блоки AI-дайджеста
        try:
            cursor.execute(
                "ALTER TABLE ai_alert_settings ADD COLUMN digest_context TEXT DEFAULT '[\"products\",\"sellers\",\"plans\"]'"
            )
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # Расписание мотивации по месяцам
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS motivation_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                motivation_type TEXT NOT NULL CHECK (motivation_type IN ('percentage', 'fixed')),
                motivation_value REAL NOT NULL,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products (id),
                FOREIGN KEY (created_by) REFERENCES users (id),
                UNIQUE(product_id, year, month)
            )
        ''')

        # Расписание доп. условий мотивации по месяцам
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS extra_conditions_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_type TEXT NOT NULL,
                description TEXT,
                shop_name TEXT,
                min_sellers INTEGER,
                coefficient REAL,
                user_id INTEGER,
                allowed_categories TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                calc_mode TEXT DEFAULT 'individual',
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                created_by INTEGER
            )
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS uix_extra_cond_schedule
            ON extra_conditions_schedule(
                condition_type,
                COALESCE(shop_name, ''),
                COALESCE(CAST(user_id AS TEXT), ''),
                year,
                month
            )
        ''')

        # Архив мотиваций — история изменений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS motivation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                motivation_type TEXT NOT NULL,
                motivation_value REAL NOT NULL,
                changed_by INTEGER,
                changed_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (product_id) REFERENCES products (id)
            )
        ''')

        # Ручные корректировки результатов конкурса по магазинам
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contest_manual_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contest_id INTEGER NOT NULL,
                shop_name TEXT NOT NULL,
                manual_value REAL NOT NULL,
                edited_by INTEGER,
                edited_at TEXT DEFAULT (datetime('now')),
                UNIQUE(contest_id, shop_name)
            )
        ''')

        # Реферальная программа
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_telegram_id INTEGER NOT NULL,
                referred_telegram_id INTEGER NOT NULL UNIQUE,
                bonus_days INTEGER DEFAULT 30,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                applied INTEGER DEFAULT 0,
                applied_at TEXT
            )
        ''')

        # Надстройки к подписке (add-ons)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscription_addons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_telegram_id INTEGER NOT NULL,
                addon_type TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                price REAL NOT NULL,
                purchased_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                payment_request_id INTEGER,
                is_active INTEGER DEFAULT 1
            )
        ''')

        # Заработок продавцов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS seller_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                commission_amount REAL NOT NULL DEFAULT 0.0,
                motivation_type TEXT NOT NULL DEFAULT 'percentage',
                motivation_value REAL NOT NULL DEFAULT 0.0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sale_id) REFERENCES sales (id),
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (product_id) REFERENCES products (id)
            )
        ''')

        # Проверяем и очищаем структуру таблицы seller_earnings
        cursor.execute("PRAGMA table_info(seller_earnings)")
        earnings_columns = [column[1] for column in cursor.fetchall()]

        # Если есть старые столбцы commission, пересоздаем таблицу
        if 'commission_type' in earnings_columns or 'commission_value' in earnings_columns:
            logger.info("Обнаружены старые столбцы commission, пересоздаем таблицу...")
            
            # Сохраняем существующие данные
            cursor.execute('''
                CREATE TABLE seller_earnings_backup AS 
                SELECT sale_id, user_id, product_id, commission_amount,
                       COALESCE(motivation_type, commission_type, 'percentage') as motivation_type,
                       COALESCE(motivation_value, commission_value, 0.0) as motivation_value,
                       created_at
                FROM seller_earnings
            ''')
            
            # Удаляем старую таблицу
            cursor.execute('DROP TABLE seller_earnings')
            
            # Создаем новую таблицу с правильной структурой
            cursor.execute('''
                CREATE TABLE seller_earnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sale_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    commission_amount REAL NOT NULL DEFAULT 0.0,
                    motivation_type TEXT NOT NULL DEFAULT 'percentage',
                    motivation_value REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sale_id) REFERENCES sales (id),
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    FOREIGN KEY (product_id) REFERENCES products (id)
                )
            ''')
            
            # Восстанавливаем данные
            cursor.execute('''
                INSERT INTO seller_earnings (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value, created_at)
                SELECT sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value, created_at
                FROM seller_earnings_backup
            ''')
            
            # Удаляем временную таблицу
            cursor.execute('DROP TABLE seller_earnings_backup')
            
        else:
            # Добавляем недостающие столбцы если их нет
            if 'motivation_type' not in earnings_columns:
                cursor.execute('ALTER TABLE seller_earnings ADD COLUMN motivation_type TEXT NOT NULL DEFAULT "percentage"')
            if 'motivation_value' not in earnings_columns:
                cursor.execute('ALTER TABLE seller_earnings ADD COLUMN motivation_value REAL NOT NULL DEFAULT 0.0')

        # Источник ставки мотивации (task #49): global/trade_network/city/shop/user/schedule
        cursor.execute("PRAGMA table_info(seller_earnings)")
        _se_cols = [c[1] for c in cursor.fetchall()]
        if 'motivation_source' not in _se_cols:
            try:
                cursor.execute("ALTER TABLE seller_earnings ADD COLUMN motivation_source TEXT DEFAULT 'global'")
            except Exception as _exc:
                logger.debug("create_tables: ALTER seller_earnings motivation_source подавлено: %s", _exc)

        conn.commit()

        # --- Миграции: добавляем колонки которых нет в существующих БД ---
        cursor.execute("PRAGMA table_info(payment_requests)")
        pr_cols = [c[1] for c in cursor.fetchall()]
        if 'promocode_id' not in pr_cols:
            cursor.execute("ALTER TABLE payment_requests ADD COLUMN promocode_id INTEGER")

        # Миграции для таблицы promocodes
        cursor.execute("PRAGMA table_info(promocodes)")
        promo_cols = [c[1] for c in cursor.fetchall()]
        if 'discount_type' not in promo_cols:
            cursor.execute("ALTER TABLE promocodes ADD COLUMN discount_type TEXT NOT NULL DEFAULT 'percent'")
        if 'expires_at' not in promo_cols:
            cursor.execute("ALTER TABLE promocodes ADD COLUMN expires_at TEXT DEFAULT NULL")
        if 'allowed_plans' not in promo_cols:
            cursor.execute("ALTER TABLE promocodes ADD COLUMN allowed_plans TEXT DEFAULT NULL")
        if 'last_used_at' not in promo_cols:
            cursor.execute("ALTER TABLE promocodes ADD COLUMN last_used_at TEXT DEFAULT NULL")

        # Миграция: создаём promocode_usage_log если отсутствует
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='promocode_usage_log'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE promocode_usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    promocode_id INTEGER NOT NULL,
                    used_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, promocode_id)
                )
            ''')

        # Миграция: создаём sales_plans если отсутствует
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sales_plans'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE sales_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_type TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    target_value REAL NOT NULL,
                    target_type TEXT NOT NULL,
                    user_id INTEGER,
                    shop_name TEXT,
                    filter_type TEXT DEFAULT 'all',
                    filter_value TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_by INTEGER,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')

        # Миграция: создаём motivation_extra_conditions если отсутствует
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='motivation_extra_conditions'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE motivation_extra_conditions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_type TEXT NOT NULL,
                    description TEXT,
                    shop_name TEXT,
                    min_sellers INTEGER,
                    coefficient REAL,
                    user_id INTEGER,
                    allowed_categories TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')

        # Миграция: создаём salary_settings если отсутствует
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='salary_settings'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE salary_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    daily_rate REAL NOT NULL DEFAULT 0,
                    updated_by INTEGER,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            ''')

        # Миграция: создаём work_schedule если отсутствует
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='work_schedule'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE work_schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    work_date TEXT NOT NULL,
                    marked_by INTEGER,
                    start_time TEXT,
                    end_time TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, work_date)
                )
            ''')

        # Миграция: добавляем time-колонки в work_schedule (для старых БД)
        for _col in ('start_time', 'end_time'):
            try:
                cursor.execute(f'ALTER TABLE work_schedule ADD COLUMN {_col} TEXT')
            except Exception as _exc:
                logger.debug("create_tables: подавлено исключение: %s", _exc)  # колонка уже существует

        # Шаблоны смен по дням недели
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shift_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                start_time TEXT,
                end_time TEXT,
                UNIQUE(user_id, weekday)
            )
        ''')

        # ── Настройки типов отсутствий (per-org) ────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS absence_type_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL UNIQUE,
                is_paid INTEGER DEFAULT 1,
                annual_limit INTEGER DEFAULT 0,
                penalty_mode TEXT DEFAULT 'none',
                penalty_amount REAL DEFAULT 0.0,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        for _atp, _pd, _pm in [
            ('vacation',     1, 'none'),
            ('sick',         1, 'none'),
            ('compensatory', 1, 'none'),
            ('absence',      0, 'no_pay'),
            ('other',        1, 'none'),
        ]:
            cursor.execute(
                'INSERT OR IGNORE INTO absence_type_settings '
                '(type, is_paid, penalty_mode) VALUES (?, ?, ?)',
                (_atp, _pd, _pm)
            )

        # ── Записи об отсутствиях ────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS absence_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                is_paid INTEGER,
                comment TEXT,
                admin_comment TEXT,
                created_by INTEGER,
                reviewed_by INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_absence_user '
            'ON absence_records(user_id, start_date)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_absence_status '
            'ON absence_records(status)'
        )

        # ── Общие настройки организации (key-value) ─────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS org_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                contest_type TEXT NOT NULL DEFAULT 'any',
                metric_type TEXT NOT NULL DEFAULT 'turnover',
                target_value REAL NOT NULL DEFAULT 0,
                reward_type TEXT NOT NULL DEFAULT 'fixed',
                reward_value REAL NOT NULL DEFAULT 0,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                shop_filter TEXT,
                city_filter TEXT,
                user_filter TEXT,
                product_filter TEXT,
                category_filter TEXT,
                extra_conditions TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                notify_on_start INTEGER NOT NULL DEFAULT 0,
                notify_on_end INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contest_salary_payouts (
                contest_id INTEGER PRIMARY KEY,
                paid_at    TEXT DEFAULT (datetime('now'))
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_type TEXT NOT NULL,
                start_date TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Избранные товары продавца
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, product_id)
            )
        ''')

        # Журнал алертов о достижении milestone планов (50/75/100%)
        # UNIQUE по (user_id, plan_id, milestone, period_start) — алерт может повторяться каждый период
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plan_milestone_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                period_start TEXT NOT NULL DEFAULT '',
                alerted_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, plan_id, milestone, period_start)
            )
        ''')

        # Миграция: добавляем period_start если его нет (старая схема без него)
        cursor.execute("PRAGMA table_info(plan_milestone_alerts)")
        _pma_cols = {row[1] for row in cursor.fetchall()}
        if 'period_start' not in _pma_cols:
            # Добавляем колонку через ALTER TABLE — сохраняем историю алертов
            try:
                cursor.execute(
                    "ALTER TABLE plan_milestone_alerts "
                    "ADD COLUMN period_start TEXT NOT NULL DEFAULT ''"
                )
                logger.info("plan_milestone_alerts: добавлена колонка period_start")
            except sqlite3.OperationalError:
                pass  # колонка уже существует

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS integration_connections (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                provider   TEXT    NOT NULL DEFAULT 'google_sheets',
                config     TEXT    NOT NULL DEFAULT '{}',
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    DEFAULT (datetime('now')),
                updated_at TEXT    DEFAULT (datetime('now'))
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS integration_exports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER NOT NULL,
                export_type   TEXT    NOT NULL,
                enabled       INTEGER NOT NULL DEFAULT 1,
                schedule      TEXT,
                target_sheet  TEXT,
                operation     TEXT    NOT NULL DEFAULT 'append_row',
                mapping       TEXT    DEFAULT '{}',
                lookup_config TEXT    DEFAULT '{}',
                extra         TEXT    DEFAULT '{}',
                last_run      TEXT,
                created_at    TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (connection_id) REFERENCES integration_connections(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS integration_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER,
                export_id     INTEGER,
                status        TEXT NOT NULL,
                message       TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gs_bonus_cache (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER,
                model_name    TEXT NOT NULL,
                chain         TEXT NOT NULL,
                bonus         REAL NOT NULL DEFAULT 0,
                rrp           REAL,
                synced_at     TEXT DEFAULT (datetime('now')),
                UNIQUE(connection_id, model_name, chain)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shops (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        ''')
        try:
            cursor.execute("ALTER TABLE shops ADD COLUMN city TEXT DEFAULT ''")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)
        try:
            cursor.execute("ALTER TABLE shops ADD COLUMN trade_network TEXT DEFAULT ''")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)
        try:
            cursor.execute("ALTER TABLE shops ADD COLUMN notes TEXT DEFAULT ''")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # ── Chat topics ───────────────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_topics (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL DEFAULT 'Общий',
                created_by INTEGER,
                created_at TEXT    DEFAULT (datetime('now')),
                is_archived INTEGER DEFAULT 0,
                sort_order  INTEGER DEFAULT 0
            )
        ''')
        # Тема «Общий» — создаётся один раз при инициализации
        cursor.execute('''
            INSERT OR IGNORE INTO chat_topics (id, name, sort_order)
            VALUES (1, 'Общий', 0)
        ''')
        # Миграция: is_ai — выделенная тема «AI-ассистент» (любое сообщение → ответ AI)
        cursor.execute("PRAGMA table_info(chat_topics)")
        _ct_cols = [col[1] for col in cursor.fetchall()]
        if 'is_ai' not in _ct_cols:
            cursor.execute("ALTER TABLE chat_topics ADD COLUMN is_ai INTEGER DEFAULT 0")
        # Не более одной AI-темы (защита от гонки при первом создании)
        cursor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_topics_ai '
            'ON chat_topics(is_ai) WHERE is_ai = 1'
        )

        # ── Chat messages (internal org messenger) ───────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                topic_id   INTEGER DEFAULT 1,
                message    TEXT    DEFAULT '',
                file_path  TEXT    DEFAULT '',
                file_name  TEXT    DEFAULT '',
                file_type  TEXT    DEFAULT '',
                file_size  INTEGER DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now')),
                is_deleted INTEGER DEFAULT 0
            )
        ''')
        # Миграция: добавляем topic_id если колонки ещё нет (старые БД)
        try:
            cursor.execute('ALTER TABLE chat_messages ADD COLUMN topic_id INTEGER DEFAULT 1')
            conn.commit()
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # ── Direct messages (личные сообщения) ───────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS direct_messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                to_user_id   INTEGER NOT NULL,
                message      TEXT    DEFAULT '',
                file_path    TEXT    DEFAULT '',
                file_name    TEXT    DEFAULT '',
                file_type    TEXT    DEFAULT '',
                file_size    INTEGER DEFAULT 0,
                created_at   TEXT    DEFAULT (datetime('now')),
                is_read      INTEGER DEFAULT 0,
                is_deleted   INTEGER DEFAULT 0
            )
        ''')

        # Миграция: ai_peer_id — к какой переписке привязан ответ AI (from_user_id=0)
        cursor.execute("PRAGMA table_info(direct_messages)")
        _dm_cols = [col[1] for col in cursor.fetchall()]
        if 'ai_peer_id' not in _dm_cols:
            cursor.execute("ALTER TABLE direct_messages ADD COLUMN ai_peer_id INTEGER DEFAULT 0")

        # ── Chat message files (multi-file support, до 10 файлов на сообщение) ──
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_message_files (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                file_path  TEXT    NOT NULL DEFAULT '',
                file_name  TEXT    NOT NULL DEFAULT '',
                file_type  TEXT    NOT NULL DEFAULT '',
                file_size  INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_msg_files_msg ON chat_message_files(message_id)')

        # ── DM message files (multi-file support) ──────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dm_message_files (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                dm_id      INTEGER NOT NULL,
                file_path  TEXT    NOT NULL DEFAULT '',
                file_name  TEXT    NOT NULL DEFAULT '',
                file_type  TEXT    NOT NULL DEFAULT '',
                file_size  INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_dm_msg_files_dm ON dm_message_files(dm_id)')

        # ── Chat: deleted_at для live-распространения удалений по polling ───────
        try:
            cursor.execute('ALTER TABLE chat_messages ADD COLUMN deleted_at TEXT')
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_msg_deleted ON chat_messages(topic_id, is_deleted, deleted_at)')

        # ── AI-сессии: is_session_break, is_ai_summary ───────────────────────────
        cursor.execute("PRAGMA table_info(direct_messages)")
        _dm_cols2 = [col[1] for col in cursor.fetchall()]
        if 'is_session_break' not in _dm_cols2:
            cursor.execute("ALTER TABLE direct_messages ADD COLUMN is_session_break INTEGER DEFAULT 0")
        if 'is_ai_summary' not in _dm_cols2:
            cursor.execute("ALTER TABLE direct_messages ADD COLUMN is_ai_summary INTEGER DEFAULT 0")

        cursor.execute("PRAGMA table_info(chat_messages)")
        _cm_cols = [col[1] for col in cursor.fetchall()]
        if 'is_session_break' not in _cm_cols:
            cursor.execute("ALTER TABLE chat_messages ADD COLUMN is_session_break INTEGER DEFAULT 0")
        if 'is_ai_summary' not in _cm_cols:
            cursor.execute("ALTER TABLE chat_messages ADD COLUMN is_ai_summary INTEGER DEFAULT 0")

        # ── Chat read state (серверный учёт прочитанного по темам) ─────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_read_state (
                user_id      INTEGER NOT NULL,
                topic_id     INTEGER NOT NULL,
                last_read_id INTEGER NOT NULL DEFAULT 0,
                updated_at   TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, topic_id)
            )
        ''')

        # ── Task topics (категории задач) ─────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_topics (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                color      TEXT    DEFAULT 'blue',
                sort_order INTEGER DEFAULT 0,
                created_by INTEGER DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        ''')

        # ── Tasks ──────────────────────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                title                TEXT    NOT NULL,
                description          TEXT    DEFAULT '',
                topic_id             INTEGER DEFAULT NULL,
                created_by           INTEGER NOT NULL DEFAULT 0,
                assigned_to          INTEGER DEFAULT NULL,
                shop_id              INTEGER DEFAULT NULL,
                assigned_shop        TEXT    DEFAULT NULL,
                assign_all           INTEGER DEFAULT 0,
                priority             TEXT    DEFAULT 'normal',
                status               TEXT    DEFAULT 'new',
                deadline             TEXT    DEFAULT NULL,
                linked_chat_topic_id INTEGER DEFAULT NULL,
                created_at           TEXT    DEFAULT (datetime('now')),
                updated_at           TEXT    DEFAULT (datetime('now'))
            )
        ''')
        for _col, _def in [('assigned_shop',    'TEXT DEFAULT NULL'),
                            ('assign_all',      'INTEGER DEFAULT 0'),
                            ('recurrence',      'TEXT DEFAULT NULL'),
                            ('rating',          'INTEGER DEFAULT NULL'),
                            ('rating_comment',  'TEXT DEFAULT NULL')]:
            try:
                cursor.execute(f'ALTER TABLE tasks ADD COLUMN {_col} {_def}')
            except Exception as _exc:
                logger.debug("create_tables: подавлено исключение: %s", _exc)

        # ── Task checklist items ───────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_checklist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                is_done    INTEGER DEFAULT 0,
                done_by    INTEGER DEFAULT NULL,
                done_at    TEXT    DEFAULT NULL,
                sort_order INTEGER DEFAULT 0
            )
        ''')

        # ── Task comments ──────────────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        ''')

        # ── Task attachments (до 10 файлов любого формата на задачу) ──────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_attachments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL DEFAULT 0,
                file_path  TEXT    NOT NULL DEFAULT '',
                file_name  TEXT    NOT NULL DEFAULT '',
                file_type  TEXT    NOT NULL DEFAULT '',
                file_size  INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_attach_task ON task_attachments(task_id)')

        # ── Task per-user completions (командные задачи assign_all / shop) ────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_user_completions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id      INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                status       TEXT    DEFAULT 'done',
                completed_at TEXT    DEFAULT (datetime('now')),
                UNIQUE(task_id, user_id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tuc_task ON task_user_completions(task_id)')

        # ── Task history (per-org, аудит-лог изменений задач) ────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                user_id    INTEGER,
                action     TEXT    NOT NULL,
                old_val    TEXT,
                new_val    TEXT,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_th_task ON task_history(task_id)')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                remind_at  TEXT    NOT NULL,
                sent       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tr_remind ON task_reminders(sent, remind_at)')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_templates (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT    NOT NULL,
                description   TEXT    DEFAULT '',
                priority      TEXT    NOT NULL DEFAULT 'normal',
                checklist_json TEXT   DEFAULT '[]',
                topic_id      INTEGER DEFAULT NULL,
                created_by    INTEGER DEFAULT NULL,
                created_at    TEXT    DEFAULT (datetime('now'))
            )
        ''')

        # ── AI alerts log (per-org, история смарт-алертов и дайджестов) ─────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_alerts_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL DEFAULT 'alert',
                text       TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_ai_alerts_log_ts ON ai_alerts_log(created_at DESC)'
        )

        # ── AI smart alert settings (per-org, singleton row id=1) ─────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_alert_settings (
                id                 INTEGER PRIMARY KEY DEFAULT 1,
                enabled            INTEGER DEFAULT 1,
                threshold_pct      INTEGER DEFAULT 35,
                alert_hour_msk     INTEGER DEFAULT 10,
                metrics            TEXT    DEFAULT '["revenue"]',
                updated_at         TEXT    DEFAULT (datetime('now')),
                digest_context     TEXT    DEFAULT '["products","sellers","plans"]',
                digest_enabled     INTEGER DEFAULT 1,
                digest_day_of_week  INTEGER DEFAULT 0,
                digest_hour_msk     INTEGER DEFAULT 9,
                digest_push_enabled INTEGER DEFAULT 1,
                alert_push_enabled  INTEGER DEFAULT 1,
                alert_email_enabled INTEGER DEFAULT 0,
                digest_email_enabled INTEGER DEFAULT 0
            )
        ''')
        # Миграция: добавляем колонки дайджеста если их нет (существующие org БД)
        _ais_cols = {r[1] for r in cursor.execute("PRAGMA table_info(ai_alert_settings)").fetchall()}
        if 'digest_context' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN digest_context TEXT DEFAULT '[\"products\",\"sellers\",\"plans\"]'")
        if 'digest_enabled' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN digest_enabled INTEGER DEFAULT 1")
        if 'digest_day_of_week' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN digest_day_of_week INTEGER DEFAULT 0")
        if 'digest_hour_msk' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN digest_hour_msk INTEGER DEFAULT 9")
        if 'digest_push_enabled' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN digest_push_enabled INTEGER DEFAULT 1")
        if 'alert_push_enabled' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN alert_push_enabled INTEGER DEFAULT 1")
        if 'alert_email_enabled' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN alert_email_enabled INTEGER DEFAULT 0")
        if 'digest_email_enabled' not in _ais_cols:
            cursor.execute("ALTER TABLE ai_alert_settings ADD COLUMN digest_email_enabled INTEGER DEFAULT 0")

        # ── Гибкая оргструктура (Вариант A+B) ───────────────────────────────
        # Подразделения с иерархией (регион → город → магазин → отдел → команда)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS departments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                type          TEXT DEFAULT 'department',
                parent_id     INTEGER DEFAULT NULL,
                manager_tg_id INTEGER DEFAULT NULL,
                sort_order    INTEGER DEFAULT 0,
                is_active     INTEGER DEFAULT 1,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        ''')
        # Кастомные роли организации с набором прав (платный модуль org_structure)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS org_roles (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT NOT NULL,
                icon                TEXT DEFAULT '🎖️',
                color               TEXT DEFAULT 'slate',
                base_role           TEXT DEFAULT 'user',
                scope_type          TEXT DEFAULT NULL,
                scope_values        TEXT DEFAULT NULL,
                can_manage_users    INTEGER DEFAULT 0,
                can_manage_products INTEGER DEFAULT 0,
                can_view_salary     INTEGER DEFAULT 0,
                can_manage_salary   INTEGER DEFAULT 0,
                can_view_reports    INTEGER DEFAULT 0,
                can_manage_plans    INTEGER DEFAULT 0,
                modules             TEXT DEFAULT NULL,
                is_active           INTEGER DEFAULT 1,
                created_at          TEXT DEFAULT (datetime('now'))
            )
        ''')
        # Гранулярный доступ к модулям на уровне сотрудника (allow/deny override)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_module_access (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                module_key  TEXT NOT NULL,
                access      TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(telegram_id, module_key)
            )
        ''')

        conn.commit()

        # Удаляем осиротевшие записи motivation_schedule (товар уже удалён)
        cursor.execute('''
            DELETE FROM motivation_schedule
            WHERE product_id NOT IN (SELECT id FROM products)
        ''')

        # ── push_subscriptions (только shop_bot.db) ─────────────────────────
        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    endpoint    TEXT NOT NULL,
                    p256dh      TEXT NOT NULL,
                    auth        TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, endpoint)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_push_subs_user ON push_subscriptions(user_id)')

        # ── push_delivery_log (только shop_bot.db) ───────────────────────────
        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS push_delivery_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id  INTEGER NOT NULL,
                    title    TEXT,
                    sent_at  TEXT DEFAULT (datetime('now'))
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_push_log_user '
                'ON push_delivery_log(user_id, sent_at)'
            )

        # ── web_credentials (только shop_bot.db) ────────────────────────────
        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS web_credentials (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    email           TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    password_hash   TEXT NOT NULL,
                    telegram_id     INTEGER UNIQUE,
                    synthetic_tg_id INTEGER UNIQUE,
                    org_db          TEXT,
                    first_name      TEXT,
                    email_verified  INTEGER DEFAULT 0,
                    verify_token    TEXT,
                    verify_expires  INTEGER,
                    reset_token     TEXT,
                    reset_expires   INTEGER,
                    created_at      TEXT DEFAULT (datetime('now')),
                    last_login      TEXT
                )
            ''')

        # ── login_ips — новые IP-адреса для уведомлений (только shop_bot.db) ───
        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS login_ips (
                    telegram_id INTEGER NOT NULL,
                    ip          TEXT NOT NULL,
                    first_seen  TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, ip)
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_login_ips_user ON login_ips(telegram_id)'
            )

        # ── Биллинг: модули, расширения, пакеты, подписки (только shop_bot.db) ─
        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS billing_modules (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    key           TEXT UNIQUE NOT NULL,
                    name          TEXT NOT NULL,
                    icon          TEXT DEFAULT '📦',
                    description   TEXT DEFAULT '',
                    price_monthly REAL DEFAULT 0,
                    sort_order    INTEGER DEFAULT 0,
                    is_active     INTEGER DEFAULT 1,
                    features_json TEXT DEFAULT '[]',
                    updated_at    TEXT DEFAULT (datetime('now'))
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS billing_extensions (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    module_key    TEXT NOT NULL,
                    key           TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    icon          TEXT DEFAULT '⚡',
                    description   TEXT DEFAULT '',
                    price_monthly REAL DEFAULT 0,
                    sort_order    INTEGER DEFAULT 0,
                    is_active     INTEGER DEFAULT 1,
                    updated_at    TEXT DEFAULT (datetime('now')),
                    UNIQUE(module_key, key)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS billing_bundles (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    key           TEXT UNIQUE NOT NULL,
                    name          TEXT NOT NULL,
                    icon          TEXT DEFAULT '🎁',
                    description   TEXT DEFAULT '',
                    includes_json TEXT NOT NULL DEFAULT '{"modules":[],"extensions":[]}',
                    price_monthly REAL DEFAULT 0,
                    sort_order    INTEGER DEFAULT 0,
                    is_active     INTEGER DEFAULT 1,
                    updated_at    TEXT DEFAULT (datetime('now'))
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS billing_module_subs (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_telegram_id   INTEGER NOT NULL,
                    item_type          TEXT NOT NULL,
                    item_key           TEXT NOT NULL,
                    quantity           INTEGER DEFAULT 1,
                    price_paid         REAL DEFAULT 0,
                    start_date         TEXT DEFAULT (datetime('now')),
                    end_date           TEXT,
                    is_active          INTEGER DEFAULT 1,
                    payment_request_id INTEGER,
                    granted_by         TEXT DEFAULT 'payment',
                    note               TEXT DEFAULT '',
                    created_at         TEXT DEFAULT (datetime('now'))
                )
            ''')

        # ── Индексы для ускорения тяжёлых запросов ──────────────────────────
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_user_date     ON sales(user_id, sale_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_shop_date     ON sales(shop_name, sale_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_date          ON sales(sale_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_product       ON sales(product_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_product_date  ON sales(product_id, sale_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_inventory_shop_prod ON inventory(shop_name, product_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_work_schedule_date  ON work_schedule(work_date, user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_seller_earnings_sale ON seller_earnings(sale_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_shop_name     ON users(shop_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram_id   ON users(telegram_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_city          ON users(city)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_trade_network ON users(trade_network)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_products_category   ON products(category)')
        cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_products_article ON products(article) WHERE article IS NOT NULL')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_user  ON subscriptions(user_id, end_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_plans_user    ON sales_plans(user_id, target_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_notif_history_user  ON notification_history(user_id, is_read)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sched_notif_dt      ON scheduled_notifications(scheduled_datetime, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_plan_milestones     ON plan_milestone_alerts(user_id, plan_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_referrals_referrer  ON referrals(referrer_telegram_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_addons_user         ON subscription_addons(user_telegram_id, is_active, expires_at)')
        try:
            cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_addons_payment ON subscription_addons(payment_request_id) WHERE payment_request_id IS NOT NULL')
        except Exception as _e:
            # legacy-данные с дублями payment_request_id — не валим миграции, лог + не-уникальный fallback
            logger.warning("idx_addons_payment UNIQUE не создан (legacy дубли?): %s", _e)
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_user    ON chat_messages(user_id, created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_id      ON chat_messages(id, is_deleted)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_topic   ON chat_messages(topic_id, id, is_deleted)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_topics_archived  ON chat_topics(is_archived, sort_order)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_dm_from_to    ON direct_messages(from_user_id, to_user_id, id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_dm_to_unread  ON direct_messages(to_user_id, is_read, is_deleted)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_salary_adj_user_ym   ON salary_adjustments(user_id, year, month)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_seller_earnings_user  ON seller_earnings(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_absence_rec_user_dt   ON absence_records(user_id, status, start_date, end_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_work_sched_user_date  ON work_schedule(user_id, work_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tasks_assigned_status  ON tasks(assigned_to, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tasks_created_by       ON tasks(created_by, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_checklist_task    ON task_checklist(task_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_comments_task     ON task_comments(task_id, created_at)')

        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ai_insights_cache (
                    tg_id        INTEGER PRIMARY KEY,
                    insights_text TEXT NOT NULL,
                    generated_at  TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ai_network_digest_prefs (
                    tg_id         INTEGER PRIMARY KEY,
                    weekday       INTEGER DEFAULT 0,
                    hour_msk      INTEGER DEFAULT 12,
                    updated_at    TEXT DEFAULT (datetime(\'now\'))
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ai_weekly_digest_cache (
                    org_db       TEXT PRIMARY KEY,
                    digest_text  TEXT NOT NULL,
                    generated_at TEXT NOT NULL
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_billing_msubs_user ON billing_module_subs(user_telegram_id, is_active, end_date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_billing_msubs_key  ON billing_module_subs(item_key, is_active)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_billing_ext_mod    ON billing_extensions(module_key)')
            # Migration: fix all_in_one bundle to include chat module (was missing)
            cursor.execute(
                """UPDATE billing_bundles
                   SET includes_json='{"modules":["analytics","team","notifications","plans_motivation","ai_assistant","integrations","chat"],"extensions":[]}',
                       description='Все 7 модулей — максимальный функционал'
                   WHERE key='all_in_one'
                     AND includes_json NOT LIKE '%"chat"%'"""
            )
            # Migration: billing_extensions — change UNIQUE(key) → UNIQUE(module_key, key)
            # so the same extension key can appear under multiple modules (e.g. ai_smart_alerts
            # shown under both analytics and ai_assistant).
            _ext_schema = (cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='billing_extensions'"
            ).fetchone() or ("",))[0]
            # Old schema had "key TEXT UNIQUE NOT NULL"; new has "UNIQUE(module_key, key)"
            _has_old_unique = "key           TEXT UNIQUE" in _ext_schema or "key TEXT UNIQUE" in _ext_schema
            if _has_old_unique:
                # Recreate table with new constraint; preserve all existing rows
                cursor.execute("ALTER TABLE billing_extensions RENAME TO billing_extensions_old")
                cursor.execute("""
                    CREATE TABLE billing_extensions (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        module_key    TEXT NOT NULL,
                        key           TEXT NOT NULL,
                        name          TEXT NOT NULL,
                        icon          TEXT DEFAULT '⚡',
                        description   TEXT DEFAULT '',
                        price_monthly REAL DEFAULT 0,
                        sort_order    INTEGER DEFAULT 0,
                        is_active     INTEGER DEFAULT 1,
                        updated_at    TEXT DEFAULT (datetime('now')),
                        UNIQUE(module_key, key)
                    )""")
                cursor.execute("""
                    INSERT OR IGNORE INTO billing_extensions
                        (id, module_key, key, name, icon, description,
                         price_monthly, sort_order, is_active, updated_at)
                    SELECT id, module_key, key, name, icon, description,
                           price_monthly, sort_order, is_active, updated_at
                    FROM billing_extensions_old""")
                cursor.execute("DROP TABLE billing_extensions_old")
                # Now add ai_smart_alerts under analytics (was blocked by old UNIQUE)
                cursor.execute("""
                    INSERT OR IGNORE INTO billing_extensions
                        (module_key, key, name, icon, description, price_monthly, sort_order, is_active)
                    VALUES ('analytics','ai_smart_alerts','Умные алерты AI','🤖',
                            'AI-детектирование аномалий в продажах', 199, 6, 1)""")
            # Migration: hide gs_realtime (not yet implemented, no gate checks in code)
            cursor.execute(
                "UPDATE billing_extensions SET is_active=0 WHERE key='gs_realtime'"
            )

        # ── download_events (только shop_bot.db) ─────────────────────────────
        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS download_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  TEXT    NOT NULL DEFAULT (datetime('now')),
                    referrer   TEXT    DEFAULT '',
                    user_agent TEXT    DEFAULT ''
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_download_events_ts ON download_events(timestamp)'
            )
            for _col, _def in [('telegram_id', "TEXT DEFAULT ''"), ('owner_id', "TEXT DEFAULT ''")]:
                try:
                    cursor.execute(f'ALTER TABLE download_events ADD COLUMN {_col} {_def}')
                except Exception:
                    pass
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS apk_release_history (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    version      TEXT    NOT NULL,
                    release_url  TEXT    DEFAULT '',
                    release_date TEXT    DEFAULT '',
                    recorded_at  TEXT    NOT NULL DEFAULT (datetime('now'))
                )
            ''')

        # ── AI rate-limit config + tool stats (только shop_bot.db) ─────────────
        if 'shop_bot' in self.db_file:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ai_rate_config (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            ''')
            for _k, _v in [
                ('base_daily_limit', '20'),
                ('high_daily_limit', '200'),
            ]:
                cursor.execute(
                    'INSERT OR IGNORE INTO ai_rate_config (key, value) VALUES (?, ?)',
                    (_k, _v)
                )
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ai_tool_stats (
                    tool_name  TEXT NOT NULL,
                    org_db     TEXT NOT NULL DEFAULT '',
                    date       TEXT NOT NULL,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tool_name, org_db, date)
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_ai_tool_stats_date ON ai_tool_stats(date)'
            )

        # ── Настройки дизайна ценника (per-org, singleton row id=1) ───────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS org_label_settings (
                id          INTEGER PRIMARY KEY DEFAULT 1,
                bg_color    TEXT    DEFAULT '#ffffff',
                text_color  TEXT    DEFAULT '#000000',
                price_color TEXT    DEFAULT '#000000',
                logo_path   TEXT    DEFAULT '',
                font_size   TEXT    DEFAULT 'medium',
                updated_at  TEXT    DEFAULT (datetime('now'))
            )
        ''')
        _label_alters = [
            "ALTER TABLE org_label_settings ADD COLUMN org_logo_path   TEXT DEFAULT ''",
            "ALTER TABLE org_label_settings ADD COLUMN font_family      TEXT DEFAULT 'Arial, Helvetica, sans-serif'",
            "ALTER TABLE org_label_settings ADD COLUMN border_color     TEXT DEFAULT '#cccccc'",
            "ALTER TABLE org_label_settings ADD COLUMN border_width     TEXT DEFAULT '1'",
            "ALTER TABLE org_label_settings ADD COLUMN label_theme      TEXT DEFAULT 'standard'",
            "ALTER TABLE org_label_settings ADD COLUMN element_order    TEXT DEFAULT ''",
            "ALTER TABLE org_label_settings ADD COLUMN visible_elements TEXT DEFAULT ''",
            "ALTER TABLE org_label_settings ADD COLUMN sale_badge       TEXT DEFAULT ''",
            "ALTER TABLE org_label_settings ADD COLUMN qr_content       TEXT DEFAULT ''",
        ]
        for _sql in _label_alters:
            try:
                cursor.execute(_sql)
            except Exception as _exc:
                logger.debug("create_tables: подавлено исключение: %s", _exc)

        # ── Именованные пресеты дизайна ценника (per-org, много строк) ────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS org_label_presets (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                bg_color         TEXT    DEFAULT '#ffffff',
                text_color       TEXT    DEFAULT '#000000',
                price_color      TEXT    DEFAULT '#000000',
                logo_path        TEXT    DEFAULT '',
                font_size        TEXT    DEFAULT 'medium',
                font_family      TEXT    DEFAULT 'Arial, Helvetica, sans-serif',
                border_color     TEXT    DEFAULT '#cccccc',
                border_width     TEXT    DEFAULT '1',
                label_theme      TEXT    DEFAULT 'standard',
                element_order    TEXT    DEFAULT '',
                visible_elements TEXT    DEFAULT '',
                sale_badge       TEXT    DEFAULT '',
                qr_content       TEXT    DEFAULT '',
                created_at       TEXT    DEFAULT (datetime('now')),
                updated_at       TEXT    DEFAULT (datetime('now'))
            )
        ''')
        # Бэкфилл колонки для старых БД, где таблица уже существовала без qr_content
        # (для новых БД колонка уже есть в CREATE выше → ALTER подавляется)
        try:
            cursor.execute(
                "ALTER TABLE org_label_presets ADD COLUMN qr_content TEXT DEFAULT ''")
        except Exception as _exc:
            logger.debug("create_tables: подавлено исключение: %s", _exc)

        # Инициализация базовых данных при первом запуске
        self._initialize_default_data(cursor)

        conn.commit()
        conn.close()

        # Помечаем БД как инициализированную — следующие вызовы create_tables()
        # для этого пути будут мгновенно возвращать return.
        _INITIALIZED_DBS.add(self.db_file)

    def _initialize_default_data(self, cursor):
        """Инициализация базовых данных при первом создании базы"""

        # Проверяем, есть ли уже планы подписок
        cursor.execute('SELECT COUNT(*) FROM subscription_plans')
        plans_count = cursor.fetchone()[0]

        if plans_count == 0:
            # Создаем стандартные планы подписок с лимитами
            default_plans = [
                ('Бесплатный', 0, 0.0, 'Объём: до 50 товаров, 1 магазин, 100 продаж/мес. Возможности подключаются модулями.', 50, 1, 100, False, False, False, False),
                ('Базовый', 30, 500.0, 'Объём: до 200 товаров, 3 магазина, 500 продаж/мес. Возможности подключаются модулями.', 200, 3, 500, True, True, True, False),
                ('Стандарт', 90, 1200.0, 'Объём: до 500 товаров, 10 магазинов, 1500 продаж/мес. Возможности подключаются модулями.', 500, 10, 1500, True, True, True, True),
                ('Премиум', 365, 4000.0, 'Объём: безлимит товаров, магазинов и продаж. Возможности подключаются модулями.', -1, -1, -1, True, True, True, True)
            ]

            for name, duration, price, description, max_products, max_shops, max_sales, can_export, can_analytics, can_notifications, can_integrations in default_plans:
                cursor.execute('''
                    INSERT INTO subscription_plans (name, duration_days, price, description, max_products, max_shops, max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications, can_use_integrations)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, duration, price, description, max_products, max_shops, max_sales, can_export, can_analytics, can_notifications, can_integrations))
        else:
            # Обновляем существующие планы, у которых могут отсутствовать лимиты
            cursor.execute('SELECT name FROM subscription_plans WHERE max_products IS NULL')
            plans_to_update = cursor.fetchall()

            for plan in plans_to_update:
                plan_name = plan[0]
                if plan_name == 'Бесплатный':
                    cursor.execute('''
                        UPDATE subscription_plans SET 
                        max_products=50, max_shops=1, max_sales_per_month=100,
                        can_export_reports=0, can_view_analytics=0, can_use_notifications=0,
                        can_use_integrations=0
                        WHERE name=?
                    ''', (plan_name,))
                elif plan_name == 'Базовый':
                    cursor.execute('''
                        UPDATE subscription_plans SET 
                        max_products=200, max_shops=3, max_sales_per_month=500,
                        can_export_reports=1, can_view_analytics=1, can_use_notifications=1,
                        can_use_integrations=0
                        WHERE name=?
                    ''', (plan_name,))
                elif plan_name == 'Стандарт':
                    cursor.execute('''
                        UPDATE subscription_plans SET 
                        max_products=500, max_shops=10, max_sales_per_month=1500,
                        can_export_reports=1, can_view_analytics=1, can_use_notifications=1,
                        can_use_integrations=1
                        WHERE name=?
                    ''', (plan_name,))
                elif plan_name in ['Премиум', 'VIP']:
                    cursor.execute('''
                        UPDATE subscription_plans SET 
                        max_products=-1, max_shops=-1, max_sales_per_month=-1,
                        can_export_reports=1, can_view_analytics=1, can_use_notifications=1,
                        can_use_integrations=1
                        WHERE name=?
                    ''', (plan_name,))

        # Всегда: Базовый не имеет доступа к интеграциям (Google Таблицы — от Стандарт+)
        cursor.execute(
            "UPDATE subscription_plans SET can_use_integrations=0 WHERE name='Базовый'"
        )
        # Монетизация «объём + возможности»: нормализуем устаревшие описания планов,
        # обещавшие функции (экспорт/аналитику/Google Таблицы/«все функции»).
        # Тариф = объём; возможности подключаются модулями. Обновляем ТОЛЬКО если
        # описание всё ещё совпадает со старым дефолтом (кастомные тексты не трогаем).
        _capacity_desc = {
            'Бесплатный': (
                'Объём: до 50 товаров, 1 магазин, 100 продаж/мес. Возможности подключаются модулями.',
                ['Ограниченный функционал: до 50 товаров, 1 магазин, без экспорта и уведомлений'],
            ),
            'Базовый': (
                'Объём: до 200 товаров, 3 магазина, 500 продаж/мес. Возможности подключаются модулями.',
                ['Для малого бизнеса: до 200 товаров, 3 магазина, экспорт отчётов. Без Google Таблиц'],
            ),
            'Стандарт': (
                'Объём: до 500 товаров, 10 магазинов, 1500 продаж/мес. Возможности подключаются модулями.',
                ['Для среднего бизнеса: до 500 товаров, 10 магазинов, расширенная аналитика + Google Таблицы'],
            ),
            'Премиум': (
                'Объём: безлимит товаров, магазинов и продаж. Возможности подключаются модулями.',
                ['Для крупного бизнеса: безлимит товаров и магазинов, все функции'],
            ),
        }
        for _pname, (_new_desc, _old_descs) in _capacity_desc.items():
            for _old in _old_descs:
                cursor.execute(
                    "UPDATE subscription_plans SET description=? WHERE name=? AND description=?",
                    (_new_desc, _pname, _old),
                )
        # Всегда: нормализуем устаревшее название пробного плана 'Бизнес' → 'Премиум'
        cursor.execute(
            "UPDATE payment_settings SET value='Премиум' WHERE key='trial_plan' AND value='Бизнес'"
        )

        # Проверяем, есть ли настройки платежей
        cursor.execute('SELECT COUNT(*) FROM payment_settings')
        settings_count = cursor.fetchone()[0]

        if settings_count == 0:
            # Создаем базовые настройки платежей
            default_settings = [
                ('card_number', '0000 0000 0000 0000'),
                ('recipient_name', 'Настройте в админ панели'),
                ('bank_name', 'Настройте в админ панели'),
                ('trial_days', '14'),
                ('trial_plan', 'Премиум'),
            ]

            for key, value in default_settings:
                cursor.execute('''
                    INSERT INTO payment_settings (key, value)
                    VALUES (?, ?)
                ''', (key, value))

        # Инициализируем данные биллинг-системы (только shop_bot.db)
        if 'shop_bot' in self.db_file:
            self._init_billing_defaults(cursor)

    def _ensure_billing_extra_modules(self, cursor):
        """Идемпотентно добавляет модули, появившиеся ПОСЛЕ первичной инициализации.

        _init_billing_defaults делает early-return если billing_modules не пуст,
        поэтому новые модули (например org_structure) на уже работающем продакшене
        не появятся. Этот метод вызывается всегда и докатывает их через INSERT OR IGNORE.
        """
        EXTRA_MODULES = [
            ('org_structure', '🏢 Оргструктура', '🏢',
             'Подразделения, регионы, кастомные роли, гранулярный доступ сотрудников', 349, 8),
            ('pos_retail', '🖥️ Касса и ценники', '🖥️',
             'POS-касса, штрихкоды, дизайн и печать ценников', 0, 9),
            ('tasks_pro', '📋 Задачи Pro', '📋',
             'Kanban-доска, аналитика, шаблоны, bulk-операции, Excel-экспорт, история', 249, 10),
        ]
        for key, name, icon, description, price, sort in EXTRA_MODULES:
            cursor.execute(
                'INSERT OR IGNORE INTO billing_modules (key,name,icon,description,price_monthly,sort_order,is_active) VALUES (?,?,?,?,?,?,1)',
                (key, name, icon, description, price, sort)
            )

    def _ensure_billing_extra_extensions(self, cursor):
        """Идемпотентно добавляет расширения, появившиеся ПОСЛЕ первичной инициализации.

        Вызывается всегда из _init_billing_defaults до early-return.
        """
        EXTRA_EXTENSIONS = [
            ('pos_retail', 'barcodes', '📦', 'Штрихкоды',
             'Генерация, сканирование и импорт артикулов/штрихкодов', 149, 1),
            ('pos_retail', 'labels', '🏷️', 'Ценники и печать',
             'Дизайн ценников, PDF-печать, логотип организации', 99, 2),
            # AI-расширения: гарантируем наличие на всех установках (в т.ч. существующих,
            # где DEFAULT_EXTENSIONS не запустился из-за early-return в _init_billing_defaults)
            ('ai_assistant', 'ai_forecast', '🔮', 'Прогноз продаж (AI)',
             'ML-прогноз продаж на 7–30 дней', 99, 1),
            ('ai_assistant', 'ai_smart_alerts', '🚨', 'Умные алерты AI',
             'AI-детектирование аномалий в продажах', 99, 2),
            ('ai_assistant', 'ai_high_limit', '⚡', 'Высокий лимит AI в день',
             'Увеличенный дневной лимит запросов к AI (настраивается супер-админом)', 149, 3),
            ('ai_assistant', 'ai_chat_assistant', '💬', 'AI-ассистент в чате',
             'Отвечает на вопросы участников прямо в org-чате по данным вашей организации', 199, 4),
            ('ai_assistant', 'ai_plan_analysis', '🔍', 'AI-разбор планов',
             'Анализирует причины невыполнения плана продаж — по дням, продавцам, категориям',
             99, 5),
            ('ai_assistant', 'ai_network_insights', '🌐', 'AI-инсайты сети',
             'Сравнительный AI-анализ по всем магазинам сети — тренды, возможности, риски',
             149, 6),
            # tasks_pro extensions
            ('tasks_pro', 'tasks_ai', '🤖', 'AI для задач',
             'AI-чеклист, AI-описание задачи, AI-декомпозиция цели, создание из бота на естественном языке', 199, 1),
            ('tasks_pro', 'tasks_digest', '📰', 'AI-дайджест задач',
             'Еженедельный AI-дайджест прогресса команды + предиктор просрочки', 149, 2),
        ]
        for module_key, key, icon, name, description, price, sort in EXTRA_EXTENSIONS:
            cursor.execute(
                'INSERT OR IGNORE INTO billing_extensions (module_key,key,name,icon,description,price_monthly,sort_order,is_active) VALUES (?,?,?,?,?,?,?,1)',
                (module_key, key, name, icon, description, price, sort)
            )

    def _ensure_billing_extra_bundles(self, cursor):
        """Идемпотентно добавляет новые модули в существующие пакеты."""
        import json as _json
        BUNDLE_EXTRAS = {
            'team_bundle': ['tasks_pro'],
            'all_in_one':  ['tasks_pro'],
        }
        for bundle_key, extra_modules in BUNDLE_EXTRAS.items():
            try:
                row = cursor.execute(
                    'SELECT includes_json FROM billing_bundles WHERE key=?', (bundle_key,)
                ).fetchone()
                if not row:
                    continue
                includes = _json.loads(row[0])
                modules = includes.get('modules', [])
                changed = False
                for m in extra_modules:
                    if m not in modules:
                        modules.append(m)
                        changed = True
                if changed:
                    includes['modules'] = modules
                    cursor.execute(
                        'UPDATE billing_bundles SET includes_json=? WHERE key=?',
                        (_json.dumps(includes), bundle_key)
                    )
            except Exception:
                pass

    def _init_billing_defaults(self, cursor):
        """Заполнить billing_modules, billing_extensions, billing_bundles дефолтными данными."""
        # Сначала всегда докатываем «поздние» модули/расширения на существующих установках
        self._ensure_billing_extra_modules(cursor)
        self._ensure_billing_extra_extensions(cursor)
        self._ensure_billing_extra_bundles(cursor)

        cursor.execute('SELECT COUNT(*) FROM billing_modules')
        if cursor.fetchone()[0] > 2:
            return  # Уже инициализировано (>2 т.к. org_structure + pos_retail могли быть только что добавлены)

        DEFAULT_MODULES = [
            ('analytics',        '📊 Аналитика',         '📊', 'Углублённая аналитика продаж, рейтинги, тренды', 299, 1),
            ('team',             '👥 Команда',            '👥', 'Зарплата, мотивация, конкурсы, расписание',       399, 2),
            ('notifications',    '🔔 Уведомления',        '🔔', 'Push-уведомления, плановые рассылки, алерты',      199, 3),
            ('plans_motivation', '📈 Планы и мотивация',  '📈', 'Планы продаж, мотивационные схемы, вехи',         249, 4),
            ('ai_assistant',     '🤖 ИИ-ассистент',       '🤖', 'AI-анализ отчётов, прогноз, умные алерты',        299, 5),
            ('integrations',     '🔗 Интеграции',         '🔗', 'Google Таблицы, автоэкспорт, API',                399, 6),
            ('chat',             '💬 Чат команды',         '💬', 'Внутренний чат с темами и личными сообщениями',   149, 7),
            ('org_structure',    '🏢 Оргструктура',       '🏢', 'Подразделения, регионы, кастомные роли, гранулярный доступ сотрудников', 349, 8),
        ]
        for key, name, icon, description, price, sort in DEFAULT_MODULES:
            cursor.execute(
                'INSERT OR IGNORE INTO billing_modules (key,name,icon,description,price_monthly,sort_order,is_active) VALUES (?,?,?,?,?,?,1)',
                (key, name, icon, description, price, sort)
            )

        DEFAULT_EXTENSIONS = [
            # analytics
            ('analytics',        'abc_analysis',       '🧮', 'ABC-анализ',               'Классификация товаров A/B/C по выручке',           149, 1),
            ('analytics',        'heatmap',            '🌡️', 'Тепловая карта',           'Карта продаж по дням и часам',                      99, 2),
            ('analytics',        'turnover',           '🔄', 'Оборачиваемость',          'Анализ скорости оборота товаров',                    99, 3),
            ('analytics',        'dead_stock',         '📦', 'Залежалые товары',         'Выявление неликвидных позиций',                      99, 4),
            ('analytics',        'trend_forecast',     '📉', 'Прогноз тренда',           'Прогноз продаж на 7/30 дней',                       149, 5),
            # team
            ('team',             'contests',           '🏆', 'Конкурсы',                 'Создание конкурсов между продавцами',               199, 1),
            ('team',             'salary_export',      '📊', 'Выгрузка зарплат в Excel', 'Выгрузка зарплатных данных в Excel',                 99, 2),
            ('team',             'joint_motivation',   '💰', 'Совместная мотивация',     'Общий мотивационный фонд для команды',              149, 3),
            # notifications
            ('notifications',    'scheduled_notifs',   '📅', 'Плановые рассылки',        'Отложенные и повторяющиеся уведомления',            149, 1),
            ('notifications',    'smart_alerts',       '🤖', 'Умные алерты',             'Автоматические уведомления при отклонениях',        199, 2),
            # plans_motivation
            ('plans_motivation', 'plan_filters',       '🔍', 'Планы с детализацией',     'Фильтрация планов по категориям и товарам',          99, 1),
            ('plans_motivation', 'milestone_alerts',   '🎯', 'Контрольные точки',        'Уведомления при достижении вех плана',               99, 2),
            ('plans_motivation', 'plan_coefficients',  '⚖️', 'Коэффициенты плана',       'Мотивационные коэффициенты выполнения',             149, 3),
            # ai_assistant
            ('ai_assistant',     'ai_forecast',        '🔮', 'Прогноз продаж (AI)',      'ML-прогноз продаж на 7–30 дней',                   149, 1),
            ('ai_assistant',     'ai_smart_alerts',    '🚨', 'Умные алерты AI',          'AI-детектирование аномалий в продажах',             199, 2),
            ('ai_assistant',     'ai_high_limit',      '⚡', 'Высокий лимит AI в день', 'Увеличенный дневной лимит запросов к AI (настраивается супер-админом)',  99, 3),
            # integrations
            ('integrations',     'gs_realtime',        '🔄', 'Реалтайм в Google Таблицы','Авто-синхронизация в Google Таблицы по событию',    99, 1),
        ]
        for module_key, key, icon, name, description, price, sort in DEFAULT_EXTENSIONS:
            cursor.execute(
                'INSERT OR IGNORE INTO billing_extensions (module_key,key,name,icon,description,price_monthly,sort_order,is_active) VALUES (?,?,?,?,?,?,?,1)',
                (module_key, key, name, icon, description, price, sort)
            )

        DEFAULT_BUNDLES = [
            ('small_biz',   '🏪', 'Малый бизнес',       'Аналитика + Уведомления для небольшого магазина',
             '{"modules":["analytics","notifications"],"extensions":[]}', 399, 1),
            ('team_bundle', '👥', 'Управление командой', 'Команда + Планы + Уведомления — полный HR-пакет',
             '{"modules":["team","plans_motivation","notifications"],"extensions":[]}', 699, 2),
            ('all_in_one',  '🎯', 'Всё включено',        'Все 7 модулей — максимальный функционал',
             '{"modules":["analytics","team","notifications","plans_motivation","ai_assistant","integrations","chat"],"extensions":[]}', 1299, 3),
        ]
        for key, icon, name, description, includes_json, price, sort in DEFAULT_BUNDLES:
            cursor.execute(
                'INSERT OR IGNORE INTO billing_bundles (key,name,icon,description,includes_json,price_monthly,sort_order,is_active) VALUES (?,?,?,?,?,?,?,1)',
                (key, name, icon, description, includes_json, price, sort)
            )

    # ── Гибкая оргструктура: подразделения ──────────────────────────────────
    def add_department(self, name, type='department', parent_id=None,
                       manager_tg_id=None, sort_order=0):
        """Создать подразделение. Возвращает id или None."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO departments (name, type, parent_id, manager_tg_id, sort_order) "
                "VALUES (?,?,?,?,?)",
                (name, type, parent_id, manager_tg_id, sort_order)
            )
            conn.commit()
            return cur.lastrowid
        except Exception:
            return None
        finally:
            conn.close()

    def get_departments(self, active_only=True):
        """Список подразделений (dict). Отсортированы по sort_order, name."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            q = ("SELECT id, name, type, parent_id, manager_tg_id, sort_order, is_active, created_at "
                 "FROM departments")
            if active_only:
                q += " WHERE is_active = 1"
            q += " ORDER BY sort_order ASC, name ASC"
            cur.execute(q)
            cols = ['id', 'name', 'type', 'parent_id', 'manager_tg_id',
                    'sort_order', 'is_active', 'created_at']
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:
            return []
        finally:
            conn.close()

    def get_department(self, dept_id):
        """Одно подразделение (dict) или None."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, type, parent_id, manager_tg_id, sort_order, is_active, created_at "
                "FROM departments WHERE id = ?", (dept_id,)
            )
            r = cur.fetchone()
            if not r:
                return None
            cols = ['id', 'name', 'type', 'parent_id', 'manager_tg_id',
                    'sort_order', 'is_active', 'created_at']
            return dict(zip(cols, r))
        except Exception:
            return None
        finally:
            conn.close()

    def update_department(self, dept_id, name=None, type=None, parent_id=-1,
                          manager_tg_id=-1, sort_order=None):
        """Обновить подразделение. parent_id/manager_tg_id=-1 → не менять (None разрешён)."""
        conn = self.get_connection()
        try:
            sets, params = [], []
            if name is not None:
                sets.append("name = ?"); params.append(name)
            if type is not None:
                sets.append("type = ?"); params.append(type)
            if parent_id != -1:
                sets.append("parent_id = ?"); params.append(parent_id)
            if manager_tg_id != -1:
                sets.append("manager_tg_id = ?"); params.append(manager_tg_id)
            if sort_order is not None:
                sets.append("sort_order = ?"); params.append(sort_order)
            if not sets:
                return False
            params.append(dept_id)
            conn.execute(f"UPDATE departments SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def delete_department(self, dept_id):
        """Удалить подразделение и перевесить дочерние на его родителя."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT parent_id FROM departments WHERE id = ?", (dept_id,))
            row = cur.fetchone()
            parent = row[0] if row else None
            cur.execute("UPDATE departments SET parent_id = ? WHERE parent_id = ?",
                        (parent, dept_id))
            cur.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def count_departments(self):
        """Число активных подразделений (для лимита минимального тарифа)."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM departments WHERE is_active = 1")
            return cur.fetchone()[0]
        except Exception:
            return 0
        finally:
            conn.close()

    # ── Гибкая оргструктура: кастомные роли ─────────────────────────────────
    _ORG_ROLE_COLS = ['id', 'name', 'icon', 'color', 'base_role', 'scope_type',
                      'scope_values', 'can_manage_users', 'can_manage_products',
                      'can_view_salary', 'can_manage_salary', 'can_view_reports',
                      'can_manage_plans', 'modules', 'is_active', 'created_at']

    def add_org_role(self, name, icon='🎖️', color='slate', base_role='user',
                     scope_type=None, scope_values=None, perms=None, modules=None):
        """Создать кастомную роль. perms — dict can_* флагов. Возвращает id или None."""
        import json as _json
        perms = perms or {}
        sv = _json.dumps(scope_values, ensure_ascii=False) if scope_values else None
        md = _json.dumps(modules, ensure_ascii=False) if modules else None
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO org_roles (name, icon, color, base_role, scope_type, scope_values, "
                "can_manage_users, can_manage_products, can_view_salary, can_manage_salary, "
                "can_view_reports, can_manage_plans, modules) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, icon, color, base_role, scope_type, sv,
                 int(perms.get('can_manage_users', 0)), int(perms.get('can_manage_products', 0)),
                 int(perms.get('can_view_salary', 0)), int(perms.get('can_manage_salary', 0)),
                 int(perms.get('can_view_reports', 0)), int(perms.get('can_manage_plans', 0)), md)
            )
            conn.commit()
            return cur.lastrowid
        except Exception:
            return None
        finally:
            conn.close()

    def _row_to_org_role(self, r):
        import json as _json
        d = dict(zip(self._ORG_ROLE_COLS, r))
        try:
            d['scope_values'] = _json.loads(d['scope_values']) if d['scope_values'] else []
        except Exception:
            d['scope_values'] = []
        try:
            d['modules'] = _json.loads(d['modules']) if d['modules'] else []
        except Exception:
            d['modules'] = []
        return d

    def get_org_roles(self, active_only=True):
        """Список кастомных ролей (dict; scope_values/modules — распарсенные list)."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            q = f"SELECT {', '.join(self._ORG_ROLE_COLS)} FROM org_roles"
            if active_only:
                q += " WHERE is_active = 1"
            q += " ORDER BY id ASC"
            cur.execute(q)
            return [self._row_to_org_role(r) for r in cur.fetchall()]
        except Exception:
            return []
        finally:
            conn.close()

    def get_org_role(self, role_id):
        """Одна кастомная роль (dict) или None."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {', '.join(self._ORG_ROLE_COLS)} FROM org_roles WHERE id = ?",
                (role_id,)
            )
            r = cur.fetchone()
            return self._row_to_org_role(r) if r else None
        except Exception:
            return None
        finally:
            conn.close()

    def update_org_role(self, role_id, name=None, icon=None, color=None, base_role=None,
                        scope_type='__keep__', scope_values='__keep__', perms=None,
                        modules='__keep__'):
        """Обновить кастомную роль. '__keep__' → не менять поле."""
        import json as _json
        conn = self.get_connection()
        try:
            sets, params = [], []
            if name is not None:
                sets.append("name = ?"); params.append(name)
            if icon is not None:
                sets.append("icon = ?"); params.append(icon)
            if color is not None:
                sets.append("color = ?"); params.append(color)
            if base_role is not None:
                sets.append("base_role = ?"); params.append(base_role)
            if scope_type != '__keep__':
                sets.append("scope_type = ?"); params.append(scope_type)
            if scope_values != '__keep__':
                sets.append("scope_values = ?")
                params.append(_json.dumps(scope_values, ensure_ascii=False) if scope_values else None)
            if modules != '__keep__':
                sets.append("modules = ?")
                params.append(_json.dumps(modules, ensure_ascii=False) if modules else None)
            if perms:
                for k in ('can_manage_users', 'can_manage_products', 'can_view_salary',
                          'can_manage_salary', 'can_view_reports', 'can_manage_plans'):
                    if k in perms:
                        sets.append(f"{k} = ?"); params.append(int(perms[k]))
            if not sets:
                return False
            params.append(role_id)
            conn.execute(f"UPDATE org_roles SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def delete_org_role(self, role_id):
        """Деактивировать кастомную роль (soft-delete, is_active=0)."""
        conn = self.get_connection()
        try:
            conn.execute("UPDATE org_roles SET is_active = 0 WHERE id = ?", (role_id,))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    # ── Гибкая оргструктура: гранулярный доступ к модулям ───────────────────
    def set_user_module_access(self, telegram_id, module_key, access):
        """access ∈ {'allow','deny'} — задать override; None/'' → удалить запись."""
        conn = self.get_connection()
        try:
            if access in (None, '', 'inherit'):
                conn.execute(
                    "DELETE FROM user_module_access WHERE telegram_id = ? AND module_key = ?",
                    (telegram_id, module_key)
                )
            else:
                conn.execute(
                    "INSERT INTO user_module_access (telegram_id, module_key, access) VALUES (?,?,?) "
                    "ON CONFLICT(telegram_id, module_key) DO UPDATE SET access = excluded.access",
                    (telegram_id, module_key, access)
                )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def get_user_module_access_map(self, telegram_id):
        """{module_key: 'allow'|'deny'} для пользователя."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT module_key, access FROM user_module_access WHERE telegram_id = ?",
                (telegram_id,)
            )
            return {r[0]: r[1] for r in cur.fetchall()}
        except Exception:
            return {}
        finally:
            conn.close()

    def clear_user_module_access(self, telegram_id):
        """Удалить все override-записи доступа к модулям для пользователя."""
        conn = self.get_connection()
        try:
            conn.execute("DELETE FROM user_module_access WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def get_recent_sales(self, limit=10):
        """Получить список последних продаж"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, p.name, s.shop_name, s.quantity_sold, s.sale_price, u.first_name, s.sale_date, p.id, u.id
            FROM sales s
            JOIN products p ON s.product_id = p.id
            JOIN users u ON s.user_id = u.id
            ORDER BY s.sale_date DESC
            LIMIT ?
        ''', (limit,))
        sales = cursor.fetchall()
        conn.close()
        return sales

    def get_shop_city_map(self) -> dict:
        """{shop_name: city} — из таблицы shops, с дополнением из users."""
        conn = self.get_connection()
        cursor = conn.cursor()
        result: dict = {}
        try:
            cursor.execute(
                "SELECT name, COALESCE(city, '') FROM shops "
                "WHERE name IS NOT NULL AND name != ''"
            )
            for name, city in cursor.fetchall():
                if city:
                    result[name] = city
            cursor.execute(
                "SELECT DISTINCT shop_name, COALESCE(city, '') FROM users "
                "WHERE shop_name IS NOT NULL AND shop_name != ''"
            )
            for name, city in cursor.fetchall():
                if city and name not in result:
                    result[name] = city
        except Exception as exc:
            logging.warning(f"get_shop_city_map failed: {exc}")
        finally:
            conn.close()
        return result

    def get_user_recent_products(self, user_id: int, limit: int = 5) -> list:
        """Последние N уникальных товаров, проданных данным продавцом."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT p.id, p.name, p.price, p.category
                FROM sales s
                JOIN products p ON s.product_id = p.id
                WHERE s.user_id = ?
                ORDER BY s.sale_date DESC
                LIMIT ?
            ''', (user_id, limit * 4))
            rows = cursor.fetchall()
            conn.close()
            seen = set()
            result = []
            for row in rows:
                if row[0] not in seen:
                    seen.add(row[0])
                    result.append(row)
                    if len(result) >= limit:
                        break
            return result
        except Exception as e:
            logger.error(f"Ошибка get_user_recent_products: {e}")
            return []

    def get_favorite_products(self, user_id: int) -> list:
        """Список product_id из избранного для продавца."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT product_id FROM user_favorites WHERE user_id = ? ORDER BY created_at DESC',
                (user_id,)
            )
            rows = cursor.fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception:
            return []

    def toggle_favorite_product(self, user_id: int, product_id: int) -> bool:
        """Переключить товар в избранном. True = добавлен, False = убран."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id FROM user_favorites WHERE user_id = ? AND product_id = ?',
                (user_id, product_id)
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    'DELETE FROM user_favorites WHERE user_id = ? AND product_id = ?',
                    (user_id, product_id)
                )
                conn.commit()
                conn.close()
                return False
            else:
                cursor.execute(
                    'INSERT INTO user_favorites (user_id, product_id) VALUES (?, ?)',
                    (user_id, product_id)
                )
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            logger.error(f"Ошибка toggle_favorite_product: {e}")
            return False

    def get_user_nav_config(self, telegram_id: int):
        """Вернуть список из 4 ключей навигации для пользователя (или None)."""
        try:
            import json
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT mobile_nav FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                cfg = json.loads(row[0])
                if isinstance(cfg, list) and 1 <= len(cfg) <= 4:
                    return cfg
        except Exception as _exc:
            logger.debug("get_user_nav_config: подавлено исключение: %s", _exc)
        return None

    def set_user_nav_config(self, telegram_id: int, config: list) -> bool:
        """Сохранить список ключей навигации для пользователя."""
        try:
            import json
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET mobile_nav = ? WHERE telegram_id = ?",
                (json.dumps(config[:4]), telegram_id)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"set_user_nav_config error: {e}")
            return False

    def check_and_mark_plan_milestones(self, telegram_id: int) -> list:
        """Проверяет, какие milestone (50/75/100%) только что достигнуты.
        Возвращает [(plan, actual, pct, milestone)] — только новые вехи.
        Сохраняет их в plan_milestone_alerts (UNIQUE по user+plan+milestone+period_start).
        period_start гарантирует, что milestone может сработать заново в каждом периоде.
        """
        milestones = (50, 75, 100)
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (telegram_id,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                return []
            user_id = row[0]

            plans_progress = self.get_user_plans_progress(telegram_id)
            newly_hit = []
            now = datetime.now()
            for plan, actual, pct in plans_progress:
                plan_id = plan[0]
                plan_type = plan[1]  # 'weekly' or 'monthly'
                # Вычисляем начало текущего периода плана
                if plan_type == 'weekly':
                    period_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
                else:
                    period_start = now.replace(day=1).strftime('%Y-%m-%d')

                for milestone in sorted(milestones):
                    if pct >= milestone:
                        try:
                            conn2 = self.get_connection()
                            c2 = conn2.cursor()
                            c2.execute(
                                'INSERT OR IGNORE INTO plan_milestone_alerts '
                                '(user_id, plan_id, milestone, period_start) VALUES (?, ?, ?, ?)',
                                (user_id, plan_id, milestone, period_start)
                            )
                            inserted = c2.rowcount > 0
                            conn2.commit()
                            conn2.close()
                            if inserted:
                                newly_hit.append((plan, actual, pct, milestone))
                        except Exception as _exc:
                            logger.debug("check_and_mark_plan_milestones: подавлено исключение: %s", _exc)
            return newly_hit
        except Exception as e:
            logger.error(f"Ошибка check_and_mark_plan_milestones: {e}")
            return []
    def get_payment_settings(self):
        """Получение настроек платежной системы"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM payment_settings')
        settings = dict(cursor.fetchall())

        # Значения по умолчанию
        defaults = {
            'card_number': '0000 0000 0000 0000',
            'recipient_name': 'Не настроено',
            'bank_name': 'Не настроено'
        }

        for key, default_value in defaults.items():
            if key not in settings:
                settings[key] = default_value

        conn.close()
        return settings

    def update_payment_setting(self, key, value):
        """Обновление настройки платежной системы"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO payment_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
        conn.commit()
        conn.close()

    def get_web_interface_url(self) -> str:
        """Получить URL веб-интерфейса (None если не настроен)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM payment_settings WHERE key = 'web_interface_url'")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else None

    def set_web_interface_url(self, url: str) -> None:
        """Сохранить URL веб-интерфейса. Передать None или '' — удалить."""
        if url:
            self.update_payment_setting('web_interface_url', url)
        else:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM payment_settings WHERE key = 'web_interface_url'")
            conn.commit()
            conn.close()

    # ------------------------------------------------------------------
    # Провайдер платежей
    # ------------------------------------------------------------------

    def get_payment_provider(self) -> str:
        """Получить активного провайдера оплаты ('sbp' или 'yookassa')."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM payment_settings WHERE key = 'payment_provider'")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 'sbp'

    def set_payment_provider(self, provider: str) -> None:
        """Установить активного провайдера оплаты."""
        self.update_payment_setting('payment_provider', provider)

    def get_yookassa_config(self) -> dict:
        """Получить настройки ЮKassa (shop_id, secret_key, return_url)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key, value FROM payment_settings WHERE key IN "
            "('yookassa_shop_id','yookassa_secret_key','yookassa_return_url')"
        )
        result = dict(cursor.fetchall())
        conn.close()
        return {
            'shop_id': result.get('yookassa_shop_id', ''),
            'secret_key': result.get('yookassa_secret_key', ''),
            'return_url': result.get('yookassa_return_url', ''),
        }

    def set_yookassa_config(self, shop_id: str = None, secret_key: str = None,
                            return_url: str = None) -> None:
        """Сохранить одно или несколько полей конфига ЮKassa."""
        if shop_id is not None:
            self.update_payment_setting('yookassa_shop_id', shop_id)
        if secret_key is not None:
            self.update_payment_setting('yookassa_secret_key', secret_key)
        if return_url is not None:
            self.update_payment_setting('yookassa_return_url', return_url)

    # ------------------------------------------------------------------
    # Таблица yookassa_payments
    # ------------------------------------------------------------------

    def create_yookassa_payment_record(
        self,
        yookassa_payment_id: str,
        user_id: int,
        plan_type: str,
        amount: float,
        promocode_id: int = None,
        is_scheduled: bool = False,
        schedule_date: str = None,
    ) -> bool:
        """Сохранить запись о созданном платеже ЮKassa."""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO yookassa_payments
                    (yookassa_payment_id, user_id, plan_type, amount,
                     promocode_id, is_scheduled, schedule_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    yookassa_payment_id, user_id, plan_type, amount,
                    promocode_id, 1 if is_scheduled else 0, schedule_date,
                ),
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            if conn:
                try:
                    conn.rollback()
                except Exception as _exc:
                    logger.debug("create_yookassa_payment_record: подавлено исключение: %s", _exc)
                conn.close()
            return False
        except Exception as e:
            logger.error(f"create_yookassa_payment_record: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception as _exc:
                    logger.debug("create_yookassa_payment_record: подавлено исключение: %s", _exc)
                conn.close()
            return False

    def get_yookassa_payment_by_payment_id(self, yookassa_payment_id: str):
        """Найти запись платежа ЮKassa по его ID."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM yookassa_payments WHERE yookassa_payment_id = ?",
                (yookassa_payment_id,),
            )
            row = cursor.fetchone()
            conn.close()
            return row
        except Exception as e:
            logger.error(f"get_yookassa_payment_by_payment_id: {e}")
            return None

    def update_yookassa_payment_status(self, yookassa_payment_id: str, status: str) -> None:
        """Обновить статус платежа ЮKassa."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE yookassa_payments SET status=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE yookassa_payment_id=?",
                (status, yookassa_payment_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"update_yookassa_payment_status: {e}")

    def get_subscriptions_statistics(self):
        """Статистика подписок"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Общее количество подписчиков
        cursor.execute('SELECT COUNT(*) FROM subscriptions WHERE end_date > CURRENT_TIMESTAMP')
        total_subscribers = cursor.fetchone()[0]

        # Месячная выручка — реальная сумма из одобренных платежей
        cursor.execute('''
            SELECT COALESCE(SUM(amount), 0) FROM payment_requests
            WHERE status = 'approved' AND created_at >= date('now', '-30 days')
        ''')
        monthly_revenue = cursor.fetchone()[0]

        # Конверсия (упрощенная)
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]

        conversion_rate = (total_subscribers / total_users * 100) if total_users > 0 else 0

        conn.close()

        return {
            'total_subscribers': total_subscribers,
            'monthly_revenue': monthly_revenue,
            'conversion_rate': conversion_rate
        }

    def get_detailed_payment_statistics(self):
        """Детальная статистика платежей"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Общее количество платежей
        cursor.execute('SELECT COUNT(*) FROM payment_requests WHERE status = "approved"')
        total_payments = cursor.fetchone()[0]

        # Общая выручка
        cursor.execute('SELECT SUM(amount) FROM payment_requests WHERE status = "approved"')
        result = cursor.fetchone()[0]
        total_revenue = result if result else 0

        # Средний чек
        average_payment = total_revenue / total_payments if total_payments > 0 else 0

        # Выручка за последний месяц - ИСПРАВЛЕНО: используем created_at вместо request_date
        cursor.execute('''
            SELECT SUM(amount) FROM payment_requests 
            WHERE status = "approved" AND created_at >= date('now', '-30 days')
        ''')
        result = cursor.fetchone()[0]
        monthly_revenue = result if result else 0

        # Статистика по планам
        cursor.execute('''
            SELECT plan_type, COUNT(*) as count, SUM(amount) as revenue
            FROM payment_requests 
            WHERE status = "approved"
            GROUP BY plan_type
        ''')
        plans_data = cursor.fetchall()
        by_plans = {}
        for plan_type, count, revenue in plans_data:
            by_plans[plan_type] = {
                'count': count,
                'revenue': revenue if revenue else 0
            }

        # Конверсия и продления
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM subscriptions WHERE end_date > CURRENT_TIMESTAMP')
        active_subscriptions = cursor.fetchone()[0]

        conversion_rate = (active_subscriptions / total_users * 100) if total_users > 0 else 0
        renewal_rate = 85  # Упрощенный показатель

        conn.close()

        return {
            'total_payments': total_payments,
            'total_revenue': total_revenue,
            'average_payment': average_payment,
            'monthly_revenue': monthly_revenue,
            'by_plans': by_plans,
            'conversion_rate': conversion_rate,
            'renewal_rate': renewal_rate
        }

    # Базовые методы пользователей
    def add_user(self, telegram_id, first_name, last_name, middle_name=None, phone=None, email=None, trade_network=None, shop_name=None, city=None, username=None):
        """Добавление нового пользователя"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users 
            (telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, username)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, username))
        conn.commit()
        conn.close()

    def get_user(self, telegram_id):
        """Получение информации о пользователе"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
        user = cursor.fetchone()
        conn.close()
        return user

    def get_shop_sales_by_date(self, shop_name, start_date, end_date):
        """Получить все продажи магазина за период.
        Возвращает 11 колонок: id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
        user_id[5], sale_date[6], product_name[7], category[8], first_name[9], last_name[10]"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        query = '''
            SELECT s.id, s.product_id, s.shop_name, s.quantity_sold, s.sale_price, 
                   s.user_id, s.sale_date, p.name, p.category, u.first_name, u.last_name
            FROM sales s
            JOIN products p ON s.product_id = p.id
            LEFT JOIN users u ON s.user_id = u.id
            WHERE s.shop_name = ? AND date(s.sale_date) >= ? AND date(s.sale_date) <= ?
            ORDER BY s.sale_date DESC
        '''
        
        cursor.execute(query, (shop_name, start_date, end_date))
        sales = cursor.fetchall()
        conn.close()
        return sales

    def get_user_id(self, telegram_id):
        """Получение ID пользователя по telegram_id"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (telegram_id,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else None
        except Exception as e:
            logger.error(f"get_user_id({telegram_id}) error: {e!r}")
            return None
    
    def get_user_timezone(self, telegram_id):
        """Получение часового пояса пользователя. Результат кешируется на 300 сек."""
        key = (self.db_file, telegram_id)
        now = _time.time()
        cached = _tz_cache.get(key)
        if cached and now - cached[1] < _TZ_CACHE_TTL:
            return cached[0]
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT timezone FROM users WHERE telegram_id = ?', (telegram_id,))
        result = cursor.fetchone()
        conn.close()
        tz = result[0] if result else 'Europe/Moscow'
        _tz_cache[key] = (tz, now)
        return tz

    def set_user_timezone(self, telegram_id, timezone):
        """Установка часового пояса пользователя"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET timezone = ? WHERE telegram_id = ?', (timezone, telegram_id))
            conn.commit()
            success = cursor.rowcount > 0
            conn.close()
            if success:
                _tz_cache.pop((self.db_file, telegram_id), None)
            return success
        except Exception as e:
            logger.error(f"Ошибка при установке часового пояса: {e}")
            return False

    # Заглушки для остальных методов
    def get_all_shops(self, include_system: bool = False):
        conn = self.get_connection()
        cursor = conn.cursor()
        sys_filter = "" if include_system else " AND name NOT IN ('Системный', 'System')"
        sys_filter_u = "" if include_system else " AND shop_name NOT IN ('Системный', 'System')"
        try:
            cursor.execute(
                "SELECT DISTINCT name FROM ("
                f"  SELECT shop_name AS name FROM users "
                f"  WHERE shop_name IS NOT NULL AND shop_name != ''{sys_filter_u}"
                "  UNION"
                f"  SELECT name FROM shops WHERE name IS NOT NULL AND name != ''{sys_filter}"
                "  UNION"
                f"  SELECT shop_name AS name FROM inventory "
                f"  WHERE shop_name IS NOT NULL AND shop_name != ''{sys_filter_u}"
                ") ORDER BY name"
            )
        except Exception:
            cursor.execute(
                "SELECT DISTINCT shop_name FROM users "
                f"WHERE shop_name IS NOT NULL AND shop_name != ''{sys_filter_u}"
            )
        shops = [row[0] for row in cursor.fetchall()]
        conn.close()
        return shops

    def get_shops_with_stats(self, include_system: bool = False):
        """Возвращает список (name, user_count, inventory_items, out_of_stock, low_stock) для всех магазинов."""
        conn = self.get_connection()
        cursor = conn.cursor()
        sys_filter = "" if include_system else " AND name NOT IN ('Системный', 'System')"
        sys_filter_u = "" if include_system else " AND shop_name NOT IN ('Системный', 'System')"
        try:
            cursor.execute(
                "SELECT DISTINCT name FROM ("
                f"  SELECT shop_name AS name FROM users "
                f"  WHERE shop_name IS NOT NULL AND shop_name != ''{sys_filter_u}"
                "  UNION"
                f"  SELECT name FROM shops WHERE name IS NOT NULL AND name != ''{sys_filter}"
                "  UNION"
                f"  SELECT shop_name AS name FROM inventory "
                f"  WHERE shop_name IS NOT NULL AND shop_name != ''{sys_filter_u}"
                ") ORDER BY name"
            )
        except Exception:
            cursor.execute(
                "SELECT DISTINCT shop_name AS name FROM users "
                f"WHERE shop_name IS NOT NULL AND shop_name != ''{sys_filter_u}"
            )
        shop_names = [r[0] for r in cursor.fetchall()]
        result = []
        for sn in shop_names:
            cursor.execute("SELECT COUNT(*) FROM users WHERE shop_name = ?", (sn,))
            user_count = cursor.fetchone()[0]
            try:
                cursor.execute(
                    "SELECT COUNT(DISTINCT product_id) FROM inventory WHERE shop_name = ? AND quantity > 0",
                    (sn,)
                )
                inv_count = cursor.fetchone()[0]
            except Exception:
                inv_count = 0
            try:
                cursor.execute(
                    "SELECT COUNT(DISTINCT product_id) FROM inventory WHERE shop_name = ? AND quantity <= 0",
                    (sn,)
                )
                out_of_stock = cursor.fetchone()[0]
            except Exception:
                out_of_stock = 0
            try:
                cursor.execute(
                    "SELECT COUNT(DISTINCT product_id) FROM inventory "
                    "WHERE shop_name = ? AND quantity >= 1 AND quantity <= 5",
                    (sn,)
                )
                low_stock = cursor.fetchone()[0]
            except Exception:
                low_stock = 0
            result.append((sn, user_count, inv_count, out_of_stock, low_stock))
        conn.close()
        return result

    def add_shop(self, name: str) -> bool:
        """Добавить новый магазин в таблицу shops."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT OR IGNORE INTO shops (name) VALUES (?)", (name.strip(),))
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            return False
        finally:
            conn.close()

    def rename_shop_everywhere(self, old_name: str, new_name: str):
        """Переименовать магазин во всех таблицах: users, inventory, shops."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE users SET shop_name = ? WHERE shop_name = ?",
                (new_name, old_name)
            )
            cursor.execute(
                "UPDATE inventory SET shop_name = ? WHERE shop_name = ?",
                (new_name, old_name)
            )
            cursor.execute("INSERT OR IGNORE INTO shops (name) VALUES (?)", (new_name,))
            cursor.execute("DELETE FROM shops WHERE name = ?", (old_name,))
            conn.commit()
        finally:
            conn.close()

    def delete_shop_everywhere(self, name: str) -> int:
        """Удалить магазин: сбросить shop_name у пользователей, удалить остатки и запись."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM users WHERE shop_name = ?", (name,))
            user_count = cursor.fetchone()[0]
            cursor.execute("UPDATE users SET shop_name = '' WHERE shop_name = ?", (name,))
            cursor.execute("DELETE FROM inventory WHERE shop_name = ?", (name,))
            cursor.execute("DELETE FROM shops WHERE name = ?", (name,))
            conn.commit()
            return user_count
        finally:
            conn.close()

    def get_inventory_shops(self):
        """Возвращает список уникальных магазинов из таблицы inventory"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT shop_name FROM inventory "
            "WHERE shop_name IS NOT NULL AND shop_name != '' "
            "AND shop_name NOT IN ('Системный', 'System')"
        )
        shops = [row[0] for row in cursor.fetchall()]
        conn.close()
        return shops

    def get_all_cities(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT city FROM users WHERE city IS NOT NULL AND city != '' AND city != 'System'"
        )
        cities = [row[0] for row in cursor.fetchall()]
        conn.close()
        return cities

    def get_all_trade_networks(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT trade_network FROM users "
            "WHERE trade_network IS NOT NULL AND trade_network != '' "
            "AND trade_network != 'System'"
        )
        networks = [row[0] for row in cursor.fetchall()]
        conn.close()
        return networks

    def get_shops_by_network(self, trade_network: str) -> list:
        """Список (shop_name, city) всех магазинов данной торговой сети."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT shop_name, COALESCE(city, '') FROM users "
            "WHERE trade_network = ? AND shop_name IS NOT NULL AND shop_name != '' "
            "AND shop_name NOT IN ('Системный', 'System') "
            "ORDER BY city, shop_name",
            (trade_network,)
        )
        result = cursor.fetchall()
        conn.close()
        return result  # [(shop_name, city), ...]

    def get_cities_by_network(self, trade_network: str) -> list:
        """Список городов торговой сети (только непустые)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT COALESCE(city, '') FROM users "
            "WHERE trade_network = ? AND shop_name IS NOT NULL AND shop_name != '' "
            "AND shop_name NOT IN ('Системный', 'System') "
            "ORDER BY city",
            (trade_network,)
        )
        result = [row[0] for row in cursor.fetchall()]
        conn.close()
        return [c for c in result if c]

    # ── Настройки дизайна ценника ─────────────────────────────────────────────
    def get_label_settings(self) -> dict:
        """Return label design settings. Returns defaults if not set."""
        import json as _json
        _ELEM_DEFAULT = {
            "logo": True, "badge": False, "name": True, "price": True,
            "sep": True, "qr": True, "barcode": False,
            "article": True, "category": False, "description": False,
        }
        _ORDER_DEFAULT = ["logo", "badge", "name", "price", "sep", "qr", "barcode", "article", "category", "description"]
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT bg_color, text_color, price_color, logo_path, font_size, org_logo_path,'
            '       font_family, border_color, border_width, label_theme,'
            '       element_order, visible_elements, sale_badge, qr_content '
            'FROM org_label_settings WHERE id=1'
        )
        row = cursor.fetchone()
        conn.close()
        def _parse_json(raw, default):
            try:
                return _json.loads(raw) if raw else default
            except Exception:
                return default
        if row:
            return {
                'bg_color':         row[0] or '#ffffff',
                'text_color':       row[1] or '#000000',
                'price_color':      row[2] or '#000000',
                'logo_path':        row[3] or '',
                'font_size':        row[4] or 'medium',
                'org_logo_path':    row[5] or '',
                'font_family':      row[6] or 'Arial, Helvetica, sans-serif',
                'border_color':     row[7] or '#cccccc',
                'border_width':     row[8] or '1',
                'label_theme':      row[9] or 'standard',
                'element_order':    _parse_json(row[10], _ORDER_DEFAULT),
                'visible_elements': _parse_json(row[11], _ELEM_DEFAULT),
                'sale_badge':       row[12] or '',
                'qr_content':       (row[13] if len(row) > 13 else '') or '',
            }
        return {
            'bg_color': '#ffffff', 'text_color': '#000000',
            'price_color': '#000000', 'logo_path': '', 'font_size': 'medium',
            'org_logo_path': '',
            'font_family': 'Arial, Helvetica, sans-serif',
            'border_color': '#cccccc', 'border_width': '1',
            'label_theme': 'standard',
            'element_order': _ORDER_DEFAULT,
            'visible_elements': _ELEM_DEFAULT,
            'sale_badge': '',
            'qr_content': '',
        }

    def save_org_logo(self, org_logo_path: str) -> None:
        """Upsert only the org logo path in org_label_settings (singleton row id=1)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO org_label_settings (id, org_logo_path)
               VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET
                   org_logo_path=excluded.org_logo_path,
                   updated_at=datetime('now')''',
            (org_logo_path,),
        )
        conn.commit()
        conn.close()

    def save_label_settings(self, bg_color: str, text_color: str,
                            price_color: str, logo_path, font_size: str,
                            font_family: str = '', border_color: str = '#cccccc',
                            border_width: str = '1', label_theme: str = 'standard',
                            element_order: str = '', visible_elements: str = '',
                            sale_badge: str = '', qr_content: str = '') -> None:
        """Upsert label design settings (singleton row id=1).
        Pass logo_path=None to preserve the existing logo; '' to clear it.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        ff = font_family or 'Arial, Helvetica, sans-serif'
        if logo_path is None:
            cursor.execute(
                '''INSERT INTO org_label_settings
                       (id, bg_color, text_color, price_color, font_size,
                        font_family, border_color, border_width, label_theme,
                        element_order, visible_elements, sale_badge, qr_content)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       bg_color=excluded.bg_color,
                       text_color=excluded.text_color,
                       price_color=excluded.price_color,
                       font_size=excluded.font_size,
                       font_family=excluded.font_family,
                       border_color=excluded.border_color,
                       border_width=excluded.border_width,
                       label_theme=excluded.label_theme,
                       element_order=excluded.element_order,
                       visible_elements=excluded.visible_elements,
                       sale_badge=excluded.sale_badge,
                       qr_content=excluded.qr_content,
                       updated_at=datetime('now')''',
                (bg_color, text_color, price_color, font_size,
                 ff, border_color, border_width, label_theme,
                 element_order, visible_elements, sale_badge, qr_content),
            )
        else:
            cursor.execute(
                '''INSERT INTO org_label_settings
                       (id, bg_color, text_color, price_color, logo_path, font_size,
                        font_family, border_color, border_width, label_theme,
                        element_order, visible_elements, sale_badge, qr_content)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       bg_color=excluded.bg_color,
                       text_color=excluded.text_color,
                       price_color=excluded.price_color,
                       logo_path=excluded.logo_path,
                       font_size=excluded.font_size,
                       font_family=excluded.font_family,
                       border_color=excluded.border_color,
                       border_width=excluded.border_width,
                       label_theme=excluded.label_theme,
                       element_order=excluded.element_order,
                       visible_elements=excluded.visible_elements,
                       sale_badge=excluded.sale_badge,
                       qr_content=excluded.qr_content,
                       updated_at=datetime('now')''',
                (bg_color, text_color, price_color, logo_path, font_size,
                 ff, border_color, border_width, label_theme,
                 element_order, visible_elements, sale_badge, qr_content),
            )
        conn.commit()
        conn.close()

    # ── Именованные пресеты дизайна ценника ──────────────────────────────────
    _LABEL_PRESET_FIELDS = (
        'bg_color', 'text_color', 'price_color', 'logo_path', 'font_size',
        'font_family', 'border_color', 'border_width', 'label_theme',
        'element_order', 'visible_elements', 'sale_badge', 'qr_content',
    )

    def list_label_presets(self) -> list:
        """Return saved label design presets (lightweight: id, name, updated_at)."""
        conn = self.get_connection()
        try:
            rows = conn.execute(
                'SELECT id, name, updated_at FROM org_label_presets ORDER BY name COLLATE NOCASE'
            ).fetchall()
            return [{'id': r[0], 'name': r[1], 'updated_at': r[2]} for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def get_label_preset(self, preset_id: int) -> dict:
        """Return one preset's full design dict, or {} if not found."""
        conn = self.get_connection()
        try:
            cols = ', '.join(self._LABEL_PRESET_FIELDS)
            row = conn.execute(
                f'SELECT id, name, {cols} FROM org_label_presets WHERE id=?',
                (preset_id,),
            ).fetchone()
        except Exception:
            row = None
        finally:
            conn.close()
        if not row:
            return {}
        out = {'id': row[0], 'name': row[1]}
        for i, key in enumerate(self._LABEL_PRESET_FIELDS, start=2):
            out[key] = row[i]
        return out

    def create_label_preset(self, name: str, design: dict) -> int:
        """Insert a new named preset from a design dict. Returns new id (0 on error)."""
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cols = ', '.join(self._LABEL_PRESET_FIELDS)
            ph = ', '.join('?' for _ in self._LABEL_PRESET_FIELDS)
            vals = [design.get(k, '') or '' for k in self._LABEL_PRESET_FIELDS]
            cur.execute(
                f'INSERT INTO org_label_presets (name, {cols}) VALUES (?, {ph})',
                (name, *vals),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            return 0
        finally:
            conn.close()

    def rename_label_preset(self, preset_id: int, name: str) -> None:
        conn = self.get_connection()
        try:
            conn.execute(
                "UPDATE org_label_presets SET name=?, updated_at=datetime('now') WHERE id=?",
                (name, preset_id),
            )
            conn.commit()
        finally:
            conn.close()

    def update_label_preset(self, preset_id: int, design: dict) -> None:
        """Overwrite an existing preset's design with the given dict."""
        conn = self.get_connection()
        try:
            sets = ', '.join(f'{k}=?' for k in self._LABEL_PRESET_FIELDS)
            vals = [design.get(k, '') or '' for k in self._LABEL_PRESET_FIELDS]
            conn.execute(
                f"UPDATE org_label_presets SET {sets}, updated_at=datetime('now') WHERE id=?",
                (*vals, preset_id),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_label_preset(self, preset_id: int) -> None:
        conn = self.get_connection()
        try:
            conn.execute('DELETE FROM org_label_presets WHERE id=?', (preset_id,))
            conn.commit()
        finally:
            conn.close()

    # Методы для работы с товарами
    def get_product_count(self) -> int:
        """SELECT COUNT(*) вместо загрузки всех строк — только для счётчика."""
        conn = self.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        except Exception:
            return 0
        finally:
            conn.close()

    def get_all_products(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products')
        products = cursor.fetchall()
        conn.close()
        return products

    def get_product(self, product_id):
        """Получение товара по ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products WHERE id = ?', (product_id,))
        product = cursor.fetchone()
        conn.close()
        return product

    def get_product_by_name(self, product_name):
        """Получение товара по названию"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products WHERE name = ?', (product_name,))
        product = cursor.fetchone()
        conn.close()
        return product

    @staticmethod
    def _make_article_prefix(category: str) -> str:
        """3-символьный ASCII-префикс из названия категории для артикула."""
        _TR = {
            'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'E','Ж':'J',
            'З':'Z','И':'I','Й':'Y','К':'K','Л':'L','М':'M','Н':'N','О':'O',
            'П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F','Х':'X','Ц':'C',
            'Ч':'H','Ш':'W','Щ':'Q','Ъ':'','Ы':'Y','Ь':'','Э':'E','Ю':'U','Я':'Q',
        }
        s = (category or '').upper().strip()
        out = []
        for ch in s:
            if ch.isascii() and ch.isalpha():
                out.append(ch)
            elif ch in _TR and _TR[ch]:
                out.append(_TR[ch])
            if len(out) >= 3:
                break
        prefix = ''.join(out)[:3]
        if len(prefix) < 2:
            return 'PRD'
        return prefix.ljust(3, 'X')

    def add_product(self, name, category, price, photo_file_id=None, description=None, article=None, barcode=None, old_price=None):
        """Добавление нового товара. Если article=None — генерируется автоматически.
        old_price — старая (зачёркнутая) цена для акции; None/<=0 → не сохраняется."""
        conn = self.get_connection()
        cursor = conn.cursor()
        barcode_val = barcode.strip() if barcode and barcode.strip() else None
        try:
            old_price_val = float(old_price) if old_price not in (None, "") and float(old_price) > 0 else None
        except (TypeError, ValueError):
            old_price_val = None
        try:
            if article:
                cursor.execute(
                    'INSERT INTO products (name, category, price, photo_file_id, description, article, barcode) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (name, category, price, photo_file_id, description, article.strip().upper(), barcode_val),
                )
            else:
                cursor.execute(
                    'INSERT INTO products (name, category, price, photo_file_id, description, barcode) VALUES (?, ?, ?, ?, ?, ?)',
                    (name, category, price, photo_file_id, description, barcode_val),
                )
            product_id = cursor.lastrowid
            if not article:
                prefix = self._make_article_prefix(category)
                base = f"{prefix}-{product_id:05d}"
                auto_art = base
                for sfx in [''] + list('ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
                    cand = base + sfx
                    if not cursor.execute("SELECT 1 FROM products WHERE article=?", (cand,)).fetchone():
                        auto_art = cand
                        break
                cursor.execute("UPDATE products SET article=? WHERE id=?", (auto_art, product_id))
            if old_price_val is not None:
                cursor.execute("UPDATE products SET old_price=? WHERE id=?", (old_price_val, product_id))
            conn.commit()
            return product_id
        except Exception:
            conn.rollback()
            return None
        finally:
            conn.close()

    def add_products_bulk(self, items):
        """
        Пакетное добавление товаров.
        items = [{'name': str, 'category': str, 'price': float}, ...]
        Возвращает (added_count, skipped_names) где skipped — уже существующие.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        added = 0
        skipped = []
        try:
            for item in items:
                try:
                    cursor.execute(
                        'INSERT INTO products (name, category, price) VALUES (?, ?, ?)',
                        (item['name'], item['category'], item['price'])
                    )
                    pid = cursor.lastrowid
                    prefix = self._make_article_prefix(item.get('category', ''))
                    base = f"{prefix}-{pid:05d}"
                    auto_art = base
                    for sfx in [''] + list('ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
                        cand = base + sfx
                        if not cursor.execute("SELECT 1 FROM products WHERE article=?", (cand,)).fetchone():
                            auto_art = cand
                            break
                    cursor.execute("UPDATE products SET article=? WHERE id=?", (auto_art, pid))
                    added += 1
                except sqlite3.IntegrityError:
                    skipped.append(item['name'])
            conn.commit()
        finally:
            conn.close()
        return added, skipped

    def update_product(self, product_id, name=None, category=None, price=None,
                       photo_file_id=None, description=None, article=None, barcode=None,
                       old_price=None):
        """Обновление товара. article='' → оставить без изменений; article='XXX' → установить.
        barcode=None → не трогать; barcode='' → очистить; barcode='...' → установить.
        old_price=None → не трогать; old_price<=0 или '' → очистить (NULL); >0 → установить."""
        conn = self.get_connection()
        cursor = conn.cursor()

        updates = []
        params = []

        if name is not None:
            updates.append('name = ?')
            params.append(name)
        if category is not None:
            updates.append('category = ?')
            params.append(category)
        if price is not None:
            updates.append('price = ?')
            params.append(price)
        if photo_file_id is not None:
            updates.append('photo_file_id = ?')
            params.append(photo_file_id)
        if description is not None:
            updates.append('description = ?')
            params.append(description)
        if article is not None and article != '':
            updates.append('article = ?')
            params.append(article.strip().upper())
        if barcode is not None:
            updates.append('barcode = ?')
            params.append(barcode.strip() if barcode.strip() else None)
        if old_price is not None:
            try:
                _op = float(old_price)
            except (TypeError, ValueError):
                _op = 0
            updates.append('old_price = ?')
            params.append(_op if _op > 0 else None)

        if updates:
            params.append(product_id)
            cursor.execute(f'''
                UPDATE products SET {', '.join(updates)}
                WHERE id = ?
            ''', params)
            conn.commit()
            conn.close()
            return True

        conn.close()
        return False

    def bulk_assign_articles(self) -> int:
        """Присвоить авто-артикулы всем товарам у которых нет артикула. Возвращает кол-во обновлённых."""
        conn = self.get_connection()
        try:
            rows = conn.execute(
                "SELECT id, category FROM products WHERE article IS NULL OR TRIM(article)=''"
            ).fetchall()
            updated = 0
            for pid, cat in rows:
                prefix = self._make_article_prefix(cat)
                base = f"{prefix}-{pid:05d}"
                final = base
                for sfx in [''] + list('ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
                    cand = base + sfx
                    if not conn.execute("SELECT 1 FROM products WHERE article=?", (cand,)).fetchone():
                        final = cand
                        break
                conn.execute(
                    "UPDATE products SET article=? WHERE id=? AND (article IS NULL OR TRIM(article)='')",
                    (final, pid),
                )
                updated += 1
            conn.commit()
            return updated
        finally:
            conn.close()

    def import_articles_bulk(self, items: list) -> dict:
        """
        Bulk-assign articles to existing products by name match.
        items: list of {"name": str, "article": str}
        Returns {"updated": int, "not_found": list[str], "conflicts": list[dict]}
          conflicts: [{"name": str, "article": str, "owner": str}]  — article already used by another product
        """
        conn = self.get_connection()
        try:
            updated = 0
            not_found: list = []
            conflicts: list = []
            for item in items:
                raw_name = (item.get("name") or "").strip()
                raw_art = (item.get("article") or "").strip().upper()
                if not raw_name or not raw_art:
                    continue
                row = conn.execute(
                    "SELECT id, name, article FROM products WHERE UPPER(name)=?",
                    (raw_name.upper(),),
                ).fetchone()
                if row is None:
                    not_found.append(raw_name)
                    continue
                pid, prod_name, current_art = row[0], row[1], row[2]
                existing = conn.execute(
                    "SELECT name FROM products WHERE UPPER(article)=? AND id!=?",
                    (raw_art, pid),
                ).fetchone()
                if existing:
                    conflicts.append({
                        "name": raw_name,
                        "article": raw_art,
                        "owner": existing[0],
                    })
                    continue
                conn.execute(
                    "UPDATE products SET article=? WHERE id=?",
                    (raw_art, pid),
                )
                updated += 1
            conn.commit()
            return {"updated": updated, "not_found": not_found, "conflicts": conflicts}
        finally:
            conn.close()

    def get_product_by_article(self, article: str, trade_network: str = None):
        """Поиск товара по артикулу (без учёта регистра).

        Сначала ищет среди вариантов по торговой сети (product_network_variants):
        при переданном trade_network — приоритет точного совпадения сети, затем
        любой вариант. Если в вариантах не найдено — fallback на products.article
        (старое поведение, полная обратная совместимость для орг без вариантов).
        """
        raw = (article or "").strip()
        if not raw:
            return None
        up = raw.upper()
        conn = self.get_connection()
        try:
            pid = None
            if trade_network:
                vrow = conn.execute(
                    "SELECT product_id FROM product_network_variants "
                    "WHERE UPPER(article)=? AND trade_network=?",
                    (up, trade_network),
                ).fetchone()
                if vrow:
                    pid = vrow[0]
            if pid is None:
                vrow = conn.execute(
                    "SELECT product_id FROM product_network_variants WHERE UPPER(article)=?",
                    (up,),
                ).fetchone()
                if vrow:
                    pid = vrow[0]
            if pid is not None:
                prow = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                if prow:
                    return prow
            return conn.execute(
                "SELECT * FROM products WHERE UPPER(article)=?", (up,)
            ).fetchone()
        finally:
            conn.close()

    def get_product_by_barcode(self, barcode: str, trade_network: str = None):
        """Поиск товара по штрихкоду.

        Сначала ищет среди вариантов по торговой сети (product_network_variants):
        при переданном trade_network — приоритет точного совпадения сети, затем
        любой вариант. Если не найдено — fallback на products.barcode (старое
        поведение, полная обратная совместимость для орг без вариантов).
        """
        raw = (barcode or "").strip()
        if not raw:
            return None
        up = raw.upper()
        conn = self.get_connection()
        try:
            pid = None
            if trade_network:
                vrow = conn.execute(
                    "SELECT product_id FROM product_network_variants "
                    "WHERE barcode=? AND trade_network=?",
                    (raw, trade_network),
                ).fetchone()
                if not vrow:
                    vrow = conn.execute(
                        "SELECT product_id FROM product_network_variants "
                        "WHERE UPPER(barcode)=? AND trade_network=?",
                        (up, trade_network),
                    ).fetchone()
                if vrow:
                    pid = vrow[0]
            if pid is None:
                vrow = conn.execute(
                    "SELECT product_id FROM product_network_variants WHERE barcode=?",
                    (raw,),
                ).fetchone()
                if not vrow:
                    vrow = conn.execute(
                        "SELECT product_id FROM product_network_variants WHERE UPPER(barcode)=?",
                        (up,),
                    ).fetchone()
                if vrow:
                    pid = vrow[0]
            if pid is not None:
                prow = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                if prow:
                    return prow
            row = conn.execute(
                "SELECT * FROM products WHERE barcode=?", (raw,)
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM products WHERE UPPER(barcode)=?", (up,)
                ).fetchone()
            return row
        finally:
            conn.close()

    # ── Варианты товара по торговой сети ────────────────────────────────────

    def get_product_variants(self, product_id: int) -> list:
        """Все варианты товара по сетям.
        Строка: id[0] product_id[1] trade_network[2] article[3] barcode[4] created_at[5]
        """
        conn = self.get_connection()
        try:
            return conn.execute(
                "SELECT id, product_id, trade_network, article, barcode, created_at "
                "FROM product_network_variants WHERE product_id=? ORDER BY trade_network",
                (product_id,),
            ).fetchall()
        finally:
            conn.close()

    def get_product_variant(self, product_id: int, trade_network: str):
        """Вариант товара для конкретной сети или None."""
        conn = self.get_connection()
        try:
            return conn.execute(
                "SELECT id, product_id, trade_network, article, barcode, created_at "
                "FROM product_network_variants WHERE product_id=? AND trade_network=?",
                (product_id, trade_network),
            ).fetchone()
        finally:
            conn.close()

    def get_product_variants_map(self, product_id: int) -> dict:
        """{trade_network: {'article': str|None, 'barcode': str|None}} для товара."""
        result = {}
        for row in self.get_product_variants(product_id):
            result[row[2]] = {'article': row[3], 'barcode': row[4]}
        return result

    def set_product_variant(self, product_id: int, trade_network: str,
                            article: str = None, barcode: str = None) -> dict:
        """Upsert варианта товара для сети. Если article и barcode оба пустые —
        вариант удаляется. Возвращает {'ok': bool, 'error': str|None}.
        Ошибка 'barcode_conflict' — штрихкод уже занят другим товаром/вариантом.
        """
        net = (trade_network or "").strip()
        if not net:
            return {"ok": False, "error": "no_network"}
        art = (article or "").strip().upper() or None
        bc = (barcode or "").strip() or None
        conn = self.get_connection()
        try:
            if art is None and bc is None:
                conn.execute(
                    "DELETE FROM product_network_variants WHERE product_id=? AND trade_network=?",
                    (product_id, net),
                )
                conn.commit()
                return {"ok": True, "error": None}
            if bc is not None:
                clash = conn.execute(
                    "SELECT product_id FROM product_network_variants "
                    "WHERE barcode=? AND NOT (product_id=? AND trade_network=?)",
                    (bc, product_id, net),
                ).fetchone()
                if clash:
                    return {"ok": False, "error": "barcode_conflict"}
                # Коллизия с дефолтным штрихкодом ДРУГОГО товара (products.barcode):
                # тот же штрихкод у своего товара — ОК (это и есть дефолт для сети).
                pclash = conn.execute(
                    "SELECT id FROM products WHERE barcode=? AND id<>?",
                    (bc, product_id),
                ).fetchone()
                if pclash:
                    return {"ok": False, "error": "barcode_conflict"}
            existing = conn.execute(
                "SELECT id FROM product_network_variants WHERE product_id=? AND trade_network=?",
                (product_id, net),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE product_network_variants SET article=?, barcode=? WHERE id=?",
                    (art, bc, existing[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO product_network_variants (product_id, trade_network, article, barcode) "
                    "VALUES (?, ?, ?, ?)",
                    (product_id, net, art, bc),
                )
            conn.commit()
            return {"ok": True, "error": None}
        except sqlite3.IntegrityError:
            conn.rollback()
            return {"ok": False, "error": "barcode_conflict"}
        finally:
            conn.close()

    def delete_product_variant(self, product_id: int, trade_network: str) -> bool:
        conn = self.get_connection()
        try:
            conn.execute(
                "DELETE FROM product_network_variants WHERE product_id=? AND trade_network=?",
                (product_id, trade_network),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_effective_product_codes(self, product_id: int, trade_network: str = None) -> dict:
        """Действующие коды товара: вариант сети, иначе значения по умолчанию из products.
        Возвращает {'article': str|None, 'barcode': str|None, 'is_variant': bool}.
        """
        conn = self.get_connection()
        try:
            if trade_network:
                vrow = conn.execute(
                    "SELECT article, barcode FROM product_network_variants "
                    "WHERE product_id=? AND trade_network=?",
                    (product_id, trade_network),
                ).fetchone()
                if vrow and (vrow[0] or vrow[1]):
                    return {"article": vrow[0], "barcode": vrow[1], "is_variant": True}
            prow = conn.execute(
                "SELECT article, barcode FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if prow:
                return {"article": prow[0], "barcode": prow[1], "is_variant": False}
            return {"article": None, "barcode": None, "is_variant": False}
        finally:
            conn.close()

    def get_network_for_shop(self, shop_name: str) -> str:
        """Торговая сеть магазина (из users) или None."""
        if not shop_name:
            return None
        conn = self.get_connection()
        try:
            row = conn.execute(
                "SELECT trade_network FROM users "
                "WHERE shop_name=? AND trade_network IS NOT NULL AND trade_network NOT IN ('', 'System') "
                "LIMIT 1",
                (shop_name,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def delete_product(self, product_id):
        """Удаление товара и связанных записей motivation_schedule + варианты по сетям"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM motivation_schedule WHERE product_id = ?', (product_id,))
        cursor.execute('DELETE FROM product_network_variants WHERE product_id = ?', (product_id,))
        cursor.execute('DELETE FROM products WHERE id = ?', (product_id,))
        conn.commit()
        conn.close()

    # ── Галерея фотографий товара ──────────────────────────────────────────

    def get_product_photos(self, product_id):
        """Возвращает список фото товара отсортированных по sort_order.
        Каждая строка: id[0] product_id[1] photo_url[2] sort_order[3] source[4] created_at[5]
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, product_id, photo_url, sort_order, source, created_at '
            'FROM product_photos WHERE product_id = ? ORDER BY sort_order, id',
            (product_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def add_product_photo(self, product_id, photo_url, source='web'):
        """Добавить фото товара. source: 'web' | 'telegram'"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM product_photos WHERE product_id = ?',
            (product_id,)
        )
        next_order = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO product_photos (product_id, photo_url, sort_order, source) VALUES (?, ?, ?, ?)',
            (product_id, photo_url, next_order, source)
        )
        photo_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return photo_id

    def delete_product_photo(self, photo_id):
        """Удалить запись о фото по id. Возвращает photo_url удалённой записи или None."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT photo_url FROM product_photos WHERE id = ?', (photo_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute('DELETE FROM product_photos WHERE id = ?', (photo_id,))
            conn.commit()
        conn.close()
        return row[0] if row else None

    def delete_all_product_photos(self, product_id):
        """Удалить все записи фото товара. Возвращает список photo_url."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT photo_url FROM product_photos WHERE product_id = ?', (product_id,))
        urls = [r[0] for r in cursor.fetchall()]
        cursor.execute('DELETE FROM product_photos WHERE product_id = ?', (product_id,))
        conn.commit()
        conn.close()
        return urls

    def reorder_product_photos(self, photo_ids):
        """Обновить sort_order: photo_ids — список id в нужном порядке."""
        conn = self.get_connection()
        cursor = conn.cursor()
        for i, pid in enumerate(photo_ids):
            cursor.execute('UPDATE product_photos SET sort_order = ? WHERE id = ?', (i, pid))
        conn.commit()
        conn.close()

    def get_all_categories(self):
        """Получение всех категорий товаров"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT category FROM products ORDER BY category')
        categories = [row[0] for row in cursor.fetchall()]
        conn.close()
        return categories

    def get_user_count(self) -> int:
        """SELECT COUNT(*) вместо загрузки всех строк — только для счётчика."""
        conn = self.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        except Exception:
            return 0
        finally:
            conn.close()

    def get_all_users(self, shop_name=None, city=None, trade_network=None,
                      shop_names=None, cities=None, trade_networks=None):
        conn = self.get_connection()
        cursor = conn.cursor()
        query = 'SELECT * FROM users WHERE 1=1'
        params = []
        if shop_name:
            query += ' AND shop_name = ?'
            params.append(shop_name)
        elif shop_names:
            ph = ','.join('?' * len(shop_names))
            query += f' AND shop_name IN ({ph})'
            params.extend(shop_names)
        elif city:
            query += ' AND city = ?'
            params.append(city)
        elif cities:
            ph = ','.join('?' * len(cities))
            query += f' AND city IN ({ph})'
            params.extend(cities)
        elif trade_network:
            query += ' AND trade_network = ?'
            params.append(trade_network)
        elif trade_networks:
            ph = ','.join('?' * len(trade_networks))
            query += f' AND trade_network IN ({ph})'
            params.extend(trade_networks)
        cursor.execute(query, params)
        users = cursor.fetchall()
        conn.close()
        return users

    def get_pending_payment_requests(self):
        """Получение всех ожидающих заявок на оплату с данными пользователей"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT pr.id, pr.user_id, pr.plan_type, pr.amount, pr.status, 
                   pr.payment_proof_file_id, pr.created_at, pr.processed_at, pr.processed_by,
                   u.first_name, u.last_name, u.shop_name
            FROM payment_requests pr
            JOIN users u ON pr.user_id = u.id
            WHERE pr.status = 'pending'
            ORDER BY pr.created_at DESC
        ''')
        requests = cursor.fetchall()
        conn.close()
        return requests

    def get_payment_request_by_id(self, request_id):
        """Получение конкретной заявки на оплату по ID"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT pr.id, pr.user_id, pr.plan_type, pr.amount, pr.status,
                       pr.payment_proof_file_id, pr.created_at, pr.processed_at, pr.processed_by,
                       u.first_name, u.last_name, u.shop_name
                FROM payment_requests pr
                JOIN users u ON pr.user_id = u.id
                WHERE pr.id = ?
            ''', (request_id,))
            result = cursor.fetchone()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении заявки {request_id}: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def create_subscription(self, user_id, plan_type):
        """Создание или продление подписки для пользователя"""
        try:
            if user_id is None:
                return False
                
            conn = self.get_connection()
            cursor = conn.cursor()

            # Получаем информацию о тарифном плане
            cursor.execute('SELECT duration_days FROM subscription_plans WHERE name = ?', (plan_type,))
            plan_info = cursor.fetchone()

            if plan_info:
                duration_days = plan_info[0]
                if duration_days == 0:
                    # Бесплатный план - бессрочная подписка
                    end_date = '9999-12-31 23:59:59'
                else:
                    # Платный план - добавляем указанное количество дней
                    end_date = (datetime.now() + timedelta(days=duration_days)).strftime('%Y-%m-%d %H:%M:%S')
            else:
                # Fallback для новых пользователей - создаем бесплатную подписку
                end_date = '9999-12-31 23:59:59'

            # Проверяем, есть ли уже подписка у пользователя
            cursor.execute('SELECT id FROM subscriptions WHERE user_id = ?', (user_id,))
            existing = cursor.fetchone()

            if existing:
                # Обновляем существующую подписку
                cursor.execute('''
                    UPDATE subscriptions 
                    SET plan_type = ?, end_date = ?, start_date = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                ''', (plan_type, end_date, user_id))
            else:
                # Создаем новую подписку
                cursor.execute('''
                    INSERT INTO subscriptions (user_id, plan_type, end_date)
                    VALUES (?, ?, ?)
                ''', (user_id, plan_type, end_date))

            conn.commit()
            conn.close()
            return True

        except Exception as e:
            logger.error(f"Ошибка в create_subscription: {e}")
            return False

    def create_trial_subscription(self, user_id: int, plan_type: str, days: int) -> bool:
        """Создаёт пробную подписку для нового пользователя.
        Вызывается только если у пользователя ещё нет ни одной подписки."""
        try:
            from datetime import datetime, timedelta
            end_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            conn = self.get_connection()
            cursor = conn.cursor()
            # Двойная проверка: не выдавать повторно
            cursor.execute('SELECT id FROM subscriptions WHERE user_id = ?', (user_id,))
            if cursor.fetchone():
                conn.close()
                return False
            cursor.execute('''
                INSERT INTO subscriptions (user_id, plan_type, end_date, is_trial)
                VALUES (?, ?, ?, 1)
            ''', (user_id, plan_type, end_date))
            conn.commit()
            conn.close()
            logger.info(f"Trial subscription '{plan_type}' for {days} days granted to user_id={user_id}")
            return True
        except Exception as e:
            logger.error(f"Ошибка в create_trial_subscription: {e}")
            return False

    def get_recently_expired_trials(self):
        """Пользователи с пробным периодом, истёкшим до 48 часов назад (для upsell-пуша)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.telegram_id, u.first_name, s.plan_type, s.end_date, u.id
                FROM subscriptions s
                JOIN users u ON s.user_id = u.id
                WHERE s.is_trial = 1
                  AND datetime(s.end_date) <= datetime('now')
                  AND datetime(s.end_date) >= datetime('now', '-48 hours')
            ''')
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"get_recently_expired_trials: {e}")
            return []

    def has_sent_reminder(self, user_id: int, threshold: int, subscription_end: str) -> bool:
        """Проверяет, было ли уже отправлено напоминание для данного порога истечения."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 1 FROM subscription_reminder_log
                WHERE user_id = ? AND threshold = ? AND subscription_end = ?
            ''', (user_id, threshold, subscription_end))
            result = cursor.fetchone()
            conn.close()
            return result is not None
        except Exception as e:
            logger.error(f"Ошибка в has_sent_reminder: {e}")
            return False

    def mark_reminder_sent(self, user_id: int, threshold: int, subscription_end: str) -> None:
        """Помечает, что напоминание для данного порога уже было отправлено."""
        try:
            conn = self.get_connection()
            conn.execute('''
                INSERT OR IGNORE INTO subscription_reminder_log (user_id, threshold, subscription_end)
                VALUES (?, ?, ?)
            ''', (user_id, threshold, subscription_end))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка в mark_reminder_sent: {e}")

    def clear_reminders(self, user_id: int) -> None:
        """Сбрасывает лог напоминаний пользователя (при продлении подписки)."""
        try:
            conn = self.get_connection()
            conn.execute('DELETE FROM subscription_reminder_log WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка в clear_reminders: {e}")

    def is_subscription_active(self, user_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM subscriptions 
            WHERE user_id = ? AND datetime(end_date) > datetime('now')
        ''', (user_id,))
        result = cursor.fetchone()[0]
        conn.close()
        return result > 0

    def get_subscription_limits(self, user_id):
        """Получение лимитов для подписки пользователя"""
        from env_manager import env_manager as _em

        # Получаем telegram_id пользователя
        user_info = self.get_user_by_id(user_id)
        if user_info and _em.is_super_admin(user_info[1]):  # telegram_id находится в позиции 1
            # Супер-администратор имеет неограниченный доступ
            return {
                'max_products': -1,
                'max_shops': -1,
                'max_sales_per_month': -1,
                'can_export_reports': True,
                'can_view_analytics': True,
                'can_use_notifications': True
            }

        # Получаем информацию о подписке пользователя
        subscription_data = self.get_user_subscription(user_id)
        if subscription_data:
            plan_type = subscription_data[2]
        else:
            # Новый пользователь - создаем бесплатную подписку
            self.create_subscription(user_id, 'Бесплатный')
            plan_type = 'Бесплатный'



        # Получаем лимиты из базы данных динамически
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT max_products, max_shops, max_sales_per_month, 
                   can_export_reports, can_view_analytics, can_use_notifications, can_use_integrations
            FROM subscription_plans 
            WHERE name = ? AND is_active = TRUE
        ''', (plan_type,))

        result = cursor.fetchone()

        if result:
            conn.close()
            return {
                'max_products': result[0],
                'max_shops': result[1], 
                'max_sales_per_month': result[2],
                'can_export_reports': bool(result[3]),
                'can_view_analytics': bool(result[4]),
                'can_use_notifications': bool(result[5]),
                'can_use_integrations': bool(result[6]),
            }
        else:
            # Fallback на бесплатный план если план не найден
            cursor.execute('''
                SELECT max_products, max_shops, max_sales_per_month,
                       can_export_reports, can_view_analytics, can_use_notifications, can_use_integrations
                FROM subscription_plans 
                WHERE name = 'Бесплатный' AND is_active = TRUE
            ''')
            result = cursor.fetchone()
            conn.close()

            if result:
                return {
                    'max_products': result[0],
                    'max_shops': result[1],
                    'max_sales_per_month': result[2], 
                    'can_export_reports': bool(result[3]),
                    'can_view_analytics': bool(result[4]),
                    'can_use_notifications': bool(result[5]),
                    'can_use_integrations': bool(result[6]),
                }
            else:
                # Жесткий fallback если в базе ничего нет
                return {
                    'max_products': 50,
                    'max_shops': 1,
                    'max_sales_per_month': 100,
                    'can_export_reports': False,
                    'can_view_analytics': False,
                    'can_use_notifications': False,
                    'can_use_integrations': False,
                }

    def get_subscription_tier_level(self, plan_type):
        """Получение уровня тарифного плана для сравнения (чем больше число, тем лучше план)"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Получаем уровень на основе цены и продолжительности
        cursor.execute('''
            SELECT price, duration_days FROM subscription_plans 
            WHERE name = ? AND is_active = TRUE
        ''', (plan_type,))

        result = cursor.fetchone()
        conn.close()

        if result:
            price, duration = result
            # Чем выше цена, тем выше уровень
            if price == 0:
                return 0  # Бесплатный
            elif price <= 600:
                return 1  # Базовый
            elif price <= 1500:
                return 2  # Стандарт
            else:
                return 3  # Премиум/VIP
        else:
            return 0  # По умолчанию бесплатный

    def check_subscription_downgrade(self, user_id, new_plan_type):
        """Проверка на понижение тарифного плана"""
        current_subscription = self.get_user_subscription(user_id)

        if not current_subscription:
            return False, None  # Нет активной подписки

        current_plan = current_subscription[2]
        current_end_date = current_subscription[4]

        # Проверяем, активна ли текущая подписка
        from datetime import datetime
        try:
            end_datetime = datetime.fromisoformat(current_end_date)
            if end_datetime <= datetime.now():
                return False, None  # Подписка уже истекла
        except Exception:
            return False, None

        current_level = self.get_subscription_tier_level(current_plan)
        new_level = self.get_subscription_tier_level(new_plan_type)

        if new_level < current_level:
            return True, {
                'current_plan': current_plan,
                'current_end_date': current_end_date,
                'new_plan': new_plan_type,
                'end_datetime': end_datetime
            }

        return False, None

    def create_scheduled_subscription(self, user_id, plan_type, start_date):
        """Создание отложенной подписки, которая начнется в указанную дату"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Проверяем есть ли таблица scheduled_subscriptions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_type TEXT NOT NULL,
                start_date TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        cursor.execute('''
            INSERT INTO scheduled_subscriptions (user_id, plan_type, start_date)
            VALUES (?, ?, ?)
        ''', (user_id, plan_type, start_date))

        conn.commit()
        conn.close()
        return True

    def get_user_subscription(self, user_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM subscriptions 
            WHERE user_id = ? AND datetime(end_date) > datetime('now')
            ORDER BY end_date DESC LIMIT 1
        ''', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result

    def has_pending_payment_request(self, user_id):
        """Проверяет наличие уже активной pending-заявки у пользователя."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM payment_requests WHERE user_id = ? AND status = 'pending'",
                (user_id,)
            )
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def create_payment_request(self, user_id, plan_type, amount, payment_proof_file_id, promocode_id=None):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO payment_requests 
            (user_id, plan_type, amount, payment_proof_file_id, promocode_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, plan_type, amount, payment_proof_file_id, promocode_id))
        request_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return request_id

    def confirm_payment_request(self, request_id, admin_id):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Получаем данные заявки перед подтверждением
            cursor.execute('''
                SELECT user_id, plan_type, promocode_id
                FROM payment_requests 
                WHERE id = ? AND status = 'pending'
            ''', (request_id,))

            request_data = cursor.fetchone()
            if not request_data:
                conn.close()
                return False

            user_id, plan_type, promocode_id = request_data

            # Атомарный переход pending → approved.
            # Защита от двойной обработки/гонки: если параллельная конфирмация уже
            # перевела заявку, rowcount=0 → выходим, НЕ выдавая грант/надстройку повторно.
            cursor.execute('''
                UPDATE payment_requests 
                SET status = 'approved', processed_at = CURRENT_TIMESTAMP, processed_by = ?
                WHERE id = ? AND status = 'pending'
            ''', (admin_id, request_id))
            if cursor.rowcount == 0:
                conn.close()
                return False

            conn.commit()
            conn.close()

            # Обработка надстроек (add-ons): addon_shops_1 или addon_products_1
            if plan_type and plan_type.startswith('addon_'):
                # Специальный случай: AI-инсайты сети — грант через billing_module_subs
                if plan_type == 'addon_ai_network_insights_1':
                    _user_info = self.get_user_by_id(user_id)
                    if _user_info:
                        _tg_id = _user_info[1]
                        _res = self.grant_billing_item(
                            user_telegram_id=_tg_id,
                            item_type='extension',
                            item_key='ai_network_insights',
                            duration_days=30,
                            price_paid=399.0,
                            granted_by='payment_confirmed',
                            payment_request_id=request_id,
                        )
                        if not _res:
                            try:
                                _cc = self.get_connection()
                                _cc.execute(
                                    "UPDATE payment_requests SET status='pending', processed_at=NULL, processed_by=NULL WHERE id=?",
                                    (request_id,)
                                )
                                _cc.commit()
                                _cc.close()
                                logger.error(
                                    "confirm_payment_request: выдача ai_network_insights не удалась для request_id=%s, user_id=%s.",
                                    request_id, user_id
                                )
                            except Exception as _ce:
                                logger.error("confirm_payment_request ai_network_insights compensation: %s", _ce)
                            return False
                    return True

                _parts = plan_type.split('_')
                _addon_ok = True
                if len(_parts) >= 3:
                    _addon_key = _parts[1]   # 'shops' или 'products'
                    try:
                        _qty = int(_parts[2])
                    except (ValueError, IndexError):
                        _qty = 1
                    _user_info = self.get_user_by_id(user_id)
                    if _user_info:
                        _tg_id = _user_info[1]
                        _addon_res = -1  # неизвестный addon_key не считаем ошибкой выдачи
                        if _addon_key == 'shops':
                            _addon_res = self.create_subscription_addon(_tg_id, 'extra_shops', _qty, 150.0 * _qty, days=30, payment_request_id=request_id)
                        elif _addon_key == 'products':
                            _addon_res = self.create_subscription_addon(_tg_id, 'extra_products', _qty, 100.0 * _qty, days=30, payment_request_id=request_id)
                        if _addon_res == 0:
                            _addon_ok = False
                if not _addon_ok:
                    # Компенсация: надстройка не выдана → откат approved → pending,
                    # чтобы админ мог повторить (иначе оплата без гранта).
                    try:
                        _cc = self.get_connection()
                        _cc.execute(
                            "UPDATE payment_requests SET status='pending', processed_at=NULL, processed_by=NULL WHERE id=?",
                            (request_id,)
                        )
                        _cc.commit()
                        _cc.close()
                        logger.error(
                            "confirm_payment_request: выдача надстройки не удалась для request_id=%s, "
                            "user_id=%s, plan=%s. Статус заявки сброшен в pending.",
                            request_id, user_id, plan_type
                        )
                    except Exception as _ce:
                        logger.error("confirm_payment_request addon compensation: %s", _ce)
                    return False
                return True

            # Обработка модульных подписок: module_analytics, bundle_starter, etc.
            if plan_type and (plan_type.startswith('module_') or plan_type.startswith('bundle_')):
                _user_info = self.get_user_by_id(user_id)
                if _user_info:
                    _tg_id = _user_info[1]
                    _item_type = 'module' if plan_type.startswith('module_') else 'bundle'
                    _item_key = plan_type[len(_item_type) + 1:]
                    try:
                        _pc = self.get_connection()
                        _pr_row = _pc.execute(
                            "SELECT amount FROM payment_requests WHERE id=?", (request_id,)
                        ).fetchone()
                        _pc.close()
                        _price = float(_pr_row[0]) if _pr_row else 0.0
                    except Exception:
                        _price = 0.0
                    self.grant_billing_item(
                        user_telegram_id=_tg_id,
                        item_type=_item_type,
                        item_key=_item_key,
                        duration_days=30,
                        price_paid=_price,
                        granted_by='payment',
                        note='Оплачен из веб-кабинета',
                        payment_request_id=request_id,
                    )
                return True

            # Создаем подписку (и сбрасываем старые напоминания — подписка продлена)
            success = self.create_subscription(user_id, plan_type)
            if not success:
                # Компенсирующая транзакция: откатываем статус payment_requests → pending,
                # чтобы админ мог повторить подтверждение вместо "потери" оплаты
                try:
                    conn2 = self.get_connection()
                    conn2.execute(
                        "UPDATE payment_requests SET status='pending', processed_at=NULL, processed_by=NULL WHERE id=?",
                        (request_id,)
                    )
                    conn2.commit()
                    conn2.close()
                    logger.error(
                        f"confirm_payment_request: create_subscription вернул False для "
                        f"request_id={request_id}, user_id={user_id}, plan={plan_type}. "
                        f"Статус заявки сброшен в pending."
                    )
                except Exception as rb_err:
                    logger.error(f"confirm_payment_request: не удалось откатить статус заявки {request_id}: {rb_err}")
                return False

            # Единый путь бота и веб-кабинета: продлеваем тариф организации (main.db),
            # если плательщик — владелец/админ организации. Раньше это делал только бот.
            self._apply_org_subscription_after_payment(user_id, plan_type)

            # Автоматически выдаём модульные гранты для legacy-планов (бот-покупки СБП).
            # billing_utils больше не читает таблицу subscriptions, поэтому каждое
            # подтверждение legacy-плана сразу конвертируется в гранты billing_module_subs.
            _LEGACY_GRANT_MAP = {
                "Базовый":  ["analytics", "notifications"],
                "Стандарт": ["analytics", "notifications", "integrations"],
                "Премиум":  ["analytics", "team", "notifications",
                             "plans_motivation", "ai_assistant", "integrations", "chat"],
            }
            _modules_to_grant = _LEGACY_GRANT_MAP.get(plan_type, [])
            if _modules_to_grant:
                _user_info = self.get_user_by_id(user_id)
                if _user_info:
                    _tg_id = _user_info[1]
                    try:
                        _pc = self.get_connection()
                        _pr_row = _pc.execute(
                            "SELECT amount FROM payment_requests WHERE id=?", (request_id,)
                        ).fetchone()
                        _pc.close()
                        _price = float(_pr_row[0]) if _pr_row else 0.0
                    except Exception:
                        _price = 0.0
                    for _mk in _modules_to_grant:
                        try:
                            self.grant_billing_item(
                                user_telegram_id=_tg_id,
                                item_type='module',
                                item_key=_mk,
                                duration_days=30,
                                price_paid=_price / len(_modules_to_grant),
                                granted_by='payment',
                                note=f'Конвертирован из тарифа {plan_type}',
                                payment_request_id=request_id,
                            )
                        except Exception as _ge:
                            logger.error(f"confirm_payment_request: grant {_mk} для {_tg_id}: {_ge}")

            self.clear_reminders(user_id)

            # Применяем промокод только при успешном подтверждении оплаты
            if promocode_id:
                try:
                    self.apply_promocode(promocode_id, user_id)
                except Exception as e:
                    logger.error(f"Ошибка применения промокода {promocode_id} при подтверждении заявки {request_id}: {e}")

            return True

        except sqlite3.OperationalError as e:
            logger.error(f"Ошибка базы данных в confirm_payment_request: {e}")
            return False
        except Exception as e:
            logger.error(f"Общая ошибка в confirm_payment_request: {e}")
            return False

    def _apply_org_subscription_after_payment(self, user_id, plan_type):
        """Продлевает тариф организации (organizations в main.db) при подтверждении
        оплаты её владельцем/админом. Единый путь для бота и веб-кабинета —
        вызывается из confirm_payment_request (legacy-планы)."""
        try:
            import sqlite3 as _sql3
            from datetime import datetime as _dt, timedelta as _td
            _user_info = self.get_user_by_id(user_id)
            if not _user_info:
                return
            user_telegram_id = _user_info[1]
            _sb = _sql3.connect('data/shop_bot.db')
            _row = _sb.execute(
                "SELECT duration_days FROM subscription_plans WHERE name = ?",
                (plan_type,)
            ).fetchone()
            _sb.close()
            _duration = _row[0] if _row and _row[0] else 0
            _org_expires = (
                (_dt.now() + _td(days=_duration)).strftime('%Y-%m-%d %H:%M:%S')
                if _duration > 0 else None
            )
            main_conn = _sql3.connect('data/main.db')
            main_cursor = main_conn.cursor()
            main_cursor.execute(
                "SELECT o.id FROM organizations o "
                "JOIN user_org_mapping m ON o.id = m.org_id "
                "WHERE m.telegram_id = ? AND m.role IN ('owner', 'admin')",
                (user_telegram_id,)
            )
            org_row = main_cursor.fetchone()
            if org_row:
                main_cursor.execute(
                    "UPDATE organizations SET subscription_plan = ?, subscription_end = ? WHERE id = ?",
                    (plan_type, _org_expires, org_row[0])
                )
                main_conn.commit()
            main_conn.close()
        except Exception as e:
            logger.error(
                f"_apply_org_subscription_after_payment: user_id={user_id}, plan={plan_type}: {e}"
            )

    def get_user_by_id(self, user_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = cursor.fetchone()
        conn.close()
        return user

    def get_org_owner_tg_id(self):
        """Возвращает telegram_id владельца организации из user_org_mapping в main.db.

        Ищет по db_path == self.db_file в таблице organizations, затем
        находит пользователя с role='owner' в user_org_mapping для этого org_id.
        """
        try:
            import sqlite3 as _sqlite3
            main_conn = _sqlite3.connect('data/main.db')
            cursor = main_conn.cursor()
            cursor.execute(
                "SELECT id FROM organizations WHERE db_path = ? LIMIT 1",
                (self.db_file,)
            )
            row = cursor.fetchone()
            if not row:
                main_conn.close()
                return None
            org_id = row[0]
            cursor.execute(
                "SELECT telegram_id FROM user_org_mapping "
                "WHERE org_id = ? AND role = 'owner' AND is_active = 1 LIMIT 1",
                (org_id,)
            )
            row = cursor.fetchone()
            main_conn.close()
            return row[0] if row else None
        except Exception:
            return None

    def get_org_name(self):
        """Возвращает название организации из таблицы organizations в main.db."""
        try:
            import sqlite3 as _sqlite3
            main_conn = _sqlite3.connect('data/main.db')
            cursor = main_conn.cursor()
            cursor.execute(
                "SELECT name FROM organizations WHERE db_path = ? LIMIT 1",
                (self.db_file,)
            )
            row = cursor.fetchone()
            main_conn.close()
            return row[0] if row else "Организация"
        except Exception:
            return "Организация"

    def get_sales_summary_today(self):
        """Сводка по продажам за сегодня: (кол-во, сумма)."""
        try:
            from datetime import date
            today = date.today().isoformat()
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*), COALESCE(SUM(quantity_sold * sale_price), 0) "
                "FROM sales WHERE date(sale_date) = ?",
                (today,)
            )
            row = cursor.fetchone()
            conn.close()
            cnt, total = (row[0] or 0), (row[1] or 0)
            return f"{cnt} продаж на сумму {total:,.0f} руб."
        except Exception:
            return "нет данных"

    def get_sales_summary_month(self):
        """Сводка по продажам за текущий месяц: (кол-во, сумма)."""
        try:
            from datetime import date
            today = date.today()
            month_start = today.replace(day=1).isoformat()
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*), COALESCE(SUM(quantity_sold * sale_price), 0) "
                "FROM sales WHERE date(sale_date) >= ?",
                (month_start,)
            )
            row = cursor.fetchone()
            conn.close()
            cnt, total = (row[0] or 0), (row[1] or 0)
            return f"{cnt} продаж на сумму {total:,.0f} руб."
        except Exception:
            return "нет данных"

    def get_top_products_month(self, limit: int = 3):
        """Топ-N товаров по выручке за текущий месяц.
        Возвращает список кортежей (product_name, qty, revenue)."""
        try:
            from datetime import date
            month_start = date.today().replace(day=1).isoformat()
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT p.name, SUM(s.quantity_sold) AS qty, "
                "SUM(s.quantity_sold * s.sale_price) AS revenue "
                "FROM sales s JOIN products p ON s.product_id = p.id "
                "WHERE date(s.sale_date) >= ? "
                "GROUP BY p.id, p.name ORDER BY revenue DESC LIMIT ?",
                (month_start, limit),
            )
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception:
            return []

    def get_active_sellers_month(self, limit: int = 10):
        """Продавцы с продажами в текущем месяце — тонкая обёртка над get_sales_ranking().
        Возвращает список кортежей (first_name, last_name, shop_name, revenue)."""
        try:
            from datetime import date
            month_start = date.today().replace(day=1).isoformat()
            today_str   = date.today().isoformat()
            rows = self.get_sales_ranking(start_date=month_start, end_date=today_str)
            # get_sales_ranking returns 9 cols:
            # [0] first_name [1] last_name [2] shop_name [3] total_sold
            # [4] total_revenue [5] total_sales [6] total_earnings
            # [7] user_db_id   [8] username
            return [(r[0], r[1], r[2], r[4]) for r in rows[:limit]]
        except Exception:
            return []

    def get_plans_with_progress(self):
        """Активные планы продаж с текущим прогрессом (с учётом target_type и filter_type).
        Возвращает список словарей с ключами: label, target, current, pct,
        seller_name (для seller-планов), seller_breakdown (для shop-планов, top-5)."""
        try:
            import json as _json2
            from datetime import date, timedelta
            today = date.today()
            plans = self.get_sales_plans(active_only=True)
            if not plans:
                return []
            result = []
            for plan in plans:
                plan_type   = plan[1] or "monthly"
                metric_type = plan[2] or "turnover"
                target_val  = float(plan[3] or 0)
                target_type = plan[4] or "shop"
                user_id     = plan[5]
                shop_name   = plan[6]
                filter_type = plan[7] or "all"
                filter_value = plan[8]
                fn, ln      = plan[12] or "", plan[13] or ""
                # date range for current period
                if plan_type == "daily":
                    start = today.isoformat()
                    end   = today.isoformat()
                elif plan_type == "weekly":
                    start = (today - timedelta(days=today.weekday())).isoformat()
                    end   = today.isoformat()
                else:
                    start = today.replace(day=1).isoformat()
                    end   = today.isoformat()
                # build canonical query identical to _calc_actual_for_plan in salary calcs
                metric_expr = (
                    "COALESCE(SUM(s.sale_price * s.quantity_sold), 0)"
                    if metric_type == "turnover"
                    else "COALESCE(SUM(s.quantity_sold), 0)"
                )
                conditions = ["date(s.sale_date) BETWEEN date(?) AND date(?)"]
                params: list = [start, end]
                if target_type == "seller" and user_id:
                    conditions.append("s.user_id = ?")
                    params.append(user_id)
                elif target_type == "shop" and shop_name:
                    conditions.append("s.shop_name = ?")
                    params.append(shop_name)
                join_clause = ""
                if filter_type == "category" and filter_value:
                    join_clause = "JOIN products p ON s.product_id = p.id"
                    try:
                        cats = _json2.loads(filter_value)
                        if isinstance(cats, list) and cats:
                            ph = ",".join("?" * len(cats))
                            conditions.append(f"p.category IN ({ph})")
                            params.extend(cats)
                        else:
                            conditions.append("p.category = ?")
                            params.append(filter_value)
                    except Exception:
                        conditions.append("p.category = ?")
                        params.append(filter_value)
                elif filter_type == "product" and filter_value:
                    try:
                        ids = _json2.loads(filter_value)
                        if ids:
                            ph = ",".join("?" * len(ids))
                            conditions.append(f"s.product_id IN ({ph})")
                            params.extend(ids)
                    except Exception:
                        pass
                where = " AND ".join(conditions)
                q = f"SELECT {metric_expr} FROM sales s {join_clause} WHERE {where}"
                try:
                    conn = self.get_connection()
                    row = conn.execute(q, params).fetchone()
                    conn.close()
                    current = float(row[0] or 0) if row else 0.0
                except Exception:
                    current = 0.0
                pct = round(current / target_val * 100) if target_val else 0
                # human label
                scope = ""
                if target_type == "seller" and (fn or ln):
                    scope = f"{fn} {ln}".strip()
                elif target_type == "shop" and shop_name:
                    scope = shop_name
                period_ru = {"daily": "день", "weekly": "неделю", "monthly": "месяц"}.get(plan_type, plan_type)
                metric_ru = "выручка" if metric_type == "turnover" else "шт."
                label = f"{'на ' + scope + ': ' if scope else ''}{metric_ru} за {period_ru}"
                entry: dict = {
                    "label":   label,
                    "target":  target_val,
                    "current": current,
                    "pct":     pct,
                }
                # For seller-level plans expose the seller name explicitly
                if target_type == "seller" and (fn or ln):
                    entry["seller_name"] = f"{fn} {ln}".strip()
                # For shop-level plans add per-seller breakdown (top-5 by metric)
                if target_type == "shop":
                    try:
                        breakdown_conditions = [c for c in conditions if "s.user_id" not in c]
                        bd_where = " AND ".join(breakdown_conditions)
                        bd_params = [p for p, c in zip(params, conditions) if "s.user_id" not in c]
                        # rebuild params list correctly (conditions and params are parallel for positional ?)
                        bd_params2: list = [start, end]
                        if shop_name:
                            bd_params2.append(shop_name)
                        if filter_type == "category" and filter_value:
                            try:
                                cats2 = _json2.loads(filter_value)
                                if isinstance(cats2, list) and cats2:
                                    bd_params2.extend(cats2)
                                else:
                                    bd_params2.append(filter_value)
                            except Exception:
                                bd_params2.append(filter_value)
                        elif filter_type == "product" and filter_value:
                            try:
                                ids2 = _json2.loads(filter_value)
                                if ids2:
                                    bd_params2.extend(ids2)
                            except Exception:
                                pass
                        bd_q = (
                            f"SELECT u.first_name, u.last_name, {metric_expr} "
                            f"FROM sales s LEFT JOIN users u ON s.user_id = u.id "
                            f"{join_clause} WHERE {bd_where} "
                            f"GROUP BY s.user_id ORDER BY 3 DESC LIMIT 5"
                        )
                        conn2 = self.get_connection()
                        bd_rows = conn2.execute(bd_q, bd_params2).fetchall()
                        conn2.close()
                        breakdown = []
                        for bfn, bln, bval in bd_rows:
                            bname = f"{bfn or ''} {bln or ''}".strip() or "—"
                            bval = float(bval or 0)
                            bpct = round(bval / target_val * 100) if target_val else 0
                            breakdown.append({"name": bname, "current": bval, "pct": bpct})
                        if breakdown:
                            entry["seller_breakdown"] = breakdown
                    except Exception:
                        pass
                result.append(entry)
            return result
        except Exception:
            return []

    def get_notification_settings(self, user_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM notification_settings WHERE user_id = ?', (user_id,))
        settings = cursor.fetchone()
        conn.close()

        if settings:
            return {
                'low_stock_alerts': bool(settings[2]),
                'daily_reports': bool(settings[3]),
                'sales_alerts': bool(settings[4]),
                'payment_alerts': bool(settings[5]),
                'admin_notifications': bool(settings[6]),
                'stock_threshold': settings[7],
                'notification_time': settings[8],
                # shift_sale_alerts добавлен миграцией — индекс 11 (после created_at[9], updated_at[10])
                'shift_sale_alerts': bool(settings[11]) if len(settings) > 11 else True,
                # plan_coeff_enabled/cap — индексы 12/13, добавлены миграцией
                'plan_coeff_enabled': bool(settings[12]) if len(settings) > 12 else False,
                'plan_coeff_cap': bool(settings[13]) if len(settings) > 13 else True,
                # shift_reminders — индекс 14, добавлен миграцией (opt-out, дефолт True)
                'shift_reminders': bool(settings[14]) if len(settings) > 14 else True,
                # auto_tasks_low_stock — индекс 15, добавлен миграцией (opt-in, дефолт False)
                'auto_tasks_low_stock': bool(settings[15]) if len(settings) > 15 else False,
            }
        else:
            self.create_default_notification_settings(user_id)
            return {
                'low_stock_alerts': True,
                'daily_reports': False,
                'sales_alerts': True,
                'payment_alerts': True,
                'admin_notifications': True,
                'stock_threshold': 5,
                'notification_time': '09:00',
                'shift_sale_alerts': True,
                'plan_coeff_enabled': False,
                'plan_coeff_cap': True,
                'shift_reminders': True,
                'auto_tasks_low_stock': False,
            }

    def create_default_notification_settings(self, user_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO notification_settings (user_id)
            VALUES (?)
        ''', (user_id,))
        conn.commit()
        conn.close()

    def update_notification_settings(self, user_id, **settings):
        conn = self.get_connection()
        cursor = conn.cursor()
        set_clause = ', '.join([f'{key} = ?' for key in settings.keys()])
        values = list(settings.values()) + [user_id]
        cursor.execute(f'''
            UPDATE notification_settings 
            SET {set_clause}
            WHERE user_id = ?
        ''', values)
        conn.commit()
        conn.close()

    def add_product_history(self, product_id: int, field: str, old_value, new_value,
                            changed_by: int = None, changed_by_name: str = None) -> None:
        """Log a product field change (e.g. price) to product_history."""
        try:
            conn = self.get_connection()
            conn.execute(
                "INSERT INTO product_history (product_id, field, old_value, new_value, changed_by, changed_by_name)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (product_id, field, str(old_value) if old_value is not None else None,
                 str(new_value) if new_value is not None else None, changed_by, changed_by_name)
            )
            conn.commit()
            conn.close()
        except Exception as _exc:
            logger.debug("add_product_history: подавлено исключение: %s", _exc)

    def get_product_history(self, product_id: int, limit: int = 30):
        """Return last N changes for product. Rows: id,product_id,field,old_value,new_value,changed_by,changed_by_name,changed_at."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                "SELECT id, product_id, field, old_value, new_value, changed_by, changed_by_name, changed_at"
                " FROM product_history WHERE product_id = ? ORDER BY changed_at DESC LIMIT ?",
                (product_id, limit)
            ).fetchall()
            conn.close()
            return rows
        except Exception:
            return []

    def add_notification_to_history(self, user_id, notification_type, message):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO notification_history (user_id, notification_type, message)
            VALUES (?, ?, ?)
        ''', (user_id, notification_type, message))
        cursor.execute(
            "DELETE FROM notification_history WHERE user_id = ? "
            "AND created_at < datetime('now', '-90 days')",
            (user_id,)
        )
        conn.commit()
        conn.close()

    def has_seen_hint(self, user_id: int, hint_key: str) -> bool:
        """Проверяет, показывалась ли подсказка пользователю ранее."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT 1 FROM user_hints_seen WHERE user_id = ? AND hint_key = ?',
            (user_id, hint_key)
        )
        result = cursor.fetchone() is not None
        conn.close()
        return result

    def mark_hint_seen(self, user_id: int, hint_key: str) -> None:
        """Помечает подсказку как показанную для данного пользователя."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR IGNORE INTO user_hints_seen (user_id, hint_key) VALUES (?, ?)',
            (user_id, hint_key)
        )
        conn.commit()
        conn.close()

    def log_sale_edit(self, sale_id, changed_by_user_id,
                      old_quantity, new_quantity, old_price, new_price):
        """Записывает строку в журнал изменений продажи."""
        try:
            conn = self.get_connection()
            conn.execute('''
                INSERT INTO sales_audit_log
                (sale_id, changed_by_user_id, old_quantity, new_quantity, old_price, new_price)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (sale_id, changed_by_user_id, old_quantity, new_quantity, old_price, new_price))
            conn.commit()
            conn.close()
        except Exception as _e:
            logger.warning(f"log_sale_edit failed: {_e}")

    def get_sale_audit_log(self, sale_id):
        """Возвращает историю изменений продажи, от новых к старым."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sal.id, sal.changed_by_user_id,
                       u.first_name || ' ' || u.last_name AS editor_name,
                       sal.old_quantity, sal.new_quantity,
                       sal.old_price, sal.new_price, sal.changed_at
                FROM sales_audit_log sal
                LEFT JOIN users u ON sal.changed_by_user_id = u.id
                WHERE sal.sale_id = ?
                ORDER BY sal.changed_at DESC, sal.id DESC
            ''', (sale_id,))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception:
            return []

    def get_notification_history(self, user_id, limit=20):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM notification_history 
            WHERE user_id = ? 
            ORDER BY created_at DESC
            LIMIT ?
        ''', (user_id, limit))
        history = cursor.fetchall()
        conn.close()
        return history

    def mark_notifications_as_read(self, user_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE notification_history 
            SET is_read = TRUE 
            WHERE user_id = ?
        ''', (user_id,))
        conn.commit()
        conn.close()
    
    def get_notification_history_by_period(self, user_id, period_type='week', offset=0):
        """
        Получение истории уведомлений за определенный период
        period_type: 'week', 'month', 'all'
        offset: смещение периода назад (0=текущий, 1=предыдущий, и т.д.)
        """
        from datetime import datetime, timedelta, timezone
        
        conn = self.get_connection()
        cursor = conn.cursor()

        now = datetime.now(timezone.utc)
        
        if period_type == 'week':
            days_delta = 7 * (offset + 1)
            start_date = now - timedelta(days=days_delta)
            end_date = now - timedelta(days=7 * offset) if offset > 0 else now
        elif period_type == 'month':
            days_delta = 30 * (offset + 1)
            start_date = now - timedelta(days=days_delta)
            end_date = now - timedelta(days=30 * offset) if offset > 0 else now
        else:
            cursor.execute('''
                SELECT * FROM notification_history 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            ''', (user_id,))
            history = cursor.fetchall()
            conn.close()
            return history, None
        
        start_str = start_date.strftime('%Y-%m-%d %H:%M:%S')
        end_str = end_date.strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute('''
            SELECT * FROM notification_history 
            WHERE user_id = ? AND created_at >= ? AND created_at < ?
            ORDER BY created_at DESC
        ''', (user_id, start_str, end_str))
        
        history = cursor.fetchall()
        conn.close()
        
        period_info = {
            'start_date': start_date,
            'end_date': end_date,
            'period_type': period_type,
            'offset': offset
        }
        
        return history, period_info

    def get_notification_history_paged(self, user_id: int,
                                        start_date: str = None,
                                        end_date: str = None,
                                        limit: int = 10,
                                        offset: int = 0):
        """Paginated notification history with optional date range.
        start_date / end_date: 'YYYY-MM-DD' strings (inclusive). None = no bound.
        Returns (rows, total_count).
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            where = 'WHERE user_id = ?'
            params: list = [user_id]
            if start_date:
                where += ' AND created_at >= ?'
                params.append(f'{start_date} 00:00:00')
            if end_date:
                where += ' AND created_at <= ?'
                params.append(f'{end_date} 23:59:59')
            cursor.execute(
                f'SELECT COUNT(*) FROM notification_history {where}', params)
            total = cursor.fetchone()[0]
            cursor.execute(
                f'SELECT * FROM notification_history {where} '
                f'ORDER BY created_at DESC LIMIT ? OFFSET ?',
                params + [limit, offset]
            )
            rows = cursor.fetchall()
            conn.close()
            return rows, total
        except Exception as e:
            logger.error(f'get_notification_history_paged: {e}')
            return [], 0

    def delete_old_notifications(self, user_id, period_type='week'):
        """
        Удаление старых уведомлений
        period_type: 'week' (старше недели), 'month' (старше месяца), 'all' (все)
        """
        from datetime import datetime, timedelta, timezone
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        if period_type == 'all':
            cursor.execute('DELETE FROM notification_history WHERE user_id = ?', (user_id,))
        else:
            now = datetime.now(timezone.utc)
            if period_type == 'week':
                cutoff_date = now - timedelta(days=7)
            elif period_type == 'month':
                cutoff_date = now - timedelta(days=30)
            else:
                conn.close()
                return 0
            
            cutoff_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')
            
            cursor.execute('''
                DELETE FROM notification_history 
                WHERE user_id = ? AND created_at < ?
            ''', (user_id, cutoff_str))
        
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        
        return deleted_count

    def get_users_for_notifications(self, notification_type):
        """Получение пользователей для отправки определенного типа уведомлений"""
        conn = self.get_connection()
        cursor = conn.cursor()

        field_map = {
            'low_stock': 'low_stock_alerts',
            'daily_report': 'daily_reports', 
            'sales': 'sales_alerts',
            'payment': 'payment_alerts',
            'admin': 'admin_notifications'
        }

        field = field_map.get(notification_type)
        if not field:
            return []

        cursor.execute(f'''
            SELECT u.id, u.telegram_id, u.first_name, u.shop_name, ns.stock_threshold, ns.notification_time
            FROM users u
            LEFT JOIN notification_settings ns ON u.id = ns.user_id
            WHERE ns.{field} = 1 AND u.telegram_id IS NOT NULL
        ''')

        users = cursor.fetchall()
        conn.close()
        return users

    # Методы для работы с остатками
    def add_inventory(self, shop_name, product_id, quantity, user_id=None, change_type='manual', change_reason=None):
        """Установка абсолютного остатка товара (атомарно — BEGIN IMMEDIATE исключает TOCTOU race)."""
        from datetime import datetime
        conn = None
        try:
            conn = self.get_connection()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT quantity FROM inventory WHERE shop_name=? AND product_id=?",
                (shop_name, product_id)
            ).fetchone()
            _old_qty = row[0] if row else 0
            now = datetime.now().isoformat()
            if row:
                conn.execute(
                    """UPDATE inventory
                       SET quantity=?, updated_by=?, last_updated=?,
                           change_type=?, change_reason=?
                       WHERE shop_name=? AND product_id=?""",
                    (quantity, user_id, now, change_type, change_reason, shop_name, product_id)
                )
            else:
                conn.execute(
                    """INSERT INTO inventory
                           (shop_name, product_id, quantity, updated_by,
                            last_updated, change_type, change_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (shop_name, product_id, quantity, user_id, now, change_type, change_reason)
                )
            try:
                conn.execute(
                    """INSERT INTO inventory_log
                           (shop_name, product_id, old_quantity, new_quantity,
                            delta, change_type, change_reason, changed_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (shop_name, product_id, _old_qty, quantity,
                     quantity - _old_qty, change_type, change_reason, user_id)
                )
            except Exception as _le:
                logger.warning(f"inventory_log insert failed: {_le}")
            conn.commit()
            conn.close()
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception as _exc:
                    logger.debug("add_inventory: подавлено исключение: %s", _exc)
                conn.close()
            logger.error(f"Error in add_inventory: {e}")
            raise

    def get_inventory(self, shop_name, product_id):
        """Получение остатков товара в магазине"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT quantity FROM inventory
            WHERE shop_name = ? AND product_id = ?
        ''', (shop_name, product_id))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else 0

    def update_inventory(self, shop_name, product_id, delta, user_id=None, change_type='manual', change_reason=None):
        """Обновление остатков товара (атомарно — BEGIN IMMEDIATE исключает TOCTOU race)."""
        import time
        from datetime import datetime

        max_retries = 3
        retry_delay = 0.1

        for attempt in range(max_retries):
            conn = None
            try:
                conn = self.get_connection()
                # BEGIN IMMEDIATE — захватываем write-lock до чтения; никакой другой
                # процесс не может изменить строку между SELECT и UPDATE.
                conn.execute("BEGIN IMMEDIATE")

                row = conn.execute(
                    "SELECT quantity FROM inventory WHERE shop_name=? AND product_id=?",
                    (shop_name, product_id)
                ).fetchone()

                current_quantity = row[0] if row else 0
                new_quantity = max(0, current_quantity + delta)
                now = datetime.now().isoformat()

                if row:
                    conn.execute(
                        """UPDATE inventory
                           SET quantity=?, updated_by=?, last_updated=?,
                               change_type=?, change_reason=?
                           WHERE shop_name=? AND product_id=?""",
                        (new_quantity, user_id, now, change_type, change_reason,
                         shop_name, product_id)
                    )
                else:
                    conn.execute(
                        """INSERT INTO inventory
                               (shop_name, product_id, quantity, updated_by,
                                last_updated, change_type, change_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (shop_name, product_id, new_quantity, user_id,
                         now, change_type, change_reason)
                    )

                try:
                    conn.execute(
                        """INSERT INTO inventory_log
                               (shop_name, product_id, old_quantity, new_quantity,
                                delta, change_type, change_reason, changed_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (shop_name, product_id, current_quantity, new_quantity,
                         delta, change_type, change_reason, user_id)
                    )
                except Exception as _le:
                    logger.warning(f"inventory_log insert failed: {_le}")

                conn.commit()
                conn.close()
                return new_quantity

            except sqlite3.OperationalError as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception as _exc:
                        logger.debug("update_inventory: подавлено исключение: %s", _exc)
                    conn.close()
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                logger.error(f"Database error in update_inventory: {e}")
                raise
            except Exception as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception as _exc:
                        logger.debug("update_inventory: подавлено исключение: %s", _exc)
                    conn.close()
                logger.error(f"Unexpected error in update_inventory: {e}")
                raise

        return 0

    def get_all_inventory(self, shop_name=None):
        """Получение всех остатков с информацией о последнем изменении"""
        conn = self.get_connection()
        cursor = conn.cursor()

        if shop_name:
            cursor.execute('''
                SELECT i.id, i.product_id, i.shop_name, i.quantity, i.last_updated, i.updated_by,
                       p.name, p.category, p.price,
                       u.first_name || ' ' || u.last_name as updated_by_name,
                       p.article
                FROM inventory i
                JOIN products p ON i.product_id = p.id
                LEFT JOIN users u ON i.updated_by = u.id
                WHERE i.shop_name = ?
                ORDER BY p.name
            ''', (shop_name,))
        else:
            cursor.execute('''
                SELECT i.id, i.product_id, i.shop_name, i.quantity, i.last_updated, i.updated_by,
                       p.name, p.category, p.price,
                       u.first_name || ' ' || u.last_name as updated_by_name,
                       p.article
                FROM inventory i
                JOIN products p ON i.product_id = p.id
                LEFT JOIN users u ON i.updated_by = u.id
                ORDER BY i.shop_name, p.name
            ''')

        inventory = cursor.fetchall()
        conn.close()
        return inventory

    def get_stock_totals(self) -> dict:
        """Суммарный остаток по каждому товару: {product_id: total_qty}.
        Единственный GROUP BY запрос — не грузит все строки inventory в Python."""
        conn = self.get_connection()
        try:
            rows = conn.execute(
                "SELECT product_id, SUM(quantity) FROM inventory GROUP BY product_id"
            ).fetchall()
            return {r[0]: int(r[1] or 0) for r in rows}
        except Exception:
            return {}
        finally:
            conn.close()

    def get_inventory_turnover(self, shop_name=None, days: int = 30):
        """Оборачиваемость остатков: текущий stock + продажи за последние days дней.
        Columns: product_id[0] name[1] category[2] price[3] shop_name[4]
                 current_stock[5] sold_qty[6] avg_daily[7] days_until_empty[8]
        Сортировка: сначала критические (дней мало), потом нет продаж (NULL).
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        shop_clause = "AND i.shop_name = ?" if shop_name else ""
        # Порядок ?: avg_daily(days), days_until_empty(days), date_filter(days), shop_name(опц.)
        params: list = [days, days, days]
        if shop_name:
            params.append(shop_name)
        cursor.execute(f"""
            SELECT
                p.id,
                p.name,
                p.category,
                p.price,
                i.shop_name,
                i.quantity AS current_stock,
                COALESCE(SUM(s.quantity_sold), 0) AS sold_qty,
                ROUND(COALESCE(SUM(s.quantity_sold), 0) * 1.0 / ?, 2) AS avg_daily,
                CASE
                    WHEN COALESCE(SUM(s.quantity_sold), 0) = 0 THEN NULL
                    ELSE CAST(ROUND(i.quantity * 1.0 * ? / COALESCE(SUM(s.quantity_sold), 1)) AS INTEGER)
                END AS days_until_empty
            FROM inventory i
            JOIN products p ON i.product_id = p.id
            LEFT JOIN sales s
                ON s.product_id = i.product_id
                AND s.shop_name = i.shop_name
                AND date(s.sale_date) >= date('now', '-' || CAST(? AS TEXT) || ' days')
            WHERE i.quantity > 0
            {shop_clause}
            GROUP BY i.product_id, i.shop_name
            ORDER BY
                CASE WHEN COALESCE(SUM(s.quantity_sold), 0) = 0 THEN 1 ELSE 0 END ASC,
                days_until_empty ASC
        """, params)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_dead_stock(self, shop_name=None, days: int = 30):
        """Залежалые товары: остаток > 0 и нет продаж за последние days дней.
        Columns: product_id[0] name[1] category[2] price[3] shop_name[4]
                 current_stock[5] last_updated[6] last_sale_date[7]
        Сортировка: без продаж вообще сначала, затем по давности последней продажи.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        shop_clause = "AND i.shop_name = ?" if shop_name else ""
        params: list = []
        if shop_name:
            params.append(shop_name)
        params.append(days)
        cursor.execute(f"""
            SELECT
                p.id,
                p.name,
                p.category,
                p.price,
                i.shop_name,
                i.quantity AS current_stock,
                i.last_updated,
                (SELECT MAX(s2.sale_date) FROM sales s2
                 WHERE s2.product_id = p.id AND s2.shop_name = i.shop_name) AS last_sale_date
            FROM inventory i
            JOIN products p ON i.product_id = p.id
            WHERE i.quantity > 0
            {shop_clause}
            AND NOT EXISTS (
                SELECT 1 FROM sales s
                WHERE s.product_id = p.id
                AND s.shop_name = i.shop_name
                AND date(s.sale_date) >= date('now', '-' || CAST(? AS TEXT) || ' days')
            )
            ORDER BY last_sale_date ASC NULLS FIRST, p.name
        """, params)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_seller_card_daily(self, user_id: int, start_date: str, end_date: str, shop_name=None):
        """Продажи продавца по дням для графика.
        Columns: day[0] revenue[1] qty[2] count[3]
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        shop_clause = "AND s.shop_name = ?" if shop_name else ""
        params: list = [user_id, start_date, end_date]
        if shop_name:
            params.append(shop_name)
        cursor.execute(f"""
            SELECT date(s.sale_date) AS day,
                   SUM(s.quantity_sold * s.sale_price) AS revenue,
                   SUM(s.quantity_sold) AS qty,
                   COUNT(*) AS cnt
            FROM sales s
            WHERE s.user_id = ? AND date(s.sale_date) BETWEEN ? AND ?
            {shop_clause}
            GROUP BY day
            ORDER BY day
        """, params)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_seller_card_top_products(self, user_id: int, start_date: str, end_date: str, limit: int = 8):
        """Топ товаров продавца за период.
        Columns: product_id[0] name[1] category[2] qty[3] revenue[4]
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.name, p.category,
                   SUM(s.quantity_sold) AS qty,
                   SUM(s.quantity_sold * s.sale_price) AS revenue
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.user_id = ? AND date(s.sale_date) BETWEEN ? AND ?
            GROUP BY s.product_id
            ORDER BY revenue DESC
            LIMIT ?
        """, (user_id, start_date, end_date, limit))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_seller_card_dow(self, user_id: int, start_date: str, end_date: str):
        """Выручка продавца по дням недели (0=Вс..6=Сб, SQLite strftime).
        Columns: dow[0] revenue[1] qty[2] cnt[3]
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT CAST(strftime('%w', sale_date) AS INTEGER) AS dow,
                   SUM(quantity_sold * sale_price) AS revenue,
                   SUM(quantity_sold) AS qty,
                   COUNT(*) AS cnt
            FROM sales
            WHERE user_id = ? AND date(sale_date) BETWEEN ? AND ?
            GROUP BY dow
            ORDER BY revenue DESC
        """, (user_id, start_date, end_date))
        rows = cursor.fetchall()
        conn.close()
        return rows

    # Методы для работы с продажами
    def add_sale(self, product_id, shop_name, quantity_sold, user_id, sale_price=None):
        """Добавление продажи"""
        import time

        max_retries = 3
        retry_delay = 0.1


        for attempt in range(max_retries):
            conn = None
            try:
                conn = self.get_connection()
                cursor = conn.cursor()

                # Если цена не указана, берем из товара
                if sale_price is None:
                    cursor.execute('SELECT price FROM products WHERE id = ?', (product_id,))
                    result = cursor.fetchone()
                    sale_price = result[0] if result else 0

                # Проверяем остатки перед продажей
                cursor.execute('SELECT quantity FROM inventory WHERE shop_name = ? AND product_id = ?', (shop_name, product_id))
                inventory_result = cursor.fetchone()
                current_quantity = inventory_result[0] if inventory_result else 0


                if current_quantity < quantity_sold:
                    conn.close()
                    return None

                # Добавляем продажу
                from datetime import datetime
                _sale_now = datetime.now()
                sale_date = _sale_now.isoformat()
                _sale_year, _sale_month = _sale_now.year, _sale_now.month

                cursor.execute('''
                    INSERT INTO sales (product_id, shop_name, quantity_sold, sale_date, user_id, sale_price)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (product_id, shop_name, quantity_sold, sale_date, user_id, sale_price))

                sale_id = cursor.lastrowid

                # Атомарное обновление остатков — AND quantity >= ? предотвращает TOCTOU race condition
                cursor.execute('''
                    UPDATE inventory 
                    SET quantity = quantity - ?, last_updated = ?
                    WHERE shop_name = ? AND product_id = ? AND quantity >= ?
                ''', (quantity_sold, sale_date, shop_name, product_id, quantity_sold))

                affected_rows = cursor.rowcount

                if affected_rows == 0:
                    conn.rollback()
                    conn.close()
                    return None

                conn.commit()
                conn.close()


                # Рассчитываем и добавляем комиссию продавца
                try:
                    _seller_attrs = self._get_seller_attrs(user_id, shop_name)
                    commission_info = self.get_motivation_for_month(product_id, _sale_year, _sale_month,
                                                                    seller_attrs=_seller_attrs)
                    
                    if commission_info:
                        commission_amount = self.calculate_seller_commission(
                            sale_id, product_id, sale_price, quantity_sold,
                            user_id=user_id, shop_name=shop_name,
                            sale_year=_sale_year, sale_month=_sale_month,
                            seller_attrs=_seller_attrs
                        )
                        if commission_amount > 0:
                            self.add_seller_earning(
                                sale_id, user_id, product_id, commission_amount,
                                commission_info['motivation_type'], commission_info['motivation_value'],
                                commission_info.get('motivation_source', 'global')
                            )
                    else:
                        # Добавляем запись с нулевой комиссией для отслеживания
                        result = self.add_seller_earning(
                            sale_id, user_id, product_id, 0.0,
                            'percentage', 0.0
                        )
                        
                except Exception as commission_error:
                    logger.error(f"Ошибка расчёта комиссии для sale_id={sale_id}: {commission_error}")
                    # Добавляем хотя бы нулевую запись в случае ошибки
                    try:
                        self.add_seller_earning(sale_id, user_id, product_id, 0.0, 'percentage', 0.0)
                    except Exception as _exc:
                        logger.debug("add_sale: подавлено исключение: %s", _exc)

                return sale_id

            except sqlite3.OperationalError as e:
                if conn:
                    conn.close()
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    continue
                else:
                    logger.error(f"Database locked in add_sale: {e}")
                    return None
            except Exception as e:
                if conn:
                    conn.close()
                logger.error(f"Unexpected error in add_sale: {e}")
                return None

        return None

    def get_sales_report(self, start_date=None, end_date=None,
                         shop_name=None, city=None, trade_network=None,
                         shop_names=None, cities=None, trade_networks=None,
                         category=None, user_id=None, product_id=None):
        """Получение детального отчета по продажам.
        Поддерживает одиночные и множественные (list) фильтры зоны."""
        conn = self.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT s.*, p.name as product_name, p.category, u.first_name, u.last_name
            FROM sales s
            JOIN products p ON s.product_id = p.id
            JOIN users u ON s.user_id = u.id
            WHERE 1=1
        '''
        params = []

        if start_date and end_date:
            query += ' AND date(s.sale_date) BETWEEN ? AND ?'
            params.extend([start_date, end_date])
        elif start_date:
            query += ' AND date(s.sale_date) >= ?'
            params.append(start_date)
        elif end_date:
            query += ' AND date(s.sale_date) <= ?'
            params.append(end_date)

        if shop_name:
            query += ' AND s.shop_name = ?'
            params.append(shop_name)
        elif shop_names:
            ph = ','.join('?' * len(shop_names))
            query += f' AND s.shop_name IN ({ph})'
            params.extend(shop_names)
        elif city:
            query += ' AND u.city = ?'
            params.append(city)
        elif cities:
            ph = ','.join('?' * len(cities))
            query += f' AND u.city IN ({ph})'
            params.extend(cities)
        elif trade_network:
            query += ' AND u.trade_network = ?'
            params.append(trade_network)
        elif trade_networks:
            ph = ','.join('?' * len(trade_networks))
            query += f' AND u.trade_network IN ({ph})'
            params.extend(trade_networks)

        if category:
            query += ' AND p.category = ?'
            params.append(category)

        if user_id:
            query += ' AND s.user_id = ?'
            params.append(user_id)

        if product_id:
            query += ' AND s.product_id = ?'
            params.append(product_id)

        query += ' ORDER BY s.sale_date DESC'

        cursor.execute(query, params)
        sales = cursor.fetchall()
        conn.close()
        return sales

    def get_sales_heatmap(self, start_date=None, end_date=None, shop_name=None):
        """Heatmap: list of (weekday 0=Mon..6=Sun, hour 0-23, revenue, count)."""
        conn = self.get_connection()
        try:
            conditions, params = [], []
            if start_date:
                conditions.append("s.sale_date >= ?"); params.append(start_date)
            if end_date:
                conditions.append("s.sale_date <= ?"); params.append(end_date + " 23:59:59")
            if shop_name:
                conditions.append("s.shop_name = ?"); params.append(shop_name)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT
                    CASE strftime('%w', s.sale_date)
                        WHEN '0' THEN 6
                        ELSE CAST(strftime('%w', s.sale_date) AS INTEGER) - 1
                    END AS weekday,
                    CAST(strftime('%H', s.sale_date) AS INTEGER) AS hour,
                    SUM(s.quantity_sold * s.sale_price) AS revenue,
                    COUNT(*) AS cnt
                FROM sales s
                {where}
                GROUP BY weekday, hour
                ORDER BY weekday, hour
            """, params)
            return cursor.fetchall()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("get_sales_heatmap: %s", exc)
            return []
        finally:
            try:
                conn.close()
            except Exception as _exc:
                logger.debug("get_sales_heatmap: подавлено исключение: %s", _exc)

    def get_sales_summary(self, start_date=None, end_date=None,
                          shop_name=None, city=None, trade_network=None,
                          shop_names=None, cities=None, trade_networks=None,
                          user_id=None, category=None):
        """Получение сводного отчета по продажам.

        Поддерживает одиночные (shop_name/city/trade_network) и
        множественные (shop_names/cities/trade_networks — list[str]) фильтры зоны.
        При city/trade_network-фильтрах добавляется JOIN с users.
        user_id — внутренний users.id для фильтрации по конкретному продавцу.
        category — фильтр по категории товара (JOIN products).
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        need_user_join = any([city, trade_network, cities, trade_networks])
        need_product_join = bool(category)

        if need_user_join:
            query = '''
                SELECT
                    COUNT(*) as total_sales,
                    SUM(s.quantity_sold) as total_quantity,
                    SUM(s.quantity_sold * s.sale_price) as total_revenue,
                    AVG(s.quantity_sold * s.sale_price) as avg_sale
                FROM sales s
                JOIN users u ON s.user_id = u.id
            '''
            if need_product_join:
                query += ' JOIN products p ON s.product_id = p.id'
            query += ' WHERE 1=1'
        else:
            query = '''
                SELECT
                    COUNT(*) as total_sales,
                    SUM(s.quantity_sold) as total_quantity,
                    SUM(s.quantity_sold * s.sale_price) as total_revenue,
                    AVG(s.quantity_sold * s.sale_price) as avg_sale
                FROM sales s
            '''
            if need_product_join:
                query += ' JOIN products p ON s.product_id = p.id'
            query += ' WHERE 1=1'
        params = []

        if start_date:
            query += ' AND date(s.sale_date) >= ?'
            params.append(start_date)
        if end_date:
            query += ' AND date(s.sale_date) <= ?'
            params.append(end_date)

        if shop_name:
            query += ' AND s.shop_name = ?'
            params.append(shop_name)
        elif shop_names:
            ph = ','.join('?' * len(shop_names))
            query += f' AND s.shop_name IN ({ph})'
            params.extend(shop_names)
        elif city:
            query += ' AND u.city = ?'
            params.append(city)
        elif cities:
            ph = ','.join('?' * len(cities))
            query += f' AND u.city IN ({ph})'
            params.extend(cities)
        elif trade_network:
            query += ' AND u.trade_network = ?'
            params.append(trade_network)
        elif trade_networks:
            ph = ','.join('?' * len(trade_networks))
            query += f' AND u.trade_network IN ({ph})'
            params.extend(trade_networks)

        if user_id is not None:
            query += ' AND s.user_id = ?'
            params.append(user_id)

        if category:
            query += ' AND p.category = ?'
            params.append(category)

        cursor.execute(query, params)
        summary = cursor.fetchone()
        conn.close()
        return summary

    def get_users_sales_summary_bulk(self, start_date: str, end_date: str) -> dict:
        """Один GROUP BY запрос вместо N отдельных get_sales_summary для staff-страницы.
        Возвращает {user_id: (count, qty, revenue, avg)}.
        """
        try:
            conn = self.get_connection()
            try:
                rows = conn.execute(
                    '''SELECT user_id,
                              COUNT(*),
                              SUM(quantity_sold),
                              SUM(quantity_sold * sale_price),
                              AVG(quantity_sold * sale_price)
                       FROM sales
                       WHERE date(sale_date) >= ? AND date(sale_date) <= ?
                       GROUP BY user_id''',
                    (start_date, end_date)
                ).fetchall() or []
            finally:
                conn.close()
            return {row[0]: (row[1] or 0, row[2] or 0, row[3] or 0, row[4] or 0)
                    for row in rows}
        except Exception as e:
            logger.error("get_users_sales_summary_bulk: %s", e)
            return {}

    def get_daily_chart_data(self, start_date: str, end_date: str) -> dict:
        """Один GROUP BY запрос вместо N отдельных get_sales_summary для дашборда.
        Возвращает {date_iso: float} — выручка за каждый день диапазона.
        Дни без продаж отсутствуют в словаре (caller заполняет нулями).
        """
        try:
            conn = self.get_connection()
            try:
                rows = conn.execute(
                    '''SELECT date(sale_date), SUM(quantity_sold * sale_price)
                       FROM sales
                       WHERE date(sale_date) >= ? AND date(sale_date) <= ?
                       GROUP BY date(sale_date)''',
                    (start_date, end_date)
                ).fetchall() or []
            finally:
                conn.close()
            return {row[0]: float(row[1] or 0) for row in rows}
        except Exception as e:
            logger.error("get_daily_chart_data: %s", e)
            return {}

    def get_salary_xlsx_bulk(
        self,
        user_ids: list,
        year: int,
        month: int,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Bulk-данные для xlsx-экспорта зарплат: 3 SQL-запроса вместо 4×N.

        Возвращает:
          {
            'worked':   {user_id: int},          # число смен
            'adj_sum':  {user_id: float},         # сумма корректировок
            'earnings': {user_id: float},         # мотивация (комиссия SE)
            'earnings_detail': {user_id: [rows]}, # строки для расшифровки
          }
        Для пользователей без данных ключи в подсловарях отсутствуют
        (caller должен использовать .get(uid, default)).
        """
        if not user_ids:
            return {'worked': {}, 'adj_sum': {}, 'earnings': {}, 'earnings_detail': {}}
        placeholders = ','.join('?' * len(user_ids))
        try:
            conn = self.get_connection()
            try:
                month_start = f"{year}-{month:02d}-01"
                month_end = f"{year}-{month:02d}-31"

                # 1. Смены (work_schedule)
                worked: dict = {}
                for row in conn.execute(
                    f'SELECT user_id, COUNT(*) FROM work_schedule '
                    f'WHERE user_id IN ({placeholders}) '
                    f'AND work_date >= ? AND work_date <= ? '
                    f'GROUP BY user_id',
                    (*user_ids, month_start, month_end),
                ).fetchall():
                    worked[row[0]] = int(row[1])

                # 2. Корректировки (salary_adjustments)
                adj_sum: dict = {}
                for row in conn.execute(
                    f'SELECT user_id, COALESCE(SUM(amount), 0) FROM salary_adjustments '
                    f'WHERE user_id IN ({placeholders}) AND year = ? AND month = ? '
                    f'GROUP BY user_id',
                    (*user_ids, year, month),
                ).fetchall():
                    adj_sum[row[0]] = float(row[1])

                # 3. Заработок (seller_earnings) — агрегат и детали одним запросом
                earnings: dict = {}
                earnings_detail: dict = {}
                se_rows = conn.execute(
                    f'''SELECT se.user_id,
                               se.commission_amount,
                               COALESCE(se.motivation_type, 'percentage') AS motivation_type,
                               COALESCE(se.motivation_value, 0)           AS motivation_value,
                               p.name                                      AS product_name,
                               s.quantity_sold,
                               s.sale_price,
                               s.sale_date,
                               s.shop_name,
                               COALESCE(se.motivation_source, 'global')    AS motivation_source
                        FROM seller_earnings se
                        JOIN sales s ON se.sale_id = s.id
                        JOIN products p ON se.product_id = p.id
                        WHERE se.user_id IN ({placeholders})
                          AND s.sale_date >= ? AND s.sale_date <= ?
                        ORDER BY se.user_id, s.sale_date DESC''',
                    (*user_ids, start_date, end_date),
                ).fetchall()
                for se_row in se_rows:
                    uid = se_row[0]
                    earnings[uid] = round(earnings.get(uid, 0.0) + float(se_row[1] or 0), 2)
                    earnings_detail.setdefault(uid, []).append(se_row[1:])

            finally:
                conn.close()
            return {
                'worked': worked,
                'adj_sum': adj_sum,
                'earnings': earnings,
                'earnings_detail': earnings_detail,
            }
        except Exception as e:
            logger.error("get_salary_xlsx_bulk: %s", e)
            return {'worked': {}, 'adj_sum': {}, 'earnings': {}, 'earnings_detail': {}}

    # Дополнительные методы для полного функционала
    def delete_user(self, telegram_id):
        """Полное удаление пользователя из орг-базы.

        Продажи (sales) НЕ удаляются — они историческая запись магазина.
        Удаляются только личные настройки и вспомогательные данные:
        seller_earnings, salary_settings, work_schedule, sales_plans (по user_id),
        motivation_extra_conditions (по user_id), subscriptions, payment_requests,
        notification_settings, notification_history.
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM users WHERE telegram_id = ?', (telegram_id,))
            if cursor.fetchone()[0] == 0:
                conn.close()
                return False

            uid_sub = 'SELECT id FROM users WHERE telegram_id = ?'

            # seller_earnings и work_schedule могут отсутствовать в старых БД — игнорируем
            for tbl in ('seller_earnings', 'work_schedule'):
                try:
                    cursor.execute(f'DELETE FROM {tbl} WHERE user_id IN ({uid_sub})', (telegram_id,))
                except Exception as _exc:
                    logger.debug("delete_user: подавлено исключение: %s", _exc)

            # salary_settings
            try:
                cursor.execute(f'DELETE FROM salary_settings WHERE user_id IN ({uid_sub})', (telegram_id,))
            except Exception as _exc:
                logger.debug("delete_user: подавлено исключение: %s", _exc)

            # sales_plans — только индивидуальные планы продавца, общие (shop/org) оставляем
            try:
                cursor.execute(
                    f'DELETE FROM sales_plans WHERE user_id IN ({uid_sub}) AND target_type = ?',
                    (telegram_id, 'seller')
                )
            except Exception as _exc:
                logger.debug("delete_user: подавлено исключение: %s", _exc)

            # motivation_extra_conditions по user_id
            try:
                cursor.execute(
                    f'DELETE FROM motivation_extra_conditions WHERE user_id IN ({uid_sub})',
                    (telegram_id,)
                )
            except Exception as _exc:
                logger.debug("delete_user: подавлено исключение: %s", _exc)

            # Прочие личные данные
            cursor.execute(f'DELETE FROM subscriptions WHERE user_id IN ({uid_sub})', (telegram_id,))
            cursor.execute(f'DELETE FROM payment_requests WHERE user_id IN ({uid_sub})', (telegram_id,))
            cursor.execute(f'DELETE FROM notification_settings WHERE user_id IN ({uid_sub})', (telegram_id,))
            cursor.execute(f'DELETE FROM notification_history WHERE user_id IN ({uid_sub})', (telegram_id,))

            # Запись пользователя — удаляем последней
            # sales остаются: shop_name и user_id в них — историческая запись для отчётов
            cursor.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))

            rows_affected = cursor.rowcount
            conn.commit()
            conn.close()

            return rows_affected > 0
        except Exception as e:
            if 'conn' in locals():
                conn.close()
            logger.error(f"Ошибка при удалении пользователя: {e}")
            return False

    def update_user(self, telegram_id, first_name=None, last_name=None, middle_name=None,
                   phone=None, email=None, trade_network=None, shop_name=None, city=None,
                   username=_UNSET):
        """Обновление данных пользователя.

        username=_UNSET (default) — поле не трогается.
        username=None            — явно записывает NULL (пользователь удалил @username).
        username="somestr"       — обновляет на новое значение.
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        updates = []
        params = []

        if first_name is not None:
            updates.append('first_name = ?')
            params.append(first_name)
        if last_name is not None:
            updates.append('last_name = ?')
            params.append(last_name)
        if middle_name is not None:
            updates.append('middle_name = ?')
            params.append(middle_name)
        if phone is not None:
            updates.append('phone = ?')
            params.append(phone)
        if email is not None:
            updates.append('email = ?')
            params.append(email)
        if trade_network is not None:
            updates.append('trade_network = ?')
            params.append(trade_network)
        if shop_name is not None:
            updates.append('shop_name = ?')
            params.append(shop_name)
        if city is not None:
            updates.append('city = ?')
            params.append(city)
        if username is not _UNSET:
            updates.append('username = ?')
            params.append(username)

        if updates:
            params.append(telegram_id)
            cursor.execute(f'''
                UPDATE users SET {', '.join(updates)}
                WHERE telegram_id = ?
            ''', params)
            rows_affected = cursor.rowcount
            conn.commit()
            conn.close()
            return rows_affected > 0

        conn.close()
        return False

    def get_users_by_shop(self, shop_name):
        """Получение пользователей конкретного магазина"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE shop_name = ?', (shop_name,))
        users = cursor.fetchall()
        conn.close()
        return users

    def get_shop_coworkers_on_shift(self, shop_name, today_str, exclude_user_id):
        """Возвращает коллег по магазину, у которых стоит рабочая смена сегодня
        и включены уведомления о продажах коллег (shift_sale_alerts).

        Args:
            shop_name:        название магазина продажи
            today_str:        дата в формате 'YYYY-MM-DD'
            exclude_user_id:  internal users.id продавца, который совершил продажу

        Returns:
            list of (user_internal_id, telegram_id, first_name)
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.id, u.telegram_id, u.first_name
                FROM users u
                JOIN work_schedule ws ON ws.user_id = u.id AND ws.work_date = ?
                LEFT JOIN notification_settings ns ON ns.user_id = u.id
                WHERE u.shop_name = ?
                  AND u.id != ?
                  AND COALESCE(ns.shift_sale_alerts, 1) = 1
            ''', (today_str, shop_name, exclude_user_id))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Ошибка get_shop_coworkers_on_shift: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_users_by_city(self, city):
        """Получение пользователей конкретного города"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE city = ?', (city,))
        users = cursor.fetchall()
        conn.close()
        return users

    def get_user_sales(self, user_id, limit=10):
        """Получить последние продажи пользователя по всем магазинам"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.*, p.name as product_name, p.category
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.user_id = ?
            ORDER BY s.sale_date DESC
            LIMIT ?
        ''', (user_id, limit))
        sales = cursor.fetchall()
        conn.close()
        return sales

    def get_user_sales_by_date(self, user_id, start_date, end_date):
        """Получить продажи пользователя за период по всем магазинам"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.*, p.name as product_name, p.category
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.user_id = ? AND date(s.sale_date) BETWEEN ? AND ?
            ORDER BY s.sale_date DESC
        ''', (user_id, start_date, end_date))
        sales = cursor.fetchall()
        conn.close()
        return sales

    def get_sale_by_id(self, sale_id):
        """Получить продажу по ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, s.product_id, s.shop_name, s.quantity_sold, s.sale_price, 
                   s.user_id, s.sale_date, p.name as product_name, u.first_name, u.last_name
            FROM sales s
            JOIN products p ON s.product_id = p.id
            JOIN users u ON s.user_id = u.id
            WHERE s.id = ?
        ''', (sale_id,))
        sale = cursor.fetchone()
        conn.close()
        return sale

    def update_sale(self, sale_id, quantity_sold, sale_price=None, changed_by=None):
        """Обновить количество и цену в продаже"""
        import time

        max_retries = 3
        retry_delay = 0.1
        conn = None

        for attempt in range(max_retries):
            try:
                conn = self.get_connection()
                cursor = conn.cursor()

                # Получаем текущие данные продажи
                cursor.execute('SELECT * FROM sales WHERE id = ?', (sale_id,))
                current_sale = cursor.fetchone()

                if not current_sale:
                    conn.close()
                    return False

                old_quantity = current_sale[3]  # quantity_sold
                product_id = current_sale[1]
                shop_name = current_sale[2]
                old_price = current_sale[4]
                sale_user_id = current_sale[5]
                _sale_date_str = current_sale[6] if len(current_sale) > 6 else None

                # Обновляем продажу
                if sale_price is not None:
                    cursor.execute('''
                        UPDATE sales SET quantity_sold = ?, sale_price = ?
                        WHERE id = ?
                    ''', (quantity_sold, sale_price, sale_id))
                else:
                    cursor.execute('''
                        UPDATE sales SET quantity_sold = ?
                        WHERE id = ?
                    ''', (quantity_sold, sale_id))

                # Корректируем остатки в той же транзакции
                quantity_diff = old_quantity - quantity_sold
                if quantity_diff != 0:
                    cursor.execute('''
                        UPDATE inventory SET quantity = quantity + ?
                        WHERE shop_name = ? AND product_id = ?
                    ''', (quantity_diff, shop_name, product_id))

                # Аудит-лог (не критично — ошибка не останавливает коммит)
                try:
                    cursor.execute('''
                        INSERT INTO sales_audit_log
                        (sale_id, changed_by_user_id, old_quantity, new_quantity, old_price, new_price)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (sale_id, changed_by, old_quantity, quantity_sold,
                          old_price,
                          sale_price if sale_price is not None else old_price))
                except Exception as _exc:
                    logger.debug("update_sale: подавлено исключение: %s", _exc)

                # ВАЖНО: коммит основной транзакции ДО вызова любых других методов DB.
                # Внутренние вызовы (get_motivation_for_month, calculate_seller_commission)
                # используют тот же пул соединений и вызывают conn.close() → rollback(),
                # что откатило бы незакомиченный UPDATE.
                conn.commit()

                # Пересчитываем заработок продавца (отдельная транзакция после основного коммита)
                final_price = sale_price if sale_price is not None else old_price
                try:
                    from datetime import datetime as _dt2
                    _sdt = _dt2.fromisoformat(_sale_date_str) if _sale_date_str else _dt2.now()
                except Exception:
                    from datetime import datetime as _dt2
                    _sdt = _dt2.now()
                _upd_year, _upd_month = _sdt.year, _sdt.month
                try:
                    _seller_attrs = self._get_seller_attrs(sale_user_id, shop_name)
                    commission_info = self.get_motivation_for_month(product_id, _upd_year, _upd_month,
                                                                    seller_attrs=_seller_attrs)
                    if commission_info:
                        new_commission = self.calculate_seller_commission(
                            sale_id, product_id, final_price, quantity_sold,
                            user_id=sale_user_id, shop_name=shop_name,
                            sale_year=_upd_year, sale_month=_upd_month,
                            seller_attrs=_seller_attrs
                        )
                        conn2 = self.get_connection()
                        conn2.execute('''
                            INSERT INTO seller_earnings
                                (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value, motivation_source)
                            VALUES (?, (SELECT user_id FROM sales WHERE id = ?), ?, ?, ?, ?, ?)
                            ON CONFLICT(sale_id) DO UPDATE SET
                                commission_amount = excluded.commission_amount,
                                motivation_type   = excluded.motivation_type,
                                motivation_value  = excluded.motivation_value,
                                motivation_source = excluded.motivation_source
                        ''', (sale_id, sale_id, product_id, new_commission,
                              commission_info['motivation_type'], commission_info['motivation_value'],
                              commission_info.get('motivation_source', 'global')))
                        conn2.commit()
                        conn2.close()
                except Exception as _exc:
                    logger.debug("update_sale: подавлено исключение: %s", _exc)

                return True

            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                else:
                    logger.error(f"Database error in update_sale: {e}")
                    if conn:
                        conn.close()
                    return False
            except Exception as e:
                logger.error(f"Unexpected error in update_sale: {e}")
                if conn:
                    conn.close()
                return False

        return False

    def update_sale_full(self, sale_id, quantity_sold, sale_price, shop_name, changed_by=None, sale_date=None):
        """Обновить продажу: кол-во, цена и магазин. Пересчитывает остатки при смене магазина или кол-ва."""
        import time
        max_retries = 3
        retry_delay = 0.1
        conn = None

        for attempt in range(max_retries):
            try:
                conn = self.get_connection()
                cursor = conn.cursor()

                cursor.execute('SELECT * FROM sales WHERE id = ?', (sale_id,))
                current_sale = cursor.fetchone()
                if not current_sale:
                    conn.close()
                    return False

                old_quantity = current_sale[3]
                old_price = current_sale[4]
                old_shop = current_sale[2]
                product_id = current_sale[1]
                sale_user_id = current_sale[5]
                _sale_date_str = current_sale[6] if len(current_sale) > 6 else None

                shop_changed = (shop_name != old_shop)
                qty_changed = (quantity_sold != old_quantity)

                if shop_changed:
                    cursor.execute('''
                        UPDATE inventory SET quantity = quantity + ?
                        WHERE shop_name = ? AND product_id = ?
                    ''', (old_quantity, old_shop, product_id))
                    cursor.execute('''
                        UPDATE inventory SET quantity = quantity - ?
                        WHERE shop_name = ? AND product_id = ?
                    ''', (quantity_sold, shop_name, product_id))
                elif qty_changed:
                    quantity_diff = old_quantity - quantity_sold
                    cursor.execute('''
                        UPDATE inventory SET quantity = quantity + ?
                        WHERE shop_name = ? AND product_id = ?
                    ''', (quantity_diff, old_shop, product_id))

                if sale_date:
                    cursor.execute('''
                        UPDATE sales SET quantity_sold = ?, sale_price = ?, shop_name = ?, sale_date = ?
                        WHERE id = ?
                    ''', (quantity_sold, sale_price, shop_name, sale_date, sale_id))
                else:
                    cursor.execute('''
                        UPDATE sales SET quantity_sold = ?, sale_price = ?, shop_name = ?
                        WHERE id = ?
                    ''', (quantity_sold, sale_price, shop_name, sale_id))

                try:
                    cursor.execute('''
                        INSERT INTO sales_audit_log
                        (sale_id, changed_by_user_id, old_quantity, new_quantity, old_price, new_price)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (sale_id, changed_by, old_quantity, quantity_sold, old_price, sale_price))
                except Exception as _exc:
                    logger.debug("update_sale_full: подавлено исключение: %s", _exc)

                conn.commit()

                try:
                    from datetime import datetime as _dt2
                    _effective_date = sale_date or _sale_date_str
                    _sdt = _dt2.fromisoformat(_effective_date) if _effective_date else _dt2.now()
                except Exception:
                    from datetime import datetime as _dt2
                    _sdt = _dt2.now()
                _upd_year, _upd_month = _sdt.year, _sdt.month
                try:
                    _seller_attrs = self._get_seller_attrs(sale_user_id, shop_name)
                    commission_info = self.get_motivation_for_month(product_id, _upd_year, _upd_month,
                                                                    seller_attrs=_seller_attrs)
                    if commission_info:
                        new_commission = self.calculate_seller_commission(
                            sale_id, product_id, sale_price, quantity_sold,
                            user_id=sale_user_id, shop_name=shop_name,
                            sale_year=_upd_year, sale_month=_upd_month,
                            seller_attrs=_seller_attrs
                        )
                        conn2 = self.get_connection()
                        conn2.execute('''
                            INSERT INTO seller_earnings
                                (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value, motivation_source)
                            VALUES (?, (SELECT user_id FROM sales WHERE id = ?), ?, ?, ?, ?, ?)
                            ON CONFLICT(sale_id) DO UPDATE SET
                                commission_amount = excluded.commission_amount,
                                motivation_type   = excluded.motivation_type,
                                motivation_value  = excluded.motivation_value,
                                motivation_source = excluded.motivation_source
                        ''', (sale_id, sale_id, product_id, new_commission,
                              commission_info['motivation_type'], commission_info['motivation_value'],
                              commission_info.get('motivation_source', 'global')))
                        conn2.commit()
                        conn2.close()
                except Exception as _exc:
                    logger.debug("update_sale_full: подавлено исключение: %s", _exc)

                return True

            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                else:
                    logger.error(f"Database error in update_sale_full: {e}")
                    if conn:
                        conn.close()
                    return False
            except Exception as e:
                logger.error(f"Unexpected error in update_sale_full: {e}")
                if conn:
                    conn.close()
                return False

        return False

    def delete_sale(self, sale_id):
        """Удалить продажу и восстановить остатки"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Получаем данные продажи
            cursor.execute('SELECT * FROM sales WHERE id = ?', (sale_id,))
            sale = cursor.fetchone()

            if sale:
                product_id = sale[1]
                shop_name = sale[2]
                quantity_sold = sale[3]

                # Восстанавливаем остатки через тот же cursor (без второго соединения)
                cursor.execute('''
                    UPDATE inventory SET quantity = quantity + ?, last_updated = ?
                    WHERE shop_name = ? AND product_id = ?
                ''', (quantity_sold, datetime.now().isoformat(), shop_name, product_id))

                # Удаляем заработок продавца, связанный с продажей
                cursor.execute('DELETE FROM seller_earnings WHERE sale_id = ?', (sale_id,))

                # Удаляем продажу
                cursor.execute('DELETE FROM sales WHERE id = ?', (sale_id,))
                conn.commit()
                conn.close()
                return True
            else:
                conn.close()
                return False
        except Exception as e:
            logger.error(f"Ошибка при удалении продажи: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def update_sale_date(self, sale_id: int, new_date: str, changed_by: int = None) -> bool:
        """Изменить дату продажи. new_date — строка 'YYYY-MM-DD'."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT sale_date FROM sales WHERE id = ?', (sale_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return False
            old_date = row[0]
            cursor.execute('UPDATE sales SET sale_date = ? WHERE id = ?', (new_date, sale_id))
            try:
                cursor.execute('''
                    INSERT INTO sales_audit_log
                    (sale_id, changed_by_user_id, old_quantity, new_quantity, old_price, new_price)
                    SELECT ?, ?, quantity_sold, quantity_sold, sale_price, sale_price
                    FROM sales WHERE id = ?
                ''', (sale_id, changed_by, sale_id))
            except Exception as _exc:
                logger.debug("update_sale_date: подавлено исключение: %s", _exc)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"update_sale_date error: {e}")
            return False

    def get_sales_ranking(self, start_date=None, end_date=None,
                          shop_name=None, city=None, trade_network=None,
                          shop_names=None, cities=None, trade_networks=None):
        """Получить рейтинг продавцов.
        Поддерживает одиночные и множественные (list) фильтры зоны."""
        conn = self.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT u.first_name, u.last_name, u.shop_name,
                   SUM(s.quantity_sold) as total_sold,
                   SUM(s.quantity_sold * s.sale_price) as total_revenue,
                   COUNT(s.id) as total_sales,
                   SUM(COALESCE(se.commission_amount, 0)) as total_earnings,
                   u.id as user_db_id,
                   u.username
            FROM sales s
            JOIN users u ON s.user_id = u.id
            LEFT JOIN seller_earnings se ON s.id = se.sale_id
            WHERE 1=1
        '''
        params = []

        if start_date:
            query += ' AND date(s.sale_date) >= ?'
            params.append(start_date)
        if end_date:
            query += ' AND date(s.sale_date) <= ?'
            params.append(end_date)

        if shop_name:
            query += ' AND s.shop_name = ?'
            params.append(shop_name)
        elif shop_names:
            ph = ','.join('?' * len(shop_names))
            query += f' AND s.shop_name IN ({ph})'
            params.extend(shop_names)
        elif city:
            query += ' AND u.city = ?'
            params.append(city)
        elif cities:
            ph = ','.join('?' * len(cities))
            query += f' AND u.city IN ({ph})'
            params.extend(cities)
        elif trade_network:
            query += ' AND u.trade_network = ?'
            params.append(trade_network)
        elif trade_networks:
            ph = ','.join('?' * len(trade_networks))
            query += f' AND u.trade_network IN ({ph})'
            params.extend(trade_networks)

        query += '''
            GROUP BY u.id, u.first_name, u.last_name, u.shop_name
            ORDER BY total_revenue DESC
        '''

        cursor.execute(query, params)
        ranking = cursor.fetchall()
        conn.close()
        return ranking

    def get_shop_ranking(self, start_date=None, end_date=None):
        """Получить рейтинг магазинов по продажам и заработку"""
        conn = self.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT s.shop_name,
                   SUM(s.quantity_sold) as total_sold,
                   SUM(s.quantity_sold * s.sale_price) as total_revenue,
                   COUNT(DISTINCT s.user_id) as active_sellers,
                   COUNT(s.id) as total_sales,
                   SUM(COALESCE(se.commission_amount, 0)) as total_earnings
            FROM sales s
            LEFT JOIN seller_earnings se ON s.id = se.sale_id
            WHERE 1=1
        '''
        params = []

        if start_date:
            query += ' AND date(s.sale_date) >= ?'
            params.append(start_date)

        if end_date:
            query += ' AND date(s.sale_date) <= ?'
            params.append(end_date)

        query += '''
            GROUP BY s.shop_name
            ORDER BY total_revenue DESC
        '''

        cursor.execute(query, params)
        ranking = cursor.fetchall()
        conn.close()
        return ranking

    def get_city_ranking(self, start_date=None, end_date=None):
        """Получить рейтинг городов по продажам и заработку"""
        conn = self.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT u.city,
                   SUM(s.quantity_sold) as total_sold,
                   SUM(s.quantity_sold * s.sale_price) as total_revenue,
                   COUNT(DISTINCT s.user_id) as active_sellers,
                   COUNT(s.id) as total_sales,
                   SUM(COALESCE(se.commission_amount, 0)) as total_earnings
            FROM sales s
            JOIN users u ON s.user_id = u.id
            LEFT JOIN seller_earnings se ON s.id = se.sale_id
            WHERE u.city IS NOT NULL
        '''
        params = []

        if start_date:
            query += ' AND date(s.sale_date) >= ?'
            params.append(start_date)

        if end_date:
            query += ' AND date(s.sale_date) <= ?'
            params.append(end_date)

        query += '''
            GROUP BY u.city
            ORDER BY total_revenue DESC
        '''

        cursor.execute(query, params)
        ranking = cursor.fetchall()
        conn.close()
        return ranking

    def reject_payment_request(self, request_id, admin_id):
        """Отклонение заявки на оплату"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE payment_requests 
                SET status = 'rejected', processed_at = CURRENT_TIMESTAMP, processed_by = ?
                WHERE id = ?
            ''', (admin_id, request_id))
            conn.commit()
            conn.close()
            return True

        except sqlite3.OperationalError as e:
            logger.error(f"Ошибка базы данных в reject_payment_request: {e}")
            return False
        except Exception as e:
            logger.error(f"Общая ошибка в reject_payment_request: {e}")
            return False

    def get_stale_pending_payments(self, hours: int = 72):
        """Возвращает pending-заявки старше N часов с telegram_id пользователя."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT pr.id, pr.user_id, pr.plan_type, pr.amount,
                       u.telegram_id, u.first_name, u.last_name
                FROM payment_requests pr
                JOIN users u ON pr.user_id = u.id
                WHERE pr.status = 'pending'
                  AND datetime(pr.created_at) <= datetime('now', ?)
            ''', (f'-{hours} hours',))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"get_stale_pending_payments: {e}")
            return []

    def get_low_stock_items_for_user(self, user_id, shop_name=None, threshold=None):
        """Получение товаров с низкими остатками для конкретного пользователя"""
        conn = self.get_connection()
        cursor = conn.cursor()

        if threshold is None:
            # Получаем порог из настроек пользователя
            cursor.execute('SELECT stock_threshold FROM notification_settings WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            threshold = int(result[0]) if result else 5  # Преобразуем в int
        else:
            threshold = int(threshold)  # Преобразуем в int

        if shop_name:
            cursor.execute('''
                SELECT p.name, i.quantity, i.shop_name
                FROM inventory i
                JOIN products p ON i.product_id = p.id
                WHERE i.shop_name = ? AND i.quantity <= ?
                ORDER BY i.quantity ASC
            ''', (shop_name, threshold))
        else:
            # Получаем магазин пользователя
            cursor.execute('SELECT shop_name FROM users WHERE id = ?', (user_id,))
            user_result = cursor.fetchone()
            if user_result:
                user_shop = user_result[0]
                cursor.execute('''
                    SELECT p.name, i.quantity, i.shop_name
                    FROM inventory i
                    JOIN products p ON i.product_id = p.id
                    WHERE i.shop_name = ? AND i.quantity <= ?
                    ORDER BY i.quantity ASC
                ''', (user_shop, threshold))
            else:
                conn.close()
                return []

        items = cursor.fetchall()
        conn.close()
        return items

    # Методы для работы с подписками и платежами (расширенные)
    def get_subscription_plans(self):
        """Получение всех тарифных планов"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subscription_plans WHERE is_active = TRUE ORDER BY price')
        plans = cursor.fetchall()
        conn.close()
        return plans

    def get_all_subscription_plans(self):
        """Получение всех тарифных планов (включая неактивные)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subscription_plans ORDER BY price')
        plans = cursor.fetchall()
        conn.close()
        return plans

    def get_all_active_subscriptions(self):
        """Получение всех активных подписок с данными пользователей"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.*, u.first_name, u.last_name, u.email, u.shop_name,
                   u.telegram_id, u.phone, u.city
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.end_date > CURRENT_TIMESTAMP
            ORDER BY s.end_date DESC
        ''')
        subscriptions = cursor.fetchall()
        conn.close()
        return subscriptions

    def get_all_promocodes(self, include_inactive: bool = True):
        """Получение всех промокодов."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if include_inactive:
            cursor.execute(
                'SELECT id, code, discount_percent, discount_type, usage_count, max_usage, '
                'is_active, expires_at, allowed_plans, last_used_at, created_at '
                'FROM promocodes ORDER BY is_active DESC, created_at DESC'
            )
        else:
            cursor.execute(
                'SELECT id, code, discount_percent, discount_type, usage_count, max_usage, '
                'is_active, expires_at, allowed_plans, last_used_at, created_at '
                'FROM promocodes WHERE is_active = 1 ORDER BY created_at DESC'
            )
        promocodes = cursor.fetchall()
        conn.close()
        return promocodes

    def create_promocode(self, code: str, discount_percent: int, max_usage: int,
                         discount_type: str = 'percent',
                         expires_at: str = None,
                         allowed_plans: str = None):
        """Создание промокода с поддержкой типа скидки, срока действия и ограничений по тарифу."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO promocodes (code, discount_percent, discount_type, max_usage, expires_at, allowed_plans)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (code, discount_percent, discount_type, max_usage, expires_at, allowed_plans))
        promocode_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return promocode_id

    def create_promocodes_batch(self, prefix: str, discount_percent: int, count: int,
                                discount_type: str = 'percent',
                                expires_at: str = None,
                                allowed_plans: str = None) -> list:
        """Пакетное создание уникальных одноразовых промокодов с общим префиксом.
        Возвращает список созданных кодов."""
        import secrets
        import string
        conn = self.get_connection()
        cursor = conn.cursor()
        created = []
        attempts = 0
        max_attempts = count * 10
        while len(created) < count and attempts < max_attempts:
            attempts += 1
            suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            code = f"{prefix}{suffix}" if prefix else suffix
            try:
                cursor.execute('''
                    INSERT INTO promocodes (code, discount_percent, discount_type, max_usage, expires_at, allowed_plans)
                    VALUES (?, ?, ?, 1, ?, ?)
                ''', (code, discount_percent, discount_type, expires_at, allowed_plans))
                created.append(code)
            except Exception:
                continue
        conn.commit()
        conn.close()
        return created

    def delete_promocode(self, promocode_id):
        """Удаление промокода и его лога использований."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM promocode_usage_log WHERE promocode_id = ?', (promocode_id,))
        cursor.execute('DELETE FROM promocodes WHERE id = ?', (promocode_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def update_promocode(self, promocode_id, **kwargs):
        """Обновление промокода."""
        conn = self.get_connection()
        cursor = conn.cursor()

        valid_fields = ['code', 'discount_percent', 'discount_type', 'max_usage',
                        'is_active', 'expires_at', 'allowed_plans']
        updates = []
        values = []

        for field, value in kwargs.items():
            if field in valid_fields:
                updates.append(f"{field} = ?")
                values.append(value)

        if updates:
            values.append(promocode_id)
            query = f"UPDATE promocodes SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, values)
            updated = cursor.rowcount > 0
        else:
            updated = False

        conn.commit()
        conn.close()
        return updated

    def get_promocode_by_id(self, promocode_id):
        """Получение промокода по ID."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, code, discount_percent, discount_type, usage_count, max_usage, '
            'is_active, expires_at, allowed_plans, last_used_at, created_at '
            'FROM promocodes WHERE id = ?', (promocode_id,)
        )
        promocode = cursor.fetchone()
        conn.close()
        return promocode

    def validate_promocode(self, code: str, user_id: int = None, plan_key: str = None):
        """Проверка промокода на валидность.

        Проверяет:
        - Существование и активность
        - Срок действия (expires_at)
        - Лимит использований (max_usage)
        - Ограничение per-user (promocode_usage_log)
        - Ограничение по тарифному плану (allowed_plans)
        """
        import json
        from datetime import datetime
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, code, discount_percent, discount_type, usage_count, max_usage,
                   is_active, expires_at, allowed_plans
            FROM promocodes
            WHERE code = ? AND is_active = 1
        ''', (code,))
        promocode = cursor.fetchone()

        if not promocode:
            conn.close()
            return {'valid': False, 'error': 'Промокод не найден или неактивен'}

        (promo_id, promo_code, discount_value, discount_type,
         usage_count, max_usage, is_active, expires_at, allowed_plans) = promocode

        # Проверка срока действия
        if expires_at:
            try:
                exp_dt = datetime.strptime(expires_at, '%Y-%m-%d')
                if datetime.utcnow().date() > exp_dt.date():
                    conn.close()
                    return {'valid': False, 'error': f'Срок действия промокода истёк ({expires_at})'}
            except ValueError as _exc:
                logger.debug("validate_promocode: подавлено исключение: %s", _exc)

        # Проверка лимита использований
        if usage_count >= max_usage:
            conn.close()
            return {'valid': False, 'error': 'Промокод исчерпал лимит использований'}

        # Проверка per-user (один код — один пользователь)
        if user_id is not None:
            cursor.execute(
                'SELECT 1 FROM promocode_usage_log WHERE user_id = ? AND promocode_id = ?',
                (user_id, promo_id)
            )
            if cursor.fetchone():
                conn.close()
                return {'valid': False, 'error': 'Вы уже использовали этот промокод'}

        # Проверка ограничения по тарифному плану
        if allowed_plans and plan_key:
            try:
                plans_list = json.loads(allowed_plans)
                if plans_list and plan_key not in plans_list:
                    conn.close()
                    return {'valid': False, 'error': 'Этот промокод не действует для выбранного тарифа'}
            except (json.JSONDecodeError, TypeError) as _exc:
                logger.debug("validate_promocode: подавлено исключение: %s", _exc)

        conn.close()
        remaining = max_usage - usage_count
        return {
            'valid': True,
            'id': promo_id,
            'code': promo_code,
            'discount_percent': discount_value,
            'discount_type': discount_type,
            'remaining_usage': remaining,
            'expires_at': expires_at,
            'allowed_plans': allowed_plans,
        }

    def apply_promocode(self, promocode_id: int, user_id: int = None):
        """Применение промокода — атомарный инкремент + лог per-user + обновление last_used_at."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE promocodes
            SET usage_count = usage_count + 1,
                last_used_at = CURRENT_TIMESTAMP
            WHERE id = ? AND usage_count < max_usage AND is_active = 1
        ''', (promocode_id,))
        success = cursor.rowcount > 0
        if success and user_id is not None:
            try:
                cursor.execute(
                    'INSERT OR IGNORE INTO promocode_usage_log (user_id, promocode_id) VALUES (?, ?)',
                    (user_id, promocode_id)
                )
            except Exception as _exc:
                logger.debug("apply_promocode: подавлено исключение: %s", _exc)
        conn.commit()
        conn.close()
        return success

    def calculate_discounted_price(self, original_price: float, discount_value,
                                   discount_type: str = 'percent') -> float:
        """Расчёт итоговой цены. discount_type: 'percent' или 'fixed'."""
        if discount_type == 'fixed':
            discounted = original_price - float(discount_value)
        else:
            discounted = original_price * (1 - float(discount_value) / 100)
        return max(0.0, discounted)

    def get_promocode_stats_detailed(self) -> list:
        """Расширенная статистика по всем промокодам с финансовыми данными.
        Возвращает список dict с полями code, discount_percent, discount_type,
        usage_count, max_usage, is_active, expires_at, last_used_at, total_discount_rub."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.id, p.code, p.discount_percent, p.discount_type,
                   p.usage_count, p.max_usage, p.is_active,
                   p.expires_at, p.allowed_plans, p.last_used_at, p.created_at,
                   COALESCE(
                       (SELECT SUM(
                           CASE WHEN p.discount_type = 'fixed'
                               THEN p.discount_percent
                               ELSE ROUND(pr.amount * p.discount_percent / (100.0 - p.discount_percent), 2)
                           END
                       )
                        FROM payment_requests pr
                        WHERE pr.promocode_id = p.id AND pr.status = 'approved'), 0
                   ) AS total_discount_rub
            FROM promocodes p
            ORDER BY p.is_active DESC, p.usage_count DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            result.append({
                'id': row[0], 'code': row[1],
                'discount_percent': row[2], 'discount_type': row[3],
                'usage_count': row[4], 'max_usage': row[5],
                'is_active': bool(row[6]), 'expires_at': row[7],
                'allowed_plans': row[8], 'last_used_at': row[9],
                'created_at': row[10], 'total_discount_rub': row[11] or 0,
            })
        return result

    def add_subscription_plan(self, name, duration_days, price, description):
        """Добавление нового тарифного плана"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO subscription_plans (name, duration_days, price, description)
            VALUES (?, ?, ?, ?)
        ''', (name, duration_days, price, description))
        plan_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return plan_id

    def update_subscription_plan(self, plan_id, **kwargs):
        """Обновление тарифного плана с лимитами"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            updates = []
            params = []

            allowed_fields = ['name', 'duration_days', 'price', 'description', 'is_active',
                             'max_products', 'max_shops', 'max_sales_per_month', 
                             'can_export_reports', 'can_view_analytics', 'can_use_notifications',
                             'can_use_integrations']

            for key, value in kwargs.items():
                if key in allowed_fields:
                    updates.append(f'{key} = ?')
                    params.append(value)

            if updates:
                params.append(plan_id)
                cursor.execute(f'''
                    UPDATE subscription_plans SET {', '.join(updates)}
                    WHERE id = ?
                ''', params)
                conn.commit()
                conn.close()
                return True

            conn.close()
            return False
        except Exception as e:
            logger.error(f"Ошибка обновления плана: {e}")
            return False

    def get_subscription_plan_details(self, plan_id):
        """Получение детальной информации о тарифном плане"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, name, duration_days, price, description, 
                   max_products, max_shops, max_sales_per_month,
                   can_export_reports, can_view_analytics, can_use_notifications,
                   can_use_integrations, is_active, created_at
            FROM subscription_plans WHERE id = ?
        ''', (plan_id,))

        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'id': result[0],
                'name': result[1],
                'duration_days': result[2],
                'price': result[3],
                'description': result[4],
                'max_products': result[5],
                'max_shops': result[6],
                'max_sales_per_month': result[7],
                'can_export_reports': bool(result[8]),
                'can_view_analytics': bool(result[9]),
                'can_use_notifications': bool(result[10]),
                'can_use_integrations': bool(result[11]),
                'is_active': bool(result[12]),
                'created_at': result[13],
            }
        return None

    def create_subscription_plan_with_limits(self, name, duration_days, price, description,
                                           max_products=50, max_shops=1, max_sales_per_month=100,
                                           can_export_reports=False, can_view_analytics=False,
                                           can_use_notifications=False, can_use_integrations=False):
        """Создание нового тарифного плана с лимитами"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO subscription_plans 
            (name, duration_days, price, description, max_products, max_shops, 
             max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications,
             can_use_integrations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, duration_days, price, description, max_products, max_shops,
              max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications,
              can_use_integrations))

        plan_id = cursor.lastrowid
        conn.commit()
        conn.close()

        return plan_id

    def update_subscription_plan_field(self, plan_id, field_name, new_value):
        """Обновление отдельного поля тарифного плана"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Проверяем что план существует
            cursor.execute('SELECT id FROM subscription_plans WHERE id = ?', (plan_id,))
            if not cursor.fetchone():
                conn.close()
                return False

            # Список допустимых полей для обновления
            allowed_fields = [
                'name', 'duration_days', 'price', 'description', 
                'max_products', 'max_shops', 'max_sales_per_month',
                'can_export_reports', 'can_view_analytics', 'can_use_notifications',
                'is_active'
            ]

            if field_name not in allowed_fields:
                conn.close()
                return False

            # Обновляем поле
            query = f'UPDATE subscription_plans SET {field_name} = ? WHERE id = ?'
            cursor.execute(query, (new_value, plan_id))

            conn.commit()
            conn.close()

            return True

        except Exception as e:
            logger.error(f"Ошибка при обновлении поля плана: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_user_subscription_limits(self, user_id):
        """Получение лимитов подписки для пользователя"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Получаем активную подписку пользователя
            cursor.execute('''
                SELECT sp.max_products, sp.max_shops, sp.max_sales_per_month,
                       sp.can_export_reports, sp.can_view_analytics, sp.can_use_notifications,
                       sp.can_use_integrations
                FROM subscriptions s
                JOIN subscription_plans sp ON s.plan_type = sp.name
                WHERE s.user_id = ? AND s.end_date > datetime('now')
                ORDER BY s.end_date DESC
                LIMIT 1
            ''', (user_id,))

            result = cursor.fetchone()
            conn.close()

            if result:
                return {
                    'max_products': result[0],
                    'max_shops': result[1],
                    'max_sales_per_month': result[2],
                    'can_export_reports': bool(result[3]),
                    'can_view_analytics': bool(result[4]),
                    'can_use_notifications': bool(result[5]),
                    'can_use_integrations': bool(result[6]),
                }
            else:
                # Возвращаем лимиты бесплатного плана по умолчанию
                return {
                    'max_products': 50,
                    'max_shops': 1,
                    'max_sales_per_month': 100,
                    'can_export_reports': False,
                    'can_view_analytics': False,
                    'can_use_notifications': False,
                    'can_use_integrations': False,
                }

        except Exception as e:
            logger.error(f"Ошибка при получении лимитов пользователя {user_id}: {e}")
            # Возвращаем лимиты бесплатного плана в случае ошибки
            return {
                'max_products': 50,
                'max_shops': 1,
                'max_sales_per_month': 100,
                'can_export_reports': False,
                'can_view_analytics': False,
                'can_use_notifications': False,
                'can_use_integrations': False,
            }

    def clear_all_sales(self):
        """Очистка всех данных продаж (для сброса рейтингов)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Удаляем все записи из таблицы продаж
            cursor.execute('DELETE FROM sales')

            # Сбрасываем автоинкремент
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='sales'")

            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка при очистке продаж: {e}")
            if conn:
                conn.close()
            return False

    def delete_sales_by_shop_period(self, shop_name: str, start_date: str, end_date: str) -> int:
        """Массовое удаление продаж магазина за период с восстановлением остатков.
        Возвращает количество удалённых записей, или -1 при ошибке."""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, product_id, quantity_sold FROM sales"
                " WHERE shop_name = ? AND sale_date BETWEEN ? AND ?",
                (shop_name, start_date, end_date)
            )
            rows = cursor.fetchall()
            if not rows:
                conn.close()
                return 0
            now = datetime.now().isoformat()
            for sale_id, product_id, qty in rows:
                cursor.execute(
                    "UPDATE inventory SET quantity = quantity + ?, last_updated = ?"
                    " WHERE shop_name = ? AND product_id = ?",
                    (qty, now, shop_name, product_id)
                )
                cursor.execute("DELETE FROM seller_earnings WHERE sale_id = ?", (sale_id,))
                cursor.execute("DELETE FROM sales_audit_log WHERE sale_id = ?", (sale_id,))
            cursor.execute(
                "DELETE FROM sales WHERE shop_name = ? AND sale_date BETWEEN ? AND ?",
                (shop_name, start_date, end_date)
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Ошибка при массовом удалении продаж: {e}")
            if conn:
                conn.close()
            return -1

    def delete_subscription_plan(self, plan_id):
        """Удаление тарифного плана с сохранением активных подписок"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Получаем информацию о плане перед удалением
            cursor.execute('SELECT name FROM subscription_plans WHERE id = ?', (plan_id,))
            plan_info = cursor.fetchone()

            if not plan_info:
                conn.close()
                return False

            plan_name = plan_info[0]

            # Получаем пользователей с активными подписками на этот план
            affected_users = self.get_users_with_subscription_plan(plan_id)

            # Удаляем план из базы данных (активные подписки остаются)
            cursor.execute('DELETE FROM subscription_plans WHERE id = ?', (plan_id,))

            conn.commit()
            deleted = cursor.rowcount > 0
            conn.close()

            # Возвращаем информацию об удалении и затронутых пользователях
            return {
                'success': deleted,
                'plan_name': plan_name,
                'affected_users': affected_users
            }
        except Exception as e:
            logger.error(f"Ошибка при удалении тарифного плана: {e}")
            if conn:
                conn.close()
            return False

    def get_users_with_subscription_plan(self, plan_id):
        """Получение пользователей с определенным тарифным планом"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT DISTINCT u.*, s.plan_type, s.start_date, s.end_date
                FROM users u
                JOIN subscriptions s ON u.telegram_id = s.user_id
                JOIN subscription_plans sp ON s.plan_type = sp.name
                WHERE sp.id = ? AND s.end_date > datetime('now')
            ''', (plan_id,))

            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении пользователей с планом {plan_id}: {e}")
            if conn:
                conn.close()
            return []

    # ============ СИСТЕМА МОТИВАЦИИ ============

    def set_product_motivation(self, product_id, motivation_type, motivation_value,
                               admin_telegram_id, scope_type='global', scope_value='',
                               recalculate=True):
        """Установка мотивации для товара (таргетированная по оргструктуре, task #49).

        scope_type: global / trade_network / city / shop / user
        scope_value: '' для global; имя сети/города/магазина; str(user_id) для user.
        Для global старая ставка сохраняется в motivation_history.
        Пишет в product_motivation_rules (UNIQUE(product_id, scope_type, scope_value))."""
        try:
            scope_type = scope_type or 'global'
            scope_value = '' if scope_type == 'global' else (str(scope_value).strip() if scope_value is not None else '')

            conn = self.get_connection()
            cursor = conn.cursor()

            # Получаем внутренний ID пользователя по его telegram_id
            admin_id = None
            if admin_telegram_id:
                cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (admin_telegram_id,))
                res = cursor.fetchone()
                admin_id = res[0] if res else None

            # Сохраняем старую глобальную ставку в историю
            if scope_type == 'global':
                cursor.execute('''
                    SELECT motivation_type, motivation_value FROM product_motivation_rules
                    WHERE product_id = ? AND scope_type = 'global' AND scope_value = ''
                ''', (product_id,))
                old = cursor.fetchone()
                if old:
                    cursor.execute('''
                        INSERT INTO motivation_history (product_id, motivation_type, motivation_value, changed_by)
                        VALUES (?, ?, ?, ?)
                    ''', (product_id, old[0], old[1], admin_id))

            cursor.execute('''
                INSERT INTO product_motivation_rules
                    (product_id, scope_type, scope_value, motivation_type, motivation_value, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, scope_type, scope_value) DO UPDATE SET
                    motivation_type  = excluded.motivation_type,
                    motivation_value = excluded.motivation_value,
                    created_by       = excluded.created_by,
                    created_at       = CURRENT_TIMESTAMP
            ''', (product_id, scope_type, scope_value, motivation_type, motivation_value, admin_id))

            conn.commit()
            conn.close()

            # Пересчитываем заработки за текущий месяц
            if recalculate:
                self.recalculate_month_earnings(product_id)
            return True
        except Exception as e:
            logger.error(f"Ошибка при установке мотивации: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_motivation_rules(self, product_id):
        """Все правила мотивации для товара (для бот/веб просмотра и удаления).
        Возвращает список dict: id, scope_type, scope_value, motivation_type, motivation_value, created_at."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, scope_type, scope_value, motivation_type, motivation_value, created_at
                FROM product_motivation_rules
                WHERE product_id = ?
                ORDER BY CASE scope_type
                    WHEN 'user' THEN 1 WHEN 'shop' THEN 2 WHEN 'city' THEN 3
                    WHEN 'trade_network' THEN 4 ELSE 5 END, scope_value
            ''', (product_id,))
            rows = cursor.fetchall()
            conn.close()
            return [{'id': r[0], 'scope_type': r[1], 'scope_value': r[2],
                     'motivation_type': r[3], 'motivation_value': r[4], 'created_at': r[5]}
                    for r in rows]
        except Exception as e:
            logger.error(f"Ошибка get_motivation_rules: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_all_motivation_rules(self, include_global=False):
        """Все таргетированные правила (по умолчанию без global) с именами товаров и сотрудников.
        Возвращает список dict для веб-таблицы правил."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            where = "" if include_global else "WHERE pmr.scope_type != 'global'"
            cursor.execute(f'''
                SELECT pmr.id, pmr.product_id, p.name, pmr.scope_type, pmr.scope_value,
                       pmr.motivation_type, pmr.motivation_value, pmr.created_at,
                       u.first_name, u.last_name
                FROM product_motivation_rules pmr
                JOIN products p ON pmr.product_id = p.id
                LEFT JOIN users u ON pmr.scope_type = 'user'
                                 AND CAST(pmr.scope_value AS INTEGER) = u.id
                {where}
                ORDER BY p.name,
                    CASE pmr.scope_type
                        WHEN 'user' THEN 1 WHEN 'shop' THEN 2 WHEN 'city' THEN 3
                        WHEN 'trade_network' THEN 4 ELSE 5 END,
                    pmr.scope_value
            ''')
            rows = cursor.fetchall()
            conn.close()
            result = []
            for r in rows:
                label = r[4]
                if r[3] == 'user':
                    nm = f"{r[8] or ''} {r[9] or ''}".strip()
                    label = nm or r[4]
                result.append({
                    'id': r[0], 'product_id': r[1], 'product_name': r[2],
                    'scope_type': r[3], 'scope_value': r[4], 'scope_label': label,
                    'motivation_type': r[5], 'motivation_value': r[6], 'created_at': r[7],
                })
            return result
        except Exception as e:
            logger.error(f"Ошибка get_all_motivation_rules: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def remove_motivation_rule(self, rule_id):
        """Удалить одно правило мотивации по id. Возвращает product_id или None."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT product_id FROM product_motivation_rules WHERE id = ?', (rule_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return None
            pid = row[0]
            cursor.execute('DELETE FROM product_motivation_rules WHERE id = ?', (rule_id,))
            conn.commit()
            conn.close()
            try:
                self.recalculate_month_earnings(pid)
            except Exception as _exc:
                logger.debug("remove_motivation_rule recalc подавлено: %s", _exc)
            return pid
        except Exception as e:
            logger.error(f"Ошибка remove_motivation_rule: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def update_motivation_rule_value(self, rule_id, motivation_type, motivation_value):
        """Обновить ставку таргетированного правила мотивации. Возвращает product_id или None."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT product_id FROM product_motivation_rules WHERE id = ?', (rule_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return None
            pid = row[0]
            cursor.execute(
                'UPDATE product_motivation_rules SET motivation_type = ?, motivation_value = ? WHERE id = ?',
                (motivation_type, motivation_value, rule_id),
            )
            conn.commit()
            conn.close()
            try:
                self.recalculate_month_earnings(pid)
            except Exception as _exc:
                logger.debug("update_motivation_rule_value recalc подавлено: %s", _exc)
            return pid
        except Exception as e:
            logger.error(f"Ошибка update_motivation_rule_value: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def _get_seller_attrs(self, user_id, shop_name=None):
        """Атрибуты продавца для таргетинга мотивации: user_id, shop_name, city, trade_network.
        city/trade_network берутся из карточки сотрудника (users); shop_name — из продажи, если задан."""
        attrs = {'user_id': user_id, 'shop_name': shop_name, 'city': None, 'trade_network': None}
        if user_id is None:
            return attrs
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT trade_network, shop_name, city FROM users WHERE id = ?', (user_id,))
            r = cursor.fetchone()
            conn.close()
            if r:
                attrs['trade_network'] = r[0]
                if not shop_name:
                    attrs['shop_name'] = r[1]
                attrs['city'] = r[2]
        except Exception as e:
            logger.debug("_get_seller_attrs подавлено: %s", e)
            if 'conn' in locals():
                conn.close()
        return attrs

    def resolve_motivation(self, product_id, seller_attrs=None):
        """Выбрать наиболее специфичное правило мотивации для товара и продавца.
        Приоритет: user > shop > city > trade_network > global.
        Возвращает dict {motivation_type, motivation_value, motivation_source} или None."""
        seller_attrs = seller_attrs or {}
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT scope_type, scope_value, motivation_type, motivation_value
                FROM product_motivation_rules WHERE product_id = ?
            ''', (product_id,))
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка resolve_motivation: {e}")
            if 'conn' in locals():
                conn.close()
            return None
        if not rows:
            return None
        by_scope = {}
        for st, sv, mt, mv in rows:
            by_scope[(st, (sv or '').strip().lower())] = (mt, mv)
        uid = seller_attrs.get('user_id')
        priority = [
            ('user', str(uid) if uid is not None else None),
            ('shop', seller_attrs.get('shop_name')),
            ('city', seller_attrs.get('city')),
            ('trade_network', seller_attrs.get('trade_network')),
            ('global', ''),
        ]
        for st, val in priority:
            if val is None:
                continue
            key = (st, str(val).strip().lower())
            if key in by_scope:
                mt, mv = by_scope[key]
                return {'motivation_type': mt, 'motivation_value': mv, 'motivation_source': st}
        return None

    def get_motivation_history(self, product_id, limit=10):
        """История изменений мотивации для товара"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT mh.motivation_type, mh.motivation_value, mh.changed_at,
                       u.first_name, u.last_name
                FROM motivation_history mh
                LEFT JOIN users u ON mh.changed_by = u.id
                WHERE mh.product_id = ?
                ORDER BY mh.changed_at DESC
                LIMIT ?
            ''', (product_id, limit))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Ошибка get_motivation_history: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def recalculate_month_earnings(self, product_id=None, year=None, month=None):
        """Пересчитать seller_earnings за указанный месяц (по умолчанию — текущий).
        Если product_id задан — пересчитываем только продажи этого товара.
        Если None — пересчитываем все продажи за месяц."""
        import calendar as _cal
        from datetime import date
        try:
            today = date.today()
            if year is None:
                year = today.year
            if month is None:
                month = today.month

            _, last_day = _cal.monthrange(year, month)
            month_start = f"{year}-{month:02d}-01"
            month_end   = f"{year}-{month:02d}-{last_day:02d}"

            conn = self.get_connection()
            cursor = conn.cursor()

            # Получаем все продажи за указанный месяц (с фильтром по товару если нужно)
            if product_id is not None:
                cursor.execute('''
                    SELECT s.id, s.product_id, s.sale_price, s.quantity_sold,
                           s.user_id, s.shop_name, u.telegram_id
                    FROM sales s
                    JOIN users u ON s.user_id = u.id
                    WHERE date(s.sale_date) BETWEEN ? AND ?
                      AND s.product_id = ?
                ''', (month_start, month_end, product_id))
            else:
                cursor.execute('''
                    SELECT s.id, s.product_id, s.sale_price, s.quantity_sold,
                           s.user_id, s.shop_name, u.telegram_id
                    FROM sales s
                    JOIN users u ON s.user_id = u.id
                    WHERE date(s.sale_date) BETWEEN ? AND ?
                ''', (month_start, month_end))

            sales = cursor.fetchall()
            conn.close()

            # Пересчитываем каждую продажу
            for sale_id, prod_id, sale_price, qty, user_id, shop_name, tg_id in sales:
                _seller_attrs = self._get_seller_attrs(user_id, shop_name)
                new_commission = self.calculate_seller_commission(
                    sale_id, prod_id, sale_price, qty,
                    user_id=user_id, shop_name=shop_name,
                    sale_year=year, sale_month=month,
                    seller_attrs=_seller_attrs
                )
                motivation_info = self.get_motivation_for_month(prod_id, year, month,
                                                                seller_attrs=_seller_attrs)
                m_type = motivation_info['motivation_type'] if motivation_info else 'percentage'
                m_val  = motivation_info['motivation_value'] if motivation_info else 0.0
                m_src  = motivation_info.get('motivation_source', 'global') if motivation_info else 'global'

                conn2 = self.get_connection()
                cur2 = conn2.cursor()
                # Обновляем если запись есть, иначе вставляем
                cur2.execute('SELECT id FROM seller_earnings WHERE sale_id = ? AND user_id = ?',
                             (sale_id, user_id))
                existing = cur2.fetchone()
                if existing:
                    cur2.execute('''
                        UPDATE seller_earnings
                        SET commission_amount=?, motivation_type=?, motivation_value=?, motivation_source=?
                        WHERE sale_id=? AND user_id=?
                    ''', (new_commission, m_type, m_val, m_src, sale_id, user_id))
                else:
                    cur2.execute('''
                        INSERT INTO seller_earnings
                        (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value, motivation_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (sale_id, user_id, prod_id, new_commission, m_type, m_val, m_src))
                conn2.commit()
                conn2.close()

            logger.info(f"recalculate_month_earnings: обработано {len(sales)} продаж, product_id={product_id}, {year}-{month:02d}")
        except Exception as e:
            logger.error(f"Ошибка recalculate_month_earnings: {e}")
            if 'conn' in locals():
                try: conn.close()
                except: pass

    def set_motivation_for_month(self, product_id, year, month, motivation_type, motivation_value,
                                  admin_telegram_id=None):
        """Установить мотивацию на товар для конкретного месяца.
        Сохраняет в motivation_schedule; UNIQUE(product_id, year, month) — перезаписывает."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            admin_id = None
            if admin_telegram_id:
                cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (admin_telegram_id,))
                r = cursor.fetchone()
                admin_id = r[0] if r else None
            cursor.execute('''
                INSERT INTO motivation_schedule
                    (product_id, year, month, motivation_type, motivation_value, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, year, month) DO UPDATE SET
                    motivation_type  = excluded.motivation_type,
                    motivation_value = excluded.motivation_value,
                    created_by       = excluded.created_by,
                    created_at       = CURRENT_TIMESTAMP
            ''', (product_id, year, month, motivation_type, motivation_value, admin_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка set_motivation_for_month: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_motivation_for_month(self, product_id, year, month, seller_attrs=None):
        """Получить мотивацию для товара на конкретный месяц.
        Приоритет: motivation_schedule (месячная ставка) → таргетированное правило
        (user>shop>city>network>global). Если seller_attrs не задан — fallback на глобальную ставку."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT motivation_type, motivation_value
                FROM motivation_schedule
                WHERE product_id = ? AND year = ? AND month = ?
            ''', (product_id, year, month))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {'motivation_type': row[0], 'motivation_value': row[1],
                        'is_scheduled': True, 'motivation_source': 'schedule'}
            # Fallback на таргетированное/глобальное правило
            info = self.resolve_motivation(product_id, seller_attrs or {})
            if info:
                info['is_scheduled'] = False
                return info
            return None
        except Exception as e:
            logger.error(f"Ошибка get_motivation_for_month: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def get_motivation_schedule(self, product_id):
        """Получить все месячные записи мотивации для товара, отсортированные по убыванию."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ms.year, ms.month, ms.motivation_type, ms.motivation_value,
                       ms.created_at, u.first_name, u.last_name
                FROM motivation_schedule ms
                LEFT JOIN users u ON ms.created_by = u.id
                WHERE ms.product_id = ?
                ORDER BY ms.year DESC, ms.month DESC
            ''', (product_id,))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Ошибка get_motivation_schedule: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_all_motivation_schedules(self):
        """Получить все записи motivation_schedule с именами товаров."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ms.product_id, p.name, ms.year, ms.month,
                       ms.motivation_type, ms.motivation_value
                FROM motivation_schedule ms
                JOIN products p ON ms.product_id = p.id
                ORDER BY p.name, ms.year DESC, ms.month DESC
            ''')
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Ошибка get_all_motivation_schedules: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def set_extra_condition_for_month(self, condition_type, year, month, shop_name=None,
                                       min_sellers=None, coefficient=None, user_id=None,
                                       allowed_categories=None, description=None,
                                       calc_mode='individual', admin_telegram_id=None):
        """Добавить/обновить доп. условие мотивации для конкретного месяца."""
        try:
            import json as _json
            conn = self.get_connection()
            cursor = conn.cursor()
            allowed_str = _json.dumps(allowed_categories, ensure_ascii=False) if allowed_categories else None
            created_by = None
            if admin_telegram_id:
                cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (admin_telegram_id,))
                r = cursor.fetchone()
                created_by = r[0] if r else None
            # Удаляем старые записи того же типа/магазина/пользователя/месяца перед вставкой
            if condition_type == 'category_filter':
                cursor.execute('''
                    DELETE FROM extra_conditions_schedule
                    WHERE condition_type = ? AND user_id = ? AND year = ? AND month = ?
                ''', (condition_type, user_id, year, month))
            else:
                cursor.execute('''
                    DELETE FROM extra_conditions_schedule
                    WHERE condition_type = ? AND year = ? AND month = ?
                      AND (shop_name = ? OR (shop_name IS NULL AND ? IS NULL))
                ''', (condition_type, year, month, shop_name, shop_name))
            cursor.execute('''
                INSERT INTO extra_conditions_schedule
                    (condition_type, description, shop_name, min_sellers, coefficient,
                     user_id, allowed_categories, calc_mode, year, month, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (condition_type, description, shop_name, min_sellers, coefficient,
                  user_id, allowed_str, calc_mode, year, month, created_by))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка set_extra_condition_for_month: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_extra_conditions_for_month(self, year, month):
        """Получить доп. условия для конкретного месяца (только из расписания)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ecs.id, ecs.condition_type, ecs.description,
                       ecs.shop_name, ecs.min_sellers, ecs.coefficient,
                       ecs.user_id, ecs.allowed_categories,
                       ecs.is_active, ecs.created_at,
                       u.first_name, u.last_name,
                       COALESCE(ecs.calc_mode, 'individual') as calc_mode,
                       ecs.year, ecs.month
                FROM extra_conditions_schedule ecs
                LEFT JOIN users u ON ecs.user_id = u.id
                WHERE ecs.year = ? AND ecs.month = ? AND ecs.is_active = 1
                ORDER BY ecs.created_at DESC
            ''', (year, month))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Ошибка get_extra_conditions_for_month: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_products_by_category(self, category):
        """Получить все товары в заданной категории"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products WHERE category = ? ORDER BY name', (category,))
        products = cursor.fetchall()
        conn.close()
        return products

    # ── Ручные корректировки результатов конкурса по магазинам ────────────────

    def set_contest_manual_result(self, contest_id, shop_name, manual_value, editor_telegram_id=None):
        """Установить ручную корректировку результата магазина в конкурсе"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            editor_id = None
            if editor_telegram_id:
                cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (editor_telegram_id,))
                r = cursor.fetchone()
                editor_id = r[0] if r else None
            cursor.execute('''
                INSERT INTO contest_manual_results (contest_id, shop_name, manual_value, edited_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(contest_id, shop_name) DO UPDATE SET
                    manual_value=excluded.manual_value,
                    edited_by=excluded.edited_by,
                    edited_at=datetime('now')
            ''', (contest_id, shop_name, manual_value, editor_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка set_contest_manual_result: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_contest_manual_results(self, contest_id):
        """Получить все ручные корректировки для конкурса {shop_name: manual_value}"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT cmr.shop_name, manual_value, edited_at, u.first_name, u.last_name
                FROM contest_manual_results cmr
                LEFT JOIN users u ON cmr.edited_by = u.id
                WHERE contest_id = ?
            ''', (contest_id,))
            rows = cursor.fetchall()
            conn.close()
            return {r[0]: {'value': r[1], 'edited_at': r[2],
                           'editor': f"{r[3] or ''} {r[4] or ''}".strip()}
                    for r in rows}
        except Exception as e:
            logger.error(f"Ошибка get_contest_manual_results: {e}")
            if 'conn' in locals():
                conn.close()
            return {}

    def delete_contest_manual_result(self, contest_id, shop_name):
        """Удалить ручную корректировку для магазина"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM contest_manual_results WHERE contest_id=? AND shop_name=?',
                           (contest_id, shop_name))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка delete_contest_manual_result: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_product_motivation(self, product_id, seller_attrs=None):
        """Получение мотивации для товара.
        Если seller_attrs задан — выбирает наиболее специфичное правило (user>shop>city>network>global)
        и возвращает dict с motivation_source. Иначе — глобальную ставку (обратная совместимость)."""
        if seller_attrs is not None:
            return self.resolve_motivation(product_id, seller_attrs)
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT motivation_type, motivation_value
                FROM product_motivation_rules
                WHERE product_id = ? AND scope_type = 'global' AND scope_value = ''
            ''', (product_id,))

            result = cursor.fetchone()
            conn.close()

            if result:
                return {
                    'motivation_type': result[0],
                    'motivation_value': result[1]
                }
            return None
        except Exception as e:
            logger.error(f"Ошибка при получении мотивации: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def get_all_product_motivations(self):
        """Получение всех комиссий по товарам с ФИО администратора"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT p.id, p.name, pc.motivation_type, pc.motivation_value, 
                       u.first_name, u.last_name, pc.created_at
                FROM products p
                LEFT JOIN product_motivation_rules pc
                       ON p.id = pc.product_id AND pc.scope_type = 'global' AND pc.scope_value = ''
                LEFT JOIN users u ON pc.created_by = u.id
                ORDER BY p.name
            ''')

            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении всех комиссий: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_effective_motivation_matrix(self, col_months):
        """Вернуть эффективные ставки для всех товаров с мотивацией за указанные месяцы.
        col_months: list of (year, month) tuples.
        Возвращает (prod_order, product_names, cell_data) где:
          prod_order — отсортированный список product_id
          product_names — {prod_id: name}
          cell_data — {prod_id: {(yr, mo): {'type':..., 'value':..., 'is_scheduled': bool}}}
        Включает только товары у которых есть глобальная мотивация ИЛИ хотя бы одна запись в расписании.
        """
        from collections import defaultdict
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Все глобальные мотивации
            cursor.execute('''
                SELECT p.id, p.name, pm.motivation_type, pm.motivation_value
                FROM product_motivation_rules pm
                JOIN products p ON pm.product_id = p.id
                WHERE pm.scope_type = 'global' AND pm.scope_value = ''
                ORDER BY p.name
            ''')
            globals_rows = cursor.fetchall()

            # Все записи расписания для нужных месяцев
            if col_months:
                placeholders = ','.join(['(?,?)'] * len(col_months))
                params = [v for ym in col_months for v in ym]
                cursor.execute(f'''
                    SELECT ms.product_id, p.name, ms.year, ms.month,
                           ms.motivation_type, ms.motivation_value
                    FROM motivation_schedule ms
                    JOIN products p ON ms.product_id = p.id
                    WHERE (ms.year, ms.month) IN ({placeholders})
                ''', params)
                sched_rows = cursor.fetchall()
            else:
                sched_rows = []
            conn.close()

            product_names = {}
            global_rates = {}
            for pid, pname, gtype, gval in globals_rows:
                product_names[pid] = pname
                global_rates[pid] = (gtype, gval)

            # Add products only in schedule (no global rate)
            for pid, pname, yr, mo, mtype, mval in sched_rows:
                if pid not in product_names:
                    product_names[pid] = pname

            prod_order = sorted(product_names.keys(), key=lambda x: product_names[x])

            # Build scheduled lookup
            sched_lookup = defaultdict(dict)
            for pid, pname, yr, mo, mtype, mval in sched_rows:
                sched_lookup[pid][(yr, mo)] = (mtype, mval)

            # Build cell data with fallback
            cell_data = {}
            for pid in prod_order:
                cell_data[pid] = {}
                for ym in col_months:
                    if ym in sched_lookup[pid]:
                        mt, mv = sched_lookup[pid][ym]
                        cell_data[pid][ym] = {'type': mt, 'value': mv, 'is_scheduled': True}
                    elif pid in global_rates:
                        gt, gv = global_rates[pid]
                        cell_data[pid][ym] = {'type': gt, 'value': gv, 'is_scheduled': False}
                    # else: no data for this cell

            return prod_order, product_names, cell_data
        except Exception as e:
            logger.error(f"Ошибка get_effective_motivation_matrix: {e}")
            if 'conn' in locals():
                conn.close()
            return [], {}, {}

    def remove_product_motivation(self, product_id):
        """Удаление ВСЕХ правил мотивации с товара (глобальной и таргетированных)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('DELETE FROM product_motivation_rules WHERE product_id = ?', (product_id,))

            conn.commit()
            conn.close()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка при удалении мотивации: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def apply_extra_conditions(self, base_commission, user_id, product_id, shop_name,
                               year=None, month=None):
        """Применить доп. условия мотивации: фильтр категорий и коэффициент смены.
        Если year/month заданы — проверяет extra_conditions_schedule сначала, иначе global."""
        if base_commission <= 0:
            return base_commission
        try:
            import json as _json
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('SELECT category FROM products WHERE id = ?', (product_id,))
            prod = cursor.fetchone()
            product_category = prod[0] if prod else None

            # ─── Фильтр категорий ─────────────────────────────────────────────
            cat_row = None
            if year is not None and month is not None:
                cursor.execute('''
                    SELECT allowed_categories FROM extra_conditions_schedule
                    WHERE condition_type = 'category_filter' AND user_id = ?
                      AND year = ? AND month = ? AND is_active = 1
                ''', (user_id, year, month))
                cat_row = cursor.fetchone()
            if cat_row is None:
                cursor.execute('''
                    SELECT allowed_categories FROM motivation_extra_conditions
                    WHERE condition_type = 'category_filter' AND user_id = ? AND is_active = 1
                ''', (user_id,))
                cat_row = cursor.fetchone()

            if cat_row and cat_row[0]:
                allowed = _json.loads(cat_row[0])
                if allowed and product_category not in allowed:
                    conn.close()
                    return 0.0

            # ─── Коэффициент смены ────────────────────────────────────────────
            # Для joint-режима коэффициент НЕ применяется здесь — он применяется
            # к общему пулу магазина в get_joint_bonus_adjustment().
            # Для individual-режима коэффициент применяется к личной комиссии.
            coeff_conditions = []
            if year is not None and month is not None:
                cursor.execute('''
                    SELECT shop_name, min_sellers, coefficient,
                           COALESCE(calc_mode, 'individual') as calc_mode
                    FROM extra_conditions_schedule
                    WHERE condition_type = 'multi_seller_coeff' AND is_active = 1
                      AND year = ? AND month = ?
                      AND (shop_name = ? OR shop_name IS NULL)
                    ORDER BY shop_name DESC
                ''', (year, month, shop_name))
                coeff_conditions = cursor.fetchall()
            if not coeff_conditions:
                cursor.execute('''
                    SELECT shop_name, min_sellers, coefficient,
                           COALESCE(calc_mode, 'individual') as calc_mode
                    FROM motivation_extra_conditions
                    WHERE condition_type = 'multi_seller_coeff' AND is_active = 1
                      AND (shop_name = ? OR shop_name IS NULL)
                    ORDER BY shop_name DESC
                ''', (shop_name,))
                coeff_conditions = cursor.fetchall()

            cursor.execute(
                'SELECT daily_rate FROM salary_settings WHERE user_id = ? AND daily_rate > 0',
                (user_id,)
            )
            has_fixed_salary = cursor.fetchone() is not None

            final_commission = base_commission
            if coeff_conditions:
                if year is not None and month is not None:
                    import calendar as _cal
                    _, last_day = _cal.monthrange(year, month)
                    start_of_month = f"{year}-{month:02d}-01"
                    end_of_month   = f"{year}-{month:02d}-{last_day:02d}"
                    cursor.execute('''
                        SELECT COUNT(DISTINCT user_id) FROM sales
                        WHERE shop_name = ? AND date(sale_date) BETWEEN date(?) AND date(?)
                    ''', (shop_name, start_of_month, end_of_month))
                else:
                    start_of_month = datetime.now().replace(day=1).strftime('%Y-%m-%d')
                    cursor.execute('''
                        SELECT COUNT(DISTINCT user_id) FROM sales
                        WHERE shop_name = ? AND date(sale_date) >= date(?)
                    ''', (shop_name, start_of_month))
                sellers_count = cursor.fetchone()[0]

                for _cond_shop, min_sellers, coefficient, calc_mode in coeff_conditions:
                    if sellers_count >= min_sellers:
                        if calc_mode != 'joint':
                            # Раздельный: коэффициент умножается на личную комиссию сразу
                            final_commission = round(final_commission * coefficient, 2)
                        # Joint: коэффициент применяется к пулу в get_joint_bonus_adjustment
                        break

            conn.close()
            return round(final_commission, 2)
        except Exception as e:
            logger.error(f"Ошибка применения доп. условий мотивации: {e}")
            if 'conn' in locals():
                conn.close()
            return base_commission

    # ──────────────────────────────────────────────
    # КОНКУРСЫ
    # ──────────────────────────────────────────────

    def _ensure_contests_table(self, cursor):
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                contest_type TEXT NOT NULL DEFAULT 'any',
                metric_type TEXT NOT NULL DEFAULT 'turnover',
                target_value REAL NOT NULL DEFAULT 0,
                reward_type TEXT NOT NULL DEFAULT 'fixed',
                reward_value REAL NOT NULL DEFAULT 0,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                shop_filter TEXT,
                city_filter TEXT,
                user_filter TEXT,
                product_filter TEXT,
                category_filter TEXT,
                extra_conditions TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                notify_on_start INTEGER NOT NULL DEFAULT 0,
                notify_on_end INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                reward_mode TEXT NOT NULL DEFAULT 'total',
                individual_targets TEXT
            )
        ''')
        for col, defn in [
            ('reward_mode', "TEXT NOT NULL DEFAULT 'total'"),
            ('individual_targets', 'TEXT'),
        ]:
            try:
                cursor.execute(f'ALTER TABLE contests ADD COLUMN {col} {defn}')
            except Exception as _exc:
                logger.debug("_ensure_contests_table: подавлено исключение: %s", _exc)

    def _ensure_contest_bonuses_table(self, cursor):
        """Тиры бонусов для per_sale конкурсов (бонус за каждую продажу)."""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contest_product_bonuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contest_id INTEGER NOT NULL,
                product_id INTEGER,
                product_name TEXT,
                min_plan_pct REAL NOT NULL DEFAULT 0,
                bonus_per_unit REAL NOT NULL DEFAULT 0
            )
        ''')

    def create_contest(self, title, description=None, contest_type='any',
                       metric_type='turnover', target_value=0,
                       reward_type='fixed', reward_value=0,
                       start_date=None, end_date=None,
                       shop_filter=None, city_filter=None, user_filter=None,
                       product_filter=None, category_filter=None,
                       extra_conditions=None, notify_on_start=0, notify_on_end=0,
                       created_by=None, reward_mode='total', individual_targets=None):
        """Создать конкурс"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            self._ensure_contests_table(cursor)
            self._ensure_contest_bonuses_table(cursor)
            cursor.execute('''
                INSERT INTO contests
                (title, description, contest_type, metric_type, target_value,
                 reward_type, reward_value, start_date, end_date,
                 shop_filter, city_filter, user_filter, product_filter,
                 category_filter, extra_conditions, notify_on_start, notify_on_end,
                 created_by, reward_mode, individual_targets)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (title, description, contest_type, metric_type, target_value,
                  reward_type, reward_value, start_date, end_date,
                  shop_filter, city_filter, user_filter, product_filter,
                  category_filter, extra_conditions, notify_on_start, notify_on_end,
                  created_by, reward_mode, individual_targets))
            new_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return new_id
        except Exception as e:
            logger.error(f"Ошибка create_contest: {e}")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception as _exc:
                    logger.debug("create_contest: подавлено исключение: %s", _exc)
                conn.close()
            return None

    def save_contest_product_bonuses(self, contest_id: int, tiers: list) -> bool:
        """Сохранить тиры бонусов per_sale конкурса.

        tiers — список dict:
          {'min_plan_pct': 0, 'bonuses': [{product_id, product_name, bonus_per_unit}, ...]}
        Для flat-бонуса (any/category): {'min_plan_pct': 0, 'bonuses': [{'product_id': None, 'product_name': None, 'bonus_per_unit': X}]}
        """
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            self._ensure_contest_bonuses_table(cursor)
            cursor.execute('DELETE FROM contest_product_bonuses WHERE contest_id = ?', (contest_id,))
            for tier in tiers:
                pct = tier.get('min_plan_pct', 0)
                for b in tier.get('bonuses', []):
                    cursor.execute('''
                        INSERT INTO contest_product_bonuses
                        (contest_id, product_id, product_name, min_plan_pct, bonus_per_unit)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (contest_id, b.get('product_id'), b.get('product_name'), pct, b.get('bonus_per_unit', 0)))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка save_contest_product_bonuses: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception as _exc:
                    logger.debug("save_contest_product_bonuses: подавлено исключение: %s", _exc)
                conn.close()
            return False

    def get_contest_product_bonuses(self, contest_id: int) -> list:
        """Получить тиры бонусов для конкурса.
        Возвращает список dict: {product_id, product_name, min_plan_pct, bonus_per_unit}
        отсортированных по min_plan_pct DESC (наибольший тир первым).
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            self._ensure_contest_bonuses_table(cursor)
            cursor.execute('''
                SELECT product_id, product_name, min_plan_pct, bonus_per_unit
                FROM contest_product_bonuses
                WHERE contest_id = ?
                ORDER BY min_plan_pct DESC, product_id
            ''', (contest_id,))
            rows = cursor.fetchall()
            conn.close()
            return [{'product_id': r[0], 'product_name': r[1],
                     'min_plan_pct': r[2], 'bonus_per_unit': r[3]} for r in rows]
        except Exception as e:
            logger.error(f"Ошибка get_contest_product_bonuses: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_user_plan_pct_for_contest(self, telegram_id: int) -> float:
        """Процент выполнения плана продавца (максимум по всем активным планам продавца)."""
        try:
            progress = self.get_user_plans_progress(telegram_id)
            if not progress:
                return 0.0
            return max((pct for _, _, pct in progress), default=0.0)
        except Exception:
            return 0.0

    def get_contests(self, status=None):
        """Получить конкурсы. Порядок колонок:
        0:id 1:title 2:desc 3:contest_type 4:metric_type 5:target_value
        6:reward_type 7:reward_value 8:start_date 9:end_date
        10:shop_filter 11:city_filter 12:user_filter 13:product_filter
        14:category_filter 15:extra_conditions 16:status
        17:notify_on_start 18:notify_on_end 19:created_by 20:created_at"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            self._ensure_contests_table(cursor)
            if status:
                cursor.execute(
                    'SELECT * FROM contests WHERE status = ? ORDER BY created_at DESC',
                    (status,)
                )
            else:
                cursor.execute('SELECT * FROM contests ORDER BY created_at DESC')
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_contests: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_contest(self, contest_id):
        """Получить конкурс по id"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            self._ensure_contests_table(cursor)
            cursor.execute('SELECT * FROM contests WHERE id = ?', (contest_id,))
            result = cursor.fetchone()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_contest: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def update_contest_status(self, contest_id, status):
        """Обновить статус конкурса"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            self._ensure_contests_table(cursor)
            cursor.execute(
                'UPDATE contests SET status = ? WHERE id = ?',
                (status, contest_id)
            )
            conn.commit()
            updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Ошибка update_contest_status: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def update_contest(self, contest_id: int, **fields) -> bool:
        """Обновить поля конкурса."""
        allowed = {'start_date', 'end_date', 'target_value', 'reward_value',
                   'individual_targets', 'reward_mode'}
        to_update = {k: v for k, v in fields.items() if k in allowed}
        if not to_update:
            return False
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            sets = ', '.join(f'{k} = ?' for k in to_update)
            vals = list(to_update.values()) + [contest_id]
            cursor.execute(f'UPDATE contests SET {sets} WHERE id = ?', vals)
            conn.commit()
            updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Ошибка update_contest: {e}")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception as _exc:
                    logger.debug("update_contest: подавлено исключение: %s", _exc)
                conn.close()
            return False

    def delete_contest(self, contest_id):
        """Удалить конкурс"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM contests WHERE id = ?', (contest_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Ошибка delete_contest: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def clear_contests_archive(self):
        """Удалить все завершённые и отменённые конкурсы из архива. Возвращает кол-во удалённых."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM contests WHERE status IN ('finished', 'cancelled')")
            conn.commit()
            count = cursor.rowcount
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Ошибка clear_contests_archive: {e}")
            if 'conn' in locals():
                conn.close()
            return 0

    def _build_contest_sale_query(self, start, end, shops, cities, users, products, categories, metric):
        """Вспомогательный метод: условия WHERE и SELECT для запроса продаж конкурса."""
        conditions = ["date(s.sale_date) >= ? AND date(s.sale_date) <= ?"]
        params = [start, end]
        if shops:
            placeholders = ",".join(["?"] * len(shops))
            conditions.append(f"s.shop_name IN ({placeholders})")
            params.extend(shops)
        if users:
            placeholders = ",".join(["?"] * len(users))
            conditions.append(f"s.user_id IN ({placeholders})")
            params.extend(users)
        if products:
            placeholders = ",".join(["?"] * len(products))
            conditions.append(f"s.product_id IN ({placeholders})")
            params.extend(products)
        elif categories:
            placeholders = ",".join(["?"] * len(categories))
            conditions.append(f"p.category IN ({placeholders})")
            params.extend(categories)
        if cities:
            placeholders = ",".join(["?"] * len(cities))
            conditions.append(f"u.city IN ({placeholders})")
            params.extend(cities)
        where = " AND ".join(conditions)
        metric_expr = (
            "SUM(COALESCE(s.quantity_sold, 0) * COALESCE(s.sale_price, 0))"
            if metric == 'turnover'
            else "SUM(COALESCE(s.quantity_sold, 0))"
        )
        return where, metric_expr, params

    def compute_contest_results(self, contest_id):
        """Рассчитать результаты конкурса.
        Режим 'total': возвращает список dict: user_id, first_name, last_name, telegram_id,
            shop_name, actual, reward, is_winner, individual_target.
        Режим 'per_sale': возвращает список dict с bonus_earned вместо reward.
        """
        try:
            import json as _json
            contest = self.get_contest(contest_id)
            if not contest:
                return []

            metric   = contest[4]
            target   = contest[5]
            rtype    = contest[6]
            rval     = contest[7]
            start    = contest[8]
            end      = contest[9]
            shop_f   = contest[10]
            city_f   = contest[11]
            user_f   = contest[12]
            prod_f   = contest[13]
            cat_f    = contest[14]
            reward_mode = contest[22] if len(contest) > 22 else 'total'
            ind_tgt_raw = contest[23] if len(contest) > 23 else None

            shops      = _json.loads(shop_f)  if shop_f  else None
            cities     = _json.loads(city_f)  if city_f  else None
            users      = _json.loads(user_f)  if user_f  else None
            products   = _json.loads(prod_f)  if prod_f  else None
            categories = _json.loads(cat_f)   if cat_f   else None
            ind_targets = _json.loads(ind_tgt_raw) if ind_tgt_raw else {}

            where, metric_expr, params = self._build_contest_sale_query(
                start, end, shops, cities, users, products, categories, metric
            )

            conn = self.get_connection()
            cursor = conn.cursor()

            if reward_mode == 'per_sale':
                # ── Режим «бонус за каждую продажу» ──────────────────────────
                tier_bonuses = self.get_contest_product_bonuses(contest_id)
                if not tier_bonuses:
                    conn.close()
                    return []

                # Получаем продажи по товарам per user
                qty_metric = "SUM(COALESCE(s.quantity_sold, 0))"
                query_ps = f"""
                    SELECT u.id, u.first_name, u.last_name, u.telegram_id, u.shop_name,
                           s.product_id,
                           {qty_metric} AS qty,
                           u.username
                    FROM sales s
                    JOIN users u ON s.user_id = u.id
                    LEFT JOIN products p ON s.product_id = p.id
                    WHERE {where}
                    GROUP BY s.user_id, s.product_id
                    ORDER BY u.id
                """
                cursor.execute(query_ps, params)
                sale_rows = cursor.fetchall()
                conn.close()

                # Группируем продажи по user_id
                from collections import defaultdict
                user_info = {}
                user_product_qty = defaultdict(lambda: defaultdict(float))
                for uid, fn, ln, tg_id, sn, pid, qty, uname in sale_rows:
                    user_info[uid] = (fn or '', ln or '', tg_id, sn or '', uname)
                    user_product_qty[uid][pid] += (qty or 0)

                results = []
                for uid, (fn, ln, tg_id, sn, uname) in user_info.items():
                    plan_pct = self.get_user_plan_pct_for_contest(tg_id) if tg_id else 0.0
                    total_bonus = 0.0
                    # Для каждого товара найти максимальный применимый тир
                    seen_products = set()
                    for pid, qty in user_product_qty[uid].items():
                        best_bonus = 0.0
                        for b in tier_bonuses:
                            if b['product_id'] is not None and b['product_id'] != pid:
                                continue
                            if b['min_plan_pct'] <= plan_pct:
                                best_bonus = max(best_bonus, b['bonus_per_unit'])
                            seen_products.add(pid)
                        total_bonus += best_bonus * qty

                    # Flat-бонус (product_id=None — для any/category конкурсов)
                    flat_bonuses = [b for b in tier_bonuses if b['product_id'] is None]
                    if flat_bonuses and not seen_products:
                        total_qty = sum(user_product_qty[uid].values())
                        best_flat = 0.0
                        for b in flat_bonuses:
                            if b['min_plan_pct'] <= plan_pct:
                                best_flat = max(best_flat, b['bonus_per_unit'])
                        total_bonus += best_flat * total_qty

                    results.append({
                        'user_id': uid,
                        'first_name': fn,
                        'last_name': ln,
                        'telegram_id': tg_id,
                        'shop_name': sn,
                        'username': uname,
                        'actual': sum(user_product_qty[uid].values()),
                        'reward': round(total_bonus, 2),
                        'bonus_earned': round(total_bonus, 2),
                        'plan_pct': round(plan_pct, 1),
                        'is_winner': total_bonus > 0,
                    })
                results.sort(key=lambda r: r['reward'], reverse=True)
                return results

            else:
                # ── Режим «итоговый приз» (total) ────────────────────────────
                query = f"""
                    SELECT u.id, u.first_name, u.last_name, u.telegram_id, u.shop_name,
                           {metric_expr} AS actual_value,
                           u.username
                    FROM sales s
                    JOIN users u ON s.user_id = u.id
                    LEFT JOIN products p ON s.product_id = p.id
                    WHERE {where}
                    GROUP BY s.user_id
                    ORDER BY actual_value DESC
                """
                cursor.execute(query, params)
                rows = cursor.fetchall()
                conn.close()

                # Словарь индивидуальных порогов по магазинам
                by_shop = ind_targets.get('by_shop', {}) if isinstance(ind_targets, dict) else {}

                # Ручные корректировки результатов по магазинам
                manual_results = self.get_contest_manual_results(contest_id)

                results = []
                for row in rows:
                    uid, fname, lname, tg_id, shop_name, actual, uname = row
                    actual = actual or 0.0
                    # Применяем ручную корректировку если задана для этого магазина
                    if shop_name and shop_name in manual_results:
                        actual = manual_results[shop_name]['value']
                    # Определяем порог: индивидуальный по магазину → глобальный
                    effective_target = by_shop.get(shop_name, target) if shop_name else target
                    effective_target = float(effective_target) if effective_target else float(target)
                    is_winner = actual >= effective_target
                    if rtype == 'fixed':
                        reward = rval if is_winner else 0.0
                    else:
                        reward = round(actual * rval / 100, 2) if is_winner else 0.0
                    results.append({
                        'user_id': uid,
                        'first_name': fname or '',
                        'last_name': lname or '',
                        'telegram_id': tg_id,
                        'shop_name': shop_name,
                        'username': uname,
                        'actual': actual,
                        'reward': reward,
                        'is_winner': is_winner,
                        'individual_target': effective_target,
                        'is_manual': bool(shop_name and shop_name in manual_results),
                    })
                return results
        except Exception as e:
            logger.error(f"Ошибка compute_contest_results: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def compute_contest_shop_auto_totals(self, contest_id) -> dict:
        """Авто-итоги по магазинам без ручных корректировок, с полными фильтрами конкурса.

        Возвращает {shop_name: auto_total} — те же условия WHERE что и compute_contest_results,
        но группировка по shop_name вместо user_id. Используется для отображения «авто» в UI корректировок.
        """
        try:
            import json as _json
            contest = self.get_contest(contest_id)
            if not contest:
                return {}

            metric   = contest[4]
            start    = contest[8]
            end      = contest[9]
            shop_f   = contest[10]
            city_f   = contest[11]
            user_f   = contest[12]
            prod_f   = contest[13]
            cat_f    = contest[14]

            shops      = _json.loads(shop_f)  if shop_f  else None
            cities     = _json.loads(city_f)  if city_f  else None
            users      = _json.loads(user_f)  if user_f  else None
            products   = _json.loads(prod_f)  if prod_f  else None
            categories = _json.loads(cat_f)   if cat_f   else None

            where, metric_expr, params = self._build_contest_sale_query(
                start, end, shops, cities, users, products, categories, metric
            )

            conn = self.get_connection()
            rows = conn.execute(
                f"SELECT s.shop_name, {metric_expr}"
                f" FROM sales s"
                f" JOIN users u ON s.user_id = u.id"
                f" LEFT JOIN products p ON s.product_id = p.id"
                f" WHERE {where}"
                f" GROUP BY s.shop_name",
                params
            ).fetchall()
            conn.close()
            return {r[0]: (r[1] or 0.0) for r in rows}
        except Exception as e:
            logger.error(f"Ошибка compute_contest_shop_auto_totals: {e}")
            if 'conn' in locals():
                conn.close()
            return {}

    def get_contests_for_period(self, start_date: str, end_date: str) -> list:
        """Конкурсы, активные или завершённые в заданном периоде (пересечение дат)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            self._ensure_contests_table(cursor)
            cursor.execute('''
                SELECT * FROM contests
                WHERE status IN ('finished', 'active')
                  AND start_date <= ? AND end_date >= ?
                ORDER BY start_date
            ''', (end_date, start_date))
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_contests_for_period: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def add_extra_condition(self, condition_type, shop_name=None, min_sellers=None,
                            coefficient=None, user_id=None, allowed_categories=None,
                            description=None, calc_mode='individual'):
        """Добавить доп. условие мотивации"""
        try:
            import json as _json
            conn = self.get_connection()
            cursor = conn.cursor()
            allowed_str = _json.dumps(allowed_categories, ensure_ascii=False) if allowed_categories else None
            cursor.execute('''
                INSERT INTO motivation_extra_conditions
                (condition_type, description, shop_name, min_sellers, coefficient, user_id, allowed_categories, calc_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (condition_type, description, shop_name, min_sellers, coefficient, user_id, allowed_str, calc_mode))
            new_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return new_id
        except Exception as e:
            logger.error(f"Ошибка при добавлении доп. условия: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def get_plan_motivation_coefficient(self, user_id, year, month, admin_user_id=None):
        """Рассчитать коэффициент мотивации на основе среднего выполнения недельных планов за месяц.

        Алгоритм:
        1. Берём все недели месяца (Пн–Вс), которые пересекаются с месяцем
        2. Для каждой недели находим план: личный (target_type=seller) в приоритете,
           иначе план магазина сотрудника (target_type=shop)
        3. Считаем фактическое выполнение за каждую неделю → % от плана
        4. Если cap=True — обрезаем % до 100
        5. Среднее арифметическое по неделям → коэффициент (0.0–1.0 или выше при cap=False)

        Возвращает (coefficient: float, details: list[dict]) где details — по одной записи на неделю.
        Если планов нет ни на одну неделю — возвращает (1.0, []).
        """
        import calendar as _cal
        from datetime import date as _date, timedelta as _td
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Настройки: cap и enabled проверяет вызывающий код
            # Получаем user_id (internal) → shop_name
            cursor.execute("SELECT id, shop_name FROM users WHERE id = ?", (user_id,))
            urow = cursor.fetchone()
            if not urow:
                conn.close()
                return 1.0, []
            user_shop = urow[1]

            # Все активные планы (seller на этого пользователя + shop его магазина)
            cursor.execute("""
                SELECT id, plan_type, metric_type, target_value, target_type,
                       user_id, shop_name, filter_type, filter_value, is_active, created_by, created_at
                FROM sales_plans
                WHERE is_active = 1 AND plan_type = 'weekly'
                  AND (
                    (target_type = 'seller' AND user_id = ?)
                    OR (target_type = 'shop' AND shop_name = ? AND ? IS NOT NULL)
                  )
            """, (user_id, user_shop, user_shop))
            plans = cursor.fetchall()
            conn.close()

            if not plans:
                return 1.0, []

            # Разбиваем на личные и магазинные
            seller_plans = [p for p in plans if p[4] == 'seller']
            shop_plans   = [p for p in plans if p[4] == 'shop']

            # Генерируем недели месяца (Пн–Вс), пересекающиеся с месяцем
            import json as _json
            first_day = _date(year, month, 1)
            last_day  = _date(year, month, _cal.monthrange(year, month)[1])

            # Первый понедельник недели, содержащей первый день месяца
            week_start = first_day - _td(days=first_day.weekday())
            weeks = []
            while week_start <= last_day:
                week_end = week_start + _td(days=6)
                # Обрезаем по границам месяца для подсчёта факта
                actual_start = max(week_start, first_day)
                actual_end   = min(week_end, last_day)
                weeks.append((week_start, week_end, actual_start, actual_end))
                week_start += _td(days=7)

            if not weeks:
                return 1.0, []

            def _calc_actual_for_plan(plan, start_str, end_str):
                """Считает факт для плана за произвольный период."""
                _, _, metric_type, target_value, target_type, p_user_id, p_shop, filter_type, filter_value, *_ = plan
                metric_expr = 'COALESCE(SUM(s.sale_price * s.quantity_sold), 0)' if metric_type == 'turnover' else 'COALESCE(SUM(s.quantity_sold), 0)'
                conditions = ["date(s.sale_date) BETWEEN date(?) AND date(?)"]
                params = [start_str, end_str]
                if target_type == 'seller':
                    conditions.append("s.user_id = ?"); params.append(p_user_id)
                elif target_type == 'shop' and p_shop:
                    conditions.append("s.shop_name = ?"); params.append(p_shop)
                join_clause = ""
                if filter_type == 'category' and filter_value:
                    join_clause = "JOIN products p ON s.product_id = p.id"
                    try:
                        cats = _json.loads(filter_value)
                        if isinstance(cats, list) and cats:
                            placeholders = ','.join('?' * len(cats))
                            conditions.append(f"p.category IN ({placeholders})")
                            params.extend(cats)
                        else:
                            conditions.append("p.category = ?"); params.append(filter_value)
                    except Exception:
                        conditions.append("p.category = ?"); params.append(filter_value)
                elif filter_type == 'product' and filter_value:
                    try:
                        ids = _json.loads(filter_value)
                        if ids:
                            placeholders = ','.join('?' * len(ids))
                            conditions.append(f"s.product_id IN ({placeholders})")
                            params.extend(ids)
                    except Exception as _exc:
                        logger.debug("_calc_actual_for_plan: подавлено исключение: %s", _exc)
                where = ' AND '.join(conditions)
                q = f"SELECT {metric_expr} FROM sales s {join_clause} WHERE {where}"
                try:
                    c2 = self.get_connection()
                    cur2 = c2.cursor()
                    cur2.execute(q, params)
                    res = cur2.fetchone()[0] or 0.0
                    c2.close()
                    return float(res)
                except Exception:
                    return 0.0

            details = []
            pct_sum = 0.0
            week_count = 0

            for week_start, week_end, actual_start, actual_end in weeks:
                start_str = actual_start.strftime('%Y-%m-%d')
                end_str   = actual_end.strftime('%Y-%m-%d')

                # Выбираем план: личный в приоритете
                plan = seller_plans[0] if seller_plans else (shop_plans[0] if shop_plans else None)
                if not plan:
                    continue

                target_value = float(plan[3])
                if target_value <= 0:
                    continue

                actual = _calc_actual_for_plan(plan, start_str, end_str)
                pct = (actual / target_value * 100) if target_value > 0 else 0.0

                details.append({
                    'week': f"{actual_start.strftime('%d.%m')}–{actual_end.strftime('%d.%m')}",
                    'plan': target_value,
                    'actual': actual,
                    'pct': round(pct, 1),
                    'plan_type': plan[4],  # seller/shop
                })
                pct_sum += pct
                week_count += 1

            if week_count == 0:
                return 1.0, []

            avg_pct = pct_sum / week_count
            coefficient = round(avg_pct / 100, 4)
            return coefficient, details

        except Exception as e:
            logger.error(f"Ошибка get_plan_motivation_coefficient: {e}")
            return 1.0, []

    def get_plan_motivation_coefficient_for_user(self, user_id, year, month):
        """Обёртка: возвращает только коэффициент (float), учитывая настройку cap.
        Читает plan_coeff_cap из notification_settings пользователя."""
        try:
            ns = self.get_notification_settings(user_id)
            cap = ns.get('plan_coeff_cap', True)
            coeff, _ = self.get_plan_motivation_coefficient(user_id, year, month)
            if cap:
                coeff = min(coeff, 1.0)
            return coeff
        except Exception:
            return 1.0

    def get_joint_bonus_adjustment(self, user_id, start_date=None, end_date=None):
        """Расчёт корректировки заработка для совместного режима мотивации.

        Для магазинов с calc_mode='joint':
          base_pool    = SUM(commission_amount всех продавцов в магазине за период)
                         (комиссии записаны БЕЗ коэффициента, т.к. apply_extra_conditions
                          пропускает его для joint-условий)
          joint_total  = base_pool × coefficient   (если sellers_count >= min_sellers)
          adjustment   = joint_total - user_individual

        Пример: Андрей продал 2 шт → +20₽ в пул, Ольга 1 шт → +10₽ в пул.
        Пул = 30₽, коэф. 0.7 → joint_total = 21₽. Каждый получает +21₽.

        Если совместных условий нет или min_sellers не достигнут — возвращает 0.0.
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Извлекаем год/месяц из start_date для поиска месячных условий
            _period_year = None
            _period_month = None
            if start_date:
                import re as _re
                _m = _re.match(r'(\d{4})-(\d{2})', start_date)
                if _m:
                    _period_year, _period_month = int(_m.group(1)), int(_m.group(2))

            # Активные joint-условия из глобальной таблицы
            cursor.execute("""
                SELECT shop_name, min_sellers, coefficient
                FROM motivation_extra_conditions
                WHERE condition_type = 'multi_seller_coeff' AND calc_mode = 'joint' AND is_active = 1
            """)
            joint_conditions = cursor.fetchall()

            # Строим словарь shop_name → (min_sellers, coefficient)
            # None = условие применяется ко всем магазинам
            shop_to_cond = {}
            global_cond  = None
            for sn, min_s, coeff in joint_conditions:
                if sn is None:
                    global_cond = (min_s, coeff)
                else:
                    shop_to_cond[sn] = (min_s, coeff)

            # Перекрываем глобальные условия месячными (extra_conditions_schedule),
            # если период известен — месячные имеют приоритет над глобальными
            if _period_year is not None and _period_month is not None:
                cursor.execute("""
                    SELECT shop_name, min_sellers, coefficient
                    FROM extra_conditions_schedule
                    WHERE condition_type = 'multi_seller_coeff' AND calc_mode = 'joint'
                      AND is_active = 1 AND year = ? AND month = ?
                """, (_period_year, _period_month))
                monthly_conditions = cursor.fetchall()
                for sn, min_s, coeff in monthly_conditions:
                    if sn is None:
                        global_cond = (min_s, coeff)
                    else:
                        shop_to_cond[sn] = (min_s, coeff)

            if not shop_to_cond and global_cond is None:
                conn.close()
                return 0.0

            # Магазины, где этот пользователь продавал за период
            date_filter = ""
            params_user = [user_id]
            if start_date:
                date_filter += " AND sale_date >= ?"
                params_user.append(start_date)
            if end_date:
                date_filter += " AND sale_date <= ?"
                params_user.append(end_date)

            cursor.execute(f"""
                SELECT DISTINCT shop_name FROM sales
                WHERE user_id = ? {date_filter}
            """, params_user)
            user_shops = {r[0] for r in cursor.fetchall()}

            # Оставляем только магазины с joint-условием
            active_joint_shops = {}  # shop → (min_sellers, coefficient)
            for sn in user_shops:
                if sn in shop_to_cond:
                    active_joint_shops[sn] = shop_to_cond[sn]
                elif global_cond is not None:
                    active_joint_shops[sn] = global_cond

            if not active_joint_shops:
                conn.close()
                return 0.0

            total_adjustment = 0.0
            for shop, (min_sellers, coefficient) in active_joint_shops.items():
                date_clause = ""
                params_pool = []
                if start_date:
                    date_clause += " AND s.sale_date >= ?"
                    params_pool.append(start_date)
                if end_date:
                    date_clause += " AND s.sale_date <= ?"
                    params_pool.append(end_date)

                # Количество уникальных продавцов в магазине за период
                cursor.execute(f"""
                    SELECT COUNT(DISTINCT s.user_id)
                    FROM sales s
                    WHERE s.shop_name = ? {date_clause}
                """, [shop] + params_pool)
                sellers_count = cursor.fetchone()[0] or 0

                # Если порог продавцов не достигнут — joint не применяется
                if sellers_count < min_sellers:
                    continue

                # Базовый пул = сумма комиссий всех продавцов (без коэффициента,
                # т.к. apply_extra_conditions не применял его для joint)
                cursor.execute(f"""
                    SELECT COALESCE(SUM(se.commission_amount), 0.0)
                    FROM seller_earnings se
                    JOIN sales s ON se.sale_id = s.id
                    WHERE s.shop_name = ? {date_clause}
                """, [shop] + params_pool)
                base_pool = cursor.fetchone()[0] or 0.0

                # Каждый продавец получает: пул × коэффициент
                joint_total = round(base_pool * coefficient, 2)

                # Личный вклад пользователя (уже учтён в его seller_earnings)
                cursor.execute(f"""
                    SELECT COALESCE(SUM(se.commission_amount), 0.0)
                    FROM seller_earnings se
                    JOIN sales s ON se.sale_id = s.id
                    WHERE se.user_id = ? AND s.shop_name = ? {date_clause}
                """, [user_id, shop] + params_pool)
                user_individual = cursor.fetchone()[0] or 0.0

                total_adjustment += joint_total - user_individual

            conn.close()
            return round(total_adjustment, 2)
        except Exception as e:
            logger.error(f"Ошибка get_joint_bonus_adjustment: {e}")
            if 'conn' in locals():
                conn.close()
            return 0.0

    def get_extra_conditions(self, active_only=True):
        """Получить все доп. условия мотивации"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            where = "WHERE mec.is_active = 1" if active_only else ""
            cursor.execute(f'''
                SELECT mec.id, mec.condition_type, mec.description,
                       mec.shop_name, mec.min_sellers, mec.coefficient,
                       mec.user_id, mec.allowed_categories,
                       mec.is_active, mec.created_at,
                       u.first_name, u.last_name,
                       COALESCE(mec.calc_mode, 'individual') as calc_mode
                FROM motivation_extra_conditions mec
                LEFT JOIN users u ON mec.user_id = u.id
                {where}
                ORDER BY mec.created_at DESC
            ''')
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении доп. условий: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def delete_extra_condition(self, condition_id):
        """Удалить доп. условие мотивации"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM motivation_extra_conditions WHERE id = ?', (condition_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Ошибка при удалении доп. условия: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    # ──────────────────────────────────────────────
    # ПЛАНЫ ПРОДАЖ
    # ──────────────────────────────────────────────

    def add_sales_plan(self, plan_type, metric_type, target_value, target_type,
                       user_id=None, shop_name=None, filter_type='all',
                       filter_value=None, created_by=None):
        """Добавить план продаж"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sales_plans
                (plan_type, metric_type, target_value, target_type,
                 user_id, shop_name, filter_type, filter_value, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (plan_type, metric_type, target_value, target_type,
                  user_id, shop_name, filter_type, filter_value, created_by))
            new_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return new_id
        except Exception as e:
            logger.error(f"Ошибка при добавлении плана продаж: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def get_sales_plans(self, active_only=True):
        """Получить все планы продаж с именами продавцов"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            where = "WHERE sp.is_active = 1" if active_only else ""
            cursor.execute(f'''
                SELECT sp.id, sp.plan_type, sp.metric_type, sp.target_value,
                       sp.target_type, sp.user_id, sp.shop_name,
                       sp.filter_type, sp.filter_value,
                       sp.is_active, sp.created_by, sp.created_at,
                       u.first_name, u.last_name
                FROM sales_plans sp
                LEFT JOIN users u ON sp.user_id = u.id
                {where}
                ORDER BY sp.created_at DESC
            ''')
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении планов продаж: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def delete_sales_plan(self, plan_id):
        """Удалить план продаж"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sales_plans WHERE id = ?', (plan_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Ошибка при удалении плана продаж: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def update_sales_plan(self, plan_id, **kwargs):
        """Обновить поля плана продаж. Разрешённые поля: target_value, plan_type, metric_type и др."""
        allowed = {'target_value', 'plan_type', 'metric_type', 'target_type',
                   'user_id', 'shop_name', 'filter_type', 'filter_value', 'is_active'}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            set_clause = ', '.join(f"{k} = ?" for k in fields)
            cursor.execute(
                f"UPDATE sales_plans SET {set_clause} WHERE id = ?",
                list(fields.values()) + [plan_id]
            )
            conn.commit()
            updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Ошибка update_sales_plan: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def calculate_plan_actual(self, plan_row, local_today=None):
        """Рассчитать фактическое выполнение плана за текущий период.
        local_today — date объект в timezone пользователя; если None, используется datetime.now().date() (UTC на Amvera)."""
        try:
            import json as _json
            from datetime import date as _date
            (plan_id, plan_type, metric_type, target_value,
             target_type, user_id, shop_name,
             filter_type, filter_value, *_rest) = plan_row

            today = local_today if isinstance(local_today, _date) else datetime.now().date()
            if plan_type == 'weekly':
                start_date = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
            else:
                start_date = today.replace(day=1).strftime('%Y-%m-%d')

            if metric_type == 'turnover':
                metric_expr = 'COALESCE(SUM(s.sale_price * s.quantity_sold), 0)'
            else:
                metric_expr = 'COALESCE(SUM(s.quantity_sold), 0)'

            conditions = ["date(s.sale_date) >= date(?)"]
            params = [start_date]

            if target_type == 'seller' and user_id:
                conditions.append("s.user_id = ?")
                params.append(user_id)
            elif target_type == 'shop' and shop_name:
                conditions.append("s.shop_name = ?")
                params.append(shop_name)

            join_clause = ""
            if filter_type == 'category' and filter_value:
                join_clause = "JOIN products p ON s.product_id = p.id"
                try:
                    _cats = _json.loads(filter_value)
                    if isinstance(_cats, list) and _cats:
                        placeholders = ','.join('?' * len(_cats))
                        conditions.append(f"p.category IN ({placeholders})")
                        params.extend(_cats)
                    else:
                        conditions.append("p.category = ?")
                        params.append(filter_value)
                except (ValueError, TypeError):
                    conditions.append("p.category = ?")
                    params.append(filter_value)
            elif filter_type == 'product' and filter_value:
                try:
                    ids = _json.loads(filter_value)
                    if ids:
                        placeholders = ','.join('?' * len(ids))
                        conditions.append(f"s.product_id IN ({placeholders})")
                        params.extend(ids)
                except Exception as _exc:
                    logger.debug("calculate_plan_actual: подавлено исключение: %s", _exc)

            where = ' AND '.join(conditions)
            query = f"SELECT {metric_expr} FROM sales s {join_clause} WHERE {where}"

            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(query, params)
            result = cursor.fetchone()[0] or 0.0
            conn.close()
            return float(result)
        except Exception as e:
            logger.error(f"Ошибка при расчёте плана: {e}")
            if 'conn' in locals():
                conn.close()
            return 0.0

    def get_plans_progress(self, local_today=None):
        """Получить все планы с прогрессом выполнения.
        local_today — date в timezone пользователя для корректного расчёта периода."""
        plans = self.get_sales_plans(active_only=True)
        result = []
        for plan in plans:
            actual = self.calculate_plan_actual(plan, local_today)
            target = plan[3]
            percent = round((actual / target * 100) if target > 0 else 0.0, 1)
            result.append((plan, actual, percent))
        return result

    def get_sales_plan_by_id(self, plan_id):
        """Получить один план продаж по id → dict или None."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT sp.id, sp.plan_type, sp.metric_type, sp.target_value,
                          sp.target_type, sp.user_id, sp.shop_name,
                          sp.filter_type, sp.filter_value, sp.is_active,
                          sp.created_by, sp.created_at,
                          u.first_name, u.last_name
                   FROM sales_plans sp
                   LEFT JOIN users u ON sp.user_id = u.id
                   WHERE sp.id = ?''',
                (plan_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if not row:
                return None
            fname = (row[12] or "").strip()
            lname = (row[13] or "").strip()
            who = f"{fname} {lname}".strip() or (row[6] or "")
            return {
                "id": row[0], "plan_type": row[1], "metric_type": row[2],
                "target_value": float(row[3] or 0), "target_type": row[4],
                "user_id": row[5], "shop_name": row[6],
                "filter_type": row[7], "filter_value": row[8],
                "is_active": bool(row[9]), "created_at": row[11] or "",
                "target_who": who,
            }
        except Exception as e:
            logger.error(f"get_sales_plan_by_id error: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def get_daily_sales_for_period(self, date_from: str, date_to: str,
                                   shop_name: str = None, user_id: int = None,
                                   metric_type: str = 'turnover',
                                   filter_type: str = None, filter_value: str = None):
        """Продажи по дням за период → [{date, amount, count}].
        metric_type='turnover' → выручка (₽), 'quantity' → количество (шт).
        filter_type/filter_value — фильтр по категории или товарам (как в sales_plans)."""
        try:
            import json as _json
            conn = self.get_connection()
            cursor = conn.cursor()
            metric_expr = (
                'COALESCE(SUM(s.sale_price * s.quantity_sold), 0)'
                if metric_type == 'turnover'
                else 'COALESCE(SUM(s.quantity_sold), 0)'
            )
            conditions = ["date(s.sale_date) BETWEEN date(?) AND date(?)"]
            params: list = [date_from, date_to]
            join_clause = ""
            if shop_name:
                conditions.append("s.shop_name = ?")
                params.append(shop_name)
            if user_id:
                conditions.append("s.user_id = ?")
                params.append(user_id)
            if filter_type == 'category' and filter_value:
                join_clause = "JOIN products p ON s.product_id = p.id"
                try:
                    cats = _json.loads(filter_value)
                    if isinstance(cats, list) and cats:
                        conditions.append(f"p.category IN ({','.join('?'*len(cats))})")
                        params.extend(cats)
                    else:
                        conditions.append("p.category = ?")
                        params.append(filter_value)
                except (ValueError, TypeError):
                    conditions.append("p.category = ?")
                    params.append(filter_value)
            elif filter_type == 'product' and filter_value:
                try:
                    ids = _json.loads(filter_value)
                    if ids:
                        conditions.append(f"s.product_id IN ({','.join('?'*len(ids))})")
                        params.extend(ids)
                except Exception:
                    pass
            where = " AND ".join(conditions)
            cursor.execute(
                f'''SELECT date(s.sale_date) AS day,
                           {metric_expr} AS amount,
                           COUNT(*) AS cnt
                    FROM sales s {join_clause}
                    WHERE {where}
                    GROUP BY day
                    ORDER BY day''',
                params
            )
            rows = cursor.fetchall()
            conn.close()
            return [{"date": r[0], "amount": float(r[1]), "count": int(r[2])} for r in rows]
        except Exception as e:
            logger.error(f"get_daily_sales_for_period error: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_sales_by_seller_for_period(self, date_from: str, date_to: str,
                                       shop_name: str = None,
                                       metric_type: str = 'turnover',
                                       filter_type: str = None, filter_value: str = None):
        """Продажи по продавцам за период → [{name, amount}] топ-10.
        metric_type и filter_type/filter_value — аналогично get_daily_sales_for_period."""
        try:
            import json as _json
            conn = self.get_connection()
            cursor = conn.cursor()
            metric_expr = (
                'COALESCE(SUM(s.sale_price * s.quantity_sold), 0)'
                if metric_type == 'turnover'
                else 'COALESCE(SUM(s.quantity_sold), 0)'
            )
            conditions = ["date(s.sale_date) BETWEEN date(?) AND date(?)"]
            params: list = [date_from, date_to]
            join_clause = "LEFT JOIN users u ON s.user_id = u.id"
            if shop_name:
                conditions.append("s.shop_name = ?")
                params.append(shop_name)
            if filter_type == 'category' and filter_value:
                join_clause += " JOIN products p ON s.product_id = p.id"
                try:
                    cats = _json.loads(filter_value)
                    if isinstance(cats, list) and cats:
                        conditions.append(f"p.category IN ({','.join('?'*len(cats))})")
                        params.extend(cats)
                    else:
                        conditions.append("p.category = ?")
                        params.append(filter_value)
                except (ValueError, TypeError):
                    conditions.append("p.category = ?")
                    params.append(filter_value)
            elif filter_type == 'product' and filter_value:
                try:
                    ids = _json.loads(filter_value)
                    if ids:
                        conditions.append(f"s.product_id IN ({','.join('?'*len(ids))})")
                        params.extend(ids)
                except Exception:
                    pass
            where = " AND ".join(conditions)
            cursor.execute(
                f'''SELECT COALESCE(u.first_name || ' ' || COALESCE(u.last_name,''), 'user#' || s.user_id) AS name,
                           {metric_expr} AS amount
                    FROM sales s {join_clause}
                    WHERE {where}
                    GROUP BY s.user_id
                    ORDER BY amount DESC
                    LIMIT 10''',
                params
            )
            rows = cursor.fetchall()
            conn.close()
            return [{"name": (r[0] or "").strip(), "amount": float(r[1])} for r in rows]
        except Exception as e:
            logger.error(f"get_sales_by_seller_for_period error: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_sales_by_category_for_period(self, date_from: str, date_to: str,
                                         shop_name: str = None, user_id: int = None,
                                         filter_type: str = None, filter_value: str = None):
        """Продажи по категориям за период → [{category, revenue, quantity}].
        Всегда возвращает и выручку и количество для контекста AI.
        filter_type/filter_value ограничивают набор строк (category/product)."""
        try:
            import json as _json
            conn = self.get_connection()
            cursor = conn.cursor()
            conditions = ["date(s.sale_date) BETWEEN date(?) AND date(?)"]
            params: list = [date_from, date_to]
            join_clause = "LEFT JOIN products p ON s.product_id = p.id"
            if shop_name:
                conditions.append("s.shop_name = ?")
                params.append(shop_name)
            if user_id:
                conditions.append("s.user_id = ?")
                params.append(user_id)
            if filter_type == 'category' and filter_value:
                try:
                    cats = _json.loads(filter_value)
                    if isinstance(cats, list) and cats:
                        conditions.append(f"p.category IN ({','.join('?'*len(cats))})")
                        params.extend(cats)
                    else:
                        conditions.append("p.category = ?")
                        params.append(filter_value)
                except (ValueError, TypeError):
                    conditions.append("p.category = ?")
                    params.append(filter_value)
            elif filter_type == 'product' and filter_value:
                try:
                    ids = _json.loads(filter_value)
                    if ids:
                        conditions.append(f"s.product_id IN ({','.join('?'*len(ids))})")
                        params.extend(ids)
                except Exception:
                    pass
            where = " AND ".join(conditions)
            cursor.execute(
                f'''SELECT COALESCE(p.category, 'Без категории') AS category,
                           COALESCE(SUM(s.sale_price * s.quantity_sold), 0) AS revenue,
                           COALESCE(SUM(s.quantity_sold), 0) AS quantity
                    FROM sales s {join_clause}
                    WHERE {where}
                    GROUP BY category
                    ORDER BY revenue DESC''',
                params
            )
            rows = cursor.fetchall()
            conn.close()
            return [{"category": r[0], "revenue": float(r[1]), "quantity": float(r[2])} for r in rows]
        except Exception as e:
            logger.error(f"get_sales_by_category_for_period error: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_user_plans_progress(self, telegram_id, local_today=None):
        """Получить планы конкретного продавца по telegram_id с прогрессом"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT id, shop_name FROM users WHERE telegram_id = ?', (telegram_id,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                return []
            internal_id, user_shop = row

            plans = self.get_sales_plans(active_only=True)
            result = []
            for plan in plans:
                target_type = plan[4]
                plan_user_id = plan[5]
                plan_shop = plan[6]
                if target_type == 'seller' and plan_user_id == internal_id:
                    actual = self.calculate_plan_actual(plan, local_today)
                    target = plan[3]
                    percent = round((actual / target * 100) if target > 0 else 0.0, 1)
                    result.append((plan, actual, percent))
                elif target_type == 'shop' and plan_shop and user_shop and plan_shop == user_shop:
                    actual = self.calculate_plan_actual(plan, local_today)
                    target = plan[3]
                    percent = round((actual / target * 100) if target > 0 else 0.0, 1)
                    result.append((plan, actual, percent))
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении планов пользователя: {e}")
            return []

    def get_user_contest_rewards(self, telegram_id: int, month_start: str, month_end: str) -> float:
        """Суммарные призы/бонусы конкурсов для пользователя за период (по telegram_id).
        Учитываются только конкурсы, чей период пересекается с [month_start, month_end].
        Поддерживает оба режима: 'total' (итоговый приз) и 'per_sale' (бонус за продажу)."""
        try:
            period_contests = self.get_contests_for_period(month_start, month_end)
            total = 0.0
            for c in period_contests:
                reward_mode = c[22] if len(c) > 22 else 'total'
                results = self.compute_contest_results(c[0])
                for r in results:
                    if r.get('telegram_id') == telegram_id:
                        if reward_mode == 'per_sale':
                            total += r.get('bonus_earned', 0.0)
                        elif r.get('is_winner'):
                            total += r.get('reward', 0.0)
            return round(total, 2)
        except Exception as e:
            logger.error(f"Ошибка get_user_contest_rewards: {e}")
            return 0.0

    def get_user_contest_rewards_detail(self, telegram_id: int,
                                        month_start: str, month_end: str) -> tuple:
        """Призы конкурсов для пользователя за период с детализацией по конкурсам.
        Возвращает (total: float, details: list[dict{title, reward, contest_id}])."""
        try:
            contests = self.get_contests_for_period(month_start, month_end)
            total = 0.0
            details = []
            for c in contests:
                reward_mode = c[22] if len(c) > 22 else 'total'
                results = self.compute_contest_results(c[0])
                for r in results:
                    if r.get('telegram_id') != telegram_id:
                        continue
                    if reward_mode == 'per_sale':
                        reward = float(r.get('bonus_earned', 0.0) or 0)
                    elif r.get('is_winner'):
                        reward = float(r.get('reward', 0.0) or 0)
                    else:
                        reward = 0.0
                    if reward > 0:
                        total += reward
                        details.append({
                            'contest_id': c[0],
                            'title': c[1] or '—',
                            'reward': round(reward, 2),
                        })
                    break
            return round(total, 2), details
        except Exception as e:
            logger.error(f"get_user_contest_rewards_detail: {e}")
            return 0.0, []

    def get_bulk_contest_rewards_by_telegram(self, month_start: str, month_end: str) -> dict:
        """Призы конкурсов за период для ВСЕХ пользователей разом.
        Результаты каждого конкурса вычисляются один раз, затем разбиваются по telegram_id.
        Возвращает {telegram_id: total_reward: float}."""
        try:
            contests = self.get_contests_for_period(month_start, month_end)
            result: dict = {}
            for c in contests:
                reward_mode = c[22] if len(c) > 22 else 'total'
                results = self.compute_contest_results(c[0])
                for r in results:
                    tid = r.get('telegram_id')
                    if not tid:
                        continue
                    if reward_mode == 'per_sale':
                        reward = float(r.get('bonus_earned', 0.0) or 0)
                    elif r.get('is_winner'):
                        reward = float(r.get('reward', 0.0) or 0)
                    else:
                        reward = 0.0
                    if reward > 0:
                        result[tid] = round(result.get(tid, 0.0) + reward, 2)
            return result
        except Exception as e:
            logger.error(f"get_bulk_contest_rewards_by_telegram: {e}")
            return {}

    def calculate_seller_commission(self, sale_id, product_id, sale_price, quantity_sold,
                                    user_id=None, shop_name=None,
                                    sale_year=None, sale_month=None, seller_attrs=None):
        """Расчет мотивации продавца за продажу.
        Выбирает наиболее специфичное правило (user>shop>city>network>global) через seller_attrs.
        Если sale_year/sale_month переданы — учитывает месячное расписание (с fallback на правила)."""
        if seller_attrs is None and user_id is not None:
            seller_attrs = self._get_seller_attrs(user_id, shop_name)
        if sale_year is not None and sale_month is not None:
            commission_info = self.get_motivation_for_month(product_id, sale_year, sale_month,
                                                            seller_attrs=seller_attrs)
        else:
            commission_info = self.get_product_motivation(product_id, seller_attrs=seller_attrs)

        if not commission_info:
            return 0.0

        motivation_type = commission_info['motivation_type']
        motivation_value = commission_info['motivation_value']

        if motivation_type == 'percentage':
            total_sale_amount = sale_price * quantity_sold
            commission_amount = total_sale_amount * (motivation_value / 100)
        else:  # fixed
            commission_amount = motivation_value * quantity_sold

        if user_id is not None and shop_name is not None:
            commission_amount = self.apply_extra_conditions(
                commission_amount, user_id, product_id, shop_name,
                year=sale_year, month=sale_month
            )

        return round(commission_amount, 2)

    def add_seller_earning(self, sale_id, user_id, product_id, commission_amount, motivation_type,
                           motivation_value, motivation_source='global'):
        """Добавление заработка продавца"""
        import time

        max_retries = 3
        retry_delay = 0.1

        for attempt in range(max_retries):
            try:
                conn = self.get_connection()
                cursor = conn.cursor()

                # Убеждаемся что все параметры не None
                motivation_type = motivation_type if motivation_type is not None else 'percentage'
                motivation_value = motivation_value if motivation_value is not None else 0.0
                commission_amount = commission_amount if commission_amount is not None else 0.0
                motivation_source = motivation_source if motivation_source is not None else 'global'


                cursor.execute('''
                    INSERT INTO seller_earnings 
                    (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value, motivation_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value, motivation_source))

                conn.commit()
                conn.close()
                return True

            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    if 'conn' in locals():
                        conn.close()
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                else:
                    logger.error(f"Ошибка при добавлении заработка: {e}")
                    if 'conn' in locals():
                        conn.close()
                    return False
            except Exception as e:
                logger.error(f"Ошибка при добавлении заработка: {e}")
                if 'conn' in locals():
                    conn.close()
                return False

        return False

    def get_seller_earnings(self, user_id, start_date=None, end_date=None):
        """Получение заработка продавца за период"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Проверяем, есть ли записи в таблице заработков
            cursor.execute('SELECT COUNT(*) FROM seller_earnings WHERE user_id = ?', (user_id,))
            count = cursor.fetchone()[0]

            # Также проверяем, есть ли продажи этого пользователя
            cursor.execute('SELECT COUNT(*) FROM sales WHERE user_id = ?', (user_id,))
            sales_count = cursor.fetchone()[0]

            if count == 0 and sales_count == 0:
                conn.close()
                return []

            if count > 0:
                # Если есть записи в seller_earnings, используем их
                query = '''
                    SELECT se.commission_amount, 
                           COALESCE(se.motivation_type, 'percentage') as motivation_type,
                           COALESCE(se.motivation_value, 0) as motivation_value,
                           p.name as product_name, s.quantity_sold, s.sale_price,
                           s.sale_date, s.shop_name,
                           COALESCE(se.motivation_source, 'global') as motivation_source
                    FROM seller_earnings se
                    JOIN sales s ON se.sale_id = s.id
                    JOIN products p ON se.product_id = p.id
                    WHERE se.user_id = ?
                '''
                params = [user_id]

                if start_date:
                    query += ' AND s.sale_date >= ?'
                    params.append(start_date)

                if end_date:
                    query += ' AND s.sale_date <= ?'
                    params.append(end_date)

                query += ' ORDER BY s.sale_date DESC'

                cursor.execute(query, params)
                result = cursor.fetchall()
            else:
                # Если нет записей в seller_earnings, но есть продажи, показываем продажи с нулевой комиссией
                query = '''
                    SELECT 0.0 as commission_amount,
                           'percentage' as motivation_type,
                           0.0 as motivation_value,
                           p.name as product_name, s.quantity_sold, s.sale_price,
                           s.sale_date, s.shop_name,
                           'global' as motivation_source
                    FROM sales s
                    JOIN products p ON s.product_id = p.id
                    WHERE s.user_id = ?
                '''
                params = [user_id]

                if start_date:
                    query += ' AND s.sale_date >= ?'
                    params.append(start_date)

                if end_date:
                    query += ' AND s.sale_date <= ?'
                    params.append(end_date)

                query += ' ORDER BY s.sale_date DESC'

                cursor.execute(query, params)
                result = cursor.fetchall()

            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении заработка продавца: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_seller_total_earnings(self, user_id, start_date=None, end_date=None):
        """Получение общего заработка продавца за период"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()


            # Проверяем, есть ли записи в таблице заработков
            cursor.execute('SELECT COUNT(*) FROM seller_earnings WHERE user_id = ?', (user_id,))
            count = cursor.fetchone()[0]

            # Проверяем продажи
            cursor.execute('SELECT COUNT(*) FROM sales WHERE user_id = ?', (user_id,))
            sales_count = cursor.fetchone()[0]

            if count == 0 and sales_count == 0:
                conn.close()
                return {'total_earnings': 0.0, 'total_sales': 0}

            if count > 0:
                # Если есть записи в seller_earnings
                query = '''
                    SELECT SUM(se.commission_amount) as total_earnings,
                           COUNT(se.id) as total_sales
                    FROM seller_earnings se
                    JOIN sales s ON se.sale_id = s.id
                    WHERE se.user_id = ?
                '''
                params = [user_id]

                if start_date:
                    query += ' AND s.sale_date >= ?'
                    params.append(start_date)

                if end_date:
                    query += ' AND s.sale_date <= ?'
                    params.append(end_date)

                cursor.execute(query, params)
                result = cursor.fetchone()
            else:
                # Если нет записей в seller_earnings, но есть продажи
                query = '''
                    SELECT 0.0 as total_earnings,
                           COUNT(s.id) as total_sales
                    FROM sales s
                    WHERE s.user_id = ?
                '''
                params = [user_id]

                if start_date:
                    query += ' AND s.sale_date >= ?'
                    params.append(start_date)

                if end_date:
                    query += ' AND s.sale_date <= ?'
                    params.append(end_date)

                cursor.execute(query, params)
                result = cursor.fetchone()

            conn.close()

            if result and result[1] is not None:
                base_earnings = round(result[0] if result[0] else 0.0, 2)
            else:
                base_earnings = 0.0
                result = (0.0, 0)

            # Добавляем корректировку совместного режима мотивации
            joint_adj = self.get_joint_bonus_adjustment(user_id, start_date, end_date)
            total = round(base_earnings + joint_adj, 2)

            # Коэффициент выполнения недельных планов (если включён для пользователя)
            plan_coeff = 1.0
            plan_coeff_applied = False
            try:
                ns = self.get_notification_settings(user_id)
                if ns.get('plan_coeff_enabled'):
                    if start_date:
                        import re as _re
                        m = _re.match(r'(\d{4})-(\d{2})', start_date)
                        if m:
                            _y, _mo = int(m.group(1)), int(m.group(2))
                            raw_coeff, _ = self.get_plan_motivation_coefficient(user_id, _y, _mo)
                            if ns.get('plan_coeff_cap', True):
                                raw_coeff = min(raw_coeff, 1.0)
                            plan_coeff = raw_coeff
                            plan_coeff_applied = True
            except Exception as _exc:
                logger.debug("get_seller_total_earnings: подавлено исключение: %s", _exc)

            if plan_coeff_applied:
                total = round(total * plan_coeff, 2)

            return {
                'total_earnings': total,
                'total_sales': result[1] if result else 0,
                'plan_coeff': plan_coeff if plan_coeff_applied else None,
            }
        except Exception as e:
            logger.error(f"Ошибка при получении общего заработка: {e}")
            if 'conn' in locals():
                conn.close()
            return {'total_earnings': 0.0, 'total_sales': 0}

    def get_top_sellers_by_earnings(self, start_date=None, end_date=None, limit=10):
        """Получение топа продавцов по заработку"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            query = '''
                SELECT u.first_name, u.last_name, u.shop_name,
                       SUM(se.commission_amount) as total_earnings,
                       COUNT(se.id) as total_sales
                FROM seller_earnings se
                JOIN sales s ON se.sale_id = s.id
                JOIN users u ON se.user_id = u.id
                WHERE 1=1
            '''
            params = []

            if start_date:
                query += ' AND s.sale_date >= ?'
                params.append(start_date)

            if end_date:
                query += ' AND s.sale_date <= ?'
                params.append(end_date)

            query += '''
                GROUP BY se.user_id, u.first_name, u.last_name, u.shop_name
                ORDER BY total_earnings DESC
                LIMIT ?
            '''
            params.append(limit)

            cursor.execute(query, params)
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении топа продавцов: {e}")
            if 'conn' in locals():
                conn.close()
            return []
    # Методы для работы с запланированными уведомлениями
    def add_scheduled_notification(self, job_id, created_by, notification_text, recipients_type, recipients_list, scheduled_datetime):
        """Добавление запланированного уведомления"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            import json
            recipients_json = json.dumps(recipients_list) if recipients_list else None
            
            cursor.execute('''
                INSERT INTO scheduled_notifications 
                (job_id, created_by, notification_text, recipients_type, recipients_list, scheduled_datetime)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (job_id, created_by, notification_text, recipients_type, recipients_json, scheduled_datetime))
            
            conn.commit()
            notification_id = cursor.lastrowid
            conn.close()
            return notification_id
        except Exception as e:
            logger.error(f"Ошибка при добавлении запланированного уведомления: {e}")
            if 'conn' in locals():
                conn.close()
            return None
    
    def get_scheduled_notifications(self, status='pending'):
        """Получение всех запланированных уведомлений"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            if status:
                cursor.execute('''
                    SELECT sn.*, u.first_name, u.last_name
                    FROM scheduled_notifications sn
                    JOIN users u ON sn.created_by = u.id
                    WHERE sn.status = ?
                    ORDER BY sn.scheduled_datetime ASC
                ''', (status,))
            else:
                cursor.execute('''
                    SELECT sn.*, u.first_name, u.last_name
                    FROM scheduled_notifications sn
                    JOIN users u ON sn.created_by = u.id
                    ORDER BY sn.scheduled_datetime ASC
                ''')
            
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении запланированных уведомлений: {e}")
            if 'conn' in locals():
                conn.close()
            return []
    
    def get_scheduled_notification(self, notification_id):
        """Получение запланированного уведомления по ID"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT sn.*, u.first_name, u.last_name
                FROM scheduled_notifications sn
                JOIN users u ON sn.created_by = u.id
                WHERE sn.id = ?
            ''', (notification_id,))
            
            result = cursor.fetchone()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении запланированного уведомления: {e}")
            if 'conn' in locals():
                conn.close()
            return None
    
    def delete_scheduled_notification(self, notification_id):
        """Удаление запланированного уведомления"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM scheduled_notifications WHERE id = ?', (notification_id,))
            
            conn.commit()
            deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Ошибка при удалении запланированного уведомления: {e}")
            if 'conn' in locals():
                conn.close()
            return False
    
    def update_scheduled_notification_status(self, job_id, status):
        """Обновление статуса запланированного уведомления"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE scheduled_notifications 
                SET status = ? 
                WHERE job_id = ?
            ''', (status, job_id))
            
            conn.commit()
            updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса уведомления: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    # ── Оклады и графики работы ───────────────────────────────────────────────

    def set_salary_rate(self, user_id, daily_rate, updated_by=None):
        """Установить/обновить дневную ставку продавца"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO salary_settings (user_id, daily_rate, updated_by, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    daily_rate = excluded.daily_rate,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
            ''', (user_id, daily_rate, updated_by))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка set_salary_rate: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_salary_rate(self, user_id):
        """Получить дневную ставку продавца (0.0 если не задана)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT daily_rate FROM salary_settings WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else 0.0
        except Exception as e:
            logger.error(f"Ошибка get_salary_rate: {e}")
            if 'conn' in locals():
                conn.close()
            return 0.0

    def get_all_salary_rates(self):
        """Все продавцы с их дневными ставками: (user_id, first_name, last_name, daily_rate, telegram_id)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.id, u.first_name, u.last_name,
                       COALESCE(ss.daily_rate, 0) AS daily_rate,
                       u.telegram_id
                FROM users u
                LEFT JOIN salary_settings ss ON ss.user_id = u.id
                ORDER BY u.last_name, u.first_name
            ''')
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_all_salary_rates: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def toggle_work_day(self, user_id, work_date, marked_by=None):
        """Переключить рабочий день. True = день стал рабочим, False = удалён."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id FROM work_schedule WHERE user_id = ? AND work_date = ?',
                (user_id, work_date)
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    'DELETE FROM work_schedule WHERE user_id = ? AND work_date = ?',
                    (user_id, work_date)
                )
                conn.commit()
                conn.close()
                return False
            else:
                cursor.execute(
                    'INSERT INTO work_schedule (user_id, work_date, marked_by) VALUES (?, ?, ?)',
                    (user_id, work_date, marked_by)
                )
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            logger.error(f"Ошибка toggle_work_day: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_work_schedule(self, user_id, year, month):
        """Множество дат рабочих смен за месяц (строки YYYY-MM-DD)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            month_start = f"{year}-{month:02d}-01"
            month_end = f"{year}-{month:02d}-31"
            cursor.execute(
                'SELECT work_date FROM work_schedule WHERE user_id = ? '
                'AND work_date >= ? AND work_date <= ?',
                (user_id, month_start, month_end)
            )
            result = {row[0] for row in cursor.fetchall()}
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_work_schedule: {e}")
            if 'conn' in locals():
                conn.close()
            return set()

    def get_worked_days_count(self, user_id, year, month):
        """Количество отработанных смен за месяц"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            month_start = f"{year}-{month:02d}-01"
            month_end = f"{year}-{month:02d}-31"
            cursor.execute(
                'SELECT COUNT(*) FROM work_schedule WHERE user_id = ? '
                'AND work_date >= ? AND work_date <= ?',
                (user_id, month_start, month_end)
            )
            result = cursor.fetchone()[0]
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_worked_days_count: {e}")
            if 'conn' in locals():
                conn.close()
            return 0

    # ── Шаблоны смен и время работы ──────────────────────────────────────────

    def set_shift_template(self, user_id: int, weekday: int,
                           start_time, end_time) -> None:
        """Сохранить шаблон смены для дня недели (0=Пн … 6=Вс).
        start_time/end_time — строки "10:00" или None (выходной)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO shift_templates (user_id, weekday, start_time, end_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, weekday) DO UPDATE SET
                    start_time = excluded.start_time,
                    end_time   = excluded.end_time
            ''', (user_id, weekday, start_time, end_time))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка set_shift_template: {e}")
            if 'conn' in locals():
                conn.close()

    def get_shift_templates(self, user_id: int) -> dict:
        """Вернуть шаблоны смен: {weekday: (start_time, end_time) | None}."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT weekday, start_time, end_time FROM shift_templates WHERE user_id = ?',
                (user_id,)
            )
            rows = cursor.fetchall()
            conn.close()
            result = {}
            for wd, st, et in rows:
                result[wd] = (st, et) if (st and et) else None
            return result
        except Exception as e:
            logger.error(f"Ошибка get_shift_templates: {e}")
            if 'conn' in locals():
                conn.close()
            return {}

    def get_work_day_time(self, user_id: int, work_date: str):
        """Вернуть (start_time, end_time) для конкретного дня или (None, None)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT start_time, end_time FROM work_schedule WHERE user_id = ? AND work_date = ?',
                (user_id, work_date)
            )
            row = cursor.fetchone()
            conn.close()
            return (row[0], row[1]) if row else (None, None)
        except Exception as e:
            logger.error(f"Ошибка get_work_day_time: {e}")
            if 'conn' in locals():
                conn.close()
            return (None, None)

    def get_shifts_starting_at(self, time_str: str, date_str: str) -> list:
        """Вернуть список (user_id, telegram_id, start_time, end_time) для всех пользователей,
        у которых смена на дату date_str начинается в time_str (HH:MM, локальное время).

        Логика:
        - Проверяет work_schedule.start_time == time_str для date_str.
        - Если work_schedule.start_time IS NULL — проверяет шаблон (shift_templates)
          для дня недели, соответствующего date_str.
        - Исключает пользователей с shift_reminders = 0 (notification_settings).
        - Исключает пользователей на утверждённом отсутствии в date_str.
        """
        try:
            from datetime import date as _date
            conn = self.get_connection()
            weekday = _date.fromisoformat(date_str).weekday()

            # Пользователи с явной записью в work_schedule
            rows = conn.execute(
                """SELECT u.id, u.telegram_id, ws.start_time, ws.end_time
                   FROM work_schedule ws
                   JOIN users u ON u.id = ws.user_id
                   LEFT JOIN notification_settings ns ON ns.user_id = ws.user_id
                   WHERE ws.work_date = ?
                     AND ws.start_time = ?
                     AND COALESCE(ns.shift_reminders, 1) = 1""",
                (date_str, time_str)
            ).fetchall() or []

            # Пользователи без явной записи start_time, но с шаблоном на этот день недели
            template_rows = conn.execute(
                """SELECT u.id, u.telegram_id, st.start_time, st.end_time
                   FROM shift_templates st
                   JOIN users u ON u.id = st.user_id
                   LEFT JOIN notification_settings ns ON ns.user_id = st.user_id
                   WHERE st.weekday = ?
                     AND st.start_time = ?
                     AND COALESCE(ns.shift_reminders, 1) = 1
                     AND EXISTS (
                         SELECT 1 FROM work_schedule ws2
                         WHERE ws2.user_id = st.user_id AND ws2.work_date = ?
                           AND (ws2.start_time IS NULL OR ws2.start_time = '')
                     )""",
                (weekday, time_str, date_str)
            ).fetchall() or []

            conn.close()

            # Объединяем, исключая дублей (user_id)
            seen = set()
            result = []
            for row in list(rows) + list(template_rows):
                uid = row[0]
                if uid not in seen:
                    seen.add(uid)
                    result.append(row)

            # Исключаем пользователей на утверждённом отсутствии
            absent_ids = self.get_absent_user_ids_today(date_str)
            if absent_ids:
                result = [r for r in result if r[0] not in absent_ids]

            return result
        except Exception as e:
            logger.error(f"get_shifts_starting_at: {e}")
            return []

    def add_work_day(self, user_id: int, work_date: str,
                     start_time=None, end_time=None, marked_by=None) -> None:
        """Добавить рабочий день (INSERT OR IGNORE) с временем смены."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO work_schedule
                    (user_id, work_date, start_time, end_time, marked_by)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, work_date, start_time, end_time, marked_by))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка add_work_day: {e}")
            if 'conn' in locals():
                conn.close()

    def fill_month_by_template(self, user_id: int, year: int, month: int,
                               marked_by: int = None) -> int:
        """Заполнить месяц рабочими днями по шаблону смен.
        Пропускает уже отмеченные дни и дни, у которых шаблон = выходной (None).
        Возвращает количество добавленных смен."""
        import calendar as _calendar_mod
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT weekday, start_time, end_time FROM shift_templates WHERE user_id = ?',
                (user_id,)
            )
            templates = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
            if not templates:
                conn.close()
                return 0
            days_in_month = _calendar_mod.monthrange(year, month)[1]
            # Build set of dates covered by approved absences for this user in this month
            from datetime import date as _date, timedelta as _td
            absent_dates: set = set()
            _month_start = f"{year}-{month:02d}-01"
            _month_end = f"{year}-{month:02d}-{days_in_month:02d}"
            try:
                cursor.execute(
                    """SELECT start_date, end_date FROM absence_records
                       WHERE user_id = ? AND status = 'approved'
                         AND start_date <= ? AND end_date >= ?""",
                    (user_id, _month_end, _month_start)
                )
                for _abs in cursor.fetchall():
                    _abs_s = _date.fromisoformat(_abs[0])
                    _abs_e = _date.fromisoformat(_abs[1])
                    _cur = max(_abs_s, _date(year, month, 1))
                    _end = min(_abs_e, _date(year, month, days_in_month))
                    while _cur <= _end:
                        absent_dates.add(_cur.isoformat())
                        _cur += _td(days=1)
            except Exception:
                pass
            added = 0
            for day in range(1, days_in_month + 1):
                weekday = _date(year, month, day).weekday()
                tmpl = templates.get(weekday)
                if tmpl is None:
                    continue
                start_t, end_t = tmpl
                if start_t is None:
                    continue
                date_str = f"{year}-{month:02d}-{day:02d}"
                if date_str in absent_dates:
                    continue
                cursor.execute(
                    'INSERT OR IGNORE INTO work_schedule '
                    '(user_id, work_date, start_time, end_time, marked_by) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (user_id, date_str, start_t, end_t, marked_by)
                )
                if cursor.rowcount:
                    added += 1
            conn.commit()
            conn.close()
            return added
        except Exception as e:
            logger.error(f"Ошибка fill_month_by_template: {e}")
            if 'conn' in locals():
                conn.close()
            return 0

    def remove_work_day(self, user_id: int, work_date: str) -> None:
        """Удалить рабочий день."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM work_schedule WHERE user_id = ? AND work_date = ?',
                (user_id, work_date)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка remove_work_day: {e}")
            if 'conn' in locals():
                conn.close()

    def set_work_day_time(self, user_id: int, work_date: str,
                          start_time, end_time) -> None:
        """Обновить время смены для уже существующего рабочего дня."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE work_schedule
                SET start_time = ?, end_time = ?
                WHERE user_id = ? AND work_date = ?
            ''', (start_time, end_time, user_id, work_date))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка set_work_day_time: {e}")
            if 'conn' in locals():
                conn.close()

    # ══════════════════════════════════════════════════════════
    # Integration CRUD
    # ══════════════════════════════════════════════════════════

    def add_integration_connection(self, name: str, config: str,
                                   provider: str = 'google_sheets') -> int:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO integration_connections (name, provider, config) VALUES (?, ?, ?)",
                (name, provider, config)
            )
            row_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return row_id
        except Exception as e:
            logger.error(f"add_integration_connection: {e}")
            if 'conn' in locals():
                conn.close()
            return 0

    def get_integration_connections(self) -> list:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, provider, config, enabled, created_at FROM integration_connections ORDER BY id"
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_integration_connections: {e}")
            return []

    def get_integration_connection(self, connection_id: int):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, provider, config, enabled, created_at FROM integration_connections WHERE id = ?",
                (connection_id,)
            )
            result = cursor.fetchone()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_integration_connection: {e}")
            return None

    def update_integration_connection(self, connection_id: int, **kwargs):
        allowed = {'name', 'config', 'enabled', 'provider'}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            set_clause += ", updated_at = datetime('now')"
            cursor.execute(
                f"UPDATE integration_connections SET {set_clause} WHERE id = ?",
                list(fields.values()) + [connection_id]
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"update_integration_connection: {e}")
            if 'conn' in locals():
                conn.close()

    def delete_integration_connection(self, connection_id: int):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM integration_exports WHERE connection_id = ?", (connection_id,))
            cursor.execute("DELETE FROM integration_connections WHERE id = ?", (connection_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"delete_integration_connection: {e}")
            if 'conn' in locals():
                conn.close()

    def add_integration_export(self, connection_id: int, export_type: str,
                               operation: str, target_sheet: str, schedule,
                               enabled: int = 1, mapping=None,
                               lookup_config=None) -> int:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO integration_exports
                   (connection_id, export_type, operation, target_sheet, schedule,
                    enabled, mapping, lookup_config)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (connection_id, export_type, operation, target_sheet, schedule,
                 enabled, mapping, lookup_config)
            )
            row_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return row_id
        except Exception as e:
            logger.error(f"add_integration_export: {e}")
            if 'conn' in locals():
                conn.close()
            return 0

    def get_integration_exports(self, connection_id: int = None) -> list:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            if connection_id is not None:
                cursor.execute(
                    '''SELECT id, export_type, enabled, schedule, target_sheet,
                              operation, mapping, lookup_config, last_run
                       FROM integration_exports
                       WHERE connection_id = ? ORDER BY id''',
                    (connection_id,)
                )
            else:
                cursor.execute(
                    '''SELECT id, export_type, enabled, schedule, target_sheet,
                              operation, mapping, lookup_config, last_run
                       FROM integration_exports ORDER BY id'''
                )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_integration_exports: {e}")
            return []

    def get_integration_export(self, export_id: int):
        """Returns (export_type, connection_id, enabled, schedule, target_sheet,
                     operation, mapping, lookup_config, extra, last_run)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT export_type, connection_id, enabled, schedule, target_sheet,
                          operation, mapping, lookup_config, extra, last_run
                   FROM integration_exports WHERE id = ?''',
                (export_id,)
            )
            result = cursor.fetchone()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_integration_export: {e}")
            return None

    def get_enabled_exports_by_type(self, export_type: str,
                                    schedule: str = None) -> list:
        """Returns rows of (export_id, conn_id, export_type, schedule, target_sheet,
                             operation, mapping, lookup_config, conn_config_json)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            if schedule is not None:
                cursor.execute(
                    '''SELECT e.id, e.connection_id, e.export_type, e.schedule,
                              e.target_sheet, e.operation, e.mapping, e.lookup_config,
                              c.config
                       FROM integration_exports e
                       JOIN integration_connections c ON c.id = e.connection_id
                       WHERE e.export_type = ? AND e.enabled = 1 AND c.enabled = 1
                         AND e.schedule = ?''',
                    (export_type, schedule)
                )
            else:
                cursor.execute(
                    '''SELECT e.id, e.connection_id, e.export_type, e.schedule,
                              e.target_sheet, e.operation, e.mapping, e.lookup_config,
                              c.config
                       FROM integration_exports e
                       JOIN integration_connections c ON c.id = e.connection_id
                       WHERE e.export_type = ? AND e.enabled = 1 AND c.enabled = 1''',
                    (export_type,)
                )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_enabled_exports_by_type: {e}")
            return []

    def get_all_enabled_cron_exports(self) -> list:
        """Returns rows of (export_id, conn_id, export_type, schedule, target_sheet,
                             operation, mapping, lookup_config, conn_config_json)
           for all exports with a cron schedule (not 'immediate' and not empty)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT e.id, e.connection_id, e.export_type, e.schedule,
                          e.target_sheet, e.operation, e.mapping, e.lookup_config,
                          c.config
                   FROM integration_exports e
                   JOIN integration_connections c ON c.id = e.connection_id
                   WHERE e.enabled = 1 AND c.enabled = 1
                     AND e.schedule IS NOT NULL AND e.schedule != ''
                     AND e.schedule != 'immediate' AND e.schedule != 'disabled' '''
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_all_enabled_cron_exports: {e}")
            return []

    def move_exports_to_connection(self, from_conn_id: int, to_conn_id: int) -> int:
        """Move all exports from one connection to another. Returns count moved."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE integration_exports SET connection_id = ? WHERE connection_id = ?",
                (to_conn_id, from_conn_id)
            )
            moved = cursor.rowcount
            conn.commit()
            conn.close()
            return moved
        except Exception as e:
            logger.error(f"move_exports_to_connection: {e}")
            if 'conn' in locals():
                conn.close()
            return 0

    def update_integration_export(self, export_id: int, **kwargs):
        allowed = {'enabled', 'schedule', 'target_sheet', 'operation',
                   'mapping', 'lookup_config', 'extra'}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            cursor.execute(
                f"UPDATE integration_exports SET {set_clause} WHERE id = ?",
                list(fields.values()) + [export_id]
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"update_integration_export: {e}")
            if 'conn' in locals():
                conn.close()

    def update_integration_export_last_run(self, export_id: int):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE integration_exports SET last_run = datetime('now') WHERE id = ?",
                (export_id,)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"update_integration_export_last_run: {e}")
            if 'conn' in locals():
                conn.close()

    def delete_integration_export(self, export_id: int):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM integration_exports WHERE id = ?", (export_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"delete_integration_export: {e}")
            if 'conn' in locals():
                conn.close()

    def add_integration_log(self, connection_id, export_id, status: str,
                            message: str):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO integration_log (connection_id, export_id, status, message) VALUES (?, ?, ?, ?)",
                (connection_id, export_id, status, message[:2000] if message else '')
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"add_integration_log: {e}")
            if 'conn' in locals():
                conn.close()

    def get_integration_logs(self, connection_id: int = None,
                             limit: int = 15) -> list:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            if connection_id is not None:
                cursor.execute(
                    '''SELECT id, connection_id, export_id, status, message, created_at
                       FROM integration_log WHERE connection_id = ?
                       ORDER BY id DESC LIMIT ?''',
                    (connection_id, limit)
                )
            else:
                cursor.execute(
                    '''SELECT id, connection_id, export_id, status, message, created_at
                       FROM integration_log ORDER BY id DESC LIMIT ?''',
                    (limit,)
                )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_integration_logs: {e}")
            return []

    # ── gs_bonus_cache ─────────────────────────────────────────────────────

    def upsert_bonus_cache(self, connection_id: int, model_name: str,
                           chain: str, bonus: float, rrp: float = 0.0):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO gs_bonus_cache (connection_id, model_name, chain, bonus, rrp, synced_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(connection_id, model_name, chain)
                   DO UPDATE SET bonus=excluded.bonus, rrp=excluded.rrp,
                                 synced_at=datetime('now')''',
                (connection_id, model_name, chain, bonus, rrp)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"upsert_bonus_cache: {e}")

    def get_bonus_cache(self, connection_id: int = None) -> list:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            if connection_id is not None:
                cursor.execute(
                    '''SELECT model_name, chain, bonus, rrp, synced_at
                       FROM gs_bonus_cache WHERE connection_id = ?
                       ORDER BY model_name, chain''',
                    (connection_id,)
                )
            else:
                cursor.execute(
                    '''SELECT model_name, chain, bonus, rrp, synced_at
                       FROM gs_bonus_cache ORDER BY model_name, chain'''
                )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_bonus_cache: {e}")
            return []

    def get_bonus_for_model(self, model_name: str, chain: str):
        """Get cached bonus for a specific model and store chain.
        Returns float if found, None if not in cache."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT bonus FROM gs_bonus_cache WHERE model_name=? AND chain=?',
                (model_name, chain)
            )
            row = cursor.fetchone()
            conn.close()
            return float(row[0]) if row else None
        except Exception as e:
            logger.error(f"get_bonus_for_model: {e}")
            return None

    def clear_bonus_cache(self, connection_id: int):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM gs_bonus_cache WHERE connection_id = ?',
                           (connection_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"clear_bonus_cache: {e}")

    def get_all_admins_telegram_ids(self) -> list:
        """Return telegram_ids of all admin/owner users."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT telegram_id FROM users WHERE is_active = 1 AND role IN ('admin', 'owner') LIMIT 10"
            )
            result = [row[0] for row in cursor.fetchall()]
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_all_admins_telegram_ids: {e}")
            return []

    def get_all_sales_for_export(self) -> list:
        """Return all sales for replace_sheet export."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT s.sale_date, p.name, p.category, s.shop_name,
                          s.quantity_sold, s.sale_price,
                          ROUND(s.quantity_sold * s.sale_price, 2) as total,
                          COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '') as seller
                   FROM sales s
                   JOIN products p ON p.id = s.product_id
                   LEFT JOIN users u ON u.id = s.user_id
                   ORDER BY s.sale_date DESC
                   LIMIT 50000'''
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_all_sales_for_export: {e}")
            return []

    def get_sales_for_matrix_sync(self, date_from: str, date_to: str) -> list:
        """Return raw sales for a date range for matrix sync.
        Returns list of tuples:
        (product_name, category, shop_name, quantity_sold, sale_price, total, seller_name)
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT p.name, p.category, s.shop_name,
                          s.quantity_sold, s.sale_price,
                          ROUND(s.quantity_sold * s.sale_price, 2) AS total,
                          COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '') AS seller_name
                   FROM sales s
                   JOIN products p ON p.id = s.product_id
                   LEFT JOIN users u ON u.id = s.user_id
                   WHERE date(s.sale_date) BETWEEN ? AND ?
                   ORDER BY s.shop_name, p.name''',
                (date_from, date_to)
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_sales_for_matrix_sync: {e}")
            return []

    def get_all_inventory_for_export(self) -> list:
        """Return all inventory for replace_sheet export."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT i.shop_name, p.name, p.category, i.quantity, i.last_updated
                   FROM inventory i
                   JOIN products p ON p.id = i.product_id
                   ORDER BY i.shop_name, p.name'''
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_all_inventory_for_export: {e}")
            return []

    def get_all_products_for_export(self) -> list:
        """Return all products for replace_sheet export.
        Columns: name, category, price, description."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT name, category, price, COALESCE(description, '')
                   FROM products
                   ORDER BY category, name'''
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_all_products_for_export: {e}")
            return []

    def get_all_staff_for_export(self) -> list:
        """Return all staff (users) for replace_sheet export.
        Columns: full_name, shop_name, city, phone."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')),
                          COALESCE(shop_name, ''),
                          COALESCE(city, ''),
                          COALESCE(phone, '')
                   FROM users
                   WHERE telegram_id != 0
                   ORDER BY shop_name, first_name, last_name'''
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_all_staff_for_export: {e}")
            return []

    def get_all_plans_for_export(self) -> list:
        """Return all active sales plans for replace_sheet export.
        Columns: plan_type, metric_type, target_value, shop_name, seller_name."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT sp.plan_type, sp.metric_type, sp.target_value,
                          COALESCE(sp.shop_name, ''),
                          COALESCE(TRIM(u.first_name || ' ' || COALESCE(u.last_name,'')), '') AS seller_name
                   FROM sales_plans sp
                   LEFT JOIN users u ON u.id = sp.user_id
                   WHERE sp.is_active = 1
                   ORDER BY sp.plan_type, sp.shop_name, seller_name'''
            )
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"get_all_plans_for_export: {e}")
            return []

    def calculate_monthly_salary(self, user_id, year, month):
        """Рассчитать зарплату за месяц: смены × ставка + корректировки"""
        try:
            days = self.get_worked_days_count(user_id, year, month)
            rate = self.get_salary_rate(user_id)
            base = days * rate
            adj = self.get_salary_adjustments_sum(user_id, year, month)
            return base + adj
        except Exception as e:
            logger.error(f"Ошибка calculate_monthly_salary: {e}")
            return 0.0

    def get_team_salary_summary(self, year, month):
        """Сводка зарплат по команде: (user_id, fn, ln, daily_rate, worked_days, base_salary, shop_name, telegram_id, adj_sum)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            month_start = f"{year}-{month:02d}-01"
            month_end = f"{year}-{month:02d}-31"
            cursor.execute('''
                SELECT
                    u.id,
                    u.first_name,
                    u.last_name,
                    COALESCE(ss.daily_rate, 0) AS daily_rate,
                    COUNT(ws.id) AS worked_days,
                    COALESCE(ss.daily_rate, 0) * COUNT(ws.id) AS salary,
                    u.shop_name,
                    u.telegram_id,
                    COALESCE((
                        SELECT SUM(sa.amount) FROM salary_adjustments sa
                        WHERE sa.user_id = u.id AND sa.year = ? AND sa.month = ?
                    ), 0) AS adj_sum
                FROM users u
                LEFT JOIN salary_settings ss ON ss.user_id = u.id
                LEFT JOIN work_schedule ws ON ws.user_id = u.id
                    AND ws.work_date >= ? AND ws.work_date <= ?
                GROUP BY u.id
                ORDER BY u.last_name, u.first_name
            ''', (year, month, month_start, month_end))
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_team_salary_summary: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    # ─── Корректировки зарплат ────────────────────────────────────────────────

    def add_salary_adjustment(self, user_id, year, month, amount, comment=None, created_by=None):
        """Добавить ручную корректировку зарплаты (бонус > 0, штраф < 0).
        Возвращает id новой записи или None при ошибке."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO salary_adjustments (user_id, year, month, amount, comment, created_by) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, year, month, amount, comment, created_by)
            )
            row_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return row_id
        except Exception as e:
            logger.error(f"add_salary_adjustment: {e}")
            return None

    def get_salary_adjustments(self, user_id, year, month):
        """Вернуть все корректировки сотрудника за месяц (id, amount, comment, created_by, created_at, creator_name)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sa.id, sa.amount, sa.comment, sa.created_by, sa.created_at,
                       COALESCE(u.first_name || ' ' || u.last_name, 'Система') AS creator_name
                FROM salary_adjustments sa
                LEFT JOIN users u ON u.id = sa.created_by
                WHERE sa.user_id = ? AND sa.year = ? AND sa.month = ?
                ORDER BY sa.created_at DESC
            ''', (user_id, year, month))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"get_salary_adjustments: {e}")
            return []

    def get_salary_bulk_stats(self, year: int, month: int,
                               start_date: str, end_date: str) -> dict:
        """Return {user_id: {'worked': int, 'adj_sum': float, 'motivation': float}}
        for ALL users using 3 GROUP BY queries instead of 3 queries per user (avoids N+1).
        paid_abs is not included — computed per-user due to calendar intersection logic.
        """
        result: dict = {}
        month_start = f"{year}-{month:02d}-01"
        month_end = f"{year}-{month:02d}-31"
        try:
            conn = self.get_connection()
            # 1. worked days per user
            for uid, cnt in conn.execute(
                'SELECT user_id, COUNT(*) FROM work_schedule '
                'WHERE work_date >= ? AND work_date <= ? GROUP BY user_id',
                (month_start, month_end),
            ).fetchall():
                result.setdefault(uid, {'worked': 0, 'adj_sum': 0.0, 'motivation': 0.0})
                result[uid]['worked'] = int(cnt)
            # 2. salary adjustments sum per user
            for uid, s in conn.execute(
                'SELECT user_id, COALESCE(SUM(amount), 0) FROM salary_adjustments '
                'WHERE year = ? AND month = ? GROUP BY user_id',
                (year, month),
            ).fetchall():
                result.setdefault(uid, {'worked': 0, 'adj_sum': 0.0, 'motivation': 0.0})
                result[uid]['adj_sum'] = float(s)
            # 3. motivation (seller_earnings commissions) per user
            rows = conn.execute(
                'SELECT se.user_id, COALESCE(SUM(se.commission_amount), 0) '
                'FROM seller_earnings se JOIN sales s ON se.sale_id = s.id '
                'WHERE s.sale_date >= ? AND s.sale_date <= ? GROUP BY se.user_id',
                (start_date, end_date),
            ).fetchall()
            for uid, earn in rows:
                result.setdefault(uid, {'worked': 0, 'adj_sum': 0.0, 'motivation': 0.0})
                result[uid]['motivation'] = round(float(earn), 2)
            conn.close()
        except Exception as e:
            logger.error(f"get_salary_bulk_stats: {e}")
        return result

    def delete_salary_adjustment(self, adjustment_id, user_id=None):
        """Удалить корректировку по id.
        Если передан user_id — удаляет только если запись принадлежит этому пользователю.
        """
        try:
            conn = self.get_connection()
            if user_id is not None:
                conn.execute(
                    'DELETE FROM salary_adjustments WHERE id = ? AND user_id = ?',
                    (adjustment_id, user_id),
                )
            else:
                conn.execute(
                    'DELETE FROM salary_adjustments WHERE id = ?',
                    (adjustment_id,),
                )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"delete_salary_adjustment: {e}")
            return False

    def get_salary_adjustments_sum(self, user_id, year, month):
        """Сумма всех корректировок за месяц."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT COALESCE(SUM(amount), 0) FROM salary_adjustments WHERE user_id = ? AND year = ? AND month = ?',
                (user_id, year, month)
            )
            result = cursor.fetchone()
            conn.close()
            return float(result[0]) if result else 0.0
        except Exception as e:
            logger.error(f"get_salary_adjustments_sum: {e}")
            return 0.0

    # ─── История движения склада ──────────────────────────────────────────────

    def get_inventory_log(self, shop_name, product_id, limit=30):
        """Вернуть историю изменений остатков товара в магазине."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT il.id, il.old_quantity, il.new_quantity, il.delta,
                       il.change_type, il.change_reason, il.changed_by, il.changed_at,
                       COALESCE(u.first_name || ' ' || u.last_name, NULL) AS changer_name
                FROM inventory_log il
                LEFT JOIN users u ON u.id = il.changed_by
                WHERE il.shop_name = ? AND il.product_id = ?
                ORDER BY il.changed_at DESC, il.id DESC
                LIMIT ?
            ''', (shop_name, product_id, limit))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"get_inventory_log: {e}")
            return []

    def get_inventory_log_web(self, shop_name, product_id, limit=50):
        """История изменений остатков — JOIN по telegram_id (для веб-интерфейса)."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT il.id, il.old_quantity, il.new_quantity, il.delta,
                       il.change_type, il.change_reason, il.changed_by, il.changed_at,
                       COALESCE(u.first_name || ' ' || u.last_name, CAST(il.changed_by AS TEXT)) AS changer_name,
                       u.username
                FROM inventory_log il
                LEFT JOIN users u ON u.telegram_id = il.changed_by
                WHERE il.shop_name = ? AND il.product_id = ?
                ORDER BY il.changed_at DESC, il.id DESC
                LIMIT ?
            ''', (shop_name, product_id, limit))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"get_inventory_log_web: {e}")
            return []

    # ─── Referrals ────────────────────────────────────────────────────────
    def create_referral(self, referrer_telegram_id: int, referred_telegram_id: int) -> bool:
        """Записать реферала. Возвращает True если добавлено (не дубликат)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO referrals (referrer_telegram_id, referred_telegram_id)
                VALUES (?, ?)
            ''', (referrer_telegram_id, referred_telegram_id))
            inserted = cursor.rowcount > 0
            conn.commit()
            return inserted
        except Exception as e:
            logger.error(f"create_referral: {e}")
            return False
        finally:
            conn.close()

    def get_referral_stats(self, telegram_id: int) -> dict:
        """Статистика: сколько рефералов пригласил и сколько бонусных дней получил."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = ? AND bonus_granted = 1',
                (telegram_id,)
            )
            row = cursor.fetchone()
            paid_count = row[0] if row else 0
            cursor.execute(
                'SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = ?',
                (telegram_id,)
            )
            row = cursor.fetchone()
            total_count = row[0] if row else 0
            return {
                'total_referred': total_count,
                'bonus_granted': paid_count,
                'bonus_days': paid_count * 30,
            }
        except Exception as e:
            logger.error(f"get_referral_stats: {e}")
            return {'total_referred': 0, 'bonus_granted': 0, 'bonus_days': 0}
        finally:
            conn.close()

    def apply_referral_bonus(self, referred_telegram_id: int) -> bool:
        """Начислить +30 дней рефереру по telegram_id рефери."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT referrer_telegram_id FROM referrals
                WHERE referred_telegram_id = ? AND bonus_granted = 0
                LIMIT 1
            ''', (referred_telegram_id,))
            row = cursor.fetchone()
            if not row:
                return False
            referrer_tg_id = row[0]
            cursor.execute('''
                UPDATE referrals SET bonus_granted = 1
                WHERE referred_telegram_id = ? AND referrer_telegram_id = ?
            ''', (referred_telegram_id, referrer_tg_id))
            conn.commit()
        except Exception as e:
            logger.error(f"apply_referral_bonus stage 1: {e}")
            conn.close()
            return False
        conn.close()
        return self._extend_subscription_by_days(referrer_tg_id, 30)

    def _extend_subscription_by_days(self, telegram_id: int, days: int) -> bool:
        """Продлить активную/истёкшую подписку пользователя на N дней."""
        from datetime import datetime as _dt, timedelta as _td
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT id, end_date FROM subscriptions
                WHERE user_id = (SELECT id FROM users WHERE telegram_id = ? LIMIT 1)
                ORDER BY end_date DESC LIMIT 1
            ''', (telegram_id,))
            row = cursor.fetchone()
            if row:
                sub_id = row[0]
                try:
                    end_dt = _dt.fromisoformat(row[1])
                except Exception:
                    end_dt = _dt.now()
                base_dt = max(end_dt, _dt.now())
                new_end = (base_dt + _td(days=days)).isoformat()
                cursor.execute('UPDATE subscriptions SET end_date = ? WHERE id = ?', (new_end, sub_id))
            else:
                cursor.execute('SELECT id FROM users WHERE telegram_id = ? LIMIT 1', (telegram_id,))
                u_row = cursor.fetchone()
                if u_row:
                    end_dt2 = (_dt.now() + _td(days=days)).isoformat()
                    cursor.execute('''
                        INSERT INTO subscriptions (user_id, plan_type, start_date, end_date, is_active)
                        VALUES (?, 'referral_bonus', date('now'), ?, 1)
                    ''', (u_row[0], end_dt2))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"_extend_subscription_by_days: {e}")
            return False
        finally:
            conn.close()

    # ─── Subscription Add-ons ─────────────────────────────────────────────
    def create_subscription_addon(self, telegram_id: int, addon_type: str, quantity: int,
                                  amount_paid: float, days: int = 30,
                                  payment_request_id: int = None) -> int:
        """Создать надстройку подписки. Возвращает id новой записи.

        Идемпотентно по payment_request_id: повторный вызов с тем же payment_request_id
        не создаёт дубликат, а возвращает id уже существующей надстройки. Защита от
        двойного клика/ретрая при подтверждении оплаты (money-риск: иначе одна оплата
        наращивала бы лимиты несколько раз).
        """
        from datetime import datetime as _dt, timedelta as _td
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if payment_request_id is not None:
                cursor.execute(
                    'SELECT id FROM subscription_addons WHERE payment_request_id = ?',
                    (payment_request_id,)
                )
                _existing = cursor.fetchone()
                if _existing:
                    logger.warning(
                        "create_subscription_addon: дубль по payment_request_id=%s — пропуск, возврат id=%s",
                        payment_request_id, _existing[0]
                    )
                    return _existing[0]
            expires_at = (_dt.now() + _td(days=days)).isoformat()
            cursor.execute('''
                INSERT INTO subscription_addons
                    (user_telegram_id, addon_type, quantity, price, expires_at, payment_request_id, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            ''', (telegram_id, addon_type, quantity, amount_paid, expires_at, payment_request_id))
            addon_id = cursor.lastrowid
            conn.commit()
            return addon_id
        except Exception as e:
            # Гонка: параллельная вставка с тем же payment_request_id словила UNIQUE-индекс.
            # Это не ошибка — возвращаем id уже существующей надстройки (идемпотентность).
            if payment_request_id is not None:
                try:
                    conn.rollback()
                    cursor.execute(
                        'SELECT id FROM subscription_addons WHERE payment_request_id = ?',
                        (payment_request_id,)
                    )
                    _row = cursor.fetchone()
                    if _row:
                        logger.warning(
                            "create_subscription_addon: гонка по payment_request_id=%s, возврат id=%s",
                            payment_request_id, _row[0]
                        )
                        return _row[0]
                except Exception as _e2:
                    logger.debug("create_subscription_addon recovery: %s", _e2)
            logger.error(f"create_subscription_addon: {e}")
            return 0
        finally:
            conn.close()

    def get_active_addons(self, telegram_id: int) -> list:
        """Вернуть активные надстройки пользователя."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT id, addon_type, quantity, price, expires_at
                FROM subscription_addons
                WHERE user_telegram_id = ? AND is_active = 1
                  AND (expires_at IS NULL OR expires_at > datetime('now'))
            ''', (telegram_id,))
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"get_active_addons: {e}")
            return []
        finally:
            conn.close()

    def get_addon_totals(self, telegram_id: int) -> dict:
        """Суммарные надстройки по типам (extra_shops, extra_products)."""
        rows = self.get_active_addons(telegram_id)
        result = {'extra_shops': 0, 'extra_products': 0}
        for row in rows:
            addon_type = row[1]
            qty = row[2] or 0
            if addon_type in result:
                result[addon_type] += qty
        return result

    # ════════════════════════════════════════════════════════════════════════
    # МОДУЛЬ ОТСУТСТВИЙ (отпуска, больничные, прогулы, отгулы)
    # ════════════════════════════════════════════════════════════════════════

    def get_absence_type_settings(self) -> dict:
        """Вернуть настройки всех типов отсутствий.
        Returns: {type_str: {is_paid, annual_limit, penalty_mode, penalty_amount}}
        """
        conn = self.get_connection()
        try:
            rows = conn.execute(
                'SELECT type, is_paid, annual_limit, penalty_mode, penalty_amount '
                'FROM absence_type_settings'
            ).fetchall()
            return {
                r[0]: {
                    'is_paid': bool(r[1]),
                    'annual_limit': r[2] or 0,
                    'penalty_mode': r[3] or 'none',
                    'penalty_amount': float(r[4] or 0),
                }
                for r in rows
            }
        except Exception as e:
            logger.error(f"get_absence_type_settings: {e}")
            return {}
        finally:
            conn.close()

    def set_absence_type_setting(self, atype: str, is_paid: int,
                                  annual_limit: int, penalty_mode: str,
                                  penalty_amount: float) -> bool:
        """Обновить настройки типа отсутствия."""
        conn = self.get_connection()
        try:
            conn.execute(
                '''INSERT INTO absence_type_settings
                       (type, is_paid, annual_limit, penalty_mode, penalty_amount, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(type) DO UPDATE SET
                       is_paid=excluded.is_paid,
                       annual_limit=excluded.annual_limit,
                       penalty_mode=excluded.penalty_mode,
                       penalty_amount=excluded.penalty_amount,
                       updated_at=excluded.updated_at''',
                (atype, int(is_paid), int(annual_limit), penalty_mode, float(penalty_amount))
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"set_absence_type_setting: {e}")
            return False
        finally:
            conn.close()

    def add_absence(self, user_id: int, atype: str, start_date: str,
                    end_date: str, comment: str = None,
                    created_by: int = None, status: str = 'pending',
                    is_paid=None) -> int:
        """Добавить запись об отсутствии. Возвращает id новой записи (0 при ошибке)."""
        conn = self.get_connection()
        try:
            cur = conn.execute(
                '''INSERT INTO absence_records
                       (user_id, type, start_date, end_date, comment,
                        created_by, status, is_paid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, atype, start_date, end_date, comment,
                 created_by, status, is_paid)
            )
            conn.commit()
            return cur.lastrowid
        except Exception as e:
            logger.error(f"add_absence: {e}")
            return 0
        finally:
            conn.close()

    def update_absence_status(self, absence_id: int, status: str,
                               admin_comment: str = None,
                               reviewed_by: int = None) -> bool:
        """Сменить статус заявки (approved/rejected/cancelled)."""
        conn = self.get_connection()
        try:
            conn.execute(
                '''UPDATE absence_records
                   SET status=?, admin_comment=?, reviewed_by=?,
                       reviewed_at=datetime('now')
                   WHERE id=?''',
                (status, admin_comment, reviewed_by, absence_id)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"update_absence_status: {e}")
            return False
        finally:
            conn.close()

    def get_absence_by_id(self, absence_id: int):
        """Вернуть запись об отсутствии по id (или None)."""
        conn = self.get_connection()
        try:
            return conn.execute(
                'SELECT id, user_id, type, start_date, end_date, status, '
                'is_paid, comment, admin_comment, created_by, reviewed_by, '
                'created_at, reviewed_at FROM absence_records WHERE id=?',
                (absence_id,)
            ).fetchone()
        except Exception as e:
            logger.error(f"get_absence_by_id: {e}")
            return None
        finally:
            conn.close()

    def get_absences_for_user(self, user_id: int,
                               year: int = None, month: int = None) -> list:
        """Список отсутствий сотрудника (опционально за год/месяц).
        Возвращает строки: (id, type, start_date, end_date, status,
                             is_paid, comment, admin_comment, created_at)
        """
        conn = self.get_connection()
        try:
            if year and month:
                import calendar as _cal
                _, days = _cal.monthrange(year, month)
                ms = f"{year}-{month:02d}-01"
                me = f"{year}-{month:02d}-{days:02d}"
                rows = conn.execute(
                    '''SELECT id, type, start_date, end_date, status, is_paid,
                              comment, admin_comment, created_at
                       FROM absence_records
                       WHERE user_id=? AND start_date <= ? AND end_date >= ?
                       ORDER BY start_date DESC''',
                    (user_id, me, ms)
                ).fetchall()
            elif year:
                rows = conn.execute(
                    '''SELECT id, type, start_date, end_date, status, is_paid,
                              comment, admin_comment, created_at
                       FROM absence_records
                       WHERE user_id=? AND start_date LIKE ?
                       ORDER BY start_date DESC''',
                    (user_id, f"{year}-%")
                ).fetchall()
            else:
                rows = conn.execute(
                    '''SELECT id, type, start_date, end_date, status, is_paid,
                              comment, admin_comment, created_at
                       FROM absence_records
                       WHERE user_id=?
                       ORDER BY start_date DESC LIMIT 100''',
                    (user_id,)
                ).fetchall()
            return rows or []
        except Exception as e:
            logger.error(f"get_absences_for_user: {e}")
            return []
        finally:
            conn.close()

    def get_pending_absences(self) -> list:
        """Все ожидающие заявки с именем сотрудника.
        Строки: (id, user_id, type, start_date, end_date, comment,
                  created_at, first_name, last_name, shop_name)
        """
        conn = self.get_connection()
        try:
            return conn.execute(
                '''SELECT ar.id, ar.user_id, ar.type, ar.start_date, ar.end_date,
                          ar.comment, ar.created_at,
                          u.first_name, u.last_name, u.shop_name
                   FROM absence_records ar
                   JOIN users u ON u.id = ar.user_id
                   WHERE ar.status = 'pending'
                   ORDER BY ar.created_at ASC'''
            ).fetchall() or []
        except Exception as e:
            logger.error(f"get_pending_absences: {e}")
            return []
        finally:
            conn.close()

    def get_all_absences_admin(self, year: int, month: int) -> list:
        """Все отсутствия за месяц с именами сотрудников (для веб-таблицы).
        Строки: (id, user_id, type, start_date, end_date, status,
                  is_paid, comment, admin_comment, created_at,
                  first_name, last_name, shop_name)
        """
        import calendar as _cal
        _, days = _cal.monthrange(year, month)
        ms = f"{year}-{month:02d}-01"
        me = f"{year}-{month:02d}-{days:02d}"
        conn = self.get_connection()
        try:
            return conn.execute(
                '''SELECT ar.id, ar.user_id, ar.type, ar.start_date, ar.end_date,
                          ar.status, ar.is_paid, ar.comment, ar.admin_comment,
                          ar.created_at,
                          u.first_name, u.last_name, u.shop_name
                   FROM absence_records ar
                   JOIN users u ON u.id = ar.user_id
                   WHERE ar.start_date <= ? AND ar.end_date >= ?
                   ORDER BY ar.start_date ASC, u.first_name ASC''',
                (me, ms)
            ).fetchall() or []
        except Exception as e:
            logger.error(f"get_all_absences_admin: {e}")
            return []
        finally:
            conn.close()

    # ── org_config key-value helpers ────────────────────────────────────────

    def get_org_config(self, key: str, default: str = '') -> str:
        """Вернуть значение ключа из org_config (str).  Возвращает default если отсутствует."""
        conn = self.get_connection()
        try:
            row = conn.execute('SELECT value FROM org_config WHERE key=?', (key,)).fetchone()
            return row[0] if row else default
        except Exception as e:
            logger.error(f"get_org_config({key}): {e}")
            return default
        finally:
            conn.close()

    def set_org_config(self, key: str, value: str) -> bool:
        """Сохранить / обновить значение ключа в org_config."""
        conn = self.get_connection()
        try:
            conn.execute(
                'INSERT INTO org_config (key, value) VALUES (?, ?)'
                ' ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, str(value))
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"set_org_config({key}): {e}")
            return False
        finally:
            conn.close()

    def count_approved_absences_on_day(self, date_str: str) -> int:
        """Количество уникальных сотрудников с одобренным отсутствием на конкретную дату."""
        conn = self.get_connection()
        try:
            row = conn.execute(
                """SELECT COUNT(DISTINCT user_id) FROM absence_records
                   WHERE status = 'approved'
                     AND start_date <= ? AND end_date >= ?""",
                (date_str, date_str)
            ).fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"count_approved_absences_on_day: {e}")
            return 0
        finally:
            conn.close()

    def get_absence_days_map(self, year: int, month: int,
                              user_id: int = None) -> dict:
        """Карта отсутствий для наложения на календарь расписания.
        Returns: {user_id: {day_num: {type, status, absence_id}}}
        """
        import calendar as _cal
        from datetime import date, timedelta
        _, days_in_month = _cal.monthrange(year, month)
        ms = f"{year}-{month:02d}-01"
        me = f"{year}-{month:02d}-{days_in_month:02d}"
        conn = self.get_connection()
        try:
            q = ('SELECT ar.id, ar.user_id, ar.type, ar.start_date, ar.end_date, ar.status '
                 'FROM absence_records ar '
                 'WHERE ar.start_date <= ? AND ar.end_date >= ? '
                 "AND ar.status IN ('pending','approved')")
            params: tuple = (me, ms)
            if user_id is not None:
                q += ' AND ar.user_id = ?'
                params = (me, ms, user_id)
            rows = conn.execute(q, params).fetchall() or []
        except Exception as e:
            logger.error(f"get_absence_days_map: {e}")
            rows = []
        finally:
            conn.close()

        result: dict = {}
        month_start_d = date(year, month, 1)
        month_end_d = date(year, month, days_in_month)
        for row in rows:
            ab_id, uid, atype, sd, ed, status = row
            try:
                d_start = max(date.fromisoformat(sd[:10]), month_start_d)
                d_end = min(date.fromisoformat(ed[:10]), month_end_d)
            except Exception:
                continue
            cur = d_start
            if uid not in result:
                result[uid] = {}
            while cur <= d_end:
                result[uid][cur.day] = {
                    'type': atype, 'status': status, 'id': ab_id
                }
                cur += timedelta(days=1)
        return result

    def get_absent_user_ids_today(self, date_str: str) -> set:
        """Возвращает set user_id с одобренным отсутствием на указанную дату.
        Используется для фильтрации «На смене» на дашборде.
        """
        conn = self.get_connection()
        try:
            rows = conn.execute(
                """SELECT DISTINCT user_id FROM absence_records
                   WHERE status='approved'
                     AND start_date <= ? AND end_date >= ?""",
                (date_str, date_str)
            ).fetchall() or []
            return {r[0] for r in rows}
        except Exception as e:
            logger.error(f"get_absent_user_ids_today: {e}")
            return set()
        finally:
            conn.close()

    def get_absent_users_today(self, date_str: str) -> dict:
        """Возвращает {user_id: absence_type} для всех одобренных отсутствий на дату.
        Используется для отображения бейджей на списке сотрудников.
        """
        conn = self.get_connection()
        try:
            rows = conn.execute(
                """SELECT user_id, type FROM absence_records
                   WHERE status='approved'
                     AND start_date <= ? AND end_date >= ?""",
                (date_str, date_str)
            ).fetchall() or []
            return {r[0]: r[1] for r in rows}
        except Exception as e:
            logger.error(f"get_absent_users_today: {e}")
            return {}
        finally:
            conn.close()

    def get_paid_absence_days_count(self, user_id: int,
                                     year: int, month: int) -> int:
        """Количество оплачиваемых одобренных дней отсутствия за месяц.
        Используется в расчёте зарплаты как надбавка к отработанным дням.
        """
        import calendar as _cal
        from datetime import date, timedelta
        _, days_in_month = _cal.monthrange(year, month)
        ms = f"{year}-{month:02d}-01"
        me = f"{year}-{month:02d}-{days_in_month:02d}"
        conn = self.get_connection()
        try:
            settings_rows = conn.execute(
                'SELECT type, is_paid FROM absence_type_settings'
            ).fetchall()
            type_paid = {r[0]: bool(r[1]) for r in settings_rows}
            rows = conn.execute(
                '''SELECT ar.type, ar.start_date, ar.end_date, ar.is_paid
                   FROM absence_records ar
                   WHERE ar.user_id=? AND ar.status='approved'
                     AND ar.start_date <= ? AND ar.end_date >= ?
                     AND ar.type != 'absence' ''',
                (user_id, me, ms)
            ).fetchall() or []
        except Exception as e:
            logger.error(f"get_paid_absence_days_count: {e}")
            return 0
        finally:
            conn.close()

        month_start_d = date(year, month, 1)
        month_end_d = date(year, month, days_in_month)
        # Collect all paid-absence dates into a set to prevent double-counting
        # overlapping records (e.g. two approved records covering the same day)
        paid_days: set = set()
        for atype, sd, ed, is_paid_override in rows:
            paid = bool(is_paid_override) if is_paid_override is not None \
                else type_paid.get(atype, True)
            if not paid:
                continue
            try:
                d_start = max(date.fromisoformat(sd[:10]), month_start_d)
                d_end = min(date.fromisoformat(ed[:10]), month_end_d)
                cur = d_start
                while cur <= d_end:
                    paid_days.add(cur)
                    cur += timedelta(days=1)
            except Exception as _exc:
                logger.debug("get_paid_absence_days_count: подавлено исключение: %s", _exc)
        return len(paid_days)

    def get_paid_absence_days_bulk(self, year: int, month: int,
                                    user_ids: list) -> dict:
        """Bulk-версия get_paid_absence_days_count для списка пользователей.
        Возвращает {user_id: int} — количество оплачиваемых дней за месяц.
        Делает 2 запроса вместо N (один для type_settings, один для всех records).
        """
        import calendar as _cal
        from datetime import date, timedelta
        if not user_ids:
            return {}
        _, days_in_month = _cal.monthrange(year, month)
        ms = f"{year}-{month:02d}-01"
        me = f"{year}-{month:02d}-{days_in_month:02d}"
        month_start_d = date(year, month, 1)
        month_end_d = date(year, month, days_in_month)
        conn = self.get_connection()
        try:
            settings_rows = conn.execute(
                'SELECT type, is_paid FROM absence_type_settings'
            ).fetchall()
            type_paid = {r[0]: bool(r[1]) for r in settings_rows}
            placeholders = ','.join('?' * len(user_ids))
            rows = conn.execute(
                f'''SELECT ar.user_id, ar.type, ar.start_date, ar.end_date, ar.is_paid
                   FROM absence_records ar
                   WHERE ar.user_id IN ({placeholders}) AND ar.status='approved'
                     AND ar.start_date <= ? AND ar.end_date >= ?
                     AND ar.type != 'absence' ''',
                (*user_ids, me, ms)
            ).fetchall() or []
        except Exception as e:
            logger.error("get_paid_absence_days_bulk: %s", e)
            return {uid: 0 for uid in user_ids}
        finally:
            conn.close()
        # Group records by user_id
        by_user: dict = {}
        for row in rows:
            by_user.setdefault(row[0], []).append(row[1:])
        result = {}
        for uid in user_ids:
            paid_days: set = set()
            for atype, sd, ed, is_paid_override in by_user.get(uid, []):
                paid = bool(is_paid_override) if is_paid_override is not None \
                    else type_paid.get(atype, True)
                if not paid:
                    continue
                try:
                    d_start = max(date.fromisoformat(sd[:10]), month_start_d)
                    d_end = min(date.fromisoformat(ed[:10]), month_end_d)
                    cur = d_start
                    while cur <= d_end:
                        paid_days.add(cur)
                        cur += timedelta(days=1)
                except Exception as _exc:
                    logger.debug("get_paid_absence_days_bulk uid=%s: %s", uid, _exc)
            result[uid] = len(paid_days)
        return result

    def get_absence_used_days(self, user_id: int, atype: str, year: int) -> int:
        """Использованных дней данного типа за год (для лимитов)."""
        from datetime import date, timedelta
        conn = self.get_connection()
        try:
            rows = conn.execute(
                '''SELECT start_date, end_date FROM absence_records
                   WHERE user_id=? AND type=? AND status='approved'
                     AND start_date LIKE ?''',
                (user_id, atype, f"{year}-%")
            ).fetchall() or []
        except Exception as e:
            logger.error(f"get_absence_used_days: {e}")
            return 0
        finally:
            conn.close()

        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        total = 0
        for sd, ed in rows:
            try:
                d_start = max(date.fromisoformat(sd[:10]), year_start)
                d_end = min(date.fromisoformat(ed[:10]), year_end)
                if d_end >= d_start:
                    total += (d_end - d_start).days + 1
            except Exception as _exc:
                logger.debug("get_absence_used_days: подавлено исключение: %s", _exc)
        return total

    def apply_absence_penalty(self, user_id: int, absence_id: int,
                               year: int, month: int,
                               amount: float, admin_id: int) -> bool:
        """Добавить штраф за прогул в salary_adjustments (с дедупликацией)."""
        self.delete_absence_penalty(absence_id)
        comment = f"Штраф (прогул, запись #{absence_id})"
        return bool(self.add_salary_adjustment(
            user_id, year, month, -abs(amount), comment, admin_id
        ))

    def delete_absence_penalty(self, absence_id: int) -> bool:
        """Удалить штраф за прогул из salary_adjustments (при отмене/отклонении)."""
        try:
            conn = self.get_connection()
            conn.execute(
                "DELETE FROM salary_adjustments WHERE comment = ?",
                (f"Штраф (прогул, запись #{absence_id})",)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"delete_absence_penalty: {e}")
            return False

    def mark_contest_salary_paid(self, contest_id: int) -> bool:
        """Отметить конкурс как выплаченный в зарплату.
        Возвращает True если запись создана впервые, False если уже существовала.
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR IGNORE INTO contest_salary_payouts (contest_id) VALUES (?)',
                (contest_id,)
            )
            inserted = cursor.rowcount > 0
            conn.commit()
            return inserted
        except Exception as e:
            logger.error(f"mark_contest_salary_paid: {e}")
            return False
        finally:
            conn.close()

    def is_contest_salary_paid(self, contest_id: int) -> bool:
        """True если призы конкурса уже были записаны в salary_adjustments."""
        conn = self.get_connection()
        try:
            row = conn.execute(
                'SELECT 1 FROM contest_salary_payouts WHERE contest_id=?',
                (contest_id,)
            ).fetchone()
            return row is not None
        except Exception as e:
            logger.error(f"is_contest_salary_paid: {e}")
            return False
        finally:
            conn.close()

    # ── Chat methods ─────────────────────────────────────────────────────────

    def add_chat_message(self, user_id: int, message: str = '',
                         file_path: str = '', file_name: str = '',
                         file_type: str = '', file_size: int = 0,
                         topic_id: int = 1) -> int:
        """Добавить сообщение в чат. Возвращает id нового сообщения."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO chat_messages (user_id, topic_id, message, file_path, file_name, file_type, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, topic_id, message, file_path, file_name, file_type, file_size))
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return new_id

    def get_chat_messages(self, limit: int = 50, topic_id: int = 1,
                          since_id: int = 0) -> list:
        """Последние N сообщений темы чата с данными пользователя.

        since_id > 0 — только сообщения с id > since_id (для сессионного окна AI).
        Служебные строки (session_break / ai_summary) исключены — для AI истории
        используй get_ai_chat_history_rows.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        extra = f' AND m.id > {int(since_id)}' if since_id > 0 else ''
        cursor.execute(f'''
            SELECT m.id, m.user_id, m.message, m.file_path, m.file_name,
                   m.file_type, m.file_size, m.created_at,
                   u.first_name, u.last_name, u.username,
                   m.is_session_break, m.is_ai_summary
            FROM chat_messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.is_deleted = 0 AND m.topic_id = ?
              AND m.is_session_break = 0 AND m.is_ai_summary = 0{extra}
            ORDER BY m.id DESC
            LIMIT ?
        ''', (topic_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return list(reversed(rows))

    def get_chat_messages_since(self, since_id: int, topic_id: int = 1) -> list:
        """Сообщения с id > since_id в теме (для polling)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.id, m.user_id, m.message, m.file_path, m.file_name,
                   m.file_type, m.file_size, m.created_at,
                   u.first_name, u.last_name, u.username
            FROM chat_messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.is_deleted = 0 AND m.topic_id = ? AND m.id > ?
              AND m.is_session_break = 0 AND m.is_ai_summary = 0
            ORDER BY m.id ASC
        ''', (topic_id, since_id))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_chat_latest_id(self, topic_id: int = 0) -> int:
        """Максимальный id сообщения. topic_id=0 — по всем темам (для FAB-бейджа)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if topic_id:
            cursor.execute(
                'SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE is_deleted = 0 AND topic_id = ?',
                (topic_id,)
            )
        else:
            cursor.execute('SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE is_deleted = 0')
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0

    def soft_delete_chat_message(self, msg_id: int, user_id: int,
                                  is_admin: bool = False) -> bool:
        """Мягкое удаление: своё сообщение или admin/owner."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if is_admin:
            cursor.execute(
                "UPDATE chat_messages SET is_deleted = 1, deleted_at = datetime('now') WHERE id = ?",
                (msg_id,)
            )
        else:
            cursor.execute(
                "UPDATE chat_messages SET is_deleted = 1, deleted_at = datetime('now') WHERE id = ? AND user_id = ?",
                (msg_id, user_id)
            )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def get_chat_deleted_ids_since(self, topic_id: int, since_ts: str) -> list:
        """ID сообщений темы, удалённых после since_ts (UTC-строка datetime('now')).

        Используется polling-ом, чтобы убрать удалённые сообщения у всех клиентов
        в реальном времени. since_ts пустой → ничего не возвращаем (baseline).
        """
        if not since_ts:
            return []
        try:
            conn = self.get_connection()
            rows = conn.execute(
                '''SELECT id FROM chat_messages
                   WHERE topic_id = ? AND is_deleted = 1
                     AND deleted_at IS NOT NULL AND deleted_at >= ?''',
                (topic_id, since_ts)
            ).fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception as e:
            logger.error("get_chat_deleted_ids_since: %s", e)
            return []

    def set_chat_read(self, user_id: int, topic_id: int, last_read_id: int) -> None:
        """Запомнить максимальный прочитанный id в теме для пользователя (upsert, монотонно)."""
        if not user_id or not topic_id:
            return
        try:
            conn = self.get_connection()
            conn.execute(
                '''INSERT INTO chat_read_state (user_id, topic_id, last_read_id, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(user_id, topic_id) DO UPDATE SET
                       last_read_id = MAX(last_read_id, excluded.last_read_id),
                       updated_at   = datetime('now')''',
                (user_id, topic_id, last_read_id)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("set_chat_read: %s", e)

    def get_chat_unread_counts(self, user_id: int) -> dict:
        """{topic_id: кол-во непрочитанных} по всем не-архивным темам для пользователя.

        Непрочитанное = сообщения не своего авторства (user_id != me),
        не удалённые, с id > last_read_id (0 если тема ни разу не открывалась).
        """
        if not user_id:
            return {}
        try:
            conn = self.get_connection()
            rows = conn.execute(
                '''SELECT t.id,
                          COUNT(m.id) AS cnt
                   FROM chat_topics t
                   LEFT JOIN chat_read_state r
                          ON r.topic_id = t.id AND r.user_id = ?
                   LEFT JOIN chat_messages m
                          ON m.topic_id = t.id
                         AND m.is_deleted = 0
                         AND m.user_id != ?
                         AND m.id > COALESCE(r.last_read_id, 0)
                   WHERE t.is_archived = 0
                   GROUP BY t.id''',
                (user_id, user_id)
            ).fetchall()
            conn.close()
            return {r[0]: r[1] or 0 for r in rows}
        except Exception as e:
            logger.error("get_chat_unread_counts: %s", e)
            return {}

    def add_chat_message_files(self, message_id: int, files: list) -> None:
        """Сохранить список файлов для сообщения чата (chat_message_files)."""
        if not files:
            return
        conn = self.get_connection()
        try:
            for i, f in enumerate(files):
                conn.execute(
                    "INSERT INTO chat_message_files (message_id, file_path, file_name, file_type, file_size, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                    (message_id, f.get("file_path", ""), f.get("file_name", ""), f.get("file_type", ""), f.get("file_size", 0), i)
                )
            conn.commit()
        finally:
            conn.close()

    def get_chat_message_files_bulk(self, message_ids: list) -> dict:
        """Вернуть {msg_id: [file_dict, ...]} для списка id сообщений."""
        if not message_ids:
            return {}
        conn = self.get_connection()
        try:
            ph = ','.join('?' * len(message_ids))
            rows = conn.execute(
                f"SELECT id, message_id, file_path, file_name, file_type, file_size FROM chat_message_files WHERE message_id IN ({ph}) ORDER BY message_id, sort_order",
                message_ids
            ).fetchall()
        finally:
            conn.close()
        result: dict = {}
        for fid, mid, fpath, fname, ftype, fsize in rows:
            result.setdefault(mid, []).append({
                "id": fid, "file_path": fpath, "file_name": fname,
                "file_type": ftype, "file_size": fsize,
            })
        return result

    def get_chat_message_file(self, file_id: int) -> tuple | None:
        """Вернуть (file_path, file_name, file_type, message_id) по id вложения."""
        conn = self.get_connection()
        try:
            return conn.execute(
                "SELECT file_path, file_name, file_type, message_id FROM chat_message_files WHERE id = ?",
                (file_id,)
            ).fetchone()
        finally:
            conn.close()

    def delete_chat_message_files(self, message_id: int) -> list:
        """Удалить все вложения сообщения. Возвращает список file_path для удаления с диска."""
        conn = self.get_connection()
        try:
            rows = conn.execute(
                "SELECT file_path FROM chat_message_files WHERE message_id = ?", (message_id,)
            ).fetchall()
            conn.execute("DELETE FROM chat_message_files WHERE message_id = ?", (message_id,))
            conn.commit()
            return [r[0] for r in rows if r[0]]
        finally:
            conn.close()

    def add_dm_files(self, dm_id: int, files: list) -> None:
        """Сохранить список файлов для личного сообщения (dm_message_files)."""
        if not files:
            return
        conn = self.get_connection()
        try:
            for i, f in enumerate(files):
                conn.execute(
                    "INSERT INTO dm_message_files (dm_id, file_path, file_name, file_type, file_size, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                    (dm_id, f.get("file_path", ""), f.get("file_name", ""), f.get("file_type", ""), f.get("file_size", 0), i)
                )
            conn.commit()
        finally:
            conn.close()

    def get_dm_files_bulk(self, dm_ids: list) -> dict:
        """Вернуть {dm_id: [file_dict, ...]} для списка id DM."""
        if not dm_ids:
            return {}
        conn = self.get_connection()
        try:
            ph = ','.join('?' * len(dm_ids))
            rows = conn.execute(
                f"SELECT id, dm_id, file_path, file_name, file_type, file_size FROM dm_message_files WHERE dm_id IN ({ph}) ORDER BY dm_id, sort_order",
                dm_ids
            ).fetchall()
        finally:
            conn.close()
        result: dict = {}
        for fid, did, fpath, fname, ftype, fsize in rows:
            result.setdefault(did, []).append({
                "id": fid, "file_path": fpath, "file_name": fname,
                "file_type": ftype, "file_size": fsize,
            })
        return result

    def get_dm_file(self, file_id: int) -> tuple | None:
        """Вернуть (file_path, file_name, file_type, dm_id) по id DM-вложения."""
        conn = self.get_connection()
        try:
            return conn.execute(
                "SELECT file_path, file_name, file_type, dm_id FROM dm_message_files WHERE id = ?",
                (file_id,)
            ).fetchone()
        finally:
            conn.close()

    def delete_dm_files(self, dm_id: int) -> list:
        """Удалить все записи dm_message_files для dm_id. Возвращает список file_path удалённых записей."""
        conn = self.get_connection()
        try:
            rows = conn.execute(
                "SELECT file_path FROM dm_message_files WHERE dm_id = ?", (dm_id,)
            ).fetchall()
            conn.execute("DELETE FROM dm_message_files WHERE dm_id = ?", (dm_id,))
            conn.commit()
            return [r[0] for r in rows if r[0]]
        finally:
            conn.close()

    def get_chat_topics(self) -> list:
        """Все не-архивные темы чата, отсортированные по sort_order."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT t.id, t.name, t.created_by, t.created_at, t.sort_order,
                   COUNT(m.id) AS msg_count, t.is_ai
            FROM chat_topics t
            LEFT JOIN chat_messages m ON m.topic_id = t.id AND m.is_deleted = 0
            WHERE t.is_archived = 0
            GROUP BY t.id
            ORDER BY t.sort_order ASC, t.id ASC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_ai_topic_id(self) -> int | None:
        """id выделенной AI-темы (is_ai=1, не архивная) или None."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id FROM chat_topics WHERE is_ai = 1 AND is_archived = 0 ORDER BY id ASC LIMIT 1'
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def ensure_ai_topic(self, name: str = '🤖 AI-ассистент') -> int:
        """Гарантирует наличие выделенной AI-темы. Возвращает её id.

        Если тема была заархивирована — реактивирует её. Создаётся с sort_order=0,
        чтобы стоять сразу после «Общий» (id=1, тоже sort_order=0; tie-break по id).
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, is_archived FROM chat_topics WHERE is_ai = 1 ORDER BY id ASC LIMIT 1')
        row = cursor.fetchone()
        if row:
            tid = row[0]
            if row[1]:
                cursor.execute('UPDATE chat_topics SET is_archived = 0 WHERE id = ?', (tid,))
                conn.commit()
            conn.close()
            return tid
        try:
            cursor.execute(
                'INSERT INTO chat_topics (name, created_by, sort_order, is_ai) VALUES (?, ?, 0, 1)',
                (name[:64], None)
            )
            new_id = cursor.lastrowid
            conn.commit()
        except Exception:
            # Гонка: параллельный вызов уже создал AI-тему (partial unique index)
            conn.rollback()
            cursor.execute('SELECT id FROM chat_topics WHERE is_ai = 1 ORDER BY id ASC LIMIT 1')
            _r = cursor.fetchone()
            new_id = _r[0] if _r else 1
        conn.close()
        return new_id

    def add_chat_topic(self, name: str, created_by: int) -> int:
        """Создать новую тему. Возвращает id."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT COALESCE(MAX(sort_order), 0) + 1 FROM chat_topics WHERE is_archived = 0'
        )
        order = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO chat_topics (name, created_by, sort_order) VALUES (?, ?, ?)',
            (name[:64], created_by, order)
        )
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return new_id

    def rename_chat_topic(self, topic_id: int, name: str,
                           user_id: int, is_admin: bool = False) -> bool:
        """Переименовать тему. Разрешено admin или создателю. Тему «Общий» (id=1) и AI-тему нельзя переименовать."""
        if topic_id == 1:
            return False
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT is_ai FROM chat_topics WHERE id = ?', (topic_id,))
        _r = cursor.fetchone()
        if _r and _r[0]:
            conn.close()
            return False
        if is_admin:
            cursor.execute(
                'UPDATE chat_topics SET name = ? WHERE id = ? AND is_archived = 0',
                (name[:64], topic_id)
            )
        else:
            cursor.execute(
                'UPDATE chat_topics SET name = ? WHERE id = ? AND created_by = ? AND is_archived = 0',
                (name[:64], topic_id, user_id)
            )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def archive_chat_topic(self, topic_id: int) -> bool:
        """Архивировать тему (только admin). Тему «Общий» (id=1) и AI-тему нельзя архивировать."""
        if topic_id == 1:
            return False
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT is_ai FROM chat_topics WHERE id = ?', (topic_id,))
        _r = cursor.fetchone()
        if _r and _r[0]:
            conn.close()
            return False
        cursor.execute(
            'UPDATE chat_topics SET is_archived = 1 WHERE id = ?',
            (topic_id,)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def search_chat_messages(self, query: str, topic_id: int | None = None,
                              limit: int = 25) -> list:
        """Полнотекстовый поиск по сообщениям чата.

        Если topic_id задан — ищет только в этой теме.
        Если topic_id=None — ищет по всем не-архивным темам.
        Возвращает строки из 13 колонок:
          (m.id, m.user_id, m.message, m.file_path, m.file_name,
           m.file_type, m.file_size, m.created_at,
           u.first_name, u.last_name, u.username,
           t.id AS topic_id, t.name AS topic_name)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        pattern = f"%{query}%"
        if topic_id is not None:
            cursor.execute('''
                SELECT m.id, m.user_id, m.message, m.file_path, m.file_name,
                       m.file_type, m.file_size, m.created_at,
                       u.first_name, u.last_name, u.username,
                       t.id, t.name
                FROM chat_messages m
                LEFT JOIN users u ON u.id = m.user_id
                LEFT JOIN chat_topics t ON t.id = m.topic_id
                WHERE m.is_deleted = 0
                  AND m.topic_id = ?
                  AND LOWER(m.message) LIKE LOWER(?)
                ORDER BY m.id DESC
                LIMIT ?
            ''', (topic_id, pattern, limit))
        else:
            cursor.execute('''
                SELECT m.id, m.user_id, m.message, m.file_path, m.file_name,
                       m.file_type, m.file_size, m.created_at,
                       u.first_name, u.last_name, u.username,
                       t.id, t.name
                FROM chat_messages m
                LEFT JOIN users u ON u.id = m.user_id
                LEFT JOIN chat_topics t ON t.id = m.topic_id
                WHERE m.is_deleted = 0
                  AND (t.is_archived = 0 OR t.id IS NULL)
                  AND LOWER(m.message) LIKE LOWER(?)
                ORDER BY m.id DESC
                LIMIT ?
            ''', (pattern, limit))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def search_dm_messages(self, query: str, user_id: int, limit: int = 10) -> list:
        """Поиск в личных сообщениях где user_id — отправитель или получатель.

        Возвращает строки из 15 колонок:
          (id, from_user_id, peer_id, message, file_path, file_name,
           file_type, file_size, created_at,
           from_fname, from_lname, from_uname,
           peer_fname, peer_lname, peer_uname)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        pattern = f"%{query}%"
        cursor.execute('''
            SELECT
                d.id,
                d.from_user_id,
                CASE WHEN d.from_user_id = ? THEN d.to_user_id ELSE d.from_user_id END AS peer_id,
                d.message,
                d.file_path, d.file_name, d.file_type, d.file_size,
                d.created_at,
                fu.first_name, fu.last_name, fu.username,
                pu.first_name, pu.last_name, pu.username
            FROM direct_messages d
            JOIN users fu ON fu.id = d.from_user_id
            LEFT JOIN users pu ON pu.id = (
                CASE WHEN d.from_user_id = ? THEN d.to_user_id ELSE d.from_user_id END
            )
            WHERE d.is_deleted = 0
              AND (d.from_user_id = ? OR d.to_user_id = ?)
              AND LOWER(d.message) LIKE LOWER(?)
            ORDER BY d.id DESC
            LIMIT ?
        ''', (user_id, user_id, user_id, user_id, pattern, limit))
        rows = cursor.fetchall()
        conn.close()
        return rows

    # ══════════════════════════════════════════════════════════════════════════
    # DIRECT MESSAGES MODULE
    # ══════════════════════════════════════════════════════════════════════════

    def add_dm(self, from_user_id: int, to_user_id: int,
               message: str = '', file_path: str = '',
               file_name: str = '', file_type: str = '',
               file_size: int = 0, ai_peer_id: int = 0) -> int:
        """Сохранить личное сообщение. Возвращает id записи.

        ai_peer_id — для ответов AI (from_user_id=0): id собеседника, в переписке
        с которым пользователь задал вопрос, чтобы история подтянула ответ.
        """
        try:
            conn = self.get_connection()
            cur = conn.execute(
                '''INSERT INTO direct_messages
                   (from_user_id, to_user_id, message, file_path, file_name, file_type, file_size, ai_peer_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (from_user_id, to_user_id, message, file_path, file_name, file_type, file_size, ai_peer_id)
            )
            conn.commit()
            new_id = cur.lastrowid
            conn.close()
            return new_id
        except Exception as e:
            logger.error("add_dm: %s", e)
            return 0

    def get_dm_conversation(self, user_a: int, user_b: int,
                             limit: int = 50, before_id: int = 0) -> list:
        """История переписки между двумя пользователями (ASC по id).

        before_id > 0 — пагинация назад: сообщения с id < before_id.
        Возвращает список в хронологическом порядке (старые → новые).
        """
        try:
            conn = self.get_connection()
            # Peer-to-peer сообщения + ответы AI (from_user_id=0). AI-ответ виден
            # ТОЛЬКО задавшему вопрос (user_a) в переписке с собеседником (user_b):
            # to_user_id=user_a AND ai_peer_id=user_b. Обратное направление НЕ
            # добавляем — иначе собеседник увидит чужой AI-ответ (утечка).
            params: list = [user_a, user_b, user_b, user_a,
                            user_a, user_b]
            extra = ''
            if before_id > 0:
                extra = ' AND d.id < ?'
                params.append(before_id)
            params.append(limit)
            rows = conn.execute(
                f'''SELECT d.id, d.from_user_id, d.to_user_id,
                           d.message, d.file_path, d.file_name, d.file_type, d.file_size,
                           d.created_at, d.is_read,
                           uf.first_name, uf.last_name, uf.username
                    FROM direct_messages d
                    LEFT JOIN users uf ON uf.id = d.from_user_id
                    WHERE ((d.from_user_id = ? AND d.to_user_id = ?)
                        OR (d.from_user_id = ? AND d.to_user_id = ?)
                        OR (d.from_user_id = 0 AND d.to_user_id = ? AND d.ai_peer_id = ?))
                      AND d.is_deleted = 0{extra}
                    ORDER BY d.id DESC
                    LIMIT ?''',
                params
            ).fetchall()
            conn.close()
            return list(reversed(rows))
        except Exception as e:
            logger.error("get_dm_conversation: %s", e)
            return []

    def get_dm_contacts(self, user_id: int) -> list:
        """Список контактов: последнее сообщение + непрочитанные.

        Возвращает строки:
        (peer_id, first_name, last_name, username, last_msg, last_from, last_file_name, last_at, unread_count)
        Сортировка: DESC по id последнего сообщения.
        """
        try:
            conn = self.get_connection()
            rows = conn.execute(
                '''SELECT
                       last_dm.peer_id,
                       u.first_name, u.last_name, u.username,
                       d.message      AS last_msg,
                       d.from_user_id AS last_from,
                       d.file_name    AS last_file_name,
                       d.created_at   AS last_at,
                       COALESCE(unread.cnt, 0) AS unread_count
                   FROM (
                       SELECT
                           CASE WHEN from_user_id = ? THEN to_user_id
                                ELSE from_user_id END AS peer_id,
                           MAX(id) AS last_id
                       FROM direct_messages
                       WHERE (from_user_id = ? OR to_user_id = ?)
                         AND is_deleted = 0
                       GROUP BY peer_id
                   ) last_dm
                   JOIN direct_messages d ON d.id = last_dm.last_id
                   -- INNER JOIN drops peer_id=0 (AI assistant) — no users row exists
                   -- for it, so the AI never appears as a ghost DM contact.
                   JOIN users u ON u.id = last_dm.peer_id
                   LEFT JOIN (
                       -- AI-ответы (from_user_id=0) привязываем к ai_peer_id,
                       -- иначе их непрочитанное не попадает ни в одну строку
                       -- контакта (но учитывается в общем счётчике) → рассинхрон.
                       SELECT CASE WHEN from_user_id = 0 THEN ai_peer_id
                                   ELSE from_user_id END AS peer_id,
                              COUNT(*) AS cnt
                       FROM direct_messages
                       WHERE to_user_id = ? AND is_read = 0 AND is_deleted = 0
                       GROUP BY peer_id
                   ) unread ON unread.peer_id = last_dm.peer_id
                   ORDER BY last_dm.last_id DESC''',
                (user_id, user_id, user_id, user_id)
            ).fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error("get_dm_contacts: %s", e)
            return []

    def get_dm_org_members(self, exclude_user_id: int) -> list:
        """Все пользователи орга для выбора собеседника.

        Возвращает (id, first_name, last_name, username, shop_name).
        """
        try:
            conn = self.get_connection()
            rows = conn.execute(
                '''SELECT id, first_name, last_name, username, shop_name
                   FROM users
                   WHERE id != ?
                   ORDER BY first_name, last_name''',
                (exclude_user_id,)
            ).fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error("get_dm_org_members: %s", e)
            return []

    def mark_dm_read(self, viewer_id: int, from_user_id: int) -> None:
        """Пометить переписку viewer_id↔from_user_id прочитанной.

        Закрывает как обычные входящие (from_user_id → viewer_id), так и
        AI-ответы этой переписки (from_user_id=0, ai_peer_id=собеседник).
        AI-ответы видны в get_dm_conversation на тех же условиях, поэтому без
        этого их is_read=0 оставался навсегда и счётчик ЛС висел не обнуляясь.
        """
        try:
            conn = self.get_connection()
            conn.execute(
                '''UPDATE direct_messages
                   SET is_read = 1
                   WHERE to_user_id = ? AND is_read = 0 AND is_deleted = 0
                     AND (from_user_id = ?
                          OR (from_user_id = 0 AND ai_peer_id = ?))''',
                (viewer_id, from_user_id, from_user_id)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("mark_dm_read: %s", e)

    def get_dm_unread_count(self, user_id: int) -> int:
        """Общее количество непрочитанных ЛС для пользователя."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT COUNT(*) FROM direct_messages
                   WHERE to_user_id = ? AND is_read = 0 AND is_deleted = 0''',
                (user_id,)
            ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception as e:
            logger.error("get_dm_unread_count: %s", e)
            return 0

    # ── AI-ассистент: выделенный личный тред (peer = AI) ───────────────────────
    # Хранение: запрос пользователя = (from_user_id=user, to_user_id=0);
    #           ответ AI          = (from_user_id=0, to_user_id=user, ai_peer_id=0).
    # Это отличает выделенный AI-тред от СТАРЫХ AI-ответов внутри реальных диалогов
    # (там ai_peer_id = id реального собеседника, т.е. >= 1).

    def get_ai_dm_conversation(self, user_id: int,
                                limit: int = 50, before_id: int = 0,
                                since_id: int = 0) -> list:
        """История личного треда пользователя с AI-ассистентом (ASC по id).

        since_id > 0 — только сообщения с id > since_id (сессионное окно).
        before_id > 0 — пагинация назад (id < before_id).
        Служебные строки (session_break / ai_summary) исключены — только
        содержательные сообщения для истории и отображения.
        """
        try:
            conn = self.get_connection()
            params: list = [user_id, user_id]
            extra = ''
            if since_id > 0:
                extra += ' AND d.id > ?'
                params.append(since_id)
            if before_id > 0:
                extra += ' AND d.id < ?'
                params.append(before_id)
            params.append(limit)
            rows = conn.execute(
                f'''SELECT d.id, d.from_user_id, d.to_user_id,
                           d.message, d.file_path, d.file_name, d.file_type, d.file_size,
                           d.created_at, d.is_read,
                           uf.first_name, uf.last_name, uf.username,
                           d.is_session_break, d.is_ai_summary
                    FROM direct_messages d
                    LEFT JOIN users uf ON uf.id = d.from_user_id
                    WHERE ((d.from_user_id = ? AND d.to_user_id = 0)
                        OR (d.from_user_id = 0 AND d.to_user_id = ? AND d.ai_peer_id = 0))
                      AND d.is_deleted = 0
                      AND d.is_session_break = 0 AND d.is_ai_summary = 0{extra}
                    ORDER BY d.id DESC
                    LIMIT ?''',
                params
            ).fetchall()
            conn.close()
            return list(reversed(rows))
        except Exception as e:
            logger.error("get_ai_dm_conversation: %s", e)
            return []

    def mark_ai_dm_read(self, user_id: int) -> None:
        """Пометить личный AI-тред прочитанным (только ответы AI: from=0, ai_peer_id=0)."""
        try:
            conn = self.get_connection()
            conn.execute(
                '''UPDATE direct_messages
                   SET is_read = 1
                   WHERE to_user_id = ? AND from_user_id = 0 AND ai_peer_id = 0
                     AND is_read = 0 AND is_deleted = 0''',
                (user_id,)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("mark_ai_dm_read: %s", e)

    def get_ai_dm_summary(self, user_id: int) -> tuple:
        """Сводка по AI-треду для строки контакта.

        Возвращает (last_msg, last_from, last_at, unread).
        """
        try:
            conn = self.get_connection()
            last = conn.execute(
                '''SELECT message, from_user_id, created_at
                   FROM direct_messages
                   WHERE ((from_user_id = ? AND to_user_id = 0)
                       OR (from_user_id = 0 AND to_user_id = ? AND ai_peer_id = 0))
                     AND is_deleted = 0
                   ORDER BY id DESC LIMIT 1''',
                (user_id, user_id)
            ).fetchone()
            unread_row = conn.execute(
                '''SELECT COUNT(*) FROM direct_messages
                   WHERE to_user_id = ? AND from_user_id = 0 AND ai_peer_id = 0
                     AND is_read = 0 AND is_deleted = 0''',
                (user_id,)
            ).fetchone()
            conn.close()
            last_msg = last[0] if last else ''
            last_from = last[1] if last else 0
            last_at = last[2] if last else ''
            unread = unread_row[0] if unread_row else 0
            return (last_msg, last_from, last_at, unread)
        except Exception as e:
            logger.error("get_ai_dm_summary: %s", e)
            return ('', 0, '', 0)

    # ── AI Session methods (DM тред) ──────────────────────────────────────────

    def get_last_ai_dm_session_break_id(self, user_id: int) -> int:
        """ID последнего маркера разрыва сессии в личном AI-треде. 0 если нет."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT MAX(id) FROM direct_messages
                   WHERE ((from_user_id = ? AND to_user_id = 0)
                       OR (from_user_id = 0 AND to_user_id = ? AND ai_peer_id = 0))
                     AND is_session_break = 1 AND is_deleted = 0''',
                (user_id, user_id)
            ).fetchone()
            conn.close()
            return int(row[0]) if row and row[0] else 0
        except Exception as e:
            logger.error("get_last_ai_dm_session_break_id: %s", e)
            return 0

    def add_ai_dm_session_break(self, user_id: int) -> int:
        """Вставить маркер разрыва сессии в личный AI-тред. Возвращает id."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                '''INSERT INTO direct_messages
                   (from_user_id, to_user_id, message, is_session_break)
                   VALUES (?, 0, '', 1)''',
                (user_id,)
            )
            conn.commit()
            new_id = cur.lastrowid
            conn.close()
            return new_id
        except Exception as e:
            logger.error("add_ai_dm_session_break: %s", e)
            return 0

    def get_ai_session_text_dm(self, user_id: int, since_id: int = 0) -> str | None:
        """Текст последнего сжатого резюме в текущей AI DM сессии. None если нет."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT message FROM direct_messages
                   WHERE ((from_user_id = 0 AND to_user_id = ?)
                       OR (from_user_id = ? AND to_user_id = 0))
                     AND is_ai_summary = 1 AND is_deleted = 0 AND id > ?
                   ORDER BY id DESC LIMIT 1''',
                (user_id, user_id, since_id)
            ).fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            logger.error("get_ai_session_text_dm: %s", e)
            return None

    def add_ai_session_summary_dm(self, user_id: int, summary: str) -> int:
        """Вставить сжатое резюме сессии в AI DM тред. Возвращает id."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                '''INSERT INTO direct_messages
                   (from_user_id, to_user_id, message, is_ai_summary, ai_peer_id)
                   VALUES (0, ?, ?, 1, 0)''',
                (user_id, summary)
            )
            conn.commit()
            new_id = cur.lastrowid
            conn.close()
            return new_id
        except Exception as e:
            logger.error("add_ai_session_summary_dm: %s", e)
            return 0

    def count_ai_dm_session_msgs(self, user_id: int, since_id: int = 0) -> int:
        """Количество содержательных сообщений в текущей AI DM сессии."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT COUNT(*) FROM direct_messages
                   WHERE ((from_user_id = ? AND to_user_id = 0)
                       OR (from_user_id = 0 AND to_user_id = ? AND ai_peer_id = 0))
                     AND is_deleted = 0 AND is_session_break = 0 AND is_ai_summary = 0
                     AND id > ?''',
                (user_id, user_id, since_id)
            ).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception as e:
            logger.error("count_ai_dm_session_msgs: %s", e)
            return 0

    def archive_old_ai_dm_sessions(self, days: int = 30) -> int:
        """Вставить авто-разрыв для AI DM тредов без активности > N дней.
        Возвращает количество затронутых пользователей."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                '''SELECT from_user_id, MAX(created_at) as last_at
                   FROM direct_messages
                   WHERE to_user_id = 0 AND from_user_id > 0
                     AND is_session_break = 0 AND is_deleted = 0
                   GROUP BY from_user_id
                   HAVING last_at < datetime('now', ?)''',
                (f'-{int(days)} days',)
            ).fetchall()
            count = 0
            for row in rows:
                uid = row[0]
                has_break = conn.execute(
                    '''SELECT MAX(id) FROM direct_messages
                       WHERE from_user_id = ? AND to_user_id = 0
                         AND is_session_break = 1 AND is_deleted = 0''',
                    (uid,)
                ).fetchone()
                last_msg = conn.execute(
                    '''SELECT MAX(id) FROM direct_messages
                       WHERE from_user_id = ? AND to_user_id = 0
                         AND is_session_break = 0 AND is_deleted = 0''',
                    (uid,)
                ).fetchone()
                # Вставляем разрыв только если нет разрыва после последнего сообщения
                break_id = (has_break[0] or 0) if has_break else 0
                last_id = (last_msg[0] or 0) if last_msg else 0
                if last_id > 0 and break_id < last_id:
                    conn.execute(
                        '''INSERT INTO direct_messages
                           (from_user_id, to_user_id, message, is_session_break)
                           VALUES (?, 0, '', 1)''',
                        (uid,)
                    )
                    count += 1
            conn.commit()
            conn.close()
            return count
        except Exception as e:
            logger.error("archive_old_ai_dm_sessions: %s", e)
            return 0

    # ── AI Session methods (Chat тема) ─────────────────────────────────────────

    def get_last_ai_chat_session_break_id(self, topic_id: int) -> int:
        """ID последнего маркера разрыва сессии в AI-теме. 0 если нет."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT MAX(id) FROM chat_messages
                   WHERE topic_id = ? AND is_session_break = 1 AND is_deleted = 0''',
                (topic_id,)
            ).fetchone()
            conn.close()
            return int(row[0]) if row and row[0] else 0
        except Exception as e:
            logger.error("get_last_ai_chat_session_break_id: %s", e)
            return 0

    def add_ai_chat_session_break(self, user_db_id: int, topic_id: int) -> int:
        """Вставить маркер разрыва сессии в AI-тему. Возвращает id."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                '''INSERT INTO chat_messages
                   (user_id, message, topic_id, is_session_break)
                   VALUES (?, '', ?, 1)''',
                (user_db_id, topic_id)
            )
            conn.commit()
            new_id = cur.lastrowid
            conn.close()
            return new_id
        except Exception as e:
            logger.error("add_ai_chat_session_break: %s", e)
            return 0

    def get_ai_session_text_chat(self, topic_id: int, since_id: int = 0) -> str | None:
        """Текст последнего сжатого резюме в текущей AI chat сессии. None если нет."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT message FROM chat_messages
                   WHERE topic_id = ? AND is_ai_summary = 1 AND is_deleted = 0 AND id > ?
                   ORDER BY id DESC LIMIT 1''',
                (topic_id, since_id)
            ).fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            logger.error("get_ai_session_text_chat: %s", e)
            return None

    def add_ai_session_summary_chat(self, topic_id: int, summary: str) -> int:
        """Вставить сжатое резюме сессии в AI-тему. Возвращает id."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                '''INSERT INTO chat_messages
                   (user_id, message, topic_id, is_ai_summary)
                   VALUES (0, ?, ?, 1)''',
                (summary, topic_id)
            )
            conn.commit()
            new_id = cur.lastrowid
            conn.close()
            return new_id
        except Exception as e:
            logger.error("add_ai_session_summary_chat: %s", e)
            return 0

    def count_ai_chat_session_msgs(self, topic_id: int, since_id: int = 0) -> int:
        """Количество содержательных сообщений в текущей AI chat сессии."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT COUNT(*) FROM chat_messages
                   WHERE topic_id = ? AND is_deleted = 0
                     AND is_session_break = 0 AND is_ai_summary = 0
                     AND id > ?''',
                (topic_id, since_id)
            ).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception as e:
            logger.error("count_ai_chat_session_msgs: %s", e)
            return 0

    def archive_old_ai_chat_session(self, topic_id: int, days: int = 30) -> bool:
        """Вставить авто-разрыв для AI-темы, если > N дней нет активности.
        Возвращает True если разрыв был вставлен."""
        try:
            conn = self.get_connection()
            last_row = conn.execute(
                '''SELECT MAX(id), MAX(created_at) FROM chat_messages
                   WHERE topic_id = ? AND is_session_break = 0
                     AND is_ai_summary = 0 AND is_deleted = 0''',
                (topic_id,)
            ).fetchone()
            if not last_row or not last_row[1]:
                conn.close()
                return False
            last_id, last_at = last_row
            # Проверяем возраст последнего сообщения
            is_old = conn.execute(
                "SELECT ? < datetime('now', ?)",
                (last_at, f'-{int(days)} days')
            ).fetchone()
            if not (is_old and is_old[0]):
                conn.close()
                return False
            # Проверяем: нет ли уже разрыва после последнего сообщения
            break_row = conn.execute(
                '''SELECT MAX(id) FROM chat_messages
                   WHERE topic_id = ? AND is_session_break = 1 AND is_deleted = 0''',
                (topic_id,)
            ).fetchone()
            break_id = (break_row[0] or 0) if break_row else 0
            if break_id >= (last_id or 0):
                conn.close()
                return False
            conn.execute(
                '''INSERT INTO chat_messages (user_id, message, topic_id, is_session_break)
                   VALUES (0, '', ?, 1)''',
                (topic_id,)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("archive_old_ai_chat_session: %s", e)
            return False

    def get_dm_message(self, msg_id: int) -> tuple | None:
        """Получить одно ЛС по id (для скачивания файлов)."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                '''SELECT id, from_user_id, to_user_id,
                          message, file_path, file_name, file_type, file_size,
                          created_at, is_read, is_deleted
                   FROM direct_messages WHERE id = ?''',
                (msg_id,)
            ).fetchone()
            conn.close()
            return row
        except Exception as e:
            logger.error("get_dm_message: %s", e)
            return None

    def soft_delete_dm(self, msg_id: int, user_id: int, is_admin: bool = False) -> bool:
        """Мягкое удаление ЛС. Владелец или admin может удалить."""
        try:
            conn = self.get_connection()
            if is_admin:
                conn.execute(
                    'UPDATE direct_messages SET is_deleted = 1 WHERE id = ?',
                    (msg_id,)
                )
            else:
                conn.execute(
                    'UPDATE direct_messages SET is_deleted = 1 WHERE id = ? AND from_user_id = ?',
                    (msg_id, user_id)
                )
            affected = conn.execute('SELECT changes()').fetchone()[0]
            conn.commit()
            conn.close()
            return affected > 0
        except Exception as e:
            logger.error("soft_delete_dm: %s", e)
            return False

    # ══════════════════════════════════════════════════════════════════════════
    # TASKS MODULE
    # ══════════════════════════════════════════════════════════════════════════

    def create_task_topic(self, name: str, color: str = 'blue',
                          created_by: int = 0, sort_order: int = 0) -> int:
        """Создать тему задач. Возвращает id."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                "INSERT INTO task_topics (name, color, created_by, sort_order) VALUES (?, ?, ?, ?)",
                (name, color, created_by, sort_order)
            )
            conn.commit()
            topic_id = cur.lastrowid
            conn.close()
            return topic_id
        except Exception as e:
            logger.error("create_task_topic: %s", e)
            return 0

    def get_task_topics(self) -> list:
        """Список всех тем задач с количеством задач."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """
                SELECT t.id, t.name, t.color, t.sort_order,
                       COUNT(tk.id) AS task_count
                FROM task_topics t
                LEFT JOIN tasks tk ON tk.topic_id = t.id
                GROUP BY t.id
                ORDER BY t.sort_order, t.name
                """
            ).fetchall()
            conn.close()
            return [
                {"id": r[0], "name": r[1], "color": r[2] or "blue",
                 "sort_order": r[3], "task_count": r[4]}
                for r in rows
            ]
        except Exception as e:
            logger.error("get_task_topics: %s", e)
            return []

    def update_task_topic(self, topic_id: int, name: str, color: str) -> bool:
        """Переименовать тему и/или сменить цвет."""
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE task_topics SET name = ?, color = ? WHERE id = ?",
                (name, color, topic_id)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("update_task_topic: %s", e)
            return False

    def delete_task_topic(self, topic_id: int) -> bool:
        """Удалить тему; задачи темы получают topic_id=NULL."""
        try:
            conn = self.get_connection()
            conn.execute("UPDATE tasks SET topic_id = NULL WHERE topic_id = ?", (topic_id,))
            conn.execute("DELETE FROM task_topics WHERE id = ?", (topic_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("delete_task_topic: %s", e)
            return False

    def create_task(self, title: str, description: str = '',
                    topic_id: int | None = None, created_by: int = 0,
                    assigned_to: int | None = None, shop_id: int | None = None,
                    assigned_shop: str | None = None, assign_all: int = 0,
                    priority: str = 'normal', deadline: str | None = None,
                    linked_chat_topic_id: int | None = None,
                    checklist: list | None = None,
                    recurrence: str | None = None) -> int:
        """Создать задачу. Возвращает task_id."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                """
                INSERT INTO tasks
                    (title, description, topic_id, created_by, assigned_to,
                     shop_id, assigned_shop, assign_all,
                     priority, deadline, linked_chat_topic_id, recurrence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (title, description, topic_id, created_by, assigned_to,
                 shop_id, assigned_shop, assign_all,
                 priority, deadline, linked_chat_topic_id, recurrence)
            )
            task_id = cur.lastrowid
            if checklist:
                for idx, text in enumerate(checklist):
                    conn.execute(
                        "INSERT INTO task_checklist (task_id, text, sort_order) VALUES (?, ?, ?)",
                        (task_id, text.strip(), idx)
                    )
            conn.commit()
            conn.close()
            return task_id
        except Exception as e:
            logger.error("create_task: %s", e)
            return 0

    def get_tasks(self, assigned_to: int | None = None,
                  topic_id: int | None = None, status: str | None = None,
                  created_by: int | None = None,
                  is_admin: bool = False, my_user_id: int | None = None,
                  my_shop: str | None = None,
                  shop_filter: str | None = None,
                  q: str | None = None) -> list:
        """Список задач с фильтрацией. Admin видит все, user — только свои."""
        try:
            conn = self.get_connection()
            where = ["1=1"]
            params = []
            if not is_admin and my_user_id:
                sub_clauses = ["t.created_by = ?", "t.assign_all = 1"]
                sub_params = [my_user_id]
                sub_clauses.append("t.assigned_to = ?")
                sub_params.append(my_user_id)
                if my_shop:
                    sub_clauses.append("t.assigned_shop = ?")
                    sub_params.append(my_shop)
                where.append(f"({' OR '.join(sub_clauses)})")
                params += sub_params
            if assigned_to:
                where.append("t.assigned_to = ?")
                params.append(assigned_to)
            if shop_filter:
                where.append("t.assigned_shop = ?")
                params.append(shop_filter)
            if topic_id:
                where.append("t.topic_id = ?")
                params.append(topic_id)
            if status:
                where.append("t.status = ?")
                params.append(status)
            if q:
                where.append("(lower_u(t.title) LIKE lower_u(?) OR lower_u(t.description) LIKE lower_u(?))")
                like = f"%{q}%"
                params += [like, like]
            where_sql = " AND ".join(where)
            rows = conn.execute(
                f"""
                SELECT t.id, t.title, t.description, t.topic_id, t.created_by,
                       t.assigned_to, t.shop_id, t.priority, t.status, t.deadline,
                       t.linked_chat_topic_id, t.created_at, t.updated_at,
                       tt.name AS topic_name, tt.color AS topic_color,
                       ua.first_name AS a_fn, ua.last_name AS a_ln, ua.username AS a_un,
                       uc.first_name AS c_fn, uc.last_name AS c_ln, uc.username AS c_un,
                       t.assigned_shop, t.assign_all,
                       (SELECT COUNT(*) FROM task_checklist cl WHERE cl.task_id = t.id) AS cl_total,
                       (SELECT COUNT(*) FROM task_checklist cl WHERE cl.task_id = t.id AND cl.is_done = 1) AS cl_done,
                       t.recurrence
                FROM tasks t
                LEFT JOIN task_topics tt ON tt.id = t.topic_id
                LEFT JOIN users ua ON ua.id = t.assigned_to
                LEFT JOIN users uc ON uc.id = t.created_by
                WHERE {where_sql}
                ORDER BY
                    CASE t.status WHEN 'new' THEN 0 WHEN 'in_progress' THEN 1
                                  WHEN 'review' THEN 2 WHEN 'done' THEN 3 ELSE 4 END,
                    CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                    WHEN 'normal' THEN 2 ELSE 3 END,
                    t.deadline ASC NULLS LAST, t.id DESC
                """,
                params
            ).fetchall()
            conn.close()
            result = []
            for r in rows:
                a_name = f"{r[15] or ''} {r[16] or ''}".strip() or r[17] or ""
                c_name = f"{r[18] or ''} {r[19] or ''}".strip() or r[20] or ""
                result.append({
                    "id": r[0], "title": r[1], "description": r[2],
                    "topic_id": r[3], "created_by": r[4], "assigned_to": r[5],
                    "shop_id": r[6], "priority": r[7] or "normal",
                    "status": r[8] or "new", "deadline": r[9],
                    "linked_chat_topic_id": r[10],
                    "created_at": r[11], "updated_at": r[12],
                    "topic_name": r[13], "topic_color": r[14] or "blue",
                    "assigned_name": a_name, "creator_name": c_name,
                    "assigned_shop": r[21], "assign_all": bool(r[22]),
                    "checklist_total": r[23], "checklist_done": r[24],
                    "recurrence": r[25] or "none",
                })
            return result
        except Exception as e:
            logger.error("get_tasks: %s", e)
            return []

    def get_task(self, task_id: int) -> dict | None:
        """Получить задачу по id с чеклистом, темой, именами участников."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                """
                SELECT t.id, t.title, t.description, t.topic_id, t.created_by,
                       t.assigned_to, t.shop_id, t.priority, t.status, t.deadline,
                       t.linked_chat_topic_id, t.created_at, t.updated_at,
                       tt.name AS topic_name, tt.color AS topic_color,
                       ua.first_name AS a_fn, ua.last_name AS a_ln, ua.username AS a_un,
                       uc.first_name AS c_fn, uc.last_name AS c_ln, uc.username AS c_un,
                       t.assigned_shop, t.assign_all, t.recurrence,
                       t.rating, t.rating_comment
                FROM tasks t
                LEFT JOIN task_topics tt ON tt.id = t.topic_id
                LEFT JOIN users ua ON ua.id = t.assigned_to
                LEFT JOIN users uc ON uc.id = t.created_by
                WHERE t.id = ?
                """,
                (task_id,)
            ).fetchone()
            if not row:
                conn.close()
                return None
            a_name = f"{row[15] or ''} {row[16] or ''}".strip() or row[17] or ""
            c_name = f"{row[18] or ''} {row[19] or ''}".strip() or row[20] or ""
            checklist_rows = conn.execute(
                "SELECT id, text, is_done, done_by, done_at, sort_order "
                "FROM task_checklist WHERE task_id = ? ORDER BY sort_order, id",
                (task_id,)
            ).fetchall()
            conn.close()
            checklist = [
                {"id": cl[0], "text": cl[1], "is_done": bool(cl[2]),
                 "done_by": cl[3], "done_at": cl[4]}
                for cl in checklist_rows
            ]
            return {
                "id": row[0], "title": row[1], "description": row[2],
                "topic_id": row[3], "created_by": row[4], "assigned_to": row[5],
                "shop_id": row[6], "priority": row[7] or "normal",
                "status": row[8] or "new", "deadline": row[9],
                "linked_chat_topic_id": row[10],
                "created_at": row[11], "updated_at": row[12],
                "topic_name": row[13], "topic_color": row[14] or "blue",
                "assigned_name": a_name, "creator_name": c_name,
                "assigned_shop": row[21], "assign_all": bool(row[22]),
                "recurrence": row[23] or "none",
                "rating": row[24], "rating_comment": row[25] or "",
                "checklist": checklist,
            }
        except Exception as e:
            logger.error("get_task: %s", e)
            return None

    def rate_task(self, task_id: int, rating: int, comment: str = "") -> bool:
        """Сохранить оценку выполнения задачи (1-5 звёзд)."""
        try:
            conn = self.get_connection()
            try:
                conn.execute(
                    "UPDATE tasks SET rating = ?, rating_comment = ?, updated_at = datetime('now') WHERE id = ?",
                    (max(1, min(5, rating)), comment.strip(), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            return True
        except Exception as e:
            logger.error("rate_task: %s", e)
            return False

    def update_task_status(self, task_id: int, status: str) -> bool:
        """Обновить статус задачи."""
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, task_id)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("update_task_status: %s", e)
            return False

    def add_task_history(self, task_id: int, user_id: int | None,
                         action: str, old_val: str | None = None,
                         new_val: str | None = None) -> bool:
        """Добавить запись в историю изменений задачи."""
        try:
            conn = self.get_connection()
            conn.execute(
                "INSERT INTO task_history (task_id, user_id, action, old_val, new_val) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, user_id, action, old_val, new_val)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("add_task_history: %s", e)
            return False

    def get_task_history(self, task_id: int, limit: int = 50) -> list:
        """Получить историю изменений задачи (новые первые)."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """
                SELECT h.id, h.task_id, h.user_id, h.action, h.old_val, h.new_val,
                       h.created_at,
                       (u.first_name || COALESCE(' ' || u.last_name, '')) AS actor_name
                FROM task_history h
                LEFT JOIN users u ON u.id = h.user_id
                WHERE h.task_id = ?
                ORDER BY h.created_at DESC
                LIMIT ?
                """,
                (task_id, limit)
            ).fetchall()
            conn.close()
            return [
                {"id": r[0], "task_id": r[1], "user_id": r[2],
                 "action": r[3], "old_val": r[4], "new_val": r[5],
                 "created_at": r[6], "actor_name": (r[7] or "").strip() or "Система"}
                for r in rows
            ]
        except Exception as e:
            logger.error("get_task_history: %s", e)
            return []

    def add_task_reminder(self, task_id: int, user_id: int, remind_at: str) -> int | None:
        """Добавить напоминание о задаче (remind_at — UTC ISO строка)."""
        try:
            conn = self.get_connection()
            try:
                cur = conn.execute(
                    "INSERT INTO task_reminders (task_id, user_id, remind_at) VALUES (?, ?, ?)",
                    (task_id, user_id, remind_at),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
        except Exception as e:
            logger.error("add_task_reminder: %s", e)
            return None

    def get_due_task_reminders(self) -> list:
        """Вернуть неотправленные напоминания с remind_at <= now (UTC)."""
        try:
            conn = self.get_connection()
            try:
                rows = conn.execute(
                    """
                    SELECT r.id, r.task_id, r.user_id, r.remind_at,
                           t.title,
                           u.telegram_id
                    FROM task_reminders r
                    JOIN tasks t ON t.id = r.task_id
                    JOIN users u ON u.id  = r.user_id
                    WHERE r.sent = 0
                      AND r.remind_at <= datetime('now')
                    ORDER BY r.remind_at
                    """
                ).fetchall()
            finally:
                conn.close()
            return [
                {"id": r[0], "task_id": r[1], "user_id": r[2],
                 "remind_at": r[3], "title": r[4], "telegram_id": r[5]}
                for r in rows
            ]
        except Exception as e:
            logger.error("get_due_task_reminders: %s", e)
            return []

    def mark_task_reminder_sent(self, reminder_id: int) -> None:
        """Пометить напоминание как отправленное."""
        try:
            conn = self.get_connection()
            try:
                conn.execute("UPDATE task_reminders SET sent = 1 WHERE id = ?", (reminder_id,))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error("mark_task_reminder_sent: %s", e)

    def get_task_reminders_for_user(self, task_id: int, user_id: int) -> list:
        """Активные (неотправленные) напоминания пользователя по задаче."""
        try:
            conn = self.get_connection()
            try:
                rows = conn.execute(
                    "SELECT id, remind_at FROM task_reminders WHERE task_id=? AND user_id=? AND sent=0 ORDER BY remind_at",
                    (task_id, user_id),
                ).fetchall()
            finally:
                conn.close()
            return [{"id": r[0], "remind_at": r[1]} for r in rows]
        except Exception as e:
            logger.error("get_task_reminders_for_user: %s", e)
            return []

    def update_task(self, task_id: int, title: str, description: str,
                    topic_id: int | None, assigned_to: int | None,
                    shop_id: int | None, priority: str,
                    deadline: str | None,
                    assigned_shop: str | None = None,
                    assign_all: int = 0,
                    recurrence: str | None = None) -> bool:
        """Обновить поля задачи (редактирование admin)."""
        try:
            conn = self.get_connection()
            conn.execute(
                """
                UPDATE tasks SET title=?, description=?, topic_id=?,
                    assigned_to=?, shop_id=?, assigned_shop=?, assign_all=?,
                    priority=?, deadline=?, recurrence=?,
                    updated_at=datetime('now')
                WHERE id=?
                """,
                (title, description, topic_id, assigned_to,
                 shop_id, assigned_shop, assign_all,
                 priority, deadline, recurrence, task_id)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("update_task: %s", e)
            return False

    def delete_task(self, task_id: int) -> bool:
        """Удалить задачу вместе с чеклистом и комментариями."""
        try:
            conn = self.get_connection()
            conn.execute("DELETE FROM task_checklist WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("delete_task: %s", e)
            return False

    def add_task_comment(self, task_id: int, user_id: int, text: str) -> int:
        """Добавить комментарий к задаче. Возвращает id комментария."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                "INSERT INTO task_comments (task_id, user_id, text) VALUES (?, ?, ?)",
                (task_id, user_id, text)
            )
            comment_id = cur.lastrowid
            conn.commit()
            conn.close()
            return comment_id
        except Exception as e:
            logger.error("add_task_comment: %s", e)
            return 0

    def get_task_comments(self, task_id: int) -> list:
        """Список комментариев задачи с именами авторов."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """
                SELECT c.id, c.task_id, c.user_id, c.text, c.created_at,
                       u.first_name, u.last_name, u.username
                FROM task_comments c
                LEFT JOIN users u ON u.id = c.user_id
                WHERE c.task_id = ?
                ORDER BY c.created_at ASC
                """,
                (task_id,)
            ).fetchall()
            conn.close()
            result = []
            for r in rows:
                author = f"{r[5] or ''} {r[6] or ''}".strip() or r[7] or f"User#{r[2]}"
                raw = str(r[4] or "")[:16].replace("T", " ")
                try:
                    d, t = raw.split(" ")
                    y, mo, day = d.split("-")
                    ts = f"{day}.{mo}.{y} {t}"
                except Exception:
                    ts = raw
                result.append({
                    "id": r[0], "task_id": r[1], "user_id": r[2],
                    "text": r[3], "created_at": r[4],
                    "created_at_fmt": ts, "author_name": author,
                })
            return result
        except Exception as e:
            logger.error("get_task_comments: %s", e)
            return []

    def add_task_attachments(self, task_id: int, user_id: int, files: list) -> int:
        """Добавить вложения к задаче. Возвращает количество сохранённых файлов."""
        if not files:
            return 0
        conn = self.get_connection()
        saved = 0
        try:
            for f in files:
                conn.execute(
                    "INSERT INTO task_attachments (task_id, user_id, file_path, file_name, file_type, file_size) VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, user_id, f.get("file_path", ""), f.get("file_name", ""), f.get("file_type", ""), f.get("file_size", 0))
                )
                saved += 1
            conn.commit()
        except Exception as e:
            logger.error("add_task_attachments: %s", e)
        finally:
            conn.close()
        return saved

    def get_task_attachments(self, task_id: int) -> list:
        """Список вложений задачи с именем автора."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """SELECT a.id, a.task_id, a.user_id, a.file_path, a.file_name,
                          a.file_type, a.file_size, a.created_at,
                          u.first_name, u.last_name, u.username
                   FROM task_attachments a
                   LEFT JOIN users u ON u.id = a.user_id
                   WHERE a.task_id = ?
                   ORDER BY a.created_at ASC""",
                (task_id,)
            ).fetchall()
            conn.close()
            result = []
            for r in rows:
                author = f"{r[8] or ''} {r[9] or ''}".strip() or r[10] or f"User#{r[2]}"
                raw = str(r[7] or "")[:16].replace("T", " ")
                try:
                    d, t = raw.split(" ")
                    y, mo, day = d.split("-")
                    ts = f"{day}.{mo}.{y} {t}"
                except Exception:
                    ts = raw
                is_image = (r[5] or "").startswith("image/")
                result.append({
                    "id": r[0], "task_id": r[1], "user_id": r[2],
                    "file_name": r[4], "file_type": r[5], "file_size": r[6],
                    "created_at_fmt": ts, "author_name": author,
                    "is_image": is_image,
                    "file_url": f"/tasks/attachment/{r[0]}",
                })
            return result
        except Exception as e:
            logger.error("get_task_attachments: %s", e)
            return []

    def get_task_attachment(self, att_id: int) -> tuple | None:
        """Вернуть (file_path, file_name, file_type, task_id, user_id) по id вложения."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT file_path, file_name, file_type, task_id, user_id FROM task_attachments WHERE id = ?",
                (att_id,)
            ).fetchone()
            conn.close()
            return row
        except Exception as e:
            logger.error("get_task_attachment: %s", e)
            return None

    def delete_task_attachment(self, att_id: int, user_id: int, is_admin: bool = False) -> tuple[bool, str]:
        """Удалить вложение задачи. Возвращает (success, file_path)."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT file_path, user_id FROM task_attachments WHERE id = ?", (att_id,)
            ).fetchone()
            if not row:
                conn.close()
                return False, ""
            fpath, owner_id = row
            if not is_admin and owner_id != user_id:
                conn.close()
                return False, ""
            conn.execute("DELETE FROM task_attachments WHERE id = ?", (att_id,))
            conn.commit()
            conn.close()
            return True, fpath or ""
        except Exception as e:
            logger.error("delete_task_attachment: %s", e)
            return False, ""

    def get_task_attachments_count(self, task_id: int) -> int:
        """Количество вложений у задачи."""
        try:
            conn = self.get_connection()
            row = conn.execute("SELECT COUNT(*) FROM task_attachments WHERE task_id = ?", (task_id,)).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0

    def record_task_user_completion(self, task_id: int, user_id: int, status: str = 'done') -> bool:
        """Записать/обновить персональное выполнение задачи (для командных задач assign_all/shop)."""
        try:
            conn = self.get_connection()
            conn.execute(
                "INSERT INTO task_user_completions (task_id, user_id, status, completed_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(task_id, user_id) DO UPDATE SET status=excluded.status, completed_at=excluded.completed_at",
                (task_id, user_id, status)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("record_task_user_completion: %s", e)
            return False

    def get_task_user_completions(self, task_id: int) -> list:
        """Список персональных выполнений задачи (для командных задач)."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                "SELECT tuc.user_id, tuc.status, tuc.completed_at, "
                "u.first_name, u.last_name, u.username "
                "FROM task_user_completions tuc "
                "LEFT JOIN users u ON u.id = tuc.user_id "
                "WHERE tuc.task_id = ? ORDER BY tuc.completed_at",
                (task_id,)
            ).fetchall()
            conn.close()
            return [
                {
                    "user_id": r[0], "status": r[1], "completed_at": r[2],
                    "name": f"{r[3] or ''} {r[4] or ''}".strip() or r[5] or f"id={r[0]}"
                }
                for r in rows
            ]
        except Exception as e:
            logger.error("get_task_user_completions: %s", e)
            return []

    def get_task_user_completion(self, task_id: int, user_id: int) -> dict | None:
        """Проверить, выполнил ли конкретный пользователь командную задачу."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT status, completed_at FROM task_user_completions WHERE task_id=? AND user_id=?",
                (task_id, user_id)
            ).fetchone()
            conn.close()
            if not row:
                return None
            return {"status": row[0], "completed_at": row[1]}
        except Exception as e:
            logger.error("get_task_user_completion: %s", e)
            return None

    def toggle_task_checklist_item(self, item_id: int,
                                   done_by: int | None = None) -> bool:
        """Переключить is_done у пункта чеклиста."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT is_done FROM task_checklist WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                conn.close()
                return False
            new_done = 0 if row[0] else 1
            if new_done:
                conn.execute(
                    "UPDATE task_checklist SET is_done=1, done_by=?, done_at=datetime('now') WHERE id=?",
                    (done_by, item_id)
                )
            else:
                conn.execute(
                    "UPDATE task_checklist SET is_done=0, done_by=NULL, done_at=NULL WHERE id=?",
                    (item_id,)
                )
            conn.commit()
            conn.close()
            return bool(new_done)
        except Exception as e:
            logger.error("toggle_task_checklist_item: %s", e)
            return False

    def get_open_tasks_count(self, user_id: int, is_admin: bool = False) -> int:
        """Счётчик незакрытых задач для сайдбара (assigned + created, не done/cancelled)."""
        try:
            conn = self.get_connection()
            if is_admin:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','cancelled')"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','cancelled') "
                    "AND (assigned_to = ? OR created_by = ?)",
                    (user_id, user_id)
                ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception as e:
            logger.error("get_open_tasks_count: %s", e)
            return 0

    def get_tasks_analytics(self) -> dict:
        """Агрегированная статистика задач для дашборда аналитики."""
        empty = {
            'total': 0, 'by_status': {}, 'overdue': 0,
            'by_topic': [], 'by_assignee': [], 'by_priority': [],
            'created_daily': [], 'completed_daily': [],
            'avg_rating': None, 'rated_count': 0, 'avg_time_to_done': None,
        }
        try:
            conn = self.get_connection()
            try:
                by_status_rows = conn.execute(
                    "SELECT status, COUNT(*) FROM tasks WHERE is_active=1 GROUP BY status"
                ).fetchall()
                by_status = {r[0]: r[1] for r in by_status_rows}
                total = sum(by_status.values())

                overdue = conn.execute(
                    """SELECT COUNT(*) FROM tasks
                       WHERE is_active=1 AND status NOT IN ('done','cancelled')
                         AND deadline IS NOT NULL AND deadline < datetime('now')"""
                ).fetchone()[0]

                topic_rows = conn.execute(
                    """SELECT COALESCE(tt.name,'Без темы'), COUNT(t.id)
                       FROM tasks t LEFT JOIN task_topics tt ON t.topic_id=tt.id
                       WHERE t.is_active=1 GROUP BY COALESCE(tt.name,'Без темы')
                       ORDER BY 2 DESC LIMIT 10"""
                ).fetchall()

                assignee_rows = conn.execute(
                    """SELECT COALESCE(u.first_name||CASE WHEN u.last_name IS NOT NULL THEN ' '||u.last_name ELSE '' END, 'Без назначения'),
                              COUNT(t.id)
                       FROM tasks t LEFT JOIN users u ON t.assigned_to=u.id
                       WHERE t.is_active=1 GROUP BY t.assigned_to ORDER BY 2 DESC LIMIT 10"""
                ).fetchall()

                priority_rows = conn.execute(
                    "SELECT priority, COUNT(*) FROM tasks WHERE is_active=1 GROUP BY priority ORDER BY 2 DESC"
                ).fetchall()

                created_rows = conn.execute(
                    """SELECT DATE(created_at), COUNT(*) FROM tasks
                       WHERE is_active=1 AND created_at >= datetime('now','-30 days')
                       GROUP BY DATE(created_at) ORDER BY 1"""
                ).fetchall()

                completed_rows = conn.execute(
                    """SELECT DATE(updated_at), COUNT(*) FROM tasks
                       WHERE is_active=1 AND status='done'
                         AND updated_at >= datetime('now','-30 days')
                       GROUP BY DATE(updated_at) ORDER BY 1"""
                ).fetchall()

                rating_row = conn.execute(
                    "SELECT AVG(CAST(rating AS REAL)), COUNT(*) FROM tasks WHERE is_active=1 AND rating IS NOT NULL"
                ).fetchone()

                avg_time_row = conn.execute(
                    """SELECT AVG(CAST((julianday(updated_at)-julianday(created_at))*24 AS REAL))
                       FROM tasks WHERE is_active=1 AND status='done'
                         AND updated_at IS NOT NULL AND created_at IS NOT NULL"""
                ).fetchone()

                return {
                    'total': total,
                    'by_status': by_status,
                    'overdue': overdue,
                    'by_topic': [(r[0], r[1]) for r in topic_rows],
                    'by_assignee': [(r[0], r[1]) for r in assignee_rows],
                    'by_priority': [(r[0], r[1]) for r in priority_rows],
                    'created_daily': [(r[0], r[1]) for r in created_rows],
                    'completed_daily': [(r[0], r[1]) for r in completed_rows],
                    'avg_rating': round(rating_row[0], 1) if rating_row[0] else None,
                    'rated_count': rating_row[1] or 0,
                    'avg_time_to_done': round(avg_time_row[0], 1) if avg_time_row and avg_time_row[0] else None,
                }
            finally:
                conn.close()
        except Exception as e:
            logger.error("get_tasks_analytics: %s", e)
            return empty

    def get_unassigned_tasks(self, topic_id: int | None = None, q: str | None = None) -> list:
        """Задачи без назначения (пул) — assign_all=0, assigned_to IS NULL, assigned_shop IS NULL, status=open."""
        try:
            conn = self.get_connection()
            where = [
                "t.status = 'open'",
                "t.assign_all = 0",
                "t.assigned_to IS NULL",
                "t.assigned_shop IS NULL",
            ]
            params: list = []
            if topic_id:
                where.append("t.topic_id = ?")
                params.append(topic_id)
            if q:
                where.append("(lower_u(t.title) LIKE lower_u(?) OR lower_u(t.description) LIKE lower_u(?))")
                like = f"%{q}%"
                params += [like, like]
            where_sql = " AND ".join(where)
            rows = conn.execute(
                f"""SELECT t.id, t.title, t.description, t.priority, t.deadline,
                           t.created_at, tt.name AS topic_name, tt.color AS topic_color,
                           uc.first_name AS c_fn, uc.last_name AS c_ln,
                           (SELECT COUNT(*) FROM task_checklist cl WHERE cl.task_id = t.id) AS cl_total
                    FROM tasks t
                    LEFT JOIN task_topics tt ON tt.id = t.topic_id
                    LEFT JOIN users uc ON uc.id = t.created_by
                    WHERE {where_sql}
                    ORDER BY
                      CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                      t.deadline ASC NULLS LAST, t.created_at DESC""",
                params
            ).fetchall()
            conn.close()
            result = []
            for r in rows:
                result.append({
                    'id': r[0], 'title': r[1], 'description': r[2] or '',
                    'priority': r[3], 'deadline': r[4] or '',
                    'created_at': r[5] or '',
                    'topic_name': r[6] or '', 'topic_color': r[7] or 'blue',
                    'creator': ' '.join(filter(None, [r[8], r[9]])) or '',
                    'checklist_total': r[10] or 0,
                })
            return result
        except Exception as e:
            logger.error("get_unassigned_tasks: %s", e)
            return []

    def self_assign_task(self, task_id: int, user_db_id: int) -> bool:
        """Назначить задачу из пула на себя. Возвращает True если успешно (задача была свободна)."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                """UPDATE tasks SET assigned_to = ?, updated_at = datetime('now')
                   WHERE id = ? AND assign_all = 0 AND assigned_to IS NULL
                     AND assigned_shop IS NULL AND status = 'open'""",
                (user_db_id, task_id)
            )
            changed = cur.rowcount > 0
            if changed:
                conn.execute(
                    """INSERT INTO task_history (task_id, user_id, action, old_val, new_val)
                       VALUES (?, ?, 'self_assign', NULL, ?)""",
                    (task_id, user_db_id, str(user_db_id))
                )
            conn.commit()
            conn.close()
            return changed
        except Exception as e:
            logger.error("self_assign_task: %s", e)
            return False

    def create_auto_low_stock_task(self, product_name: str, shop_name: str,
                                    quantity: int, created_by_user_id: int = 0) -> int:
        """Создать авто-задачу «Пополнить остаток» с anti-duplicate за 7 дней.
        Возвращает task_id или 0 если задача уже существует."""
        try:
            conn = self.get_connection()
            title = f"📦 Пополнить остаток: {product_name}"
            if shop_name:
                title += f" ({shop_name})"
            title = title[:200]
            # Anti-duplicate: open task с тем же названием за последние 7 дней
            dup = conn.execute(
                """SELECT id FROM tasks
                   WHERE title = ?
                     AND status NOT IN ('done', 'cancelled')
                     AND created_at >= datetime('now', '-7 days')
                   LIMIT 1""",
                (title,)
            ).fetchone()
            if dup:
                conn.close()
                return 0
            description = (
                f"Автоматически создано: остаток товара «{product_name}» "
                f"в магазине «{shop_name}» составляет {quantity} шт.\n"
                f"Пополните запасы как можно скорее."
            )
            cur = conn.execute(
                """INSERT INTO tasks
                       (title, description, priority, created_by, assign_all,
                        created_at)
                   VALUES (?, ?, 'high', ?, 1, datetime('now'))""",
                (title, description, created_by_user_id)
            )
            task_id = cur.lastrowid
            conn.commit()
            conn.close()
            return task_id
        except Exception as e:
            logger.error("create_auto_low_stock_task: %s", e)
            return 0

    def create_task_template(self, title: str, description: str = '',
                              priority: str = 'normal', checklist_json: str = '[]',
                              topic_id: int | None = None, created_by: int | None = None) -> int | None:
        """Создать шаблон задачи. Возвращает id."""
        import json as _json
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO task_templates (title, description, priority, checklist_json, topic_id, created_by)
                   VALUES (?,?,?,?,?,?)""",
                (title[:200], description[:2000], priority,
                 checklist_json if checklist_json else '[]', topic_id, created_by)
            )
            conn.commit()
            return cur.lastrowid
        except Exception as e:
            logger.error("create_task_template: %s", e)
            return None
        finally:
            try: conn.close()
            except: pass

    def get_task_templates(self) -> list:
        """Список всех шаблонов задач."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """SELECT tt.id, tt.title, tt.description, tt.priority, tt.checklist_json,
                          tt.topic_id, top.name as topic_name, tt.created_at
                   FROM task_templates tt
                   LEFT JOIN task_topics top ON top.id = tt.topic_id
                   ORDER BY tt.created_at DESC"""
            ).fetchall()
            conn.close()
            import json as _json
            result = []
            for r in rows:
                try:
                    checklist = _json.loads(r[4] or '[]')
                except Exception:
                    checklist = []
                result.append({
                    'id': r[0], 'title': r[1], 'description': r[2] or '',
                    'priority': r[3], 'checklist': checklist,
                    'topic_id': r[5], 'topic_name': r[6] or '',
                    'created_at': r[7] or '',
                })
            return result
        except Exception as e:
            logger.error("get_task_templates: %s", e)
            return []

    def get_task_template(self, template_id: int) -> dict | None:
        """Одиночный шаблон задачи по id."""
        try:
            conn = self.get_connection()
            r = conn.execute(
                """SELECT id, title, description, priority, checklist_json, topic_id, created_at
                   FROM task_templates WHERE id=?""",
                (template_id,)
            ).fetchone()
            conn.close()
            if not r:
                return None
            import json as _json
            try:
                checklist = _json.loads(r[4] or '[]')
            except Exception:
                checklist = []
            return {
                'id': r[0], 'title': r[1], 'description': r[2] or '',
                'priority': r[3], 'checklist': checklist,
                'topic_id': r[5], 'created_at': r[6] or '',
            }
        except Exception as e:
            logger.error("get_task_template: %s", e)
            return None

    def delete_task_template(self, template_id: int) -> bool:
        """Удалить шаблон задачи."""
        try:
            conn = self.get_connection()
            conn.execute("DELETE FROM task_templates WHERE id=?", (template_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error("delete_task_template: %s", e)
            return False

    def get_tasks_with_deadline_today(self) -> list:
        """Задачи с дедлайном сегодня (для APScheduler напоминаний)."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """
                SELECT t.id, t.title, t.assigned_to, t.created_by,
                       ua.telegram_id AS assigned_tg,
                       uc.telegram_id AS creator_tg
                FROM tasks t
                LEFT JOIN users ua ON ua.id = t.assigned_to
                LEFT JOIN users uc ON uc.id = t.created_by
                WHERE t.deadline = date('now')
                  AND t.status NOT IN ('done', 'cancelled')
                """
            ).fetchall()
            conn.close()
            return [
                {"id": r[0], "title": r[1], "assigned_to": r[2],
                 "created_by": r[3], "assigned_tg": r[4], "creator_tg": r[5]}
                for r in rows
            ]
        except Exception as e:
            logger.error("get_tasks_with_deadline_today: %s", e)
            return []

    def get_overdue_tasks(self) -> list:
        """Просроченные незавершённые задачи (дедлайн < сегодня)."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """
                SELECT t.id, t.title, t.assigned_to, t.created_by,
                       ua.telegram_id AS assigned_tg,
                       uc.telegram_id AS creator_tg,
                       t.deadline
                FROM tasks t
                LEFT JOIN users ua ON ua.id = t.assigned_to
                LEFT JOIN users uc ON uc.id = t.created_by
                WHERE t.deadline < date('now')
                  AND t.status NOT IN ('done', 'cancelled')
                """
            ).fetchall()
            conn.close()
            return [
                {"id": r[0], "title": r[1], "assigned_to": r[2],
                 "created_by": r[3], "assigned_tg": r[4], "creator_tg": r[5],
                 "deadline": r[6]}
                for r in rows
            ]
        except Exception as e:
            logger.error("get_overdue_tasks: %s", e)
            return []

    # ── web_credentials methods (shop_bot.db only) ───────────────────────────

    def _wc_row(self, row) -> dict | None:
        if not row:
            return None
        keys = ('id', 'email', 'password_hash', 'telegram_id', 'synthetic_tg_id',
                'org_db', 'first_name', 'email_verified', 'verify_token',
                'verify_expires', 'reset_token', 'reset_expires', 'created_at', 'last_login')
        return dict(zip(keys, row))

    def create_web_credential(self, email: str, password_hash: str,
                              telegram_id: int | None = None) -> int | None:
        try:
            conn = self.get_connection()
            cur = conn.execute(
                "INSERT INTO web_credentials (email, password_hash, telegram_id) VALUES (?, ?, ?)",
                (email.lower().strip(), password_hash, telegram_id)
            )
            conn.commit()
            return cur.lastrowid
        except Exception as exc:
            logger.error("create_web_credential: %s", exc)
            return None

    def get_web_credential_by_email(self, email: str) -> dict | None:
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT * FROM web_credentials WHERE email=? COLLATE NOCASE LIMIT 1",
                (email.strip(),)
            ).fetchone()
            return self._wc_row(row)
        except Exception as exc:
            logger.error("get_web_credential_by_email: %s", exc)
            return None

    def get_web_credential_by_telegram_id(self, telegram_id: int) -> dict | None:
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT * FROM web_credentials WHERE telegram_id=? LIMIT 1",
                (telegram_id,)
            ).fetchone()
            return self._wc_row(row)
        except Exception as exc:
            logger.error("get_web_credential_by_telegram_id: %s", exc)
            return None

    def get_web_credential_by_id(self, cred_id: int) -> dict | None:
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT * FROM web_credentials WHERE id=? LIMIT 1", (cred_id,)
            ).fetchone()
            return self._wc_row(row)
        except Exception as exc:
            logger.error("get_web_credential_by_id: %s", exc)
            return None

    def set_web_synthetic_tg_id(self, cred_id: int, synthetic_tg_id: int,
                                 org_db: str, first_name: str) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE web_credentials SET synthetic_tg_id=?, org_db=?, first_name=? WHERE id=?",
                (synthetic_tg_id, org_db, first_name, cred_id)
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("set_web_synthetic_tg_id: %s", exc)
            return False

    def set_web_verify_token(self, cred_id: int, token: str, expires: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE web_credentials SET verify_token=?, verify_expires=? WHERE id=?",
                (token, expires, cred_id)
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("set_web_verify_token: %s", exc)
            return False

    def verify_web_email_token(self, token: str) -> int | None:
        """Mark email as verified if token valid and not expired. Returns cred_id."""
        import time
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT id, verify_expires FROM web_credentials WHERE verify_token=?",
                (token,)
            ).fetchone()
            if not row:
                return None
            cred_id, expires = row
            if expires and int(time.time()) > expires:
                return None
            conn.execute(
                "UPDATE web_credentials SET email_verified=1, verify_token=NULL, verify_expires=NULL WHERE id=?",
                (cred_id,)
            )
            conn.commit()
            return cred_id
        except Exception as exc:
            logger.error("verify_web_email_token: %s", exc)
            return None

    def set_web_reset_token(self, email: str, token: str, expires: int) -> bool:
        try:
            conn = self.get_connection()
            result = conn.execute(
                "UPDATE web_credentials SET reset_token=?, reset_expires=? "
                "WHERE email=? COLLATE NOCASE",
                (token, expires, email.strip())
            )
            conn.commit()
            return result.rowcount > 0
        except Exception as exc:
            logger.error("set_web_reset_token: %s", exc)
            return False

    def verify_web_reset_token(self, token: str) -> dict | None:
        """Return credential dict if reset token is valid and not expired."""
        import time
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT * FROM web_credentials WHERE reset_token=?", (token,)
            ).fetchone()
            if not row:
                return None
            cred = self._wc_row(row)
            if cred['reset_expires'] and int(time.time()) > cred['reset_expires']:
                return None
            return cred
        except Exception as exc:
            logger.error("verify_web_reset_token: %s", exc)
            return None

    def reset_web_password(self, token: str, new_hash: str) -> bool:
        """Reset password using reset token. Clears token after use."""
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE web_credentials SET password_hash=?, reset_token=NULL, reset_expires=NULL "
                "WHERE reset_token=?",
                (new_hash, token)
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("reset_web_password: %s", exc)
            return False

    def update_web_password(self, cred_id: int, new_hash: str) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE web_credentials SET password_hash=? WHERE id=?",
                (new_hash, cred_id)
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("update_web_password: %s", exc)
            return False

    def update_web_credential_email(self, cred_id: int, email: str,
                                    password_hash: str | None = None) -> bool:
        try:
            conn = self.get_connection()
            if password_hash:
                conn.execute(
                    "UPDATE web_credentials SET email=?, password_hash=?, "
                    "email_verified=0, verify_token=NULL, verify_expires=NULL WHERE id=?",
                    (email, password_hash, cred_id)
                )
            else:
                conn.execute(
                    "UPDATE web_credentials SET email=?, "
                    "email_verified=0, verify_token=NULL, verify_expires=NULL WHERE id=?",
                    (email, cred_id)
                )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("update_web_credential_email: %s", exc)
            return False

    def update_web_last_login(self, cred_id: int) -> None:
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE web_credentials SET last_login=datetime('now') WHERE id=?",
                (cred_id,)
            )
            conn.commit()
        except Exception as exc:
            logger.error("update_web_last_login: %s", exc)

    def link_web_credential_to_telegram(self, email: str, telegram_id: int) -> str:
        """Link an email credential to a real Telegram account.
        Returns: 'ok' | 'already_linked' | 'not_found' | 'error'
        """
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT id, telegram_id FROM web_credentials WHERE email=? COLLATE NOCASE LIMIT 1",
                (email.strip().lower(),)
            ).fetchone()
            if not row:
                return 'not_found'
            cred_id = row['id']
            existing_tg = row['telegram_id']
            if existing_tg and existing_tg > 0:
                return 'ok' if existing_tg == telegram_id else 'already_linked'
            conn.execute(
                "UPDATE web_credentials SET telegram_id=? WHERE id=?",
                (telegram_id, cred_id)
            )
            conn.commit()
            return 'ok'
        except Exception as exc:
            logger.error("link_web_credential_to_telegram: %s", exc)
            return 'error'

    def get_all_web_credentials(self) -> list:
        """Return all email-registered users for super_admin overview."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                "SELECT id, email, first_name, telegram_id, synthetic_tg_id, "
                "email_verified, last_login, created_at, org_db "
                "FROM web_credentials ORDER BY created_at DESC"
            ).fetchall()
            _cols = ["id", "email", "first_name", "telegram_id", "synthetic_tg_id",
                     "email_verified", "last_login", "created_at", "org_db"]
            return [dict(zip(_cols, r)) for r in rows]
        except Exception as exc:
            logger.error("get_all_web_credentials: %s", exc)
            return []

    def delete_web_credential_by_id(self, cred_id: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute("DELETE FROM web_credentials WHERE id=?", (cred_id,))
            conn.commit()
            return True
        except Exception as exc:
            logger.error("delete_web_credential_by_id: %s", exc)
            return False

    # ── push_subscriptions methods (shop_bot.db only) ─────────────────────────

    def save_push_subscription(self, user_id: int, endpoint: str, p256dh: str, auth: str) -> bool:
        """Upsert a browser push subscription for user_id."""
        try:
            conn = self.get_connection()
            conn.execute(
                """
                INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, endpoint) DO UPDATE SET
                    p256dh = excluded.p256dh,
                    auth   = excluded.auth
                """,
                (user_id, endpoint, p256dh, auth),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("save_push_subscription: %s", exc)
            return False

    def delete_push_subscription(self, user_id: int, endpoint: str) -> bool:
        """Remove a specific push subscription (e.g. user unsubscribed)."""
        try:
            conn = self.get_connection()
            conn.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                (user_id, endpoint),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("delete_push_subscription: %s", exc)
            return False

    def delete_all_push_subscriptions(self, user_id: int) -> bool:
        """Remove all push subscriptions for user_id."""
        try:
            conn = self.get_connection()
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
            conn.commit()
            return True
        except Exception as exc:
            logger.error("delete_all_push_subscriptions: %s", exc)
            return False

    def get_push_subscriptions(self, user_id: int) -> list:
        """Return list of {endpoint, p256dh, auth} for user_id."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            conn.close()
            return [{"endpoint": r[0], "p256dh": r[1], "auth": r[2]} for r in rows]
        except Exception as exc:
            logger.error("get_push_subscriptions: %s", exc)
            return []

    # ── push_delivery_log methods (shop_bot.db only) ──────────────────────────

    def log_push_delivery(self, user_id: int, title: str) -> bool:
        """Record that a push notification was successfully sent to user_id."""
        try:
            conn = self.get_connection()
            conn.execute(
                "INSERT INTO push_delivery_log (user_id, title) VALUES (?, ?)",
                (user_id, title or ""),
            )
            conn.execute(
                """DELETE FROM push_delivery_log
                   WHERE user_id = ?
                     AND id NOT IN (
                         SELECT id FROM push_delivery_log
                         WHERE user_id = ?
                         ORDER BY sent_at DESC
                         LIMIT 20
                     )""",
                (user_id, user_id),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("log_push_delivery: %s", exc)
            return False

    def get_recent_push_deliveries(self, user_id: int, limit: int = 3) -> list:
        """Return up to `limit` most recent push events for user_id.

        Each row: {"title": str, "sent_at": str} — sent_at is a UTC ISO string.
        """
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """SELECT title, sent_at FROM push_delivery_log
                   WHERE user_id = ?
                   ORDER BY sent_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [{"title": r[0], "sent_at": r[1]} for r in rows]
        except Exception as exc:
            logger.error("get_recent_push_deliveries: %s", exc)
            return []

    # ── AI smart alert settings ───────────────────────────────────────────────

    def get_ai_alert_settings(self) -> dict:
        """Return AI alert settings dict with defaults."""
        import json as _json
        defaults = {
            "enabled": True,
            "threshold_pct": 35,
            "alert_hour_msk": 10,
            "metrics": ["revenue"],
            "digest_context": ["products", "sellers", "plans"],
            "digest_enabled": True,
            "digest_day_of_week": 0,
            "digest_hour_msk": 9,
            "digest_push_enabled": True,
            "alert_push_enabled": True,
            "alert_email_enabled": False,
            "digest_email_enabled": False,
        }
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT enabled, threshold_pct, alert_hour_msk, metrics, digest_context,"
                " digest_enabled, digest_day_of_week, digest_hour_msk, digest_push_enabled,"
                " alert_push_enabled, alert_email_enabled, digest_email_enabled"
                " FROM ai_alert_settings WHERE id = 1"
            ).fetchone()
            conn.close()
            if row:
                return {
                    "enabled": bool(row[0]),
                    "threshold_pct": int(row[1] or 35),
                    "alert_hour_msk": int(row[2] or 10),
                    "metrics": _json.loads(row[3] or '["revenue"]'),
                    "digest_context": _json.loads(row[4] or '["products","sellers","plans"]'),
                    "digest_enabled": bool(row[5] if row[5] is not None else 1),
                    "digest_day_of_week": int(row[6] or 0),
                    "digest_hour_msk": int(row[7] if row[7] is not None else 9),
                    "digest_push_enabled": bool(row[8] if row[8] is not None else 1),
                    "alert_push_enabled": bool(row[9] if row[9] is not None else 1),
                    "alert_email_enabled": bool(row[10] if row[10] is not None else 0),
                    "digest_email_enabled": bool(row[11] if row[11] is not None else 0),
                }
            return defaults
        except Exception as exc:
            logger.error("get_ai_alert_settings: %s", exc)
            return defaults

    def save_ai_alert_settings(
        self,
        enabled: bool,
        threshold_pct: int,
        alert_hour_msk: int,
        metrics: list,
        digest_context: list | None = None,
        digest_enabled: bool = True,
        digest_day_of_week: int = 0,
        digest_hour_msk: int = 9,
        digest_push_enabled: bool = True,
        alert_push_enabled: bool = True,
        alert_email_enabled: bool = False,
        digest_email_enabled: bool = False,
    ) -> bool:
        import json as _json
        if digest_context is None:
            digest_context = ["products", "sellers", "plans"]
        try:
            conn = self.get_connection()
            conn.execute(
                """INSERT INTO ai_alert_settings
                       (id, enabled, threshold_pct, alert_hour_msk, metrics, digest_context, updated_at,
                        digest_enabled, digest_day_of_week, digest_hour_msk, digest_push_enabled,
                        alert_push_enabled, alert_email_enabled, digest_email_enabled)
                   VALUES (1, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       enabled              = excluded.enabled,
                       threshold_pct        = excluded.threshold_pct,
                       alert_hour_msk       = excluded.alert_hour_msk,
                       metrics              = excluded.metrics,
                       digest_context       = excluded.digest_context,
                       updated_at           = excluded.updated_at,
                       digest_enabled       = excluded.digest_enabled,
                       digest_day_of_week   = excluded.digest_day_of_week,
                       digest_hour_msk      = excluded.digest_hour_msk,
                       digest_push_enabled  = excluded.digest_push_enabled,
                       alert_push_enabled   = excluded.alert_push_enabled,
                       alert_email_enabled  = excluded.alert_email_enabled,
                       digest_email_enabled = excluded.digest_email_enabled""",
                (int(enabled), int(threshold_pct), int(alert_hour_msk), _json.dumps(metrics),
                 _json.dumps(digest_context), int(digest_enabled), int(digest_day_of_week), int(digest_hour_msk),
                 int(digest_push_enabled), int(alert_push_enabled),
                 int(alert_email_enabled), int(digest_email_enabled)),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("save_ai_alert_settings: %s", exc)
            return False

    def add_ai_alert_log(self, alert_type: str, text: str) -> None:
        """Append a smart-alert or digest entry to ai_alerts_log; keep last 50 rows."""
        try:
            conn = self.get_connection()
            conn.execute(
                "INSERT INTO ai_alerts_log (alert_type, text) VALUES (?, ?)",
                (alert_type, (text or "")[:2000]),
            )
            conn.execute(
                "DELETE FROM ai_alerts_log WHERE id NOT IN "
                "(SELECT id FROM ai_alerts_log ORDER BY id DESC LIMIT 50)"
            )
            conn.commit()
        except Exception as exc:
            logger.error("add_ai_alert_log: %s", exc)

    def get_ai_alerts_log(self, limit: int = 10) -> list:
        """Return the latest AI alert/digest entries, newest first."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                "SELECT id, alert_type, text, created_at FROM ai_alerts_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            return [
                {"id": r[0], "type": r[1], "text": r[2], "created_at": (r[3] or "")[:16].replace("T", " ")}
                for r in rows
            ]
        except Exception as exc:
            logger.error("get_ai_alerts_log: %s", exc)
            return []

    # ── AI weekly digest preferences (shop_bot.db) ────────────────────────────

    def get_network_digest_prefs(self, tg_id: int) -> dict:
        """Return weekly AI digest delivery prefs for a network owner. Defaults: Mon, 12:00 MSK."""
        defaults = {"weekday": 0, "hour_msk": 12}
        try:
            conn = self.get_connection()
            row = conn.execute(
                "SELECT weekday, hour_msk FROM ai_network_digest_prefs WHERE tg_id = ?",
                (int(tg_id),)
            ).fetchone()
            conn.close()
            if row:
                return {"weekday": int(row[0]), "hour_msk": int(row[1])}
            return defaults
        except Exception as exc:
            logger.error("get_network_digest_prefs: %s", exc)
            return defaults

    def save_network_digest_prefs(self, tg_id: int, weekday: int, hour_msk: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                """INSERT INTO ai_network_digest_prefs (tg_id, weekday, hour_msk, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(tg_id) DO UPDATE SET
                       weekday    = excluded.weekday,
                       hour_msk   = excluded.hour_msk,
                       updated_at = excluded.updated_at""",
                (int(tg_id), int(weekday), int(hour_msk)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error("save_network_digest_prefs: %s", exc)
            return False

    # ── BILLING SYSTEM ────────────────────────────────────────────────────────

    def get_all_billing_modules(self) -> list:
        """Все модули биллинга, сортированные по sort_order."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id,key,name,icon,description,price_monthly,sort_order,is_active,features_json '
                'FROM billing_modules ORDER BY sort_order,id'
            )
            rows = cursor.fetchall()
            conn.close()
            return [
                {'id': r[0], 'key': r[1], 'name': r[2], 'icon': r[3], 'description': r[4],
                 'price_monthly': r[5], 'sort_order': r[6], 'is_active': bool(r[7]),
                 'features_json': r[8] or '[]'}
                for r in rows
            ]
        except Exception as exc:
            logger.error('get_all_billing_modules: %s', exc)
            return []

    def upsert_billing_module(
        self, key: str, name: str, icon: str, description: str,
        price_monthly: float, sort_order: int = 0,
        is_active: int = 1, features_json: str = '[]'
    ) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                '''INSERT INTO billing_modules (key,name,icon,description,price_monthly,sort_order,is_active,features_json,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET
                       name=excluded.name, icon=excluded.icon, description=excluded.description,
                       price_monthly=excluded.price_monthly, sort_order=excluded.sort_order,
                       is_active=excluded.is_active, features_json=excluded.features_json,
                       updated_at=datetime('now')''',
                (key, name, icon, description, price_monthly, sort_order, is_active, features_json)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('upsert_billing_module: %s', exc)
            return False

    def toggle_billing_module(self, module_id: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE billing_modules SET is_active=1-is_active, updated_at=datetime('now') WHERE id=?",
                (module_id,)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('toggle_billing_module: %s', exc)
            return False

    def delete_billing_module(self, module_id: int) -> bool:
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute('SELECT key FROM billing_modules WHERE id=?', (module_id,))
            row = cur.fetchone()
            if row:
                conn.execute('DELETE FROM billing_extensions WHERE module_key=?', (row[0],))
            conn.execute('DELETE FROM billing_modules WHERE id=?', (module_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('delete_billing_module: %s', exc)
            return False

    def get_all_billing_extensions(self, module_key: str = None) -> list:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            if module_key:
                cursor.execute(
                    'SELECT id,module_key,key,name,icon,description,price_monthly,sort_order,is_active '
                    'FROM billing_extensions WHERE module_key=? ORDER BY sort_order,id',
                    (module_key,)
                )
            else:
                cursor.execute(
                    'SELECT id,module_key,key,name,icon,description,price_monthly,sort_order,is_active '
                    'FROM billing_extensions ORDER BY module_key,sort_order,id'
                )
            rows = cursor.fetchall()
            conn.close()
            return [
                {'id': r[0], 'module_key': r[1], 'key': r[2], 'name': r[3], 'icon': r[4],
                 'description': r[5], 'price_monthly': r[6], 'sort_order': r[7], 'is_active': bool(r[8])}
                for r in rows
            ]
        except Exception as exc:
            logger.error('get_all_billing_extensions: %s', exc)
            return []

    def upsert_billing_extension(
        self, module_key: str, key: str, name: str, icon: str,
        description: str, price_monthly: float,
        sort_order: int = 0, is_active: int = 1
    ) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                '''INSERT INTO billing_extensions (module_key,key,name,icon,description,price_monthly,sort_order,is_active,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(module_key,key) DO UPDATE SET
                       name=excluded.name, icon=excluded.icon,
                       description=excluded.description, price_monthly=excluded.price_monthly,
                       sort_order=excluded.sort_order, is_active=excluded.is_active, updated_at=datetime('now')''',
                (module_key, key, name, icon, description, price_monthly, sort_order, is_active)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('upsert_billing_extension: %s', exc)
            return False

    def toggle_billing_extension(self, ext_id: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE billing_extensions SET is_active=1-is_active, updated_at=datetime('now') WHERE id=?",
                (ext_id,)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('toggle_billing_extension: %s', exc)
            return False

    def delete_billing_extension(self, ext_id: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute('DELETE FROM billing_extensions WHERE id=?', (ext_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('delete_billing_extension: %s', exc)
            return False

    def get_all_billing_bundles(self) -> list:
        """Возвращает пакеты с предпарсенным полем includes."""
        import json as _j
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id,key,name,icon,description,includes_json,price_monthly,sort_order,is_active '
                'FROM billing_bundles ORDER BY sort_order,id'
            )
            rows = cursor.fetchall()
            conn.close()
            result = []
            for r in rows:
                try:
                    includes = _j.loads(r[5] or '{}')
                except Exception:
                    includes = {'modules': [], 'extensions': []}
                result.append({
                    'id': r[0], 'key': r[1], 'name': r[2], 'icon': r[3],
                    'description': r[4], 'includes_json': r[5],
                    'includes': includes,
                    'price_monthly': r[6], 'sort_order': r[7], 'is_active': bool(r[8])
                })
            return result
        except Exception as exc:
            logger.error('get_all_billing_bundles: %s', exc)
            return []

    def upsert_billing_bundle(
        self, key: str, name: str, icon: str, description: str,
        includes_json: str, price_monthly: float,
        sort_order: int = 0, is_active: int = 1
    ) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                '''INSERT INTO billing_bundles (key,name,icon,description,includes_json,price_monthly,sort_order,is_active,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET
                       name=excluded.name, icon=excluded.icon, description=excluded.description,
                       includes_json=excluded.includes_json, price_monthly=excluded.price_monthly,
                       sort_order=excluded.sort_order, is_active=excluded.is_active, updated_at=datetime('now')''',
                (key, name, icon, description, includes_json, price_monthly, sort_order, is_active)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('upsert_billing_bundle: %s', exc)
            return False

    def toggle_billing_bundle(self, bundle_id: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute(
                "UPDATE billing_bundles SET is_active=1-is_active, updated_at=datetime('now') WHERE id=?",
                (bundle_id,)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('toggle_billing_bundle: %s', exc)
            return False

    def delete_billing_bundle(self, bundle_id: int) -> bool:
        try:
            conn = self.get_connection()
            conn.execute('DELETE FROM billing_bundles WHERE id=?', (bundle_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            logger.error('delete_billing_bundle: %s', exc)
            return False

    def grant_billing_item(
        self,
        user_telegram_id: int,
        item_type: str,
        item_key: str,
        duration_days: int = 0,
        price_paid: float = 0.0,
        granted_by: str = 'admin_grant',
        note: str = '',
        payment_request_id: int = None,
    ) -> int:
        """Выдать модуль/расширение/пакет. duration_days=0 → бессрочно. Возвращает id строки."""
        try:
            from datetime import datetime as _dt, timedelta as _td
            start = _dt.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            end = None
            if duration_days and duration_days > 0:
                end = (_dt.utcnow() + _td(days=duration_days)).strftime('%Y-%m-%d %H:%M:%S')
            conn = self.get_connection()
            cur = conn.cursor()
            # Deactivate existing active rows for same user+item (renewal / re-grant)
            cur.execute(
                """UPDATE billing_module_subs SET is_active=0
                   WHERE user_telegram_id=? AND item_key=? AND is_active=1""",
                (user_telegram_id, item_key)
            )
            cur.execute(
                '''INSERT INTO billing_module_subs
                   (user_telegram_id,item_type,item_key,price_paid,start_date,end_date,
                    is_active,payment_request_id,granted_by,note,created_at)
                   VALUES (?,?,?,?,?,?,1,?,?,?,datetime('now'))''',
                (user_telegram_id, item_type, item_key, price_paid,
                 start, end, payment_request_id, granted_by, note)
            )
            sub_id = cur.lastrowid
            conn.commit()
            conn.close()
            return sub_id or 0
        except Exception as exc:
            logger.error('grant_billing_item: %s', exc)
            return 0

    def revoke_billing_item(self, sub_id: int, cascade: bool = True) -> dict:
        """Отзыв доступа.

        Логика:
        - Если end_date в будущем → устанавливаем end_date = now() (soft-expire:
          запись остаётся, история сохраняется, доступ закрывается немедленно).
        - Если end_date в прошлом или NULL → is_active=0 (hard deactivate).
        - cascade=True и item_type='module' → каскадно отзываем все расширения
          этого модуля у того же пользователя.

        Возвращает {'revoked': 1, 'cascaded': N}
        """
        result = {'revoked': 0, 'cascaded': 0}
        try:
            from datetime import datetime as _dt
            conn = self.get_connection()
            cur = conn.cursor()

            row = cur.execute(
                'SELECT user_telegram_id, item_type, item_key, end_date, is_active '
                'FROM billing_module_subs WHERE id=?',
                (sub_id,)
            ).fetchone()
            if not row:
                conn.close()
                return result
            tg_id, item_type, item_key, end_date, is_active = row
            if not is_active:
                conn.close()
                return result

            def _has_future_end(ed):
                if not ed:
                    return False
                try:
                    return _dt.strptime(ed[:19], '%Y-%m-%d %H:%M:%S') > _dt.utcnow()
                except Exception:
                    return False

            if _has_future_end(end_date):
                cur.execute(
                    "UPDATE billing_module_subs SET end_date=datetime('now') WHERE id=?",
                    (sub_id,)
                )
            else:
                cur.execute(
                    'UPDATE billing_module_subs SET is_active=0 WHERE id=?',
                    (sub_id,)
                )
            result['revoked'] = 1

            if cascade and item_type == 'module':
                ext_keys = [r[0] for r in cur.execute(
                    'SELECT key FROM billing_extensions WHERE module_key=?', (item_key,)
                ).fetchall()]
                if ext_keys:
                    ph = ','.join('?' * len(ext_keys))
                    cur.execute(
                        f"""UPDATE billing_module_subs
                            SET is_active=0, end_date=datetime('now')
                            WHERE user_telegram_id=? AND item_type='extension'
                              AND item_key IN ({ph}) AND is_active=1""",
                        (tg_id, *ext_keys)
                    )
                    result['cascaded'] = cur.rowcount

            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error('revoke_billing_item: %s', exc)
        return result

    def get_billing_module_subs(
        self,
        user_telegram_id: int = None,
        active_only: bool = True,
        limit: int = 300,
    ) -> list:
        """Список подписок на модули/расширения/пакеты."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            wheres, params = [], []
            if active_only:
                wheres.append("is_active=1 AND (end_date IS NULL OR end_date > datetime('now'))")
            if user_telegram_id:
                wheres.append('user_telegram_id=?')
                params.append(user_telegram_id)
            where_sql = ('WHERE ' + ' AND '.join(wheres)) if wheres else ''
            cursor.execute(
                f'''SELECT id,user_telegram_id,item_type,item_key,price_paid,
                           start_date,end_date,is_active,granted_by,note,created_at
                    FROM billing_module_subs {where_sql} ORDER BY created_at DESC LIMIT ?''',
                params + [limit]
            )
            rows = cursor.fetchall()
            conn.close()
            return [
                {'id': r[0], 'user_telegram_id': r[1], 'item_type': r[2], 'item_key': r[3],
                 'price_paid': r[4], 'start_date': r[5], 'end_date': r[6],
                 'is_active': bool(r[7]), 'granted_by': r[8], 'note': r[9], 'created_at': r[10]}
                for r in rows
            ]
        except Exception as exc:
            logger.error('get_billing_module_subs: %s', exc)
            return []

    def check_billing_item(self, user_telegram_id: int, item_key: str) -> bool:
        """True если у пользователя есть активная подписка на item_key."""
        try:
            conn = self.get_connection()
            row = conn.execute(
                """SELECT 1 FROM billing_module_subs
                   WHERE user_telegram_id=? AND item_key=? AND is_active=1
                     AND (end_date IS NULL OR end_date > datetime('now')) LIMIT 1""",
                (user_telegram_id, item_key)
            ).fetchone()
            conn.close()
            return row is not None
        except Exception as exc:
            logger.error('check_billing_item: %s', exc)
            return False

    def get_user_active_billing_items(self, user_telegram_id: int) -> set:
        """Множество активных item_key для пользователя."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                """SELECT item_key FROM billing_module_subs
                   WHERE user_telegram_id=? AND is_active=1
                     AND (end_date IS NULL OR end_date > datetime('now'))""",
                (user_telegram_id,)
            ).fetchall()
            conn.close()
            return {r[0] for r in rows}
        except Exception as exc:
            logger.error('get_user_active_billing_items: %s', exc)
            return set()

    def record_ai_tool_call(self, tool_name: str, org_db: str, date_str: str) -> None:
        """Upsert one call into ai_tool_stats (only valid on shop_bot.db)."""
        try:
            conn = self.get_connection()
            conn.execute(
                """INSERT INTO ai_tool_stats (tool_name, org_db, date, call_count)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(tool_name, org_db, date)
                   DO UPDATE SET call_count = call_count + 1""",
                (tool_name, org_db, date_str),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error('record_ai_tool_call: %s', exc)

    def get_ai_tool_stats_db(self, date_str: str) -> dict:
        """Return {(tool_name, org_db): call_count} for a specific date."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                'SELECT tool_name, org_db, call_count FROM ai_tool_stats WHERE date=?',
                (date_str,),
            ).fetchall()
            conn.close()
            return {(r[0], r[1]): r[2] for r in rows}
        except Exception as exc:
            logger.error('get_ai_tool_stats_db: %s', exc)
            return {}

    def get_ai_tool_stats_all_dates_db(self) -> dict:
        """Return {(tool_name, org_db, date): call_count} for all dates."""
        try:
            conn = self.get_connection()
            rows = conn.execute(
                'SELECT tool_name, org_db, date, call_count FROM ai_tool_stats ORDER BY date DESC',
            ).fetchall()
            conn.close()
            return {(r[0], r[1], r[2]): r[3] for r in rows}
        except Exception as exc:
            logger.error('get_ai_tool_stats_all_dates_db: %s', exc)
            return {}

    def prune_ai_tool_stats(self, days: int = 90) -> int:
        """Delete ai_tool_stats rows older than *days* days. Returns deleted row count."""
        try:
            conn = self.get_connection()
            cur = conn.execute(
                "DELETE FROM ai_tool_stats WHERE date < date('now', ?)",
                (f'-{days} days',),
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            return deleted
        except Exception as exc:
            logger.error('prune_ai_tool_stats: %s', exc)
            return 0

    def get_billing_stats(self) -> dict:
        """Статистика биллинга для super admin панели."""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM billing_modules WHERE is_active=1')
            modules_active = cur.fetchone()[0]
            cur.execute('SELECT COUNT(*) FROM billing_extensions WHERE is_active=1')
            exts_active = cur.fetchone()[0]
            cur.execute('SELECT COUNT(*) FROM billing_bundles WHERE is_active=1')
            bundles_active = cur.fetchone()[0]
            cur.execute(
                """SELECT COUNT(DISTINCT user_telegram_id) FROM billing_module_subs
                   WHERE is_active=1 AND (end_date IS NULL OR end_date > datetime('now'))"""
            )
            clients_count = cur.fetchone()[0]
            cur.execute(
                "SELECT SUM(price_paid) FROM billing_module_subs "
                "WHERE is_active=1 AND created_at >= date('now','-30 days')"
            )
            rev_30d = cur.fetchone()[0] or 0.0
            cur.execute(
                """SELECT item_key, COUNT(*) AS cnt, SUM(price_paid) AS rev
                   FROM billing_module_subs WHERE item_type='module'
                   GROUP BY item_key ORDER BY rev DESC"""
            )
            per_module = [{'key': r[0], 'count': r[1], 'revenue': r[2] or 0} for r in cur.fetchall()]
            conn.close()
            return {
                'modules_active': modules_active,
                'exts_active': exts_active,
                'bundles_active': bundles_active,
                'clients_count': clients_count,
                'revenue_30d': rev_30d,
                'per_module': per_module,
            }
        except Exception as exc:
            logger.error('get_billing_stats: %s', exc)
            return {'modules_active': 0, 'exts_active': 0, 'bundles_active': 0,
                    'clients_count': 0, 'revenue_30d': 0, 'per_module': []}

