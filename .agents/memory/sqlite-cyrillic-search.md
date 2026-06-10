---
name: SQLite Cyrillic case-insensitive search
description: SQLite LOWER()/UPPER()/LIKE only fold ASCII case — Cyrillic search silently returns nothing
---

# SQLite LOWER()/LIKE is ASCII-only — breaks Cyrillic search

SQLite's built-in `LOWER()`, `UPPER()`, and the case-insensitivity of `LIKE`
only work on ASCII characters. For Cyrillic (and other non-ASCII), `LOWER('Тарасов')`
returns `'Тарасов'` unchanged, and `'Тарасов' LIKE '%тарасов%'` is FALSE.

**Symptom:** a search box finds Latin names but silently returns nothing for
Russian names (e.g. global user search found orgs by name — that comparison ran
in Python via `q.lower()` — but `WHERE LOWER(col) LIKE ?` against a Python-lowercased
query never matched Cyrillic rows).

**Fix:** register a Python-backed function on the connection and use it instead of
SQL `LOWER()`:
```python
conn.create_function("lower_u", 1, lambda s: s.lower() if isinstance(s, str) else s)
# then: WHERE lower_u(col) LIKE ?   with a Python-lowercased pattern
```
Python's `str.lower()` is Unicode-aware, so both sides fold correctly.

**Why:** Python `.lower()` ≠ SQLite `LOWER()`. Mixing them (lowercase the query in
Python, lowercase the column in SQL) guarantees a mismatch for any non-ASCII text.

**How to apply:** any time you build a `LIKE`-based text search over user-entered
names/shops in SQLite where the data can be Russian, route the column through
`lower_u()` (or filter in Python). `_raw_conn()` in `web/routes/admin.py` registers
`lower_u` for all raw super-admin connections.
