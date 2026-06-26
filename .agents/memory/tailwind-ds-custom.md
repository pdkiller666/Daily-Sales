---
name: Tailwind overwrite of ds-custom.css
description: deploy.sh runs Tailwind which overwrites app.css — any CSS added directly to app.css is lost; custom classes must live in ds-custom.css
---

## Rule

**Never add CSS directly to `web/static/app.css`.** It is overwritten on every deploy by `npx tailwindcss ... -o app.css --minify`.

All custom (non-Tailwind-generated) CSS classes must go into `web/static/ds-custom.css`.

## How it works

`deploy.sh` step 0b:
1. Tailwind rebuilds `app.css` from scratch (overwrites the file)
2. `deploy.sh` then appends `web/static/ds-custom.css` to `app.css`
3. Recomputes md5 hash → updates `?v=` cache-bust in all templates

So `ds-custom.css` is the single source of truth for custom classes. After every deploy the combined `app.css` = Tailwind output + ds-custom.css content.

## What classes live in ds-custom.css

~90 utility classes used across templates, prefixed `ds-` and `ls-`:
- Layout helpers: `ds-product-grid`, `ds-pb-safe-11rem`, `ds-bottom-safe-64/72`, `ds-max-h-82vh-*`, `ds-sh-*`
- Gradients: `ds-grad-blue-indigo`, `ds-grad-blue-indigo-glow`, `ds-grad-blue-indigo-glow-lg`, `ds-logo-grad`, `ds-logo-btn`
- Avatar/DM: `dm-av-*`, `dm-photo-*`
- Dashboard icons: `ds-icon-blue/green/indigo/purple`
- Landing: `ls-anim-*`, `ls-bar-*`, `ls-bg-*`, `ls-grad-*`, etc.
- Misc: `ds-hint-*`, `ds-dbg-*`, `ds-kbd`, `ds-touch-manip`, `[data-pct]`

## How the bug happened (Task #10, 2026-06-26)

Task #10 agent correctly removed `unsafe-inline` from `style-src` CSP and replaced `style="..."` attributes with CSS classes. But it added the new classes directly to `app.css` (+216 lines in the commit). The next `deploy.sh` run rebuilt `app.css` with Tailwind (no knowledge of the added classes) → 13 843 bytes of custom CSS vanished → production site broke (POS grid gone, gradients missing, menus unstyled).

## Diagnosis signs

- `app.css` in repo is 128 866 bytes (Tailwind-only output) instead of ~142 709 bytes
- `grep -c "ds-product-grid" web/static/app.css` returns 0
- Templates reference classes like `ds-product-grid`, `ds-grad-blue-indigo` that don't exist in the served CSS

## Fix applied (2026-06-26)

1. Extracted the 13 843 bytes of appended CSS from the Task #10 git commit into `web/static/ds-custom.css`
2. Added append step to `deploy.sh` (after Tailwind build): `cat ds-custom.css >> app.css`
3. Recomputed md5 hash → updated `?v=` in all templates

## Safe deploy paths

| Method | Safe? | Why |
|---|---|---|
| `bash deploy.sh "..."` | ✅ | Tailwind builds + ds-custom.css appended automatically |
| Direct `git push` (no local Tailwind run) | ✅ | `app.css` committed correctly; Amvera serves the file as-is |
| Manual `npx tailwindcss ... -o app.css` + push | ❌ | Overwrites app.css without ds-custom.css → classes lost |

**Why:** Amvera runs `python main.py`, not deploy.sh. No server-side CSS rebuild.
