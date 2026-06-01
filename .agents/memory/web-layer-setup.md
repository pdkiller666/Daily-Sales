---
name: FastAPI web layer setup
description: Key quirks for the FastAPI+Jinja2 web interface running alongside the Telegram bot
---

## Starlette 1.x TemplateResponse API change
In Starlette 1.x (installed with fastapi 0.136.3), the signature changed:
- **OLD (broken):** `templates.TemplateResponse("name.html", {"request": request, ...})`
- **NEW (correct):** `templates.TemplateResponse(request, "name.html", {"key": "val"})`

Passing the old way causes `TypeError: unhashable type: 'dict'` in Jinja2 LRU cache — because `name` receives the context dict.

**Why:** Starlette 1.x moved `request` to be the first positional arg; `request` is auto-injected into context when using the new form.

## jinja2 not bundled with fastapi
`pip install fastapi` does NOT install jinja2. Must explicitly add `jinja2` to requirements.txt, otherwise:
`jinja2 must be installed to use Jinja2Templates`

## Port configuration
- Dev (Replit): `WEB_PORT=5000` (default) — Replit screenshot/preview tool looks at port 5000
- Prod (Amvera): set `WEB_PORT=5000` in Amvera env OR keep default; `amvera.yml: ingressPort: 5000`
- Port is read via `int(os.getenv('WEB_PORT', '5000'))` in `main.py`

## Architecture
- Web server started via `asyncio.gather(dp.start_polling(bot), web_server.serve())` in `main.py`
- If web server fails to start, falls back to bot-only polling
- Jinja2 globals: `bot_username` is a lambda calling `bot_holder.get_username()` (set after bot.get_me())
- `fmt_date` and `fmt_currency` registered as Jinja2 filters in `web/app.py`
- All TemplateResponse calls strip `request` from context (Starlette auto-injects it)

## Files
- `web/app.py` — FastAPI factory, templates, filters, globals, routers
- `web/auth.py` — Telegram widget HMAC verify, JWT create/decode, COOKIE_NAME
- `web/deps.py` — get_web_db(telegram_id, org_db), get_user_role_from_db()
- `web/routes/auth_routes.py` — /login, /auth/telegram/callback, /logout
- `web/routes/dashboard.py` — /dashboard (sync route, thread pool)
- `web/templates/base.html` — sidebar layout (Tailwind+HTMX+Alpine+Chart.js CDN)
