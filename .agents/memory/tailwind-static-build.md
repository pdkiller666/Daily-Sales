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

**Trap 4 — the `npx tailwindcss` step can hang/corrupt under a degraded
package-firewall.** Step 0b runs `npx --yes tailwindcss@3.4.17` which downloads the
CLI + deps on a cold cache. When `package-firewall.replit.local` returns
intermittent 502s, each dep fetch retries for 60s+, and partial extractions leave
the npx cache (`~/.npm/_npx/<hash>`) in an `ENOTEMPTY` state (rename of
chokidar/fast-glob fails) → next runs exit fast with code 127/217. Running two npx
builds concurrently makes it worse (they corrupt the shared cache).
**Why:** the rebuild is explicitly non-essential — the `if (...)` skips on failure
and ships the already-committed `app.css`. The only real risk is an *infinite hang*
blocking the whole deploy. So step 0b now wraps npx in `timeout 120`.
**How to apply:** if a deploy stalls at "0b. Сборка Tailwind CSS", it's the
firewall, not your code. It's safe to let it skip when your change adds no NEW
Tailwind utility classes (custom CSS in `<style>` blocks and already-used utilities
need no rebuild). To unstick: `rm -rf ~/.npm/_npx && npm cache clean --force`, never
run two builds at once, and let the `timeout` guard bound the step.
