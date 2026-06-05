"""
Модуль задач в Telegram-боте.
Сотрудник видит назначенные ему задачи и может менять их статус.
Admin видит все задачи организации.
"""
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

from keyboards import InlineKeyboardBuilder, back_button, home_button
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit

tasks_router = Router()
logger = logging.getLogger(__name__)

STATUS_LABELS = {
    'new':         '🆕 Новая',
    'in_progress': '🔄 В работе',
    'review':      '🔍 На проверке',
    'done':        '✅ Выполнена',
    'cancelled':   '🚫 Отменена',
}
PRIORITY_LABELS = {
    'low':    '🟢 Низкий',
    'normal': '🔵 Обычный',
    'high':   '🟡 Высокий',
    'urgent': '🔴 Срочно',
}

_STATUS_NEXT = {
    'new':         'in_progress',
    'in_progress': 'review',
    'review':      'done',
}


def _fmt_task_line(t: dict) -> str:
    status = STATUS_LABELS.get(t.get('status', ''), t.get('status', ''))
    title = t.get('title', '—')[:40]
    deadline = t.get('deadline', '')
    dl = ""
    if deadline:
        try:
            from datetime import date
            d = date.fromisoformat(deadline[:10])
            dl = f" · {d.strftime('%d.%m')}"
        except Exception:
            pass
    return f"{status} {title}{dl}"


def _tasks_keyboard(tasks: list, is_admin: bool, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    page_size = 5
    start = page * page_size
    end = min(start + page_size, len(tasks))
    for t in tasks[start:end]:
        task_id = t.get('id', 0)
        label = _fmt_task_line(t)
        kb.row(InlineKeyboardButton(
            text=label,
            callback_data=f"tsk_view_{task_id}"
        ))
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"tsk_page_{page-1}"))
    if end < len(tasks):
        nav_row.append(InlineKeyboardButton(text="Далее ▶", callback_data=f"tsk_page_{page+1}"))
    if nav_row:
        kb.row(*nav_row)
    try:
        from bot_holder import get_username as _get_uname
        _un = _get_uname() or ""
        _web_url = f"https://t.me/{_un}" if _un else None
    except Exception:
        _web_url = None
    if is_admin and _web_url:
        kb.row(InlineKeyboardButton(text="🌐 Открыть в веб", url=_web_url))
    kb.row(home_button())
    return kb.as_markup()


