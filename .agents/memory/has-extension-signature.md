---
name: has_extension signature
description: Correct call signature for has_extension() and the silent-rollback failure mode when it's wrong
---

`has_extension(tg_id, ext_key)` takes exactly two positional arguments: the Telegram id and a
single combined extension key (e.g. `"services_motivation"`), not three (`tg_id, module, ext`).

**Why:** four call sites (`web/routes/appointments.py`, `web/routes/services.py` ×2,
`services_handlers.py`) called it with 3 args. This threw `TypeError` *before* the surrounding
`conn.commit()`, so the exception silently rolled back the whole status-update transaction whenever
a staff member was assigned to an appointment/service — appointment completion (web and bot)
appeared to do nothing, with no visible error to the user.

**How to apply:** whenever adding new logic in the same code path as an existing
`has_extension(...)` call (e.g. anything gated on appointment/service completion), grep for
`has_extension(` first and confirm all call sites still pass exactly 2 args. This bug pattern
(billing/extension gate call raising before a commit → whole transaction silently vanishes) is
worth checking for other `has_module`/`has_extension`/`has_addon`-style gates guarding a
multi-statement transaction.
