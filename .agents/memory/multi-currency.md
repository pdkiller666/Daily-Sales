---
name: Multi-currency implementation
description: Как реализована мультивалютность — middleware, fmt_currency, настройка, миграция
---

## Правило

`fmt_currency` Jinja2-фильтр — context-aware через `@pass_context`; читает `request.state.currency_symbol` (устанавливается `CurrencyMiddleware` из `web/app.py`).

**Why:** `₽` хардкодить нельзя — клиенты из KZ/BY/UZ платят другой валютой. Смена одного поля в настройках меняет символ везде без касания 126 шаблонов.

## Архитектура

- `currency_utils.py` — dict `CURRENCIES` (12 валют), `get_currency_symbol(code)`, `format_amount(amount, code)`
- `tenant_manager.py` — ALTER TABLE organizations ADD COLUMN `currency TEXT DEFAULT 'RUB'`
- `database.py` — `get_org_currency()` / `set_org_currency()` — читают/пишут main.db по `db_path == self.db_file`
- `web/app.py` — `CurrencyMiddleware` (парсит JWT payload без проверки подписи → LRU-кэш `_CURRENCY_SYMBOL_CACHE{org_db: symbol}`); `@pass_context` на `_fmt_currency`
- `web/routes/settings.py` — GET заполняет `org_currency` + `currencies` dict для owner; POST `/settings/currency` сохраняет + инвалидирует кэш
- `web/templates/settings/index.html` — карточка 💱 валюты рядом с часовым поясом

## Как применять

- Новые Python-форматтеры денег → `format_amount(amount, db.get_org_currency())`
- В Jinja2 шаблонах → `{{ amount|fmt_currency }}` (автоматически org-aware)
- Инвалидация кэша при смене валюты → `_CURRENCY_SYMBOL_CACHE.pop(org_db, None)`

## Gotchas

- `from web.app import _CURRENCY_SYMBOL_CACHE` внутри функции (lazy) — избегает circular import
- JWT payload декодируется без verify (только для кэша) — это нормально (auth происходит в роутах)
- `dashboard.py::_fmt(amount, symbol='₽')` принимает символ явно, берётся из `request.state.currency_symbol`
