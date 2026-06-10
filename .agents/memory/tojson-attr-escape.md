---
name: tojson in HTML attributes
description: Why `{{ x | tojson | e }}` silently breaks Alpine attributes, and the correct filter to use
---

# `tojson` inside HTML attributes

**Rule:** When embedding `tojson` output inside a **double-quoted** HTML attribute (e.g. Alpine `@click="editMod={{ m | tojson }}"`, `x-data="gallery({{ urls | tojson }})"`), you MUST use `| forceescape`, not `| e`.

**Why:** Jinja2's `tojson` returns a `Markup`-safe object. The `| e` / `escape()` filter is a **no-op on Markup objects** (markupsafe sees `__html__` and returns it unchanged). So the JSON's `"` delimiters are never converted to `&#34;`. The first `"` then prematurely terminates the `attr="..."` value → the Alpine expression is truncated to garbage → the handler throws on click and nothing happens. The page still renders (no 500), so it looks like a dead button, not an error. `forceescape` forces escaping even on Markup → `"`→`&#34;`, browser decodes back to `"` at parse time → valid JS.

**How to apply:**
- Double-quoted attr + JSON → `{{ x | tojson | forceescape }}`.
- Single-quoted outer attr (e.g. `x-data='{ "tab": {{ active_tab | tojson }} }'`) is already safe — `tojson` escapes `'`→`\u0027`, so no conflict.
- Scalars (ints, bools, controlled enums, quote-free dates) injected into double-quoted attrs are fine without escaping.
- For complex objects, passing via `data-*` + `JSON.parse()` is an even more robust alternative.

**Symptom signature:** an Alpine button/modal that "does nothing" while the rest of Alpine on the page works (x-show/x-cloak content visible). That combination = the `@click` expression itself is malformed, usually from unescaped quotes.
