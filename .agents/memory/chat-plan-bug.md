---
name: Chat plan check bug
description: _get_org_active_plan() в chat.py неправильно проверяла план — shop_bot.db раньше main.db, из-за чего приглашённые члены орга видели "Бесплатный"
---

## Симптом
Сотрудник (admin/user), приглашённый через Telegram-ссылку, видит в чате команды "Ваш тариф: Бесплатный" — хотя орг на Премиум и другие сотрудники чат видят нормально.

## Причина
`_get_org_active_plan()` в `web/routes/chat.py` проверяла shop_bot.db **первой**:
```python
# OLD (broken) priority:
# 1. super_admin
# 2. shop_bot.db subscriptions  ← ВОТ ОШИБКА
# 3. main.db org plan
```

Каждый Telegram-пользователь при первом взаимодействии с ботом получает запись `subscriptions(plan_type='Бесплатный', end_date='9999-12-31')` в shop_bot.db (database.py `create_subscription()` при `duration_days=0` пишет вечный срок). Эта строка всегда находилась раньше проверки орга → функция возвращала "Бесплатный" и до main.db не доходила.

## Исправление
Порядок переставлен так же, как в `subscription_utils.get_plan_limits()`:
```python
# NEW (correct) priority:
# 1. super_admin → Премиум
# 2. Trial (is_trial=1 в shop_bot.db)
# 3. Org plan из main.db  ← теперь раньше
# 4. Paid (non-free) individual subscription из shop_bot.db
```

Также добавлен явный фильтр `AND plan_type != 'Бесплатный'` в шаге 4, чтобы вечная строка никогда не пробивалась.

**Why:** subscription_utils.py уже использовал правильный порядок (org → individual). chat.py была написана позже и воспроизвела ту же логику в неверном порядке.

**How to apply:** Если добавляешь новый модуль с проверкой плана пользователя — всегда смотри на `subscription_utils.get_plan_limits()` как эталон порядка проверок.
