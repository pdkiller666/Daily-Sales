"""
Модуль задач в Telegram-боте.
Сотрудник видит назначенные ему задачи и может менять их статус.
Admin видит все задачи организации.

Фичи:
  - Фото-отчёт при завершении задачи (FSM state waiting_photo)
  - Кнопка «Я выполнил» для командных задач (assign_all / shop)
  - Кнопка «↩️ Вернуть» для admin
  - Повторяющиеся задачи: auto-spawn при завершении
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from keyboards import InlineKeyboardBuilder, back_button, home_button, safe_cb, resolve_cb_name
from pagination_utils import page_nav_row
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit
from utils import he
from states import TaskCreateStates, AiTaskCreateStates, TaskEditStates, TaskCommentStates, TaskRateStates, TaskReminderStates
from notif_utils import add_read_btn as _add_read_btn_tasks

tasks_router = Router()
logger = logging.getLogger(__name__)


def _get_sync_db(db):
    """Получить синхронный Database из AsyncDatabase для прямых SQL-запросов.

    AsyncDatabase оборачивает все callable в to_thread — при вызове
    db.get_connection() без await возвращается coroutine. Этот хелпер
    достаёт исходный синхронный Database для inline-SQL внутри async-функций.
    """
    try:
        return object.__getattribute__(db, '_db')
    except AttributeError:
        return db

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

RECURRENCE_LABELS = {
    'daily':   '📅 Ежедневно',
    'weekly':  '📅 Еженедельно',
    'monthly': '📅 Ежемесячно',
}


class TaskPhotoStates(StatesGroup):
    waiting_photo = State()


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
    checklist_total = t.get('checklist_total') or 0
    checklist_done = t.get('checklist_done') or 0
    cl = f" · {checklist_done}/{checklist_total} ☑" if checklist_total > 0 else ""
    return f"{status} {title}{dl}{cl}"


def _tasks_keyboard(tasks: list, is_admin: bool, page: int = 0, tg_id: int = 0,
                    status_filter: str = 'active') -> InlineKeyboardMarkup:
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
    _total_pages = max(1, -(-len(tasks) // page_size))
    nav_row = page_nav_row("tsk_page_", page, page > 0, end < len(tasks), _total_pages)
    if nav_row:
        kb.row(*nav_row)
    # ── Фильтр по статусу ─────────────────────────────────────────────────────
    _filter_tabs = [
        ('active', '📋 Активные'),
        ('done',   '✅ Завершённые'),
        ('all',    '📂 Все'),
    ]
    _tab_row = []
    for _fk, _fl in _filter_tabs:
        _pfx = '› ' if _fk == status_filter else ''
        _tab_row.append(InlineKeyboardButton(
            text=f"{_pfx}{_fl}",
            callback_data=f"tsk_filter_{_fk}"
        ))
    kb.row(*_tab_row)
    if is_admin:
        try:
            from billing_utils import has_module as _hm, has_extension as _he
            _tasks_ai_ok = bool(tg_id and _hm(tg_id, 'tasks_pro') and _he(tg_id, 'tasks_ai'))
        except Exception:
            _tasks_ai_ok = False
        row_btns = [InlineKeyboardButton(text="➕ Создать задачу", callback_data="tsk_create")]
        if _tasks_ai_ok:
            row_btns.append(InlineKeyboardButton(text="✨ AI-задача", callback_data="tsk_ai_create"))
        kb.row(*row_btns)
    try:
        from billing_utils import has_module as _hm_pool
        _pool_ok = bool(tg_id and _hm_pool(tg_id, 'tasks_pro'))
    except Exception:
        _pool_ok = False
    if _pool_ok:
        kb.row(InlineKeyboardButton(text="📬 Пул задач", callback_data="task_pool"))
    try:
        from keyboards import _get_web_interface_url
        _web_url = _get_web_interface_url()
        if _web_url:
            _web_url = _web_url.rstrip("/") + "/tasks"
    except Exception:
        _web_url = None
    if _web_url:
        kb.row(InlineKeyboardButton(text="🌐 Открыть в веб", url=_web_url))
    kb.row(home_button())
    return kb.as_markup()


def _task_detail_keyboard(task: dict, my_db_id: int, is_admin: bool,
                          my_shop: str | None = None,
                          comment_count: int = 0,
                          has_subtasks: bool = False) -> InlineKeyboardMarkup:
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

    if status not in ('done', 'cancelled'):
        if (assign_all or assigned_shop) and not is_admin and can_act:
            kb.row(InlineKeyboardButton(
                text="✅ Я выполнил",
                callback_data=f"tsk_mycomp_{task['id']}"
            ))
        elif status in _STATUS_NEXT and can_act:
            next_status = _STATUS_NEXT[status]
            next_label = STATUS_LABELS.get(next_status, next_status)
            kb.row(InlineKeyboardButton(
                text=f"➡️ {next_label}",
                callback_data=f"tsk_setstatus_{task['id']}_{next_status}"
            ))
        if is_admin or (my_db_id and task.get('created_by') == my_db_id):
            kb.row(InlineKeyboardButton(
                text="✏️ Редактировать",
                callback_data=f"tsk_edit_{task['id']}"
            ))
        if is_admin:
            kb.row(InlineKeyboardButton(
                text="🚫 Отменить задачу",
                callback_data=f"tsk_setstatus_{task['id']}_cancelled"
            ))
    else:
        if is_admin:
            kb.row(InlineKeyboardButton(
                text="↩️ Вернуть в работу",
                callback_data=f"tsk_reopen_{task['id']}"
            ))

    # ── Чеклист (toggle-кнопки) ───────────────────────────────────────────────
    checklist = task.get('checklist', [])
    if checklist and (can_act or is_admin) and status not in ('done', 'cancelled'):
        for _cl in checklist[:10]:
            _cl_mark = "✅" if _cl.get('is_done') else "☐"
            _cl_txt = f"{_cl_mark} {_cl.get('text', '')[:40]}"
            kb.row(InlineKeyboardButton(
                text=_cl_txt,
                callback_data=f"tsk_cl_{task['id']}_{_cl['id']}"
            ))

    # ── Подзадачи: кнопка управления в вебе ─────────────────────────────────
    if has_subtasks:
        try:
            from keyboards import _get_web_interface_url as _gwiu_st
            _wurl_st = _gwiu_st()
            if _wurl_st:
                kb.row(InlineKeyboardButton(
                    text="📎 Управлять подзадачами →",
                    url=f"{_wurl_st.rstrip('/')}/tasks/{task['id']}"
                ))
        except Exception:
            pass

    # ── Напоминание ───────────────────────────────────────────────────────────
    if status not in ('done', 'cancelled') and can_act:
        kb.row(InlineKeyboardButton(
            text="🔔 Напомнить",
            callback_data=f"tsk_remind_{task['id']}"
        ))

    # ── Оценка (только admin, только после завершения) ────────────────────────
    if status == 'done' and is_admin:
        existing_rating = task.get('rating')
        if existing_rating:
            _lbl = f"{'⭐' * existing_rating} Изменить оценку"
        else:
            _lbl = "⭐ Оценить задачу"
        kb.row(InlineKeyboardButton(text=_lbl, callback_data=f"tsk_ratepick_{task['id']}"))

    # ── Ссылка на задачу в вебе ───────────────────────────────────────────────
    try:
        from keyboards import _get_web_interface_url as _gwiu_td
        _wurl = _gwiu_td()
        if _wurl:
            kb.row(InlineKeyboardButton(
                text="🔗 В вебе",
                url=f"{_wurl.rstrip('/')}/tasks/{task['id']}"
            ))
    except Exception:
        pass

    kb.row(InlineKeyboardButton(
        text=f"💬 Комментарии ({comment_count})",
        callback_data=f"tsk_cmts_{task['id']}_0"
    ))
    kb.row(back_button("tsk_list_0", "⬅️ К списку"))
    kb.row(home_button())
    return kb.as_markup()


async def _show_tasks_list(target, state: FSMContext, page: int = 0,
                           status_filter: str | None = None):
    """Показать список задач. target — Message или CallbackQuery."""
    from aiogram.types import Message as Msg
    tg_id = target.from_user.id

    # ── Gate: модуль tasks_pro ─────────────────────────────────────────────
    try:
        from billing_utils import has_module as _hm_gate
        if not _hm_gate(tg_id, 'tasks_pro'):
            _no_access = (
                "📋 <b>Задачи</b>\n\n"
                "Модуль задач не подключён.\n"
                "Обратитесь к владельцу организации для активации."
            )
            from aiogram.utils.keyboard import InlineKeyboardBuilder as _IKB
            _kb = _IKB()
            _kb.row(home_button())
            if isinstance(target, Msg):
                await target.answer(_no_access, parse_mode="HTML",
                                    reply_markup=_kb.as_markup())
            else:
                await target.answer()
                await target.message.edit_text(_no_access, parse_mode="HTML",
                                               reply_markup=_kb.as_markup())
            return
    except Exception:
        pass  # при ошибке биллинга — пропускаем (fail-open)

    db = await get_db(tg_id, state)
    if db is None:
        text = "⚠️ Нет активной организации."
        if isinstance(target, Msg):
            await target.answer(text)
        else:
            await target.answer()
            await target.message.edit_text(text)
        return

    try:
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = user[8] if user and len(user) > 8 else None
        admin = is_any_admin(tg_id)

        if status_filter is None:
            _fdata = await state.get_data()
            status_filter = _fdata.get('tsk_status_filter')
            if not status_filter and my_db_id:
                try:
                    _db_pref = await db.get_user_task_pref(my_db_id, 'status_filter', 'active')
                    status_filter = _db_pref if _db_pref in ('active', 'done', 'all') else 'active'
                except Exception:
                    status_filter = 'active'
            if not status_filter:
                status_filter = 'active'

        tasks = await db.get_tasks(is_admin=admin, my_user_id=my_db_id, my_shop=my_shop)
        if status_filter == 'done':
            filtered = [t for t in tasks if t.get('status') in ('done', 'cancelled')]
        elif status_filter == 'all':
            filtered = tasks
        else:
            filtered = [t for t in tasks if t.get('status') not in ('done', 'cancelled')]
            status_filter = 'active'

        _filter_label = {'active': 'активных', 'done': 'завершённых', 'all': 'всего'}
        if not filtered:
            _empty = 'Активных задач нет.' if status_filter == 'active' else 'Задач нет.'
            text = f"📋 <b>Задачи</b>\n\n{_empty}"
        else:
            _title = 'Мои задачи' if status_filter == 'active' else 'Задачи'
            text = (f"📋 <b>{_title}</b>\n"
                    f"<i>Всего {_filter_label.get(status_filter, '')}: {len(filtered)}</i>")

        kb = _tasks_keyboard(filtered, admin, page, tg_id=tg_id, status_filter=status_filter)
        await state.update_data(tsk_list=filtered, tsk_page=page, tsk_my_db_id=my_db_id,
                                tsk_status_filter=status_filter)

        if isinstance(target, Msg):
            await fsm_edit(state, target, text, kb)
        else:
            await target.answer()
            try:
                await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception as _edit_err:
                _emsg = str(_edit_err).lower()
                if "message is not modified" in _emsg:
                    pass  # контент не изменился — ничего не делаем
                else:
                    raise
    except Exception as e:
        logger.error("_show_tasks_list: %s", e, exc_info=True)
        error_text = "⚠️ Ошибка загрузки задач."
        if isinstance(target, Msg):
            await target.answer(error_text)
        else:
            try:
                await target.answer()
                await target.message.edit_text(error_text)
            except Exception:
                pass


# ── Entrypoint ────────────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data == "tasks_menu")
async def tasks_menu_cb(callback: CallbackQuery, state: FSMContext):
    await _show_tasks_list(callback, state, page=0)


# ── Пагинация ────────────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_page_"))
async def tasks_page_cb(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[-1])
    await callback.answer()
    await _show_tasks_list(callback, state, page=page)


# ── Фильтр по статусу ─────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_filter_"))
async def tsk_filter_cb(callback: CallbackQuery, state: FSMContext):
    """Переключить фильтр списка задач (active / done / all)."""
    sf = callback.data.split("_")[-1]
    if sf not in ('active', 'done', 'all'):
        await callback.answer()
        return
    await state.update_data(tsk_status_filter=sf)
    try:
        _db = await get_db(callback.from_user.id, state)
        if _db is not None:
            _user = await _db.get_user(callback.from_user.id)
            _uid = _user[0] if _user else 0
            if _uid:
                await _db.set_user_task_pref(_uid, 'status_filter', sf)
    except Exception:
        pass
    await _show_tasks_list(callback, state, page=0, status_filter=sf)


# ── Пул задач ────────────────────────────────────────────────────────────────

def _pool_tasks_keyboard(tasks: list, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    page_size = 5
    start = page * page_size
    end = min(start + page_size, len(tasks))
    for t in tasks[start:end]:
        task_id = t.get('id', 0)
        priority_icon = {'urgent': '🔴', 'high': '🟡', 'normal': '🔵', 'low': '🟢'}.get(t.get('priority', ''), '🔵')
        title = t.get('title', '—')[:38]
        deadline = t.get('deadline', '')
        dl = ""
        if deadline:
            try:
                from datetime import date as _d
                dl = f" · {_d.fromisoformat(deadline[:10]).strftime('%d.%m')}"
            except Exception:
                pass
        kb.row(InlineKeyboardButton(
            text=f"{priority_icon} {title}{dl}",
            callback_data=f"tsk_pool_view_{task_id}"
        ))
    _total_pages = max(1, -(-len(tasks) // page_size))
    nav_row = page_nav_row("tsk_pool_p_", page, page > 0, end < len(tasks), _total_pages)
    if nav_row:
        kb.row(*nav_row)
    kb.row(back_button("tsk_list_0", "⬅️ Мои задачи"))
    kb.row(home_button())
    return kb.as_markup()


async def _show_pool_list(target, state: FSMContext, page: int = 0, already_answered: bool = False):
    from aiogram.types import Message as Msg
    tg_id = target.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        text = "⚠️ Нет активной организации."
        if isinstance(target, Msg):
            await target.answer(text)
        else:
            if not already_answered:
                await target.answer()
            await target.message.edit_text(text)
        return

    try:
        from billing_utils import has_module as _hm
        if not _hm(tg_id, 'tasks_pro'):
            await target.answer("Пул задач доступен в модуле «Задачи Pro».", show_alert=True)
            return
    except Exception:
        pass

    try:
        tasks = await db.get_unassigned_tasks()
        if not tasks:
            text = "📬 <b>Пул задач</b>\n\nСвободных задач нет."
        else:
            text = f"📬 <b>Пул задач</b>\n<i>Задачи без исполнителя: {len(tasks)}</i>"
        kb = _pool_tasks_keyboard(tasks, page)
        await state.update_data(tsk_pool_list=tasks, tsk_pool_page=page)
        if isinstance(target, Msg):
            await fsm_edit(state, target, text, kb)
        else:
            if not already_answered:
                await target.answer()
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("_show_pool_list: %s", e)
        error_text = "⚠️ Ошибка загрузки пула задач."
        if isinstance(target, Msg):
            await target.answer(error_text)
        else:
            if not already_answered:
                await target.answer()
            await target.message.edit_text(error_text)


@tasks_router.callback_query(F.data == "task_pool")
async def task_pool_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _show_pool_list(callback, state, page=0, already_answered=True)


@tasks_router.callback_query(F.data.startswith("tsk_pool_p_"))
async def task_pool_page_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    page = int(callback.data.split("_")[-1])
    await _show_pool_list(callback, state, page=page, already_answered=True)


@tasks_router.callback_query(F.data.startswith("tsk_pool_view_"))
async def task_pool_view_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена или уже взята")
            return

        if task.get('assigned_to') or task.get('assign_all') or task.get('assigned_shop'):
            await callback.answer("Задача уже назначена и недоступна в пуле", show_alert=True)
            await _show_pool_list(callback, state)
            return

        title = task.get('title', '—')
        desc = task.get('description', '')
        priority = PRIORITY_LABELS.get(task.get('priority', ''), task.get('priority', ''))
        deadline = task.get('deadline', '')
        dl_str = ""
        if deadline:
            try:
                from datetime import date as _date, datetime as _dt
                has_time = len(deadline) >= 13 and ("T" in deadline or " " in deadline[10:])
                if has_time:
                    dl_dt = _dt.fromisoformat(deadline[:16].replace("T", " "))
                    overdue_flag = dl_dt < _dt.now() and task.get('status') not in ('done', 'cancelled')
                    dl_str = f"\n📅 Срок: {dl_dt.strftime('%d.%m.%Y %H:%M')}"
                else:
                    d = _date.fromisoformat(deadline[:10])
                    overdue_flag = d < _date.today()
                    dl_str = f"\n📅 Срок: {d.strftime('%d.%m.%Y')}"
                if overdue_flag:
                    dl_str += " ⚠️ Просрочена"
            except Exception:
                dl_str = f"\n📅 Срок: {deadline}"

        topic_name = task.get('topic_name', '')
        creator_name = task.get('creator_name', '')

        checklist = task.get('checklist', [])
        cl_str = ""
        if checklist:
            lines = [f"  {'✅' if item.get('is_done') else '☐'} {he(item.get('text', ''))}" for item in checklist]
            cl_str = "\n\nЧеклист:\n" + "\n".join(lines)

        text = f"📬 <b>{he(title)}</b>\n{priority}\n"
        if topic_name:
            text += f"🏷 {he(topic_name)}\n"
        if creator_name:
            text += f"✍️ Автор: {he(creator_name)}\n"
        text += dl_str
        if desc:
            text += f"\n\n{he(desc)}"
        text += cl_str
        text += "\n\n<i>Задача свободна — нажмите «Взять задачу», чтобы назначить её себе.</i>"

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="✋ Взять задачу", callback_data=f"tsk_take_{task_id}"))
        kb.row(back_button("task_pool", "⬅️ Пул задач"))
        kb.row(home_button())

        await callback.answer()
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception as e:
        logger.error("task_pool_view_cb: %s", e)
        await callback.answer("Ошибка загрузки задачи")


@tasks_router.callback_query(F.data.startswith("tsk_take_"))
async def task_take_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    _has_tasks_pro = False
    try:
        from billing_utils import has_module as _hm
        _has_tasks_pro = bool(_hm(tg_id, 'tasks_pro'))
    except Exception as _be:
        logger.error("task_take_cb billing check: %s", _be)
    if not _has_tasks_pro:
        await callback.answer(
            "Пул задач доступен в модуле «Задачи Pro».", show_alert=True)
        return

    try:
        user = await db.get_user(tg_id)
        if not user:
            await callback.answer("Пользователь не найден")
            return
        my_db_id = user[0]
        _display = (f"{user[2] or ''} {user[3] or ''}".strip()
                    or (user[12] if len(user) > 12 else None)
                    or str(my_db_id))

        ok = await db.self_assign_task(task_id, my_db_id)
        if ok:
            try:
                await db.add_task_history(task_id, my_db_id, 'assigned', None, f"Взял в работу: {_display}")
            except Exception as _he:
                logger.error("task_take_cb add_task_history: %s", _he)
            await callback.answer("✅ Задача взята в работу!", show_alert=True)
            await _show_tasks_list(callback, state, page=0)
        else:
            await callback.answer("⚠️ Задача уже занята другим сотрудником.", show_alert=True)
            await _show_pool_list(callback, state, page=0)
    except Exception as e:
        logger.error("task_take_cb: %s", e)
        await callback.answer("Ошибка назначения задачи")


# ── Просмотр задачи ──────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_view_"))
async def task_view_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id

    # ── Gate: модуль tasks_pro ─────────────────────────────────────────────
    try:
        from billing_utils import has_module as _hm_gate
        if not _hm_gate(tg_id, 'tasks_pro'):
            await callback.answer("Модуль задач не подключён", show_alert=True)
            return
    except Exception:
        pass  # fail-open при ошибке биллинга

    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = user[8] if user and len(user) > 8 else None
        admin = is_any_admin(tg_id)

        task = await db.get_task(task_id)
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
                has_time = len(deadline) >= 13 and ("T" in deadline or " " in deadline[10:])
                if has_time:
                    from datetime import datetime as _dt
                    dl_dt = _dt.fromisoformat(deadline[:16].replace("T", " "))
                    overdue_flag = dl_dt < _dt.now() and task.get('status') not in ('done', 'cancelled')
                    dl_str = f"\n📅 Срок: {dl_dt.strftime('%d.%m.%Y %H:%M')}"
                else:
                    from datetime import date as _date
                    d = _date.fromisoformat(deadline[:10])
                    overdue_flag = d < _date.today() and task.get('status') not in ('done', 'cancelled')
                    dl_str = f"\n📅 Срок: {d.strftime('%d.%m.%Y')}"
                if overdue_flag:
                    dl_str += " ⚠️ Просрочена"
            except Exception:
                dl_str = f"\n📅 Срок: {deadline}"

        assigned_name = task.get('assigned_name', '')
        creator_name = task.get('creator_name', '')
        topic_name = task.get('topic_name', '')
        assigned_shop = task.get('assigned_shop', '')
        assign_all = task.get('assign_all', False)
        recurrence = task.get('recurrence') or ''

        checklist = task.get('checklist', [])
        cl_str = ""
        if checklist:
            cl_done = sum(1 for i in checklist if i.get('is_done'))
            lines = []
            for item in checklist:
                mark = "✅" if item.get('is_done') else "☐"
                lines.append(f"  {mark} {he(item.get('text', ''))}")
            cl_str = f"\n\nЧеклист ({cl_done}/{len(checklist)}):\n" + "\n".join(lines)

        text = f"📋 <b>{he(title)}</b>\n{status} · {priority}\n"
        if topic_name:
            text += f"🏷 {he(topic_name)}\n"
        if assign_all:
            text += "👥 Исполнитель: Вся команда\n"
        elif assigned_shop:
            text += f"🏪 Магазин: {he(assigned_shop)}\n"
        elif assigned_name:
            text += f"👤 Исполнитель: {he(assigned_name)}\n"
        if creator_name:
            text += f"✍️ Автор: {he(creator_name)}\n"
        if recurrence and recurrence not in ('none', ''):
            text += f"🔁 {RECURRENCE_LABELS.get(recurrence, recurrence)}\n"
        text += dl_str
        if desc:
            text += f"\n\n{he(desc)}"
        text += cl_str

        if admin and (assign_all or assigned_shop):
            try:
                completions = await db.get_task_user_completions(task_id)
                if completions:
                    text += f"\n\n👥 Выполнили ({len(completions)}):\n"
                    for c in completions[:8]:
                        text += f"  ✅ {he(c['name'])}\n"
            except Exception:
                pass

        # ── Подзадачи ──────────────────────────────────────────────────────────
        try:
            subtasks = await db.get_subtasks(task_id)
            if subtasks:
                _st_done = sum(1 for s in subtasks if s.get('status') == 'done')
                _st_total = len(subtasks)
                text += f"\n\n📎 <b>Подзадачи ({_st_total})</b>: {_st_done} выполнено\n"
                _ST_ICON = {'new': '🆕', 'in_progress': '▶️', 'done': '✅', 'cancelled': '🚫'}
                for _s in subtasks[:5]:
                    _ico = _ST_ICON.get(_s.get('status', 'new'), '•')
                    text += f"  {_ico} {he(_s.get('title', '—'))}\n"
                if _st_total > 5:
                    text += f"  <i>+ ещё {_st_total - 5}</i>\n"
        except Exception as _ste:
            logger.error("task_view_cb subtasks: %s", _ste)

        # ── Оценка ─────────────────────────────────────────────────────────────
        _rating = task.get('rating')
        if _rating:
            _rc = task.get('rating_comment', '') or ''
            text += f"\n\n⭐ <b>Оценка: {'⭐' * _rating} ({_rating}/5)</b>"
            if _rc:
                text += f"\n<i>{he(_rc)}</i>"

        try:
            comment_count = await db.get_task_comment_count(task_id)
        except Exception:
            comment_count = 0
        kb = _task_detail_keyboard(task, my_db_id, admin, my_shop=my_shop,
                                    comment_count=comment_count,
                                    has_subtasks=bool(subtasks))
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("task_view_cb: %s", e)
        await callback.answer("Ошибка загрузки задачи")


@tasks_router.callback_query(F.data.startswith("task_open_"))
async def task_open_cb(callback: CallbackQuery, state: FSMContext):
    """Открыть задачу из напоминания (callback_data = task_open_{task_id})."""
    await task_view_cb(callback, state)


# ── Смена статуса ─────────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_setstatus_"))
async def task_setstatus_cb(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_", 3)
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

    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = user[8] if user and len(user) > 8 else None
        admin = is_any_admin(tg_id)

        task = await db.get_task(task_id)
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

        # Для не-admin: только допустимый следующий шаг по цепочке (cancelled — только admin)
        if not admin:
            allowed_next = _STATUS_NEXT.get(task.get('status', ''))
            if new_status != allowed_next:
                await callback.answer("Недопустимый переход статуса")
                return

        await db.update_task_status(task_id, new_status)

        try:
            await db.add_task_history(task_id, my_db_id, 'status', task['status'], new_status)
        except Exception:
            pass

        try:
            from task_automation import run_rules as _run_rules
            _ev_task = dict(task)
            _ev_task['_old_status'] = task.get('status', '')
            _ev_task['status'] = new_status
            _run_rules(db, 'status_changed', _ev_task, my_db_id)
        except Exception as _are:
            logger.warning("task status automation: %s", _are)

        if new_status in ('done', 'review') and (assign_all or assigned_shop):
            try:
                await db.record_task_user_completion(task_id, my_db_id, new_status)
            except Exception:
                pass

        if new_status == 'done':
            try:
                _new_assigned_to = _spawn_recurring_task(db, task)
                if _new_assigned_to:
                    try:
                        _conn_r = _get_sync_db(db).get_connection()
                        _r_row = _conn_r.execute(
                            "SELECT telegram_id FROM users WHERE id = ?", (_new_assigned_to,)
                        ).fetchone()
                        _conn_r.close()
                        _r_tg = _r_row[0] if _r_row else None
                        if _r_tg:
                            await db.add_notification_to_history(
                                _new_assigned_to, 'task_assigned',
                                f"🔁 Создана следующая задача: {task['title']}")
                            await callback.bot.send_message(
                                _r_tg,
                                f"🔁 <b>Создана следующая задача</b>\n\n<b>{he(task['title'])}</b>",
                                parse_mode="HTML",
                                reply_markup=_add_read_btn_tasks()
                            )
                            try:
                                from web.push_utils import send_web_push
                                await asyncio.to_thread(
                                    send_web_push, int(_r_tg),
                                    "🔁 Создана следующая задача", task['title'], "/tasks"
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass

        status_label = STATUS_LABELS.get(new_status, new_status)
        creator_id = task.get('created_by')
        if creator_id and creator_id != my_db_id:
            try:
                await db.add_notification_to_history(
                    creator_id, "task_status",
                    f"📋 Задача «{task['title']}»: {status_label}"
                )
            except Exception:
                pass

        if new_status in ('done', 'review'):
            await state.update_data(tsk_photo_task_id=task_id)
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="tsk_photo_skip"))
            await callback.answer()
            await callback.message.edit_text(
                f"✅ <b>Статус обновлён</b>: {status_label}\n\n"
                f"📎 Хотите прикрепить файл к задаче\n«{he(task['title'][:50])}»?\n\n"
                "<i>Отправьте фото, документ, видео или любой файл — или нажмите «Пропустить»</i>",
                reply_markup=kb.as_markup(),
                parse_mode="HTML"
            )
            await state.set_state(TaskPhotoStates.waiting_photo)
        else:
            await callback.answer(f"Статус: {status_label}")
            await _show_tasks_list(callback, state, page=0)

    except Exception as e:
        logger.error("task_setstatus_cb: %s", e)
        await callback.answer("Ошибка изменения статуса")


def _spawn_recurring_task(db, task: dict):
    """Создать следующую задачу для повторяющейся задачи (бот-обёртка).

    Делегирует в единый идемпотентный хелпер ``task_automation.spawn_recurring_if_done``,
    общий для веба, бота и SLA-автоматизации. ``task['status']`` здесь — старый
    статус (читается до ``update_task_status``), переход — в ``done``.
    """
    from task_automation import spawn_recurring_if_done
    return spawn_recurring_if_done(db, task, task.get('status', ''), 'done')


# ── Фото-отчёт: пропустить ────────────────────────────────────────────────────

@tasks_router.callback_query(F.data == "tsk_photo_skip")
async def task_photo_skip_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.update_data(tsk_photo_task_id=None)
    await _show_tasks_list(callback, state, page=0)


# ── Вложение при завершении: любой файл (фото / документ / видео / аудио / голос) ──

@tasks_router.message(
    TaskPhotoStates.waiting_photo,
    F.photo | F.document | F.video | F.audio | F.voice | F.video_note
)
async def task_attachment_handler(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    task_id = data.get('tsk_photo_task_id')
    db = await get_db(message.from_user.id, state)

    if not task_id or db is None:
        await state.set_state(None)
        await message.answer("⚠️ Не удалось сохранить файл.")
        return

    try:
        # Определяем тип файла и метаданные
        if message.photo:
            file_obj   = message.photo[-1]
            file_name  = "фото_отчёт.jpg"
            file_type  = "image/jpeg"
            ext        = ".jpg"
        elif message.document:
            file_obj   = message.document
            file_name  = message.document.file_name or "документ"
            file_type  = message.document.mime_type or "application/octet-stream"
            ext        = os.path.splitext(file_name)[1] or ""
        elif message.video:
            file_obj   = message.video
            file_name  = message.video.file_name or "видео_отчёт.mp4"
            file_type  = message.video.mime_type or "video/mp4"
            ext        = os.path.splitext(file_name)[1] or ".mp4"
        elif message.audio:
            file_obj   = message.audio
            file_name  = message.audio.file_name or "аудио.mp3"
            file_type  = message.audio.mime_type or "audio/mpeg"
            ext        = os.path.splitext(file_name)[1] or ".mp3"
        elif message.voice:
            file_obj   = message.voice
            file_name  = "голосовой_отчёт.ogg"
            file_type  = "audio/ogg"
            ext        = ".ogg"
        elif message.video_note:
            file_obj   = message.video_note
            file_name  = "кружок_отчёт.mp4"
            file_type  = "video/mp4"
            ext        = ".mp4"
        else:
            await message.answer("⚠️ Неподдерживаемый тип файла.")
            return

        file_info = await bot.get_file(file_obj.file_id)

        org_db_path = db.db_file
        base = os.path.splitext(org_db_path)[0]
        uploads_dir = base + "_uploads/tasks"
        month_dir = datetime.now().strftime("%Y-%m")
        dest_dir = os.path.join(uploads_dir, month_dir)
        os.makedirs(dest_dir, exist_ok=True)

        uid = uuid.uuid4().hex[:12]
        dest = os.path.join(dest_dir, f"{uid}_report{ext}")

        file_data = await bot.download_file(file_info.file_path)
        with open(dest, 'wb') as f:
            if hasattr(file_data, 'read'):
                f.write(file_data.read())
            else:
                f.write(file_data)

        tg_id = message.from_user.id
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0

        await db.add_task_attachments(task_id, my_db_id, [{
            "file_path": dest,
            "file_name": file_name,
            "file_type": file_type,
            "file_size": os.path.getsize(dest),
            "uploaded_by": my_db_id,
        }])

        await state.set_state(None)
        await state.update_data(tsk_photo_task_id=None)
        await message.answer("📎 Файл прикреплён к задаче!")
        await _show_tasks_list(message, state, page=0)

    except Exception as e:
        logger.error("task_attachment_handler: %s", e)
        await state.set_state(None)
        await message.answer("⚠️ Ошибка сохранения файла. Попробуйте снова.")


@tasks_router.message(TaskPhotoStates.waiting_photo)
async def task_attachment_wrong_input(message: Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="tsk_photo_skip"))
    await message.answer(
        "📎 Пожалуйста, отправьте <b>фото, документ, видео или аудио</b> — или нажмите «Пропустить».",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )


# ── Личное выполнение задачи (assign_all / shop) ──────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_mycomp_"))
async def task_mycomp_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = (user[8] or '') if user and len(user) > 8 else ''

        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return

        # For shop-specific tasks: verify user belongs to the assigned shop
        _assigned_shop = task.get('assigned_shop', '')
        if _assigned_shop and not task.get('assign_all'):
            if my_shop != _assigned_shop:
                await callback.answer("Эта задача назначена другому магазину", show_alert=True)
                return

        await db.record_task_user_completion(task_id, my_db_id, 'done')

        # Авто-переход в «На проверку» когда все участники выполнили
        try:
            _assign_all = task.get('assign_all', False)
            _assigned_shop = task.get('assigned_shop', '')
            if (_assign_all or _assigned_shop) and task.get('status') not in ('done', 'cancelled', 'review'):
                _conn_s = _get_sync_db(db).get_connection()
                try:
                    _staff_rows = _conn_s.execute(
                        "SELECT id, shop_name FROM users"
                    ).fetchall()
                finally:
                    _conn_s.close()
                member_ids = (
                    {r[0] for r in _staff_rows} if _assign_all
                    else {r[0] for r in _staff_rows if (r[1] or '') == _assigned_shop}
                )
                if member_ids:
                    _comps = await db.get_task_user_completions(task_id)
                    if member_ids.issubset({c['user_id'] for c in _comps}):
                        _mc_old = task.get('status', '')
                        await db.update_task_status(task_id, 'review')
                        if _mc_old != 'review':
                            try:
                                from task_automation import run_rules as _run_rules
                                _ev_task = dict(task)
                                _ev_task['_old_status'] = _mc_old
                                _ev_task['status'] = 'review'
                                _run_rules(db, 'status_changed', _ev_task, my_db_id)
                            except Exception as _are:
                                logger.warning("task_mycomp_cb automation: %s", _are)
        except Exception as _ae:
            logger.error("task_mycomp_cb auto-advance: %s", _ae)

        creator_id = task.get('created_by')
        if creator_id and creator_id != my_db_id:
            try:
                user_name = callback.from_user.first_name or "Сотрудник"
                await db.add_notification_to_history(
                    creator_id, "task_status",
                    f"✅ «{task['title']}» — {user_name} отметил выполнено"
                )
            except Exception:
                pass

        await state.update_data(tsk_photo_task_id=task_id)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="tsk_photo_skip"))
        await callback.answer()
        await callback.message.edit_text(
            f"✅ <b>Отмечено как выполнено!</b>\n\n"
            f"📎 Хотите прикрепить файл к задаче?\n"
            "<i>Отправьте фото, документ, видео или любой файл — или нажмите «Пропустить»</i>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(TaskPhotoStates.waiting_photo)

    except Exception as e:
        logger.error("task_mycomp_cb: %s", e)
        await callback.answer("Ошибка")


# ── Вернуть задачу в работу (admin) ──────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_reopen_"))
async def task_reopen_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        admin = is_any_admin(tg_id)
        if not admin:
            await callback.answer("Нет доступа")
            return

        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return

        try:
            _conn_r = _get_sync_db(db).get_connection()
            _my_row = _conn_r.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (tg_id,)
            ).fetchone()
            _conn_r.close()
            my_db_id = _my_row[0] if _my_row else None
        except Exception:
            my_db_id = None

        _ro_old = task.get('status', '')
        await db.update_task_status(task_id, 'in_progress')

        try:
            await db.add_task_history(task_id, my_db_id, 'status', task['status'], 'in_progress')
        except Exception:
            pass

        if _ro_old != 'in_progress':
            try:
                from task_automation import run_rules as _run_rules
                _ev_task = dict(task)
                _ev_task['_old_status'] = _ro_old
                _ev_task['status'] = 'in_progress'
                _run_rules(db, 'status_changed', _ev_task, my_db_id)
            except Exception as _are:
                logger.warning("task_reopen_cb automation: %s", _are)

        # Уведомить исполнителя — Telegram + колокольчик + Web Push
        _assigned_to = task.get('assigned_to')
        if _assigned_to:
            try:
                _conn2 = _get_sync_db(db).get_connection()
                _ar = _conn2.execute(
                    "SELECT telegram_id FROM users WHERE id = ?", (_assigned_to,)
                ).fetchone()
                _conn2.close()
                _assignee_tg = _ar[0] if _ar else None
                if _assignee_tg:
                    try:
                        await db.add_notification_to_history(
                            _assigned_to, 'task_assigned',
                            f"↩️ Задача возвращена в работу: {task['title']}")
                    except Exception:
                        pass
                    try:
                        await callback.bot.send_message(
                            _assignee_tg,
                            f"↩️ <b>Задача возвращена в работу</b>\n\n"
                            f"<b>{he(task['title'])}</b>\n\n"
                            f"🌐 Откройте веб-кабинет для деталей.",
                            parse_mode="HTML",
                            reply_markup=_add_read_btn_tasks()
                        )
                    except Exception:
                        pass
                    try:
                        from web.push_utils import send_web_push
                        await asyncio.to_thread(
                            send_web_push, _assignee_tg,
                            "↩️ Задача возвращена", task['title'], "/tasks"
                        )
                    except Exception:
                        pass
            except Exception as _ne:
                logger.warning("task_reopen notify: %s", _ne)

        await callback.answer("↩️ Задача возвращена в работу")
        await _show_tasks_list(callback, state, page=0)

    except Exception as e:
        logger.error("task_reopen_cb: %s", e)
        await callback.answer("Ошибка")


# ── Список задач (tsk_list_N) ─────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_list_"))
async def tasks_list_cb(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[-1])
    await callback.answer()
    await _show_tasks_list(callback, state, page=page)


# ── Toggle пункта чеклиста ────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_cl_"))
async def tsk_cl_cb(callback: CallbackQuery, state: FSMContext):
    """Переключить пункт чеклиста и обновить экран задачи."""
    parts = callback.data.split("_")
    try:
        task_id = int(parts[2])
        item_id = int(parts[3])
    except (ValueError, IndexError):
        await callback.answer("Ошибка формата")
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    user = await db.get_user(tg_id)
    my_db_id = user[0] if user else 0
    my_shop = user[8] if user and len(user) > 8 else None
    _is_admin_cl = is_any_admin(tg_id)
    _can_act_cl = (
        _is_admin_cl
        or task.get('assigned_to') == my_db_id
        or bool(task.get('assign_all'))
        or bool(task.get('assigned_shop') and my_shop
                and task.get('assigned_shop') == my_shop)
    )
    if not _can_act_cl:
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        await db.toggle_task_checklist_item(item_id, my_db_id)
    except Exception as _tce:
        logger.error("tsk_cl_cb toggle: %s", _tce)
        await callback.answer("Ошибка обновления")
        return
    await callback.answer()
    await _show_task_after_edit(callback, state, task_id, tg_id)


# ══════════════════════════════════════════════════════════════════════════════
# СОЗДАНИЕ ЗАДАЧИ ИЗ БОТА (FSM-визард, только для admin)
# Шаги: название → описание → исполнитель → приоритет → дедлайн → подтверждение
# ══════════════════════════════════════════════════════════════════════════════

def _tc_cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_c_cancel"))
    return kb.as_markup()


def _tc_desc_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="tsk_c_skip_desc"))
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_c_cancel"))
    return kb.as_markup()


def _tc_who_kb(db) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="👥 Всей команде", callback_data="tsk_c_who_all"))
    try:
        conn = _get_sync_db(db).get_connection()
        shops = conn.execute(
            "SELECT DISTINCT shop_name FROM users "
            "WHERE shop_name IS NOT NULL AND shop_name != '' ORDER BY shop_name"
        ).fetchall()
        conn.close()
        for (sh,) in shops[:8]:
            kb.row(InlineKeyboardButton(
                text=f"🏪 {sh}",
                callback_data=safe_cb("tsk_csh_", sh)
            ))
    except Exception:
        pass
    kb.row(InlineKeyboardButton(text="👤 Конкретный сотрудник", callback_data="tsk_c_who_users"))
    kb.row(InlineKeyboardButton(text="📋 Без назначения", callback_data="tsk_c_who_none"))
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_c_cancel"))
    return kb.as_markup()


def _tc_users_kb(db, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    try:
        rows = _get_sync_db(db).get_all_users()
        PAGE = 8
        start = page * PAGE
        chunk = rows[start:start + PAGE]
        for r in chunk:
            uid = r[0]
            name = f"{r[2] or ''} {r[3] or ''}".strip() or r[12] or f"User#{uid}"
            shop = f" ({r[8]})" if r[8] else ""
            kb.row(InlineKeyboardButton(
                text=f"👤 {name}{shop}",
                callback_data=f"tsk_cu_{uid}"
            ))
        _tp = max(1, -(-len(rows) // PAGE))
        nav = page_nav_row("tsk_c_upg_", page, page > 0, start + PAGE < len(rows), _tp)
        if nav:
            kb.row(*nav)
    except Exception:
        pass
    kb.row(back_button("tsk_c_back_who"))
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_c_cancel"))
    return kb.as_markup()


def _tc_prio_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🟢 Низкий",  callback_data="tsk_c_prio_low"),
        InlineKeyboardButton(text="🔵 Обычный", callback_data="tsk_c_prio_normal"),
    )
    kb.row(
        InlineKeyboardButton(text="🟡 Высокий", callback_data="tsk_c_prio_high"),
        InlineKeyboardButton(text="🔴 Срочно",  callback_data="tsk_c_prio_urgent"),
    )
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_c_cancel"))
    return kb.as_markup()


def _tc_dl_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏭ Без дедлайна", callback_data="tsk_c_skip_dl"))
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_c_cancel"))
    return kb.as_markup()


def _tc_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Создать", callback_data="tsk_c_ok"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_c_cancel"),
    )
    return kb.as_markup()


def _tc_summary(data: dict) -> str:
    title    = he(data.get('tsk_c_title', '—'))
    desc     = he(data.get('tsk_c_desc', '')) or '<i>нет</i>'
    atype    = data.get('tsk_c_assign_type', 'none')
    priority = data.get('tsk_c_priority', 'normal')
    deadline = data.get('tsk_c_deadline', '') or '<i>нет</i>'

    if atype == 'all':
        assignee = '👥 Вся команда'
    elif atype == 'shop':
        assignee = f"🏪 {he(data.get('tsk_c_assign_shop', ''))}"
    elif atype == 'user':
        assignee = f"👤 {he(data.get('tsk_c_assign_uname', ''))}"
    else:
        assignee = '📋 Без назначения'

    prio_map = {'low': '🟢 Низкий', 'normal': '🔵 Обычный',
                'high': '🟡 Высокий', 'urgent': '🔴 Срочно'}
    prio_label = prio_map.get(priority, priority)

    return (
        f"📋 <b>Новая задача — подтверждение</b>\n\n"
        f"<b>Название:</b> {title}\n"
        f"<b>Описание:</b> {desc}\n"
        f"<b>Исполнитель:</b> {assignee}\n"
        f"<b>Приоритет:</b> {prio_label}\n"
        f"<b>Дедлайн:</b> {deadline}"
    )


def _tc_parse_deadline(text: str) -> str | None:
    """Парсит дедлайн из текста. Форматы: 25.06, 25.06.2026, 25.06 14:00, 25.06.2026 14:00"""
    from datetime import date as _date, datetime as _dt
    text = text.strip()
    formats = [
        ("%d.%m.%Y %H:%M", True),
        ("%d.%m %H:%M",    True),
        ("%d.%m.%Y",       False),
        ("%d.%m",          False),
    ]
    today = _date.today()
    for fmt, has_time in formats:
        try:
            if not has_time:
                d = _dt.strptime(text, fmt).date()
                if fmt == "%d.%m":
                    d = d.replace(year=today.year)
                    if d < today:
                        d = d.replace(year=today.year + 1)
                return d.isoformat()
            else:
                dt = _dt.strptime(text, fmt)
                if fmt == "%d.%m %H:%M":
                    dt = dt.replace(year=today.year)
                    if dt.date() < today:
                        dt = dt.replace(year=today.year + 1)
                return dt.strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return None


# ── Шаг 0: Вход в визард ──────────────────────────────────────────────────────

@tasks_router.callback_query(F.data == "tsk_create")
async def tsk_create_entry(callback: CallbackQuery, state: FSMContext):
    admin = is_any_admin(callback.from_user.id)
    if not admin:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(TaskCreateStates.waiting_title)
    await callback.message.edit_text(
        "📋 <b>Создание задачи</b> — шаг 1/5\n\n"
        "Введите <b>название</b> задачи:",
        reply_markup=_tc_cancel_kb(),
        parse_mode="HTML"
    )


# ── Шаг 1: Название ───────────────────────────────────────────────────────────

@tasks_router.message(TaskCreateStates.waiting_title)
async def tsk_c_title_msg(message: Message, state: FSMContext):
    title = message.text.strip() if message.text else ""
    if not title:
        await message.answer("⚠️ Название не может быть пустым. Введите название задачи:")
        try:
            await message.delete()
        except Exception:
            pass
        return
    if len(title) > 200:
        await message.answer(
            "⚠️ Название слишком длинное. Пожалуйста, сократите его до 200 символов и повторите ввод."
        )
        try:
            await message.delete()
        except Exception:
            pass
        return
    await state.update_data(tsk_c_title=title)
    await state.set_state(TaskCreateStates.waiting_desc)
    try:
        await message.delete()
    except Exception:
        pass
    await fsm_edit(
        state,
        message,
        f"📋 <b>Создание задачи</b> — шаг 2/5\n\n"
        f"<b>Название:</b> {he(title)}\n\n"
        f"Введите <b>описание</b> задачи или нажмите «Пропустить»:",
        _tc_desc_kb()
    )


# ── Шаг 2: Описание ───────────────────────────────────────────────────────────

@tasks_router.message(TaskCreateStates.waiting_desc)
async def tsk_c_desc_msg(message: Message, state: FSMContext):
    desc = message.text.strip() if message.text else ""
    await state.update_data(tsk_c_desc=desc)
    await state.set_state(None)
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    db = await get_db(message.from_user.id, state)
    await fsm_edit(
        state,
        message,
        f"📋 <b>Создание задачи</b> — шаг 3/5\n\n"
        f"<b>Название:</b> {he(data.get('tsk_c_title', ''))}\n\n"
        f"Выберите <b>исполнителя</b>:",
        _tc_who_kb(db) if db else _tc_cancel_kb()
    )


@tasks_router.callback_query(F.data == "tsk_c_skip_desc")
async def tsk_c_skip_desc(callback: CallbackQuery, state: FSMContext):
    await state.update_data(tsk_c_desc='')
    await state.set_state(None)
    await callback.answer()
    data = await state.get_data()
    db = await get_db(callback.from_user.id, state)
    await callback.message.edit_text(
        f"📋 <b>Создание задачи</b> — шаг 3/5\n\n"
        f"<b>Название:</b> {he(data.get('tsk_c_title', ''))}\n\n"
        f"Выберите <b>исполнителя</b>:",
        reply_markup=_tc_who_kb(db) if db else _tc_cancel_kb(),
        parse_mode="HTML"
    )


# ── Шаг 3: Исполнитель ────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data == "tsk_c_who_all")
async def tsk_c_who_all(callback: CallbackQuery, state: FSMContext):
    await state.update_data(tsk_c_assign_type='all')
    await callback.answer()
    await callback.message.edit_text(
        "📋 <b>Создание задачи</b> — шаг 4/5\n\n"
        "<b>Исполнитель:</b> 👥 Вся команда\n\n"
        "Выберите <b>приоритет</b>:",
        reply_markup=_tc_prio_kb(),
        parse_mode="HTML"
    )


@tasks_router.callback_query(F.data == "tsk_c_who_none")
async def tsk_c_who_none(callback: CallbackQuery, state: FSMContext):
    await state.update_data(tsk_c_assign_type='none')
    await callback.answer()
    await callback.message.edit_text(
        "📋 <b>Создание задачи</b> — шаг 4/5\n\n"
        "<b>Исполнитель:</b> 📋 Без назначения\n\n"
        "Выберите <b>приоритет</b>:",
        reply_markup=_tc_prio_kb(),
        parse_mode="HTML"
    )


@tasks_router.callback_query(F.data == "tsk_c_who_users")
async def tsk_c_who_users(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    db = await get_db(callback.from_user.id, state)
    await callback.message.edit_text(
        "📋 <b>Создание задачи</b> — шаг 3/5\n\n"
        "Выберите <b>сотрудника</b>:",
        reply_markup=_tc_users_kb(db) if db else _tc_cancel_kb(),
        parse_mode="HTML"
    )


@tasks_router.callback_query(F.data.startswith("tsk_c_upg_"))
async def tsk_c_users_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[-1])
    await callback.answer()
    db = await get_db(callback.from_user.id, state)
    await callback.message.edit_text(
        "📋 <b>Создание задачи</b> — шаг 3/5\n\n"
        "Выберите <b>сотрудника</b>:",
        reply_markup=_tc_users_kb(db, page) if db else _tc_cancel_kb(),
        parse_mode="HTML"
    )


@tasks_router.callback_query(F.data == "tsk_c_back_who")
async def tsk_c_back_who(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    await callback.message.edit_text(
        f"📋 <b>Создание задачи</b> — шаг 3/5\n\n"
        f"<b>Название:</b> {he(data.get('tsk_c_title', ''))}\n\n"
        f"Выберите <b>исполнителя</b>:",
        reply_markup=_tc_who_kb(db) if db else _tc_cancel_kb(),
        parse_mode="HTML"
    )


@tasks_router.callback_query(F.data.startswith("tsk_csh_"))
async def tsk_c_shop_selected(callback: CallbackQuery, state: FSMContext):
    shop_raw = callback.data[len("tsk_csh_"):]
    db = await get_db(callback.from_user.id, state)
    shop = shop_raw
    if db:
        try:
            conn = _get_sync_db(db).get_connection()
            shops = [r[0] for r in conn.execute(
                "SELECT DISTINCT shop_name FROM users WHERE shop_name IS NOT NULL AND shop_name != ''"
            ).fetchall()]
            conn.close()
            shop = resolve_cb_name(shop_raw, shops)
        except Exception:
            pass
    await state.update_data(tsk_c_assign_type='shop', tsk_c_assign_shop=shop)
    await callback.answer()
    await callback.message.edit_text(
        f"📋 <b>Создание задачи</b> — шаг 4/5\n\n"
        f"<b>Исполнитель:</b> 🏪 {he(shop)}\n\n"
        f"Выберите <b>приоритет</b>:",
        reply_markup=_tc_prio_kb(),
        parse_mode="HTML"
    )


@tasks_router.callback_query(F.data.startswith("tsk_cu_"))
async def tsk_c_user_selected(callback: CallbackQuery, state: FSMContext):
    try:
        uid = int(callback.data[len("tsk_cu_"):])
    except ValueError:
        await callback.answer("Ошибка")
        return
    db = await get_db(callback.from_user.id, state)
    uname = f"User#{uid}"
    if db:
        try:
            conn = _get_sync_db(db).get_connection()
            row = conn.execute(
                "SELECT first_name, last_name, username FROM users WHERE id = ?", (uid,)
            ).fetchone()
            conn.close()
            if row:
                uname = f"{row[0] or ''} {row[1] or ''}".strip() or row[2] or uname
        except Exception:
            pass
    await state.update_data(tsk_c_assign_type='user', tsk_c_assign_uid=uid,
                            tsk_c_assign_uname=uname)
    await callback.answer()
    await callback.message.edit_text(
        f"📋 <b>Создание задачи</b> — шаг 4/5\n\n"
        f"<b>Исполнитель:</b> 👤 {he(uname)}\n\n"
        f"Выберите <b>приоритет</b>:",
        reply_markup=_tc_prio_kb(),
        parse_mode="HTML"
    )


# ── Шаг 4: Приоритет ──────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_c_prio_"))
async def tsk_c_prio_selected(callback: CallbackQuery, state: FSMContext):
    priority = callback.data[len("tsk_c_prio_"):]
    if priority not in ('low', 'normal', 'high', 'urgent'):
        await callback.answer("Ошибка")
        return
    await state.update_data(tsk_c_priority=priority)
    await state.set_state(TaskCreateStates.waiting_deadline)
    await callback.answer()
    prio_map = {'low': '🟢 Низкий', 'normal': '🔵 Обычный',
                'high': '🟡 Высокий', 'urgent': '🔴 Срочно'}
    await callback.message.edit_text(
        f"📋 <b>Создание задачи</b> — шаг 5/5\n\n"
        f"<b>Приоритет:</b> {prio_map.get(priority, priority)}\n\n"
        f"Введите <b>срок выполнения</b> или нажмите «Без дедлайна».\n"
        f"<i>Форматы: 25.06 · 25.06.2026 · 25.06 14:00 · 25.06.2026 14:00</i>",
        reply_markup=_tc_dl_kb(),
        parse_mode="HTML"
    )


# ── Шаг 5: Дедлайн ────────────────────────────────────────────────────────────

@tasks_router.message(TaskCreateStates.waiting_deadline)
async def tsk_c_dl_msg(message: Message, state: FSMContext):
    text = message.text.strip() if message.text else ""
    deadline = _tc_parse_deadline(text) if text else None
    try:
        await message.delete()
    except Exception:
        pass
    if text and not deadline:
        await fsm_edit(
            state,
            message,
            "⚠️ Не распознан формат даты.\n\n"
            "Используйте: <code>25.06</code> · <code>25.06.2026</code> · "
            "<code>25.06 14:00</code>\n\n"
            "Попробуйте ещё раз или нажмите «Без дедлайна»:",
            _tc_dl_kb()
        )
        return
    await state.update_data(tsk_c_deadline=deadline or '')
    await state.set_state(None)
    data = await state.get_data()
    dl_display = ""
    if deadline:
        try:
            from datetime import datetime as _dt, date as _date
            has_time = "T" in deadline
            if has_time:
                dl_display = _dt.fromisoformat(deadline).strftime("%d.%m.%Y %H:%M")
            else:
                dl_display = _date.fromisoformat(deadline).strftime("%d.%m.%Y")
        except Exception:
            dl_display = deadline
    data['tsk_c_deadline'] = dl_display or ''
    await fsm_edit(state, message, _tc_summary(data), _tc_confirm_kb())


@tasks_router.callback_query(F.data == "tsk_c_skip_dl")
async def tsk_c_skip_dl(callback: CallbackQuery, state: FSMContext):
    await state.update_data(tsk_c_deadline='')
    await state.set_state(None)
    await callback.answer()
    data = await state.get_data()
    await callback.message.edit_text(
        _tc_summary(data),
        reply_markup=_tc_confirm_kb(),
        parse_mode="HTML"
    )


# ── Шаг 6: Подтверждение и создание ──────────────────────────────────────────

@tasks_router.callback_query(F.data == "tsk_c_ok")
async def tsk_c_ok(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    admin = is_any_admin(tg_id)
    if not admin:
        await callback.answer("Нет доступа", show_alert=True)
        return
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org", show_alert=True)
        return

    data = await state.get_data()
    title    = data.get('tsk_c_title', '').strip()
    desc     = data.get('tsk_c_desc', '').strip()
    atype    = data.get('tsk_c_assign_type', 'none')
    priority = data.get('tsk_c_priority', 'normal')
    deadline_raw = data.get('tsk_c_deadline', '')

    if not title:
        await callback.answer("Ошибка: название не заполнено", show_alert=True)
        return

    # Восстановить ISO-дедлайн из отформатированной строки
    deadline_iso = None
    if deadline_raw:
        try:
            from datetime import datetime as _dt, date as _date
            for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    parsed = _dt.strptime(deadline_raw, fmt)
                    deadline_iso = (
                        parsed.strftime("%Y-%m-%dT%H:%M") if "%H" in fmt
                        else parsed.date().isoformat()
                    )
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    assigned_to   = None
    assigned_shop = None
    assign_all    = 0
    if atype == 'all':
        assign_all = 1
    elif atype == 'shop':
        assigned_shop = data.get('tsk_c_assign_shop')
    elif atype == 'user':
        assigned_to = data.get('tsk_c_assign_uid')

    try:
        user = await db.get_user(tg_id)
        created_by = user[0] if user else 0
    except Exception:
        created_by = 0

    try:
        task_id = await db.create_task(
            title=title,
            description=desc,
            created_by=created_by,
            assigned_to=assigned_to,
            assigned_shop=assigned_shop,
            assign_all=assign_all,
            priority=priority,
            deadline=deadline_iso,
        )
    except Exception as e:
        logger.error("tsk_c_ok create_task: %s", e)
        await callback.answer("⚠️ Ошибка создания задачи", show_alert=True)
        return

    # Уведомить исполнителей
    try:
        notify_text = (
            f"📋 <b>Новая задача</b>\n\n"
            f"<b>{he(title)}</b>\n"
            f"{PRIORITY_LABELS.get(priority, priority)}\n"
            + (f"📅 Срок: {deadline_raw}" if deadline_raw else "")
        )
        _sdb_notif = _get_sync_db(db)
        if assign_all:
            conn_all = _sdb_notif.get_connection()
            members = [(r[0], r[1]) for r in conn_all.execute(
                "SELECT id, telegram_id FROM users WHERE telegram_id IS NOT NULL"
            ).fetchall()]
            conn_all.close()
        elif assigned_shop:
            conn3 = _sdb_notif.get_connection()
            members = [(r[0], r[1]) for r in conn3.execute(
                "SELECT id, telegram_id FROM users "
                "WHERE shop_name = ? AND telegram_id IS NOT NULL", (assigned_shop,)
            ).fetchall()]
            conn3.close()
        elif assigned_to:
            conn4 = _sdb_notif.get_connection()
            row4 = conn4.execute(
                "SELECT id, telegram_id FROM users WHERE id = ?", (assigned_to,)
            ).fetchone()
            conn4.close()
            members = [(row4[0], row4[1])] if row4 and row4[1] else []
        else:
            members = []

        for _uid, _tgid in members:
            if _tgid and _tgid != tg_id:
                try:
                    await callback.bot.send_message(
                        _tgid, notify_text, parse_mode="HTML",
                        reply_markup=_add_read_btn_tasks()
                    )
                except Exception:
                    pass
                try:
                    await db.add_notification_to_history(
                        _uid, 'task_assigned', f"📋 Новая задача: {title}"
                    )
                except Exception:
                    pass
    except Exception as _ne:
        logger.warning("tsk_c_ok notify: %s", _ne)

    # Automation rules: task created / assigned
    try:
        from task_automation import run_rules as _run_rules
        _new_task = await db.get_task(task_id) if task_id else None
        if _new_task:
            _run_rules(db, 'task_created', dict(_new_task), created_by)
            if _new_task.get('assigned_to') or _new_task.get('assigned_shop') or _new_task.get('assign_all'):
                _run_rules(db, 'task_assigned', dict(_new_task), created_by)
    except Exception as _are:
        logger.warning("tsk_c_ok automation: %s", _are)

    await clear_state_keep_org(state)
    await callback.answer("✅ Задача создана!")
    await callback.message.edit_text(
        f"✅ <b>Задача создана!</b>\n\n<b>{he(title)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 К списку задач", callback_data="tsk_list_0")
        ]])
    )


# ── Отмена визарда ────────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data == "tsk_c_cancel")
async def tsk_c_cancel(callback: CallbackQuery, state: FSMContext):
    await clear_state_keep_org(state)
    await callback.answer("Отменено")
    await _show_tasks_list(callback, state, page=0)


# ═══════════════════════════════════════════════════════════════════════════════
# ── Phase 3.4: AI-создание задачи (бот) ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _ai_tc_cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_ai_cancel"))
    return kb.as_markup()


def _ai_tc_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Создать", callback_data="tsk_ai_ok"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="tsk_ai_cancel"),
    )
    return kb.as_markup()


@tasks_router.callback_query(F.data == "tsk_ai_create")
async def tsk_ai_create_entry(callback: CallbackQuery, state: FSMContext):
    """Вход в AI-визард создания задачи."""
    tg_id = callback.from_user.id
    admin = is_any_admin(tg_id)
    if not admin:
        await callback.answer("Нет доступа", show_alert=True)
        return
    # Billing gate: tasks_ai extension required
    try:
        from billing_utils import has_module as _hm, has_extension as _he
        if not _hm(tg_id, 'tasks_pro') or not _he(tg_id, 'tasks_ai'):
            await callback.answer("Требуется расширение «AI для задач».", show_alert=True)
            return
    except Exception:
        await callback.answer("Расширение «AI для задач» недоступно.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(AiTaskCreateStates.waiting_goal)
    await callback.message.edit_text(
        "✨ <b>AI-создание задачи</b>\n\n"
        "Опишите цель или задачу в свободной форме — AI сформирует готовую задачу.\n\n"
        "<i>Пример: «Провести инвентаризацию склада и обновить остатки в системе»</i>",
        reply_markup=_ai_tc_cancel_kb(),
        parse_mode="HTML",
    )


@tasks_router.message(AiTaskCreateStates.waiting_goal)
async def tsk_ai_goal_msg(message: Message, state: FSMContext):
    """Получаем цель и генерируем задачу через AI."""
    goal = message.text.strip() if message.text else ""
    try:
        await message.delete()
    except Exception:
        pass
    if not goal:
        await fsm_edit(state, message, "⚠️ Введите описание задачи или нажмите «Отменить»:",
                       _ai_tc_cancel_kb())
        return

    await fsm_edit(state, message, "⏳ AI генерирует задачу…", _ai_tc_cancel_kb())

    try:
        import aiohttp as _ahttp
        import json as _json

        # Попытка вызвать LLM через web.ai_utils
        from web.ai_utils import ask_llm, is_configured
        if not is_configured():
            await fsm_edit(state, message, "⚠️ AI не настроен. Используйте обычное создание задачи.",
                           _ai_tc_cancel_kb())
            await clear_state_keep_org(state)
            return

        prompt = (
            f"Задача: {goal}\n\n"
            "Сформируй JSON-объект задачи для розничного магазина: "
            "{\"title\": \"...\", \"description\": \"...\", \"priority\": \"normal|high|urgent|low\"}. "
            "title — до 80 символов, конкретное и ёмкое название. "
            "description — 2-3 предложения, что нужно сделать. "
            "Только JSON без пояснений."
        )
        system = (
            "Ты — менеджер розничного магазина. "
            "Возвращай ТОЛЬКО валидный JSON. Пиши по-русски."
        )
        result = await ask_llm(prompt, system=system, max_tokens=200, temperature=0.3,
                               feature="bot_task_create")
        if not result:
            await fsm_edit(state, message, "⚠️ AI не ответил. Попробуйте позже.",
                           _ai_tc_cancel_kb())
            await clear_state_keep_org(state)
            return

        import re as _re
        json_match = _re.search(r'\{.*\}', result, _re.DOTALL)
        if not json_match:
            raise ValueError("no JSON")
        task_data = _json.loads(json_match.group())
        title = str(task_data.get('title', goal))[:200].strip()
        description = str(task_data.get('description', ''))[:1000].strip()
        priority = task_data.get('priority', 'normal')
        if priority not in ('low', 'normal', 'high', 'urgent'):
            priority = 'normal'

        prio_map = {'low': '🟢 Низкий', 'normal': '🔵 Обычный',
                    'high': '🟡 Высокий', 'urgent': '🔴 Срочно'}

        await state.update_data(ai_title=title, ai_desc=description, ai_prio=priority)
        await state.set_state(AiTaskCreateStates.confirming)
        await fsm_edit(
            state,
            message,
            f"✨ <b>AI создал задачу — подтвердите:</b>\n\n"
            f"<b>Название:</b> {he(title)}\n"
            f"<b>Описание:</b> {he(description) or '<i>нет</i>'}\n"
            f"<b>Приоритет:</b> {prio_map.get(priority, priority)}",
            _ai_tc_confirm_kb(),
        )
    except Exception as e:
        logger.warning("tsk_ai_goal_msg AI error: %s", e)
        await fsm_edit(state, message, "⚠️ Ошибка AI. Попробуйте ещё раз или отмените.",
                       _ai_tc_cancel_kb())
        await clear_state_keep_org(state)


@tasks_router.callback_query(F.data == "tsk_ai_ok")
async def tsk_ai_ok(callback: CallbackQuery, state: FSMContext):
    """Создаём задачу из AI-результата."""
    data = await state.get_data()
    title = data.get('ai_title', '')
    description = data.get('ai_desc', '')
    priority = data.get('ai_prio', 'normal')
    org_db = data.get('selected_org_db')

    if not title or not org_db:
        await callback.answer("Ошибка данных.", show_alert=True)
        await clear_state_keep_org(state)
        return

    try:
        db = await get_db(callback.from_user.id, state)
        if db is None:
            await callback.answer("Нет активной org.", show_alert=True)
            await clear_state_keep_org(state)
            return
        user = await db.get_user(callback.from_user.id)
        my_db_id = user[0] if user else 0

        task_id = await db.create_task(
            title=title,
            description=description,
            created_by=my_db_id,
            priority=priority,
        )
        # Automation rules: task created
        try:
            from task_automation import run_rules as _run_rules
            _new_task = await db.get_task(task_id) if task_id else None
            if _new_task:
                _run_rules(db, 'task_created', dict(_new_task), my_db_id)
        except Exception as _are:
            logger.warning("tsk_ai_ok automation: %s", _are)
        await clear_state_keep_org(state)
        await callback.answer("✅ Задача создана!")
        await callback.message.edit_text(
            f"✅ <b>Задача создана!</b>\n\n<b>{he(title)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 К списку задач", callback_data="tsk_list_0")
            ]])
        )
    except Exception as e:
        logger.error("tsk_ai_ok create: %s", e)
        await callback.answer("Ошибка создания задачи.", show_alert=True)
        await clear_state_keep_org(state)


@tasks_router.callback_query(F.data == "tsk_ai_cancel")
async def tsk_ai_cancel(callback: CallbackQuery, state: FSMContext):
    await clear_state_keep_org(state)
    await callback.answer("Отменено")
    await _show_tasks_list(callback, state, page=0)


# ══════════════════════════════════════════════════════════════════════════════
# РЕДАКТИРОВАНИЕ ЗАДАЧИ (admin или создатель) — FSM-wizard по одному полю
# ══════════════════════════════════════════════════════════════════════════════

def _can_edit_task(task: dict, my_db_id: int | None, is_admin: bool) -> bool:
    """Может ли пользователь редактировать задачу (admin или создатель)."""
    if is_admin:
        return True
    return bool(my_db_id and task.get('created_by') == my_db_id)


def _can_view_task(task: dict, my_db_id: int | None, is_admin: bool,
                   my_shop: str | None = None) -> bool:
    """Может ли пользователь видеть задачу и её комментарии."""
    if is_admin:
        return True
    if not my_db_id:
        return False
    return (
        task.get('created_by') == my_db_id
        or task.get('assigned_to') == my_db_id
        or bool(task.get('assign_all'))
        or bool(task.get('assigned_shop') and my_shop
                and task.get('assigned_shop') == my_shop)
    )


def _build_task_view_text(task: dict, subtasks: list | None = None) -> str:
    """Собрать HTML-текст детального вида задачи (переиспользуется после сохранения)."""
    from datetime import datetime as _dt2, date as _date2
    title    = task.get('title', '—')
    desc     = task.get('description', '')
    status   = STATUS_LABELS.get(task.get('status', ''), task.get('status', ''))
    priority = PRIORITY_LABELS.get(task.get('priority', ''), task.get('priority', ''))
    deadline = task.get('deadline', '')
    dl_str   = ""
    if deadline:
        try:
            has_time = len(deadline) >= 13 and ("T" in deadline or " " in deadline[10:])
            if has_time:
                dl_dt2 = _dt2.fromisoformat(deadline[:16].replace("T", " "))
                overdue = dl_dt2 < _dt2.now() and task.get('status') not in ('done', 'cancelled')
                dl_str = f"\n📅 Срок: {dl_dt2.strftime('%d.%m.%Y %H:%M')}"
            else:
                d2 = _date2.fromisoformat(deadline[:10])
                overdue = d2 < _date2.today() and task.get('status') not in ('done', 'cancelled')
                dl_str = f"\n📅 Срок: {d2.strftime('%d.%m.%Y')}"
            if overdue:
                dl_str += " ⚠️ Просрочена"
        except Exception:
            dl_str = f"\n📅 Срок: {deadline}"
    assigned_name = task.get('assigned_name', '')
    creator_name  = task.get('creator_name', '')
    topic_name    = task.get('topic_name', '')
    assigned_shop = task.get('assigned_shop', '')
    assign_all    = task.get('assign_all', False)
    recurrence    = task.get('recurrence') or ''
    checklist     = task.get('checklist', [])
    text = f"📋 <b>{he(title)}</b>\n{status} · {priority}\n"
    if topic_name:
        text += f"🏷 {he(topic_name)}\n"
    if assign_all:
        text += "👥 Исполнитель: Вся команда\n"
    elif assigned_shop:
        text += f"🏪 Магазин: {he(assigned_shop)}\n"
    elif assigned_name:
        text += f"👤 Исполнитель: {he(assigned_name)}\n"
    if creator_name:
        text += f"✍️ Автор: {he(creator_name)}\n"
    if recurrence and recurrence not in ('none', ''):
        text += f"🔁 {RECURRENCE_LABELS.get(recurrence, recurrence)}\n"
    text += dl_str
    if desc:
        text += f"\n\n{he(desc)}"
    if checklist:
        cl_done2 = sum(1 for i in checklist if i.get('is_done'))
        cl_total2 = len(checklist)
        lines2 = []
        for item in checklist:
            mark2 = "✅" if item.get('is_done') else "☐"
            lines2.append(f"  {mark2} {he(item.get('text', ''))}")
        text += f"\n\nЧеклист ({cl_done2}/{cl_total2}):\n" + "\n".join(lines2)
    # ── Подзадачи ──────────────────────────────────────────────────────────────
    if subtasks:
        _st_done = sum(1 for s in subtasks if s.get('status') == 'done')
        _st_total = len(subtasks)
        text += f"\n\n📎 <b>Подзадачи ({_st_total})</b>: {_st_done} выполнено\n"
        _ST_ICON = {'new': '🆕', 'in_progress': '▶️', 'done': '✅', 'cancelled': '🚫'}
        for _s in subtasks[:5]:
            _ico = _ST_ICON.get(_s.get('status', 'new'), '•')
            text += f"  {_ico} {he(_s.get('title', '—'))}\n"
        if _st_total > 5:
            text += f"  <i>+ ещё {_st_total - 5}</i>\n"
    # ── Оценка ─────────────────────────────────────────────────────────────────
    _rating = task.get('rating')
    if _rating:
        _rc = task.get('rating_comment', '') or ''
        text += f"\n\n⭐ <b>Оценка: {'⭐' * _rating} ({_rating}/5)</b>"
        if _rc:
            text += f"\n<i>{he(_rc)}</i>"
    return text


async def _show_task_after_edit(target, state: FSMContext, task_id: int,
                                tg_id: int, is_msg: bool = False):
    """Показать обновлённый детальный вид задачи после редактирования."""
    try:
        db = await get_db(tg_id, state)
        if db is None:
            return
        user = await db.get_user(tg_id)
        my_db_id2 = user[0] if user else 0
        my_shop2  = user[8] if user and len(user) > 8 else None
        admin2    = is_any_admin(tg_id)
        task = await db.get_task(task_id)
        if not task:
            return
        try:
            _subs = await db.get_subtasks(task_id)
        except Exception:
            _subs = None
        text = _build_task_view_text(task, subtasks=_subs)
        try:
            _cc = await db.get_task_comment_count(task_id)
        except Exception:
            _cc = 0
        kb   = _task_detail_keyboard(task, my_db_id2, admin2, my_shop=my_shop2,
                                     comment_count=_cc,
                                     has_subtasks=bool(_subs))
        if is_msg:
            await target.answer(text, parse_mode="HTML", reply_markup=kb)
        else:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as _sae:
        logger.error("_show_task_after_edit: %s", _sae)

def _task_edit_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Меню выбора поля для редактирования задачи."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✏️ Название",   callback_data=f"tsk_ed_t_{task_id}"),
        InlineKeyboardButton(text="📝 Описание",   callback_data=f"tsk_ed_d_{task_id}"),
    )
    kb.row(
        InlineKeyboardButton(text="🎯 Приоритет",  callback_data=f"tsk_ed_p_{task_id}"),
        InlineKeyboardButton(text="📅 Дедлайн",    callback_data=f"tsk_ed_l_{task_id}"),
    )
    kb.row(InlineKeyboardButton(text="👤 Исполнитель", callback_data=f"tsk_ed_w_{task_id}"))
    kb.row(back_button(f"tsk_view_{task_id}", "⬅️ К задаче"))
    return kb.as_markup()


def _te_prio_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🟢 Низкий",  callback_data=f"tsk_ep_{task_id}_low"),
        InlineKeyboardButton(text="🔵 Обычный", callback_data=f"tsk_ep_{task_id}_normal"),
    )
    kb.row(
        InlineKeyboardButton(text="🟡 Высокий", callback_data=f"tsk_ep_{task_id}_high"),
        InlineKeyboardButton(text="🔴 Срочно",  callback_data=f"tsk_ep_{task_id}_urgent"),
    )
    kb.row(back_button(f"tsk_edit_{task_id}", "⬅️ Назад"))
    return kb.as_markup()


def _te_who_kb(task_id: int, db) -> InlineKeyboardMarkup:
    """Клавиатура выбора исполнителя для редактирования (task_id в FSM)."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="👥 Всей команде", callback_data="tsk_ew_a"))
    try:
        conn = _get_sync_db(db).get_connection()
        shops = conn.execute(
            "SELECT DISTINCT shop_name FROM users "
            "WHERE shop_name IS NOT NULL AND shop_name != '' ORDER BY shop_name"
        ).fetchall()
        conn.close()
        for (sh,) in shops[:6]:
            kb.row(InlineKeyboardButton(
                text=f"🏪 {sh}",
                callback_data=safe_cb("tsk_ews_", sh)
            ))
    except Exception:
        pass
    kb.row(InlineKeyboardButton(text="👤 Конкретный сотрудник", callback_data="tsk_ew_u"))
    kb.row(InlineKeyboardButton(text="📋 Без назначения",        callback_data="tsk_ew_n"))
    kb.row(back_button(f"tsk_edit_{task_id}", "⬅️ Назад"))
    return kb.as_markup()


