---
name: Product table positional indices
description: products row column positions — critical for handlers that use row[N] slicing
---

## products table — полный список колонок

| Index | Name | Type | Notes |
|---|---|---|---|
| 0 | id | INTEGER | PK |
| 1 | name | TEXT | |
| 2 | category | TEXT | |
| 3 | price | TEXT | |
| 4 | created_at | TEXT | |
| 5 | photo_file_id | TEXT | Telegram file_id основного фото |
| 6 | description | TEXT | опциональное описание |
| 7 | article | TEXT UNIQUE | внутренний артикул орг., авто-генерируется |
| 8 | barcode | TEXT UNIQUE | штрихкод производителя, EAN-13/QR, опциональный; добавлен 2026-06-15 |
| 9 | old_price | TEXT | зачёркнутая цена для акций, опциональный; добавлен 2026-06-19 |

## Важные правила

- CREATE TABLE содержит только колонки id..created_at (0–4)
- Все дополнительные поля добавлены через `ALTER TABLE` миграцией в `create_tables()`
- Срезы `row[:7]` и `row[:8]` **не включают** old_price[9] — проверять при добавлении новых фич
- Позиционные индексы критичны: handlers используют `row[7]`, `row[8]`, `row[9]` напрямую
- `product_photos` — отдельная таблица в org_*.db для галереи (multi-photo, subdir по org hash)

**Why:** Индексы смещаются при добавлении новых ALTER TABLE-полей; срезы типа `row[:7]` пропускают новые поля без ошибок, что маскирует баг.
