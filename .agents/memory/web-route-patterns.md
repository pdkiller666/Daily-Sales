---
name: Web route patterns
description: Critical patterns for FastAPI web layer — DB access, auth, CSRF, flash, redirect codes, payments DB path
---

# Web Route Patterns

## The rule
Web routes use **sync** `Database()` — not `AsyncDatabase`. Every call is direct, no `await`.

**Why:** `get_web_db()` in `web/deps.py` returns a plain `Database(path)` instance. The web layer runs in a normal sync context (FastAPI sync route functions), not in an aiogram async context. Mixing AsyncDatabase here would break everything.

**How to apply:** In every web route: `db = get_web_db(int(user["sub"]), user.get("org_db"))` then call `db.method()` with no await.

---

## POST → redirect code must be 303, not 302

**Why:** 303 (See Other) tells the browser to switch to GET for the redirect. 302 can cause browsers to re-POST on refresh.

**How to apply:** `return RedirectResponse(url="...", status_code=303)` for all form submissions.

---

## CSRF token flow

1. In GET route: `ctx["csrf_token"] = get_csrf_token(request)` (from `web/auth.py`)
2. In template: `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`
3. In POST handler (sync route — standard): use FastAPI `Form(...)` parameter injection:
```python
def my_post(request: Request, csrf_token: str = Form(default=""), ...):
    from web.auth import get_session_user, verify_csrf_token
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/page?error=csrf", status_code=303)
```

**Why:** Web routes are sync (`def`, not `async def`). FastAPI injects `Form(...)` params automatically. Using `await request.form()` would require async route — avoid that pattern here.

**Why:** Other templates pass `csrf_token` from context — not calling `get_csrf_token(request)` directly in Jinja2.

---

## Flash messages — query params only

No server-side session. All flash state is passed as query params:
- `?saved=1` → «Сохранено»
- `?error=text` → красный баннер
- `?imported=N` → «Импортировано N товаров»
- `?msg=confirmed_N` / `?msg=rejected_N` → платёжные флеши
- `?tab=pending` → сохраняет активную вкладку после redirect

---

## Payments always use shop_bot.db directly

`payment_requests` live in `data/shop_bot.db`, never in org tenants.

**How to apply:** In `web/routes/payments.py`, always use `sqlite3.connect("data/shop_bot.db")` or `Database("data/shop_bot.db")` directly — never `get_web_db()`.

---

## TemplateResponse — request is FIRST argument

```python
return request.app.state.templates.TemplateResponse(request, "folder/name.html", ctx)
```

Not `TemplateResponse("folder/name.html", ctx)` — that's the old Starlette API.

---

## Auth pattern (every route starts with this)

```python
user = get_session_user(request)
if not user:
    return RedirectResponse(url="/login", status_code=302)
telegram_id = int(user["sub"])
org_db = user.get("org_db")
```

`user` dict contains: `sub` (str telegram_id), `name`, `role`, `org_db`.

---

## super_admin in web

```python
if user.get("role") != "super_admin":
    # return 403 or redirect
```

Set during login: `if env_manager.is_super_admin(telegram_id): role = 'super_admin'` in `auth_routes.py`.

---

## Telegram Bot API calls from web routes

`httpx` **не установлен**. Для отправки сообщений через Bot API — использовать stdlib `urllib.request`:

```python
import json, urllib.request

def _send_tg(bot_token: str, chat_id: int, text: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False
```

Токен: `env_manager.get_bot_token()`. Admin ID: `env_manager.get_main_admin_id()`.

---

## Rate limiting — support form (per-user, not per-IP)

`/support/send` использует rate limit **по telegram_id**, а не по IP (в отличие от auth/api лимитов):

```python
_rate_store: dict[int, list[float]] = {}  # {telegram_id: [timestamps]}
# 3 req / 3600s / telegram_id
```

**Why:** Пользователи могут быть за NAT (один IP на всю организацию). Лимит по IP заблокировал бы весь магазин после 3 обращений. Per-user — корректнее.
