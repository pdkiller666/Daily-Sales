"""
test_task_filter_persistence.py — Проверка сохранения фильтра статуса задач.

Покрывает требования задачи:
  1. После set_user_task_pref('status_filter', 'all'), вызов _show_tasks_list
     с пустым FSM (bot restart) использует 'all', а не дефолтный 'active'.
  2. Неизвестное значение в БД → fallback 'active' (без краша).
  3. get_user_task_pref возвращает '' когда строки нет.

Вспомогательные проверки:
  4. set + get round-trip для 'done'
  5. upsert: повторный set обновляет значение, строка одна
  6. FSM-значение имеет приоритет над БД

Не требует живого соединения с Telegram.
Тестирует реальную production-функцию _show_tasks_list напрямую.
"""
import asyncio
import os
import sys
import tempfile
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

PASS = "✅"
FAIL = "❌"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


# ─── Инфраструктура ─────────────────────────────────────────────────────────
_tmp_dir = tempfile.mkdtemp()
TG_ID = 999_001


def make_db(name: str):
    path = os.path.join(_tmp_dir, name)
    from database import Database
    db = Database(path)
    db.create_tables()
    return db


def make_async_db(db):
    from db_utils import AsyncDatabase
    return AsyncDatabase(db)


def _setup_user(db) -> int:
    """Создаёт тестового пользователя и возвращает его внутренний user_id."""
    db.add_user(TG_ID, "Test", "User", shop_name="Shop1")
    user = db.get_user(TG_ID)
    return user[0] if user else 0


def _make_callback_target(tg_id: int):
    """Имитирует CallbackQuery с нужными полями (от_user.id, answer, message)."""
    target = MagicMock()
    target.from_user.id = tg_id
    target.answer = AsyncMock(return_value=None)
    target.message.edit_text = AsyncMock(return_value=None)
    return target


def _make_state(fsm_data: dict | None = None):
    """Имитирует aiogram FSMContext с get_data / update_data."""
    state = MagicMock()
    state.get_data = AsyncMock(return_value=dict(fsm_data) if fsm_data else {})
    state.update_data = AsyncMock(return_value=None)
    return state


async def _call_show_tasks_list(db, fsm_data: dict | None = None) -> str | None:
    """
    Вызывает реальную _show_tasks_list с реальной БД и пустым FSM.
    Возвращает tsk_status_filter, сохранённый в state.update_data().
    """
    from tasks_handlers import _show_tasks_list

    target = _make_callback_target(TG_ID)
    state = _make_state(fsm_data)
    async_db = make_async_db(db)

    with (
        patch('tasks_handlers.get_db', AsyncMock(return_value=async_db)),
        patch('tasks_handlers.is_any_admin', return_value=False),
        patch('tasks_handlers.fsm_edit', AsyncMock(return_value=None)),
        patch('billing_utils.has_module', return_value=True),
    ):
        await _show_tasks_list(target, state, page=0, status_filter=None)

    if state.update_data.called:
        kwargs = state.update_data.call_args.kwargs
        return kwargs.get('tsk_status_filter')
    return None


# ════════════════════════════════════════════════════════════════════════════
section("1. get_user_task_pref — нет строки → возвращает default")
# ════════════════════════════════════════════════════════════════════════════
db_basic = make_db("basic.db")

val_no_row = db_basic.get_user_task_pref(user_id=42, key='status_filter')
check("get_user_task_pref: нет строки → '' (default='')",
      val_no_row == '',
      f"got={val_no_row!r}")

val_no_row_custom = db_basic.get_user_task_pref(user_id=42, key='status_filter', default='active')
check("get_user_task_pref: нет строки → явный default='active'",
      val_no_row_custom == 'active',
      f"got={val_no_row_custom!r}")


# ════════════════════════════════════════════════════════════════════════════
section("2. set_user_task_pref + get round-trip")
# ════════════════════════════════════════════════════════════════════════════
db_rt = make_db("roundtrip.db")
uid_rt = _setup_user(db_rt)

ok = db_rt.set_user_task_pref(uid_rt, 'status_filter', 'done')
check("set_user_task_pref('done') → True", ok is True)

val = db_rt.get_user_task_pref(uid_rt, 'status_filter')
check("get_user_task_pref после set('done') → 'done'",
      val == 'done', f"got={val!r}")


# ════════════════════════════════════════════════════════════════════════════
section("3. upsert — повторный set не дублирует строку")
# ════════════════════════════════════════════════════════════════════════════
db_upsert = make_db("upsert.db")
uid_up = _setup_user(db_upsert)

db_upsert.set_user_task_pref(uid_up, 'status_filter', 'done')
db_upsert.set_user_task_pref(uid_up, 'status_filter', 'all')

