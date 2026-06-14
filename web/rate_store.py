"""Persistent SQLite-backed rate limiter.

Used for security-critical endpoints (auth login) so that rate limits
survive bot restarts and Amvera container redeploys.

For non-security-critical limits (API polling, support form) the in-memory
dicts in each route file remain sufficient.
"""
import datetime
import os
import sqlite3
import time
import threading

_lock = threading.Lock()
_DB_PATH = "data/rate_limits.db"

_SHOP_BOT_DB = "data/shop_bot.db"


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_hits (
            key    TEXT NOT NULL,
            hit_at REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_key_time ON rate_hits(key, hit_at)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage_log (
            tg_id      INTEGER NOT NULL,
            usage_date TEXT    NOT NULL,
            count      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tg_id, usage_date)
        )
    """)
    conn.commit()
    return conn


# ─── AI daily usage tracking ─────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def get_ai_rate_limits() -> tuple[int, int]:
    """Возвращает (base_daily_limit, high_daily_limit) из ai_rate_config в shop_bot.db.
    Fallback: (20, 200) если таблица/БД недоступны.
    """
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT key, value FROM ai_rate_config WHERE key IN ('base_daily_limit','high_daily_limit')"
        ).fetchall()
        conn.close()
        cfg = {r[0]: int(r[1]) for r in rows}
        return cfg.get("base_daily_limit", 20), cfg.get("high_daily_limit", 200)
    except Exception:
        return 20, 200


def get_ai_daily_usage(tg_id: int) -> int:
    """Возвращает количество AI-запросов пользователя за сегодня (UTC)."""
    today = _today_utc()
    with _lock:
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT count FROM ai_usage_log WHERE tg_id = ? AND usage_date = ?",
                (tg_id, today),
            ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0


def check_and_increment_ai(tg_id: int, limit: int) -> bool:
    """Проверяет дневной лимит и инкрементирует счётчик если разрешено.
    Возвращает True если запрос разрешён, False если лимит превышен.
    """
    today = _today_utc()
    with _lock:
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT count FROM ai_usage_log WHERE tg_id = ? AND usage_date = ?",
                (tg_id, today),
            ).fetchone()
            current = row[0] if row else 0
            if current >= limit:
                conn.close()
                return False
            conn.execute(
                """INSERT INTO ai_usage_log (tg_id, usage_date, count) VALUES (?, ?, 1)
                   ON CONFLICT(tg_id, usage_date) DO UPDATE SET count = count + 1""",
                (tg_id, today),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            try:
                import logging
                logging.error(f"check_and_increment_ai({tg_id}): {e} — fail-open")
            except Exception:
                pass
            return True


def get_ai_usage_stats_today(top_n: int = 30) -> list:
    """Возвращает топ пользователей по AI-запросам за сегодня.
    Каждый элемент: {'tg_id': int, 'count': int, 'name': str}
    """
    today = _today_utc()
    with _lock:
        try:
            conn = _get_conn()
            rows = conn.execute(
                """SELECT tg_id, count FROM ai_usage_log
                   WHERE usage_date = ? ORDER BY count DESC LIMIT ?""",
                (today, top_n),
            ).fetchall()
            conn.close()
            result = []
            for tg_id, count in rows:
                name = _lookup_user_name(tg_id)
                result.append({"tg_id": tg_id, "count": count, "name": name})
            return result
        except Exception:
            return []


def _lookup_user_name(tg_id: int) -> str:
    """Ищет имя пользователя в shop_bot.db users."""
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        row = conn.execute(
            "SELECT first_name, last_name FROM users WHERE telegram_id = ?", (tg_id,)
        ).fetchone()
        conn.close()
        if row:
            return f"{row[0]} {row[1] or ''}".strip()
    except Exception:
        pass
    return f"ID {tg_id}"


# ─── Auth rate limiting ───────────────────────────────────────────────────────

def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """Return True if request is allowed, False if rate-limited.

    Args:
        key: unique scope string, e.g. 'auth:1.2.3.4'
        max_requests: allowed requests per window
        window_seconds: rolling window length in seconds
    """
    now = time.time()
    cutoff = now - window_seconds
    with _lock:
        try:
            conn = _get_conn()
            conn.execute(
                "DELETE FROM rate_hits WHERE key = ? AND hit_at < ?", (key, cutoff)
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_hits WHERE key = ? AND hit_at >= ?",
                (key, cutoff),
            ).fetchone()[0]
            if count >= max_requests:
                conn.commit()
                conn.close()
                return False
            conn.execute(
                "INSERT INTO rate_hits (key, hit_at) VALUES (?, ?)", (key, now)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            # fail-closed: для auth-эндпоинтов сбой стора НЕ должен отключать
            # защиту от перебора. Запрос блокируется, пользователь может повторить.
            try:
                import logging
                logging.error(f"check_rate_limit({key}): {e} — fail-closed")
            except Exception:
                pass
            return False
