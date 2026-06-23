---
name: Referral bonus grant
description: referrals table column is `applied` (not `bonus_granted`); atomic claim + bonus-module grant
---

# Referral bonus grant (apply_referral_bonus)

- The `referrals` table column is **`applied`** (+ `applied_at`), NOT `bonus_granted`.
  For a long time `apply_referral_bonus`/`get_referral_stats` queried a non-existent
  `bonus_granted` column → every call threw `no such column`, was swallowed by the
  outer `except` → returned False. **Referral bonuses silently never granted.**
  - The Python dict key `'bonus_granted'` in `get_referral_stats`'s return value is a
    caller-facing API key — keep it; it is NOT a DB column.

- **Idempotency is an atomic compare-and-set**, not SELECT-then-UPDATE:
  `UPDATE referrals SET applied=1, applied_at=... WHERE referred_telegram_id=? AND
  referrer_telegram_id=? AND applied=0` then check `cursor.rowcount==1` BEFORE issuing
  any bonus. Without `applied=0` in the UPDATE predicate + rowcount gate, two concurrent
  calls both read 0 and double-grant.
  **Why:** the bonus now grants TWO things (module `analytics` 30d via
  `grant_billing_item(granted_by='referral_bonus')` + `_extend_subscription_by_days(...,30)`);
  `grant_billing_item` has no payment_request_id on this path, so it can't absorb a
  duplicate — the atomic claim is the only guard.
  **How to apply:** any future "one-time per row" grant on `referrals` (or similar
  flag-gated tables) must use the conditional-UPDATE + rowcount pattern, covered by a
  concurrency test (see test_imports.py §6c — 8 parallel threads, assert exactly 1 grant).
