---
name: Startup "duplicate column" despite guard = inconsistent WAL
description: create_tables() guarded ALTER throws duplicate-column after a SIGKILLed boot; fix by checkpointing WAL, not editing migrations
---

Symptom: on startup `db.create_tables()` crashes with `sqlite3.OperationalError: duplicate column name: max_shops` (or similar) on an ALTER that is guarded by `if 'col' not in columns:` (PRAGMA table_info check). The column already exists in the .db file, yet the guard evaluated as missing — a contradiction impossible within one healthy connection.

**Root cause:** a previous boot was SIGKILLed mid-migration (e.g. Replit `restart_workflow` sends SIGTERM then SIGKILL after the timeout). That leaves a large un-checkpointed `-wal` file in an inconsistent state, so PRAGMA table_info and the actual ALTER see different schema snapshots.

**Fix (non-destructive):** force a WAL checkpoint on each db, then restart:
```python
import sqlite3
for db in ['data/main.db','data/shop_bot.db','data/fsm_storage.db']:
    c=sqlite3.connect(db); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.commit(); c.close()
```
After this the `-wal` files drop to 0 bytes and the schema is consolidated; the guarded ALTERs then skip correctly.

**Do NOT** edit the migration logic or delete the DBs to "fix" this — the migration guards are correct; the WAL was the problem. Deleting DBs only works because it forces a clean rebuild, but loses data.

**Why:** `restart_workflow` on Replit can SIGKILL a slow-starting process; this bot does heavy create_tables work on boot across main.db + shop_bot.db, so a killed boot is a real risk.
