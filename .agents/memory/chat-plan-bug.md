---
name: Chat plan check bug
description: _get_org_active_plan() в chat.py — два бага: неверный порядок проверок + NULL subscription_end + отсутствующая миграция
---

## Симптом
Сотрудник (admin/user) организации с оплаченным Premium тарифом видит в чате "Ваш тариф: Бесплатный" и не может войти.

## Баг 1 (исправлен ранее): неверный порядок проверок
`_get_org_active_plan()` проверяла shop_bot.db **первой**. Каждый пользователь при входе в бот получает запись `subscriptions(plan_type='Бесплатный', end_date='9999-12-31')` → вечный Бесплатный блокировал проверку орга.

Исправление: порядок переставлен (super_admin → trial → org plan из main.db → individual paid).

## Баг 2 (исправлен 2026-06-07): NULL subscription_end

### Причина
В `_get_org_active_plan` step 3 было:
```python
if (org_row and org_row[0] and org_row[0] in PLAN_ORDER
        and org_row[1] and org_row[1] >= datetime.now().strftime("%Y-%m-%d")):
    return org_row[0]
```
`and org_row[1]` — проверка требовала subscription_end ненулевым. Если organizations.subscription_end = NULL (возможно при duration_days=0 или при ряде edge cases), вся проверка проваливалась → возвращался "Бесплатный".

subscription_utils.py при этом работал корректно: возвращал plan_name даже при NULL subscription_end.

### Исправление
```python
if org_row and org_row[0] and org_row[0] in PLAN_ORDER:
    end = org_row[1]
    if end is None or end >= datetime.now().strftime("%Y-%m-%d"):
        return org_row[0]
```

## Баг 3 (исправлен 2026-06-07): отсутствующая миграция колонок в organizations

### Причина
`tenant_manager.py._init_main_db()` создавал organizations через `CREATE TABLE IF NOT EXISTS` — но у существующих БД (до добавления subscription_plan/subscription_end в схему) эти колонки не добавлялись. Запрос в step 3 падал с "no such column" → `except Exception: pass` → "Бесплатный". Попытка обновить в payment_admin_handlers тоже падала молча.

### Исправление
Добавлены миграции в tenant_manager.py после PRAGMA table_info(organizations):
```python
if 'subscription_plan' not in org_cols:
    cursor.execute("ALTER TABLE organizations ADD COLUMN subscription_plan TEXT DEFAULT 'Бесплатный'")
if 'subscription_end' not in org_cols:
    cursor.execute("ALTER TABLE organizations ADD COLUMN subscription_end TEXT")
```

**Why:** `CREATE TABLE IF NOT EXISTS` не добавляет новые колонки в существующую таблицу. Все новые колонки для organizations должны добавляться через явные миграции, как это делается для user_org_mapping.

**How to apply:**
- При добавлении нового модуля с проверкой плана → смотри на `subscription_utils.get_plan_limits()` как эталон порядка проверок
- При добавлении новой колонки в organizations → всегда добавляй миграцию `if 'col' not in org_cols` в `_init_main_db()`
- Колонки organizations: id, name, db_path, owner_id, invite_code, **subscription_plan**, **subscription_end**, is_active, created_at, invite_preset_role, invite_preset_shop
