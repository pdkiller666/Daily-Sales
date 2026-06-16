"""
Модуль учёта отсутствий: отпуска, больничные, отгулы, прогулы.
Сотрудник подаёт заявку, администратор одобряет/отклоняет.
Прогул добавляется только администратором.
"""
import logging
from datetime import date, datetime, timedelta

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from keyboards import InlineKeyboardBuilder, back_button, home_button
from db_utils import get_db, clear_state_keep_org, is_any_admin
from utils import he
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_BTN
from message_utils import fsm_edit, delete_message_safe

absence_router = Router()
logger = logging.getLogger(__name__)

# ── Словари ──────────────────────────────────────────────────────────────────

_TYPE_LABELS = {
    'vacation':     '⚪ Отпуск',
    'sick':         '🔵 Больничный',
    'compensatory': '🟡 Отгул',
    'absence':      '🔴 Прогул',
    'other':        '⬜ Другое',
}
_TYPE_KEYS = ['vacation', 'sick', 'compensatory', 'other']  # доступны сотруднику
_STATUS_LABELS = {
    'pending':   '❓ На рассмотрении',
    'approved':  '✅ Одобрено',
    'rejected':  '❌ Отклонено',
    'cancelled': '🚫 Отменено',
}
_PENALTY_LABELS = {
    'none':    'Без штрафа',
    'no_pay':  'Не засчитывать день',
    'fine':    'Штраф (фиксированный)',
    'both':    'Не засчитывать + штраф',
}
_MONTH_NAMES = [
    'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'
]


class AbsenceStates(StatesGroup):
    new_start_date   = State()
    new_end_date     = State()
    new_comment      = State()
    reject_comment   = State()
    prg_date         = State()
    settings_limit   = State()
    settings_penalty = State()


# ── Утилиты ──────────────────────────────────────────────────────────────────

def _parse_date(s: str):
    """Принимает DD.MM.YYYY, DD.MM.YY или DD.MM (текущий год). Возвращает date или None."""
    s = s.strip()
    try:
        if '.' in s:
            parts = s.split('.')
            if len(parts) == 2:  # DD.MM — текущий год
                return date(date.today().year, int(parts[1]), int(parts[0]))
            if len(parts) == 3:  # DD.MM.YYYY или DD.MM.YY
                year = int(parts[2])
                if year < 100:
                    year += 2000
                return date(year, int(parts[1]), int(parts[0]))
    except Exception:
        pass
    return None


def _fmt_date(d) -> str:
    if isinstance(d, str):
        d = d[:10]
        try:
            d = date.fromisoformat(d)
        except Exception:
            return d
    return d.strftime('%d.%m.%Y')


def _days_count(sd: str, ed: str) -> int:
    try:
        return (date.fromisoformat(ed[:10]) - date.fromisoformat(sd[:10])).days + 1
    except Exception:
        return 1


def _absence_line(row, with_user=False) -> str:
    """Одна строка для списка отсутствий."""
    if with_user:
        ab_id, uid, atype, sd, ed, status, _, comment, _, _, fn, ln, shop = row[:13]
        name = f"{fn or ''} {ln or ''}".strip() or "?"
        header = f"<b>{he(name)}</b> · "
    else:
        ab_id, atype, sd, ed, status, _, comment, _, _ = row[:9]
        header = ""
    days = _days_count(sd, ed)
    label = _TYPE_LABELS.get(atype, atype)
    st_label = _STATUS_LABELS.get(status, status)
    return (f"{header}{label}\n"
            f"📅 {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)\n"
            f"Статус: {st_label}")


async def _notify_user(state, telegram_id: int, text: str):
    """Отправить уведомление пользователю по telegram_id."""
    try:
        import bot_holder as _bh
        from notif_utils import add_read_btn
        bot = _bh.get_bot()
        if bot:
            await bot.send_message(telegram_id, text, parse_mode='HTML',
                                   reply_markup=add_read_btn())
        try:
            from web.push_utils import send_web_push
            import re as _re, asyncio as _aio
            _pb = _re.sub(r'<[^>]+>', '', text)[:120].strip()
            await _aio.to_thread(send_web_push, int(telegram_id), "📋 Отсутствия", _pb, "/absences")
            try:
                import sqlite3 as _sq3
                _mc = _sq3.connect("data/main.db")
                _org_row = _mc.execute(
                    "SELECT org_db FROM user_org_mapping WHERE telegram_id = ?", (int(telegram_id),)
                ).fetchone()
                _mc.close()
                if _org_row and _org_row[0]:
                    from database import Database as _ADB
                    _tdb = _ADB(_org_row[0])
                    _uc = _tdb.get_connection()
                    _ur = _uc.execute("SELECT id FROM users WHERE telegram_id = ?", (int(telegram_id),)).fetchone()
                    _uc.close()
                    if _ur:
                        _tdb.add_notification_to_history(_ur[0], 'absence', _pb)
            except Exception:
                pass
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"_notify_user: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINTS
# ═══════════════════════════════════════════════════════════════════════════════

