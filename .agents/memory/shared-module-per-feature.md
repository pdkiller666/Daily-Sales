---
name: Shared module per feature
description: Where to put sell/consume/commission-style business logic when a feature is triggered from web, bot, and auto-triggers
---

For a feature that can be triggered from multiple channels (web route, Telegram bot, an automatic
trigger like appointment-completion), put the core logic in a single `*_utils.py` module
(e.g. `packages_utils.py` for the subscription-packages/абонементы module).

**Why:** earlier modules (services, packages) had the same sell/consume/commission logic
duplicated across web routes and bot handlers, which drifted out of sync. A shared module keeps
web, bot, and auto-consume triggers calling identical, tested logic.

**How to apply:**
- Functions in the shared module do the DB reads/writes but do NOT `conn.commit()` — the caller
  commits, so partial writes across multiple shared calls in one request/transaction stay atomic.
- Every caller must actually call `conn.commit()` after checking for an `error` key/None return.
- Time-based validity (e.g. `expires_at`) must be checked inside the shared "find candidate" and
  "consume" functions themselves, not only in a separate cron/auto-expire job — otherwise a stale
  `status='active'` row can still be consumed between expiry and the next cron run.
- Commission/motivation calculations reusing an existing rules table (e.g.
  `service_motivation_rules`) should write to their own dedicated earnings table
  (e.g. `package_sale_earnings`) mirroring the existing `service_earnings` table shape, so
  reports/salary code can UNION them with a consistent pattern.
