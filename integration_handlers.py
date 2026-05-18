"""Handlers for Google Sheets integration setup wizard."""
import json
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db_utils import get_db, clear_state_keep_org, is_any_admin
from keyboards import back_button
from states import IntegrationStates
from integration.manager import AVAILABLE_FIELDS, FIELD_LABELS

integration_router = Router()
logger = logging.getLogger(__name__)

EXPORT_TYPE_LABELS = {
    'sales':     '💰 Продажи',
    'inventory': '📦 Остатки',
    'products':  '🏷 Товары',
    'staff':     '👥 Сотрудники',
    'plans':     '📋 Планы',
}
OPERATION_LABELS = {
    'append_row':    '➕ Добавить строку (append_row)',
    'update_cell':   '✏️ Обновить ячейку (update_cell)',
    'replace_sheet': '🔄 Заменить лист (replace_sheet)',
}
SCHEDULE_LABELS = {
    'immediate': '⚡ Немедленно (по событию)',
    'cron':      '🕒 По расписанию (cron)',
    'disabled':  '❌ Отключено',
}

def _back(cb): return back_button(cb)


# ═══════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data == "integration_menu")
async def integration_menu(callback: CallbackQuery, state: FSMContext):
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)

    connections = current_db.get_integration_connections()
    text = "📊 <b>Интеграция с Google Sheets</b>\n\n"
    if connections:
        text += f"Подключений: {len(connections)}\n\n"
        for c in connections:
            status = "✅" if c[4] else "❌"
            text += f"{status} <b>{c[1]}</b> (id={c[0]})\n"
    else:
        text += "Подключений нет. Создайте первое!\n"
        text += "\nДля работы нужно задать секрет <code>GOOGLE_SERVICE_ACCOUNT_JSON</code> в Replit Secrets."

    kb = InlineKeyboardBuilder()
    for c in connections:
        kb.row(InlineKeyboardButton(
            text=f"⚙️ {c[1]}",
            callback_data=f"gs_conn_{c[0]}"
        ))
    kb.row(InlineKeyboardButton(text="➕ Добавить подключение", callback_data="gs_add_conn"))
    kb.row(_back("admin_management"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  ADD CONNECTION WIZARD
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data == "gs_add_conn")
async def gs_add_conn(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "📊 <b>Новое подключение к Google Sheets</b>\n\nВведите название подключения (например: «Главная таблица»):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back("integration_menu")]]),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_conn_name)


@integration_router.message(IntegrationStates.waiting_conn_name)
async def gs_conn_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Введите название.")
        return
    await state.update_data(gs_conn_name=name)
    await message.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        "Введите <b>ID таблицы Google Sheets</b>.\n"
        "Его можно найти в URL: <code>docs.google.com/spreadsheets/d/<b>ID</b>/edit</code>",
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_spreadsheet_id)


@integration_router.message(IntegrationStates.waiting_spreadsheet_id)
async def gs_spreadsheet_id(message: Message, state: FSMContext):
    spreadsheet_id = message.text.strip()
    if not spreadsheet_id:
        await message.answer("❌ Введите ID таблицы.")
        return

    data = await state.get_data()
    name = data.get('gs_conn_name', 'Подключение')

    await message.answer("⏳ Проверяю подключение…")

    from integration.providers.google_sheets import GoogleSheetsProvider
    provider = GoogleSheetsProvider()
    ok, msg = await provider.test_connection({'spreadsheet_id': spreadsheet_id})

    if not ok:
        await message.answer(
            f"❌ <b>Ошибка подключения:</b>\n<code>{msg}</code>\n\nПроверьте:\n"
            "• ID таблицы верный\n"
            "• Сервисный аккаунт добавлен в таблицу с правами редактора\n"
            "• Переменная <code>GOOGLE_SERVICE_ACCOUNT_JSON</code> задана в секретах\n\n"
            "Попробуйте ввести ID снова или /menu для отмены.",
            parse_mode="HTML"
        )
        return

    current_db = await get_db(message.from_user.id, state)
    conn_id = current_db.add_integration_connection(
        name=name,
        config=json.dumps({'spreadsheet_id': spreadsheet_id, 'provider': 'google_sheets'}),
    )
    await clear_state_keep_org(state)
    await message.answer(
        f"✅ <b>Подключение создано!</b>\n\n"
        f"🔗 {msg}\n\n"
        "Теперь можно добавить экспорт данных.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои подключения", callback_data="integration_menu")],
            [InlineKeyboardButton(text=f"➕ Добавить экспорт", callback_data=f"gs_exports_{conn_id}")],
        ]),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════
