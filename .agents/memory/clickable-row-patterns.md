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

## Verified current state (as of 2026-06-11)
- Already clickable: inventory rows (onclick→/products/{id}), staff rows (onclick→/staff/{id}),
  shops (href→/inventory?shop= and /shops/{name}/stock), plans rows (href→/plans/{id}),
  rankings sellers→/staff/{id} & shops→/inventory?shop=, reports product tops→/products/{id},
  sales journal product name→/products/{id} (since 2026-06-09).
- Genuinely NOT clickable: dashboard "Последние продажи" rows (plain <p>; recent_sales tuple
  has NO product_id — s[0]=sale_id, s[1]=name, s[5]=first_name only, no seller id → enabling
  links needs a query change to add ids), and rankings "Cities" tab rows (no onclick branch).
- Weak affordance (works but no visual cue beyond hover color, invisible on touch): sales
  journal product link; journal shop (s[2]) and seller (s[9]) columns are not linked at all.