def _te_users_kb(task_id: int, db, page: int = 0) -> InlineKeyboardMarkup:
    """Список сотрудников для назначения при редактировании."""
    kb = InlineKeyboardBuilder()
    try:
        rows = _get_sync_db(db).get_all_users()
        PAGE = 8
        start = page * PAGE
        chunk = rows[start:start + PAGE]
        for r in chunk:
            uid = r[0]
            name = f"{r[2] or ''} {r[3] or ''}".strip() or (r[12] if len(r) > 12 else None) or f"User#{uid}"
            shop = f" ({r[8]})" if len(r) > 8 and r[8] else ""
            kb.row(InlineKeyboardButton(
                text=f"👤 {name}{shop}",
                callback_data=f"tsk_eu2_{uid}"
            ))
        _tp = max(1, -(-len(rows) // PAGE))
        nav = page_nav_row("tsk_eup_", page, page > 0, start + PAGE < len(rows), _tp)
        if nav:
            kb.row(*nav)
    except Exception:
        pass
    kb.row(back_button(f"tsk_ed_w_{task_id}", "⬅️ Назад"))
    return kb.as_markup()


async def _te_get_my_db_id(db, tg_id: int) -> int | None:
    """Внутренний id пользователя в org-БД."""
    try:
        conn = _get_sync_db(db).get_connection()
        row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


async def _te_notify_assignee(db, task: dict, bot, editor_tg: int,
                               field_ru: str, new_val_str: str):
    """Уведомить исполнителя задачи об изменении поля."""
    assigned_to = task.get('assigned_to')
    if not assigned_to:
        return
    try:
        conn = _get_sync_db(db).get_connection()
        row = conn.execute("SELECT telegram_id FROM users WHERE id = ?", (assigned_to,)).fetchone()
        conn.close()
        if not row or row[0] == editor_tg:
            return
        await bot.send_message(
            row[0],
            f"✏️ <b>Задача изменена</b>\n\n"
            f"<b>{he(task['title'])}</b>\n"
            f"{field_ru}: {he(new_val_str)}",
            parse_mode="HTML",
            reply_markup=_add_read_btn_tasks()
        )
    except Exception:
        pass


def _te_done_kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 К задаче", callback_data=f"tsk_view_{task_id}")
    ]])


