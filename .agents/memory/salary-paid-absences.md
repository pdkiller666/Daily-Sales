---
name: Salary double-count paid absences
description: get_worked_days_count и bulk-аналоги должны вычитать approved paid absence дни, иначе они считаются и как смены, и как отпускные одновременно.
---

## Правило

`get_worked_days_count` / `get_salary_bulk_stats` / `get_team_salary_summary` ОБЯЗАНЫ возвращать **чистые смены** — за вычетом дней, покрытых одобренными (status='approved') оплачиваемыми отсутствиями.

## Почему

Формула оклада везде: `(worked + paid_abs) × rate`.  
Если vacation-дни есть одновременно в `work_schedule` И в `absence_records`, они попадают в оба слагаемых → двойной счёт.

Пример (был баг): 20 смен (вкл. 14 отпуска) + 14 оплач. отп. = 34 × 1178 = 40 052 ₽.  
Правильно: (20−14) чист. смен + 14 отп. = 20 × 1178 = 23 560 ₽.

## Какие типы отсутствий вычитаются

Все, где `absence_type_settings.is_paid = 1` **И** `absence_records.is_paid IS NULL OR 1`:
- `vacation` — отпуск
- `sick` — больничный
- `compensatory` — отгул
- `other` — другое

НЕ вычитается:
- `absence` — прогул (is_paid=0 по умолчанию, да и фильтр `AND type != 'absence'` уже есть)
- `pending` статус — только `approved`

## Где исправлено (все три пути)

| Метод | Файл | Что делает |
|---|---|---|
| `get_worked_days_count` | `database.py` | Выбирает work_date как set, вычитает paid_abs_dates |
| `get_salary_bulk_stats` | `database.py` | Аналогично для всех юзеров сразу (bulk) |
| `get_team_salary_summary` | `database.py` | То же, возвращает net worked_days в кортеже |

В `salary_handlers.py` — 4 места заменены: `len(get_work_schedule())` → `get_worked_days_count()` (уже исправленный метод).

## How to apply

При любом новом методе расчёта смен — всегда строить `paid_abs_dates` и делать `worked_dates - paid_abs_dates` перед подсчётом.
