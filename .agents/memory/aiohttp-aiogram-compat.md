---
name: aiohttp/aiogram version compatibility
description: aiogram 3.20.0.post0 requires aiohttp<3.12; aiohttp 3.12.x ломает сборку на Amvera
---

## Правило

`aiogram==3.20.0.post0` объявляет зависимость `aiohttp>=3.9.0,<3.12`.
Указывать в `requirements.txt` `aiohttp==3.12.x` нельзя — Amvera падает на pip-сборке с `ResolutionImpossible` и `exit code 1`, бот не запускается.

**Правильная версия:** `aiohttp==3.11.18` (последняя совместимая в ветке 3.11).

**Why:** Локально (Replit) пакет может быть установлен без strict-resolution (через --no-deps или через upm), поэтому ошибка не видна в dev-среде — проявляется только при чистой сборке на Amvera.

**How to apply:** При обновлении aiogram — сначала проверять верхнюю границу aiohttp в его метаданных (`pip index versions aiogram` или PyPI), и удерживать aiohttp ниже этой границы.
