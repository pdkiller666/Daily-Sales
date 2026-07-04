---
name: NULL sale_price defensive arithmetic
description: sale_price/product price can be NULL in legacy rows; any template/route doing qty*price must guard with `or 0`, and Starlette TemplateResponse needs the request-first signature
---

`sales.sale_price` (and similarly `products.price`) can be `NULL` for legacy or edge-case rows (e.g. price removed/edited to unknown, partial-return flows). Any arithmetic on it — Jinja `{{ s[3] * s[4] }}` or Python `quantity * sale[4]` — raises `TypeError` at runtime and does NOT show up in normal manual testing because real data usually has a price. It only surfaces via synthetic NULL-price rows or in rare live edge cases, then causes an opaque 500.

**Why:** A returns-flow 500 traced back to this exact class of bug (`web/routes/returns.py`, `returns_handlers.py`, `web/templates/returns/index.html`, and separately `web/templates/sales/index.html` line ~652) — multiple independent call sites all needed the same `(price or 0)` guard. It's a recurring pattern, not a one-off.

**How to apply:** Whenever touching code that multiplies quantity by a stored price field, wrap the price with `(price or 0)` (Python) or `(x or 0)` (Jinja) defensively, even if the immediate bug report is about something else. Also test with a NULL-price row via TestClient, not just live data.

Related, found in the same investigation: Starlette's `TemplateResponse` requires the `request` object as the first positional arg in current versions — `TemplateResponse("template.html", ctx)` (old 2-arg form) raises at render time, not at startup. Grep for `TemplateResponse("` (starting directly with a string, no `request,`) when debugging mysterious 500s in newer FastAPI/Starlette stacks.
