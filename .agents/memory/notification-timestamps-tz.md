---
name: Notification timestamps must be TZ-converted
description: Bell + history notification times are stored UTC and must convert via timezone_utils, not raw string slicing
---

Любое отображение времени уведомлений в вебе обязано конвертировать UTC → таймзону пользователя через `timezone_utils` + `db.get_user_timezone(telegram_id)`.

**Why:** `notification_history.created_at` и `push_subscriptions.created_at` пишутся SQLite `CURRENT_TIMESTAMP` / `datetime('now')` = UTC-naive. Был реальный баг: и колокольчик (`/api/my-notifications`), и страница истории (`/notifications`) показывали время простой нарезкой строки без конвертации → сотрудник в UTC+7 видел UTC-время (09:26 вместо 16:26).

**How to apply:** при выводе любого хранимого времени — `format_user_datetime(value, tz, fmt)` или существующий хелпер (`_fmt_scheduled_dt` в notifications.py). Никогда не отдавать сырой `created_at[:16]`. Это частный случай общего правила (replit.md gotcha #11: Amvera = UTC).
