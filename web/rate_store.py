"""Persistent SQLite-backed rate limiter.

Used for security-critical endpoints (auth login) so that rate limits
survive bot restarts and Amvera container redeploys.

For non-security-critical limits (API polling, support form) the in-memory
dicts in each route file remain sufficient.
"""
import os
import sqlite3
import time
import threading

_lock = threading.Lock()
_DB_PATH = "data/rate_limits.db"


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
    conn.commit()
    return conn


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
        except Exception:
            return True  # fail open — availability > strict limiting
