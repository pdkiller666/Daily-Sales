---
name: Annual module/bundle billing
description: How annual (365-day) module/bundle purchase is encoded and kept money-safe
---

Annual billing is an OPTION alongside monthly, not a replacement. Modules/bundles
carry both `price_monthly` and `price_annual` columns (shop_bot.db, added via guarded
ALTER). Annual is offered only when `price_annual > 0`.

**plan_type encoding:** `module_annual_<key>` / `bundle_annual_<key>` (monthly stays
`module_<key>` / `bundle_<key>`). `confirm_payment_request` strips the `annual_` prefix
after the item-type prefix → duration 365, else 30. Bot callbacks: `buymod_annual_<key>`
/ `buybnd_annual_<key>` — parse the `_annual_` form BEFORE the plain form (longest prefix
first) or key becomes `annual_<key>`.

**Why money-safe:** both bot (`subscription_handlers.start_module_purchase`) and web
(`subscription.subscription_module_request`) MUST reject an annual request when the
resolved price is 0 — otherwise a 0₽ request can be approved and grants 365 days free.
The module/bundle branch in `confirm_payment_request` is idempotent by
`payment_request_id` (pre-check on `billing_module_subs.payment_request_id`) and checks
`grant_billing_item` return → compensation (status back to pending) on failure, mirroring
the addon path. Regression test: "module/bundle annual grant" in test_imports.py.

**How to apply:** any new paid-period variant must (a) gate UI on price>0, (b) reject
zero-price server-side on both bot and web, (c) keep grant idempotent by
payment_request_id + add a duplicate-confirmation test.
