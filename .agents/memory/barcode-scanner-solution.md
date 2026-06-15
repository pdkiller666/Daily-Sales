---
name: Barcode Scanner Solution
description: Стек сканирования штрихкодов/QR, архитектура двух полей (article vs barcode), логика поиска
---

## Два отдельных поля: article и barcode

**Архитектурное правило (добавлено сессия 714):**
- `article` — внутренний артикул организации (авто-генерируется вида `КАТ-0001`), индекс row[7]
- `barcode` — заводской штрихкод (EAN-13, QR и др.), опциональный, индекс row[8]
- Это **разные поля в таблице `products`** (org_*.db). Не смешивать.
- Сканер при **продаже**: ищет сначала `get_product_by_barcode()`, fallback → `get_product_by_article()`
- Сканер при **создании/редактировании товара**: FSM-состояние `waiting_for_barcode`; кнопки «📸 Сканировать» / «⏭ Пропустить» — записывает в `barcode`, не в `article`
- Поиск по тексту: `query_lower in p[7]` (article) OR `query_lower in (p[8] or "")` (barcode)

## Решение — библиотека для сервера (бот)

**opencv-python-headless** — единственный pip-пакет, нет системных зависимостей, работает на Amvera.

```python
import cv2
# QR коды
result, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
# EAN-13, EAN-8, UPC, Code128, Code39
result, _, _ = cv2.barcode.BarcodeDetector().detectAndDecode(img)
```

Сначала QRCodeDetector, если пусто → BarcodeDetector. `try/except ImportError` при старте — если не встал, кнопка скрывается, бот не падает.

## Веб (browser-side)

- `BarcodeDetector` Web API — JS декодирует QR + EAN прямо на устройстве (без серверной нагрузки)
- Используется в `web/templates/products/form.html` (поле barcode при создании/редактировании товара) и в `sales/index.html` + `pos/index.html` (при продаже)
- Промпт камеры: требует `Permissions-Policy: camera=(self)` в security headers (НЕ `camera=()` — то молча блокирует)

## Серверный API (поиск по barcode/article)

- `get_product_by_barcode(barcode)` — поиск по barcode; `UPPER(barcode)=?`
- `get_product_by_article(article)` — поиск по article; `UPPER(article)=?`
- Оба в `database.py`

## Ключевые факты

- **pyzbar** требует libzbar0 (системный пакет) — ломает Amvera. НЕ использовать.
- **zxingcpp** недоступен на pip для данной платформы. НЕ использовать.
- **opencv-python-headless** — предсобранное колесо, без системных зависимостей.
- Новые FSM-состояния в `states.py`: `waiting_for_barcode`, `waiting_for_edit_barcode`, `waiting_for_edit_barcode_photo`
- Позиционный индекс barcode = **8** (article = 7); при любом `row[:N]` с N < 9 barcode не включается — проверять места с `row[:7]`