# ── Точка входа: показать меню редактирования ────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_edit_"))
async def tsk_edit_entry_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    my_db_id = await _te_get_my_db_id(db, tg_id)
    if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"✏️ <b>Редактирование задачи</b>\n\n"
        f"<b>{he(task['title'])}</b>\n\n"
        "Выберите поле для изменения:",
        parse_mode="HTML",
        reply_markup=_task_edit_keyboard(task_id)
    )


# ── Редактирование: НАЗВАНИЕ ──────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_ed_t_"))
async def tsk_ed_title_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    my_db_id = await _te_get_my_db_id(db, tg_id)
    if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.update_data(tsk_edit_task_id=task_id)
    await state.set_state(TaskEditStates.waiting_title)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data=f"tsk_edit_{task_id}"))
    await callback.answer()
    await callback.message.edit_text(
        "✏️ <b>Новое название задачи:</b>\n"
        "<i>Введите название (до 200 символов)</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


@tasks_router.message(TaskEditStates.waiting_title)
async def tsk_ed_title_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await clear_state_keep_org(state)
        return
    new_title = (message.text or "").strip()[:200]
    if not new_title:
        await message.answer("⚠️ Название не может быть пустым.")
        return
    tg_id = message.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await clear_state_keep_org(state)
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await message.answer("Задача не найдена.")
            await clear_state_keep_org(state)
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await message.answer("⚠️ Нет доступа к редактированию.")
            await clear_state_keep_org(state)
            return
        old_title = task['title']
        await db.update_task(
            task_id, new_title, task.get('description', ''),
            task.get('topic_id'), task.get('assigned_to'), task.get('shop_id'),
            task.get('priority', 'normal'), task.get('deadline'),
            task.get('assigned_shop') or '', int(task.get('assign_all', False)),
            task.get('recurrence') or 'none'
        )
        try:
            await db.add_task_history(task_id, my_db_id, 'edit_title', old_title, new_title)
        except Exception:
            pass
        await clear_state_keep_org(state)
        await _show_task_after_edit(message, state, task_id, tg_id, is_msg=True)
    except Exception as e:
        logger.error("tsk_ed_title_msg: %s", e)
        await message.answer("⚠️ Ошибка сохранения.")
        await clear_state_keep_org(state)


# ── Редактирование: ОПИСАНИЕ ──────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_ed_d_"))
async def tsk_ed_desc_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    my_db_id = await _te_get_my_db_id(db, tg_id)
    if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.update_data(tsk_edit_task_id=task_id)
    await state.set_state(TaskEditStates.waiting_desc)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🗑 Очистить описание", callback_data=f"tsk_ed_dc_{task_id}"))
    kb.row(InlineKeyboardButton(text="❌ Отменить",          callback_data=f"tsk_edit_{task_id}"))
    await callback.answer()
    await callback.message.edit_text(
        "📝 <b>Новое описание задачи:</b>\n"
        "<i>Введите текст или нажмите «Очистить описание»</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


@tasks_router.callback_query(F.data.startswith("tsk_ed_dc_"))
async def tsk_ed_desc_clear_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    await clear_state_keep_org(state)
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await db.update_task(
            task_id, task['title'], '',
            task.get('topic_id'), task.get('assigned_to'), task.get('shop_id'),
            task.get('priority', 'normal'), task.get('deadline'),
            task.get('assigned_shop') or '', int(task.get('assign_all', False)),
            task.get('recurrence') or 'none'
        )
        try:
            await db.add_task_history(task_id, my_db_id, 'edit_desc', task.get('description', ''), '')
        except Exception:
            pass
        await callback.answer("✅ Описание очищено")
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_ed_desc_clear_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.message(TaskEditStates.waiting_desc)
async def tsk_ed_desc_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await clear_state_keep_org(state)
        return
    new_desc = (message.text or "").strip()[:2000]
    tg_id = message.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await clear_state_keep_org(state)
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await message.answer("Задача не найдена.")
            await clear_state_keep_org(state)
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await message.answer("⚠️ Нет доступа к редактированию.")
            await clear_state_keep_org(state)
            return
        await db.update_task(
            task_id, task['title'], new_desc,
            task.get('topic_id'), task.get('assigned_to'), task.get('shop_id'),
            task.get('priority', 'normal'), task.get('deadline'),
            task.get('assigned_shop') or '', int(task.get('assign_all', False)),
            task.get('recurrence') or 'none'
        )
        try:
            await db.add_task_history(task_id, my_db_id, 'edit_desc', task.get('description', ''), new_desc)
        except Exception:
            pass
        await clear_state_keep_org(state)
        await _show_task_after_edit(message, state, task_id, tg_id, is_msg=True)
    except Exception as e:
        logger.error("tsk_ed_desc_msg: %s", e)
        await message.answer("⚠️ Ошибка сохранения.")
        await clear_state_keep_org(state)


# ── Редактирование: ПРИОРИТЕТ ─────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_ed_p_"))
async def tsk_ed_prio_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    my_db_id = await _te_get_my_db_id(db, tg_id)
    if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "🎯 <b>Выберите новый приоритет:</b>",
        parse_mode="HTML",
        reply_markup=_te_prio_kb(task_id)
    )


@tasks_router.callback_query(F.data.startswith("tsk_ep_"))
async def tsk_ep_cb(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")  # ['tsk', 'ep', '{id}', '{prio}']
    if len(parts) < 4:
        await callback.answer("Ошибка данных")
        return
    try:
        task_id = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка данных")
        return
    new_prio = parts[3]
    if new_prio not in PRIORITY_LABELS:
        await callback.answer("Неверный приоритет")
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await callback.answer("Нет доступа", show_alert=True)
            return
        old_prio = task.get('priority', 'normal')
        await db.update_task(
            task_id, task['title'], task.get('description', ''),
            task.get('topic_id'), task.get('assigned_to'), task.get('shop_id'),
            new_prio, task.get('deadline'),
            task.get('assigned_shop') or '', int(task.get('assign_all', False)),
            task.get('recurrence') or 'none'
        )
        try:
            await db.add_task_history(task_id, my_db_id, 'edit_priority', old_prio, new_prio)
        except Exception:
            pass
        prio_label = PRIORITY_LABELS.get(new_prio, new_prio)
        await _te_notify_assignee(db, task, callback.bot, tg_id, "Приоритет", prio_label)
        await callback.answer(f"✅ {prio_label}")
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_ep_cb: %s", e)
        await callback.answer("Ошибка")


# ── Редактирование: ДЕДЛАЙН ───────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_ed_l_"))
async def tsk_ed_dl_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    my_db_id = await _te_get_my_db_id(db, tg_id)
    if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.update_data(tsk_edit_task_id=task_id)
    await state.set_state(TaskEditStates.waiting_deadline)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🗑 Убрать дедлайн", callback_data=f"tsk_ed_lc_{task_id}"))
    kb.row(InlineKeyboardButton(text="❌ Отменить",       callback_data=f"tsk_edit_{task_id}"))
    await callback.answer()
    await callback.message.edit_text(
        "📅 <b>Новый дедлайн:</b>\n"
        "<i>Форматы: 25.06 / 25.06.2026 / 25.06 14:00 / 25.06.2026 14:00</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


@tasks_router.callback_query(F.data.startswith("tsk_ed_lc_"))
async def tsk_ed_dl_clear_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    await clear_state_keep_org(state)
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await db.update_task(
            task_id, task['title'], task.get('description', ''),
            task.get('topic_id'), task.get('assigned_to'), task.get('shop_id'),
            task.get('priority', 'normal'), None,
            task.get('assigned_shop') or '', int(task.get('assign_all', False)),
            task.get('recurrence') or 'none'
        )
        try:
            await db.add_task_history(task_id, my_db_id, 'edit_deadline', task.get('deadline') or '', '')
        except Exception:
            pass
        await _te_notify_assignee(db, task, callback.bot, tg_id, "Дедлайн", "убран")
        await callback.answer("✅ Дедлайн убран")
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_ed_dl_clear_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.message(TaskEditStates.waiting_deadline)
async def tsk_ed_dl_msg(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await clear_state_keep_org(state)
        return
    dl_str = _tc_parse_deadline(message.text or "")
    if dl_str is None:
        await message.answer(
            "⚠️ Не могу разобрать дату.\n"
            "Форматы: <code>25.06</code> / <code>25.06.2026</code> / <code>25.06 14:00</code>",
            parse_mode="HTML"
        )
        return
    tg_id = message.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await clear_state_keep_org(state)
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await message.answer("Задача не найдена.")
            await clear_state_keep_org(state)
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await message.answer("⚠️ Нет доступа к редактированию.")
            await clear_state_keep_org(state)
            return
        old_dl = task.get('deadline') or ''
        await db.update_task(
            task_id, task['title'], task.get('description', ''),
            task.get('topic_id'), task.get('assigned_to'), task.get('shop_id'),
            task.get('priority', 'normal'), dl_str,
            task.get('assigned_shop') or '', int(task.get('assign_all', False)),
            task.get('recurrence') or 'none'
        )
        try:
            await db.add_task_history(task_id, my_db_id, 'edit_deadline', old_dl, dl_str)
        except Exception:
            pass
        await _te_notify_assignee(db, task, bot, tg_id, "Новый дедлайн", dl_str)
        await clear_state_keep_org(state)
        await _show_task_after_edit(message, state, task_id, tg_id, is_msg=True)
    except Exception as e:
        logger.error("tsk_ed_dl_msg: %s", e)
        await message.answer("⚠️ Ошибка сохранения.")
        await clear_state_keep_org(state)


# ── Редактирование: ИСПОЛНИТЕЛЬ ───────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_ed_w_"))
async def tsk_ed_who_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    my_db_id = await _te_get_my_db_id(db, tg_id)
    if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.update_data(tsk_edit_task_id=task_id)
    await callback.answer()
    await callback.message.edit_text(
        "👤 <b>Изменить исполнителя:</b>",
        parse_mode="HTML",
        reply_markup=_te_who_kb(task_id, db)
    )


@tasks_router.callback_query(F.data == "tsk_ew_a")
async def tsk_ew_all_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await callback.answer("Ошибка сессии: перейдите к задаче заново", show_alert=True)
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await db.update_task(
            task_id, task['title'], task.get('description', ''),
            task.get('topic_id'), None, None,
            task.get('priority', 'normal'), task.get('deadline'),
            None, 1, task.get('recurrence') or 'none'
        )
        try:
            old = task.get('assigned_name') or task.get('assigned_shop') or '—'
            await db.add_task_history(task_id, my_db_id, 'edit_assignee', old, 'Вся команда')
        except Exception:
            pass
        await callback.answer("✅ Назначено всей команде")
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_ew_all_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.callback_query(F.data == "tsk_ew_n")
async def tsk_ew_none_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await callback.answer("Ошибка сессии: перейдите к задаче заново", show_alert=True)
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await db.update_task(
            task_id, task['title'], task.get('description', ''),
            task.get('topic_id'), None, None,
            task.get('priority', 'normal'), task.get('deadline'),
            None, 0, task.get('recurrence') or 'none'
        )
        try:
            old = task.get('assigned_name') or task.get('assigned_shop') or '—'
            await db.add_task_history(task_id, my_db_id, 'edit_assignee', old, 'Без назначения')
        except Exception:
            pass
        await callback.answer("✅ Назначение снято")
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_ew_none_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.callback_query(F.data == "tsk_ew_u")
async def tsk_ew_users_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await callback.answer("Ошибка сессии", show_alert=True)
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    await callback.answer()
    await callback.message.edit_text(
        "👤 <b>Выберите исполнителя:</b>",
        parse_mode="HTML",
        reply_markup=_te_users_kb(task_id, db, page=0)
    )


@tasks_router.callback_query(F.data.startswith("tsk_eup_"))
async def tsk_eup_cb(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[-1])
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await callback.answer("Ошибка сессии")
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=_te_users_kb(task_id, db, page=page))


@tasks_router.callback_query(F.data.startswith("tsk_eu2_"))
async def tsk_eu2_cb(callback: CallbackQuery, state: FSMContext):
    """Выбрать конкретного сотрудника как исполнителя (edit)."""
    uid = int(callback.data.split("_")[-1])
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await callback.answer("Ошибка сессии: перейдите к задаче заново", show_alert=True)
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await callback.answer("Нет доступа", show_alert=True)
            return
        conn = _get_sync_db(db).get_connection()
        row = conn.execute(
            "SELECT first_name, last_name, username, telegram_id FROM users WHERE id = ?", (uid,)
        ).fetchone()
        conn.close()
        if row:
            assignee_name = f"{row[0] or ''} {row[1] or ''}".strip() or row[2] or f"User#{uid}"
            assignee_tg = row[3]
        else:
            assignee_name = f"User#{uid}"
            assignee_tg = None
        await db.update_task(
            task_id, task['title'], task.get('description', ''),
            task.get('topic_id'), uid, None,
            task.get('priority', 'normal'), task.get('deadline'),
            None, 0, task.get('recurrence') or 'none'
        )
        try:
            old = task.get('assigned_name') or task.get('assigned_shop') or '—'
            await db.add_task_history(task_id, my_db_id, 'edit_assignee', old, assignee_name)
        except Exception:
            pass
        if assignee_tg and assignee_tg != tg_id:
            try:
                await callback.bot.send_message(
                    assignee_tg,
                    f"👤 <b>Вам назначена задача</b>\n\n<b>{he(task['title'])}</b>",
                    parse_mode="HTML",
                    reply_markup=_add_read_btn_tasks()
                )
            except Exception:
                pass
        await callback.answer(f"✅ {assignee_name}")
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_eu2_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.callback_query(F.data.startswith("tsk_ews_"))
async def tsk_ews_cb(callback: CallbackQuery, state: FSMContext):
    """Назначить задачу магазину (edit)."""
    raw_suffix = callback.data[len("tsk_ews_"):]
    data = await state.get_data()
    task_id = data.get('tsk_edit_task_id')
    if not task_id:
        await callback.answer("Ошибка сессии: перейдите к задаче заново", show_alert=True)
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        my_db_id = await _te_get_my_db_id(db, tg_id)
        if not _can_edit_task(task, my_db_id, is_any_admin(tg_id)):
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            conn2 = _get_sync_db(db).get_connection()
            shops_raw = conn2.execute(
                "SELECT DISTINCT shop_name FROM users "
                "WHERE shop_name IS NOT NULL AND shop_name != '' ORDER BY shop_name"
            ).fetchall()
            conn2.close()
            shop_candidates = [sh for (sh,) in shops_raw]
        except Exception:
            shop_candidates = []
        shop_name = resolve_cb_name(raw_suffix, shop_candidates) if shop_candidates else raw_suffix
        if not shop_name:
            await callback.answer("Ошибка: магазин не найден", show_alert=True)
            return
        await db.update_task(
            task_id, task['title'], task.get('description', ''),
            task.get('topic_id'), None, None,
            task.get('priority', 'normal'), task.get('deadline'),
            shop_name, 0, task.get('recurrence') or 'none'
        )
        try:
            old = task.get('assigned_name') or task.get('assigned_shop') or '—'
            await db.add_task_history(task_id, my_db_id, 'edit_assignee', old, f"🏪 {shop_name}")
        except Exception:
            pass
        await callback.answer(f"✅ Магазин: {shop_name}")
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_ews_cb: %s", e)
        await callback.answer("Ошибка")


# ── Комментарии к задаче ──────────────────────────────────────────────────────

_CMTS_PAGE = 10


async def _show_task_comments(target, state: FSMContext, task_id: int,
                               tg_id: int, page: int = 0):
    """Показать список комментариев задачи с пагинацией."""
    from aiogram.types import Message as _Msg
    db = await get_db(tg_id, state)
    if db is None:
        try:
            await target.answer("Нет активной org")
        except Exception:
            pass
        return
    try:
        comments = await db.get_task_comments(task_id)
        task = await db.get_task(task_id)
        task_title = task['title'] if task else f"#{task_id}"

        # Newest first
        comments = list(reversed(comments))
        total = len(comments)
        start = page * _CMTS_PAGE
        page_items = comments[start:start + _CMTS_PAGE]

        if total == 0:
            body = "<i>Комментариев пока нет.</i>"
        else:
            parts = []
            for c in page_items:
                parts.append(
                    f"<b>{he(c['author_name'])}</b> · {c['created_at_fmt']}\n"
                    f"{he(c['text'])}"
                )
            body = "\n\n".join(parts)

        header = f"💬 <b>Комментарии ({total})</b>\n<b>{he(task_title)}</b>\n\n"
        text = header + body

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(
            text="✍️ Написать комментарий",
            callback_data=f"tsk_cmt_add_{task_id}"
        ))

        total_pages = max(1, (total + _CMTS_PAGE - 1) // _CMTS_PAGE)
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(
                    text="◀️", callback_data=f"tsk_cmts_{task_id}_{page - 1}"
                ))
            nav.append(InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data="tsk_cmts_noop"
            ))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(
                    text="▶️", callback_data=f"tsk_cmts_{task_id}_{page + 1}"
                ))
            kb.row(*nav)

        kb.row(back_button(f"tsk_view_{task_id}", "⬅️ К задаче"))
        kb.row(home_button())
        markup = kb.as_markup()

        if isinstance(target, _Msg):
            await target.answer(text, parse_mode="HTML", reply_markup=markup)
        else:
            await target.answer()
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.error("_show_task_comments: %s", e)
        try:
            await target.answer("Ошибка загрузки комментариев")
        except Exception:
            pass


