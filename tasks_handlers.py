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
from states import TaskCreateStates, AiTaskCreateStates

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
    return f"{status} {title}{dl}"


def _tasks_keyboard(tasks: list, is_admin: bool, page: int = 0, tg_id: int = 0) -> InlineKeyboardMarkup:
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

    kb.row(back_button("tsk_list_0", "⬅️ К списку"))
    kb.row(home_button())
    return kb.as_markup()


async def _show_tasks_list(target, state: FSMContext, page: int = 0):
    """Показать список задач. target — Message или CallbackQuery."""
    from aiogram.types import Message as Msg
    tg_id = target.from_user.id
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
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (tg_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = my_row[1] if my_row and len(my_row) > 1 else None
        admin = is_any_admin(tg_id)

        tasks = db.get_tasks(is_admin=admin, my_user_id=my_db_id, my_shop=my_shop)
        active = [t for t in tasks if t.get('status') not in ('done', 'cancelled')]

        if not active:
            text = "📋 <b>Задачи</b>\n\nАктивных задач нет."
        else:
            text = f"📋 <b>Мои задачи</b>\n<i>Всего активных: {len(active)}</i>"

        kb = _tasks_keyboard(active, admin, page, tg_id=tg_id)
        await state.update_data(tsk_list=active, tsk_page=page, tsk_my_db_id=my_db_id)

        if isinstance(target, Msg):
            await fsm_edit(state, target, text, kb)
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


# ── Просмотр задачи ──────────────────────────────────────────────────────────

@tasks_router.callback_query(F.data.startswith("tsk_view_"))
async def task_view_cb(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    db = await get_db(tg_id, state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (tg_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = my_row[1] if my_row and len(my_row) > 1 else None
        admin = is_any_admin(tg_id)

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
            lines = []
            for item in checklist:
                mark = "✅" if item.get('is_done') else "☐"
                lines.append(f"  {mark} {he(item.get('text', ''))}")
            cl_str = "\n\nЧеклист:\n" + "\n".join(lines)

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
                completions = db.get_task_user_completions(task_id)
                if completions:
                    text += f"\n\n👥 Выполнили ({len(completions)}):\n"
                    for c in completions[:8]:
                        text += f"  ✅ {he(c['name'])}\n"
            except Exception:
                pass

        kb = _task_detail_keyboard(task, my_db_id, admin, my_shop=my_shop)
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("task_view_cb: %s", e)
        await callback.answer("Ошибка загрузки задачи")


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
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id, shop_name FROM users WHERE telegram_id = ?", (tg_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        my_shop = my_row[1] if my_row and len(my_row) > 1 else None
        admin = is_any_admin(tg_id)

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

        # Для не-admin: только допустимый следующий шаг по цепочке (cancelled — только admin)
        if not admin:
            allowed_next = _STATUS_NEXT.get(task.get('status', ''))
            if new_status != allowed_next:
                await callback.answer("Недопустимый переход статуса")
                return

        db.update_task_status(task_id, new_status)

        try:
            db.add_task_history(task_id, my_db_id, 'status', task['status'], new_status)
        except Exception:
            pass

        if new_status in ('done', 'review') and (assign_all or assigned_shop):
            try:
                db.record_task_user_completion(task_id, my_db_id, new_status)
            except Exception:
                pass

        if new_status == 'done':
            try:
                _new_assigned_to = _spawn_recurring_task(db, task)
                if _new_assigned_to:
                    try:
                        _conn_r = db.get_connection()
                        _r_row = _conn_r.execute(
                            "SELECT telegram_id FROM users WHERE id = ?", (_new_assigned_to,)
                        ).fetchone()
                        _conn_r.close()
                        _r_tg = _r_row[0] if _r_row else None
                        if _r_tg:
                            db.add_notification_to_history(
                                _new_assigned_to, 'task_assigned',
                                f"🔁 Создана следующая задача: {task['title']}")
                            await callback.bot.send_message(
                                _r_tg,
                                f"🔁 <b>Создана следующая задача</b>\n\n<b>{he(task['title'])}</b>",
                                parse_mode="HTML"
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
                db.add_notification_to_history(
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


def _next_recurrence_date(base, recurrence: str):
    """Вернуть следующую дату для повторяющейся задачи без сторонних зависимостей."""
    from datetime import timedelta
    import calendar
    if recurrence == 'daily':
        return base + timedelta(days=1)
    if recurrence == 'weekly':
        return base + timedelta(weeks=1)
    if recurrence == 'monthly':
        month = base.month + 1
        year = base.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        max_day = calendar.monthrange(year, month)[1]
        return base.replace(year=year, month=month, day=min(base.day, max_day))
    return None


def _spawn_recurring_task(db, task: dict):
    """Создать следующую задачу для повторяющейся задачи."""
    from datetime import date
    recurrence = task.get('recurrence') or ''
    if not recurrence or recurrence in ('none', ''):
        return

    old_deadline = task.get('deadline') or ''
    try:
        base = date.fromisoformat(old_deadline[:10]) if old_deadline else date.today()
    except Exception:
        base = date.today()

    new_date = _next_recurrence_date(base, recurrence)
    if new_date is None:
        return

    # Сохраняем время дедлайна если было задано
    if old_deadline and len(old_deadline) >= 13 and ("T" in old_deadline or " " in old_deadline[10:]):
        new_deadline = new_date.isoformat() + old_deadline[10:16]
    else:
        new_deadline = new_date.isoformat()

    # Копируем пункты чеклиста из исходной задачи
    checklist_items = None
    old_checklist = task.get('checklist') or []
    if old_checklist:
        checklist_items = [item.get('text', '') for item in old_checklist if item.get('text', '').strip()]

    _assigned_to = task.get('assigned_to')
    db.create_task(
        title=task['title'],
        description=task.get('description', ''),
        topic_id=task.get('topic_id'),
        created_by=task.get('created_by', 0),
        assigned_to=_assigned_to,
        assigned_shop=task.get('assigned_shop'),
        assign_all=1 if task.get('assign_all') else 0,
        priority=task.get('priority', 'normal'),
        deadline=new_deadline,
        recurrence=recurrence,
        checklist=checklist_items,
    )
    logger.info("_spawn_recurring_task: '%s' → %s", task['title'], new_deadline)
    return _assigned_to


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
        conn = db.get_connection()
        my_row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,)).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        db.add_task_attachments(task_id, my_db_id, [{
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
        conn = db.get_connection()
        my_row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,)).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0

        task = db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return

        db.record_task_user_completion(task_id, my_db_id, 'done')

        # Авто-переход в «На проверку» когда все участники выполнили
        try:
            _assign_all = task.get('assign_all', False)
            _assigned_shop = task.get('assigned_shop', '')
            if (_assign_all or _assigned_shop) and task.get('status') not in ('done', 'cancelled', 'review'):
                _conn_s = db.get_connection()
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
                    _comps = db.get_task_user_completions(task_id)
                    if member_ids.issubset({c['user_id'] for c in _comps}):
                        db.update_task_status(task_id, 'review')
        except Exception as _ae:
            logger.error("task_mycomp_cb auto-advance: %s", _ae)

        creator_id = task.get('created_by')
        if creator_id and creator_id != my_db_id:
            try:
                user_name = callback.from_user.first_name or "Сотрудник"
                db.add_notification_to_history(
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

        task = db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return

        try:
            _conn_r = db.get_connection()
            _my_row = _conn_r.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (tg_id,)
            ).fetchone()
            _conn_r.close()
            my_db_id = _my_row[0] if _my_row else None
        except Exception:
            my_db_id = None

        db.update_task_status(task_id, 'in_progress')

        try:
            db.add_task_history(task_id, my_db_id, 'status', task['status'], 'in_progress')
        except Exception:
            pass

        # Уведомить исполнителя — Telegram + колокольчик + Web Push
        _assigned_to = task.get('assigned_to')
        if _assigned_to:
            try:
                _conn2 = db.get_connection()
                _ar = _conn2.execute(
                    "SELECT telegram_id FROM users WHERE id = ?", (_assigned_to,)
                ).fetchone()
                _conn2.close()
                _assignee_tg = _ar[0] if _ar else None
                if _assignee_tg:
                    try:
                        db.add_notification_to_history(
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
                            parse_mode="HTML"
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
        conn = db.get_connection()
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
        rows = db.get_all_users()
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
            conn = db.get_connection()
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
            conn = db.get_connection()
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
        conn = db.get_connection()
        row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,)).fetchone()
        conn.close()
        created_by = row[0] if row else 0
    except Exception:
        created_by = 0

    try:
        task_id = db.create_task(
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
        if assign_all:
            conn_all = db.get_connection()
            members = [(r[0], r[1]) for r in conn_all.execute(
                "SELECT id, telegram_id FROM users WHERE telegram_id IS NOT NULL"
            ).fetchall()]
            conn_all.close()
        elif assigned_shop:
            conn3 = db.get_connection()
            members = [(r[0], r[1]) for r in conn3.execute(
                "SELECT id, telegram_id FROM users "
                "WHERE shop_name = ? AND telegram_id IS NOT NULL", (assigned_shop,)
            ).fetchall()]
            conn3.close()
        elif assigned_to:
            conn4 = db.get_connection()
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
                        _tgid, notify_text, parse_mode="HTML"
                    )
                except Exception:
                    pass
                try:
                    db.add_notification_to_history(
                        _uid, 'task_assigned', f"📋 Новая задача: {title}"
                    )
                except Exception:
                    pass
    except Exception as _ne:
        logger.warning("tsk_c_ok notify: %s", _ne)

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
        conn = db.get_connection()
        try:
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?",
                (callback.from_user.id,)
            ).fetchone()
        finally:
            conn.close()
        my_db_id = my_row[0] if my_row else 0

        db.create_task(
            title=title,
            description=description,
            created_by=my_db_id,
            priority=priority,
        )
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
