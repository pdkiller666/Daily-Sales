---
name: Clickable row/navigation patterns in web templates
description: Web cabinet rows navigate via BOTH <a href> AND onclick="window.location" — href-only audits undercount clickable elements
---

# Clickable navigation in web/templates

The web cabinet makes list/table rows navigate in **two different ways**, and any audit
of "what is/isn't clickable" must check for both:

1. `<a href="...">` wrapping content (e.g. sales journal product name → `/products/{id}`,
   shop/category filter chips, plans rows, rankings period/tab pills).
2. `onclick="window.location='...'"` on `<tr>` or individual `<td>` cells
   (e.g. inventory rows, staff rows, rankings sellers/shops rows).

**Why:** A naive `rg "href="` grep misses every `onclick`-based row and reports it as
"not clickable" — this caused a wrong audit (claimed inventory/staff/reports rows were
unlinked when they already navigate via onclick or href).

**How to apply:** When auditing clickability, grep for `onclick`, `window.location`,
AND `href=`. Read the actual row loop markup — do not trust an href-only search or a
subagent that only looked for anchors.

## Stat-card filter-toggle pattern (Остатки/Планы)
Stat cards ("Нет в наличии"/"Мало", "Выполнено"/"В процессе") double as filter toggles
via `?status=` query params, NOT just decoration.

**Why:** users expect to click a count and see that subset.

**How to apply (two non-obvious invariants):**
1. **Counts BEFORE filter** — compute the card numbers on the full (shop/q/category) set,
   THEN apply the status filter to the displayed list only. Otherwise a toggled card shows
   its own filtered count instead of the total, and the other cards read 0.
2. **Summary + reset OUTSIDE the empty-list gate** — gate the strip on
   `{% if data or selected_status %}` (a separate block from `{% if grouped/list %}`), and
   make the empty-state filter-aware with its own reset link. Otherwise selecting a filter
   that yields zero rows hides the cards/reset and strands the user (must hand-edit the URL).

## Filter state must survive cross-navigation
When a list view carries a filter (e.g. rankings `city` on the shops tab), every other
nav control on the page (period chips, tab pills, custom-range apply/reset) must re-append
that param or it silently drops on the next click. Pattern: set one Jinja var
(`{% set _city_q = "&city="~(selected_city|urlencode) if selected_city else "" %}`) and
append it to each link. Always `|urlencode` user labels (Cyrillic shop/city names → 400 raw).
