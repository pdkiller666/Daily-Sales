---
name: Schedule Index (event-driven notif scheduler)
description: Why notification minute-jobs read an in-memory index instead of scanning all org DBs; the dirty-flag ownership rule and per-entry isolation invariant.
---

# Schedule Index — событийный планировщик уведомлений

Минутные джобы (`send_sales_alerts`, `send_payment_alerts`, `send_personalized_notifications`, `send_daily_reports`) больше НЕ открывают каждую org-базу раз в минуту. Вместо этого `main.py` держит in-memory индекс `{notif_type: {(hhmm_local, tz_name): [entry]}}`, где `entry=(db_path,user_id,telegram_id,first_name,shop_name,threshold)`. Джоб зовёт `await _ensure_sched_index()` затем `_get_sched_hits(notif_type, current_utc)` — это O(уникальных (hhmm,tz)) пар, без диска.

**Когда индекс перестраивается:** dirty-флаг ИЛИ ts==0 (cold start) ИЛИ TTL>900с. `_invalidate_sched_index()` (ставит dirty=True) вызывается при смене настроек уведомлений:
- Веб: `web/routes/settings.py` — notif POST + timezone POST
- Бот: `notifications_handlers.py` — toggle / threshold / notification_time
- Бот: `handlers.py` — смена timezone пользователем (profile)
- Бот: `admin_handlers.py` — смена timezone пользователя через admin

Lazy-import `from main import _invalidate_sched_index` чтобы не плодить циклы импорта. Wrap в try/except чтобы сбой импорта не ломал основную логику.

**Что НЕ требует инвалидации:** `shift_remind_minutes` (обрабатывает `shift_start_notifier`, не индекс), `plan_coeff_enabled/cap` (зарплата, не расписание).

## Правило: dirty-флагом владеет ТОЛЬКО `_ensure_sched_index`
`_rebuild_sched_index()` НЕ трогает `_sched_index_dirty`. `_ensure_sched_index` снимает dirty=False **ДО** запуска сборки (не после).
**Why:** если снимать dirty в конце сборки, инвалидация, пришедшая ВО ВРЕМЯ сборки, затрётся → изменение настроек потеряется до TTL (15 мин). Снятие до сборки гарантирует, что параллельная инвалидация снова поставит dirty=True и следующий цикл перестроит.
**How to apply:** любые новые точки сборки/инвалидации индекса должны сохранять это разделение. При падении сборки `_ensure_sched_index` ставит dirty=True обратно, а прежний индекс остаётся в силе (`_rebuild_sched_index` публикует новый индекс атомарной переподвязкой ссылки только в конце успешного прохода; фатальная ошибка `_get_scheduler_db_paths()` бросается наружу до публикации).

## Правило: per-entry изоляция в теле джоба
Тело `for entry in hits:` ОБЯЗАНО быть в собственном `try/except ...: continue`.
**Why:** индекс flatten-ит пользователей всех орг в один список. Без per-entry try один необработанный exception оборвёт рассылку ВСЕМ оставшимся (а не одной орг, как было при старом per-path обходе). Это регрессия с большим blast-radius.
**How to apply:** при правке любого из 4 джобов держать внешний `try/except` (защита всего цикла) И внутренний per-entry guard.

`Database` кэшируется по path внутри одного прогона джоба через `db_by_path` dict (daily_reports оборачивает в `wrap_db`).
