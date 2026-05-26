---
name: UX hub pattern
description: Паттерн консолидации кнопок в хаб-меню — как правильно внедрять и обновлять back-кнопки
---

## Правило
При объединении N кнопок в один хаб (пример: `admin_motivation` + `admin_sales_plans` + `contests_menu` → `motivation_hub`):
1. В родительском меню (keyboards.py) заменить N кнопок на 1 хаб-кнопку
2. Добавить функцию `motivation_hub_menu()` в keyboards.py
3. Добавить хэндлер `F.data == "motivation_hub"` в соответствующий router
4. В КАЖДОМ дочернем разделе: back-кнопка меняется с родительского (`admin_management`) на хаб (`motivation_hub`)

**Why:** Пользователь входит через хаб → должен возвращаться в хаб, а не прыгать уровень выше.

**How to apply:**
- Хэндлеры хабов admin_management → `admin_handlers.py` (admin_router)
- Хэндлеры хабов main_menu → `reports_handlers.py` или `handlers.py` (соответствующий router)
- back_target в `_get_admin_users_params` строки 319/324/362 → `team_hub`; строка 314 → `system_admin_panel` (не трогать!)
- Top-level back в дочерних разделах меняется; deep sub-pages (ratings sub-view, subscription upsell) остаются на `main_menu` — это нормально

## Реализованные хабы (v2/v3)
- `catalog_menu` (admin_management → products + inventory)
- `motivation_hub` (admin_management → admin_motivation + admin_sales_plans + contests_menu)
- `team_hub` (admin_management → admin_users + admin_salary_menu)
- `analytics_hub` (main_menu → reports + rankings_menu/user_rankings_menu)
