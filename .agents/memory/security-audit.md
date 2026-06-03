---
name: Security audit findings
description: Key non-obvious security rules and patterns found during comprehensive audit — must apply to all future code
---

## Critical rules going forward

### 1. verify_csrf_token MUST check return value
`verify_csrf_token(request, token)` returns bool — always wrap in `if not`:
```python
if not verify_csrf_token(request, csrf_token):
    return RedirectResponse("/dashboard", 303)
```
The admin.py was guilty of calling it without checking — 8 routes had broken CSRF.

**Why:** The function doesn't raise, it returns False. Ignoring the return = no CSRF protection at all.

**How to apply:** Every new POST route must use the `if not` pattern, never bare call.

### 2. Subscription limits must be enforced in web layer too
`subscription_utils.check_product_limit()`, `check_sales_limit()`, `check_shop_limit()` exist and are used in the bot. They MUST also be called from web routes before creating products/sales/shops.

**Why:** Web layer previously had zero limit enforcement — users could bypass plan limits entirely via web interface.

**How to apply:** Any web POST route that creates a product/sale/shop must call the corresponding check function.

### 3. Never expose str(exc) to users
- Template routes: use `ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."` + `logging.error(exc)`
- JSON API routes: `{"error": "Внутренняя ошибка сервера"}`
- URL params (`_err(f"... {e}")`) in import routes: use generic message + log.

**Why:** str(exc) leaks SQLite DB paths, table names, column names, internal paths.

### 4. aiohttp 3.12.x works with aiogram 3.20 despite declared constraint
aiogram 3.20 declares `aiohttp<3.12` but 3.12.15 is runtime-compatible. Keep aiohttp at 3.12.x for CVE fixes. If aiogram is upgraded, re-check.

### 5. add_sale TOCTOU fix
`UPDATE inventory ... AND quantity >= ?` prevents race condition where two concurrent sales could both pass the SELECT check but bring inventory negative.

### 6. TOCTOU pattern for atomic DB updates
When checking a value then updating it, add the constraint to the WHERE clause of the UPDATE, not just as a pre-check SELECT. Check `cursor.rowcount == 0` to detect the race.

### 7. Google OAuth tokens stored plaintext in integration_connections.config
Known acceptable risk — tokens are server-side only, in SQLite files with no public access path. If encryption is ever added, use a key derived from BOT_TOKEN.

### 8. CSP header in SecurityHeadersMiddleware
`_CSP` constant in `web/app.py` — allow list includes unpkg.com, cdn.jsdelivr.net, cdn.tailwindcss.com, telegram.org, fonts.googleapis.com/gstatic.com. `'unsafe-inline'` required for Alpine.js + Chart.js inline configs.

### 9. secrets.choice for all generated codes/tokens
- `payment_system_admin.py` promo code gen: use `secrets.choice`, not `random.choices`
- `tenant_manager.py` already uses secrets ✅
- `web_login_codes.py` uses secrets ✅ (now 8 digits, was 6)

### 10. Input validation checklist for new web routes (POST)
1. CSRF: `if not verify_csrf_token(...)` → redirect
2. Subscription gate: `check_*_permission()` before creating resources
3. Enum fields: `re.match(r'^[a-z_]{1,50}$', atype)` or explicit `in {set}` check
4. Year/month: `year = max(2015, min(year, 2040))`, `month = max(1, min(month, 12))`
5. Float amounts: `amount = max(-1_000_000.0, min(amount, 1_000_000.0))`
6. String fields: `.strip()[:500]` before DB write; `maxlength="500"` on HTML input
7. Image uploads: `_is_valid_image(raw)` magic bytes check (JPEG/PNG/WebP)
8. Error disclosure: `logging.error(e)` + generic user message, never `str(e)` to user

### 11. N+1 DB query fix pattern for salary/stats pages
Use `get_salary_bulk_stats(year, month, start, end)` → returns `{user_id: {worked, adj_sum, motivation}}` via 3 GROUP BY queries for ALL users. paid_abs still per-user (calendar intersection logic). Pattern: prefetch bulk dict, then loop and `.get(uid, defaults)`.

### 12. _import_sessions memory management
`_import_sessions` dict in `web/routes/products.py`: every session gets `"_ts": time.time()`. Call `_cleanup_import_sessions()` before creating a new session to evict entries older than 1h (`_IMPORT_SESSION_TTL = 3600`). Same pattern should apply to any future in-memory session store.

### 13. DB indexes added in create_tables()
`idx_salary_adj_user_ym`, `idx_seller_earnings_user`, `idx_absence_rec_user_dt`, `idx_work_sched_user_date` — all `CREATE INDEX IF NOT EXISTS`. Always add indexes this way so they auto-apply on Amvera when DB is first accessed.

### 14. Dependency pinning
requirements.txt: `google-auth==2.53.0`, `gspread-asyncio==2.0.0`, `tenacity==9.1.4` — previously unpinned. `reportlab>=4.0` kept loose intentionally (no breaking changes expected). Pin all new deps on addition.
