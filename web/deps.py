import os
import sqlite3
from database import Database
from tenant_manager import tenant_manager


def get_web_db(telegram_id: int, org_db: str | None = None) -> Database:
    """Return a Database instance for the user's org.

    shop_bot.db is intentionally excluded as a valid org_db — it is the
    personal/payments DB and must never be shown in place of an org DB.

    WAL/synchronous PRAGMAs are set once per (thread × db_file) inside
    Database._get_pooled_conn() at connection creation time — no need to
    re-apply them here on every request (removed 2026-07-09: this used to
    open a second throwaway connection and re-run the same PRAGMAs on every
    single page load, which was pure overhead).
    """
    if org_db and org_db != 'data/shop_bot.db' and os.path.exists(org_db):
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
        from env_manager import env_manager as _env_mgr
        if _env_mgr.is_super_admin(telegram_id):
            return 'super_admin'
    except Exception:
        pass
    try:
        conn = sqlite3.connect('data/main.db')
        cursor = conn.cursor()
        cursor.execute(
            'SELECT role FROM user_org_mapping WHERE telegram_id = ? AND is_active = 1',
            (telegram_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 'user'
    except Exception:
        return 'user'


def get_user_org_db_path(telegram_id: int) -> str | None:
    """Return the org DB path for this user, or None if not mapped to any org."""
    path = tenant_manager.get_user_db_path(telegram_id)
    if path and path != 'data/shop_bot.db':
        return path
    return None


def get_first_available_org_db() -> str | None:
    """Return db_path of the first active org whose file exists (super_admin fallback)."""
    try:
        conn = sqlite3.connect('data/main.db')
        rows = conn.execute(
            "SELECT db_path FROM organizations WHERE is_active=1 ORDER BY id"
        ).fetchall()
        conn.close()
        for row in rows:
            if row[0] and row[0] != 'data/shop_bot.db' and os.path.exists(row[0]):
                return row[0]
    except Exception:
        pass
    return None


def get_all_active_orgs() -> list[dict]:
    """Return list of active orgs with existing DB files (for org switcher)."""
    result = []
    try:
        conn = sqlite3.connect('data/main.db')
        rows = conn.execute(
            "SELECT id, name, db_path FROM organizations WHERE is_active=1 ORDER BY name"
        ).fetchall()
        conn.close()
        for row in rows:
            if row[2] and os.path.exists(row[2]):
                result.append({"id": row[0], "name": row[1] or f"org#{row[0]}", "db_path": row[2]})
    except Exception:
        pass
    return result
