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

from keyboards import InlineKeyboardBuilder, back_button, home_button
from db_utils import get_db, clear_state_keep_org, is_any_admin
from message_utils import fsm_edit
from utils import he

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

    kb.row(InlineKeyboardButton(text="◀ К списку", callback_data="tsk_list_0"))
    kb.row(home_button())
    return kb.as_markup()


async def _show_tasks_list(target, state: FSMContext, page: int = 0):
    """Показать список задач. target — Message или CallbackQuery."""
    from aiogram.types import Message as Msg
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
        tg_id = target.from_user.id
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

        if new_status in ('done', 'review') and (assign_all or assigned_shop):
            try:
                db.record_task_user_completion(task_id, my_db_id, new_status)
            except Exception:
                pass

        if new_status == 'done':
            try:
                _spawn_recurring_task(db, task)
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

    db.create_task(
        title=task['title'],
        description=task.get('description', ''),
        topic_id=task.get('topic_id'),
        created_by=task.get('created_by', 0),
        assigned_to=task.get('assigned_to'),
        assigned_shop=task.get('assigned_shop'),
        assign_all=1 if task.get('assign_all') else 0,
        priority=task.get('priority', 'normal'),
        deadline=new_deadline,
        recurrence=recurrence,
        checklist=checklist_items,
    )
    logger.info("_spawn_recurring_task: '%s' → %s", task['title'], new_deadline)


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
    db = await get_db(state)

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
    db = await get_db(state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        tg_id = callback.from_user.id
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
    db = await get_db(state)
    if db is None:
        await callback.answer("Нет активной org")
        return

    try:
        admin = await is_any_admin(state)
        if not admin:
            await callback.answer("Нет доступа")
            return

        task = db.get_task(task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return

        db.update_task_status(task_id, 'in_progress')

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
