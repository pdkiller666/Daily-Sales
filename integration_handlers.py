"""Handlers for Google Sheets integration: OAuth Device Flow + export wizard + motivation sync."""
import asyncio
import json
import logging
import os
import time

from aiogram import Router, F
from aiogram.types import (CallbackQuery, Message,
                           InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db_utils import get_db, clear_state_keep_org, is_any_admin
from keyboards import back_button
from states import IntegrationStates
from integration.manager import AVAILABLE_FIELDS, FIELD_LABELS, integration_manager
from message_utils import fsm_edit, delete_message_safe

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
    'append_row':    '➕ Добавить строку',
    'update_cell':   '✏️ Обновить ячейку (матрица)',
    'replace_sheet': '🔄 Заменить весь лист',
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

    oauth_ready = bool(
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    )

    text = "📊 <b>Интеграция с Google Sheets</b>\n\n"
    if not oauth_ready:
        text += (
            "⚠️ <b>OAuth не настроен.</b> Для подключения через личный Google-аккаунт "
            "системный администратор должен задать секреты:\n"
            "• <code>GOOGLE_OAUTH_CLIENT_ID</code>\n"
            "• <code>GOOGLE_OAUTH_CLIENT_SECRET</code>\n\n"
        )

    if connections:
        text += f"Подключений: {len(connections)}\n\n"
        for c in connections:
            cfg = json.loads(c[3] or '{}')
            auth_icon = "🔑" if cfg.get("auth_type") == "oauth" else "⚙️"
            status = "✅" if c[4] else "❌"
            text += f"{status} {auth_icon} <b>{c[1]}</b>\n"
    else:
        text += "Подключений нет. Создайте первое!\n"

    kb = InlineKeyboardBuilder()
    for c in connections:
        kb.row(InlineKeyboardButton(
            text=f"⚙️ {c[1]}",
            callback_data=f"gs_conn_{c[0]}"
        ))
    kb.row(InlineKeyboardButton(text="➕ Добавить подключение", callback_data="gs_add_conn"))
    if not oauth_ready:
        kb.row(InlineKeyboardButton(
            text="📖 Как подключить? (инструкция)",
            callback_data="gs_guide_1"
        ))
    else:
        kb.row(InlineKeyboardButton(
            text="📖 Инструкция по настройке",
            callback_data="gs_guide_1"
        ))
    kb.row(_back("admin_management"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  SETUP GUIDE (6 шагов для супер-администратора)
# ═══════════════════════════════════════════════════════════

_GUIDE_TOTAL = 6

def _guide_kb(step: int, back_to: str = "integration_menu") -> InlineKeyboardMarkup:
    """Navigation keyboard for setup guide steps."""
    rows = []
    nav = []
    if step > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"gs_guide_{step - 1}"))
    if step < _GUIDE_TOTAL:
        nav.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"gs_guide_{step + 1}"))
    else:
        nav.append(InlineKeyboardButton(text="✅ Проверить секреты", callback_data="gs_check_secrets"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 В меню интеграций", callback_data=back_to)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_GUIDE_STEPS = {
    1: (
        "📖 <b>Настройка Google Sheets — Шаг 1 из 6</b>\n"
        "<b>Создайте проект в Google Cloud</b>\n\n"
        "1. Откройте <a href=\"https://console.cloud.google.com\">console.cloud.google.com</a>\n"
        "   (можно войти с <b>любого</b> Google-аккаунта)\n\n"
        "2. Вверху нажмите на название проекта → <b>Новый проект</b>\n\n"
        "3. Название: <code>DailySalesBot</code> → <b>Создать</b>\n\n"
        "4. Убедитесь, что новый проект выбран в верхнем меню\n\n"
        "✅ Готово → нажмите <b>Далее</b>"
    ),
    2: (
        "📖 <b>Настройка Google Sheets — Шаг 2 из 6</b>\n"
        "<b>Включите нужные API</b>\n\n"
        "В поиске вверху найдите и включите два API:\n\n"
        "1. Введите <b>Google Sheets API</b> → откройте → нажмите <b>Включить</b>\n\n"
        "2. Введите <b>Google Drive API</b> → откройте → нажмите <b>Включить</b>\n\n"
        "⚠️ Оба API обязательны — без Drive API бот не сможет искать файлы.\n\n"
        "✅ Оба включены → нажмите <b>Далее</b>"
    ),
    3: (
        "📖 <b>Настройка Google Sheets — Шаг 3 из 6</b>\n"
        "<b>Настройте экран согласия OAuth</b>\n\n"
        "1. Левое меню → <b>API и сервисы → Экран согласия OAuth</b>\n\n"
        "2. Выберите <b>Внешний (External)</b> → <b>Создать</b>\n\n"
        "3. Заполните:\n"
        "   • Название приложения: <code>DailySalesBot</code>\n"
        "   • Email поддержки: ваш email\n"
        "   • Email разработчика: ваш email\n\n"
        "4. Нажимайте <b>Сохранить и продолжить</b> до шага <b>Тестовые пользователи</b>\n\n"
        "5. ⚠️ <b>Добавьте тестового пользователя</b> — введите email того Google-аккаунта,\n"
        "   у которого есть доступ к таблице (например: <code>promo.brn.tarasov.i@gmail.com</code>)\n\n"
        "6. <b>Сохранить и продолжить</b> → <b>Вернуться на панель</b>\n\n"
        "⚠️ Без этого шага Google заблокирует авторизацию!\n\n"
        "✅ Готово → нажмите <b>Далее</b>"
    ),
    4: (
        "📖 <b>Настройка Google Sheets — Шаг 4 из 6</b>\n"
        "<b>Создайте OAuth-ключи приложения</b>\n\n"
        "1. Левое меню → <b>API и сервисы → Учётные данные (Credentials)</b>\n\n"
        "2. Нажмите <b>+ Создать учётные данные → OAuth 2.0 Client ID</b>\n\n"
        "3. Тип приложения: <b>«Телевизоры и устройства с ограниченным вводом»</b>\n"
        "   (TV and Limited Input devices)\n"
        "   ⚠️ Именно этот тип — он позволяет авторизоваться без браузера на сервере\n\n"
        "4. Название: <code>DailySalesBot</code> → <b>Создать</b>\n\n"
        "5. Откроется окно — скопируйте:\n"
        "   • <b>Client ID</b> (длинная строка, оканчивается на <code>.apps.googleusercontent.com</code>)\n"
        "   • <b>Client Secret</b> (короткая строка)\n\n"
        "💾 Сохраните оба значения — они понадобятся на следующем шаге.\n\n"
        "✅ Ключи скопированы → нажмите <b>Далее</b>"
    ),
    5: (
        "📖 <b>Настройка Google Sheets — Шаг 5 из 6</b>\n"
        "<b>Добавьте секреты в Replit</b>\n\n"
        "1. В Replit откройте панель <b>🔒 Secrets</b> (иконка замка слева)\n\n"
        "2. Добавьте два секрета:\n\n"
        "   Ключ: <code>GOOGLE_OAUTH_CLIENT_ID</code>\n"
        "   Значение: вставьте <b>Client ID</b> из шага 4\n\n"
        "   Ключ: <code>GOOGLE_OAUTH_CLIENT_SECRET</code>\n"
        "   Значение: вставьте <b>Client Secret</b> из шага 4\n\n"
        "3. После добавления секретов — <b>перезапустите бота</b>\n"
        "   (вкладка Workflows → Start application → Restart)\n\n"
        "✅ Секреты добавлены, бот перезапущен → нажмите <b>Далее</b>"
    ),
    6: (
        "📖 <b>Настройка Google Sheets — Шаг 6 из 6</b>\n"
        "<b>Подключите таблицу через бота</b>\n\n"
        "1. Вернитесь в <b>📊 Google Sheets</b> → <b>➕ Добавить подключение</b>\n\n"
        "2. Введите название, например: <code>Таблица Huawei</code>\n\n"
        "3. Выберите <b>🔑 OAuth — мой аккаунт Google</b>\n\n"
        "4. Введите ID таблицы из URL:\n"
        "   URL: <code>docs.google.com/spreadsheets/d/<b>ВОТ_ЭТО</b>/edit</code>\n\n"
        "5. Бот пришлёт ссылку и код, например:\n"
        "   🔗 <code>https://google.com/device</code>\n"
        "   🔑 Код: <code>ABCD-EFGH</code>\n\n"
        "6. Откройте ссылку → войдите в аккаунт с доступом к таблице\n"
        "   → введите код → нажмите <b>Разрешить</b>\n\n"
        "7. Бот напишет <b>«✅ Google аккаунт подключён!»</b> — готово!\n\n"
        "После подключения настройте экспорт и синхронизацию мотивации.\n\n"
        "👇 Нажмите <b>Проверить секреты</b>, чтобы убедиться что всё готово:"
    ),
}


@integration_router.callback_query(F.data.regexp(r'^gs_guide_\d+$'))
async def gs_guide_step(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        step = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        step = 1
    step = max(1, min(step, _GUIDE_TOTAL))
    text = _GUIDE_STEPS[step]
    header = f"<i>Шаг {step} из {_GUIDE_TOTAL}</i>\n\n" if "Шаг" not in text[:20] else ""
    await callback.message.edit_text(
        header + text,
        reply_markup=_guide_kb(step),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@integration_router.callback_query(F.data == "gs_check_secrets")
async def gs_check_secrets(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

    ok_id = bool(client_id and len(client_id) > 10)
    ok_secret = bool(client_secret and len(client_secret) > 4)

    id_icon = "✅" if ok_id else "❌"
    sec_icon = "✅" if ok_secret else "❌"

    if ok_id and ok_secret:
        status_text = (
            "🎉 <b>Всё готово!</b>\n\n"
            f"{id_icon} <code>GOOGLE_OAUTH_CLIENT_ID</code> — задан\n"
            f"{sec_icon} <code>GOOGLE_OAUTH_CLIENT_SECRET</code> — задан\n\n"
            "Теперь нажмите <b>➕ Добавить подключение</b> и выберите\n"
            "<b>🔑 OAuth — мой аккаунт Google</b>."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить подключение", callback_data="gs_add_conn")],
            [InlineKeyboardButton(text="🏠 В меню интеграций", callback_data="integration_menu")],
        ])
    else:
        missing = []
        if not ok_id:
            missing.append("• <code>GOOGLE_OAUTH_CLIENT_ID</code>")
        if not ok_secret:
            missing.append("• <code>GOOGLE_OAUTH_CLIENT_SECRET</code>")
        status_text = (
            "⚠️ <b>Секреты не найдены</b>\n\n"
            f"{id_icon} <code>GOOGLE_OAUTH_CLIENT_ID</code>\n"
            f"{sec_icon} <code>GOOGLE_OAUTH_CLIENT_SECRET</code>\n\n"
            "Не хватает:\n" + "\n".join(missing) + "\n\n"
            "Вернитесь к <b>Шагу 5</b> — добавьте секреты в Replit Secrets\n"
            "и перезапустите бота, затем нажмите «Проверить снова»."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить снова", callback_data="gs_check_secrets")],
            [InlineKeyboardButton(text="◀️ Шаг 5", callback_data="gs_guide_5")],
            [InlineKeyboardButton(text="🏠 В меню интеграций", callback_data="integration_menu")],
        ])

    await callback.message.edit_text(status_text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  ADD CONNECTION — STEP 1: NAME
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data == "gs_add_conn")
async def gs_add_conn(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "📊 <b>Новое подключение к Google Sheets</b>\n\n"
        "Введите <b>название</b> подключения (например: «Главная таблица»):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_back("integration_menu")]]
        ),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_conn_name)


def _auth_type_kb(oauth_ready: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if oauth_ready:
        kb.row(InlineKeyboardButton(
            text="🔑 OAuth — мой аккаунт Google (рекомендуется)",
            callback_data="gs_auth_oauth"
        ))
    kb.row(InlineKeyboardButton(
        text="⚙️ Сервисный аккаунт (JSON-ключ)",
        callback_data="gs_auth_sa"
    ))
    kb.row(_back("gs_add_conn"))
    return kb.as_markup()


@integration_router.message(IntegrationStates.waiting_conn_name)
async def gs_conn_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await delete_message_safe(message)
        return
    await state.update_data(gs_conn_name=name)

    oauth_ready = bool(
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    )

    await fsm_edit(
        state, message,
        f"📊 <b>Новое подключение к Google Sheets</b>\n\n"
        f"Название: <b>{name}</b>\n\n"
        "<b>Выберите способ авторизации:</b>\n\n"
        "🔑 <b>OAuth</b> — вход через личный Google-аккаунт. "
        "Один раз перейдите по ссылке и введите код.\n\n"
        "⚙️ <b>Сервисный аккаунт</b> — для продвинутых. "
        "Нужен JSON-ключ из Google Cloud Console.",
        reply_markup=_auth_type_kb(oauth_ready),
    )


# ═══════════════════════════════════════════════════════════
#  PATH A: SERVICE ACCOUNT
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data == "gs_auth_sa")
async def gs_auth_sa(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(
        gs_auth_type="service_account",
        anchor_msg_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "⚙️ <b>Сервисный аккаунт</b>\n\n"
        "Введите <b>ID таблицы Google Sheets</b>.\n"
        "URL: <code>docs.google.com/spreadsheets/d/<b>ID</b>/edit</code>\n\n"
        "Убедитесь, что сервисный аккаунт добавлен в таблицу как <b>редактор</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back("gs_add_conn")]]),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_spreadsheet_id)


# ═══════════════════════════════════════════════════════════
#  PATH B: OAUTH DEVICE FLOW
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data == "gs_auth_oauth")
async def gs_auth_oauth(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(
        gs_auth_type="oauth",
        anchor_msg_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "🔑 <b>OAuth — вход через Google</b>\n\n"
        "Введите <b>ID таблицы Google Sheets</b>.\n"
        "URL: <code>docs.google.com/spreadsheets/d/<b>ID</b>/edit</code>\n\n"
        "Можно вставить полную ссылку — ID извлечётся автоматически.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back("gs_add_conn")]]),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_spreadsheet_id)


def _extract_spreadsheet_id(text: str) -> str:
    """Extract spreadsheet ID from a full Google Sheets URL or return as-is."""
    import re
    text = text.strip()
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', text)
    if m:
        return m.group(1)
    return text


async def _edit_anchor(bot, chat_id: int, anchor_id: int,
                       text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Edit anchor message in background tasks where state/message are not available."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=anchor_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception as e:
        logger.error(f"_edit_anchor error: {e}")
        try:
            await bot.send_message(chat_id, text,
                                   reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass


async def _fsm_edit(message: Message, state: FSMContext,
                    text: str, reply_markup=None):
    """In message handlers: delete user message, then edit the anchor stored in FSM state."""
    await delete_message_safe(message)
    data = await state.get_data()
    anchor_id = data.get('anchor_msg_id')
    if anchor_id:
        await _edit_anchor(message.bot, message.chat.id, anchor_id,
                           text, reply_markup=reply_markup)
    else:
        sent = await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
        await state.update_data(anchor_msg_id=sent.message_id)


@integration_router.message(IntegrationStates.waiting_spreadsheet_id)
async def gs_spreadsheet_id_handler(message: Message, state: FSMContext):
    """Unified handler for spreadsheet_id — routes to OAuth or service account."""
    spreadsheet_id = _extract_spreadsheet_id(message.text or "")
    if not spreadsheet_id:
        await delete_message_safe(message)
        return

    data = await state.get_data()
    auth_type = data.get('gs_auth_type', 'service_account')
    name = data.get('gs_conn_name', 'Подключение')

    if auth_type == 'oauth':
        await state.update_data(gs_spreadsheet_id_pending=spreadsheet_id)
        await _start_device_flow(message, state)
    else:
        # Service account — show loading in anchor, test, show result
        await fsm_edit(state, message, "⏳ <b>Проверяю подключение…</b>")
        from integration.providers.google_sheets import GoogleSheetsProvider
        ok, msg = await GoogleSheetsProvider().test_connection({
            'spreadsheet_id': spreadsheet_id,
            'auth_type': 'service_account',
        })
        if not ok:
            data2 = await state.get_data()
            anchor_id = data2.get('anchor_msg_id')
            kb = InlineKeyboardMarkup(inline_keyboard=[[_back("gs_auth_sa")]])
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=anchor_id,
                    text=(
                        f"❌ <b>Ошибка подключения:</b>\n<code>{msg}</code>\n\n"
                        "Проверьте:\n• ID таблицы верный\n"
                        "• Переменная <code>GOOGLE_SERVICE_ACCOUNT_JSON</code> задана"
                    ),
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        current_db = await get_db(message.from_user.id, state)
        conn_id = current_db.add_integration_connection(
            name=name,
            config=json.dumps({
                'spreadsheet_id': spreadsheet_id,
                'provider': 'google_sheets',
                'auth_type': 'service_account',
            }),
        )
        data2 = await state.get_data()
        anchor_id = data2.get('anchor_msg_id')
        await clear_state_keep_org(state)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои подключения",
                                  callback_data="integration_menu")],
            [InlineKeyboardButton(text="➕ Добавить экспорт",
                                  callback_data=f"gs_exports_{conn_id}")],
        ])
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=anchor_id,
                text=f"✅ <b>Подключение создано!</b>\n\n{msg}",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            await message.answer(
                f"✅ <b>Подключение создано!</b>\n\n{msg}",
                reply_markup=kb, parse_mode="HTML",
            )


async def _start_device_flow(message: Message, state: FSMContext):
    """
    Initiate OAuth Device Flow. Deletes user message, edits anchor with code.
    Background polling task edits the same anchor on completion.
    """
    from integration.auth.google_oauth import initiate_device_flow

    # Delete the user's spreadsheet ID message
    await delete_message_safe(message)

    data = await state.get_data()
    anchor_id = data.get('anchor_msg_id')
    chat_id = message.chat.id

    # Show loading in anchor while calling Google
    try:
        await message.bot.edit_message_text(
            chat_id=chat_id, message_id=anchor_id,
            text="⏳ <b>Запускаю авторизацию Google…</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        flow = await initiate_device_flow()
    except ValueError as e:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id, message_id=anchor_id,
                text=f"❌ <b>Ошибка запуска OAuth:</b> {e}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back("gs_auth_oauth")]]),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    device_code = flow['device_code']
    user_code = flow['user_code']
    verification_url = flow.get('verification_url', 'https://google.com/device')
    expires_in = flow.get('expires_in', 900)
    interval = flow.get('interval', 5)

    await state.update_data(
        gs_device_code=device_code,
        gs_oauth_interval=interval,
        gs_oauth_expires=time.time() + expires_in,
    )
    await state.set_state(IntegrationStates.waiting_oauth_poll)

    auth_text = (
        f"🔑 <b>Авторизация Google</b>\n\n"
        f"1. Нажмите кнопку ниже — откроется страница Google\n\n"
        f"2. Введите этот код (нажмите чтобы скопировать):\n"
        f"<code>{user_code}</code>\n\n"
        f"⏳ Ожидаю авторизации… (до {expires_in // 60} мин)\n\n"
        f"После авторизации бот обновит это сообщение автоматически."
    )
    auth_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть google.com/device", url=verification_url)],
    ])
    try:
        await message.bot.edit_message_text(
            chat_id=chat_id, message_id=anchor_id,
            text=auth_text, parse_mode="HTML",
            reply_markup=auth_kb,
        )
    except Exception:
        sent = await message.answer(auth_text, parse_mode="HTML", reply_markup=auth_kb)
        anchor_id = sent.message_id
        await state.update_data(anchor_msg_id=anchor_id)

    asyncio.create_task(
        _poll_oauth_token(chat_id, anchor_id, device_code, interval,
                          expires_in, state, data, message.bot)
    )


@integration_router.message(IntegrationStates.waiting_oauth_poll)
async def gs_oauth_poll_message(message: Message, state: FSMContext):
    """User sends a message while OAuth Device Flow is in progress — delete and ignore."""
    await delete_message_safe(message)


async def _poll_oauth_token(chat_id: int, anchor_id: int,
                             device_code: str, interval: int,
                             expires_in: int, state: FSMContext, data: dict, bot):
    """Background task: poll Google for OAuth token, edit anchor on result."""
    from integration.auth.google_oauth import poll_for_token

    deadline = time.time() + expires_in

    while time.time() < deadline:
        await asyncio.sleep(interval)
        try:
            token_data = await poll_for_token(device_code)
        except ValueError as e:
            await _edit_anchor(
                bot, chat_id, anchor_id,
                f"❌ <b>Ошибка авторизации:</b> {e}\n\nПопробуйте подключить снова.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Попробовать снова",
                                         callback_data="gs_add_conn")],
                    [_back("integration_menu")],
                ]),
            )
            return
        except Exception as e:
            logger.error(f"OAuth poll error: {e}")
            continue

        if token_data is None:
            continue

        # Success — save connection
        try:
            name = data.get('gs_conn_name', 'Google Sheets')
            spreadsheet_id = data.get('gs_spreadsheet_id_pending', '')

            if not spreadsheet_id:
                await _edit_anchor(
                    bot, chat_id, anchor_id,
                    "❌ spreadsheet_id не найден. Начните добавление подключения заново.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[_back("integration_menu")]]
                    ),
                )
                return

            conn_config = {
                'spreadsheet_id': spreadsheet_id,
                'provider': 'google_sheets',
                'auth_type': 'oauth',
                'tokens': {
                    'access_token':  token_data.get('access_token', ''),
                    'refresh_token': token_data.get('refresh_token', ''),
                    'expiry':        token_data.get('expiry', time.time() + 3600),
                },
            }

            from db_utils import get_db as _get_db
            current_db = await _get_db(chat_id, state)
            conn_id = current_db.add_integration_connection(
                name=name,
                config=json.dumps(conn_config),
            )
            await clear_state_keep_org(state)

            from integration.providers.google_sheets import GoogleSheetsProvider
            ok, test_msg = await GoogleSheetsProvider().test_connection(conn_config)

            await _edit_anchor(
                bot, chat_id, anchor_id,
                f"✅ <b>Google аккаунт подключён!</b>\n\n"
                f"{test_msg}\n\n"
                f"Теперь настройте экспорт данных.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="📋 Мои подключения",
                        callback_data="integration_menu"
                    )],
                    [InlineKeyboardButton(
                        text="➕ Добавить экспорт",
                        callback_data=f"gs_exports_{conn_id}"
                    )],
                    [InlineKeyboardButton(
                        text="🔄 Синхронизировать мотивацию",
                        callback_data=f"gs_sync_motiv_{conn_id}"
                    )],
                ]),
            )
        except Exception as e:
            logger.error(f"OAuth save connection error: {e}")
            await _edit_anchor(
                bot, chat_id, anchor_id,
                f"❌ Авторизация прошла, но не удалось сохранить подключение:\n{e}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[_back("integration_menu")]]
                ),
            )
        return

    await _edit_anchor(
        bot, chat_id, anchor_id,
        "⏰ <b>Время авторизации истекло.</b>\n\nНачните добавление подключения заново.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать снова",
                                  callback_data="gs_add_conn")],
            [_back("integration_menu")],
        ]),
    )


