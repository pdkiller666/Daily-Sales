---
name: Web Push VAPID
description: web/push_utils.py API (single/bulk/async), where pushes are wired, and the sync-def vs async-route blocking rule
---

# Web Push (VAPID)

- pywebpush==2.3.0; env: `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_MAILTO`.
- subscriptions live in `push_subscriptions` (shop_bot.db): `user_id` = telegram_id, endpoint, p256dh, auth.
- `web/push_utils.py` API:
  - `send_web_push(tg_id, title, body, url, badge)` — one user (all their devices), sync.
  - `send_web_push_bulk(tg_ids, ...)` — many users, dedups ids, loads config+pywebpush once, isolates per-user failures; returns devices reached.
  - `apush(...)` / `apush_bulk(...)` — async wrappers (run the sync send via `asyncio.to_thread`).
  - `_push_to_user()` shared by single+bulk; fresh `_vapid_claims()` per send; auto-deletes 404/410 (Gone) subscriptions.

## Blocking rule (event loop)
**Inside an `async def` route/handler, NEVER call `send_web_push(...)` directly** — it does blocking sqlite + network and stalls the loop. Use `await apush(...)` / `await apush_bulk(...)`, or `await asyncio.to_thread(send_web_push, ...)`.
**Why:** sync send on the loop blocks all concurrent requests for the duration of the push HTTP calls.
**How to apply:** sync `def` FastAPI routes run in the threadpool, so a plain `send_web_push(...)` there is fine (e.g. tasks.py task_change_status/add_comment/edit_post). Only `async def` paths must use the async wrappers.

## Coverage (push mirrors Telegram for team/comms)
- DM: web/routes/chat.py (dm_send + ws_dm) → apush.
- Общий чат (topics): chat_send pushes all org members except sender (`db.get_all_users()`, url `/chat`). Note: org topic chat had NO bot-side notification at all — push is the only notifier there.
- Tasks: create (tasks_new_post, async → apush), status/comment/edit (sync defs → send_web_push), deadlines (main.py check_task_deadlines → to_thread).
- Broadcast/рассылка: bot immediate (notifications_handlers.py → bulk via to_thread); web broadcast goes through scheduler → main.py check_scheduled_notifications already pushes.
- Absences: absence_handlers.py _notify_user (user) + admin-notify loop (bulk).
- Contests/sales/daily-report/low-stock/subscription/POS sale: already wired in main.py + pos.py.
- VAPID_MAILTO: falls back to mailto:admin@dailysales.app with a warning if unset; auto-prefixes `mailto:`.
- robots.txt already disallows /api/.

## Key generation from the super-admin UI
- `/admin/push-diagnostics` имеет генератор: POST `/admin/push-diagnostics/generate-vapid` (super_admin guard + CSRF) → `generate_vapid_keypair()` в push_utils.py (cryptography EC P-256 → PKCS8 PEM private + raw url-safe base64 public). Ключи НЕ сохраняются и НЕ логируются, отдаются один раз в JSON, копируются на странице.
- **Приложение НЕ может писать env-переменные Amvera изнутри контейнера** — модель только «сгенерировал → скопировал → вставил в Amvera вручную → перезапуск».
- **Ротация VAPID обнуляет все `push_subscriptions`** (старый applicationServerKey больше не валиден) — юзеры переподписываются. Безопасно только при первичной настройке.

## ROOT CAUSE: "Could not deserialize key data ... ASN.1 parsing error: invalid length"
**Symptom:** push fails for ALL subscriptions with that ValueError, persists even after generating brand-new keys.
**Cause:** pywebpush 2.x passes the key string to py_vapid `Vapid.from_string()` (NOT `from_pem`). `from_string` only `.replace("\n","")` then `b64urldecode(WHOLE string)` — it does NOT strip the `-----BEGIN/END-----` armor, so any **PEM** input gets mangled → truncated DER → "invalid length". `from_pem` would strip the armor, but pywebpush never calls it for a string arg (only for file paths / Vapid instances).
**Fix:** `_normalize_vapid_key()` must output **single-line url-safe base64 PKCS8 DER** (~184 chars, no newlines, no armor), NOT PEM. Convert every input (PEM/collapsed-PEM/DER-b64/raw-32B-scalar/explicit-params-via-openssl) into a `cryptography` key object, then `private_bytes(DER, PKCS8) → urlsafe_b64encode → rstrip("=")`.
**Why this matters:** earlier "fixes" that normalized TO PEM made it worse. The whole point is the format py_vapid.from_string round-trips. Verify any change with `Vapid02.from_string(private_key=normalized)`.
**How to apply:** never return a PEM from `_normalize_vapid_key`; admin generator can still SHOW PEM, but the env value that works is the single-line url-safe DER.

## Gotcha: VAPID_PRIVATE_KEY shows "Не настроен" despite being set
**Symptom:** диагностика/`_is_configured()` = False хотя ключ задан в env. **Cause:** многострочный PEM, вставленный в env-UI хостинга (Amvera), приходит с ведущим `\n`/пробелом/кавычками → `startswith("-----BEGIN")` ломается → Web Push молча выключен (отправка тоже гейтится `_is_configured`). **Fix:** `_VAPID_PRIVATE` чистится `.strip().strip('"').strip("'").strip()`; `_is_configured()` лоялен — `"BEGIN" in ... and "KEY" in ...` (PEM) ИЛИ компактная base64 ≥20 без пробелов (raw url-safe ключ). Не возвращать к строгому `startswith`.
