---
name: Money-grant paths must be idempotent + tested
description: Why every "оплата → выдача" path in confirm_payment_request needs a regression test and payment-id idempotency
---

Любой путь «оплата подтверждена → что-то выдаётся» (подписка, модуль, надстройка/addon)
ОБЯЗАН быть идемпотентным по `payment_request_id` и покрыт регрессионным тестом.

**Why:** `confirm_payment_request` (и бот, и веб) сначала переводит заявку в approved,
потом вызывает выдачу. Внешние `except` глотают ошибки выдачи — был реальный инцидент:
INSERT надстройки шёл в несуществующую колонку (`amount_paid` вместо `price`), падал,
ошибка подавлялась → пользователь платил и НЕ получал ничего, и это никто не замечал.
Без теста schema/code-рассинхрон не виден; без идемпотентности повтор/ретрай заявки
накручивает оплаченные лимиты.

**How to apply:**
- Новая ветка выдачи в `confirm_payment_request` → передавать `payment_request_id` в
  метод выдачи; метод сначала SELECT по этому id, нашёл — вернуть существующий, не вставлять.
- Защита на уровне БД: partial UNIQUE index на `payment_request_id WHERE ... IS NOT NULL`
  + ловить IntegrityError (гонка) → re-SELECT.
- Если выдача вернула «не удалось» (0/None) — компенсировать: откат заявки approved→pending
  (как для create_subscription), вернуть False. Не оставлять approved без гранта.
- Добавлять функциональный тест в `test_imports.py` (повторный вызов с тем же id не дублирует).
- Сверять имена колонок с ЖИВОЙ схемой в `create_tables()`, а не догадками.