# ═══════════════════════════════════════════════════════════
#  CONNECTION DETAIL
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_conn_"))
async def gs_conn_detail(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    current_db = await get_db(callback.from_user.id, state)
    conn = current_db.get_integration_connection(conn_id)
    if not conn:
        await callback.answer("❌ Подключение не найдено", show_alert=True)
        return
    await callback.answer()

    exports = current_db.get_integration_exports(conn_id)
    cfg = json.loads(conn[3] or '{}')
    status_icon = "✅" if conn[4] else "❌"
    auth_label = ("🔑 OAuth (личный аккаунт)"
                  if cfg.get("auth_type") == "oauth"
                  else "⚙️ Сервисный аккаунт")
    tokens_ok = ""
    if cfg.get("auth_type") == "oauth":
        expiry = cfg.get("tokens", {}).get("expiry", 0)
        if expiry:
            from datetime import datetime
            exp_dt = datetime.fromtimestamp(expiry).strftime("%d.%m %H:%M")
            tokens_ok = f"\nТокен до: {exp_dt}"

    text = (
        f"⚙️ <b>{conn[1]}</b>\n\n"
        f"Статус: {status_icon} {'Активно' if conn[4] else 'Отключено'}\n"
        f"Авторизация: {auth_label}{tokens_ok}\n"
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
        text="❌ Отключить" if enabled else "✅ Включить",
        callback_data=f"gs_toggle_conn_{conn_id}"
    ))
    kb.row(InlineKeyboardButton(text="📋 Экспорты", callback_data=f"gs_exports_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🔍 Тест подключения", callback_data=f"gs_test_conn_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🔄 Синхронизировать мотивацию",
                                callback_data=f"gs_sync_motiv_{conn_id}"))
    kb.row(InlineKeyboardButton(text="📊 Кэш мотивации",
                                callback_data=f"gs_show_motiv_{conn_id}"))
    kb.row(InlineKeyboardButton(text="📢 Журнал событий", callback_data=f"gs_log_{conn_id}"))
    if cfg.get("auth_type") == "oauth":
        kb.row(InlineKeyboardButton(text="🔑 Переавторизовать Google",
                                    callback_data=f"gs_reauth_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gs_del_conn_{conn_id}"))
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
    try:
        cfg = await integration_manager._ensure_valid_token(current_db, conn_id, cfg)
    except Exception:
        pass
    from integration.providers.google_sheets import GoogleSheetsProvider
    ok, msg = await GoogleSheetsProvider().test_connection(cfg)
    icon = "✅" if ok else "❌"
    await callback.message.edit_text(
        f"{icon} {msg}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back(f"gs_conn_{conn_id}")]]),
        parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_reauth_"))
async def gs_reauth(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    conn = current_db.get_integration_connection(conn_id)
    if not conn:
        return
    cfg = json.loads(conn[3] or '{}')
    await state.update_data(
        gs_conn_name=conn[1],
        gs_auth_type='oauth',
        gs_spreadsheet_id_pending=cfg.get('spreadsheet_id', ''),
        gs_reauth_conn_id=conn_id,
    )
    await _start_device_flow(callback.message, state)


@integration_router.callback_query(F.data.startswith("gs_del_conn_"))
async def gs_del_conn_confirm(callback: CallbackQuery, state: FSMContext):
    if "_ok_" in callback.data:
        conn_id = int(callback.data.split("_ok_")[1])
        current_db = await get_db(callback.from_user.id, state)
        current_db.delete_integration_connection(conn_id)
        await callback.answer("✅ Удалено")
        await integration_menu(callback, state)
        return
    conn_id = int(callback.data.split("_")[3])
    await callback.answer()
    await callback.message.edit_text(
        "🗑 <b>Удалить подключение?</b>\n\nВсе экспорты и кэш мотивации будут удалены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить",
                                  callback_data=f"gs_del_conn_ok_{conn_id}")],
            [InlineKeyboardButton(text="❌ Отмена",
                                  callback_data=f"gs_conn_{conn_id}")],
        ]),
        parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_log_"))
