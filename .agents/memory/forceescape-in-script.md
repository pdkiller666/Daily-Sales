---
name: forceescape in script body breaks the whole block
description: why Alpine components silently die when a template uses tojson|forceescape inside a <script> tag
---

Using `{{ value | tojson | forceescape }}` inside a `<script>` body renders quotes
as HTML entities (`&#34;...&#34;`) which is a JavaScript SyntaxError. A single
SyntaxError makes the browser skip the ENTIRE `<script>` block — every function
declared there becomes undefined, so any `x-data="fn()"` Alpine component in the
page silently fails (no error visible to the user, native inputs still toggle
visually but no reactivity fires).

**Why:** `forceescape` is meant for HTML **attribute** contexts (e.g.
`x-data="{...}"`, `@click="..."`, `data-*="..."`) where the value sits inside
double quotes and must be entity-escaped. In a `<script>` body there is no HTML
entity decoding, so entities stay literal and break JS. Jinja's `tojson` alone is
already safe for `<script>` (it escapes `<`, `>`, `&` as `\uXXXX`).

**How to apply:** In `<script>` bodies use `{{ value | tojson }}` (no forceescape).
Keep `| tojson | forceescape` only when the interpolation is inside an HTML
attribute. Symptom of the bug: an Alpine feature (checkboxes/bulk-select, star
rating, etc.) renders server-side but does nothing on click, and selected-state
classes (`:class` ring) never apply — check the shared `<script>` block for a
stray `forceescape`.
