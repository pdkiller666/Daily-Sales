---
name: Barcode Scanner Solution
description: Оптимальный стек для сканирования штрихкодов/QR — без системных зависимостей, безопасен для Amvera
---

## Решение

**opencv-python-headless** — единственный pip-пакет, нет системных зависимостей, работает на Amvera.

```python
import cv2
# QR коды
result, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
# EAN-13, EAN-8, UPC, Code128, Code39
result, _, _ = cv2.barcode.BarcodeDetector().detectAndDecode(img)
```

Протестировано на Replit (Python 3.11, Linux): оба детектора отвечают "ok".

## Архитектура

**Веб (Фаза 1, нулевой риск):**
- `BarcodeDetector` API в браузере — JS декодирует QR + EAN прямо на устройстве
- Успех → `GET /api/products/by-article?q=<code>` → заполняет поле товара
- Fallback: поле ввода вручную (если BarcodeDetector недоступен)
- Никаких серверных изменений

**Бот (Фаза 2, минимальный риск):**
- `opencv-python-headless` в `requirements.txt`
- `try/except ImportError` при старте — если не встал, кнопка скрывается, бот не падает
- `cv2.imdecode(np.frombuffer(photo_bytes, np.uint8), cv2.IMREAD_COLOR)`
- Сначала QRCodeDetector, если пусто → BarcodeDetector
- `db.get_product_by_article(code)` → уже есть в database.py (строка 3168)

## Ключевые факты

- `get_product_by_article(article)` — уже есть в `database.py`
- `GET /api/products/by-article?q=...` — нужно проверить/создать в `web/routes/products.py`
- Новое FSM-состояние: `waiting_barcode_photo` в `states.py`
- Новый хэндлер в `sales_handlers.py` (photo message, FSM)
- Добавить `opencv-python-headless` и `numpy` в `requirements.txt`
- `amvera.yml` — НЕ трогать

**Why:**
pyzbar требует libzbar0 (системный пакет) — ломает Amvera.
zxingcpp недоступен на pip для данной платформы.
opencv-python-headless — предсобранное колесо, встаёт без системных зависимостей.
