---
name: Web hot-path DB connection pooling
description: Как не тормозить веб-кабинет при переключении разделов — почему raw sqlite3.connect() в Jinja2-globals это узкое место, а не create_tables()
---

При жалобе «переключение модулей в веб-кабинете медленное» причина почти никогда не в `create_tables()` — он кэшируется в процессе (`_INITIALIZED_DBS` в `database.py`), повторные вызовы почти бесплатны.

Реальная причина: любой Jinja2 global (`nav_modules`, счётчики в сайдбаре, `current_org_name`, `beta_mode`, `chat_enabled` и т.п.) вызывается на **каждом** рендере **каждой** страницы. Если такой global открывает `sqlite3.connect(path)` напрямую (а не через `Database(path).get_connection()`), это лишний open/close файлового дескриптора на каждый клик — и особенно больно, если global вызывается в цикле (например, `nav_modules` раньше дёргал `get_user_module_access()` по разу на каждый из ~10 модулей меню = 10 raw-подключений на страницу).

**Why:** в `database.py` уже есть thread-local пул соединений (`_get_pooled_conn`), который один раз на (поток × файл БД) открывает соединение и настраивает все нужные PRAGMA (WAL, synchronous, cache_size, mmap). Raw `sqlite3.connect()` в обход этого пула — это не переиспользование, а всегда новый connect/disconnect.

**How to apply:** при добавлении нового Jinja2 global / счётчика в `web/app.py`, который трогает БД — использовать `Database(path).get_connection()`, никогда голый `sqlite3.connect(path)`. Если global нужен несколько раз за один рендер (как раньше было с одинаковым `SELECT id FROM users WHERE telegram_id=?` в `open_tasks_count` и `dm_unread_count`) — мемоизировать промежуточный результат на `request.state` (паттерн уже используется в `nav_modules`/`chat_enabled`), а не пересчитывать в каждом global заново.

Также: `web/deps.py get_web_db()` раньше на каждый запрос открывал ВТОРОЕ соединение только чтобы повторно выставить те же PRAGMA journal_mode/synchronous, которые пул уже ставит при создании соединения — было чистым оверхедом, убрано (`_enable_wal` удалён).
