---
name: Bot module/bundle self-purchase
description: How the Telegram bot lets users buy billing modules/bundles, reusing existing grant infra
---

Бот-самообслуживание покупки модулей/пакетов (G1) НЕ требует своей grant-логики.

**Ключевое:** `database.confirm_payment_request` уже маршрутизирует `plan_type` вида
`module_<key>` / `bundle_<key>` в `grant_billing_item(..., duration_days=30)`.
`check_yookassa_payment` тоже грантит по сохранённому `plan_type`. Значит UI бота
обязан лишь создать `payment_request` (СБП-скриншот) или yookassa-запись с таким plan_type.

**Why:** дублирование grant-логики в боте рассинхронизировалось бы с веб-кабинетом.

**How to apply / подводные камни:**
- `start_module_purchase` рендерит имена модулей из БД — ВСЕ через `he()` (markup-инъекция).
- `he` импортировать на уровне модуля (`from utils import he`), не локально — иначе NameError.
- ЮKassa-ветка: org-пользователь может отсутствовать в shop_bot.db → копировать из
  tenant DB (как в `process_payment_proof`); если user_id всё ещё None — STOP с ошибкой,
  не создавать платёж (иначе check_yookassa_payment упадёт "Запись не найдена").
- callback_data `buymod_<key>`/`buybnd_<key>` — гард на 64 байта (ключи из админки не лимитированы).