async def gs_log(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    logs = current_db.get_integration_logs(conn_id, limit=10)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[_back(f"gs_conn_{conn_id}")]])
    if not logs:
        await callback.message.edit_text(
            "📢 <b>Журнал пуст.</b>",
            reply_markup=back_kb, parse_mode="HTML"
        )
        return
    lines = []
    for log in logs:
        icon = "✅" if log[3] == "success" else "❌"
        lines.append(f"{icon} {log[5][:16]} — {log[4][:80]}")
    await callback.message.edit_text(
        "📢 <b>Последние события:</b>\n\n" + "\n".join(lines),
        reply_markup=back_kb, parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════
#  MOTIVATION SYNC
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_sync_motiv_"))
async def gs_sync_motiv_start(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    current_db = await get_db(callback.from_user.id, state)
    conn = current_db.get_integration_connection(conn_id)
    if not conn:
        await callback.answer("❌ Подключение не найдено", show_alert=True)
        return
    await callback.answer()

    await state.update_data(gs_motiv_conn_id=conn_id,
                            anchor_msg_id=callback.message.message_id)
    now_week = __import__('datetime').datetime.now().isocalendar()[1]

    await callback.message.edit_text(
        f"🔄 <b>Синхронизация мотивации</b>\n\n"
        f"Укажите <b>название листа</b>, где менеджер прописывает бонусы.\n\n"
        f"Поддерживаются макросы:\n"
        f"• <code>w{{week}}</code> → текущая неделя (сейчас: <code>w{now_week}</code>)\n"
        f"• <code>{{year}}</code>, <code>{{month}}</code>\n\n"
        f"Пример: <code>w{{week}}</code>\n\n"
        f"Введите название листа:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_back(f"gs_conn_{conn_id}")]]
        ),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_motiv_sheet)


@integration_router.message(IntegrationStates.waiting_motiv_sheet)
async def gs_motiv_sheet_input(message: Message, state: FSMContext):
    sheet = message.text.strip()
    data = await state.get_data()
    conn_id = data.get('gs_motiv_conn_id')
    if not sheet:
        await _fsm_edit(message, state, "❌ Введите название листа.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[_back(f"gs_conn_{conn_id}")]]))
        return
    await state.update_data(gs_motiv_sheet=sheet)
    await _fsm_edit(
        message, state,
        f"✅ Лист: <code>{sheet}</code>\n\n"
        f"<b>Параметры структуры листа</b>\n\n"
        f"Введите через пробел 4 числа:\n"
        f"<code>строка_заголовков  строка_DNS  строка_MVM  первый_столбец_моделей</code>\n\n"
        f"Для вашего листа w{{week}} стандартные значения:\n"
        f"<code>6 2 3 14</code>\n\n"
        f"Отправьте <code>6 2 3 14</code> или введите свои значения:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_back(f"gs_conn_{conn_id}")]])
    )
    await state.set_state(IntegrationStates.waiting_motiv_rows)


@integration_router.message(IntegrationStates.waiting_motiv_rows)
async def gs_motiv_rows_input(message: Message, state: FSMContext):
    data = await state.get_data()
    conn_id = data.get('gs_motiv_conn_id')
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[_back(f"gs_conn_{conn_id}")]])

    parts = message.text.strip().split()
    if len(parts) != 4:
        await _fsm_edit(message, state, "❌ Введите ровно 4 числа через пробел.",
                        reply_markup=back_kb)
        return
    try:
        header_row, dns_row, mvm_row, model_start_col = [int(p) for p in parts]
    except ValueError:
        await _fsm_edit(message, state, "❌ Все значения должны быть целыми числами.",
                        reply_markup=back_kb)
        return

    sheet = data.get('gs_motiv_sheet', 'w{week}')
    bot = message.bot
    chat_id = message.chat.id
    anchor_id = data.get('anchor_msg_id')

    await _fsm_edit(message, state, "⏳ Читаю лист и синхронизирую мотивацию…")

    current_db = await get_db(message.from_user.id, state)
    try:
        result = await integration_manager.sync_motivation_from_sheet(
            current_db, conn_id, sheet,
            header_row=header_row,
            dns_row=dns_row,
            mvm_row=mvm_row,
            rrp_row=dns_row - 0 + 2 if dns_row == 2 else 4,
            model_start_col=model_start_col,
        )
        synced = result['synced']
        actual_sheet = result['sheet']
        models = result['models']

        models_preview = ", ".join(models[:8])
        if len(models) > 8:
            models_preview += f" … ещё {len(models) - 8}"

        await _edit_anchor(
            bot, chat_id, anchor_id,
            f"✅ <b>Мотивация синхронизирована!</b>\n\n"
            f"📋 Лист: <code>{actual_sheet}</code>\n"
            f"🔢 Моделей: <b>{synced}</b>\n"
            f"📦 {models_preview}\n\n"
            f"Просмотреть кэш: кнопка «📊 Кэш мотивации» в подключении.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Кэш мотивации",
                                      callback_data=f"gs_show_motiv_{conn_id}")],
                [InlineKeyboardButton(text="⬅️ К подключению",
                                      callback_data=f"gs_conn_{conn_id}")],
            ]),
        )
    except Exception as e:
        await _edit_anchor(
            bot, chat_id, anchor_id,
            f"❌ <b>Ошибка синхронизации:</b>\n<code>{e}</code>\n\n"
            f"Проверьте название листа и параметры структуры.",
            reply_markup=back_kb,
        )
    finally:
        await clear_state_keep_org(state)


