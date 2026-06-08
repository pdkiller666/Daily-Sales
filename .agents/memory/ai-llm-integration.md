---
name: AI LLM integration
description: web/ai_utils.py fallback chain DeepSeek→Gemini→OpenRouter; API routes; smart alerts job; key constraints
---

# AI LLM Integration

## Core module: `web/ai_utils.py`
- `ask_llm(prompt, system, max_tokens)` → `str | None` — пробует провайдеров по очереди
- `is_configured()` → `bool` — хотя бы один ключ задан
- `_reload_keys()` — перечитывает env при каждом вызове (поддержка смены ключей без рестарта)
- Prompt builders: `build_report_explain_prompt`, `build_product_description_prompt`, `build_sales_forecast_prompt`, `build_smart_alert_prompt`

## Провайдеры (порядок fallback)
1. **DeepSeek** — `https://api.deepseek.com/v1/chat/completions`, model `deepseek-chat`, key `DEEPSEEK_API_KEY`
2. **Gemini** — `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent`, key `GEMINI_API_KEY`
3. **OpenRouter** — `https://openrouter.ai/api/v1/chat/completions`, model `deepseek/deepseek-chat-v3-0324:free`, key `OPENROUTER_API_KEY`

## API routes: `web/routes/ai_routes.py`
- `POST /api/ai/explain-report` — объяснение отчёта (принимает summary, growth_pct, top_items)
- `POST /api/ai/product-description` — генерация описания товара (name, category, price)
- `POST /api/ai/sales-forecast` — прогноз продаж (daily_data[], period_days, total_revenue)
- Rate limit: 20 req/hour/user (in-memory)

## APScheduler job: `ai_smart_alerts` (main.py)
- CronTrigger: **07:05** ежедневно
- Логика: вчерашняя выручка vs 7-дневный avg; алерт если падение >35% или zero-day
- LLM-текст если `is_configured()`, иначе plain-text шаблон
- Отправляет owners+admins (max 3) через Telegram Bot API (urllib, threading)

## Ключевые constraints
- **httpx НЕ установлен** — только `aiohttp==3.11.18` для HTTP-запросов к LLM API
- **CSP connect-src НЕ менять** — все вызовы к LLM делаются server-side (FastAPI), браузер не видит внешних URL
- Ключи: `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` — Replit Secrets + Amvera env vars
- Graceful degradation: если ни одного ключа → `is_configured()` = False → кнопки AI показывают ошибку 503

## UI
- **Форма товара** (`web/templates/products/form.html`): кнопка «✨ Сгенерировать» рядом с label «Описание»; x-data Alpine
- **Отчёты** (`web/templates/reports/index.html`): блок «🤖 AI-анализ» с кнопками «💡 Объяснить» + «🔮 Прогноз»; данные берутся из Jinja2-переменных `summary`, `chart_data`, `groups`

**Why:**
Fallback-цепочка позволяет менять/отключать провайдеров без изменения кода. Server-side вызовы исключают CSP-изменения и утечку ключей на клиент.
