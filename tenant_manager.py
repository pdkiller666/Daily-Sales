import sqlite3
import os
import shutil
import time

# Кеш путей к БД: {telegram_id: (path, timestamp)}
# Избегает запросов к main.db при каждом get_db()
_path_cache: dict = {}
_PATH_CACHE_TTL = 300   # 5 минут


class TenantManager:
    def __init__(self, main_db_path='data/main.db', tenants_dir='data/tenants'):
        self.main_db_path = main_db_path
        self.tenants_dir = tenants_dir
        if not os.path.exists(self.tenants_dir):
            os.makedirs(self.tenants_dir)
        self._init_main_db()

    def _init_main_db(self):
        """Инициализация центральной базы данных"""
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                db_path TEXT NOT NULL,
                owner_id INTEGER,
                invite_code TEXT UNIQUE,
                subscription_plan TEXT DEFAULT 'Бесплатный',
                subscription_end TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_org_mapping (
                telegram_id INTEGER PRIMARY KEY,
                org_id INTEGER,
                role TEXT DEFAULT 'user',
                FOREIGN KEY (org_id) REFERENCES organizations (id)
            )
        ''')
        
        conn.commit()

        # Миграция: добавляем колонки если их нет
        cursor.execute("PRAGMA table_info(user_org_mapping)")
        cols = [c[1] for c in cursor.fetchall()]
        if 'scope_type' not in cols:
            cursor.execute("ALTER TABLE user_org_mapping ADD COLUMN scope_type TEXT DEFAULT NULL")
        if 'scope_value' not in cols:
            cursor.execute("ALTER TABLE user_org_mapping ADD COLUMN scope_value TEXT DEFAULT NULL")
        if 'custom_title' not in cols:
            cursor.execute("ALTER TABLE user_org_mapping ADD COLUMN custom_title TEXT DEFAULT NULL")
        if 'is_active' not in cols:
            cursor.execute("ALTER TABLE user_org_mapping ADD COLUMN is_active INTEGER DEFAULT 1")
            cursor.execute("UPDATE user_org_mapping SET is_active = 1 WHERE is_active IS NULL")

        # Миграция: переименовываем роль super_admin → owner
        cursor.execute("UPDATE user_org_mapping SET role = 'owner' WHERE role = 'super_admin'")

        # Миграция: invite_preset_role и invite_preset_shop в organizations
        cursor.execute("PRAGMA table_info(organizations)")
        org_cols = [c[1] for c in cursor.fetchall()]
        if 'invite_preset_role' not in org_cols:
            cursor.execute("ALTER TABLE organizations ADD COLUMN invite_preset_role TEXT DEFAULT NULL")
        if 'invite_preset_shop' not in org_cols:
            cursor.execute("ALTER TABLE organizations ADD COLUMN invite_preset_shop TEXT DEFAULT NULL")

        conn.commit()
        conn.close()

    def generate_invite_code(self, org_id):
        """Генерация или получение существующего кода приглашения"""
        import secrets
        import string
        
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        
        # Проверяем, есть ли уже код
        cursor.execute("SELECT invite_code FROM organizations WHERE id = ?", (org_id,))
        result = cursor.fetchone()
        if result and result[0]:
            conn.close()
            return result[0]
            
        # Генерируем новый
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        try:
            cursor.execute("UPDATE organizations SET invite_code = ? WHERE id = ?", (code, org_id))
            conn.commit()
            return code
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def rotate_invite_code(self, org_id) -> str | None:
        """Генерация нового кода приглашения (всегда создаёт новый, заменяя старый)."""
        import secrets
        import string
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        conn = sqlite3.connect(self.main_db_path)
        try:
            conn.execute("UPDATE organizations SET invite_code = ? WHERE id = ?", (code, org_id))
            conn.commit()
            return code
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def set_invite_preset(self, org_id, role: str | None, shop_name: str | None) -> bool:
        """Установить пресет роли и магазина для новых участников по этому приглашению."""
        conn = sqlite3.connect(self.main_db_path)
        try:
            conn.execute(
                "UPDATE organizations SET invite_preset_role=?, invite_preset_shop=? WHERE id=?",
                (role, shop_name, org_id)
            )
            conn.commit()
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def get_invite_preset_by_code(self, invite_code: str) -> dict | None:
        """Вернуть пресет и данные орг по коду приглашения."""
        conn = sqlite3.connect(self.main_db_path)
        try:
            row = conn.execute(
                "SELECT invite_preset_role, invite_preset_shop, id, name FROM organizations WHERE invite_code=?",
                (invite_code,)
            ).fetchone()
            if row:
                return {
                    'preset_role': row[0],
                    'preset_shop': row[1],
                    'org_id': row[2],
                    'org_name': row[3],
                }
            return None
        except Exception:
            return None
        finally:
            conn.close()

    def get_invite_preset_by_org(self, org_id: int) -> dict:
        """Вернуть текущий пресет для организации."""
        conn = sqlite3.connect(self.main_db_path)
        try:
            row = conn.execute(
                "SELECT invite_preset_role, invite_preset_shop FROM organizations WHERE id=?",
                (org_id,)
            ).fetchone()
            if row:
                return {'preset_role': row[0], 'preset_shop': row[1]}
            return {'preset_role': None, 'preset_shop': None}
        except Exception:
            return {'preset_role': None, 'preset_shop': None}
        finally:
            conn.close()

    def get_org_admin_telegram_ids(self, org_id: int) -> list:
        """Telegram ID всех owner и admin данной организации."""
        conn = sqlite3.connect(self.main_db_path)
        try:
            rows = conn.execute(
                "SELECT telegram_id FROM user_org_mapping "
                "WHERE org_id=? AND role IN ('owner','admin') AND is_active=1",
                (org_id,)
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def get_user_org_id(self, telegram_id: int) -> int | None:
        """Получить org_id для активного пользователя."""
        conn = sqlite3.connect(self.main_db_path)
        try:
            row = conn.execute(
                "SELECT org_id FROM user_org_mapping WHERE telegram_id=? AND is_active=1",
                (telegram_id,)
            ).fetchone()
            return row[0] if row else None
        except Exception:
            return None
        finally:
            conn.close()

    def join_organization_by_invite(self, telegram_id, invite_code):
        """Присоединение пользователя к организации по коду."""
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, name FROM organizations WHERE invite_code = ?", (invite_code,))
        org = cursor.fetchone()
        
        if not org:
            conn.close()
            return False, "Неверный код приглашения"
            
        org_id, org_name = org
        try:
            # Проверяем, не был ли пользователь исключён из этой же организации
            cursor.execute(
                "SELECT is_active FROM user_org_mapping WHERE telegram_id = ? AND org_id = ?",
                (telegram_id, org_id)
            )
            existing = cursor.fetchone()
            if existing is not None and existing[0] == 0:
                conn.close()
                return False, "KICKED"  # специальный маркер — обработчик покажет верное сообщение

            cursor.execute('''
                INSERT OR REPLACE INTO user_org_mapping (telegram_id, org_id, role)
                VALUES (?, ?, ?)
            ''', (telegram_id, org_id, 'user'))
            conn.commit()
            self._invalidate_path_cache(telegram_id)
            return True, org_name
        except sqlite3.Error as e:
            return False, str(e)
        finally:
            conn.close()

    def get_user_db_path(self, telegram_id):
        """Получить путь к БД для конкретного пользователя.

        Результат кешируется на _PATH_CACHE_TTL секунд, чтобы не дёргать
        main.db при каждом нажатии кнопки.
        """
        now = time.time()
        cached = _path_cache.get(telegram_id)
        if cached and now - cached[1] < _PATH_CACHE_TTL:
            return cached[0]

        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT o.db_path FROM organizations o
            JOIN user_org_mapping m ON o.id = m.org_id
            WHERE m.telegram_id = ? AND m.is_active = 1
        ''', (telegram_id,))
        result = cursor.fetchone()
        conn.close()

        if result:
            db_path = result[0]
            if os.path.exists(db_path):
                _path_cache[telegram_id] = (db_path, now)
                return db_path

        _path_cache[telegram_id] = ('data/shop_bot.db', now)
        return 'data/shop_bot.db'

    def _invalidate_path_cache(self, telegram_id: int):
        """Сбросить кеш пути для пользователя (при смене роли/орга)."""
        _path_cache.pop(telegram_id, None)

    def create_organization(self, name, owner_id):
        """Создать новую организацию с собственной БД"""
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM organizations WHERE name = ?", (name,))
            if cursor.fetchone():
                return False, "Организация с таким названием уже существует"

            # Вставляем запись с временным путём, чтобы получить org_id
            cursor.execute('''
                INSERT INTO organizations (name, db_path, owner_id)
                VALUES (?, ?, ?)
            ''', (name, 'pending', owner_id))
            org_id = cursor.lastrowid

            # Имя файла основано на org_id — гарантированно уникально
            db_path = os.path.join(self.tenants_dir, f"org_{org_id}.db")
            cursor.execute("UPDATE organizations SET db_path = ? WHERE id = ?", (db_path, org_id))

            cursor.execute('''
                INSERT OR REPLACE INTO user_org_mapping (telegram_id, org_id, role, scope_type, scope_value)
                VALUES (?, ?, ?, NULL, NULL)
            ''', (owner_id, org_id, 'owner'))

            conn.commit()

            # Создаём чистую БД (без копирования чужих данных из shop_bot.db)
            from database import Database
            temp_db = Database(db_path)
            temp_db.create_tables()

            # Копируем только таблицу тарифных планов из shop_bot.db
            try:
                src_conn = sqlite3.connect('data/shop_bot.db')
                src_cursor = src_conn.cursor()
                src_cursor.execute(
                    "SELECT name, duration_days, price, description, max_products, max_shops, "
                    "max_sales_per_month, can_export_reports, can_view_analytics, "
                    "can_use_notifications, is_active FROM subscription_plans"
                )
                plans = src_cursor.fetchall()
                src_conn.close()

                if plans:
                    dst_conn = sqlite3.connect(db_path)
                    dst_cursor = dst_conn.cursor()
                    dst_cursor.execute("DELETE FROM subscription_plans")
                    dst_cursor.executemany(
                        "INSERT OR IGNORE INTO subscription_plans "
                        "(name, duration_days, price, description, max_products, max_shops, "
                        "max_sales_per_month, can_export_reports, can_view_analytics, "
                        "can_use_notifications, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        plans
                    )
                    dst_conn.commit()
                    dst_conn.close()
            except Exception:
                pass

            return True, org_id
        except sqlite3.Error as e:
            conn.rollback()
            return False, f"Ошибка базы данных: {str(e)}"
        finally:
            conn.close()

    def delete_organization(self, org_id):
        """Удалить организацию: запись в main.db, маппинг пользователей и файл БД"""
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT name, db_path FROM organizations WHERE id = ?", (org_id,))
            org = cursor.fetchone()
            if not org:
                return False, "Организация не найдена"
            org_name, db_path = org

            cursor.execute("DELETE FROM user_org_mapping WHERE org_id = ?", (org_id,))
            cursor.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
            conn.commit()

            if db_path and os.path.exists(db_path):
                try:
                    os.remove(db_path)
                except OSError:
                    pass

            return True, org_name
        except sqlite3.Error as e:
            return False, f"Ошибка базы данных: {str(e)}"
        finally:
            conn.close()

    def change_user_role(self, telegram_id: int, new_role: str,
                         scope_type: str = None, scope_value=None,
                         custom_title: str = None) -> tuple[bool, str]:
        """Изменить роль, зону ответственности и/или название должности.

        new_role: 'owner' | 'admin' | 'user'
        scope_type (только для admin): None/'all' — весь орг;
            'shop'/'city'/'network' — конкретная зона.
        scope_value: str или list[str] — одно или несколько значений зоны.
        custom_title: пользовательское название должности (None — сброс).
        """
        import json as _json
        if new_role not in ('owner', 'admin', 'user'):
            return False, "Неверная роль"
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT org_id FROM user_org_mapping WHERE telegram_id = ?",
                (telegram_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False, "Пользователь не состоит ни в одной организации"

            if new_role == 'admin':
                actual_scope_type = scope_type if scope_type else None
                if scope_type and scope_type != 'all' and scope_value:
                    if isinstance(scope_value, list):
                        actual_scope_value = _json.dumps(scope_value, ensure_ascii=False)
                    else:
                        actual_scope_value = _json.dumps([scope_value], ensure_ascii=False)
                else:
                    actual_scope_value = None
            else:
                actual_scope_type = None
                actual_scope_value = None

            cursor.execute(
                "UPDATE user_org_mapping SET role=?, scope_type=?, scope_value=?, custom_title=? "
                "WHERE telegram_id=?",
                (new_role, actual_scope_type, actual_scope_value, custom_title, telegram_id)
            )
            conn.commit()
            self._invalidate_path_cache(telegram_id)
            return True, new_role
        except sqlite3.Error as e:
            return False, str(e)
        finally:
            conn.close()

    def set_user_title(self, telegram_id: int, title: str | None) -> bool:
        """Установить или сбросить (title=None) пользовательское название должности."""
        conn = sqlite3.connect(self.main_db_path)
        try:
            conn.execute(
                "UPDATE user_org_mapping SET custom_title=? WHERE telegram_id=?",
                (title, telegram_id)
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def remove_user_from_org(self, telegram_id: int) -> tuple[bool, str]:
        """Заморозить пользователя в организации (is_active=0).
        Все данные (продажи, история, профиль в орг-БД) сохраняются.
        Пользователь теряет доступ к org-режиму, но его история остаётся в отчётах.
        Администратор может восстановить пользователя через restore_user_to_org().
        """
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT org_id, is_active FROM user_org_mapping WHERE telegram_id = ?",
                (telegram_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False, "Пользователь не состоит ни в одной организации"
            cursor.execute(
                "UPDATE user_org_mapping SET is_active = 0 WHERE telegram_id = ?",
                (telegram_id,)
            )
            conn.commit()
            self._invalidate_path_cache(telegram_id)
            return True, "ok"
        except sqlite3.Error as e:
            return False, str(e)
        finally:
            conn.close()

    def restore_user_to_org(self, telegram_id: int) -> tuple[bool, str]:
        """Восстановить ранее исключённого пользователя (is_active=1)."""
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT org_id FROM user_org_mapping WHERE telegram_id = ?",
                (telegram_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False, "Пользователь не найден в базе организации"
            cursor.execute(
                "UPDATE user_org_mapping SET is_active = 1 WHERE telegram_id = ?",
                (telegram_id,)
            )
            conn.commit()
            self._invalidate_path_cache(telegram_id)
            return True, "ok"
        except sqlite3.Error as e:
            return False, str(e)
        finally:
            conn.close()

    def get_user_org_is_active(self, telegram_id: int) -> bool | None:
        """Возвращает is_active из user_org_mapping, или None если нет записи."""
        try:
            conn = sqlite3.connect(self.main_db_path)
            row = conn.execute(
                "SELECT is_active FROM user_org_mapping WHERE telegram_id = ?",
                (telegram_id,)
            ).fetchone()
            conn.close()
            if row is None:
                return None
            return bool(row[0])
        except Exception:
            return None

    def get_all_organizations(self):
        """Получить список всех организаций"""
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, db_path, owner_id, invite_code, subscription_plan, is_active, created_at "
            "FROM organizations ORDER BY created_at DESC"
        )
        orgs = cursor.fetchall()
        conn.close()
        return orgs

    def get_org_users(self, org_id):
        """Получить пользователей конкретной организации из её tenant DB"""
        conn = sqlite3.connect(self.main_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT db_path FROM organizations WHERE id = ?", (org_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or not os.path.exists(row[0]):
            return []
        try:
            org_conn = sqlite3.connect(row[0])
            org_cursor = org_conn.cursor()
            org_cursor.execute(
                "SELECT id, telegram_id, first_name, last_name, middle_name, phone, email, "
                "trade_network, shop_name, city, timezone, created_at FROM users"
            )
            users = org_cursor.fetchall()
            org_conn.close()
            return users
        except Exception:
            return []


tenant_manager = TenantManager()
