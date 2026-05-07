"""
Модуль для работы с базой данных
"""
import sqlite3
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_file=None):
        self.db_file = db_file or 'data/shop_bot.db'
        # Создаем директорию, если она не существует
        db_dir = os.path.dirname(self.db_file)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
        # self.create_tables()  # Убираем автоматический вызов здесь, так как мы копируем уже готовую структуру

    def get_connection(self):
        """Получить объект соединения с БД"""
        conn = sqlite3.connect(self.db_file, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def create_tables(self):
        """Создание таблиц в базе данных"""
        conn = sqlite3.connect(self.db_file)
        conn.execute("PRAGMA busy_timeout=10000")
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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
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

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                discount_percent INTEGER NOT NULL,
                usage_count INTEGER DEFAULT 0,
                max_usage INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT DEFAULT (datetime('now'))
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

        conn.commit()

        # --- Миграции: добавляем колонки которых нет в существующих БД ---
        cursor.execute("PRAGMA table_info(payment_requests)")
        pr_cols = [c[1] for c in cursor.fetchall()]
        if 'promocode_id' not in pr_cols:
            cursor.execute("ALTER TABLE payment_requests ADD COLUMN promocode_id INTEGER")

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
            except Exception:
                pass  # колонка уже существует

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
            # Пересоздаём таблицу — данные алертов не критичны
            cursor.execute('DROP TABLE plan_milestone_alerts')
            cursor.execute('''
                CREATE TABLE plan_milestone_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    plan_id INTEGER NOT NULL,
                    milestone INTEGER NOT NULL,
                    period_start TEXT NOT NULL DEFAULT '',
                    alerted_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, plan_id, milestone, period_start)
                )
            ''')

        conn.commit()

        # ── Индексы для ускорения тяжёлых запросов ──────────────────────────
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_user_date     ON sales(user_id, sale_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_shop_date     ON sales(shop_name, sale_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_inventory_shop_prod ON inventory(shop_name, product_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_work_schedule_date  ON work_schedule(work_date, user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_seller_earnings_sale ON seller_earnings(sale_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_shop_name     ON users(shop_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram_id   ON users(telegram_id)')

        # Инициализация базовых данных при первом запуске
        self._initialize_default_data(cursor)

        conn.commit()
        conn.close()

    def _initialize_default_data(self, cursor):
        """Инициализация базовых данных при первом создании базы"""

        # Проверяем, есть ли уже планы подписок
        cursor.execute('SELECT COUNT(*) FROM subscription_plans')
        plans_count = cursor.fetchone()[0]

        if plans_count == 0:
            # Создаем стандартные планы подписок с лимитами
            default_plans = [
                ('Бесплатный', 0, 0.0, 'Ограниченный функционал: до 50 товаров, 1 магазин, без экспорта и уведомлений', 50, 1, 100, False, False, False),
                ('Базовый', 30, 500.0, 'Для малого бизнеса: до 200 товаров, 3 магазина, экспорт отчетов', 200, 3, 500, True, True, True),
                ('Стандарт', 90, 1200.0, 'Для среднего бизнеса: до 500 товаров, 10 магазинов, расширенная аналитика', 500, 10, 1500, True, True, True),
                ('Премиум', 365, 4000.0, 'Для крупного бизнеса: безлимит товаров и магазинов, все функции', -1, -1, -1, True, True, True)
            ]

            for name, duration, price, description, max_products, max_shops, max_sales, can_export, can_analytics, can_notifications in default_plans:
                cursor.execute('''
                    INSERT INTO subscription_plans (name, duration_days, price, description, max_products, max_shops, max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, duration, price, description, max_products, max_shops, max_sales, can_export, can_analytics, can_notifications))
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
                        can_export_reports=0, can_view_analytics=0, can_use_notifications=0
                        WHERE name=?
                    ''', (plan_name,))
                elif plan_name == 'Базовый':
                    cursor.execute('''
                        UPDATE subscription_plans SET 
                        max_products=200, max_shops=3, max_sales_per_month=500,
                        can_export_reports=1, can_view_analytics=1, can_use_notifications=1
                        WHERE name=?
                    ''', (plan_name,))
                elif plan_name == 'Стандарт':
                    cursor.execute('''
                        UPDATE subscription_plans SET 
                        max_products=500, max_shops=10, max_sales_per_month=1500,
                        can_export_reports=1, can_view_analytics=1, can_use_notifications=1
                        WHERE name=?
                    ''', (plan_name,))
                elif plan_name in ['Премиум', 'VIP']:
                    cursor.execute('''
                        UPDATE subscription_plans SET 
                        max_products=-1, max_shops=-1, max_sales_per_month=-1,
                        can_export_reports=1, can_view_analytics=1, can_use_notifications=1
                        WHERE name=?
                    ''', (plan_name,))

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
                ('trial_plan', 'Бизнес'),
            ]

            for key, value in default_settings:
                cursor.execute('''
                    INSERT INTO payment_settings (key, value)
                    VALUES (?, ?)
                ''', (key, value))

    def get_recent_sales(self, limit=10):
        """Получить список последних продаж"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, p.name, s.shop_name, s.quantity_sold, s.sale_price, u.first_name, s.sale_date
            FROM sales s
            JOIN products p ON s.product_id = p.id
            JOIN users u ON s.user_id = u.id
            ORDER BY s.sale_date DESC
            LIMIT ?
        ''', (limit,))
        sales = cursor.fetchall()
        conn.close()
        return sales

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
                        except Exception:
                            pass
            return newly_hit
        except Exception as e:
            logger.error(f"Ошибка check_and_mark_plan_milestones: {e}")
            return []
    def get_payment_settings(self):
        """Получение настроек платежной системы"""
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO payment_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
        conn.commit()
        conn.close()

    def get_subscriptions_statistics(self):
        """Статистика подписок"""
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
    def add_user(self, telegram_id, first_name, last_name, middle_name=None, phone=None, email=None, trade_network=None, shop_name=None, city=None):
        """Добавление нового пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users 
            (telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city))
        conn.commit()
        conn.close()

    def get_user(self, telegram_id):
        """Получение информации о пользователе"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
        user = cursor.fetchone()
        conn.close()
        return user

    def get_shop_sales_by_date(self, shop_name, start_date, end_date):
        """Получить все продажи магазина за период"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        query = '''
            SELECT s.id, s.product_id, s.shop_name, s.quantity_sold, s.sale_price, 
                   s.user_id, s.sale_date, p.name, p.category
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.shop_name = ? AND date(s.sale_date) >= ? AND date(s.sale_date) <= ?
            ORDER BY s.sale_date DESC
        '''
        
        cursor.execute(query, (shop_name, start_date, end_date))
        sales = cursor.fetchall()
        conn.close()
        return sales

    def get_user_id(self, telegram_id):
        """Получение ID пользователя по telegram_id"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (telegram_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None
    
    def get_user_timezone(self, telegram_id):
        """Получение часового пояса пользователя"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT timezone FROM users WHERE telegram_id = ?', (telegram_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else 'Europe/Moscow'
    
    def set_user_timezone(self, telegram_id, timezone):
        """Установка часового пояса пользователя"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET timezone = ? WHERE telegram_id = ?', (timezone, telegram_id))
            conn.commit()
            success = cursor.rowcount > 0
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Ошибка при установке часового пояса: {e}")
            return False

    # Заглушки для остальных методов
    def get_all_shops(self):
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT shop_name FROM users "
            "WHERE shop_name IS NOT NULL AND shop_name != '' "
            "AND shop_name != 'Системный' AND shop_name != 'System'"
        )
        shops = [row[0] for row in cursor.fetchall()]
        conn.close()
        return shops

    def get_inventory_shops(self):
        """Возвращает список уникальных магазинов из таблицы inventory"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT shop_name FROM inventory WHERE shop_name IS NOT NULL AND shop_name != ''")
        shops = [row[0] for row in cursor.fetchall()]
        conn.close()
        return shops

    def get_all_cities(self):
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT city FROM users WHERE city IS NOT NULL AND city != '' AND city != 'System'"
        )
        cities = [row[0] for row in cursor.fetchall()]
        conn.close()
        return cities

    def get_all_trade_networks(self):
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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

    # Методы для работы с товарами
    def get_all_products(self):
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products')
        products = cursor.fetchall()
        conn.close()
        return products

    def get_product(self, product_id):
        """Получение товара по ID"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products WHERE id = ?', (product_id,))
        product = cursor.fetchone()
        conn.close()
        return product

    def get_product_by_name(self, product_name):
        """Получение товара по названию"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products WHERE name = ?', (product_name,))
        product = cursor.fetchone()
        conn.close()
        return product

    def add_product(self, name, category, price):
        """Добавление нового товара"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO products (name, category, price)
            VALUES (?, ?, ?)
        ''', (name, category, price))
        product_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return product_id

    def add_products_bulk(self, items):
        """
        Пакетное добавление товаров.
        items = [{'name': str, 'category': str, 'price': float}, ...]
        Возвращает (added_count, skipped_names) где skipped — уже существующие.
        """
        conn = sqlite3.connect(self.db_file)
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
                    added += 1
                except sqlite3.IntegrityError:
                    skipped.append(item['name'])
            conn.commit()
        finally:
            conn.close()
        return added, skipped

    def update_product(self, product_id, name=None, category=None, price=None):
        """Обновление товара"""
        conn = sqlite3.connect(self.db_file)
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

    def delete_product(self, product_id):
        """Удаление товара"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM products WHERE id = ?', (product_id,))
        conn.commit()
        conn.close()

    def get_all_categories(self):
        """Получение всех категорий товаров"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT category FROM products ORDER BY category')
        categories = [row[0] for row in cursor.fetchall()]
        conn.close()
        return categories

    def get_all_users(self, shop_name=None, city=None, trade_network=None,
                      shop_names=None, cities=None, trade_networks=None):
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
                
            conn = sqlite3.connect(self.db_file, timeout=30.0)
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout = 5000")

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
                    end_date = (datetime.now() + timedelta(days=duration_days)).isoformat()
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
            end_date = (datetime.now() + timedelta(days=days)).isoformat()
            conn = sqlite3.connect(self.db_file, timeout=30.0)
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout = 5000")
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

    def has_sent_reminder(self, user_id: int, threshold: int, subscription_end: str) -> bool:
        """Проверяет, было ли уже отправлено напоминание для данного порога истечения."""
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
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
            conn = sqlite3.connect(self.db_file, timeout=10)
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
            conn = sqlite3.connect(self.db_file, timeout=10)
            conn.execute('DELETE FROM subscription_reminder_log WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка в clear_reminders: {e}")

    def is_subscription_active(self, user_id):
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT max_products, max_shops, max_sales_per_month, 
                   can_export_reports, can_view_analytics, can_use_notifications
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
                'can_use_notifications': bool(result[5])
            }
        else:
            # Fallback на бесплатный план если план не найден
            cursor.execute('''
                SELECT max_products, max_shops, max_sales_per_month,
                       can_export_reports, can_view_analytics, can_use_notifications
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
                    'can_use_notifications': bool(result[5])
                }
            else:
                # Жесткий fallback если в базе ничего нет
                return {
                    'max_products': 50,
                    'max_shops': 1,
                    'max_sales_per_month': 100,
                    'can_export_reports': False,
                    'can_view_analytics': False,
                    'can_use_notifications': False
                }

    def get_subscription_tier_level(self, plan_type):
        """Получение уровня тарифного плана для сравнения (чем больше число, тем лучше план)"""
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file, timeout=30.0)
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

            # Обновляем статус заявки
            cursor.execute('''
                UPDATE payment_requests 
                SET status = 'approved', processed_at = CURRENT_TIMESTAMP, processed_by = ?
                WHERE id = ?
            ''', (admin_id, request_id))

            conn.commit()
            conn.close()

            # Создаем подписку (и сбрасываем старые напоминания — подписка продлена)
            success = self.create_subscription(user_id, plan_type)
            if success:
                self.clear_reminders(user_id)

            # Применяем промокод только при успешном подтверждении оплаты
            if success and promocode_id:
                try:
                    self.apply_promocode(promocode_id)
                except Exception as e:
                    logger.error(f"Ошибка применения промокода {promocode_id} при подтверждении заявки {request_id}: {e}")

            return success

        except sqlite3.OperationalError as e:
            logger.error(f"Ошибка базы данных в confirm_payment_request: {e}")
            return False
        except Exception as e:
            logger.error(f"Общая ошибка в confirm_payment_request: {e}")
            return False

    def get_user_by_id(self, user_id):
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = cursor.fetchone()
        conn.close()
        return user

    def get_notification_settings(self, user_id):
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM notification_settings WHERE user_id = ?', (user_id,))
        settings = cursor.fetchone()
        conn.close()

        if settings:
            # Возвращаем словарь для удобства использования
            return {
                'low_stock_alerts': bool(settings[2]),
                'daily_reports': bool(settings[3]),
                'sales_alerts': bool(settings[4]),
                'payment_alerts': bool(settings[5]),
                'admin_notifications': bool(settings[6]),
                'stock_threshold': settings[7],
                'notification_time': settings[8]
            }
        else:
            # Создаем настройки по умолчанию если их нет
            self.create_default_notification_settings(user_id)
            return {
                'low_stock_alerts': True,
                'daily_reports': False,
                'sales_alerts': True,
                'payment_alerts': True,
                'admin_notifications': True,
                'stock_threshold': 5,
                'notification_time': '09:00'
            }

    def create_default_notification_settings(self, user_id):
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO notification_settings (user_id)
            VALUES (?)
        ''', (user_id,))
        conn.commit()
        conn.close()

    def update_notification_settings(self, user_id, **settings):
        conn = sqlite3.connect(self.db_file)
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

    def add_notification_to_history(self, user_id, notification_type, message):
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO notification_history (user_id, notification_type, message)
            VALUES (?, ?, ?)
        ''', (user_id, notification_type, message))
        conn.commit()
        conn.close()

    def has_seen_hint(self, user_id: int, hint_key: str) -> bool:
        """Проверяет, показывалась ли подсказка пользователю ранее."""
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file, timeout=10.0)
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
            conn = sqlite3.connect(self.db_file, timeout=10.0)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sal.id, sal.changed_by_user_id,
                       u.first_name || ' ' || u.last_name AS editor_name,
                       sal.old_quantity, sal.new_quantity,
                       sal.old_price, sal.new_price, sal.changed_at
                FROM sales_audit_log sal
                LEFT JOIN users u ON sal.changed_by_user_id = u.id
                WHERE sal.sale_id = ?
                ORDER BY sal.changed_at DESC
            ''', (sale_id,))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception:
            return []

    def get_notification_history(self, user_id, limit=20):
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
        
        conn = sqlite3.connect(self.db_file)
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
    
    def delete_old_notifications(self, user_id, period_type='week'):
        """
        Удаление старых уведомлений
        period_type: 'week' (старше недели), 'month' (старше месяца), 'all' (все)
        """
        from datetime import datetime, timedelta, timezone
        
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
        """Добавление остатков товара"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        from datetime import datetime
        cursor.execute('''
            INSERT OR REPLACE INTO inventory (shop_name, product_id, quantity, updated_by, last_updated, change_type, change_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (shop_name, product_id, quantity, user_id, datetime.now().isoformat(), change_type, change_reason))
        conn.commit()
        conn.close()

    def get_inventory(self, shop_name, product_id):
        """Получение остатков товара в магазине"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT quantity FROM inventory
            WHERE shop_name = ? AND product_id = ?
        ''', (shop_name, product_id))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else 0

    def update_inventory(self, shop_name, product_id, delta, user_id=None, change_type='manual', change_reason=None):
        """Обновление остатков товара"""
        import time

        max_retries = 3
        retry_delay = 0.1

        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_file, timeout=30.0)
                conn.execute('PRAGMA busy_timeout = 30000')  # 30 секунд
                cursor = conn.cursor()

                # Получаем текущее количество
                cursor.execute('''
                    SELECT quantity FROM inventory
                    WHERE shop_name = ? AND product_id = ?
                ''', (shop_name, product_id))

                result = cursor.fetchone()
                current_quantity = result[0] if result else 0
                new_quantity = current_quantity + delta

                if new_quantity < 0:
                    new_quantity = 0

                # Обновляем остатки
                from datetime import datetime
                cursor.execute('''
                    INSERT OR REPLACE INTO inventory (shop_name, product_id, quantity, updated_by, last_updated, change_type, change_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (shop_name, product_id, new_quantity, user_id, datetime.now().isoformat(), change_type, change_reason))

                conn.commit()
                conn.close()

                return new_quantity

            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    continue
                else:
                    logger.error(f"Database error in update_inventory: {e}")
                    if 'conn' in locals():
                        conn.close()
                    raise
            except Exception as e:
                logger.error(f"Unexpected error in update_inventory: {e}")
                if 'conn' in locals():
                    conn.close()
                raise

        return current_quantity

    def get_all_inventory(self, shop_name=None):
        """Получение всех остатков с информацией о последнем изменении"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        if shop_name:
            cursor.execute('''
                SELECT i.*, p.name, p.category, p.price, 
                       u.first_name || ' ' || u.last_name as updated_by_name
                FROM inventory i
                JOIN products p ON i.product_id = p.id
                LEFT JOIN users u ON i.updated_by = u.id
                WHERE i.shop_name = ?
                ORDER BY p.name
            ''', (shop_name,))
        else:
            cursor.execute('''
                SELECT i.*, p.name, p.category, p.price, 
                       u.first_name || ' ' || u.last_name as updated_by_name
                FROM inventory i
                JOIN products p ON i.product_id = p.id
                LEFT JOIN users u ON i.updated_by = u.id
                ORDER BY i.shop_name, p.name
            ''')

        inventory = cursor.fetchall()
        conn.close()
        return inventory

    # Методы для работы с продажами
    def add_sale(self, product_id, shop_name, quantity_sold, user_id, sale_price=None):
        """Добавление продажи"""
        import time

        max_retries = 3
        retry_delay = 0.1


        for attempt in range(max_retries):
            conn = None
            try:
                conn = sqlite3.connect(self.db_file, timeout=30.0)
                conn.execute('PRAGMA busy_timeout = 30000')  # 30 секунд
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
                sale_date = datetime.now().isoformat()

                cursor.execute('''
                    INSERT INTO sales (product_id, shop_name, quantity_sold, sale_date, user_id, sale_price)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (product_id, shop_name, quantity_sold, sale_date, user_id, sale_price))

                sale_id = cursor.lastrowid

                # Обновляем остатки с информацией о пользователе
                cursor.execute('''
                    UPDATE inventory 
                    SET quantity = quantity - ?, last_updated = ?
                    WHERE shop_name = ? AND product_id = ?
                ''', (quantity_sold, sale_date, shop_name, product_id))

                affected_rows = cursor.rowcount

                if affected_rows == 0:
                    conn.rollback()
                    conn.close()
                    return None

                conn.commit()
                conn.close()


                # Рассчитываем и добавляем комиссию продавца
                try:
                    commission_info = self.get_product_motivation(product_id)
                    
                    if commission_info:
                        commission_amount = self.calculate_seller_commission(
                            sale_id, product_id, sale_price, quantity_sold,
                            user_id=user_id, shop_name=shop_name
                        )
                        if commission_amount > 0:
                            self.add_seller_earning(
                                sale_id, user_id, product_id, commission_amount,
                                commission_info['motivation_type'], commission_info['motivation_value']
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
                    except Exception:
                        pass

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
                         shop_names=None, cities=None, trade_networks=None):
        """Получение детального отчета по продажам.
        Поддерживает одиночные и множественные (list) фильтры зоны."""
        conn = sqlite3.connect(self.db_file)
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

        query += ' ORDER BY s.sale_date DESC'

        cursor.execute(query, params)
        sales = cursor.fetchall()
        conn.close()
        return sales

    def get_sales_summary(self, start_date=None, end_date=None,
                          shop_name=None, city=None, trade_network=None,
                          shop_names=None, cities=None, trade_networks=None):
        """Получение сводного отчета по продажам.

        Поддерживает одиночные (shop_name/city/trade_network) и
        множественные (shop_names/cities/trade_networks — list[str]) фильтры зоны.
        При city/trade_network-фильтрах добавляется JOIN с users.
        """
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        need_user_join = any([city, trade_network, cities, trade_networks])

        if need_user_join:
            query = '''
                SELECT
                    COUNT(*) as total_sales,
                    SUM(s.quantity_sold) as total_quantity,
                    SUM(s.quantity_sold * s.sale_price) as total_revenue,
                    AVG(s.quantity_sold * s.sale_price) as avg_sale
                FROM sales s
                JOIN users u ON s.user_id = u.id
                WHERE 1=1
            '''
        else:
            query = '''
                SELECT
                    COUNT(*) as total_sales,
                    SUM(quantity_sold) as total_quantity,
                    SUM(quantity_sold * sale_price) as total_revenue,
                    AVG(quantity_sold * sale_price) as avg_sale
                FROM sales s
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

        cursor.execute(query, params)
        summary = cursor.fetchone()
        conn.close()
        return summary

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
            conn = sqlite3.connect(self.db_file)
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
                except Exception:
                    pass

            # salary_settings
            try:
                cursor.execute(f'DELETE FROM salary_settings WHERE user_id IN ({uid_sub})', (telegram_id,))
            except Exception:
                pass

            # sales_plans — только индивидуальные планы продавца, общие (shop/org) оставляем
            try:
                cursor.execute(
                    f'DELETE FROM sales_plans WHERE user_id IN ({uid_sub}) AND target_type = ?',
                    (telegram_id, 'seller')
                )
            except Exception:
                pass

            # motivation_extra_conditions по user_id
            try:
                cursor.execute(
                    f'DELETE FROM motivation_extra_conditions WHERE user_id IN ({uid_sub})',
                    (telegram_id,)
                )
            except Exception:
                pass

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
                   phone=None, email=None, trade_network=None, shop_name=None, city=None):
        """Обновление данных пользователя"""
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE shop_name = ?', (shop_name,))
        users = cursor.fetchall()
        conn.close()
        return users

    def get_users_by_city(self, city):
        """Получение пользователей конкретного города"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE city = ?', (city,))
        users = cursor.fetchall()
        conn.close()
        return users

    def get_user_sales(self, user_id, limit=10):
        """Получить последние продажи пользователя по всем магазинам"""
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
                conn = sqlite3.connect(self.db_file, timeout=30.0)
                conn.execute('PRAGMA busy_timeout = 30000')  # 30 секунд
                cursor = conn.cursor()

                # Получаем текущие данные продажи
                cursor.execute('SELECT * FROM sales WHERE id = ?', (sale_id,))
                current_sale = cursor.fetchone()

                if current_sale:
                    old_quantity = current_sale[3]  # quantity_sold
                    product_id = current_sale[1]
                    shop_name = current_sale[2]

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

                    # Корректируем остатки
                    quantity_diff = old_quantity - quantity_sold
                    if quantity_diff != 0:
                        # Обновляем остатки в той же транзакции
                        cursor.execute('''
                            UPDATE inventory SET quantity = quantity + ?
                            WHERE shop_name = ? AND product_id = ?
                        ''', (quantity_diff, shop_name, product_id))

                    # Пересчитываем заработок продавца
                    final_price = sale_price if sale_price is not None else current_sale[4]  # sale_price col
                    sale_user_id = current_sale[5]  # user_id col
                    commission_info = self.get_product_motivation(product_id)
                    if commission_info:
                        new_commission = self.calculate_seller_commission(
                            sale_id, product_id, final_price, quantity_sold,
                            user_id=sale_user_id, shop_name=shop_name
                        )
                        # Используем INSERT OR REPLACE чтобы обновить или создать запись
                        cursor.execute('''
                            INSERT INTO seller_earnings (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value)
                            VALUES (?, (SELECT user_id FROM sales WHERE id = ?), ?, ?, ?, ?)
                            ON CONFLICT(sale_id) DO UPDATE SET
                                commission_amount = excluded.commission_amount,
                                motivation_type = excluded.motivation_type,
                                motivation_value = excluded.motivation_value
                        ''', (sale_id, sale_id, product_id, new_commission,
                              commission_info['motivation_type'], commission_info['motivation_value']))

                    try:
                        cursor.execute('''
                            INSERT INTO sales_audit_log
                            (sale_id, changed_by_user_id, old_quantity, new_quantity, old_price, new_price)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (sale_id, changed_by, old_quantity, quantity_sold,
                              current_sale[4],
                              sale_price if sale_price is not None else current_sale[4]))
                    except Exception:
                        pass

                    conn.commit()
                    conn.close()
                    return True

                conn.close()
                return False

            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
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

    def delete_sale(self, sale_id):
        """Удалить продажу и восстановить остатки"""
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file, timeout=30.0)
            conn.execute('PRAGMA busy_timeout = 30000')
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
            except Exception:
                pass
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
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        query = '''
            SELECT u.first_name, u.last_name, u.shop_name,
                   SUM(s.quantity_sold) as total_sold,
                   SUM(s.quantity_sold * s.sale_price) as total_revenue,
                   COUNT(s.id) as total_sales,
                   SUM(COALESCE(se.commission_amount, 0)) as total_earnings,
                   u.id as user_db_id
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
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file, timeout=30.0)
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

    def get_low_stock_items_for_user(self, user_id, shop_name=None, threshold=None):
        """Получение товаров с низкими остатками для конкретного пользователя"""
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subscription_plans WHERE is_active = TRUE ORDER BY price')
        plans = cursor.fetchall()
        conn.close()
        return plans

    def get_all_subscription_plans(self):
        """Получение всех тарифных планов (включая неактивные)"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subscription_plans ORDER BY price')
        plans = cursor.fetchall()
        conn.close()
        return plans

    def get_all_active_subscriptions(self):
        """Получение всех активных подписок с данными пользователей"""
        conn = sqlite3.connect(self.db_file)
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

    def get_all_promocodes(self):
        """Получение всех промокодов"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM promocodes ORDER BY created_at DESC')
        promocodes = cursor.fetchall()
        conn.close()
        return promocodes

    def create_promocode(self, code, discount_percent, max_usage):
        """Создание промокода"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO promocodes (code, discount_percent, max_usage)
            VALUES (?, ?, ?)
        ''', (code, discount_percent, max_usage))
        promocode_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return promocode_id

    def delete_promocode(self, promocode_id):
        """Удаление промокода"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM promocodes WHERE id = ?', (promocode_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def update_promocode(self, promocode_id, **kwargs):
        """Обновление промокода"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        valid_fields = ['code', 'discount_percent', 'max_usage', 'is_active']
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
        """Получение промокода по ID"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM promocodes WHERE id = ?', (promocode_id,))
        promocode = cursor.fetchone()
        conn.close()
        return promocode

    def validate_promocode(self, code):
        """Проверка промокода на валидность"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, code, discount_percent, usage_count, max_usage, is_active
            FROM promocodes 
            WHERE code = ? AND is_active = 1
        ''', (code,))
        promocode = cursor.fetchone()
        conn.close()

        if not promocode:
            return {'valid': False, 'error': 'Промокод не найден или неактивен'}

        promo_id, promo_code, discount_percent, usage_count, max_usage, is_active = promocode

        if usage_count >= max_usage:
            return {'valid': False, 'error': 'Превышен лимит использований промокода'}

        return {
            'valid': True,
            'id': promo_id,
            'code': promo_code,
            'discount_percent': discount_percent,
            'remaining_usage': max_usage - usage_count
        }

    def apply_promocode(self, promocode_id):
        """Применение промокода — атомарный инкремент с проверкой лимита."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE promocodes
            SET usage_count = usage_count + 1
            WHERE id = ? AND usage_count < max_usage
        ''', (promocode_id,))
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def calculate_discounted_price(self, original_price, discount_percent):
        """Расчет цены со скидкой"""
        discount_amount = original_price * (discount_percent / 100)
        discounted_price = original_price - discount_amount
        return max(0, discounted_price)  # Не может быть отрицательной

    def add_subscription_plan(self, name, duration_days, price, description):
        """Добавление нового тарифного плана"""
        conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

            updates = []
            params = []

            allowed_fields = ['name', 'duration_days', 'price', 'description', 'is_active',
                             'max_products', 'max_shops', 'max_sales_per_month', 
                             'can_export_reports', 'can_view_analytics', 'can_use_notifications']

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
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, name, duration_days, price, description, 
                   max_products, max_shops, max_sales_per_month,
                   can_export_reports, can_view_analytics, can_use_notifications,
                   is_active, created_at
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
                'is_active': bool(result[11]),
                'created_at': result[12]
            }
        return None

    def create_subscription_plan_with_limits(self, name, duration_days, price, description,
                                           max_products=50, max_shops=1, max_sales_per_month=100,
                                           can_export_reports=False, can_view_analytics=False, 
                                           can_use_notifications=False):
        """Создание нового тарифного плана с лимитами"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO subscription_plans 
            (name, duration_days, price, description, max_products, max_shops, 
             max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, duration_days, price, description, max_products, max_shops, 
              max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications))

        plan_id = cursor.lastrowid
        conn.commit()
        conn.close()

        return plan_id

    def update_subscription_plan_field(self, plan_id, field_name, new_value):
        """Обновление отдельного поля тарифного плана"""
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

            # Получаем активную подписку пользователя
            cursor.execute('''
                SELECT sp.max_products, sp.max_shops, sp.max_sales_per_month,
                       sp.can_export_reports, sp.can_view_analytics, sp.can_use_notifications
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
                    'can_use_notifications': bool(result[5])
                }
            else:
                # Возвращаем лимиты бесплатного плана по умолчанию
                return {
                    'max_products': 50,
                    'max_shops': 1,
                    'max_sales_per_month': 100,
                    'can_export_reports': False,
                    'can_view_analytics': False,
                    'can_use_notifications': False
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
                'can_use_notifications': False
            }

    def clear_all_sales(self):
        """Очистка всех данных продаж (для сброса рейтингов)"""
        try:
            conn = sqlite3.connect(self.db_file)
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

    def delete_subscription_plan(self, plan_id):
        """Удаление тарифного плана с сохранением активных подписок"""
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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

    def set_product_motivation(self, product_id, motivation_type, motivation_value, admin_telegram_id):
        """Установка мотивации для товара с использованием telegram_id администратора"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

            # Получаем внутренний ID пользователя по его telegram_id
            cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (admin_telegram_id,))
            res = cursor.fetchone()
            admin_id = res[0] if res else None

            # Удаляем существующую мотивацию, если есть
            cursor.execute('DELETE FROM product_motivations WHERE product_id = ?', (product_id,))

            # Добавляем новую мотивацию
            cursor.execute('''
                INSERT INTO product_motivations (product_id, motivation_type, motivation_value, created_by)
                VALUES (?, ?, ?, ?)
            ''', (product_id, motivation_type, motivation_value, admin_id))

            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка при установке мотивации: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def get_product_motivation(self, product_id):
        """Получение мотивации для товара"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT motivation_type, motivation_value
                FROM product_motivations
                WHERE product_id = ?
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
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT p.id, p.name, pc.motivation_type, pc.motivation_value, 
                       u.first_name, u.last_name, pc.created_at
                FROM products p
                LEFT JOIN product_motivations pc ON p.id = pc.product_id
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

    def remove_product_motivation(self, product_id):
        """Удаление мотивации с товара"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

            cursor.execute('DELETE FROM product_motivations WHERE product_id = ?', (product_id,))

            conn.commit()
            conn.close()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка при удалении мотивации: {e}")
            if 'conn' in locals():
                conn.close()
            return False

    def apply_extra_conditions(self, base_commission, user_id, product_id, shop_name):
        """Применить доп. условия мотивации: фильтр категорий и коэффициент смены"""
        if base_commission <= 0:
            return base_commission
        try:
            import json as _json
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

            cursor.execute('SELECT category FROM products WHERE id = ?', (product_id,))
            prod = cursor.fetchone()
            product_category = prod[0] if prod else None

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

            cursor.execute('''
                SELECT shop_name, min_sellers, coefficient FROM motivation_extra_conditions
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
            if coeff_conditions and not has_fixed_salary:
                start_of_month = datetime.now().replace(day=1).strftime('%Y-%m-%d')
                cursor.execute('''
                    SELECT COUNT(DISTINCT user_id) FROM sales
                    WHERE shop_name = ? AND date(sale_date) >= date(?)
                ''', (shop_name, start_of_month))
                sellers_count = cursor.fetchone()[0]

                for _cond_shop, min_sellers, coefficient in coeff_conditions:
                    if sellers_count >= min_sellers:
                        final_commission = round(final_commission * coefficient, 2)
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
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')

    def create_contest(self, title, description=None, contest_type='any',
                       metric_type='turnover', target_value=0,
                       reward_type='fixed', reward_value=0,
                       start_date=None, end_date=None,
                       shop_filter=None, city_filter=None, user_filter=None,
                       product_filter=None, category_filter=None,
                       extra_conditions=None, notify_on_start=0, notify_on_end=0,
                       created_by=None):
        """Создать конкурс"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            self._ensure_contests_table(cursor)
            cursor.execute('''
                INSERT INTO contests
                (title, description, contest_type, metric_type, target_value,
                 reward_type, reward_value, start_date, end_date,
                 shop_filter, city_filter, user_filter, product_filter,
                 category_filter, extra_conditions, notify_on_start, notify_on_end, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (title, description, contest_type, metric_type, target_value,
                  reward_type, reward_value, start_date, end_date,
                  shop_filter, city_filter, user_filter, product_filter,
                  category_filter, extra_conditions, notify_on_start, notify_on_end, created_by))
            new_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return new_id
        except Exception as e:
            logger.error(f"Ошибка create_contest: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def get_contests(self, status=None):
        """Получить конкурсы. Порядок колонок:
        0:id 1:title 2:desc 3:contest_type 4:metric_type 5:target_value
        6:reward_type 7:reward_value 8:start_date 9:end_date
        10:shop_filter 11:city_filter 12:user_filter 13:product_filter
        14:category_filter 15:extra_conditions 16:status
        17:notify_on_start 18:notify_on_end 19:created_by 20:created_at"""
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
        """Обновить поля конкурса. Разрешённые поля: start_date, end_date, target_value, reward_value."""
        allowed = {'start_date', 'end_date', 'target_value', 'reward_value'}
        to_update = {k: v for k, v in fields.items() if k in allowed}
        if not to_update:
            return False
        try:
            conn = sqlite3.connect(self.db_file)
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
            if 'conn' in locals():
                conn.close()
            return False

    def delete_contest(self, contest_id):
        """Удалить конкурс"""
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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

    def compute_contest_results(self, contest_id):
        """Рассчитать результаты конкурса.
        Возвращает список dict: user_id, first_name, last_name, telegram_id,
        shop_name, actual, reward, is_winner"""
        try:
            import json as _json
            contest = self.get_contest(contest_id)
            if not contest:
                return []

            metric = contest[4]
            target = contest[5]
            rtype = contest[6]
            rval = contest[7]
            start = contest[8]
            end = contest[9]
            shop_f = contest[10]
            city_f = contest[11]
            user_f = contest[12]
            prod_f = contest[13]
            cat_f = contest[14]

            shops = _json.loads(shop_f) if shop_f else None
            cities = _json.loads(city_f) if city_f else None
            users = _json.loads(user_f) if user_f else None
            products = _json.loads(prod_f) if prod_f else None
            categories = _json.loads(cat_f) if cat_f else None

            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()

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

            query = f"""
                SELECT u.id, u.first_name, u.last_name, u.telegram_id, u.shop_name,
                       {metric_expr} AS actual_value
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

            results = []
            for row in rows:
                uid, fname, lname, tg_id, shop_name, actual = row
                actual = actual or 0.0
                is_winner = actual >= target
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
                    'actual': actual,
                    'reward': reward,
                    'is_winner': is_winner,
                })
            return results
        except Exception as e:
            logger.error(f"Ошибка compute_contest_results: {e}")
            if 'conn' in locals():
                conn.close()
            return []

    def get_contests_for_period(self, start_date: str, end_date: str) -> list:
        """Конкурсы, активные или завершённые в заданном периоде (пересечение дат)"""
        try:
            conn = sqlite3.connect(self.db_file)
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
                            description=None):
        """Добавить доп. условие мотивации"""
        try:
            import json as _json
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            allowed_str = _json.dumps(allowed_categories, ensure_ascii=False) if allowed_categories else None
            cursor.execute('''
                INSERT INTO motivation_extra_conditions
                (condition_type, description, shop_name, min_sellers, coefficient, user_id, allowed_categories)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (condition_type, description, shop_name, min_sellers, coefficient, user_id, allowed_str))
            new_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return new_id
        except Exception as e:
            logger.error(f"Ошибка при добавлении доп. условия: {e}")
            if 'conn' in locals():
                conn.close()
            return None

    def get_extra_conditions(self, active_only=True):
        """Получить все доп. условия мотивации"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            where = "WHERE mec.is_active = 1" if active_only else ""
            cursor.execute(f'''
                SELECT mec.id, mec.condition_type, mec.description,
                       mec.shop_name, mec.min_sellers, mec.coefficient,
                       mec.user_id, mec.allowed_categories,
                       mec.is_active, mec.created_at,
                       u.first_name, u.last_name
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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

    def calculate_plan_actual(self, plan_row):
        """Рассчитать фактическое выполнение плана за текущий период"""
        try:
            import json as _json
            (plan_id, plan_type, metric_type, target_value,
             target_type, user_id, shop_name,
             filter_type, filter_value, *_rest) = plan_row

            now = datetime.now()
            if plan_type == 'weekly':
                start_date = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
            else:
                start_date = now.replace(day=1).strftime('%Y-%m-%d')

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
                except Exception:
                    pass

            where = ' AND '.join(conditions)
            query = f"SELECT {metric_expr} FROM sales s {join_clause} WHERE {where}"

            conn = sqlite3.connect(self.db_file)
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

    def get_plans_progress(self):
        """Получить все планы с прогрессом выполнения"""
        plans = self.get_sales_plans(active_only=True)
        result = []
        for plan in plans:
            actual = self.calculate_plan_actual(plan)
            target = plan[3]
            percent = round((actual / target * 100) if target > 0 else 0.0, 1)
            result.append((plan, actual, percent))
        return result

    def get_user_plans_progress(self, telegram_id):
        """Получить планы конкретного продавца по telegram_id с прогрессом"""
        try:
            conn = sqlite3.connect(self.db_file)
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
                    actual = self.calculate_plan_actual(plan)
                    target = plan[3]
                    percent = round((actual / target * 100) if target > 0 else 0.0, 1)
                    result.append((plan, actual, percent))
                elif target_type == 'shop' and plan_shop and user_shop and plan_shop == user_shop:
                    actual = self.calculate_plan_actual(plan)
                    target = plan[3]
                    percent = round((actual / target * 100) if target > 0 else 0.0, 1)
                    result.append((plan, actual, percent))
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении планов пользователя: {e}")
            return []

    def get_user_contest_rewards(self, telegram_id: int, month_start: str, month_end: str) -> float:
        """Суммарные призы конкурсов для пользователя за период (по telegram_id).
        Учитываются только конкурсы, чей период пересекается с [month_start, month_end]."""
        try:
            # Фильтруем конкурсы по переданному периоду (не все за все время)
            period_contests = self.get_contests_for_period(month_start, month_end)
            total = 0.0
            for c in period_contests:
                results = self.compute_contest_results(c[0])
                for r in results:
                    if r.get('telegram_id') == telegram_id and r.get('is_winner'):
                        total += r.get('reward', 0.0)
            return round(total, 2)
        except Exception as e:
            logger.error(f"Ошибка get_user_contest_rewards: {e}")
            return 0.0

    def calculate_seller_commission(self, sale_id, product_id, sale_price, quantity_sold,
                                    user_id=None, shop_name=None):
        """Расчет мотивации продавца за продажу"""
        commission_info = self.get_product_motivation(product_id)

        if not commission_info:
            return 0.0

        motivation_type = commission_info['motivation_type']
        motivation_value = commission_info['motivation_value']

        if motivation_type == 'percentage':
            # Процент от суммы продажи
            total_sale_amount = sale_price * quantity_sold
            commission_amount = total_sale_amount * (motivation_value / 100)
        else:  # fixed
            # Фиксированная сумма за каждую единицу товара
            commission_amount = motivation_value * quantity_sold

        if user_id is not None and shop_name is not None:
            commission_amount = self.apply_extra_conditions(
                commission_amount, user_id, product_id, shop_name
            )

        return round(commission_amount, 2)

    def add_seller_earning(self, sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value):
        """Добавление заработка продавца"""
        import time

        max_retries = 3
        retry_delay = 0.1

        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_file, timeout=30.0)
                conn.execute('PRAGMA busy_timeout = 30000')
                cursor = conn.cursor()

                # Убеждаемся что все параметры не None
                motivation_type = motivation_type if motivation_type is not None else 'percentage'
                motivation_value = motivation_value if motivation_value is not None else 0.0
                commission_amount = commission_amount if commission_amount is not None else 0.0


                cursor.execute('''
                    INSERT INTO seller_earnings 
                    (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (sale_id, user_id, product_id, commission_amount, motivation_type, motivation_value))

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
            conn = sqlite3.connect(self.db_file)
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
                           s.sale_date, s.shop_name
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
                           s.sale_date, s.shop_name
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
            conn = sqlite3.connect(self.db_file)
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
                return {
                    'total_earnings': round(result[0] if result[0] else 0.0, 2),
                    'total_sales': result[1]
                }
            return {
                'total_earnings': 0.0,
                'total_sales': 0
            }
        except Exception as e:
            logger.error(f"Ошибка при получении общего заработка: {e}")
            if 'conn' in locals():
                conn.close()
            return {'total_earnings': 0.0, 'total_sales': 0}

    def get_top_sellers_by_earnings(self, start_date=None, end_date=None, limit=10):
        """Получение топа продавцов по заработку"""
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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

    def add_work_day(self, user_id: int, work_date: str,
                     start_time=None, end_time=None, marked_by=None) -> None:
        """Добавить рабочий день (INSERT OR IGNORE) с временем смены."""
        try:
            conn = sqlite3.connect(self.db_file)
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

    def remove_work_day(self, user_id: int, work_date: str) -> None:
        """Удалить рабочий день."""
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
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

    def calculate_monthly_salary(self, user_id, year, month):
        """Рассчитать зарплату за месяц: смены × ставка"""
        try:
            days = self.get_worked_days_count(user_id, year, month)
            rate = self.get_salary_rate(user_id)
            return days * rate
        except Exception as e:
            logger.error(f"Ошибка calculate_monthly_salary: {e}")
            return 0.0

    def get_team_salary_summary(self, year, month):
        """Сводка зарплат по команде: (user_id, fn, ln, daily_rate, worked_days, salary, shop_name, telegram_id)"""
        try:
            conn = sqlite3.connect(self.db_file)
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
                    u.telegram_id
                FROM users u
                LEFT JOIN salary_settings ss ON ss.user_id = u.id
                LEFT JOIN work_schedule ws ON ws.user_id = u.id
                    AND ws.work_date >= ? AND ws.work_date <= ?
                GROUP BY u.id
                ORDER BY u.last_name, u.first_name
            ''', (month_start, month_end))
            result = cursor.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Ошибка get_team_salary_summary: {e}")
            if 'conn' in locals():
                conn.close()
            return []

