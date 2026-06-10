---
name: Dynamic landing pricing
description: Landing page renders the real billing model (tariffs/modules/extensions/bundles) from shop_bot.db; Jinja dict-key pitfalls and accuracy rules.
---

# Лендинг отражает реальный биллинг

Промо-страница (`web/templates/landing.html`) обязана соответствовать реальной модели биллинга. Данные тянутся из `shop_bot.db` помощниками внутри `create_web_app()` в `web/app.py` и передаются в шаблон из `root()`:

- `_landing_billing_modules()` → `billing_modules` (модули с темой/фолбэк-фичами по `key` в шаблоне)
- `_landing_extensions()` → `landing_extensions` (расширения, сгруппированные по родительскому модулю)
- `_landing_bundles()` → `landing_bundles` (пакеты со скидкой: `full_price`/`save`)
- `_landing_pricing_meta()` → `pricing_meta` (low/high/count для JSON-LD `AggregateOffer`)

**3 оси модели:** Тариф (объём: товары/магазины/продажи) → Модули (возможности) → Расширения (точечные функции внутри модуля). Пакеты (bundles) собирают модули со скидкой. Лимит-аддоны (`subscription_addons`) — отдельная статичная карточка «Дополнительные лимиты».

## Правила точности (почему так)
- **Никаких фейковых соц-доказательств в JSON-LD.** Был удалён `aggregateRating` (4.9/120) и захардкоженные `highPrice`/`offerCount`. Все цифры офферов теперь из БД.
- **Bundles считать по ВСЕМ включённым модулям, не только `is_active=1`.** `_landing_bundles()` строит `mod_map` без фильтра по активности — иначе состав/`full_price`/`save` занижаются, если пакет ссылается на временно выключенный модуль. Видимость самого пакета по-прежнему гейтится `billing_bundles.is_active`.
- Помощники fail-open: при любой ошибке возвращают `[]`/фолбэк-meta, чтобы лендинг не падал в 500.

## Jinja2 gotcha (стоило отладки)
`{{ grp.items }}` в Jinja резолвится в **метод словаря `dict.items`**, а не в ключ `"items"` → `TypeError: object ... has no len()` / тихо ломает циклы. Никогда не называйте ключи передаваемых в шаблон dict-ов именами методов dict (`items`, `keys`, `values`, `get`). Решение: переименовали ключ в `exts`. Альтернатива — bracket-доступ `grp['items']`.
