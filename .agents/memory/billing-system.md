---
name: Modular billing system
description: Architecture of billing_modules/extensions/bundles/subs tables, billing_utils.py, and super-admin /admin/billing/* routes
---

## Tables (shop_bot.db only, guarded by `if 'shop_bot' in self.db_file`)
- `billing_modules` — key UNIQUE, name, icon, description, price_monthly, sort_order, is_active, features_json
- `billing_extensions` — module_key FK→modules.key, key UNIQUE, name, icon, price_monthly, sort_order, is_active
- `billing_bundles` — key UNIQUE, name, icon, includes_json `{"modules":[...],"extensions":[...]}`, price_monthly, is_active
- `billing_module_subs` — user_telegram_id, item_type (module|extension|bundle), item_key, price_paid, start_date, end_date, is_active, granted_by, note

## Default data (_init_billing_defaults — only runs if billing_modules is empty)
- 7 modules: analytics(299₽), team(399₽), notifications(199₽), plans_motivation(249₽), ai_assistant(299₽), integrations(399₽), chat(149₽)
- 17 extensions across all 6 modules (abc_analysis, heatmap, contests, scheduled_notifs, plan_filters, ai_forecast, gs_realtime, etc.)
- 3 bundles: small_biz(399₽), team_bundle(699₽), all_in_one(1299₽)

## billing_utils.py (top-level module)
- `has_module(tg_id, module_key)` — priority: super_admin → trial → direct grant → bundle expansion → legacy plan map
- `has_extension(tg_id, ext_key)` — same priority chain
- `get_active_billing_items(tg_id)` → `{modules: set, extensions: set, bundles: set}`
- Legacy plan map: Бесплатный→{}, Базовый→{analytics,notifications}, Стандарт→+integrations, Премиум→all

## Key implementation rules
- `get_all_billing_bundles()` returns dicts with `includes` key (pre-parsed from includes_json) — templates use `b.includes.get('modules', [])` directly, NOT `fromjson` Jinja2 filter (it doesn't exist)
- Bundle grants stored as item_type='bundle'; billing_utils expands bundle→module set at check time via _bundle_modules()
- `grant_billing_item(duration_days=0)` → end_date=NULL (perpetual)
- `upsert_billing_module/extension/bundle` uses `INSERT ... ON CONFLICT(key) DO UPDATE` pattern

## Routes (/admin/billing/*)
- GET `/admin/billing` — hub with stats cards + module overview
- GET `/admin/billing/modules?tab=modules|extensions` — tabs, edit modals (Alpine x-show + editMod/editExt state)
- GET `/admin/billing/bundles` — bundles list, edit modal uses `JSON.parse(editBnd.includes_json)` client-side
- GET `/admin/billing/grants?q=` — search by tg_id/item_key/granted_by; POST grant + POST revoke/{id}
- All routes guarded by `_guard(user)` (role != 'super_admin' → redirect)
- `/admin` already in robots.txt Disallow — no new entry needed for /admin/billing

**Why:** Needed a catalog-driven feature flag system to gradually roll out per-module pricing without breaking legacy plan subscribers.

**How to apply:** When adding a new chargeable feature, add it to `billing_modules` (or as an extension), then gate it with `billing_utils.has_module(tg_id, 'key')` in the web route or bot handler.
