---
name: AI billing rate_store
description: Архитектура rate_store.py для AI-биллинга — таблицы, функции, приоритеты лимитов
---

## Таблицы в rate_limits.db (web/rate_store.py)

- `ai_usage_log(tg_id, usage_date, count)` — персональные запросы AI по пользователю; прунинг 03:30 UTC (90 дней)
- `ai_cost_log(date, provider, prompt_tokens, completion_tokens, cost_usd)` — агрегированная стоимость по дням+провайдерам; прунинг 03:35 UTC (90 дней)
- `ai_org_usage_log(org_key, usage_date, count)` — per-org счётчик для chat AI (org_key = basename org_db); прунинг вместе с ai_usage_log 03:30 UTC

## Кастомные лимиты в shop_bot.db ai_rate_config

- Ключи вида `custom_limit_{tg_id}` — индивидуальный дневной лимит для конкретного пользователя
- Приоритет в `_get_limits(tg_id)`: **custom > ai_high_limit extension > base_daily_limit**
- CRUD: `get_custom_ai_limit(tg_id)`, `set_custom_ai_limit(tg_id, limit)`, `clear_custom_ai_limit(tg_id)`, `get_all_custom_limits()`
- Admin UI: `/admin/ai-limits` → POST `/admin/ai-limits/set-custom` и `/admin/ai-limits/clear-custom`

## Chat AI quota — per-org (НЕ per-owner)

- Chat AI (темы + DM) использует `check_and_increment_ai_for_org(org_db, limit)`, а НЕ `check_and_increment_ai(owner_tg_id, limit)`
- Цель: каждая орг имеет свой дневной пул, не делит его с другими орг того же владельца
- Billing-гейт (`has_extension(owner_tg_id, 'ai_chat_assistant')`) остаётся по владельцу — только квота per-org

**Why:** Один владелец с 3+ орг потреблял весь пул в одной орге, блокируя остальные до следующего дня.

## Кэш лимитов

- `get_ai_rate_limits()` и `get_ai_chat_daily_limit()` кэшированы 60s в `_rl_cache` dict под `_rl_cache_lock`
- Инвалидация: `invalidate_rate_limits_cache()` — вызывать после записи в `ai_rate_config`

## Admin page /admin/ai-limits

- Показывает intraday anomaly banner когда `today_total > anomaly_threshold * 0.8`
- Карточка «Расходы AI — история» с 30-дневным Chart.js bar chart (зелёный)
- Карточка «Индивидуальные лимиты» — список + форма добавления + удаление (✕)

## Dashboard квота-виджет

- `ctx["ai_quota_widget"]` в dashboard.py — заполняется через `_get_limits(tg_id)` + `get_ai_daily_usage(tg_id)`
- Только для tg_id > 0 (не email-only) и когда AI включён и лимит > 0
- Шаблон: прогресс-бар с цветом (синий/жёлтый/красный по % заполнения)