@tasks_router.callback_query(F.data.startswith("tsk_cmts_"))
async def tsk_cmts_cb(callback: CallbackQuery, state: FSMContext):
    """Показать список комментариев (с пагинацией). Сбрасывает FSM-state."""
    if callback.data == "tsk_cmts_noop":
        await callback.answer()
        return
    await clear_state_keep_org(state)
    parts = callback.data.split("_")
    try:
        task_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await callback.answer("Ошибка формата")
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    user = await db.get_user(tg_id)
    my_db_id = user[0] if user else 0
    my_shop = user[8] if user and len(user) > 8 else None
    if not _can_view_task(task, my_db_id, is_any_admin(tg_id), my_shop):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _show_task_comments(callback, state, task_id, tg_id, page)


@tasks_router.callback_query(F.data.startswith("tsk_cmt_add_"))
async def tsk_cmt_add_cb(callback: CallbackQuery, state: FSMContext):
    """Войти в FSM для написания комментария."""
    try:
        task_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка формата")
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return
    user = await db.get_user(tg_id)
    my_db_id = user[0] if user else 0
    my_shop = user[8] if user and len(user) > 8 else None
    if not _can_view_task(task, my_db_id, is_any_admin(tg_id), my_shop):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.update_data(tsk_comment_task_id=task_id)
    await state.set_state(TaskCommentStates.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="❌ Отменить",
        callback_data=f"tsk_cmts_{task_id}_0"
    ))
    await callback.answer()
    await callback.message.edit_text(
        f"✍️ <b>Новый комментарий</b>\n\n"
        f"<b>{he(task['title'])}</b>\n\n"
        "Введите текст комментария:",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


@tasks_router.message(TaskCommentStates.waiting_text)
async def tsk_cmt_text_msg(message: Message, state: FSMContext, bot: Bot):
    """Сохранить комментарий и уведомить участников задачи."""
    data = await state.get_data()
    task_id = data.get('tsk_comment_task_id')
    if not task_id:
        await clear_state_keep_org(state)
        return
    text = (message.text or "").strip()[:2000]
    if not text:
        await message.answer("⚠️ Комментарий не может быть пустым.")
        return
    tg_id = message.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await clear_state_keep_org(state)
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await message.answer("Задача не найдена.")
            await clear_state_keep_org(state)
            return
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = user[8] if user and len(user) > 8 else None
        if not _can_view_task(task, my_db_id, is_any_admin(tg_id), my_shop):
            await message.answer("⚠️ Нет доступа к задаче.")
            await clear_state_keep_org(state)
            return
        await db.add_task_comment(task_id, my_db_id or 0, text)
        # ── Уведомления участникам ────────────────────────────────────────────
        try:
            conn = _get_sync_db(db).get_connection()
            notify_ids: set[int] = set()
            created_by = task.get('created_by')
            assigned_to = task.get('assigned_to')
            task_assign_all = task.get('assign_all', False)
            task_assigned_shop = task.get('assigned_shop') or None

            # Always notify creator and direct assignee
            for uid in (created_by, assigned_to):
                if uid:
                    row = conn.execute(
                        "SELECT telegram_id FROM users WHERE id = ?", (uid,)
                    ).fetchone()
                    if row and row[0] and row[0] != tg_id:
                        notify_ids.add(row[0])

            # Expand recipients for shared tasks
            if task_assign_all:
                rows = conn.execute(
                    "SELECT telegram_id FROM users"
                    " WHERE telegram_id IS NOT NULL AND telegram_id != ?",
                    (tg_id,)
                ).fetchall()
                for r in rows:
                    if r[0]:
                        notify_ids.add(r[0])
            elif task_assigned_shop:
                rows = conn.execute(
                    "SELECT telegram_id FROM users"
                    " WHERE shop_name = ? AND telegram_id IS NOT NULL AND telegram_id != ?",
                    (task_assigned_shop, tg_id)
                ).fetchall()
                for r in rows:
                    if r[0]:
                        notify_ids.add(r[0])

            conn.close()

            # Cap at 30 to avoid flooding small-team orgs
            capped_ids = list(notify_ids)[:30]

            notif_text = (
                f"💬 <b>Новый комментарий</b>\n\n"
                f"<b>{he(task['title'])}</b>\n\n"
                f"{he(text)}"
            )
            btn_markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="📋 К задаче",
                    callback_data=f"tsk_view_{task_id}"
                )
            ]])

            async def _send_comment_notif(ntg: int) -> None:
                try:
                    await bot.send_message(
                        ntg, notif_text,
                        parse_mode="HTML",
                        reply_markup=btn_markup
                    )
                except Exception:
                    pass

            await asyncio.gather(*[_send_comment_notif(ntg) for ntg in capped_ids])
        except Exception as ne:
            logger.error("tsk_cmt_text_msg notify: %s", ne)
        await clear_state_keep_org(state)
        await _show_task_comments(message, state, task_id, tg_id, page=0)
    except Exception as e:
        logger.error("tsk_cmt_text_msg: %s", e)
        await message.answer("⚠️ Ошибка сохранения.")
        await clear_state_keep_org(state)


