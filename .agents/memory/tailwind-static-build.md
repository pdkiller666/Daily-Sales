---
name: Tailwind static build (CDN → app.css)
description: How the compiled Tailwind setup works and the traps when changing it
---

# Tailwind static CSS (replaces cdn.tailwindcss.com runtime)

The web layer compiles `web/static/tailwind.input.css` → `web/static/app.css` via
`tailwind.config.js` (Tailwind v3 CLI). `deploy.sh` step 0b rebuilds it and stamps
a `?v=<md5-prefix>` cache-bust into the `<link>`.

**Trap 1 — standalone templates carry their OWN head.** `base.html` is NOT the only
place Tailwind is loaded. These standalone templates have their own `<head>` and
previously each embedded `<script src="cdn.tailwindcss.com">`: `landing.html`,
`auth/login.html`, `auth/register.html`, `auth/verify_sent.html`,
`auth/reset_request.html`, `auth/reset_confirm.html`, `errors/404.html`,
`errors/500.html`. Any CDN/CSS change must touch ALL of them, not just `base.html`.
(`products/label.html` is standalone but uses only inline `<style>` — no Tailwind.)

**Trap 2 — landing has a custom theme.** `landing.html` shipped a large inline
`tailwind.config` (Inter font + ~13 custom animations/keyframes: float, orb1-3,
shimmer, slide-up, etc.). These MUST live in `tailwind.config.js` `theme.extend`
or the landing animations silently die. A single shared `app.css` is still safe:
off-landing pages don't load Inter, so `font-sans` falls back to system-ui exactly
as before — no visual change.

**Trap 3 — cache-bust drift.** `deploy.sh` must rewrite `?v=` in EVERY template that
references `app.css`, not just `base.html` (it uses `grep -rl ... | xargs sed`).
Static assets are served `immutable`; if only base.html got the new hash, the
standalone pages would stay pinned to stale CSS forever.

**Why:** purge is safe because no class names are dynamically concatenated — every
class is a literal in templates/inline-JS, which the content globs scan.
**How to apply:** after editing any UI classes, the deploy rebuild regenerates
app.css automatically; if adding a new standalone `<head>` template, link app.css
and ensure it's covered by the cache-bust grep.