@integration_router.callback_query(F.data.startswith("gs_show_motiv_"))
async def gs_show_motiv(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    cache = current_db.get_bonus_cache(conn_id)

    if not cache:
        await callback.message.edit_text(
            "📊 <b>Кэш мотивации пуст</b>\n\n"
            "Нажмите «🔄 Синхронизировать мотивацию» чтобы загрузить данные из таблицы.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Синхронизировать",
                                      callback_data=f"gs_sync_motiv_{conn_id}")],
                [_back(f"gs_conn_{conn_id}")],
            ]),
            parse_mode="HTML"
        )
        return

    by_model = {}
    synced_at = cache[0][4] if cache else "—"
    for row in cache:
        model, chain, bonus, rrp, sat = row
        if model not in by_model:
            by_model[model] = {}
        by_model[model][chain] = bonus
        by_model[model]['rrp'] = rrp
        synced_at = sat

    lines = [f"📊 <b>Кэш мотивации</b> (обновлено: {synced_at[:16]})\n"]
    lines.append(f"{'Модель':<20} {'DNS':>6} {'МВМ':>6}")
    lines.append("─" * 35)
    for model, rates in sorted(by_model.items()):
        dns = rates.get('dns', 0)
        mvm = rates.get('mvm', 0)
        dns_str = f"{int(dns):,}" if dns else "—"
        mvm_str = f"{int(mvm):,}" if mvm else "—"
        lines.append(f"{model:<20} {dns_str:>6} {mvm_str:>6}")

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 Обновить",
                                callback_data=f"gs_sync_motiv_{conn_id}"))
    kb.row(_back(f"gs_conn_{conn_id}"))

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )


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
            sched_label = SCHEDULE_LABELS.get(e[3], e[3] or 'immediate')
            text += (f"{icon} {EXPORT_TYPE_LABELS.get(e[1], e[1])} | "
                     f"{e[4]} | {OPERATION_LABELS.get(e[5], e[5])[:20]}\n")
            text += f"   📅 {sched_label}\n\n"

    kb = InlineKeyboardBuilder()
    for e in exports:
        kb.row(InlineKeyboardButton(
            text=f"⚙️ {EXPORT_TYPE_LABELS.get(e[1], e[1])} → {e[4]}",
            callback_data=f"gs_exp_{e[0]}"
        ))
    kb.row(InlineKeyboardButton(text="➕ Добавить экспорт",
                                callback_data=f"gs_add_exp_{conn_id}"))
    kb.row(_back(f"gs_conn_{conn_id}"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  EXPORT DETAIL
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.regexp(r'^gs_exp_\d+$'))
async def gs_exp_detail(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    try:
        exp_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    current_db = await get_db(callback.from_user.id, state)
    exp = current_db.get_integration_export(exp_id)
    if not exp:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    await callback.answer()

    conn_id = exp[1]
    icon = "✅" if exp[2] else "❌"
    lookup = json.loads(exp[8] or '{}')
    mapping = json.loads(exp[7] or '{}')
    text = (
        f"⚙️ <b>Экспорт #{exp_id}</b>\n\n"
        f"Тип: {EXPORT_TYPE_LABELS.get(exp[3], exp[3])}\n"
        f"Лист: <code>{exp[5]}</code>\n"
        f"Операция: {OPERATION_LABELS.get(exp[6], exp[6])}\n"
        f"Статус: {icon} {'Вкл' if exp[2] else 'Выкл'}\n"
        f"Расписание: {SCHEDULE_LABELS.get(exp[4], exp[4] or 'immediate')}\n"
        f"Последний запуск: {exp[10] or 'не запускался'}\n"
    )
    if mapping:
        text += f"\nМаппинг: {json.dumps(mapping, ensure_ascii=False)[:100]}\n"
    if lookup:
        text += f"Поиск: {json.dumps(lookup, ensure_ascii=False)[:100]}\n"

    kb = InlineKeyboardBuilder()
    new_enabled = 0 if exp[2] else 1
    kb.row(InlineKeyboardButton(
        text="❌ Отключить" if exp[2] else "✅ Включить",
        callback_data=f"gs_exp_toggle_{exp_id}_{conn_id}"
    ))
    kb.row(InlineKeyboardButton(text="🗑 Удалить",
                                callback_data=f"gs_exp_del_{exp_id}_{conn_id}"))
    kb.row(_back(f"gs_exports_{conn_id}"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_exp_toggle_"))
async def gs_exp_toggle(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    exp_id, conn_id = int(parts[3]), int(parts[4])
    current_db = await get_db(callback.from_user.id, state)
    exp = current_db.get_integration_export(exp_id)
    if exp:
        current_db.update_integration_export(exp_id, enabled=0 if exp[2] else 1)
        await callback.answer("✅ Изменено")
    await gs_exp_detail(callback, state)


@integration_router.callback_query(F.data.startswith("gs_exp_del_"))
async def gs_exp_del(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    exp_id, conn_id = int(parts[3]), int(parts[4])
    current_db = await get_db(callback.from_user.id, state)
    current_db.delete_integration_export(exp_id)
    await callback.answer("✅ Удалён")
    await gs_exports_list(callback, state)


# ═══════════════════════════════════════════════════════════
#  ADD EXPORT WIZARD
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_add_exp_"))
async def gs_add_exp_start(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    await state.update_data(gs_conn_id=conn_id, gs_mapping={}, gs_mapping_idx=0,
                            gs_lookup={}, gs_lookup_step=0,
                            anchor_msg_id=callback.message.message_id)
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
        f"✅ Тип: {EXPORT_TYPE_LABELS[exp_type]}\n\n<b>Способ записи в таблицу:</b>\n\n"
        "📌 <b>Обновить ячейку</b> — для матриц «магазин × товар» (инкремент/установка).\n"
        "➕ <b>Добавить строку</b> — лог-запись каждого события.\n"
        "🔄 <b>Заменить лист</b> — полная замена данных по расписанию.",
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
        "Введите <b>название листа</b> (поддерживаются макросы: "
        "<code>{year}</code> <code>{month}</code> <code>{week}</code> <code>{day}</code>).\n"
        "Пример: <code>w{week}</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_back(f"gs_exp_type_{conn_id}_{exp_type}")]]
        ),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_export_sheet)


@integration_router.message(IntegrationStates.waiting_export_sheet)
async def gs_export_sheet(message: Message, state: FSMContext):
    sheet = message.text.strip()
    data = await state.get_data()
    conn_id = data.get('gs_conn_id')
    exp_type = data.get('gs_exp_type', 'sales')
    if not sheet:
        await _fsm_edit(message, state, "❌ Введите название листа.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[_back(f"gs_exp_type_{conn_id}_{exp_type}")]]))
        return
    await state.update_data(gs_target_sheet=sheet)
    operation = data.get('gs_exp_op', 'append_row')

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
    idx    = data.get('gs_mapping_idx', 0)
    if idx >= len(fields):
        await _ask_schedule(message, state)
        return
    field = fields[idx]
    label = FIELD_LABELS.get(field, field)
    done  = len(data.get('gs_mapping', {}))
    total = len(fields)
    await _fsm_edit(
        message, state,
        f"📋 <b>Маппинг столбцов ({done}/{total})</b>\n\n"
        f"Поле: <b>{label}</b> (<code>{field}</code>)\n\n"
        f"Введите <b>заголовок столбца</b> в вашей таблице.\n"
        f"Или <code>skip</code> — пропустить.",
    )
    await state.set_state(IntegrationStates.waiting_mapping)


@integration_router.message(IntegrationStates.waiting_mapping)
async def gs_mapping_field(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    fields  = data.get('gs_mapping_fields', [])
    idx     = data.get('gs_mapping_idx', 0)
    mapping = data.get('gs_mapping', {})

    if text.lower() != 'skip' and text:
        mapping[fields[idx]] = text

    await state.update_data(gs_mapping=mapping, gs_mapping_idx=idx + 1)
    await _ask_next_mapping_field(message, state)


async def _start_lookup_wizard(message: Message, state: FSMContext):
    await state.update_data(gs_lookup={}, gs_lookup_step=0)
    await state.set_state(IntegrationStates.waiting_lookup_step)
    await _fsm_edit(
        message, state,
        "🔍 <b>Настройка поиска ячейки (update_cell)</b>\n\n"
        "Используется для матриц: бот ищет строку по ID магазина и столбец по названию товара.\n\n"
        "<b>Шаг 1/7:</b> В какой <b>колонке</b> искать строку?\n"
        "Введите номер (1 = A, 2 = B…)\n"
        "Пример для вашей таблицы: <code>1</code> (Shop ID в колонке A)",
    )


LOOKUP_STEPS = [
    ("row_search_col",   "🔍 <b>Шаг 1/7:</b> Номер <b>колонки</b> для поиска строки (1=A, 2=B…)"),
    ("row_search_field", "🔍 <b>Шаг 2/7:</b> Поле данных для поиска строки\n"
                         "Доступные: shop_name, seller_name, product_name\n"
                         "Пример: <code>shop_name</code>"),
    ("col_search_row",   "🔍 <b>Шаг 3/7:</b> Номер <b>строки</b> с заголовками столбцов\n"
                         "Пример для w{week}: <code>6</code>"),
    ("col_search_field", "🔍 <b>Шаг 4/7:</b> Поле данных для поиска столбца\n"
                         "Пример: <code>product_name</code>"),
    ("operation",        "🔍 <b>Шаг 5/7:</b> Операция над ячейкой\n"
                         "• <code>set</code> — установить значение\n"
                         "• <code>increment</code> — прибавить (продажи)\n"
                         "• <code>decrement</code> — вычесть"),
    ("value_field",      "🔍 <b>Шаг 6/7:</b> Поле данных — значение для записи\n"
                         "Пример: <code>quantity</code>"),
    ("data_start_row",   "🔍 <b>Шаг 7/7:</b> С какой строки начинаются данные? (строки до неё — заголовки)\n"
                         "Пример для w{week}: <code>7</code>"),
]


@integration_router.message(IntegrationStates.waiting_lookup_step)
async def gs_lookup_step(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    step   = data.get('gs_lookup_step', 0)
    lookup = data.get('gs_lookup', {})

    key = LOOKUP_STEPS[step][0]
    if key in ('row_search_col', 'col_search_row', 'data_start_row'):
        try:
            lookup[key] = int(text)
        except ValueError:
            await _fsm_edit(message, state, "❌ Введите целое число.")
            return
    else:
        lookup[key] = text

    step += 1
    await state.update_data(gs_lookup=lookup, gs_lookup_step=step)

    if step >= len(LOOKUP_STEPS):
        await _ask_schedule(message, state)
    else:
        await _fsm_edit(message, state, LOOKUP_STEPS[step][1])


async def _ask_schedule(message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=SCHEDULE_LABELS['immediate'],
                                callback_data="gs_sched_immediate"))
    kb.row(InlineKeyboardButton(text=SCHEDULE_LABELS['cron'],
                                callback_data="gs_sched_cron"))
    kb.row(InlineKeyboardButton(text=SCHEDULE_LABELS['disabled'],
                                callback_data="gs_sched_disabled"))
    data = await state.get_data()
    anchor_id = data.get('anchor_msg_id')
    text = "📅 <b>Расписание экспорта:</b>"
    # message may be a user Message (needs delete+edit anchor) or callback.message (just edit)
    if anchor_id and getattr(message, 'message_id', None) != anchor_id:
        await delete_message_safe(message)
        await _edit_anchor(message.bot, message.chat.id, anchor_id,
                           text, reply_markup=kb.as_markup())
    else:
        await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_sched_"))
async def gs_sched(callback: CallbackQuery, state: FSMContext):
    sched = callback.data.replace("gs_sched_", "")
    await state.update_data(gs_schedule=sched)
    await callback.answer()
    if sched == 'cron':
        await callback.message.edit_text(
            "🕒 <b>Введите cron-расписание</b>\n\n"
            "Формат: <code>мин час день месяц нед</code>\n"
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
    if len(cron_str.split()) != 5:
        await _fsm_edit(message, state,
                        "❌ Неверный формат. Нужно 5 частей через пробел.")
        return
    await state.update_data(gs_schedule=cron_str)
    await _save_export(message, state, from_message=True)


async def _save_export(msg, state: FSMContext, from_message: bool = False):
    data = await state.get_data()
    conn_id      = data.get('gs_conn_id')
    exp_type     = data.get('gs_exp_type', 'sales')
    operation    = data.get('gs_exp_op', 'append_row')
    target_sheet = data.get('gs_target_sheet', 'Sheet1')
    schedule     = data.get('gs_schedule', 'immediate')
    mapping      = data.get('gs_mapping', {})
    lookup       = data.get('gs_lookup', {})
    anchor_id    = data.get('anchor_msg_id')

    from db_utils import get_db as _get_db
    user_id = msg.from_user.id if hasattr(msg, 'from_user') else msg.chat.id
    current_db = await _get_db(user_id, state)

    exp_id = current_db.add_integration_export(
        connection_id=conn_id,
        export_type=exp_type,
        schedule=schedule,
        target_sheet=target_sheet,
        operation=operation,
        mapping=json.dumps(mapping, ensure_ascii=False),
        lookup_config=json.dumps(lookup, ensure_ascii=False),
    )
    await clear_state_keep_org(state)

    op_label = OPERATION_LABELS.get(operation, operation)
    sched_label = SCHEDULE_LABELS.get(schedule, schedule)

    summary = (
        f"✅ <b>Экспорт настроен!</b>\n\n"
        f"📊 Тип: {EXPORT_TYPE_LABELS.get(exp_type, exp_type)}\n"
        f"📋 Лист: <code>{target_sheet}</code>\n"
        f"✏️ Операция: {op_label}\n"
        f"📅 Расписание: {sched_label}\n"
    )
    if operation == 'update_cell' and lookup:
        summary += (
            f"\n<b>Поиск строки:</b> колонка {lookup.get('row_search_col','?')}, "
            f"поле «{lookup.get('row_search_field','?')}»\n"
            f"<b>Поиск столбца:</b> строка {lookup.get('col_search_row','?')}, "
            f"поле «{lookup.get('col_search_field','?')}»\n"
            f"<b>Операция:</b> {lookup.get('operation','set')} "
            f"поле «{lookup.get('value_field','?')}»\n"
            f"<b>Данные с строки:</b> {lookup.get('data_start_row', 1)}\n"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Экспорты",
                              callback_data=f"gs_exports_{conn_id}")],
        [_back(f"gs_conn_{conn_id}")],
    ])

    if from_message and anchor_id:
        await delete_message_safe(msg)
        await _edit_anchor(msg.bot, msg.chat.id, anchor_id, summary, reply_markup=kb)
    else:
        await msg.edit_text(summary, reply_markup=kb, parse_mode="HTML")
