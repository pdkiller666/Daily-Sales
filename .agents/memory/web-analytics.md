---
name: Web analytics features
description: Patterns for analytics improvements — dark mode charts, drill-down, sparklines, ABC, heatmap, plan forecast
---

## Chart.js dark mode (universal pattern)
```js
const isDark    = () => document.documentElement.classList.contains('dark');
const gridColor = () => isDark() ? '#1e293b' : 'rgba(0,0,0,0.04)';
const tickColor = () => isDark() ? '#475569' : '#94a3b8';
// ... create chart with gridColor()/tickColor() ...
const obs = new MutationObserver(() => {
    chart.options.scales.y.grid.color  = gridColor();
    chart.options.scales.y.ticks.color = tickColor();
    chart.options.scales.x.ticks.color = tickColor();
    chart.update('none');
});
obs.observe(document.documentElement, {attributes:true, attributeFilter:['class']});
```
Copy from rankings/index.html or dashboard/index.html as canonical source.

## Drill-down on bar/line charts
Pass `chart_dates` (ISO date list) alongside `chart_labels`/`chart_data` from the route.
In Chart.js options: `onClick: (event, elements) => { idx = elements[0].index; window.location.href = '/reports?period=custom&date_from='+dates[idx]+'&date_to='+dates[idx]; }`

## Sparklines (inline SVG, Jinja2)
```jinja
{% set _mn = chart_data|min %}{% set _mx = chart_data|max %}{% set _r = (_mx - _mn) if _mx != _mn else 1 %}
<svg width="64" height="24" viewBox="0 0 64 24" fill="none">
  <polyline stroke="#10b981" stroke-width="1.8"
    points="{% for v in chart_data %}{{ loop.index0 * 64 // 6 }},{{ 22 - ((v - _mn) / _r * 20)|int }}{% if not loop.last %} {% endif %}{% endfor %}"/>
</svg>
```
No extra Chart.js instance — pure SVG computed in template.

## Period comparison badges
dashboard.py `_growth(cur, prev) → str|None`: returns "+12.3%" or "-5.1%" or None.
`today_vs_yesterday` and `month_vs_prev` added to dashboard context.
Green badge if starts with '+', red otherwise.

## Plan milestone ticks (25/50/75%)
```html
<div class="relative w-full bg-slate-200 rounded-full h-2">
  <div class="{{ bar_color }} h-2 rounded-full ds-bar-fill" style="width: {{ [p.pct,100]|min }}%"></div>
  <div class="absolute top-0 bottom-0 w-px bg-white/70" style="left:25%"></div>
  <div class="absolute top-0 bottom-0 w-px bg-white/70" style="left:50%"></div>
  <div class="absolute top-0 bottom-0 w-px bg-white/70" style="left:75%"></div>
</div>
```

## Plan forecast
`_add_forecast(plans_dash, today)` in dashboard.py: linear extrapolation `actual/elapsed * total / target * 100`.
Weekly: elapsed=weekday+1, total=7. Monthly: elapsed=today.day, total=monthrange()[1].
Each plan dict gets `forecast_pct: int|None`.

## ABC analysis route: /reports/abc
Aggregates sales by product, sorts by revenue, assigns A (≤80%), B (≤95%), C (rest).
DB query: uses existing `get_sales_report()` with optional shop filter.
Template: abc.html — 3 summary cards + doughnut Chart.js + product table with group badge.

## Heatmap route: /reports/heatmap
New DB method `get_sales_heatmap(start_date, end_date, shop_name)` → list of (weekday 0=Mon..6=Sun, hour, revenue, count).
SQLite: `CASE strftime('%w',...) WHEN '0' THEN 6 ELSE CAST(...)-1 END` converts Sun=0 → Mon=0 convention.
Template: CSS grid 7×15 hours (8-22), green opacity cells, Top-5 periods summary.
`sale_date TEXT DEFAULT CURRENT_TIMESTAMP` stores full datetime → strftime('%H') works correctly.

## Drill-down rows in Reports breakdown
row_link computed in Jinja2 per group_by: `category`→product+category=, `shop`→product+shop= (reuses existing shop param), `seller`→product+seller_id= (new param, breadcrumb shown).
Route: `seller_id: int = 0` → filter `all_sales` by `s[5]`. Breadcrumb: `{% if seller_id %}← По продавцам / {{ seller_name }}{% endif %}`

## ABC inline badges (Reports group rows)
`_add_abc_badges(groups)`: cumulative revenue → A≤80%, B≤95%, C=rest. Template: `{% if g.abc %}<span ...>{{ g.abc }}</span>{% endif %}`

## Sparklines (inline SVG in Reports rows, Jinja2 namespace trick)
`_compute_sparklines(groups, all_sales, group_by)`: last 7 unique dates, per-group daily revenue normalized 0..1.
Jinja2 namespace needed for loop variable mutation: `{% set ns = namespace(pts='') %}` then `{% set ns.pts = ns.pts ~ x ~ ',' ~ y ~ ' ' %}`.
SVG: `viewBox="0 0 60 20"` polyline, x = loop.index0*60/(n-1), y = (1-v)*17+1.5.

## Period comparison in Rankings
`growth_pct`/`prev_revenue`/`cur_revenue` added to rankings ctx. Computed after all tab branches, only when df+dt set. Badge shown in card header. Same formula as reports: span = cur_end - cur_start.

## Custom date picker in Reports
Filter card wrapped with Alpine `x-data="{customOpen, df, dt}"`. Button `📅 Период` toggles picker. Same pattern as rankings. Period pill `'custom'` highlighted when active.

## **Why**
Dark mode Chart.js required MutationObserver because CSS class is toggled dynamically after page load.
Sparklines as inline SVG avoid extra Chart.js instances (4 instances already = enough).
ABC/heatmap as separate routes (not embedded) to keep reports/index.html fast and simple.
Seller drill-down needs breadcrumb (seller_id not visible in any UI control); shop drill-down reuses existing shop param (visible in dropdown = user can clear it).

## **How to apply**
When adding any new Chart.js instance: always use isDark()/gridColor()/tickColor() pattern + MutationObserver.
When adding new analytics route: add Disallow to _ROBOTS_TXT in web/app.py.
When extending Reports drill-down: add case to row_link block in reports/index.html + route param + breadcrumb block.
