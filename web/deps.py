import os
import sqlite3
from database import Database
from tenant_manager import tenant_manager


def get_web_db(telegram_id: int, org_db: str | None = None) -> Database:
    if org_db and os.path.exists(org_db):
        db = Database(org_db)
        db.create_tables()
        return db
    path = tenant_manager.get_user_db_path(telegram_id)
    if path and os.path.exists(path) and path != 'data/shop_bot.db':
        db = Database(path)
        db.create_tables()
        return db
    db = Database('data/shop_bot.db')
    db.create_tables()
    return db


def get_user_role_from_db(telegram_id: int) -> str:
    try:
        conn = sqlite3.connect('data/main.db')
        cursor = conn.cursor()
        cursor.execute('SELECT role FROM user_org_mapping WHERE telegram_id = ?', (telegram_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 'user'
    except Exception:
        return 'user'


def get_user_org_db_path(telegram_id: int) -> str:
    path = tenant_manager.get_user_db_path(telegram_id)
    return path if path else 'data/shop_bot.db'
