---
name: GS import + motivation configurator
description: Google Sheets motivation table orientation, saved-config sync, clickable import column mapping, and the web/bot split.
---

# Motivation table orientation (the inversion fix)
- Real motivation sheets are **rows = models, columns = chains (сети)**. Earlier code read it transposed.
- Provider: `read_motivation_table(config, sheet_name, header_row, model_col, bonus_col_map: dict[int,str], rrp_col=None)` → `list[dict(model, rrp, bonuses={chain: float})]`. Chain name = header-row cell text (fallback `Кол.{letter}`). JSON keys normalized to int. Old `read_motivation_rows` is DEPRECATED.
- **Why:** bonus lookup (`get_bonus_for_model`) and the future targeted-motivation consumer depend on (model, chain) pairs; a transposed read silently produced wrong/empty bonuses.

# Saved-config sync
- Manager stores the wizard answers in the connection config under `config['motiv_config']` (`get_motiv_config`/`save_motiv_config`). `run_motiv_sync_from_config(db, conn_id)` replays it.
- `sync_motivation_from_sheet(...)` does `clear_bonus_cache(conn_id)` then per-(model,chain) `upsert_bonus_cache`, and **returns `{synced, models, sheet, chains}`** — web/bot must read `synced` (not `imported`/`upserted`) and `chains` (sorted list) for the success message.

# Import column mapping: bot is precise, web is default-order
- Bot wizard reads the real sheet and lets the user click columns onto fields (`gs_impc_*` pickers), producing a 1-based selection converted to **0-based `col_mapping`** for `run_import`.
- Web import route passes `col_mapping=None` → `run_import` falls back to default left-to-right column order. Web is the quick path; use the bot wizard for exact binding.
- `run_import` supported `import_type` values: **products / inventory / sales / staff / plans**. Validate against exactly this set in the web route.

# aiogram prefix gotcha
- `gs_impc_` and `gs_imptyp_` do NOT collide with the `startswith("gs_import_")` handler: they differ at index 6 (`c`/`t` vs `o`). Keep new pickers registered before the broader menu handler anyway.

# Aliases direction (sheet → system)
- Alias map is keyed by the **sheet's** model name, value is the **system** model name (`{sheet: system}`). Manager applies it case-insensitively/trimmed to each row's model BEFORE upsert. The bot prompt must say "Название в листе → Название в системе" or the mapping inverts and matches nothing.
- **Why:** targeted-motivation lookup keys on the system model name; if aliases were stored system→sheet the cache would store sheet names and bonus lookups would miss.

# Manual fallback (GS API unreachable)
- When the live sheet read fails, the wizard must still be completable by manual numeric entry (1-based rows/cols), mirroring export. Manual path feeds the SAME finalize as the clickable path, so it must set every state key the pickers set (`gs_mtv_header_row`, `gs_mtv_model_col`, `gs_mtv_bonus_map`, `gs_mtv_rrp_col`) before finalize.
- **Why:** Amvera/Google outages otherwise hard-block setup with no recovery; finalize reads only from state, so a partial manual fill silently produced a broken config until a guard was added.

# Web async safety
- New web routes are `async def` and `await integration_manager....` directly. Safe because the provider wraps gspread network calls in `asyncio.to_thread`; only short SQLite upsert loops run on the request worker. For very large imports, consider a threadpool/background task.
