---
name: absence_days_map struct
description: get_absence_days_map returns nested dict with outer user_id key; callers must unwrap it
---

## Правило

`Database.get_absence_days_map(year, month, user_id=None)` возвращает:
```python
{
  user_id_int: {
    day_num_int: {"type": "vacation", "status": "approved", "id": 42},
    ...
  },
  ...
}
```

Вызывающий код ОБЯЗАН делать `.get(user_id, {})` для получения дня-карты:
```python
# ПРАВИЛЬНО:
raw = await current_db.get_absence_days_map(year, month, user_id)
absence_map = raw.get(user_id, {})  # {day_num: {type, status, id}}

# НЕПРАВИЛЬНО — получишь весь внешний dict с user_id-ключами:
absence_map = raw  # {user_id: {day_num: {...}}}
```

**Why:** Метод поддерживает `user_id=None` (возвращает всех пользователей) — поэтому внешний ключ всегда user_id, даже при запросе одного пользователя. Call-сайты в `web/routes/schedule.py` и `web/routes/absences.py` исправлены добавлением `.get(user_id, {})` (сессия 344).

**How to apply:** Всегда при вызове `get_absence_days_map` с конкретным user_id сразу добавлять `.get(user_id, {})`. При итерации по всем пользователям (admin-вид): `for uid, day_map in raw.items()`.
