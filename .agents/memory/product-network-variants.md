---
name: Product network variants (per-network codes)
description: Один товар → разные артикул/штрихкод по торговой сети (product_network_variants поверх products)
---

- Таблица `product_network_variants` лежит поверх `products`; `products.article/barcode` остаются дефолтом (fallback). Lookup `get_product_by_barcode/article(code, trade_network=None)`: сначала варианты (по сети, иначе любой), затем fallback на products.* — старое поведение для орг без вариантов сохраняется.

**Инвариант уникальности штрихкода (важно):** `set_product_variant` обязан проверять конфликт И внутри `product_network_variants`, И с `products.barcode` ДРУГОГО товара (`SELECT id FROM products WHERE barcode=? AND id<>?`). Свой собственный дефолтный штрихкод как вариант — разрешён. Без этой проверки lookup (variants-first) может вернуть неверный товар.
**Why:** один и тот же штрихкод в двух «пространствах» (products vs variants) ломает однозначность поиска по коду.

**FSM-flow редактирования вариантов в боте:** после успешного сохранения варианта НЕЛЬЗЯ звать `clear_state_keep_org` — он стирает `edit_product_id`/`variant_networks` из data, и кнопки `evnet_`/`edit_variants` ломаются. Использовать `await state.set_state(None)` — выходит из waiting-состояния, но СОХРАНЯЕТ data.
**How to apply:** любой многошаговый FSM, где после ввода остаются inline-кнопки, зависящие от data — сбрасывать только state, не data.

**Сеть при продаже** берётся из магазина: `get_network_for_shop(shop_name)` (DISTINCT trade_network из users, исключая ''/System).

**Веб:** секция «Коды по торговым сетям» в form.html показывается при ≥2 сетей орг (создание И редактирование). Поля `var_net__<idx>`(hidden)/`var_article__<idx>`/`var_barcode__<idx>` читаются через `await request.form()`. Hardening: `var_net__*` валидируется против `db.get_all_trade_networks()` (защита от подмены hidden-поля). Двойное чтение формы (`Form(...)` + `await request.form()`) в Starlette безопасно (форма кэшируется на Request).