@absence_router.callback_query(F.data == 'abs_my')
async def abs_my(callback: CallbackQuery, state: FSMContext):
    """Мои отсутствия (сотрудник)."""
    db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    telegram_id = callback.from_user.id
    conn = db._db.get_connection()
    try:
        row = conn.execute(
            'SELECT id FROM users WHERE telegram_id=?', (telegram_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        await callback.answer('Пользователь не найден', show_alert=True)
        return
    uid = row[0]
    today = date.today()
    rows = await db.get_absences_for_user(uid, today.year, today.month)
    text = f'<b>📋 Мои отсутствия</b> — {_MONTH_NAMES[today.month - 1]} {today.year}\n\n'
    if rows:
        for r in rows[:10]:
            text += _absence_line(r) + '\n\n'
    else:
        text += 'Нет записей за текущий месяц.\n'
    kb = InlineKeyboardBuilder()
    kb.button(text='➕ Подать заявку', callback_data='abs_new')
    kb.button(text='📂 История за год', callback_data=f'abs_hist_{today.year}')
    kb.adjust(1)
    kb.row(back_button('my_schedule'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@absence_router.callback_query(F.data.startswith('abs_hist_'))
async def abs_hist(callback: CallbackQuery, state: FSMContext):
    year = int(callback.data.split('_')[-1])
    db = await get_db(callback.from_user.id, state)
    telegram_id = callback.from_user.id
    conn = db._db.get_connection()
    try:
        row = conn.execute('SELECT id FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        await callback.answer()
        return
    rows = await db.get_absences_for_user(row[0], year)
    text = f'<b>📂 История отсутствий {year}</b>\n\n'
    if rows:
        for r in rows[:20]:
            text += _absence_line(r) + '\n\n'
    else:
        text += 'Нет записей.\n'
    kb = InlineKeyboardBuilder()
    kb.button(text='← Назад', callback_data='abs_my')
    kb.row(home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


# ─── Новая заявка ─────────────────────────────────────────────────────────────

@absence_router.callback_query(F.data == 'abs_new')
async def abs_new(callback: CallbackQuery, state: FSMContext):
    text = '<b>➕ Новая заявка на отсутствие</b>\nВыбери тип:'
    kb = InlineKeyboardBuilder()
    for k in _TYPE_KEYS:
        kb.button(text=_TYPE_LABELS[k], callback_data=f'abs_nt_{k}')
    kb.adjust(2)
    kb.row(back_button('abs_my'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@absence_router.callback_query(F.data.startswith('abs_nt_'))
async def abs_new_type(callback: CallbackQuery, state: FSMContext):
    atype = callback.data[7:]
    if atype not in _TYPE_KEYS:
        await callback.answer()
        return

    # Проверить лимит
    db = await get_db(callback.from_user.id, state)
    settings = await db.get_absence_type_settings()
    if settings.get(atype, {}).get('annual_limit', 0) > 0:
        telegram_id = callback.from_user.id
        conn = db._db.get_connection()
        try:
            row = conn.execute('SELECT id FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
        finally:
            conn.close()
        if row:
            used = await db.get_absence_used_days(row[0], atype, date.today().year)
            limit = settings[atype]['annual_limit']
            if used >= limit:
                await callback.answer(
                    f'Лимит {_TYPE_LABELS[atype]} на год исчерпан '
                    f'({used}/{limit} дней)', show_alert=True
                )
                return

    await state.update_data(abs_type=atype, anchor_msg_id=callback.message.message_id)
    label = _TYPE_LABELS[atype]
    text = (f'<b>{label}</b>\n\n'
            f'Введи дату начала отсутствия в формате <code>DD.MM.YYYY</code>:')
    kb = InlineKeyboardBuilder()
    kb.row(back_button('abs_new'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')
    await state.set_state(AbsenceStates.new_start_date)


@absence_router.message(AbsenceStates.new_start_date)
async def abs_enter_start(message: Message, state: FSMContext):
    data = await state.get_data()
    label = _TYPE_LABELS.get(data.get('abs_type', ''), '')
    kb = InlineKeyboardBuilder()
    kb.row(back_button('abs_new'), home_button())
    d = _parse_date(message.text or '')
    if not d:
        await fsm_edit(state, message,
                       f'<b>{label}</b>\n\n'
                       f'❌ Неверный формат. Введи дату: <code>DD.MM.YYYY</code>',
                       reply_markup=kb.as_markup())
        return
    if d < date.today() - timedelta(days=30):
        await fsm_edit(state, message,
                       f'<b>{label}</b>\n\n'
                       f'❌ Дата слишком далеко в прошлом (>30 дней). Введи другую:',
                       reply_markup=kb.as_markup())
        return
    await state.update_data(abs_start=d.isoformat())
    text = (f'<b>{label}</b>\n'
            f'Начало: {_fmt_date(d)}\n\n'
            f'Введи дату окончания (<code>DD.MM.YYYY</code>):\n'
            f'<i>Для одного дня — ту же дату.</i>')
    await fsm_edit(state, message, text, reply_markup=kb.as_markup())
    await state.set_state(AbsenceStates.new_end_date)


@absence_router.message(AbsenceStates.new_end_date)
async def abs_enter_end(message: Message, state: FSMContext):
    data = await state.get_data()
    label = _TYPE_LABELS.get(data.get('abs_type', ''), '')
    start_d = date.fromisoformat(data['abs_start'])
    kb = InlineKeyboardBuilder()
    kb.row(back_button('abs_new'), home_button())
    d = _parse_date(message.text or '')
    if not d:
        await fsm_edit(state, message,
                       f'<b>{label}</b>\nНачало: {_fmt_date(start_d)}\n\n'
                       f'❌ Неверный формат. Введи дату: <code>DD.MM.YYYY</code>',
                       reply_markup=kb.as_markup())
        return
    if d < start_d:
        await fsm_edit(state, message,
                       f'<b>{label}</b>\nНачало: {_fmt_date(start_d)}\n\n'
                       f'❌ Дата окончания не может быть раньше начала. Введи снова:',
                       reply_markup=kb.as_markup())
        return
    if (d - start_d).days > 365:
        await fsm_edit(state, message,
                       f'<b>{label}</b>\nНачало: {_fmt_date(start_d)}\n\n'
                       f'❌ Слишком большой диапазон (>365 дней). Введи снова:',
                       reply_markup=kb.as_markup())
        return
    await state.update_data(abs_end=d.isoformat())
    days = (d - start_d).days + 1
    text = (f'<b>{label}</b>\n'
            f'📅 {_fmt_date(start_d)}–{_fmt_date(d)} ({days} дн.)\n\n'
            f'Добавь комментарий (или /skip для отправки без комментария):')
    await fsm_edit(state, message, text, reply_markup=kb.as_markup())
    await state.set_state(AbsenceStates.new_comment)


@absence_router.message(AbsenceStates.new_comment)
async def abs_enter_comment(message: Message, state: FSMContext):
    comment = None if (message.text or '').strip().lower() in ('/skip', 'skip') \
        else (message.text or '').strip()
    await delete_message_safe(message)
    await _abs_submit(message, state, comment)


async def _abs_submit(message: Message, state: FSMContext, comment):
    data = await state.get_data()
    db = await get_db(message.from_user.id, state)
    telegram_id = message.from_user.id
    conn = db._db.get_connection()
    try:
        row = conn.execute('SELECT id FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        await message.answer('Ошибка: пользователь не найден.')
        await clear_state_keep_org(state)
        return
    uid = row[0]
    atype = data.get('abs_type', 'other')
    sd = data.get('abs_start', date.today().isoformat())
    ed = data.get('abs_end', sd)
    ab_id = await db.add_absence(uid, atype, sd, ed, comment, uid, 'pending')
    if not ab_id:
        await message.answer('Ошибка при отправке заявки. Попробуй позже.')
        await clear_state_keep_org(state)
        return
    label = _TYPE_LABELS.get(atype, atype)
    days = _days_count(sd, ed)
    text = (f'✅ <b>Заявка отправлена!</b>\n\n'
            f'{label}\n'
            f'📅 {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)\n'
            f'Статус: ❓ На рассмотрении\n\n'
            f'Администратор рассмотрит заявку и уведомит тебя.')
    await message.answer(text, parse_mode='HTML')
    # Уведомить администраторов
    try:
        from notif_utils import add_read_btn
        import bot_holder as _bh
        bot = _bh.get_bot()
        if bot:
            import sqlite3
            conn_m = sqlite3.connect('data/main.db')
            admins = conn_m.execute(
                "SELECT telegram_id FROM user_org_mapping "
                "WHERE org_id=(SELECT org_id FROM user_org_mapping WHERE telegram_id=? AND is_active=1) "
                "AND role IN ('owner','admin') AND is_active=1",
                (telegram_id,)
            ).fetchall()
            conn_m.close()
            conn_u = db._db.get_connection()
            u_row = conn_u.execute(
                'SELECT first_name, last_name FROM users WHERE telegram_id=?',
                (telegram_id,)
            ).fetchone()
            conn_u.close()
            name = f"{u_row[0] or ''} {u_row[1] or ''}".strip() if u_row else str(telegram_id)
            notif = (f'📋 <b>Новая заявка на отсутствие</b>\n\n'
                     f'Сотрудник: <b>{he(name)}</b>\n'
                     f'{label}\n'
                     f'📅 {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)\n'
                     f'Рассмотри в разделе «График / Отсутствия».')
            for (atid,) in admins:
                try:
                    await bot.send_message(atid, notif, parse_mode='HTML',
                                           reply_markup=add_read_btn())
                except Exception:
                    pass
            try:
                from web.push_utils import send_web_push_bulk
                import re as _re, asyncio as _aio
                _pb = _re.sub(r'<[^>]+>', '', notif)[:120].strip()
                await _aio.to_thread(send_web_push_bulk, [a[0] for a in admins], "📋 Новая заявка на отсутствие", _pb, "/absences")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"absence notify admins: {e}")
    await clear_state_keep_org(state)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════════════

@absence_router.callback_query(F.data == 'abs_admin')
async def abs_admin(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    db = await get_db(callback.from_user.id, state)
    pending = await db.get_pending_absences()
    cnt = len(pending)
    badge = f' 🔴 {cnt}' if cnt else ''
    text = f'<b>📋 Управление отсутствиями</b>{badge}\n\nВыбери раздел:'
    kb = InlineKeyboardBuilder()
    kb.button(text=f'❓ Заявки{badge}', callback_data='abs_pnd')
    kb.button(text='🔴 Добавить прогул', callback_data='abs_prg_list')
    kb.button(text='📅 Все за месяц', callback_data='abs_all_month')
    kb.button(text='⚙️ Настройки типов', callback_data='abs_cfg')
    kb.adjust(2)
    kb.row(home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')


@absence_router.callback_query(F.data == 'abs_pnd')
async def abs_pending_list(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    db = await get_db(callback.from_user.id, state)
    rows = await db.get_pending_absences()
    if not rows:
        text = '✅ Нет ожидающих заявок.'
        kb = InlineKeyboardBuilder()
        kb.row(back_button('abs_admin'), home_button())
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')
        return
    text = f'<b>❓ Заявки на рассмотрении</b> ({len(rows)}):\n\n'
    for r in rows[:10]:
        ab_id, uid, atype, sd, ed, comment, created_at, fn, ln, shop = r
        name = f"{fn or ''} {ln or ''}".strip() or "?"
        days = _days_count(sd, ed)
        text += (f'#{ab_id} · <b>{he(name)}</b> ({he(shop or "—")})\n'
                 f'{_TYPE_LABELS.get(atype, atype)}: {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)\n'
                 f'{"💬 " + he(comment) if comment else ""}\n\n')
    kb = InlineKeyboardBuilder()
    for r in rows[:8]:
        ab_id = r[0]
        fn, ln = r[7], r[8]
        name = f"{fn or ''} {ln or ''}".strip()[:15]
        kb.button(text=f'#{ab_id} {name}', callback_data=f'abs_rv_{ab_id}')
    kb.adjust(2)
    kb.row(back_button('abs_admin'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')


@absence_router.callback_query(F.data.startswith('abs_rv_'))
async def abs_review(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    ab_id = int(callback.data[7:])
    db = await get_db(callback.from_user.id, state)
    rec = await db.get_absence_by_id(ab_id)
    if not rec:
        await callback.answer('Запись не найдена', show_alert=True)
        return
    r_id, uid, atype, sd, ed, status, is_paid, comment, admin_comment, _, _, created_at, _ = rec
    conn = db._db.get_connection()
    try:
        u = conn.execute(
            'SELECT first_name, last_name, shop_name, telegram_id FROM users WHERE id=?',
            (uid,)
        ).fetchone()
    finally:
        conn.close()
    fn, ln, shop, tg_id = (u or ('?', '', '—', 0))
    name = f"{fn or ''} {ln or ''}".strip()
    days = _days_count(sd, ed)
    text = (f'<b>📋 Заявка #{ab_id}</b>\n\n'
            f'Сотрудник: <b>{he(name)}</b> ({he(shop or "—")})\n'
            f'Тип: {_TYPE_LABELS.get(atype, atype)}\n'
            f'Период: {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)\n'
            f'Статус: {_STATUS_LABELS.get(status, status)}\n')
    if comment:
        text += f'Комментарий: {he(comment)}\n'
    if admin_comment:
        text += f'Ответ: {he(admin_comment)}\n'
    text += f'\nСоздана: {created_at[:10] if created_at else "—"}\n'
    kb = InlineKeyboardBuilder()
    if status == 'pending':
        kb.button(text='✅ Одобрить', callback_data=f'abs_ok_{ab_id}')
        kb.button(text='❌ Отклонить', callback_data=f'abs_rj_{ab_id}')
        kb.adjust(2)
    kb.button(text='🗑 Удалить', callback_data=f'abs_del_{ab_id}')
    kb.row(back_button('abs_pnd'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')


@absence_router.callback_query(F.data.startswith('abs_ok_'))
async def abs_approve(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    ab_id = int(callback.data[7:])
    db = await get_db(callback.from_user.id, state)
    rec = await db.get_absence_by_id(ab_id)
    if not rec:
        await callback.answer('Не найдено', show_alert=True)
        return
    _, uid, atype, sd, ed, *_ = rec
    ok = await db.update_absence_status(ab_id, 'approved',
                                         None, callback.from_user.id)
    if ok:
        await callback.answer('✅ Одобрено')
        days = _days_count(sd, ed)
        # Уведомить сотрудника
        conn = db._db.get_connection()
        try:
            u = conn.execute('SELECT telegram_id FROM users WHERE id=?', (uid,)).fetchone()
        finally:
            conn.close()
        if u and u[0]:
            await _notify_user(state, u[0],
                f'✅ <b>Заявка одобрена!</b>\n\n'
                f'{_TYPE_LABELS.get(atype, atype)}\n'
                f'📅 {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)')
        # Штраф за прогул — автозапись в salary_adjustments
        if atype == 'absence':
            try:
                settings = await db.get_absence_type_settings()
                s = settings.get('absence', {})
                pmode = s.get('penalty_mode', 'no_pay')
                pamt = float(s.get('penalty_amount') or 0)
                if pmode in ('fine', 'both') and pamt > 0:
                    adm_conn = db._db.get_connection()
                    try:
                        adm_row = adm_conn.execute(
                            'SELECT id FROM users WHERE telegram_id=?',
                            (callback.from_user.id,)
                        ).fetchone()
                    finally:
                        adm_conn.close()
                    reviewer_db_id = adm_row[0] if adm_row else None
                    d_start = date.fromisoformat(sd[:10])
                    await db.apply_absence_penalty(
                        uid, ab_id, d_start.year, d_start.month,
                        pamt * days, reviewer_db_id
                    )
            except Exception as _pe:
                logger.error(f"abs_approve penalty: {_pe}")
    else:
        await callback.answer('Ошибка', show_alert=True)
    await abs_pending_list(callback, state)


@absence_router.callback_query(F.data.startswith('abs_rj_'))
async def abs_reject_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    ab_id = int(callback.data[7:])
    await state.update_data(abs_reject_id=ab_id, anchor_msg_id=callback.message.message_id)
    text = f'❌ Заявка #{ab_id}\n\nВведи причину отказа (или /skip для отказа без комментария):'
    kb = InlineKeyboardBuilder()
    kb.row(back_button(f'abs_rv_{ab_id}'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')
    await state.set_state(AbsenceStates.reject_comment)


@absence_router.message(AbsenceStates.reject_comment)
async def abs_reject_do(message: Message, state: FSMContext):
    data = await state.get_data()
    ab_id = data.get('abs_reject_id')
    if not ab_id:
        await clear_state_keep_org(state)
        return
    comment = None if (message.text or '').strip().lower() in ('/skip', 'skip') \
        else (message.text or '').strip()
    db = await get_db(message.from_user.id, state)
    rec = await db.get_absence_by_id(ab_id)
    ok = await db.update_absence_status(ab_id, 'rejected', comment, message.from_user.id)
    if ok and rec:
        _, uid, atype, sd, ed, *_ = rec
        conn = db._db.get_connection()
        try:
            u = conn.execute('SELECT telegram_id FROM users WHERE id=?', (uid,)).fetchone()
        finally:
            conn.close()
        if u and u[0]:
            days = _days_count(sd, ed)
            await _notify_user(state, u[0],
                f'❌ <b>Заявка отклонена</b>\n\n'
                f'{_TYPE_LABELS.get(atype, atype)}\n'
                f'📅 {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)\n'
                + (f'Причина: {he(comment)}' if comment else ''))
    await delete_message_safe(message)
    await message.answer('❌ Заявка отклонена.' if ok else 'Ошибка.')
    await clear_state_keep_org(state)


@absence_router.callback_query(F.data.startswith('abs_del_'))
async def abs_delete(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    ab_id = int(callback.data[8:])
    db = await get_db(callback.from_user.id, state)
    rec = await db.get_absence_by_id(ab_id)
    if rec and rec[2] == 'absence' and rec[5] == 'approved':
        await db.delete_absence_penalty(ab_id)
    ok = await db.update_absence_status(ab_id, 'cancelled', 'Удалено администратором',
                                         callback.from_user.id)
    await callback.answer('🗑 Отменено' if ok else 'Ошибка')
    await abs_pending_list(callback, state)


# ─── Добавление прогула ────────────────────────────────────────────────────────

@absence_router.callback_query(F.data == 'abs_prg_list')
async def abs_prg_list(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    db = await get_db(callback.from_user.id, state)
    conn = db._db.get_connection()
    try:
        users = conn.execute(
            "SELECT id, first_name, last_name, shop_name FROM users "
            "ORDER BY first_name"
        ).fetchall() or []
    finally:
        conn.close()
    if not users:
        await callback.answer('Нет сотрудников')
        return
    text = '🔴 <b>Отметить прогул</b>\nВыбери сотрудника:'
    kb = InlineKeyboardBuilder()
    for u in users[:20]:
        uid, fn, ln, shop = u
        label = f"{fn or ''} {ln or ''}".strip() or f"id{uid}"
        if shop:
            label += f' ({shop})'
        kb.button(text=label[:35], callback_data=f'abs_prg_u_{uid}')
    kb.adjust(1)
    kb.row(back_button('abs_admin'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')


@absence_router.callback_query(F.data.startswith('abs_prg_u_'))
async def abs_prg_user(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    uid = int(callback.data[10:])
    await state.update_data(abs_prg_uid=uid, anchor_msg_id=callback.message.message_id)
    text = ('🔴 <b>Прогул</b>\n\n'
            'Введи дату прогула (<code>DD.MM.YYYY</code>) '
            'или диапазон через дефис: <code>01.06.2025-03.06.2025</code>')
    kb = InlineKeyboardBuilder()
    kb.row(back_button('abs_prg_list'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')
    await state.set_state(AbsenceStates.prg_date)


@absence_router.message(AbsenceStates.prg_date)
async def abs_prg_date(message: Message, state: FSMContext):
    text = (message.text or '').strip()
    if '-' in text and '.' in text:
        parts = text.split('-', 1)
        sd = _parse_date(parts[0])
        ed = _parse_date(parts[1])
    else:
        sd = _parse_date(text)
        ed = sd
    if not sd or not ed:
        kb_err = InlineKeyboardBuilder()
        kb_err.row(back_button('abs_prg_list'), home_button())
        await fsm_edit(state, message,
                       '🔴 <b>Прогул</b>\n\n'
                       '❌ Неверный формат. Пример: <code>05.06.2025</code>\n'
                       'Диапазон: <code>01.06.2025-03.06.2025</code>',
                       reply_markup=kb_err.as_markup())
        return
    if ed < sd:
        sd, ed = ed, sd
    data = await state.get_data()
    uid = data.get('abs_prg_uid')
    if not uid:
        await clear_state_keep_org(state)
        return
    db = await get_db(message.from_user.id, state)
    settings = await db.get_absence_type_settings()
    ab_id = await db.add_absence(uid, 'absence', sd.isoformat(), ed.isoformat(),
                                  None, message.from_user.id, 'approved')
    if not ab_id:
        await message.answer('Ошибка при добавлении прогула.')
        await clear_state_keep_org(state)
        return
    days = (ed - sd).days + 1
    pmode = settings.get('absence', {}).get('penalty_mode', 'no_pay')
    pamt = settings.get('absence', {}).get('penalty_amount', 0.0)
    conn = db._db.get_connection()
    try:
        u = conn.execute(
            'SELECT first_name, last_name, telegram_id FROM users WHERE id=?', (uid,)
        ).fetchone()
    finally:
        conn.close()
    fn, ln, tg_id = (u or ('?', '', 0))
    name = f"{fn or ''} {ln or ''}".strip()
    text_ok = f'🔴 Прогул добавлен: <b>{he(name)}</b>\n📅 {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)'
    if pmode in ('fine', 'both') and pamt:
        total_fine = pamt * days
        await db.apply_absence_penalty(uid, ab_id, sd.year, sd.month,
                                        total_fine, message.from_user.id)
        text_ok += f'\nШтраф: −{total_fine:g} ₽ добавлен в расчёт зарплаты.'
    elif pmode == 'no_pay':
        text_ok += '\nДни не засчитываются в отработанное время.'
    await message.answer(text_ok, parse_mode='HTML')
    if tg_id:
        await _notify_user(state, tg_id,
            f'🔴 <b>Зафиксирован прогул</b>\n'
            f'📅 {_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.)\n'
            f'Обратись к администратору за подробностями.')
    await clear_state_keep_org(state)


# ─── Все отсутствия за месяц ──────────────────────────────────────────────────

@absence_router.callback_query(F.data == 'abs_all_month')
async def abs_all_month(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    today = date.today()
    db = await get_db(callback.from_user.id, state)
    rows = await db.get_all_absences_admin(today.year, today.month)
    text = f'<b>📅 Отсутствия: {_MONTH_NAMES[today.month - 1]} {today.year}</b>\n\n'
    if rows:
        for r in rows[:20]:
            ab_id, uid, atype, sd, ed, status, _, comment, _, _, fn, ln, shop = r
            name = f"{fn or ''} {ln or ''}".strip()
            days = _days_count(sd, ed)
            text += (f'<b>{he(name)}</b> ({he(shop or "—")})\n'
                     f'{_TYPE_LABELS.get(atype, atype)}: '
                     f'{_fmt_date(sd)}–{_fmt_date(ed)} ({days} дн.) '
                     f'{_STATUS_LABELS.get(status, status)}\n\n')
    else:
        text += 'Нет отсутствий за этот месяц.\n'
    kb = InlineKeyboardBuilder()
    kb.row(back_button('abs_admin'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')


# ═══════════════════════════════════════════════════════════════════════════════
# НАСТРОЙКИ ТИПОВ
# ═══════════════════════════════════════════════════════════════════════════════

@absence_router.callback_query(F.data == 'abs_cfg')
async def abs_cfg_list(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    db = await get_db(callback.from_user.id, state)
    settings = await db.get_absence_type_settings()
    lines = ['<b>⚙️ Настройки типов отсутствий</b>\n']
    all_types = ['vacation', 'sick', 'compensatory', 'absence', 'other']
    for t in all_types:
        s = settings.get(t, {})
        paid = '💰 Оплачивается' if s.get('is_paid') else '🚫 Без оплаты'
        limit = f'лимит {s.get("annual_limit")} дн./год' \
            if s.get('annual_limit') else 'без лимита'
        pmode = _PENALTY_LABELS.get(s.get('penalty_mode', 'none'), '—')
        lines.append(f'{_TYPE_LABELS[t]}: {paid}, {limit}')
        if t == 'absence':
            lines.append(f'  Прогул: {pmode}')
    text = '\n'.join(lines)
    kb = InlineKeyboardBuilder()
    for t in all_types:
        kb.button(text=_TYPE_LABELS[t], callback_data=f'abs_cfg_t_{t}')
    kb.adjust(2)
    kb.row(back_button('abs_admin'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')


@absence_router.callback_query(F.data.startswith('abs_cfg_t_'))
async def abs_cfg_type(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    atype = callback.data[10:]
    db = await get_db(callback.from_user.id, state)
    settings = await db.get_absence_type_settings()
    s = settings.get(atype, {})
    label = _TYPE_LABELS.get(atype, atype)
    text = (f'<b>⚙️ {label}</b>\n\n'
            f'Оплата: {"💰 Оплачивается" if s.get("is_paid") else "🚫 Без оплаты"}\n'
            f'Лимит в год: {s.get("annual_limit") or "без лимита"} дн.\n'
            f'Прогул — режим: {_PENALTY_LABELS.get(s.get("penalty_mode", "none"), "—")}\n'
            f'Штраф/день: {s.get("penalty_amount") or 0:.0f} ₽\n')
    paid = s.get('is_paid', True)
    kb = InlineKeyboardBuilder()
    kb.button(
        text='✅ Оплачивается' if paid else '💰 Включить оплату',
        callback_data=f'abs_cfg_pay_{atype}_{"0" if paid else "1"}'
    )
    for pm, plabel in _PENALTY_LABELS.items():
        kb.button(text=f'{"✓ " if s.get("penalty_mode") == pm else ""}{plabel}',
                  callback_data=f'abs_cfg_pm_{atype}_{pm}')
    kb.button(text='✏️ Изменить лимит', callback_data=f'abs_cfg_lim_{atype}')
    kb.button(text='✏️ Штраф/день ₽', callback_data=f'abs_cfg_pen_{atype}')
    kb.adjust(1)
    kb.row(back_button('abs_cfg'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')


@absence_router.callback_query(F.data.startswith('abs_cfg_pay_'))
async def abs_cfg_toggle_pay(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    _, _, _, _, atype, val = callback.data.split('_')
    db = await get_db(callback.from_user.id, state)
    settings = await db.get_absence_type_settings()
    s = settings.get(atype, {})
    await db.set_absence_type_setting(
        atype, int(val), s.get('annual_limit', 0),
        s.get('penalty_mode', 'none'), s.get('penalty_amount', 0)
    )
    await callback.answer('Сохранено')
    await abs_cfg_type(callback, state)


@absence_router.callback_query(F.data.startswith('abs_cfg_pm_'))
async def abs_cfg_penalty_mode(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    parts = callback.data.split('_')
    atype = parts[3]
    pmode = parts[4]
    db = await get_db(callback.from_user.id, state)
    settings = await db.get_absence_type_settings()
    s = settings.get(atype, {})
    await db.set_absence_type_setting(
        atype, int(s.get('is_paid', 1)), s.get('annual_limit', 0),
        pmode, s.get('penalty_amount', 0)
    )
    await callback.answer('Сохранено')
    await abs_cfg_type(callback, state)


@absence_router.callback_query(F.data.startswith('abs_cfg_lim_'))
async def abs_cfg_limit_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    atype = callback.data[12:]
    await state.update_data(abs_cfg_type=atype, abs_cfg_field='limit')
    text = (f'✏️ Лимит дней в год для «{_TYPE_LABELS.get(atype, atype)}»\n\n'
            'Введи число (0 = без лимита):')
    kb = InlineKeyboardBuilder()
    kb.row(back_button(f'abs_cfg_t_{atype}'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')
    await state.set_state(AbsenceStates.settings_limit)


@absence_router.message(AbsenceStates.settings_limit)
async def abs_cfg_limit_save(message: Message, state: FSMContext):
    try:
        val = int((message.text or '').strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await message.answer('Введи целое число ≥ 0:')
        return
    data = await state.get_data()
    atype = data.get('abs_cfg_type')
    db = await get_db(message.from_user.id, state)
    settings = await db.get_absence_type_settings()
    s = settings.get(atype, {})
    await db.set_absence_type_setting(
        atype, int(s.get('is_paid', 1)), val,
        s.get('penalty_mode', 'none'), s.get('penalty_amount', 0)
    )
    await message.answer(f'✅ Лимит сохранён: {val} дн./год')
    await clear_state_keep_org(state)


@absence_router.callback_query(F.data.startswith('abs_cfg_pen_'))
async def abs_cfg_penalty_start(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    atype = callback.data[12:]
    await state.update_data(abs_cfg_type=atype, abs_cfg_field='penalty')
    text = (f'✏️ Штраф за день прогула «{_TYPE_LABELS.get(atype, atype)}»\n\n'
            'Введи сумму в ₽ (0 = без штрафа):')
    kb = InlineKeyboardBuilder()
    kb.row(back_button(f'abs_cfg_t_{atype}'), home_button())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')
    await state.set_state(AbsenceStates.settings_penalty)


@absence_router.message(AbsenceStates.settings_penalty)
async def abs_cfg_penalty_save(message: Message, state: FSMContext):
    try:
        val = float((message.text or '').replace(',', '.').strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await message.answer('Введи число ≥ 0:')
        return
    data = await state.get_data()
    atype = data.get('abs_cfg_type')
    db = await get_db(message.from_user.id, state)
    settings = await db.get_absence_type_settings()
    s = settings.get(atype, {})
    await db.set_absence_type_setting(
        atype, int(s.get('is_paid', 1)), s.get('annual_limit', 0),
        s.get('penalty_mode', 'none'), val
    )
    await message.answer(f'✅ Штраф/день: {val:g} ₽')
    await clear_state_keep_org(state)
