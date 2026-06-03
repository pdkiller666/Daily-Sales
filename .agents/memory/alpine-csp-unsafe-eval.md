---
name: Alpine.js 3 requires unsafe-eval in CSP
description: Alpine.js 3 uses new Function() for x-* expression evaluation — CSP must include 'unsafe-eval' or Alpine silently dies
---

Alpine.js 3 evaluates ALL `x-data`, `x-show`, `@click`, `x-for` etc. via `new Function()` internally.

**Why:** If CSP `script-src` does not include `'unsafe-eval'`, the browser blocks every `new Function()` call. Alpine partially initializes (removes `x-cloak` from all elements), then throws one "Script error. @ ?:0" per directive — silently, because the error originates from Alpine's CDN code. Result: `x-cloak` is gone but `x-show` is never applied → hidden sheets become visible; `@click` handlers never registered → buttons dead.

**How to apply:** `_CSP` in `web/app.py` must contain `'unsafe-eval'` in `script-src`:
```
"script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com ..."
```

**Diagnosis pattern:** 30 simultaneous "Script error. @ ?:0" in `window.onerror` log = Alpine eval blocked by CSP. Zero `$watch` callbacks firing = Alpine dead before init completes.

**Alternative (not used):** Alpine's CSP-compatible build (`@alpinejs/csp`) avoids `new Function()` but forbids dynamic JS in templates — incompatible with our expression-heavy codebase.