val_up = db_upsert.get_user_task_pref(uid_up, 'status_filter')
check("upsert: второй set перезаписывает первый",
      val_up == 'all', f"got={val_up!r}")

conn = db_upsert.get_connection()
row_count = conn.execute(
    "SELECT COUNT(*) FROM user_task_prefs WHERE user_id=? AND key='status_filter'",
    (uid_up,)
).fetchone()[0]
conn.close()
check("upsert: ровно 1 строка в таблице", row_count == 1, f"rows={row_count}")


# ════════════════════════════════════════════════════════════════════════════
section("4. _show_tasks_list: пустой FSM + DB='all' → фильтр 'all'")
# ════════════════════════════════════════════════════════════════════════════
db_all = make_db("filter_all.db")
uid_all = _setup_user(db_all)
db_all.set_user_task_pref(uid_all, 'status_filter', 'all')

resolved_all = asyncio.run(_call_show_tasks_list(db_all, fsm_data={}))
check("bot restart + DB='all' → _show_tasks_list сохраняет 'all' в FSM",
      resolved_all == 'all',
      f"got={resolved_all!r}")


# ════════════════════════════════════════════════════════════════════════════
section("5. _show_tasks_list: пустой FSM + DB='done' → фильтр 'done'")
# ════════════════════════════════════════════════════════════════════════════
db_done = make_db("filter_done.db")
uid_done = _setup_user(db_done)
db_done.set_user_task_pref(uid_done, 'status_filter', 'done')

resolved_done = asyncio.run(_call_show_tasks_list(db_done, fsm_data={}))
check("bot restart + DB='done' → _show_tasks_list сохраняет 'done' в FSM",
      resolved_done == 'done',
      f"got={resolved_done!r}")


# ════════════════════════════════════════════════════════════════════════════
section("6. _show_tasks_list: неизвестное значение в БД → fallback 'active'")
# ════════════════════════════════════════════════════════════════════════════
db_garbage = make_db("filter_garbage.db")
uid_garbage = _setup_user(db_garbage)

# Вставляем невалидное значение напрямую (имитируем испорченную/старую запись)
conn_g = db_garbage.get_connection()
conn_g.execute(
    "INSERT INTO user_task_prefs (user_id, key, value) VALUES (?, 'status_filter', 'garbage')",
    (uid_garbage,)
)
conn_g.commit()
conn_g.close()

resolved_garbage = asyncio.run(_call_show_tasks_list(db_garbage, fsm_data={}))
check("неизвестное значение в БД → _show_tasks_list не крашится и выбирает 'active'",
      resolved_garbage == 'active',
      f"got={resolved_garbage!r}")


# ════════════════════════════════════════════════════════════════════════════
section("7. _show_tasks_list: нет строки в БД → fallback 'active'")
# ════════════════════════════════════════════════════════════════════════════
db_norow = make_db("filter_norow.db")
_setup_user(db_norow)  # пользователь есть, но pref не задан

resolved_norow = asyncio.run(_call_show_tasks_list(db_norow, fsm_data={}))
check("нет строки в user_task_prefs → _show_tasks_list выбирает 'active'",
      resolved_norow == 'active',
      f"got={resolved_norow!r}")


# ════════════════════════════════════════════════════════════════════════════
section("8. _show_tasks_list: FSM-значение имеет приоритет над БД")
# ════════════════════════════════════════════════════════════════════════════
db_prio = make_db("filter_priority.db")
uid_prio = _setup_user(db_prio)
db_prio.set_user_task_pref(uid_prio, 'status_filter', 'done')

# FSM уже содержит 'all' (пользователь явно выбрал, FSM не сброшен)
resolved_prio = asyncio.run(
    _call_show_tasks_list(db_prio, fsm_data={'tsk_status_filter': 'all'})
)
check("FSM='all' + DB='done' → _show_tasks_list использует FSM ('all')",
      resolved_prio == 'all',
      f"got={resolved_prio!r}")


# ════════════════════════════════════════════════════════════════════════════
# Итог
# ════════════════════════════════════════════════════════════════════════════
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
total = len(results)

if failed:
    print(f"\n  Провалившиеся тесты ({failed}):")
    for status, label, detail in results:
        if status == FAIL:
            print(f"    {FAIL} {label}" + (f" | {detail}" if detail else ""))

print(f"\n  {'='*40}")
print(f"  Всего тестов:  {total}")
print(f"  ✅ Прошло:     {passed}")
print(f"  ❌ Провалено:  {failed}")
print(f"  {'='*40}")

if failed == 0:
    print(f"\n  🎉 Все {total} тестов прошли успешно!")
else:
    print(f"\n  ⚠️  Есть провалы — требуется проверка.")
print()

shutil.rmtree(_tmp_dir, ignore_errors=True)
sys.exit(0 if failed == 0 else 1)
