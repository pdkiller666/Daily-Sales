---
name: Bot super-admin billing card (telegram_id vs users.id)
description: How the bot "Платёжная система" panel addresses customers for base subs vs modular billing items
---

The bot super-admin billing panel (payment_system_admin.py) mirrors web /admin/billing
(source of truth: web/routes/admin_billing.py). Two different identity keys are in play and
mixing them silently grants to the wrong/no user:

- **Modular billing items** (modules/extensions/bundles): `grant_billing_item` /
  `revoke_billing_item` / `get_billing_module_subs` are keyed by **user_telegram_id** (the
  raw Telegram ID). Pass the Telegram ID directly.
- **Base subscription**: `subscriptions.user_id` is the **internal users.id**, NOT the
  Telegram ID. Resolve via `db.get_user_id(telegram_id)` first, then
  `create_subscription(user_id, plan_name)` / `get_user_subscription(user_id)`.

**Why:** a unified "customer card by Telegram ID" must translate the same input ID two ways.
Using the Telegram ID where users.id is expected (or vice-versa) creates orphan rows.

**How to apply:** after any grant/revoke/set-base, call `invalidate_plan_cache(telegram_id)`
(from subscription_utils). Notify via `bot.send_message` but skip `tg_id < 0` (email-only
synthetic IDs have no Telegram chat). Grant catalog uses positional index in deterministic
catalog order for ≤64-byte callbacks (gbp_{idx}_{tg} → gbd_{idx}_{tg}_{days}); rebuild the
same catalog on each step — fine unless the catalog is edited mid-flow.