def _task_detail_keyboard(task: dict, my_db_id: int, is_admin: bool,
                          my_shop: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    status = task.get('status', 'new')
    assigned_to = task.get('assigned_to')
    assign_all = task.get('assign_all', False)
    assigned_shop = task.get('assigned_shop', '')

    can_act = (
        is_admin
        or assigned_to == my_db_id
        or assign_all
        or (assigned_shop and my_shop and assigned_shop == my_shop)
    )

    if status in _STATUS_NEXT and can_act:
        next_status = _STATUS_NEXT[status]
        next_label = STATUS_LABELS.get(next_status, next_status)
        kb.row(InlineKeyboardButton(
            text=f"➡️ {next_label}",
            callback_data=f"tsk_setstatus_{task['id']}_{next_status}"
        ))

    if is_admin and status not in ('done', 'cancelled'):
        kb.row(InlineKeyboardButton(
            text="🚫 Отменить задачу",
            callback_data=f"tsk_setstatus_{task['id']}_cancelled"
        ))

    kb.row(InlineKeyboardButton(
        text="◀ К списку",
        callback_data="tsk_list_0"
    ))
    kb.row(home_button())
    return kb.as_markup()


async def _show_tasks_list(
    target, state: FSMContext, page: int = 0
):
    """Показать список задач. target — Message или CallbackQuery."""
    from aiogram.types import Message as Msg, CallbackQuery as CQ
    data = await state.get_data()
    db = await get_db(state)
    if db is None:
        text = "⚠️ Нет активной организации."
        if isinstance(target, Msg):
            await target.answer(text)
        else:
            await target.answer()
            await target.message.edit_text(text)
        return

    try:
        tg_id = target.from_user.id if isinstance(target, Msg) else target.from_user.id
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (tg_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = my_row[1] if my_row and len(my_row) > 1 else None
        admin = await is_any_admin(state)

        tasks = db.get_tasks(is_admin=admin, my_user_id=my_db_id, my_shop=my_shop)
        active = [t for t in tasks if t.get('status') not in ('done', 'cancelled')]

        if not active:
            text = "📋 <b>Задачи</b>\n\nАктивных задач нет."
        else:
            text = f"📋 <b>Мои задачи</b>\n<i>Всего активных: {len(active)}</i>"

        kb = _tasks_keyboard(active, admin, page)
        await state.update_data(tsk_list=active, tsk_page=page, tsk_my_db_id=my_db_id)

        if isinstance(target, Msg):
            await fsm_edit(target, text, kb)
        else:
            await target.answer()
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("_show_tasks_list: %s", e)
        error_text = "⚠️ Ошибка загрузки задач."
        if isinstance(target, Msg):
            await target.answer(error_text)
        else:
            await target.answer()
            await target.message.edit_text(error_text)


# ── Entrypoint: callback из главного меню ────────────────────────────────────

@tasks_router.callback_query(F.data == "tasks_menu")
async def tasks_menu_cb(callback: CallbackQuery, state: FSMContext):
    await _show_tasks_list(callback, state, page=0)


# ── Пагинация ────────────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_page_"))
async def tasks_page_cb(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[-1])
    await _show_tasks_list(callback, state, page=page)


# ── Просмотр задачи ──────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_view_"))
async def task_view_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    db = await get_db(state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        tg_id = callback.from_user.id
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (tg_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = my_row[1] if my_row and len(my_row) > 1 else None
        admin = await is_any_admin(state)

        task = db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return

        title = task.get('title', '—')
        desc = task.get('description', '')
        status = STATUS_LABELS.get(task.get('status', ''), task.get('status', ''))
        priority = PRIORITY_LABELS.get(task.get('priority', ''), task.get('priority', ''))
        deadline = task.get('deadline', '')
        dl_str = ""
        if deadline:
            try:
                from datetime import date
                d = date.fromisoformat(deadline[:10])
                today = date.today()
                overdue = " ⚠️ Просрочена" if d < today and task.get('status') not in ('done', 'cancelled') else ""
                dl_str = f"\n📅 Срок: {d.strftime('%d.%m.%Y')}{overdue}"
            except Exception:
                dl_str = f"\n📅 Срок: {deadline}"

        assigned_name = task.get('assigned_name', '')
        creator_name = task.get('creator_name', '')
        topic_name = task.get('topic_name', '')
        assigned_shop = task.get('assigned_shop', '')
        assign_all = task.get('assign_all', False)

        checklist = task.get('checklist', [])
        cl_str = ""
        if checklist:
            lines = []
            for item in checklist:
                mark = "✅" if item.get('is_done') else "☐"
                lines.append(f"  {mark} {item.get('text', '')}")
            cl_str = "\n\nЧеклист:\n" + "\n".join(lines)

        text = (
            f"📋 <b>{title}</b>\n"
            f"{status} · {priority}\n"
        )
        if topic_name:
            text += f"🏷 {topic_name}\n"
        if assign_all:
            text += "👥 Исполнитель: Вся команда\n"
        elif assigned_shop:
            text += f"🏪 Магазин: {assigned_shop}\n"
        elif assigned_name:
            text += f"👤 Исполнитель: {assigned_name}\n"
        if creator_name:
            text += f"✍️ Автор: {creator_name}\n"
        text += dl_str
        if desc:
            text += f"\n\n{desc}"
        text += cl_str

        kb = _task_detail_keyboard(task, my_db_id, admin, my_shop=my_shop)
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("task_view_cb: %s", e)
        await callback.answer("Ошибка загрузки задачи")


# ── Смена статуса ─────────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_setstatus_"))
async def task_setstatus_cb(callback: CallbackQuery, state: FSMContext):
    # Format: tsk_setstatus_{task_id}_{status}
    # Status may contain '_' (e.g. in_progress), so split with maxsplit=3
    parts = callback.data.split("_", 3)
    # parts: ['tsk', 'setstatus', '{task_id}', '{status}']
    if len(parts) < 4:
        await callback.answer("Неверный формат команды")
        return
    try:
        task_id = int(parts[2])
    except ValueError:
        await callback.answer("Неверный id задачи")
        return
    new_status = parts[3]

    if new_status not in STATUS_LABELS:
        await callback.answer("Неверный статус")
        return

    db = await get_db(state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        tg_id = callback.from_user.id
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (tg_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = my_row[1] if my_row and len(my_row) > 1 else None
        admin = await is_any_admin(state)

        task = db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        assign_all = task.get('assign_all', False)
        assigned_shop = task.get('assigned_shop', '')
        can_act = (
            admin
            or task.get('assigned_to') == my_db_id
            or assign_all
            or (assigned_shop and my_shop and assigned_shop == my_shop)
        )
        if not can_act:
            await callback.answer("Нет доступа")
            return

        db.update_task_status(task_id, new_status)
        status_label = STATUS_LABELS.get(new_status, new_status)
        await callback.answer(f"Статус: {status_label}")

        creator_id = task.get('created_by')
        if creator_id and creator_id != my_db_id:
            try:
                db.add_notification_to_history(
                    creator_id, "task_status",
                    f"📋 Задача «{task['title']}»: {status_label}"
                )
            except Exception:
                pass

        await _show_tasks_list(callback, state, page=0)
    except Exception as e:
        logger.error("task_setstatus_cb: %s", e)
        await callback.answer("Ошибка изменения статуса")


# ── Список всех задач (tsk_list_N) ───────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_list_"))
async def tasks_list_cb(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[-1])
    await _show_tasks_list(callback, state, page=page)
