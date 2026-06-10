---
name: Super-admin page header macro
description: Unified header banner convention for all /admin/* sub-pages, plus the Jinja import-in-block gotcha
---

# Super-admin sub-page header

All super-admin sub-pages (`/admin/*`, templates under `web/templates/admin/`) share one header
component: the `page_header(icon, title, subtitle='', back_url='/admin', back_label='Супер-Кабинет')`
macro in `web/templates/admin/_macros.html` (a dark gradient hero banner matching the hub pages).

**Rule:** any NEW super-admin page must use this macro for its header — do NOT hand-roll a plain
`<h1>` block, or the page will visually diverge from the rest of the cabinet.

- For a right-side action button (e.g. "Создать бэкап"), invoke via `{% call page_header(...) %}<button class="bg-white/10 hover:bg-white/20 text-white ...">…</button>{% endcall %}`. The macro renders `caller()` only when `{% if caller %}` is truthy.
- Billing sub-pages pass `back_url='/admin/billing', back_label='Биллинг'`.

**Why:** before this, only the two hub pages had hero banners; the 10 sub-pages used inconsistent
plain `<h1>` headers with mismatched/absent back-links. Centralizing avoids the drift recurring.

## Jinja gotcha: import macros INSIDE the block, not at top level

In an `{% extends "base.html" %}` child template, put
`{% from "admin/_macros.html" import page_header %}` at the **start of `{% block content %}`**, never
at the top level of the file. Statements outside blocks in an extending child are not reliably
executed, so a top-level import leaves the macro undefined when the block renders.

**How to apply:** first two lines of every admin sub-page's content block are the `{% from ... import %}`
then the `{{ page_header(...) }}` / `{% call %}` invocation.