#  CONNECTION DETAIL
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_conn_"))
async def gs_conn_detail(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    conn = current_db.get_integration_connection(conn_id)
    if not conn:
        await callback.answer("❌ Подключение не найдено", show_alert=True)
        return

    exports = current_db.get_integration_exports(conn_id)
    cfg = json.loads(conn[3] or '{}')
    status_icon = "✅" if conn[4] else "❌"
    text = (
        f"⚙️ <b>{conn[1]}</b>\n\n"
        f"Статус: {status_icon} {'Активно' if conn[4] else 'Отключено'}\n"
        f"Таблица: <code>{cfg.get('spreadsheet_id', '—')}</code>\n"
        f"Экспортов: {len(exports)}\n"
    )
    if exports:
        text += "\n<b>Экспорты:</b>\n"
        for e in exports:
            e_icon = "✅" if e[2] else "❌"
            text += f"  {e_icon} {EXPORT_TYPE_LABELS.get(e[1], e[1])} → {e[4] or '?'} ({e[5]})\n"

    enabled = bool(conn[4])
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text=f"{'❌ Отключить' if enabled else '✅ Включить'}",
        callback_data=f"gs_toggle_conn_{conn_id}"
    ))
    kb.row(InlineKeyboardButton(text="📋 Экспорты", callback_data=f"gs_exports_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🔍 Тест подключения", callback_data=f"gs_test_conn_{conn_id}"))
    kb.row(InlineKeyboardButton(text="📢 Журнал ошибок", callback_data=f"gs_log_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🗑 Удалить подключение", callback_data=f"gs_del_conn_{conn_id}"))
    kb.row(_back("integration_menu"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_toggle_conn_"))
async def gs_toggle_conn(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    current_db = await get_db(callback.from_user.id, state)
    conn = current_db.get_integration_connection(conn_id)
    if not conn:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    new_enabled = 1 if not conn[4] else 0
    current_db.update_integration_connection(conn_id, enabled=new_enabled)
    await callback.answer("✅ Изменено")
    await gs_conn_detail(callback, state)


@integration_router.callback_query(F.data.startswith("gs_test_conn_"))
async def gs_test_conn(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    await callback.answer("⏳ Проверяю…")
    current_db = await get_db(callback.from_user.id, state)
    conn = current_db.get_integration_connection(conn_id)
    if not conn:
        return
    cfg = json.loads(conn[3] or '{}')
    from integration.providers.google_sheets import GoogleSheetsProvider
    ok, msg = await GoogleSheetsProvider().test_connection(cfg)
    icon = "✅" if ok else "❌"
    await callback.message.answer(f"{icon} {msg}")


@integration_router.callback_query(F.data.startswith("gs_del_conn_"))
async def gs_del_conn_confirm(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    await callback.answer()
    await callback.message.edit_text(
        "🗑 <b>Удалить подключение?</b>\n\nВсе экспорты этого подключения тоже будут удалены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"gs_del_conn_ok_{conn_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"gs_conn_{conn_id}")],
        ]),
        parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_del_conn_ok_"))
async def gs_del_conn_ok(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[4])
    current_db = await get_db(callback.from_user.id, state)
    current_db.delete_integration_connection(conn_id)
    await callback.answer("✅ Удалено")
    await integration_menu(callback, state)


# ═══════════════════════════════════════════════════════════
#  EXPORT LIST
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_exports_"))
async def gs_exports_list(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    exports = current_db.get_integration_exports(conn_id)
    text = "📋 <b>Экспорты</b>\n\n"
    if not exports:
        text += "Экспортов нет.\n"
    else:
        for e in exports:
            icon = "✅" if e[2] else "❌"
            sched = e[3] or 'immediate'
            text += f"{icon} {EXPORT_TYPE_LABELS.get(e[1], e[1])} | {e[4]} | {OPERATION_LABELS.get(e[5], e[5])[:20]}\n"
            text += f"   📅 {SCHEDULE_LABELS.get(sched, sched)}\n\n"

    kb = InlineKeyboardBuilder()
    for e in exports:
        kb.row(InlineKeyboardButton(
            text=f"⚙️ {EXPORT_TYPE_LABELS.get(e[1], e[1])} → {e[4]}",
            callback_data=f"gs_exp_{e[0]}"
        ))
    kb.row(InlineKeyboardButton(
        text="➕ Добавить экспорт",
        callback_data=f"gs_add_exp_{conn_id}"
    ))
    kb.row(_back(f"gs_conn_{conn_id}"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  ADD EXPORT WIZARD
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_add_exp_"))
async def gs_add_exp_start(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    await state.update_data(gs_conn_id=conn_id, gs_mapping={}, gs_mapping_idx=0,
                            gs_lookup={}, gs_lookup_step=0)
    await callback.answer()
    kb = InlineKeyboardBuilder()
    for k, v in EXPORT_TYPE_LABELS.items():
        kb.row(InlineKeyboardButton(text=v, callback_data=f"gs_exp_type_{conn_id}_{k}"))
    kb.row(_back(f"gs_exports_{conn_id}"))
    await callback.message.edit_text(
        "📊 <b>Тип данных для экспорта</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_exp_type_"))
async def gs_exp_type(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    conn_id = int(parts[3])
    exp_type = parts[4]
    await state.update_data(gs_exp_type=exp_type, gs_conn_id=conn_id)
    await callback.answer()
    kb = InlineKeyboardBuilder()
    for k, v in OPERATION_LABELS.items():
        kb.row(InlineKeyboardButton(text=v, callback_data=f"gs_exp_op_{k}"))
    kb.row(_back(f"gs_add_exp_{conn_id}"))
    await callback.message.edit_text(
        f"✅ Тип: {EXPORT_TYPE_LABELS[exp_type]}\n\n<b>Способ записи в таблицу:</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_exp_op_"))
async def gs_exp_op(callback: CallbackQuery, state: FSMContext):
    operation = callback.data.replace("gs_exp_op_", "")
    await state.update_data(gs_exp_op=operation)
    await callback.answer()
    data = await state.get_data()
    exp_type = data.get('gs_exp_type', 'sales')
    conn_id = data.get('gs_conn_id')
    await callback.message.edit_text(
        f"✅ Операция: {OPERATION_LABELS[operation]}\n\n"
        "Введите <b>название листа</b> в таблице (поддерживаются макросы: "
        "<code>{year}</code> <code>{month}</code> <code>{week}</code> <code>{day}</code>).\n"
        "Пример: <code>Продажи {year}-{month}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back(f"gs_exp_type_{conn_id}_{exp_type}")]]),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_export_sheet)


@integration_router.message(IntegrationStates.waiting_export_sheet)
async def gs_export_sheet(message: Message, state: FSMContext):
    sheet = message.text.strip()
    if not sheet:
        await message.answer("❌ Введите название листа.")
        return
    await state.update_data(gs_target_sheet=sheet)
    data = await state.get_data()
    operation = data.get('gs_exp_op', 'append_row')
    exp_type = data.get('gs_exp_type', 'sales')

    if operation == 'append_row':
        await _start_mapping_wizard(message, state, exp_type)
    elif operation == 'update_cell':
        await _start_lookup_wizard(message, state)
    else:
        await _ask_schedule(message, state)


async def _start_mapping_wizard(message: Message, state: FSMContext, exp_type: str):
    fields = AVAILABLE_FIELDS.get(exp_type, [])
    await state.update_data(gs_mapping={}, gs_mapping_fields=fields, gs_mapping_idx=0)
    await _ask_next_mapping_field(message, state)


async def _ask_next_mapping_field(message, state: FSMContext):
    data = await state.get_data()
    fields = data.get('gs_mapping_fields', [])
    idx = data.get('gs_mapping_idx', 0)
    if idx >= len(fields):
        await _ask_schedule(message, state)
        return
    field = fields[idx]
    label = FIELD_LABELS.get(field, field)
    done = len(data.get('gs_mapping', {}))
    total = len(fields)
    await message.answer(
        f"📋 <b>Настройка маппинга ({done}/{total})</b>\n\n"
        f"Поле: <b>{label}</b> (<code>{field}</code>)\n\n"
        f"Введите <b>заголовок столбца</b> в вашей таблице для этого поля.\n"
        f"Или введите <code>skip</code> чтобы пропустить.",
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_mapping)


@integration_router.message(IntegrationStates.waiting_mapping)
async def gs_mapping_field(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    fields = data.get('gs_mapping_fields', [])
    idx = data.get('gs_mapping_idx', 0)
    mapping = data.get('gs_mapping', {})

    if text.lower() != 'skip' and text:
        field = fields[idx]
        mapping[field] = text

    await state.update_data(gs_mapping=mapping, gs_mapping_idx=idx + 1)
    await _ask_next_mapping_field(message, state)


async def _start_lookup_wizard(message: Message, state: FSMContext):
    await state.update_data(gs_lookup={}, gs_lookup_step=0)
    await state.set_state(IntegrationStates.waiting_lookup_step)
    await message.answer(
        "🔍 <b>Настройка поиска строки</b>\n\n"
        "Шаг 1/6: В какой <b>колонке</b> искать строку? Введите номер (1 = A, 2 = B, …):",
        parse_mode="HTML"
    )


LOOKUP_STEPS = [
    ("row_search_col",   "🔍 Шаг 1/6: В какой <b>колонке</b> искать строку? (номер: 1=A, 2=B…)"),
    ("row_search_field", "🔍 Шаг 2/6: Какое <b>поле данных</b> использовать для поиска строки?\n"
                         "Доступные: date, product_name, shop_name, quantity, price, total, seller_name, category"),
    ("col_search_row",   "🔍 Шаг 3/6: В какой <b>строке</b> искать столбец? (номер, обычно 1 — строка заголовков)"),
    ("col_search_field", "🔍 Шаг 4/6: Какое <b>поле данных</b> использовать для поиска столбца?\n"
                         "Пример: product_name (найдёт столбец с именем товара)"),
    ("operation",        "🔍 Шаг 5/6: <b>Операция</b> над ячейкой:\n"
                         "• <code>set</code> — установить значение\n"
                         "• <code>increment</code> — прибавить\n"
                         "• <code>decrement</code> — вычесть"),
    ("value_field",      "🔍 Шаг 6/6: Какое <b>поле данных</b> взять как значение?\n"
                         "Пример: quantity, total, price"),
]


@integration_router.message(IntegrationStates.waiting_lookup_step)
async def gs_lookup_step(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    step = data.get('gs_lookup_step', 0)
    lookup = data.get('gs_lookup', {})

    key = LOOKUP_STEPS[step][0]
    if key == 'row_search_col' or key == 'col_search_row':
        try:
            lookup[key] = int(text)
        except ValueError:
            await message.answer("❌ Введите целое число.")
            return
    else:
        lookup[key] = text

    step += 1
    await state.update_data(gs_lookup=lookup, gs_lookup_step=step)

    if step >= len(LOOKUP_STEPS):
        await _ask_schedule(message, state)
    else:
        await message.answer(LOOKUP_STEPS[step][1], parse_mode="HTML")


async def _ask_schedule(message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=SCHEDULE_LABELS['immediate'], callback_data="gs_sched_immediate"))
    kb.row(InlineKeyboardButton(text=SCHEDULE_LABELS['cron'],      callback_data="gs_sched_cron"))
    kb.row(InlineKeyboardButton(text=SCHEDULE_LABELS['disabled'],  callback_data="gs_sched_disabled"))
    await message.answer(
        "📅 <b>Расписание экспорта:</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_sched_"))
async def gs_sched(callback: CallbackQuery, state: FSMContext):
    sched = callback.data.replace("gs_sched_", "")
    await state.update_data(gs_schedule=sched)
    await callback.answer()
    if sched == 'cron':
        await callback.message.edit_text(
            "🕒 <b>Введите cron-расписание</b>\n\n"
            "Формат: <code>мин час день месяц день_нед</code>\n"
            "Примеры:\n"
            "• <code>0 22 * * *</code> — каждый день в 22:00\n"
            "• <code>0 9 * * 1</code> — каждый понедельник в 09:00\n"
            "• <code>*/30 * * * *</code> — каждые 30 минут",
            parse_mode="HTML"
        )
        await state.set_state(IntegrationStates.waiting_cron)
    else:
        await _save_export(callback.message, state)


@integration_router.message(IntegrationStates.waiting_cron)
async def gs_cron_input(message: Message, state: FSMContext):
    cron_str = message.text.strip()
    parts = cron_str.split()
    if len(parts) != 5:
        await message.answer("❌ Неверный формат. Нужно 5 частей через пробел (мин час день месяц нед).")
        return
    await state.update_data(gs_schedule=cron_str)
    await _save_export(message, state)


async def _save_export(msg, state: FSMContext):
    data = await state.get_data()
    conn_id       = data.get('gs_conn_id')
    exp_type      = data.get('gs_exp_type', 'sales')
    operation     = data.get('gs_exp_op', 'append_row')
    target_sheet  = data.get('gs_target_sheet', 'Sheet1')
    schedule      = data.get('gs_schedule', 'immediate')
    mapping       = data.get('gs_mapping', {})
    lookup        = data.get('gs_lookup', {})

    from db_utils import get_db as _get_db
    current_db = await _get_db(msg.from_user.id if hasattr(msg, 'from_user') else
                                msg.chat.id, state)
    exp_id = current_db.add_integration_export(
        connection_id=conn_id,
        export_type=exp_type,
        operation=operation,
        target_sheet=target_sheet,
        schedule=schedule if schedule != 'disabled' else None,
        enabled=1 if schedule != 'disabled' else 0,
        mapping=json.dumps(mapping, ensure_ascii=False) if mapping else None,
        lookup_config=json.dumps(lookup, ensure_ascii=False) if lookup else None,
    )
    await clear_state_keep_org(state)

    summary = (
        f"✅ <b>Экспорт создан!</b>\n\n"
        f"Тип: {EXPORT_TYPE_LABELS.get(exp_type, exp_type)}\n"
        f"Операция: {OPERATION_LABELS.get(operation, operation)}\n"
        f"Лист: <code>{target_sheet}</code>\n"
        f"Расписание: {SCHEDULE_LABELS.get(schedule, schedule)}\n"
    )
    if mapping:
        summary += "\nМаппинг:\n" + "\n".join(f"  • {k} → {v}" for k, v in mapping.items())

    await msg.answer(
        summary,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 К списку экспортов",
                                  callback_data=f"gs_exports_{conn_id}")],
        ]),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════
#  EXPORT DETAIL
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_exp_"))
async def gs_exp_detail(callback: CallbackQuery, state: FSMContext):
    if "_" not in callback.data.replace("gs_exp_", ""):
        return
    try:
        exp_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    exp = current_db.get_integration_export(exp_id)
    if not exp:
        await callback.answer("❌ Не найдено", show_alert=True)
        return

    conn_id = exp[1]
    status = "✅" if exp[2] else "❌"
    mapping = json.loads(exp[6] or '{}')
    lookup = json.loads(exp[7] or '{}')
    last_run = exp[9] or "Никогда"

    text = (
        f"📊 <b>Экспорт #{exp_id}</b>\n\n"
        f"Тип: {EXPORT_TYPE_LABELS.get(exp[0], exp[0])}\n"
        f"Статус: {status}\n"
        f"Операция: {OPERATION_LABELS.get(exp[5], exp[5])}\n"
        f"Лист: <code>{exp[4]}</code>\n"
        f"Расписание: {SCHEDULE_LABELS.get(exp[3], exp[3] or 'immediate')}\n"
        f"Последний запуск: {last_run}\n"
    )
    if mapping:
        text += "\n<b>Маппинг:</b>\n" + "\n".join(f"  • {k} → {v}" for k, v in mapping.items())
    if lookup:
        text += f"\n<b>Lookup:</b> строка по {lookup.get('row_search_field','?')} в кол.{lookup.get('row_search_col','?')}"

    enabled = bool(exp[2])
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="❌ Отключить" if enabled else "✅ Включить",
        callback_data=f"gs_toggle_exp_{exp_id}"
    ))
    kb.row(InlineKeyboardButton(text="▶️ Тест (запустить сейчас)", callback_data=f"gs_run_exp_{exp_id}"))
    kb.row(InlineKeyboardButton(text="🔍 Проверить маппинг", callback_data=f"gs_check_map_{exp_id}"))
    kb.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gs_del_exp_{exp_id}"))
    kb.row(_back(f"gs_exports_{conn_id}"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_toggle_exp_"))
async def gs_toggle_exp(callback: CallbackQuery, state: FSMContext):
    exp_id = int(callback.data.split("_")[3])
    current_db = await get_db(callback.from_user.id, state)
    exp = current_db.get_integration_export(exp_id)
    if not exp:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    current_db.update_integration_export(exp_id, enabled=0 if exp[2] else 1)
    await callback.answer("✅ Изменено")
    await gs_exp_detail(callback, state)


@integration_router.callback_query(F.data.startswith("gs_run_exp_"))
async def gs_run_exp(callback: CallbackQuery, state: FSMContext):
    exp_id = int(callback.data.split("_")[3])
    await callback.answer("⏳ Запускаю…")
    current_db = await get_db(callback.from_user.id, state)
    exp = current_db.get_integration_export(exp_id)
    if not exp:
        return
    conn = current_db.get_integration_connection(exp[1])
    if not conn:
        return
    export_row = (
        exp_id, exp[1], exp[0], exp[3], exp[4], exp[5], exp[6], exp[7],
        conn[3]
    )
    from integration.manager import integration_manager
    import asyncio
    asyncio.create_task(integration_manager._run_export(current_db, export_row, {}))
    await callback.message.answer("▶️ Экспорт запущен в фоне. Проверьте журнал через минуту.")


@integration_router.callback_query(F.data.startswith("gs_check_map_"))
async def gs_check_map(callback: CallbackQuery, state: FSMContext):
    exp_id = int(callback.data.split("_")[3])
    await callback.answer("⏳ Читаю заголовки…")
    current_db = await get_db(callback.from_user.id, state)
    exp = current_db.get_integration_export(exp_id)
    if not exp:
        return
    conn = current_db.get_integration_connection(exp[1])
    if not conn:
        return
    cfg = json.loads(conn[3] or '{}')
    mapping = json.loads(exp[6] or '{}')

    from integration.providers.google_sheets import GoogleSheetsProvider
    headers = await GoogleSheetsProvider().get_headers(cfg, exp[4])
    if not headers:
        await callback.message.answer("❌ Не удалось прочитать заголовки. Проверьте название листа и доступ.")
        return

    text = f"📋 <b>Проверка маппинга</b>\nЛист: <code>{exp[4]}</code>\n\nЗаголовки в таблице:\n"
    text += ", ".join(f"<code>{h}</code>" for h in headers) + "\n\n"
    text += "<b>Ваш маппинг:</b>\n"
    for field, col_header in mapping.items():
        found = col_header in headers
        icon = "✅" if found else "❌"
        text += f"  {icon} {field} → {col_header}\n"

    missing = [v for v in mapping.values() if v not in headers]
    if missing:
        text += f"\n⚠️ Не найдены столбцы: {', '.join(missing)}"
    else:
        text += "\n✅ Все столбцы найдены!"

    await callback.message.answer(text, parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_del_exp_"))
async def gs_del_exp(callback: CallbackQuery, state: FSMContext):
    exp_id = int(callback.data.split("_")[3])
    current_db = await get_db(callback.from_user.id, state)
    exp = current_db.get_integration_export(exp_id)
    if not exp:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    conn_id = exp[1]
    current_db.delete_integration_export(exp_id)
    await callback.answer("✅ Удалено")
    # Redirect to export list
    callback.data = f"gs_exports_{conn_id}"
    await gs_exports_list(callback, state)


# ═══════════════════════════════════════════════════════════
#  ERROR LOG
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_log_"))
async def gs_log(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    logs = current_db.get_integration_logs(conn_id, limit=15)
    text = "📢 <b>Журнал интеграции</b>\n\n"
    if not logs:
        text += "Записей нет."
    else:
        for log in logs:
            icon = "✅" if log[3] == 'success' else "❌"
            text += f"{icon} <b>exp#{log[2]}</b> [{log[4][:80]}]\n<i>{log[5]}</i>\n\n"
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back(f"gs_conn_{conn_id}")]]),
        parse_mode="HTML"
    )
