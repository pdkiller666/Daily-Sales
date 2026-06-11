---
name: nav-module-gating
description: Web-layer module paywall gating — nav hiding is NOT a security boundary; every paid-module route must call has_module server-side.
---

# Module paywall gating (web layer)

- `nav_modules(request)` Jinja2 global (web/app.py) returns `{module_key: bool}` for nav button visibility. Set `_nm` TWICE in base.html: inside `<nav>` AND inside the more-sheet div (separate Jinja2 scope). Fails open on error.
- Nav→module map: analytics→Отчёты/Рейтинги; team→Сотрудники/Зарплата/График/Отсутствия; plans_motivation→Планы/Конкурсы/Мотивация; integrations→Интеграции; notifications→Рассылки; ai_assistant→AI; Задачи always shown.

## RULE: nav-hiding ≠ route-gate
**Hiding a nav button only prevents discovery — it does NOT protect the route.** Direct URLs and cross-module links (e.g. rankings seller row → `/staff/{id}`, dashboard recent-sales → `/staff/{id}`) bypass nav entirely. EVERY route belonging to a paid module/extension MUST enforce server-side:

```python
telegram_id = int(user["sub"])
from billing_utils import has_module   # or has_extension
if not has_module(telegram_id, KEY):
    return RedirectResponse(url="/dashboard?msg=module_<KEY>_required", status_code=302)
```
For fetch/JSON endpoints return `JSONResponse({"error": "module_required"}, status_code=403)` instead.

- Gate goes AFTER auth check / `telegram_id` extraction, BEFORE any data access. Place on GET pages, data exports (.xlsx), AND POST mutations — not just the landing page.
- `has_module(tg_id, key)` checks the LOGGED-IN user's own `billing_module_subs` rows (no org-owner resolution). Every existing gate uses plain `has_module(telegram_id, ...)` — match that; do NOT introduce owner-resolution for one route (creates inconsistency). Owner-resolution only happens in nav for the per-user `allow` override path.
- Dashboard (`web/routes/dashboard.py`) renders upsell text for `msg=module_*_required` keys (analytics/team/plans/notifications/...). Reuse existing keys; add to the map there if introducing a new one.

**Why:** users without a module reached `/rankings` (analytics) and `/staff` (team) by direct URL / cross-module links because those routes only relied on nav hiding — a real paywall bypass. Reports/salary/plans/schedule/absences/motivation/notifications/integration/ai/chat were already gated; rankings + staff were the two that weren't.
**How to apply:** when adding ANY route under a paid module, or wiring a link that points into one, confirm the target route calls has_module/has_extension. Soft-gates (render page with locked/upsell state, no data) are acceptable for pages like /integration and /org-structure (which has a free "minimal" tier).
