---
name: hx-boost incompatible with base.html
description: Why full-body HTMX hx-boost SPA-navigation is unsafe in this web layer
---

# hx-boost / HTMX SPA-navigation is unsafe with base.html as-is

`web/templates/base.html` has ~14 inline `<script>` blocks with ~30 top-level
side effects: `document/window.addEventListener` (touch, keydown, visibility,
beforeinstallprompt, custom ds-* events), multiple `setInterval` pollers
(notifications, chat unread, badge render), `navigator.serviceWorker.register`,
and Alpine `mainApp()` wiring.

Full-body `hx-boost` swaps `<body>` and RE-RUNS those scripts on every navigation
→ double-bound listeners, stacked intervals, repeated SW registration, and broken
charts (page `extra_head` Chart.js not re-loaded without head-support).

**Why:** this can't be validated by screenshots — it needs interactive click-testing
across pages, which is unsafe to ship blind to a live multi-tenant production app.
Also note: most perceived nav slowness was the ~50-70 billing queries per render,
already fixed by `get_modules_access` + request.state memoization — so the speed
win that motivated hx-boost is largely already delivered.

**How to apply (if revisiting):** do NOT use body-level boost. Use a content-target
approach: wrap the page body in `<div id="content">`, `hx-boost` with
`hx-target/hx-select="#content"` so base.html scripts are NOT re-run, and load
Chart.js globally (or via head-support) so chart pages survive content swaps.
Requires manual click-testing of every page + all forms (file uploads must stay
non-boosted).
