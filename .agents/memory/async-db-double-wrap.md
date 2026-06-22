---
name: AsyncDatabase double-wrap bug
description: Why asyncio.to_thread(current_db.method) is always a bug in bot handlers
---

# AsyncDatabase double-wrap (asyncio.to_thread on an already-async DB)

`get_db(...)` in `db_utils.py` returns an `AsyncDatabase`, whose `__getattr__`
wraps EVERY underlying sync `Database` method in `asyncio.to_thread` and returns
an async wrapper. So `current_db.some_method` is a coroutine function.

**The bug:** `await asyncio.to_thread(current_db.some_method, args)` runs the
async wrapper inside a worker thread. Calling an async function returns a
coroutine object (never awaited); the thread returns that coroutine. So the
caller gets a **coroutine object, not the query result**.

**Symptoms:**
- `if not rows:` / `if existing:` checks see a truthy coroutine → wrong branch
  (e.g. "артикул уже занят" always, or duplicate checks silently bypassed).
- Passing the coroutine onward (`paginate(coro)`, indexing, iteration) →
  `TypeError` → caught by an outer `except` → user sees "⚠️ Произошла ошибка".

**Fix:** always call directly: `await current_db.some_method(args)`.

**How to apply:** grep `asyncio\.to_thread\(current_db\.` (and any
`*_db` var that came from `get_db`) — every hit is a double-wrap. Legit
`asyncio.to_thread(...)` calls wrap *standalone sync functions* (local `_do`,
billing `check_*_limit`, raw-sqlite readers taking `db.db_file`), NOT
AsyncDatabase methods. `main.py` jobs use `shop_bot_db` (a plain `Database`),
so `to_thread` there is correct.

**History:** found and removed across commission/products/sales/inventory
handlers (31 sites total) in June 2026 after the "Таргетированные правила"
button broke.
