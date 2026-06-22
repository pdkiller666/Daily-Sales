---
name: GS motivation sync silent no-op
description: Why Google Sheets motivation sync can report success but write 0 rules to the DB
---

# Google Sheets motivation sync — silent zero-write trap

Motivation sync reads a sheet (rows=models, cols=trade networks) and writes
`trade_network`-scoped rules into `product_motivation_rules` via
`set_product_motivation(...)`. A rule is written ONLY when the sheet model name
resolves to a product. Two traps make it silently write nothing while the UI
still says "✅ синхронизировано":

1. **Exact-name match.** Product lookup historically used
   `get_product_by_name()` = `WHERE name = ?` (byte-for-byte). Sheet names rarely
   match exactly → product=None → only `gs_bonus_cache` (the preview) is filled,
   no real rule. **Why:** SQLite `LOWER()` is ASCII-only (won't fold Cyrillic),
   so case-insensitive matching MUST be done in Python (`get_all_products()` →
   `name.strip().lower()` dict). **How to apply:** any sheet→product reconcile
   needs a normalized Python lookup as fallback after the exact match.

2. **`synced` ≠ `rules_written`.** `synced` counts every parsed row with bonuses,
   regardless of product match. The success message historically showed only
   `synced` and hid `rules_written`. **How to apply:** always surface
   `rules_written` + the `unmatched` model list so a 0-rule sync is visible, not a
   fake success.

3. **Blank cells = 0.0 overwrite.** `read_motivation_table` maps empty/"-" cells
   to `0.0`. Writing a `0` rule OVERWRITES a previously set rate with zero (a
   `trade_network` 0 rule beats the global rule in `resolve_motivation`). Skip
   writing when bonus ≤ 0 (treat blank/0 as "no change for that chain").

**Also note:** sync writes with `recalculate=False`, so existing months'
`seller_earnings` are NOT recalculated — new rates apply to future sales; users
checking historical salary totals may still think "nothing changed". The rules
table itself IS updated. `resolve_motivation` priority: user>shop>city>
trade_network>global; import writes `trade_network` scope (vocab is
`trade_network` here, NOT `network`).
