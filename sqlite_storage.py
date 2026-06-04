"""
SQLite-based FSM storage для aiogram 3.
Заменяет PickleStorage: thread-safe, персистентное хранилище без внешних зависимостей.
"""

import asyncio
import json
import sqlite3
import threading
import logging
from typing import Optional, Dict, Any
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType

logger = logging.getLogger(__name__)


class SQLiteStorage(BaseStorage):
    """Потокобезопасное FSM хранилище на базе SQLite.

    Преимущества над PickleStorage:
    - WAL-режим: параллельные чтения без блокировок
    - Каждая запись хранится отдельно — не перезаписывается весь файл при каждом update
    - Персистентное и устойчивое к сбоям
    - Thread-local пул: одно соединение на поток, без пересоздания на каждую операцию
    """

    def __init__(self, db_path: str = "data/fsm_storage.db"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Возвращает thread-local соединение. Создаёт один раз на поток."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-4000")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fsm_data (
                bot_id     TEXT    NOT NULL,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                destiny    TEXT    NOT NULL DEFAULT 'default',
                state      TEXT,
                data       TEXT    NOT NULL DEFAULT '{}',
                updated_at TEXT,
                PRIMARY KEY (bot_id, chat_id, user_id, destiny)
            )
        """)
        # Migration: add updated_at to existing tables
        try:
            conn.execute("ALTER TABLE fsm_data ADD COLUMN updated_at TEXT")
        except Exception:
            pass  # column already exists
        conn.commit()

    def _make_key(self, key: StorageKey) -> tuple:
        return (str(key.bot_id), key.chat_id, key.user_id, key.destiny)

    # ── set_state ──────────────────────────────────────────────────────────────

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_str: Optional[str] = state.state if hasattr(state, "state") else state
        await asyncio.to_thread(self._sync_set_state, key, state_str)

    def _sync_set_state(self, key: StorageKey, state_str: Optional[str]) -> None:
        bot_id, chat_id, user_id, destiny = self._make_key(key)
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO fsm_data (bot_id, chat_id, user_id, destiny, state, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(bot_id, chat_id, user_id, destiny)
                DO UPDATE SET state = excluded.state, updated_at = datetime('now')
            """, (bot_id, chat_id, user_id, destiny, state_str))
            conn.commit()
        except Exception as e:
            logger.error(f"SQLiteStorage.set_state error: {e}")

    # ── get_state ──────────────────────────────────────────────────────────────

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return await asyncio.to_thread(self._sync_get_state, key)

    def _sync_get_state(self, key: StorageKey) -> Optional[str]:
        bot_id, chat_id, user_id, destiny = self._make_key(key)
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT state FROM fsm_data
                WHERE bot_id=? AND chat_id=? AND user_id=? AND destiny=?
            """, (bot_id, chat_id, user_id, destiny))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.error(f"SQLiteStorage.get_state error: {e}")
            return None

    # ── set_data ───────────────────────────────────────────────────────────────

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        await asyncio.to_thread(self._sync_set_data, key, data)

    def _sync_set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        bot_id, chat_id, user_id, destiny = self._make_key(key)
        data_str = json.dumps(data, ensure_ascii=False)
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO fsm_data (bot_id, chat_id, user_id, destiny, data, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(bot_id, chat_id, user_id, destiny)
                DO UPDATE SET data = excluded.data, updated_at = datetime('now')
            """, (bot_id, chat_id, user_id, destiny, data_str))
            conn.commit()
        except Exception as e:
            logger.error(f"SQLiteStorage.set_data error: {e}")

    # ── get_data ───────────────────────────────────────────────────────────────

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync_get_data, key)

    def _sync_get_data(self, key: StorageKey) -> Dict[str, Any]:
        bot_id, chat_id, user_id, destiny = self._make_key(key)
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT data FROM fsm_data
                WHERE bot_id=? AND chat_id=? AND user_id=? AND destiny=?
            """, (bot_id, chat_id, user_id, destiny))
            row = cursor.fetchone()
            if not row or not row[0]:
                return {}
            return json.loads(row[0])
        except Exception as e:
            logger.error(f"SQLiteStorage.get_data error: {e}")
            return {}

    # ── close ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
