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

_db_tables_created = False

# ─── Rate-limits in-memory cache (TTL 60 s) ───────────────────────────────────
_rl_cache: dict = {}
_rl_cache_lock = threading.Lock()
_RL_CACHE_TTL = 60  # seconds


def _ensure_tables() -> None:
    """Create tables once per process lifetime. No-op on subsequent calls."""
    global _db_tables_created
    if _db_tables_created:
        return
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
    try:
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_cost_log (
                date              TEXT NOT NULL,
                provider          TEXT NOT NULL,
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd          REAL    NOT NULL DEFAULT 0.0,
                PRIMARY KEY (date, provider)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_org_usage_log (
                org_key    TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                count      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (org_key, usage_date)
            )
        """)
        conn.commit()
        _db_tables_created = True
    finally:
        conn.close()


def _get_conn() -> sqlite3.Connection:
    _ensure_tables()
    conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ─── AI daily usage tracking ─────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def get_ai_rate_limits() -> tuple[int, int]:
    """Возвращает (base_daily_limit, high_daily_limit) из ai_rate_config в shop_bot.db.
    Результат кэшируется на 60 секунд. Fallback: (20, 200).
    """
    now = time.time()
    with _rl_cache_lock:
        cached = _rl_cache.get("limits")
        if cached and (now - cached["ts"]) < _RL_CACHE_TTL:
            return cached["base"], cached["high"]
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT key, value FROM ai_rate_config WHERE key IN ('base_daily_limit','high_daily_limit')"
        ).fetchall()
        conn.close()
        cfg = {r[0]: int(r[1]) for r in rows}
        base = cfg.get("base_daily_limit", 20)
        high = cfg.get("high_daily_limit", 200)
    except Exception:
        return 20, 200
    with _rl_cache_lock:
        _rl_cache["limits"] = {"base": base, "high": high, "ts": now}
    return base, high


def get_ai_chat_daily_limit() -> int:
    """Возвращает chat_daily_limit из ai_rate_config в shop_bot.db.
    Результат кэшируется на 60 секунд. Fallback: 50.
    """
    now = time.time()
    with _rl_cache_lock:
        cached = _rl_cache.get("chat_limit")
        if cached and (now - cached["ts"]) < _RL_CACHE_TTL:
            return cached["val"]
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT value FROM ai_rate_config WHERE key = 'chat_daily_limit'"
        ).fetchone()
        conn.close()
        val = int(row[0]) if row else 50
    except Exception:
        return 50
    with _rl_cache_lock:
        _rl_cache["chat_limit"] = {"val": val, "ts": now}
    return val


def invalidate_rate_limits_cache() -> None:
    """Сбрасывает кэш лимитов — вызывать после изменения ai_rate_config."""
    with _rl_cache_lock:
        _rl_cache.clear()


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
                logging.error(f"check_and_increment_ai({tg_id}): {e} — fail-closed")
            except Exception:
                pass
            return False  # fail-closed: при сбое БД лимит держим, не открываем


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


def get_ai_enabled() -> bool:
    """Возвращает True если AI включён (по умолчанию True).
    Kill-switch: запись ai_rate_config key='ai_enabled' value='0' → выключен.
    Также проверяет env var AI_ENABLED=0.
    """
    import os as _os
    if _os.getenv("AI_ENABLED", "1").strip() == "0":
        return False
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT value FROM ai_rate_config WHERE key = 'ai_enabled'"
        ).fetchone()
        conn.close()
        if row is not None:
            return row[0].strip() != "0"
    except Exception:
        pass
    return True


def set_ai_enabled(enabled: bool) -> None:
    """Записывает флаг ai_enabled в ai_rate_config в shop_bot.db."""
    value = "1" if enabled else "0"
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ai_rate_config "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO ai_rate_config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            ("ai_enabled", value),
        )
        conn.commit()
        conn.close()
    except Exception:
        import logging as _lg
        _lg.error("set_ai_enabled: failed to write to shop_bot.db")


def get_anomaly_threshold() -> int:
    """Возвращает порог аномалии дневных AI-запросов (дефолт 500)."""
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT value FROM ai_rate_config WHERE key = 'anomaly_daily_threshold'"
        ).fetchone()
        conn.close()
        if row is not None:
            return max(1, int(row[0]))
    except Exception:
        pass
    return 500


def set_anomaly_threshold(threshold: int) -> None:
    """Сохраняет порог аномалии в ai_rate_config."""
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ai_rate_config "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO ai_rate_config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            ("anomaly_daily_threshold", str(max(1, threshold))),
        )
        conn.commit()
        conn.close()
    except Exception:
        import logging as _lg
        _lg.error("set_anomaly_threshold: failed to write to shop_bot.db")


# ─── Persistent token/cost tracking ──────────────────────────────────────────

def persist_token_cost(date: str, provider: str, prompt: int, completion: int, cost_usd: float) -> None:
    """Накапливает токены и стоимость в ai_cost_log (upsert по date+provider).
    Данные выживают рестарты сервера. Non-critical: ошибки логируются, не пробрасываются.
    """
    if not prompt and not completion:
        return
    with _lock:
        try:
            conn = _get_conn()
            conn.execute(
                """INSERT INTO ai_cost_log (date, provider, prompt_tokens, completion_tokens, cost_usd)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(date, provider) DO UPDATE SET
                       prompt_tokens     = prompt_tokens     + excluded.prompt_tokens,
                       completion_tokens = completion_tokens + excluded.completion_tokens,
                       cost_usd          = cost_usd          + excluded.cost_usd""",
                (date, provider, prompt, completion, cost_usd),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            try:
                import logging as _lg
                _lg.warning("persist_token_cost: %s", e)
            except Exception:
                pass


def get_ai_cost_history(days: int = 30) -> list:
    """Возвращает историю расходов AI за последние N дней из ai_cost_log.
    Каждый элемент: {date, provider, prompt_tokens, completion_tokens, cost_usd}.
    Отсортировано по дате ASC.
    """
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with _lock:
        try:
            conn = _get_conn()
            rows = conn.execute(
                """SELECT date, provider, prompt_tokens, completion_tokens, cost_usd
                   FROM ai_cost_log WHERE date >= ? ORDER BY date ASC, provider ASC""",
                (cutoff,),
            ).fetchall()
            conn.close()
            return [
                {
                    "date":              r[0],
                    "provider":          r[1],
                    "prompt_tokens":     int(r[2] or 0),
                    "completion_tokens": int(r[3] or 0),
                    "cost_usd":          round(float(r[4] or 0), 8),
                }
                for r in rows
            ]
        except Exception:
            return []


def get_ai_cost_totals() -> dict:
    """Возвращает суммарную стоимость AI за всё время из ai_cost_log.
    {total_prompt_tokens, total_completion_tokens, total_cost_usd, by_provider: [...]}
    """
    with _lock:
        try:
            conn = _get_conn()
            rows = conn.execute(
                """SELECT provider, SUM(prompt_tokens), SUM(completion_tokens), SUM(cost_usd)
                   FROM ai_cost_log GROUP BY provider ORDER BY SUM(cost_usd) DESC"""
            ).fetchall()
            conn.close()
            by_provider = [
                {
                    "provider":          r[0],
                    "prompt_tokens":     int(r[1] or 0),
                    "completion_tokens": int(r[2] or 0),
                    "cost_usd":          round(float(r[3] or 0), 8),
                }
                for r in rows
            ]
            return {
                "total_prompt_tokens":     sum(p["prompt_tokens"]     for p in by_provider),
                "total_completion_tokens": sum(p["completion_tokens"] for p in by_provider),
                "total_cost_usd":          round(sum(p["cost_usd"]    for p in by_provider), 8),
                "by_provider":             by_provider,
            }
        except Exception:
            return {
                "total_prompt_tokens": 0, "total_completion_tokens": 0,
                "total_cost_usd": 0.0, "by_provider": [],
            }


def get_ai_total_today() -> int:
    """Возвращает суммарное число AI-запросов за сегодня (UTC) из ai_usage_log."""
    today = _today_utc()
    with _lock:
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM ai_usage_log WHERE usage_date = ?",
                (today,),
            ).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0


# ─── Per-org chat AI quota ────────────────────────────────────────────────────

def _org_key(org_db: str) -> str:
    """Возвращает короткий ключ для org_db — базовое имя файла без пути."""
    import os as _os
    return _os.path.basename(org_db)


def check_and_increment_ai_for_org(org_db: str, limit: int) -> bool:
    """Проверяет и инкрементирует дневной лимит AI-запросов на уровне организации.

    Каждая орг имеет свой счётчик, не зависящий от других орг того же владельца.
    Возвращает True если запрос разрешён, False если лимит превышен.
    """
    today = _today_utc()
    key = _org_key(org_db)
    with _lock:
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT count FROM ai_org_usage_log WHERE org_key = ? AND usage_date = ?",
                (key, today),
            ).fetchone()
            current = row[0] if row else 0
            if current >= limit:
                conn.close()
                return False
            conn.execute(
                """INSERT INTO ai_org_usage_log (org_key, usage_date, count) VALUES (?, ?, 1)
                   ON CONFLICT(org_key, usage_date) DO UPDATE SET count = count + 1""",
                (key, today),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            try:
                import logging
                logging.error("check_and_increment_ai_for_org(%s): %s — fail-closed", key, e)
            except Exception:
                pass
            return False


# ─── Custom per-user AI limits ────────────────────────────────────────────────

def get_custom_ai_limit(tg_id: int) -> "int | None":
    """Возвращает кастомный дневной лимит AI для пользователя или None если не задан."""
    key = f"custom_limit_{tg_id}"
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT value FROM ai_rate_config WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row is not None:
            return max(0, int(row[0]))
    except Exception:
        pass
    return None


def set_custom_ai_limit(tg_id: int, limit: int) -> None:
    """Сохраняет кастомный лимит AI для конкретного пользователя."""
    key = f"custom_limit_{tg_id}"
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute(
            "INSERT OR REPLACE INTO ai_rate_config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, str(max(0, limit))),
        )
        conn.commit()
        conn.close()
    except Exception:
        import logging as _lg
        _lg.error("set_custom_ai_limit(%s): failed", tg_id)


def clear_custom_ai_limit(tg_id: int) -> None:
    """Удаляет кастомный лимит AI для пользователя (вернёт к стандартному)."""
    key = f"custom_limit_{tg_id}"
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM ai_rate_config WHERE key = ?", (key,))
        conn.commit()
        conn.close()
    except Exception:
        import logging as _lg
        _lg.error("clear_custom_ai_limit(%s): failed", tg_id)


def get_all_custom_limits() -> list:
    """Возвращает все кастомные лимиты пользователей.
    Каждый элемент: {tg_id, limit, name}.
    """
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB, timeout=3, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT key, value FROM ai_rate_config WHERE key LIKE 'custom_limit_%' ORDER BY key"
        ).fetchall()
        conn.close()
        result = []
        for k, v in rows:
            try:
                tg_id = int(k.replace("custom_limit_", ""))
                result.append({
                    "tg_id": tg_id,
                    "limit": int(v),
                    "name": _lookup_user_name(tg_id),
                })
            except (ValueError, TypeError):
                pass
        return result
    except Exception:
        return []


def get_ai_yesterday_total() -> int:
    """Возвращает суммарное число AI-запросов за вчера (UTC) из ai_usage_log."""
    yesterday = (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    with _lock:
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM ai_usage_log WHERE usage_date = ?",
                (yesterday,),
            ).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0


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
