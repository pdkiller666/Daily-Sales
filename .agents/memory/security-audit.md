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

### 15. Web Push subscribe input validation
`POST /api/push/subscribe` must validate before calling `save_push_subscription`:
1. `endpoint`: non-empty, starts with `https://`, max 2048 chars
2. `p256dh`: non-empty, max 256 chars
3. `auth`: non-empty, max 128 chars

**Why:** Browser-provided values go to DB then are passed to pywebpush. Malformed/overlong values could cause silent errors or DB bloat. `https://` prefix ensures endpoint is a real push service URL.

**How to apply:** Any route that accepts browser push subscription data must apply these three checks before DB write.

### 16. create_subscription_addon is NOT idempotent — money risk
`create_subscription_addon` (database.py) does a blind `INSERT` into `subscription_addons`. A double-click/retry stacks addons (+100 → +200 → +300 items), inflating paid limits for free.

**Why:** No dedup on `(user_telegram_id, addon_type)` and no guard against re-processing the same `payment_id`.

**How to apply:** When wiring addon grants to a payment confirmation, dedup by payment_id (mark request processed) or upsert per addon_type — never rely on a bare INSERT for money-affecting grants.

### 17. Payment confirmation must be single-pathed (web vs bot divergence)
Bot path (`payment_admin_handlers.py`) calls `confirm_payment_request` then does EXTRA manual updates to main.db/shop_bot.db (org plan expiration). Web path (`web/routes/payments.py`) calls only `confirm_payment_request`.

**Why:** If those org-plan updates live outside `confirm_payment_request`, confirming via web leaves org plans un-renewed → inconsistent billing state between the two entry points.

**How to apply:** Consolidate ALL side effects inside `confirm_payment_request` so every caller (web/bot) produces identical state.

### 18. Rate limiter fails open
`web/rate_store.py` lets the request through on exception. For auth endpoints prefer fail-closed (deny on error) so a DB hiccup can't disable brute-force protection.

### 19. ecdsa high CVE is dormant at runtime
`ecdsa` (transitive via `python-jose`) flags a high Minerva timing CVE with no fix, but JWTs use HS256 (symmetric) so ecdsa isn't exercised. To remove the finding entirely, migrate `python-jose` → `PyJWT`.