# ══════════════════════════════════════════════════════════════════════════════
# НАПОМИНАНИЯ
# ══════════════════════════════════════════════════════════════════════════════

@tasks_router.callback_query(F.data.startswith("tsk_remind_"))
async def tsk_remind_cb(callback: CallbackQuery, state: FSMContext):
    """Показать меню выбора времени напоминания."""
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = user[8] if user and len(user) > 8 else None
        if not _can_view_task(task, my_db_id, is_any_admin(tg_id), my_shop):
            await callback.answer("Нет доступа")
            return
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="⏰ Через 1 час",  callback_data=f"tsk_rmd_{task_id}_60"),
            InlineKeyboardButton(text="⏰ Через 2 часа", callback_data=f"tsk_rmd_{task_id}_120"),
        )
        kb.row(
            InlineKeyboardButton(text="⏰ Через 4 часа", callback_data=f"tsk_rmd_{task_id}_240"),
            InlineKeyboardButton(text="🌅 Завтра утром", callback_data=f"tsk_rmd_{task_id}_0"),
        )
        kb.row(InlineKeyboardButton(text="✏️ Своё время", callback_data=f"tsk_rmdc_{task_id}"))
        kb.row(back_button(f"tsk_view_{task_id}", "⬅️ К задаче"))
        await callback.answer()
        await callback.message.edit_text(
            f"🔔 <b>Напоминание о задаче</b>\n\n"
            f"<b>{he(task.get('title', '—'))}</b>\n\n"
            f"Когда напомнить?",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("tsk_remind_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.callback_query(F.data.startswith("tsk_rmd_"))
async def tsk_rmd_set_cb(callback: CallbackQuery, state: FSMContext):
    """Сохранить напоминание в БД (APScheduler job check_task_reminders подберёт его)."""
    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("Неверный формат")
        return
    try:
        task_id = int(parts[2])
        minutes = int(parts[3])
    except (ValueError, IndexError):
        await callback.answer("Неверный формат")
        return
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = user[8] if user and len(user) > 8 else None
        if not my_db_id:
            await callback.answer("Пользователь не найден")
            return
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        if not _can_view_task(task, my_db_id, is_any_admin(tg_id), my_shop):
            await callback.answer("Нет доступа")
            return
        from datetime import timezone as _tz, timedelta as _td
        now_utc = datetime.now(tz=_tz.utc)
        if minutes == 0:
            # «Завтра утром» — следующий день в 09:00 UTC
            tomorrow = (now_utc + _td(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            remind_at = tomorrow.strftime("%Y-%m-%d %H:%M:%S")
            when_str = f"завтра в 09:00 UTC"
        else:
            remind_at = (now_utc + _td(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
            if minutes < 120:
                when_str = f"через {minutes} мин."
            else:
                hrs = minutes // 60
                when_str = f"через {hrs} ч."
        await db.add_task_reminder(task_id, my_db_id, remind_at)
        await callback.answer(f"✅ Напомню {when_str}!", show_alert=True)
        await _show_task_after_edit(callback, state, task_id, tg_id)
    except Exception as e:
        logger.error("tsk_rmd_set_cb: %s", e)
        await callback.answer("Ошибка сохранения напоминания")


@tasks_router.callback_query(F.data.startswith("tsk_rmdc_"))
async def tsk_rmdc_cb(callback: CallbackQuery, state: FSMContext):
    """Запросить своё время напоминания (FSM)."""
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        my_shop = user[8] if user and len(user) > 8 else None
        if not _can_view_task(task, my_db_id, is_any_admin(tg_id), my_shop):
            await callback.answer("Нет доступа")
            return
        _cur = await state.get_data() or {}
        await state.set_data({**_cur, 'reminder_task_id': task_id})
        await state.set_state(TaskReminderStates.waiting_custom_time)
        kb = InlineKeyboardBuilder()
        kb.row(back_button(f"tsk_remind_{task_id}", "⬅️ Отмена"))
        await callback.answer()
        await callback.message.edit_text(
            "🔔 <b>Своё время напоминания</b>\n\n"
            "Введите время одним из форматов:\n"
            "• <code>через 30м</code> — через 30 минут\n"
            "• <code>через 3ч</code> — через 3 часа\n"
            "• <code>15:30</code> — сегодня в 15:30 UTC\n"
            "• <code>05.07 09:00</code> — 5 июля в 09:00 UTC",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("tsk_rmdc_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.message(TaskReminderStates.waiting_custom_time)
async def tsk_rmd_custom_msg(message: Message, state: FSMContext):
    """Принять пользовательский ввод времени напоминания и сохранить."""
    import re as _re
    from datetime import timezone as _tz, timedelta as _td
    tg_id = message.from_user.id
    data = await state.get_data()
    task_id = data.get('reminder_task_id')
    if not task_id:
        await clear_state_keep_org(state)
        return
    raw = (message.text or '').strip().lower()
    now_utc = datetime.now(tz=_tz.utc)
    remind_at = None
    when_str = ""
    # Формат: "через Nм" или "через N мин"
    m = _re.match(r'^через\s+(\d+)\s*м(?:ин)?$', raw)
    if m:
        mins = int(m.group(1))
        remind_at = (now_utc + _td(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")
        when_str = f"через {mins} мин."
    # Формат: "через Nч" или "через N ч"
    if not remind_at:
        m = _re.match(r'^через\s+(\d+)\s*ч(?:ас(?:а|ов)?)?$', raw)
        if m:
            hrs = int(m.group(1))
            remind_at = (now_utc + _td(hours=hrs)).strftime("%Y-%m-%d %H:%M:%S")
            when_str = f"через {hrs} ч."
    # Формат: "ЧЧ:ММ" — сегодня в это время UTC
    if not remind_at:
        m = _re.match(r'^(\d{1,2}):(\d{2})$', raw)
        if m:
            try:
                h, mi = int(m.group(1)), int(m.group(2))
                candidate = now_utc.replace(hour=h, minute=mi, second=0, microsecond=0)
                if candidate <= now_utc:
                    candidate += _td(days=1)
                remind_at = candidate.strftime("%Y-%m-%d %H:%M:%S")
                when_str = f"в {h:02d}:{mi:02d} UTC"
            except ValueError:
                pass  # невалидное время (напр. 25:00) → remind_at остаётся None
    # Формат: "дд.мм ЧЧ:ММ"
    if not remind_at:
        m = _re.match(r'^(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})$', raw)
        if m:
            try:
                day, mon, h, mi = (int(x) for x in m.groups())
                year = now_utc.year
                from datetime import datetime as _ddt
                candidate = _ddt(year, mon, day, h, mi, tzinfo=_tz.utc)
                if candidate <= now_utc:
                    candidate = candidate.replace(year=year + 1)
                remind_at = candidate.strftime("%Y-%m-%d %H:%M:%S")
                when_str = f"{day:02d}.{mon:02d} {h:02d}:{mi:02d} UTC"
            except Exception:
                pass
    if not remind_at:
        await message.answer(
            "⚠️ Не удалось распознать время. Примеры:\n"
            "<code>через 30м</code> · <code>через 2ч</code> · "
            "<code>15:30</code> · <code>05.07 09:00</code>",
            parse_mode="HTML",
        )
        return
    db = await get_db(tg_id, state)
    if db is None:
        await clear_state_keep_org(state)
        return
    try:
        user = await db.get_user(tg_id)
        my_db_id = user[0] if user else 0
        await db.add_task_reminder(task_id, my_db_id, remind_at)
        await clear_state_keep_org(state)
        await message.answer(f"✅ Напомню {when_str}!", parse_mode="HTML")
        await _show_task_after_edit(message, state, task_id, tg_id, is_msg=True)
    except Exception as e:
        logger.error("tsk_rmd_custom_msg: %s", e)
        await message.answer("⚠️ Ошибка сохранения напоминания.")
        await clear_state_keep_org(state)


# ══════════════════════════════════════════════════════════════════════════════
# ОЦЕНКА ЗАДАЧИ (только admin, только status=done)
# ══════════════════════════════════════════════════════════════════════════════

def _rate_pick_kb(task_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора звёзд."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="1 ⭐",  callback_data=f"tsk_rate_{task_id}_1"),
        InlineKeyboardButton(text="2 ⭐",  callback_data=f"tsk_rate_{task_id}_2"),
        InlineKeyboardButton(text="3 ⭐",  callback_data=f"tsk_rate_{task_id}_3"),
        InlineKeyboardButton(text="4 ⭐",  callback_data=f"tsk_rate_{task_id}_4"),
        InlineKeyboardButton(text="5 ⭐",  callback_data=f"tsk_rate_{task_id}_5"),
    )
    kb.row(back_button(f"tsk_view_{task_id}", "⬅️ Отмена"))
    return kb.as_markup()


@tasks_router.callback_query(F.data.startswith("tsk_ratepick_"))
async def tsk_ratepick_cb(callback: CallbackQuery, state: FSMContext):
    """Показать выбор звёзд для оценки задачи."""
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    if not is_any_admin(tg_id):
        await callback.answer("Только для администраторов")
        return
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        if task.get('status') != 'done':
            await callback.answer("Оценка доступна только для выполненных задач")
            return
        existing = task.get('rating')
        extra = f"\n\nТекущая оценка: {'⭐' * existing} ({existing}/5)" if existing else ""
        await callback.answer()
        await callback.message.edit_text(
            f"⭐ <b>Оценка задачи</b>\n\n"
            f"<b>{he(task.get('title', '—'))}</b>{extra}\n\n"
            f"Выберите оценку:",
            reply_markup=_rate_pick_kb(task_id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("tsk_ratepick_cb: %s", e)
        await callback.answer("Ошибка")


@tasks_router.callback_query(F.data.startswith("tsk_rate_"))
async def tsk_rate_cb(callback: CallbackQuery, state: FSMContext):
    """Сохранить оценку и предложить добавить комментарий (FSM)."""
    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("Неверный формат")
        return
    try:
        task_id = int(parts[2])
        stars = int(parts[3])
    except (ValueError, IndexError):
        await callback.answer("Неверный формат")
        return
    if not (1 <= stars <= 5):
        await callback.answer("Неверная оценка")
        return
    tg_id = callback.from_user.id
    if not is_any_admin(tg_id):
        await callback.answer("Только для администраторов")
        return
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return
    try:
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
        if task.get('status') != 'done':
            await callback.answer("Оценка доступна только для выполненных задач")
            return
        # Сохраняем оценку сразу (без комментария); комментарий добавим опционально
        await db.rate_task(task_id, stars)
        try:
            await db.add_task_history(task_id, 0, 'rated', None,
                                      f"{'⭐' * stars} ({stars}/5)")
        except Exception:
            pass
        # Записываем task_id и stars в FSM для handler-а текстового комментария
        await state.set_data({**((await state.get_data()) or {}),
                               'rate_task_id': task_id, 'rate_stars': stars})
        await state.set_state(TaskRateStates.waiting_comment)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(
            text="⏭ Пропустить",
            callback_data=f"tsk_rskip_{task_id}"
        ))
        await callback.answer()
        await callback.message.edit_text(
            f"{'⭐' * stars} <b>Оценка сохранена!</b>\n\n"
            f"Хотите добавить текстовый отзыв? Введите его сообщением "
            f"или нажмите «Пропустить».",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("tsk_rate_cb: %s", e)
        await callback.answer("Ошибка сохранения оценки")


@tasks_router.callback_query(F.data.startswith("tsk_rskip_"))
async def tsk_rskip_cb(callback: CallbackQuery, state: FSMContext):
    """Пропустить ввод комментария к оценке — перейти к задаче."""
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    await clear_state_keep_org(state)
    await callback.answer()
    await _show_task_after_edit(callback, state, task_id, tg_id)


@tasks_router.message(TaskRateStates.waiting_comment)
async def tsk_rate_comment_msg(message: Message, state: FSMContext):
    """Принять текстовый комментарий к оценке и сохранить его."""
    tg_id = message.from_user.id
    if not is_any_admin(tg_id):
        await clear_state_keep_org(state)
        return
    data = await state.get_data()
    task_id = data.get('rate_task_id')
    stars = data.get('rate_stars', 0)
    if not task_id:
        await clear_state_keep_org(state)
        return
    comment_text = (message.text or '').strip()
    if not comment_text:
        await message.answer("⚠️ Комментарий не может быть пустым. Введите текст или нажмите «Пропустить».")
        return
    db = await get_db(tg_id, state)
    if db is None:
        await clear_state_keep_org(state)
        return
    try:
        await db.rate_task(task_id, stars, comment_text)
        try:
            await db.add_task_history(task_id, 0, 'rated', None,
                                      f"{'⭐' * stars} ({stars}/5): {comment_text[:80]}")
        except Exception:
            pass
        await clear_state_keep_org(state)
        await _show_task_after_edit(message, state, task_id, tg_id, is_msg=True)
    except Exception as e:
        logger.error("tsk_rate_comment_msg: %s", e)
        await message.answer("⚠️ Ошибка сохранения отзыва.")
        await clear_state_keep_org(state)
