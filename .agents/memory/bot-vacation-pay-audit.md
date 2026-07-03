---
name: Bot vacation pay audit
description: При изменении формулы расчёта зарплаты в web слое — обязательно синхронизировать бот-хендлеры
---

# Аудит бота при изменении формулы зарплаты

**Правило:** веб-слой и бот вычисляют зарплату НЕЗАВИСИМО. Любое изменение формулы в `web/routes/salary.py` НЕ автоматически применяется в боте.

**Файлы бота, которые считают зарплату:**

1. `salary_handlers.py`
   - `_refresh_admin_calendar()` — карточка со сменами в admin-календаре
   - `salary_summary()` — ФОТ сводка (`asyncio.gather` c paid_abs_results)
   - `_my_schedule_text()` + `my_schedule()` + `my_schedule_nav()` — экран сотрудника
   - `_show_adj_list()` — экран корректировок

2. `dashboard_handlers.py`
   - Admin/scope dashboard (`_salary_tasks` list в gather)
   - My dashboard (`_ures` gather, индексы 0-10)

**Шаблон для каждого места:**
```python
non_vac_paid_abs = await db.get_paid_absence_days_count(uid, year, month, exclude_vacation=True)
vac_pay, vac_cal_days, avg_daily, _ = await db.get_vacation_pay_12m(uid, year, month)
salary = net_worked * rate + non_vac_paid_abs * rate + vac_pay
```

**Отображение отпускных в боте:**
```python
if vac_cal_days > 0:
    text += f"🌴 Отпуск: {vac_cal_days} кал.дн. × {format_price(avg_daily)}₽/дн. = {format_price(vac_pay)}₽"
```

**Why:** gather-цепочки в дашбордах и salary_summary строятся вручную по индексам — любой новый параметр нужно добавлять явно и обновлять индексы.
