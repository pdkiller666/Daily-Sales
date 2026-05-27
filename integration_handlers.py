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
from utils import he
from subscription_utils import check_integrations_permission
from keyboards import back_button, home_button
from states import IntegrationStates, GSImportStates
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
    if not check_integrations_permission(callback.from_user.id):
        await callback.answer()
        await callback.message.edit_text(
            "🔒 <b>Google Таблицы — тариф «Стандарт» и выше</b>\n\n"
            "💎 <b>Базовый</b> — экспорт Excel, аналитика, уведомления\n"
            "💎 <b>Стандарт</b> — всё выше + Google Таблицы\n"
            "💎 <b>Премиум</b> — безлимит на всё\n\n"
            "Выберите подходящий тариф прямо сейчас:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="subscription_plans")],
                [back_button("main_menu")],
            ])
        )
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)

    connections = await current_db.get_integration_connections()

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
    kb.row(home_button())
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


# ═══════════════════════════════════════════════════════════
#  EXPORT GUIDE (7 шагов — инструкция по настройке экспорта)
# ═══════════════════════════════════════════════════════════

_EXPORT_GUIDE_TOTAL = 7

def _export_guide_kb(step: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if step > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"gs_expguide_{step - 1}"))
    if step < _EXPORT_GUIDE_TOTAL:
        nav.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"gs_expguide_{step + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 В меню интеграций", callback_data="integration_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_EXPORT_GUIDE_STEPS = {
    1: (
        "📖 <b>Экспорт в Google Таблицы — Шаг 1 из 7</b>\n"
        "<b>Зачем нужен экспорт?</b>\n\n"
        "После настройки бот будет <b>автоматически обновлять ячейку</b> в таблице "
        "каждый раз, когда кто-то фиксирует продажу.\n\n"
        "<b>Пример:</b> продали Nova 14i в магазине 2905 → в ячейке "
        "[строка «2905», столбец «Nova 14i»] число увеличится на 1.\n\n"
        "📋 <b>Что нужно заранее:</b>\n"
        "• Таблица уже создана и открыта\n"
        "• Google-аккаунт подключён к боту\n"
        "• Ты знаешь, в какой строке написаны заголовки столбцов\n"
        "• Ты знаешь, в каком столбце написаны идентификаторы строк\n\n"
        "✅ Всё готово → нажми <b>Далее</b>"
    ),
    2: (
        "📖 <b>Экспорт в Google Таблицы — Шаг 2 из 7</b>\n"
        "<b>Мастер настройки — Шаг 1/4: Строка заголовков</b>\n\n"
        "Бот загрузит первые строки твоей таблицы. "
        "Выбери ту строку, в которой написаны <b>названия столбцов</b> "
        "(модели товаров, категории и т.п.).\n\n"
        "Пример таблицы:\n"
        "<code>"
        "Стр 5:  w21\n"
        "Стр 6:  Shop | Name | Y73 | Nova 14 | ...  ← эта\n"
        "Стр 7:  2905 | Иван | 2   | 1       | ..."
        "</code>\n\n"
        "☝️ Если таблица не загрузилась — нажми <b>🔄 Обновить</b> "
        "или введи номер строки вручную.\n\n"
        "✅ Строка выбрана → нажми <b>Далее</b>"
    ),
    3: (
        "📖 <b>Экспорт в Google Таблицы — Шаг 3 из 7</b>\n"
        "<b>Мастер настройки — Шаг 2/4: Столбец с ID строк</b>\n\n"
        "Выбери столбец, в котором написаны <b>идентификаторы строк</b> — "
        "то, по чему бот понимает какую строку обновлять.\n\n"
        "Обычно это:\n"
        "• Столбец A — коды магазинов (2905, 3059…)\n"
        "• Столбец B — имена продавцов\n\n"
        "<b>Пример:</b> если магазины перечислены в столбце A "
        "(это первый столбец = номер <code>1</code>) → выбери кнопку с <b>1 (A)</b>.\n\n"
        "☝️ Если список не загрузился — нажми <b>🔄 Обновить</b> "
        "или введи номер столбца вручную (1 = A, 2 = B, 3 = C…).\n\n"
        "✅ Столбец выбран → нажми <b>Далее</b>"
    ),
    4: (
        "📖 <b>Экспорт в Google Таблицы — Шаг 4 из 7</b>\n"
        "<b>Мастер настройки — Шаг 3/6: По какому полю искать СТРОКУ?</b>\n\n"
        "Выбери, какое поле из продажи бот будет сравнивать "
        "с идентификатором строки в таблице.\n\n"
        "| В столбце A написаны… | Выбери |\n"
        "| Коды / названия магазинов | 🏪 Название / код магазина |\n"
        "| Имена продавцов | 👤 Имя продавца |\n"
        "| Названия товаров | 📦 Название товара |\n\n"
        "<b>Пример:</b> в столбце A написаны коды магазинов → "
        "выбери <b>«🏪 Название / код магазина»</b>\n\n"
        "💡 <i>Это только поиск строки. Что записывать в ячейку — выберешь на шаге 6.</i>\n\n"
        "✅ Поле выбрано → нажми <b>Далее</b>"
    ),
    5: (
        "📖 <b>Экспорт в Google Таблицы — Шаг 5 из 7</b>\n"
        "<b>Мастер настройки — Шаг 4/6: По какому полю искать СТОЛБЕЦ?</b>\n\n"
        "Выбери, какое поле из продажи бот будет сравнивать "
        "с заголовком столбца в таблице.\n\n"
        "| В строке заголовков написаны… | Выбери |\n"
        "| Модели / названия товаров | 📦 Название товара |\n"
        "| Категории | 📂 Категория товара |\n"
        "| Названия магазинов | 🏪 Название магазина |\n\n"
        "<b>Пример:</b> в строке 6 написаны «Nova 14», «Y73», «Mate 80»… → "
        "выбери <b>«📦 Название товара»</b>\n\n"
        "✅ Поле выбрано → нажми <b>Далее</b>"
    ),
    6: (
        "📖 <b>Экспорт в Google Таблицы — Шаг 6 из 7</b>\n"
        "<b>Мастер настройки — Шаги 5–6/6: Действие и значение</b>\n\n"
        "<b>Шаг 5/6 — Что делать с ячейкой:</b>\n"
        "• <b>➕ Прибавить</b> — при каждой продаже число в ячейке увеличивается\n"
        "• <b>➖ Вычесть</b> — уменьшается (для возвратов)\n"
        "• <b>= Заменить</b> — ячейка перезаписывается\n\n"
        "👉 Для подсчёта продаж выбирай <b>«➕ Прибавить»</b>\n\n"
        "─────────────────────────\n\n"
        "<b>Шаг 6/6 — Что записывать:</b>\n"
        "• <b>🔢 Количество товаров</b> — число штук (самый частый)\n"
        "• <b>💰 Сумма продажи</b> — итоговая сумма в рублях\n"
        "• <b>💲 Цена за единицу</b> — цена одного товара\n\n"
        "<b>Пример:</b> хотим считать штуки проданных моделей → <b>«🔢 Количество»</b>\n\n"
        "✅ Оба шага выбраны → нажми <b>Далее</b>"
    ),
    7: (
        "📖 <b>Экспорт в Google Таблицы — Шаг 7 из 7</b>\n"
        "<b>Псевдонимы и расписание</b>\n\n"
        "<b>Псевдонимы</b> — нужны если название в боте <b>не совпадает</b> "
        "с тем, что написано в таблице.\n\n"
        "Формат — одна пара в строке:\n"
        "<code>Название в боте → Значение в таблице</code>\n\n"
        "Примеры:\n"
        "<code>ТЦ Лето 2905 → 2905\n"
        "Nova 15 Pro 256 → Nova 15 256\n"
        "Магазин DNS → DNS Центр</code>\n\n"
        "Если названия совпадают — нажми <b>«⏭ Пропустить»</b>.\n\n"
        "─────────────────────────\n\n"
        "<b>Расписание обновления:</b>\n"
        "• <b>⚡ Мгновенно</b> — ячейка обновляется сразу при каждой продаже\n"
        "• <b>🕐 По расписанию</b> — раз в час / день\n"
        "• <b>⏸ Отключён</b> — экспорт сохранён, но не работает\n\n"
        "💡 Псевдонимы и расписание можно изменить позже — "
        "найди экспорт в меню интеграций и нажми <b>«📝 Псевдонимы»</b>.\n\n"
        "✅ Настройка завершена! Сделай тестовую продажу и проверь обновилась ли ячейка."
    ),
}


@integration_router.callback_query(F.data.regexp(r'^gs_expguide_\d+$'))
async def gs_export_guide_step(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        step = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        step = 1
    step = max(1, min(step, _EXPORT_GUIDE_TOTAL))
    await callback.message.edit_text(
        _EXPORT_GUIDE_STEPS[step],
        reply_markup=_export_guide_kb(step),
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
        f"Название: <b>{he(name)}</b>\n\n"
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


async def _fetch_gs_config(user_id: int, state: FSMContext):
    """Return (GoogleSheetsProvider, config_dict) for the current connection in FSM state.
    Returns (None, None) on any failure."""
    try:
        data = await state.get_data()
        conn_id = data.get('gs_conn_id')
        if not conn_id:
            return None, None
        db = await get_db(user_id, state)
        conn = await db.get_integration_connection(conn_id)
        if not conn:
            return None, None
        cfg = json.loads(conn[3] or '{}')
        from integration.providers.google_sheets import GoogleSheetsProvider
        return GoogleSheetsProvider(), cfg
    except Exception as e:
        logger.warning(f"_fetch_gs_config: {e}")
        return None, None


def _render_sheet_macro(name: str) -> str:
    """Render {year}/{month}/{week}/{day} macros with today's date."""
    import datetime
    now = datetime.datetime.now()
    return (name
            .replace('{year}',  str(now.year))
            .replace('{month}', str(now.month))
            .replace('{week}',  str(now.isocalendar()[1]))
            .replace('{day}',   str(now.day)))


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
        conn_id = await current_db.add_integration_connection(
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
            conn_id = await current_db.add_integration_connection(
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

def _build_conn_detail_content(conn, conn_id: int, exports: list):
    """Return (text, markup) for the connection detail screen."""
    cfg = json.loads(conn[3] or '{}')
    status_icon = "✅" if conn[4] else "❌"
    auth_label  = ("🔑 OAuth (личный аккаунт)"
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
    kb.row(InlineKeyboardButton(text="📥 Импорт данных", callback_data=f"gs_import_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🔍 Тест подключения", callback_data=f"gs_test_conn_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🔄 Синхронизировать мотивацию",
                                callback_data=f"gs_sync_motiv_{conn_id}"))
    kb.row(InlineKeyboardButton(text="📊 Кэш мотивации",
                                callback_data=f"gs_show_motiv_{conn_id}"))
    kb.row(InlineKeyboardButton(text="📢 Журнал событий", callback_data=f"gs_log_{conn_id}"))
    if cfg.get("auth_type") == "oauth":
        kb.row(InlineKeyboardButton(text="🔑 Переавторизовать Google",
                                    callback_data=f"gs_reauth_{conn_id}"))
    if exports:
        kb.row(InlineKeyboardButton(text="📤 Перенести экспорты",
                                    callback_data=f"gs_move_exports_{conn_id}"))
    kb.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gs_del_conn_{conn_id}"))
    kb.row(_back("integration_menu"))
    return text, kb.as_markup()


@integration_router.callback_query(F.data.startswith("gs_conn_"))
async def gs_conn_detail(callback: CallbackQuery, state: FSMContext):
    conn_id    = int(callback.data.split("_")[2])
    current_db = await get_db(callback.from_user.id, state)
    conn       = await current_db.get_integration_connection(conn_id)
    if not conn:
        await callback.answer("❌ Подключение не найдено", show_alert=True)
        return
    await callback.answer()
    exports      = await current_db.get_integration_exports(conn_id)
    text, markup = _build_conn_detail_content(conn, conn_id, exports)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_toggle_conn_"))
async def gs_toggle_conn(callback: CallbackQuery, state: FSMContext):
    conn_id    = int(callback.data.split("_")[3])
    current_db = await get_db(callback.from_user.id, state)
    conn       = await current_db.get_integration_connection(conn_id)
    if not conn:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    await current_db.update_integration_connection(conn_id, enabled=0 if conn[4] else 1)
    await callback.answer("✅ Изменено")
    # Re-fetch updated conn and render — cannot call gs_conn_detail(callback) directly
    # because callback.data is gs_toggle_conn_N, not gs_conn_N
    conn         = await current_db.get_integration_connection(conn_id)
    exports      = await current_db.get_integration_exports(conn_id)
    text, markup = _build_conn_detail_content(conn, conn_id, exports)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_test_conn_"))
async def gs_test_conn(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    await callback.answer("⏳ Проверяю…")
    current_db = await get_db(callback.from_user.id, state)
    conn = await current_db.get_integration_connection(conn_id)
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
    conn = await current_db.get_integration_connection(conn_id)
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
        await current_db.delete_integration_connection(conn_id)
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


@integration_router.callback_query(F.data.startswith("gs_move_exports_"))
async def gs_move_exports_pick(callback: CallbackQuery, state: FSMContext):
    """Show list of other connections to move exports into."""
    from_id = int(callback.data.split("_")[3])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    all_conns = await current_db.get_integration_connections()
    others = [c for c in all_conns if c[0] != from_id]
    if not others:
        await callback.message.edit_text(
            "❌ <b>Нет других подключений</b>\n\nСначала создайте новое подключение.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back(f"gs_conn_{from_id}")]]),
            parse_mode="HTML"
        )
        return
    from_conn = await current_db.get_integration_connection(from_id)
    from_name = from_conn[1] if from_conn else f"#{from_id}"
    from_exports = await current_db.get_integration_exports(from_id)
    rows = []
    for c in others:
        status_icon = "✅" if c[4] else "❌"
        rows.append([InlineKeyboardButton(
            text=f"{status_icon} {c[1]}",
            callback_data=f"gs_move_exp_to_{from_id}_{c[0]}"
        )])
    rows.append([_back(f"gs_conn_{from_id}")])
    await callback.message.edit_text(
        f"📤 <b>Перенести экспорты</b>\n\n"
        f"Из: <b>{from_name}</b> ({len(from_exports)} экспорт(ов))\n\n"
        f"Выберите <b>целевое</b> подключение:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_move_exp_to_"))
async def gs_move_exports_confirm(callback: CallbackQuery, state: FSMContext):
    """Confirm screen before actually moving exports."""
    parts = callback.data.split("_")
    from_id, to_id = int(parts[4]), int(parts[5])
    current_db = await get_db(callback.from_user.id, state)
    from_conn = await current_db.get_integration_connection(from_id)
    to_conn   = await current_db.get_integration_connection(to_id)
    if not from_conn or not to_conn:
        await callback.answer("❌ Подключение не найдено", show_alert=True)
        return
    await callback.answer()
    from_exports = await current_db.get_integration_exports(from_id)
    exp_preview = "\n".join(
        f"  • {EXPORT_TYPE_LABELS.get(e[1], e[1])} → {e[4] or '?'}"
        for e in from_exports[:8]
    )
    if len(from_exports) > 8:
        exp_preview += f"\n  … ещё {len(from_exports) - 8}"
    await callback.message.edit_text(
        f"📤 <b>Подтвердите перенос экспортов</b>\n\n"
        f"Из: <b>{from_conn[1]}</b>\n"
        f"В: <b>{to_conn[1]}</b>\n\n"
        f"Будут перенесены ({len(from_exports)}):\n{exp_preview}\n\n"
        f"⚠️ Все псевдонимы и настройки сохранятся.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Перенести",
                                  callback_data=f"gs_move_exp_ok_{from_id}_{to_id}")],
            [_back(f"gs_move_exports_{from_id}")],
        ]),
        parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_move_exp_ok_"))
async def gs_move_exports_execute(callback: CallbackQuery, state: FSMContext):
    """Execute the export transfer."""
    parts = callback.data.split("_")
    from_id, to_id = int(parts[4]), int(parts[5])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    moved = await current_db.move_exports_to_connection(from_id, to_id)
    to_conn = await current_db.get_integration_connection(to_id)
    to_name = to_conn[1] if to_conn else f"#{to_id}"
    await callback.message.edit_text(
        f"✅ <b>Перенесено {moved} экспорт(ов)</b>\n\n"
        f"Все экспорты теперь привязаны к подключению <b>{to_name}</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Открыть подключение",
                                  callback_data=f"gs_conn_{to_id}")],
            [_back("integration_menu")],
        ]),
        parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_log_"))
async def gs_log(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    logs = await current_db.get_integration_logs(conn_id, limit=10)
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
    conn = await current_db.get_integration_connection(conn_id)
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
        _e_str = str(e)
        if 'invalid_grant' in _e_str.lower() or 'отозвана' in _e_str or 'Переподключите' in _e_str:
            _err_text = (
                "🔑 <b>Авторизация Google отозвана или истекла</b>\n\n"
                "Переподключите аккаунт: нажмите «⬅️ К интеграциям» → выберите подключение → "
                "«🔄 Переподключить OAuth»."
            )
        else:
            _err_text = (
                f"❌ <b>Ошибка синхронизации:</b>\n<code>{_e_str}</code>\n\n"
                "Проверьте название листа и параметры структуры."
            )
        await _edit_anchor(bot, chat_id, anchor_id, _err_text, reply_markup=back_kb)
    finally:
        await clear_state_keep_org(state)


@integration_router.callback_query(F.data.startswith("gs_show_motiv_"))
async def gs_show_motiv(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[3])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    cache = await current_db.get_bonus_cache(conn_id)

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

def _build_exports_list_content(exports: list, conn_id: int):
    """Return (text, markup) for the exports list screen."""
    text = "📋 <b>Экспорты</b>\n\n"
    if not exports:
        text += "Экспортов нет.\n"
    else:
        for e in exports:
            icon       = "✅" if e[2] else "❌"
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
    kb.row(InlineKeyboardButton(text="📖 Инструкция по настройке экспорта",
                                callback_data="gs_expguide_1"))
    kb.row(_back(f"gs_conn_{conn_id}"))
    return text, kb.as_markup()


@integration_router.callback_query(F.data.startswith("gs_exports_"))
async def gs_exports_list(callback: CallbackQuery, state: FSMContext):
    conn_id = int(callback.data.split("_")[2])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    exports    = await current_db.get_integration_exports(conn_id)
    text, markup = _build_exports_list_content(exports, conn_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  EXPORT DETAIL
# ═══════════════════════════════════════════════════════════

def _build_exp_detail_content(exp, exp_id: int):
    """Return (text, markup) for the export detail screen.
    exp = get_integration_export row:
    [0]=export_type [1]=connection_id [2]=enabled [3]=schedule [4]=target_sheet
    [5]=operation [6]=mapping [7]=lookup_config [8]=extra [9]=last_run
    """
    export_type  = exp[0]
    conn_id      = exp[1]
    enabled      = exp[2]
    schedule     = exp[3]
    target_sheet = exp[4]
    operation    = exp[5]
    mapping      = json.loads(exp[6] or '{}')
    lookup       = json.loads(exp[7] or '{}')
    last_run     = exp[9]

    icon    = "✅" if enabled else "❌"
    aliases = lookup.get('aliases', {})

    text = (
        f"⚙️ <b>Экспорт #{exp_id}</b>\n\n"
        f"Тип: {EXPORT_TYPE_LABELS.get(export_type, export_type)}\n"
        f"Лист: <code>{target_sheet}</code>\n"
        f"Операция: {OPERATION_LABELS.get(operation, operation)}\n"
        f"Статус: {icon} {'Вкл' if enabled else 'Выкл'}\n"
        f"Расписание: {SCHEDULE_LABELS.get(schedule, schedule or 'immediate')}\n"
        f"Последний запуск: {last_run or 'не запускался'}\n"
    )
    if operation == 'append_row' and mapping:
        text += f"\nПоля: {', '.join(mapping.keys())}\n"
    if operation == 'update_cell' and lookup:
        text += (
            f"\n🔍 <b>Матрица:</b>\n"
            f"Строки: кол.{lookup.get('row_search_col','?')} → "
            f"«{lookup.get('row_search_field','?')}»\n"
            f"Столбцы: стр.{lookup.get('col_search_row','?')} → "
            f"«{lookup.get('col_search_field','?')}»\n"
            f"Действие: {lookup.get('operation','set')} "
            f"«{lookup.get('value_field','?')}»\n"
        )
    if aliases:
        text += f"\n📝 Псевдонимов: {len(aliases)}\n"
        for bot_v, sheet_v in list(aliases.items())[:4]:
            text += f"  <code>{bot_v}</code> → <code>{sheet_v}</code>\n"
        if len(aliases) > 4:
            text += f"  ...ещё {len(aliases) - 4}\n"

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="❌ Отключить" if enabled else "✅ Включить",
        callback_data=f"gs_exp_toggle_{exp_id}_{conn_id}"
    ))
    if operation == 'update_cell':
        kb.row(InlineKeyboardButton(
            text="📝 Псевдонимы",
            callback_data=f"gs_alias_edit_{exp_id}_{conn_id}"
        ))
    kb.row(InlineKeyboardButton(
        text="🗑 Удалить",
        callback_data=f"gs_exp_del_confirm_{exp_id}_{conn_id}"
    ))
    kb.row(_back(f"gs_exports_{conn_id}"))
    return text, kb.as_markup()


@integration_router.callback_query(F.data.regexp(r'^gs_exp_\d+$'))
async def gs_exp_detail(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    try:
        exp_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    current_db = await get_db(callback.from_user.id, state)
    exp = await current_db.get_integration_export(exp_id)
    if not exp:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    await callback.answer()
    text, markup = _build_exp_detail_content(exp, exp_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_exp_toggle_"))
async def gs_exp_toggle(callback: CallbackQuery, state: FSMContext):
    parts  = callback.data.split("_")
    exp_id = int(parts[3])
    current_db = await get_db(callback.from_user.id, state)
    exp = await current_db.get_integration_export(exp_id)
    if not exp:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    await current_db.update_integration_export(exp_id, enabled=0 if exp[2] else 1)
    await callback.answer("✅ Изменено")
    # Re-fetch updated exp and render — cannot call gs_exp_detail(callback) directly
    # because callback.data is gs_exp_toggle_N_M, not gs_exp_N
    exp = await current_db.get_integration_export(exp_id)
    text, markup = _build_exp_detail_content(exp, exp_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@integration_router.callback_query(F.data.startswith("gs_exp_del_confirm_"))
async def gs_exp_del_confirm(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    exp_id, conn_id = int(parts[4]), int(parts[5])
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить",
                              callback_data=f"gs_exp_del_{exp_id}_{conn_id}")],
        [_back(f"gs_exp_{exp_id}")],
    ])
    await callback.message.edit_text(
        f"⚠️ <b>Удалить экспорт #{exp_id}?</b>\n\nЭто действие нельзя отменить.",
        reply_markup=kb, parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_exp_del_"))
async def gs_exp_del(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    exp_id, conn_id = int(parts[3]), int(parts[4])
    current_db = await get_db(callback.from_user.id, state)
    await current_db.delete_integration_export(exp_id)
    await callback.answer("✅ Удалён")
    # Use helper directly — callback.data here is gs_exp_del_N_M, not gs_exports_M
    exports = await current_db.get_integration_exports(conn_id)
    text, markup = _build_exports_list_content(exports, conn_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  ALIAS EDIT (update_cell exports)
# ═══════════════════════════════════════════════════════════

@integration_router.callback_query(F.data.startswith("gs_alias_edit_"))
async def gs_alias_edit(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    exp_id, conn_id = int(parts[3]), int(parts[4])
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    exp = await current_db.get_integration_export(exp_id)
    lookup = json.loads(exp[7] or '{}') if exp else {}
    aliases = lookup.get('aliases', {})

    current_text = '\n'.join(f"{k} → {v}" for k, v in aliases.items()) or "(псевдонимов нет)"
    await state.update_data(alias_exp_id=exp_id, alias_conn_id=conn_id,
                            anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        f"📝 <b>Псевдонимы — Экспорт #{exp_id}</b>\n\n"
        f"<b>Текущие:</b>\n<code>{current_text}</code>\n\n"
        "Введи соответствия — <b>по одному в строке</b>:\n"
        "<code>Название в боте → Значение в таблице</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>Хуавей DNS → 2905</code>\n"
        "<code>Nova 14 → Nova 14i</code>\n\n"
        "Разделители: <code>→</code>  <code>-&gt;</code>  <code>:</code>\n"
        "Чтобы удалить все псевдонимы — отправь <code>clear</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back(f"gs_exp_{exp_id}")]]),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_alias_edit)


@integration_router.message(IntegrationStates.waiting_alias_edit)
async def gs_alias_input(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    exp_id   = data.get('alias_exp_id')
    anchor_id = data.get('anchor_msg_id')
    await delete_message_safe(message)
    current_db = await get_db(message.chat.id, state)
    exp = await current_db.get_integration_export(exp_id)
    if not exp:
        await _edit_anchor(message.bot, message.chat.id, anchor_id, "❌ Экспорт не найден.")
        await clear_state_keep_org(state)
        return
    lookup = json.loads(exp[7] or '{}')
    if text.lower() == 'clear':
        lookup['aliases'] = {}
        result = "✅ Все псевдонимы удалены."
    else:
        aliases = _parse_aliases(text)
        lookup['aliases'] = aliases
        result = f"✅ Сохранено псевдонимов: {len(aliases)}"
        if aliases:
            result += '\n' + '\n'.join(f"  <code>{k}</code> → <code>{v}</code>"
                                       for k, v in aliases.items())
    await current_db.update_integration_export(
        exp_id, lookup_config=json.dumps(lookup, ensure_ascii=False))
    conn_id = data.get('alias_conn_id')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить ещё",
                              callback_data=f"gs_alias_edit_{exp_id}_{conn_id}")],
        [_back(f"gs_exp_{exp_id}")],
    ])
    await _edit_anchor(message.bot, message.chat.id, anchor_id, result, reply_markup=kb)
    await clear_state_keep_org(state)


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
    conn_id  = data.get('gs_conn_id')

    sheets = []
    api_error = False
    try:
        provider, cfg = await _fetch_gs_config(callback.from_user.id, state)
        if provider:
            sheets = await asyncio.wait_for(
                provider.get_sheets_list(cfg), timeout=6.0)
        else:
            api_error = True
    except Exception:
        sheets = []
        api_error = True

    back_btn = _back(f"gs_exp_type_{conn_id}_{exp_type}")

    if sheets:
        await state.update_data(gs_available_sheets=sheets)
        kb = InlineKeyboardBuilder()
        for i, s in enumerate(sheets[:12]):
            kb.row(InlineKeyboardButton(text=f"📋 {s}", callback_data=f"gs_pick_sheet_{i}"))
        kb.row(InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="gs_sheet_manual"))
        kb.row(back_btn)
        await callback.message.edit_text(
            f"✅ Операция: <b>{OPERATION_LABELS[operation]}</b>\n\n"
            "📋 <b>Выбери лист из таблицы:</b>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
    else:
        retry_cb = f"gs_exp_op_{operation}"
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=retry_cb))
        kb.row(InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="gs_sheet_manual"))
        kb.row(back_btn)
        warn = "\n\n⚠️ <i>Не удалось получить список листов — проверьте авторизацию Google.</i>" if api_error else ""
        await callback.message.edit_text(
            f"✅ Операция: <b>{OPERATION_LABELS[operation]}</b>{warn}\n\n"
            "Введите <b>название листа</b> или нажмите «Ввести вручную».",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(IntegrationStates.waiting_export_sheet)


@integration_router.callback_query(F.data == "gs_sheet_manual")
async def gs_sheet_manual(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    conn_id  = data.get('gs_conn_id')
    exp_type = data.get('gs_exp_type', 'sales')
    await callback.message.edit_text(
        "✏️ <b>Название листа вручную</b>\n\n"
        "Поддерживаются макросы: "
        "<code>{year}</code> <code>{month}</code> <code>{week}</code> <code>{day}</code>\n"
        "Пример: <code>w{week}</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_back(f"gs_exp_type_{conn_id}_{exp_type}")]]
        ),
        parse_mode="HTML"
    )
    await state.set_state(IntegrationStates.waiting_export_sheet)


@integration_router.callback_query(F.data.startswith("gs_pick_sheet_"))
async def gs_pick_sheet(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.replace("gs_pick_sheet_", ""))
    data = await state.get_data()
    sheets    = data.get('gs_available_sheets', [])
    exp_type  = data.get('gs_exp_type', 'sales')
    operation = data.get('gs_exp_op', 'append_row')
    if idx >= len(sheets):
        await callback.answer("⚠️ Лист не найден", show_alert=True)
        return
    await callback.answer()
    sheet = sheets[idx]
    await state.update_data(gs_target_sheet=sheet)

    if operation == 'append_row':
        await _start_mapping_wizard(callback.message, state, exp_type)
    elif operation == 'update_cell':
        await _start_lookup_wizard(callback.message, state)
    else:
        await _ask_schedule(callback.message, state)


@integration_router.message(IntegrationStates.waiting_export_sheet)
async def gs_export_sheet(message: Message, state: FSMContext):
    sheet = message.text.strip()
    data = await state.get_data()
    conn_id  = data.get('gs_conn_id')
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


# ── lookup wizard helpers ────────────────────────────────

def _parse_aliases(text: str) -> dict:
    """Parse alias lines: 'bot_value → sheet_value' (→, ->, :)."""
    aliases = {}
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        for sep in ['→', '->', ':']:
            if sep in line:
                parts = line.split(sep, 1)
                bot_val   = parts[0].strip()
                sheet_val = parts[1].strip()
                if bot_val and sheet_val:
                    aliases[bot_val] = sheet_val
                break
    return aliases


LOOKUP_BTN_STEPS = {
    'row_search_field': {
        'prompt': (
            "🔍 <b>Шаг 3/6 — По какому полю искать СТРОКУ?</b>\n\n"
            "В твоей таблице каждая <b>строка</b> — это один магазин или продавец.\n"
            "Выбери, какое поле из продажи совпадает с <b>идентификатором строки</b> "
            "(тем, что написано в первом столбце матрицы).\n\n"
            "Пример: если в столбце A написаны коды/названия магазинов → выбери "
            "<b>«Название магазина»</b>\n\n"
            "💡 <i>Что именно записывать в ячейку (количество, сумму) — "
            "выберешь на шаге 6.</i>"
        ),
        'options': [
            ('shop_name',    '🏪 Название / код магазина'),
            ('seller_name',  '👤 Имя продавца'),
            ('product_name', '📦 Название товара'),
        ],
        'prev': 'idcol',   # special: back to id-column picker
        'next': 'col_search_field',
    },
    'col_search_field': {
        'prompt': (
            "🔍 <b>Шаг 4/6 — По какому полю искать СТОЛБЕЦ?</b>\n\n"
            "В строке заголовков твоей таблицы написаны названия <b>столбцов</b> — "
            "как правило, это модели товаров, категории или магазины.\n"
            "Выбери, какое поле из продажи совпадает с <b>заголовком столбца</b>.\n\n"
            "Пример: если в строке 6 написаны «Nova 14», «Y73» и т.д. → выбери "
            "<b>«Название товара»</b>\n\n"
            "💡 <i>Что именно записывать — выберешь на следующем шаге.</i>"
        ),
        'options': [
            ('product_name', '📦 Название товара'),
            ('category',     '📂 Категория товара'),
            ('shop_name',    '🏪 Название магазина'),
        ],
        'prev': 'row_search_field',
        'next': 'operation',
    },
    'operation': {
        'prompt': (
            "🔍 <b>Шаг 5/6 — Что делать с ячейкой?</b>\n\n"
            "Бот нашёл нужную ячейку. Что с ней сделать при каждой продаже?"
        ),
        'options': [
            ('increment', '➕ Прибавить значение'),
            ('decrement', '➖ Вычесть значение'),
            ('set',       '= Заменить значение'),
        ],
        'prev': 'col_search_field',
        'next': 'value_field',
    },
    'value_field': {
        'prompt': (
            "🔍 <b>Шаг 6/6 — Что записывать в ячейку?</b>\n\n"
            "Выбери, какое число из продажи использовать как значение:"
        ),
        'options': [
            ('quantity', '🔢 Количество товаров'),
            ('total',    '💰 Сумма продажи (руб.)'),
            ('price',    '💲 Цена за единицу'),
        ],
        'prev': 'operation',
        'next': None,
    },
}


def _lookup_btn_kb(field_key: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for val, label in LOOKUP_BTN_STEPS[field_key]['options']:
        cb = f"gs_lkp_{field_key}_{val}"
        kb.row(InlineKeyboardButton(text=label, callback_data=cb))
    prev_key = LOOKUP_BTN_STEPS[field_key].get('prev')
    if prev_key:
        bk_cb = "gs_lkp_bk_idcol" if prev_key == 'idcol' else f"gs_lkp_bk_{prev_key}"
        kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=bk_cb))
    return kb.as_markup()


def _col_letter(n: int) -> str:
    """Convert 1-based column index to spreadsheet letter (1=A, 2=B, 27=AA)."""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _row_btn_label(row_idx: int, values: list) -> str:
    """Short label for a row button that fits in a Telegram button on mobile.
    Format: 'Строка N: val1 · val2 · val3…'  (≤ 40 chars total)
    """
    non_empty = [str(v).strip() for v in values if str(v).strip()]
    prefix    = f"Строка {row_idx}: "
    budget    = 39 - len(prefix)   # 39 = 40 - 1 reserved for '…'
    parts     = []
    used      = 0
    for val in non_empty[:3]:
        chunk = val[:12]
        sep   = " · " if parts else ""
        if used + len(sep) + len(chunk) > budget:
            break
        parts.append(chunk)
        used += len(sep) + len(chunk)
    has_more = len(non_empty) > len(parts)
    preview  = " · ".join(parts) + ("…" if has_more else "")
    return f"{prefix}{preview}" if preview else f"{prefix}(пусто)"


def _col_btn_label(col_idx: int, header: str, first_val: str) -> str:
    """Short label for a column button that fits in a Telegram button on mobile.
    Format: 'A — Header (sample)'  (≤ 40 chars total)
    """
    letter = _col_letter(col_idx)
    prefix = f"{letter} — "
    budget = 40 - len(prefix)
    h      = header.strip()[:18]
    s      = first_val.strip()
    if s and s != header.strip() and h:
        max_sample = budget - len(h) - 3   # 3 = len(" ()")
        detail = f"{h} ({s[:max_sample]})" if max_sample >= 4 else h
    elif s and not h:
        detail = s[:budget]
    else:
        detail = h
    return f"{prefix}{detail}" if detail else f"{prefix}(пусто)"


_HROW_PAGE_SIZE = 5


def _hrow_page_kb(rows_sorted: list, page: int, wiz_back_cb: str = "") -> InlineKeyboardMarkup:
    """Build paginated row-picker keyboard. rows_sorted = [(rn, values), ...]"""
    total      = len(rows_sorted)
    total_pages = max(1, -(-total // _HROW_PAGE_SIZE))   # ceil division
    page        = max(0, min(page, total_pages - 1))
    start       = page * _HROW_PAGE_SIZE
    chunk       = rows_sorted[start: start + _HROW_PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for rn, vals in chunk:
        kb.row(InlineKeyboardButton(
            text=_row_btn_label(rn, vals), callback_data=f"gs_lkp_hrow_{rn}"))

    # Prev / Next navigation row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"gs_lkp_hrow_pg_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"gs_lkp_hrow_pg_{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="✏️ Ввести номер вручную",
                                callback_data="gs_lkp_hrow_manual"))
    if wiz_back_cb:
        kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=wiz_back_cb))
    return kb.as_markup()


async def _show_hrow_picker(target, state: FSMContext, rows_data: dict, page: int = 0):
    """Show paginated row-picker buttons.
    target = Message or .message from callback.
    Stores rows in FSM state for page-turn handler.
    """
    rows_sorted = [(rn, rows_data[rn]) for rn in sorted(rows_data)]
    await state.update_data(gs_hrow_rows=rows_sorted)
    total       = len(rows_sorted)
    total_pages = max(1, -(-total // _HROW_PAGE_SIZE))
    data2 = await state.get_data()
    wiz_back_cb = data2.get('gs_wizard_back_cb', '')
    markup      = _hrow_page_kb(rows_sorted, page, wiz_back_cb=wiz_back_cb)
    text = (
        f"🔍 <b>Настройка матрицы — шаг 1/4</b>\n"
        f"<i>Стр. {page+1}/{total_pages} · всего строк: {total}</i>\n\n"
        "Выбери строку, в которой написаны <b>названия столбцов</b> "
        "(заголовки матрицы — товары, недели и т.п.):"
    )
    try:
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await _fsm_edit(target, state, text, reply_markup=markup)


@integration_router.callback_query(F.data.startswith("gs_lkp_hrow_pg_"))
async def gs_lkp_hrow_page(callback: CallbackQuery, state: FSMContext):
    """Navigate between pages of the row picker."""
    page = int(callback.data.replace("gs_lkp_hrow_pg_", ""))
    data = await state.get_data()
    rows_sorted = data.get('gs_hrow_rows', [])
    if not rows_sorted:
        await callback.answer("⚠️ Данные устарели, начните заново", show_alert=True)
        return
    await callback.answer()
    total_pages = max(1, -(-len(rows_sorted) // _HROW_PAGE_SIZE))
    wiz_back_cb = data.get('gs_wizard_back_cb', '')
    markup      = _hrow_page_kb(rows_sorted, page, wiz_back_cb=wiz_back_cb)
    text = (
        f"🔍 <b>Настройка матрицы — шаг 1/4</b>\n"
        f"<i>Стр. {page+1}/{total_pages} · всего строк: {len(rows_sorted)}</i>\n\n"
        "Выбери строку, в которой написаны <b>названия столбцов</b> "
        "(заголовки матрицы — товары, недели и т.п.):"
    )
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


def _idcol_page_kb(cols: list, page: int) -> tuple:
    """Build (markup, total_pages, clamped_page) for the ID-column picker, paginated."""
    from pagination_utils import paginate, page_nav_row, PAGE_SIZE_BTN
    page_items, has_prev, has_next, total_pages, page = paginate(cols, page, PAGE_SIZE_BTN)
    kb = InlineKeyboardBuilder()
    for i, hval, fval in page_items:
        label = _col_btn_label(i, hval, fval)
        kb.row(InlineKeyboardButton(text=label, callback_data=f"gs_lkp_idcol_{i}"))
    nav = page_nav_row("gs_lkp_idcol_pg_", page, has_prev, has_next, total_pages)
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="✏️ Ввести номер вручную",
                                callback_data="gs_lkp_idcol_manual"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="gs_lkp_bk_hrow"))
    return kb.as_markup(), total_pages, page


async def _show_idcol_picker(target, state: FSMContext,
                             header_row: list, first_data_row: list, page: int = 0):
    """Show column-picker buttons from the header row values, paginated."""
    cols = [(i, str(hval), str(first_data_row[i - 1] if i - 1 < len(first_data_row) else ""))
            for i, hval in enumerate(header_row, start=1)]
    await state.update_data(gs_idcol_cols=cols)
    markup, total_pages, page = _idcol_page_kb(cols, page)
    pg_line = f"\n<i>Стр. {page+1}/{total_pages} · всего: {len(cols)}</i>" if total_pages > 1 else ""
    text = (f"🔍 <b>Шаг 2/4 — Колонка с ID строк</b>{pg_line}\n\n"
            "Выбери колонку, в которой записаны <b>названия магазинов или продавцов</b> "
            "(то, по чему бот будет искать нужную строку):")
    try:
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await _fsm_edit(target, state, text, reply_markup=markup)


async def _start_lookup_wizard(message: Message, state: FSMContext):
    data = await state.get_data()
    conn_id  = data.get('gs_conn_id', '')
    exp_type = data.get('gs_exp_type', 'sales')
    wiz_back = f"gs_exp_type_{conn_id}_{exp_type}"
    await state.update_data(gs_lookup={}, gs_lookup_step=0, gs_wizard_back_cb=wiz_back)
    await state.set_state(IntegrationStates.waiting_lookup_step)

    data       = await state.get_data()
    sheet_name = data.get('gs_target_sheet', '')
    anchor_id  = data.get('anchor_msg_id')
    user_id    = message.chat.id
    # When called from a callback, message IS the anchor — don't delete it
    is_anchor  = (message.message_id == anchor_id)

    # Fetch up to 50 rows in ONE API call
    rows_data = {}
    try:
        provider, cfg = await _fetch_gs_config(user_id, state)
        if provider:
            rendered  = _render_sheet_macro(sheet_name)
            rows_data = await asyncio.wait_for(
                provider.get_first_rows(cfg, rendered, max_rows=50), timeout=10.0)
    except Exception:
        rows_data = {}

    if rows_data:
        # _show_hrow_picker needs a Message-like target with edit_text.
        # When is_anchor: message itself is the anchor → edit directly.
        # When NOT is_anchor: delete the user message, then show on anchor.
        if not is_anchor:
            await delete_message_safe(message)
            # Build a proxy target pointing to the anchor message
            class _AnchorProxy:
                def __init__(self, bot, chat_id, msg_id):
                    self.bot = bot; self.chat_id = chat_id; self.msg_id = msg_id
                async def edit_text(self, text, reply_markup=None, parse_mode=None):
                    await self.bot.edit_message_text(
                        text, chat_id=self.chat_id, message_id=self.msg_id,
                        reply_markup=reply_markup, parse_mode=parse_mode)
            target = _AnchorProxy(message.bot, message.chat.id, anchor_id)
        else:
            target = message
        await _show_hrow_picker(target, state, rows_data)
    else:
        fallback = (
            "🔍 <b>Настройка матрицы — шаг 1/4</b>\n\n"
            "Не удалось загрузить таблицу автоматически.\n\n"
            "<b>В какой строке написаны заголовки столбцов?</b>\n"
            "Введи номер строки. Пример: <code>6</code>"
        )
        _retry_kb = InlineKeyboardBuilder()
        _retry_kb.row(InlineKeyboardButton(
            text="🔄 Обновить", callback_data="gs_lkp_retry_hrow"))
        _retry_kb.row(InlineKeyboardButton(
            text="⬅️ Назад", callback_data=wiz_back))
        _retry_markup = _retry_kb.as_markup()
        if is_anchor:
            await message.edit_text(fallback, reply_markup=_retry_markup, parse_mode="HTML")
        else:
            await _fsm_edit(message, state, fallback, reply_markup=_retry_markup)


@integration_router.message(IntegrationStates.waiting_lookup_step)
async def gs_lookup_step(message: Message, state: FSMContext):
    """Fallback: manual text input when sheet API was unavailable."""
    text = message.text.strip()
    data = await state.get_data()
    step       = data.get('gs_lookup_step', 0)
    lookup     = data.get('gs_lookup', {})
    sheet_name = data.get('gs_target_sheet', '')
    user_id    = message.chat.id

    if step == 0:
        try:
            lookup['col_search_row'] = int(text)
        except ValueError:
            await _fsm_edit(message, state,
                            "❌ Нужно целое число. Пример: <code>6</code>")
            return
        await state.update_data(gs_lookup=lookup, gs_lookup_step=1)
        await _fsm_edit(
            message, state,
            "🔍 <b>Шаг 2/4 — Колонка с ID строк</b>\n\n"
            "В какой <b>колонке</b> хранятся названия магазинов/продавцов?\n"
            "1 = A,  2 = B,  3 = C…\nПример: <code>1</code>"
        )

    elif step == 1:
        try:
            lookup['row_search_col'] = int(text)
        except ValueError:
            await _fsm_edit(message, state,
                            "❌ Нужно целое число. Пример: <code>1</code>")
            return
        lookup['data_start_row'] = lookup.get('col_search_row', 1) + 1
        await state.update_data(gs_lookup=lookup, gs_lookup_step=2)
        await _fsm_edit(
            message, state,
            LOOKUP_BTN_STEPS['row_search_field']['prompt'],
            reply_markup=_lookup_btn_kb('row_search_field'),
        )
    else:
        await delete_message_safe(message)


@integration_router.callback_query(F.data.startswith("gs_lkp_"))
async def gs_lkp_field(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("gs_lkp_"):]
    await callback.answer()

    # ── back navigation ────────────────────────────────────────
    if raw == "bk_hrow":
        # Step 2 → back to step 1 (hrow picker)
        data = await state.get_data()
        rows_sorted = data.get('gs_hrow_rows') or []
        if not rows_sorted:
            await callback.message.edit_text("⚠️ Данные потеряны — начни мастер заново.")
            return
        wiz_back_cb = data.get('gs_wizard_back_cb', '')
        markup = _hrow_page_kb(rows_sorted, 0, wiz_back_cb=wiz_back_cb)
        total_pages = max(1, -(-len(rows_sorted) // _HROW_PAGE_SIZE))
        await callback.message.edit_text(
            f"🔍 <b>Настройка матрицы — шаг 1/4</b>\n"
            f"<i>Стр. 1/{total_pages} · всего строк: {len(rows_sorted)}</i>\n\n"
            "Выбери строку, в которой написаны <b>названия столбцов</b> "
            "(заголовки матрицы — товары, недели и т.п.):",
            reply_markup=markup, parse_mode="HTML"
        )
        return

    if raw == "retry_hrow":
        # Re-attempt loading rows from the sheet (step 1 retry)
        await _start_lookup_wizard(callback.message, state)
        return

    if raw == "retry_idcol":
        # Re-attempt loading column data from the sheet
        data = await state.get_data()
        lookup    = data.get('gs_lookup', {})
        row_num   = lookup.get('col_search_row')
        if not row_num:
            await callback.message.edit_text(
                "⚠️ Номер строки не найден — вернись на шаг 1.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ Назад", callback_data="gs_lkp_bk_hrow")
                ]])
            )
            return
        sheet_name = data.get('gs_target_sheet', '')
        user_id    = callback.from_user.id
        header_row = []
        first_drow = []
        try:
            provider, cfg = await _fetch_gs_config(user_id, state)
            if provider:
                rendered   = _render_sheet_macro(sheet_name)
                header_row = await asyncio.wait_for(
                    provider.read_row(cfg, rendered, row_num), timeout=6.0)
                first_drow = await asyncio.wait_for(
                    provider.read_row(cfg, rendered, row_num + 1), timeout=6.0)
        except Exception:
            pass
        if header_row:
            await state.update_data(gs_header_row=header_row, gs_first_drow=first_drow)
            await _show_idcol_picker(callback.message, state, header_row, first_drow, page=0)
        else:
            _retry_kb2 = InlineKeyboardBuilder()
            _retry_kb2.row(InlineKeyboardButton(
                text="🔄 Попробовать снова", callback_data="gs_lkp_retry_idcol"))
            _retry_kb2.row(InlineKeyboardButton(
                text="⬅️ Назад", callback_data="gs_lkp_bk_hrow"))
            await callback.message.edit_text(
                "⚠️ Всё равно не удалось загрузить данные колонок. Попробуй ещё раз.",
                reply_markup=_retry_kb2.as_markup()
            )
        return

    if raw == "bk_idcol":
        # Step 3 → back to step 2 (idcol picker)
        data = await state.get_data()
        cols = data.get('gs_idcol_cols') or []
        if not cols:
            header_row = data.get('gs_header_row', [])
            first_drow = data.get('gs_first_drow', [])
            cols = [(i, str(hval), str(first_drow[i-1] if i-1 < len(first_drow) else ""))
                    for i, hval in enumerate(header_row, start=1)]
        if not cols:
            await callback.message.edit_text("⚠️ Данные колонок потеряны — начни мастер заново.")
            return
        await _show_idcol_picker(callback.message, state, [c[1] for c in cols],
                                  [c[2] for c in cols], page=0)
        return

    # Step 4-6 → back to a previous LOOKUP_BTN_STEPS field
    for key in LOOKUP_BTN_STEPS:
        if raw == f"bk_{key}":
            await callback.message.edit_text(
                LOOKUP_BTN_STEPS[key]['prompt'],
                reply_markup=_lookup_btn_kb(key),
                parse_mode="HTML"
            )
            return

    # ── skip aliases ──────────────────────────────────────────
    if raw == "skip_aliases":
        await state.set_state(None)
        await _ask_schedule(callback.message, state)
        return

    # ── header-row picker (step 1) ────────────────────────────
    if raw == "hrow_manual":
        data = await state.get_data()
        wiz_back_cb = data.get('gs_wizard_back_cb', '')
        await state.update_data(gs_lookup_step=0)
        await state.set_state(IntegrationStates.waiting_lookup_step)
        _man_kb = InlineKeyboardBuilder()
        _man_kb.row(InlineKeyboardButton(
            text="🔄 Обновить", callback_data="gs_lkp_retry_hrow"))
        if wiz_back_cb:
            _man_kb.row(InlineKeyboardButton(
                text="⬅️ Назад", callback_data=wiz_back_cb))
        await callback.message.edit_text(
            "🔍 <b>Шаг 1/4 — строка заголовков</b>\n\n"
            "Введи номер строки, в которой написаны заголовки столбцов.\n"
            "Пример: <code>6</code>",
            reply_markup=_man_kb.as_markup(),
            parse_mode="HTML"
        )
        return

    if raw.startswith("hrow_"):
        row_num = int(raw[len("hrow_"):])
        data   = await state.get_data()
        lookup = data.get('gs_lookup', {})
        lookup['col_search_row'] = row_num
        await state.update_data(gs_lookup=lookup)

        # Fetch header row + first data row for column picker
        sheet_name = data.get('gs_target_sheet', '')
        user_id    = callback.from_user.id
        header_row  = []
        first_drow  = []
        try:
            provider, cfg = await _fetch_gs_config(user_id, state)
            if provider:
                rendered   = _render_sheet_macro(sheet_name)
                header_row = await asyncio.wait_for(
                    provider.read_row(cfg, rendered, row_num), timeout=6.0)
                first_drow = await asyncio.wait_for(
                    provider.read_row(cfg, rendered, row_num + 1), timeout=6.0)
        except Exception:
            pass

        if header_row:
            await state.update_data(gs_header_row=header_row,
                                    gs_first_drow=first_drow)
            await _show_idcol_picker(callback.message, state, header_row, first_drow, page=0)
        else:
            # API failed — fall back to text with retry option
            await state.update_data(gs_lookup_step=1)
            await state.set_state(IntegrationStates.waiting_lookup_step)
            _retry_kb = InlineKeyboardBuilder()
            _retry_kb.row(InlineKeyboardButton(
                text="🔄 Обновить", callback_data="gs_lkp_retry_idcol"))
            _retry_kb.row(InlineKeyboardButton(
                text="⬅️ Назад", callback_data="gs_lkp_bk_hrow"))
            await callback.message.edit_text(
                f"✅ Строка {row_num} выбрана.\n\n"
                "🔍 <b>Шаг 2/4 — Колонка с ID строк</b>\n\n"
                "Не удалось загрузить данные колонок автоматически.\n"
                "Введи номер колонки. 1 = A,  2 = B…\n"
                "Пример: <code>1</code>",
                reply_markup=_retry_kb.as_markup(),
                parse_mode="HTML"
            )
        return

    # ── id-column picker (step 2) ─────────────────────────────
    if raw == "idcol_manual":
        await state.update_data(gs_lookup_step=1)
        await state.set_state(IntegrationStates.waiting_lookup_step)
        _man2_kb = InlineKeyboardBuilder()
        _man2_kb.row(InlineKeyboardButton(
            text="🔄 Обновить", callback_data="gs_lkp_retry_idcol"))
        _man2_kb.row(InlineKeyboardButton(
            text="⬅️ Назад", callback_data="gs_lkp_bk_hrow"))
        await callback.message.edit_text(
            "🔍 <b>Шаг 2/4 — Колонка с ID строк</b>\n\n"
            "Введи номер колонки, в которой написаны названия магазинов/продавцов.\n"
            "1 = A,  2 = B,  3 = C…\nПример: <code>1</code>",
            reply_markup=_man2_kb.as_markup(),
            parse_mode="HTML"
        )
        return

    if raw.startswith("idcol_pg_"):
        pg = int(raw[len("idcol_pg_"):])
        data   = await state.get_data()
        cols   = data.get('gs_idcol_cols') or []
        if not cols:
            header_row = data.get('gs_header_row', [])
            first_drow = data.get('gs_first_drow', [])
            cols = [(i, str(hval), str(first_drow[i-1] if i-1 < len(first_drow) else ""))
                    for i, hval in enumerate(header_row, start=1)]
        markup, total_pages, pg = _idcol_page_kb(cols, pg)
        pg_line = f"\n<i>Стр. {pg+1}/{total_pages} · всего: {len(cols)}</i>" if total_pages > 1 else ""
        await callback.message.edit_text(
            f"🔍 <b>Шаг 2/4 — Колонка с ID строк</b>{pg_line}\n\n"
            "Выбери колонку, в которой записаны "
            "<b>названия магазинов или продавцов</b>:",
            reply_markup=markup, parse_mode="HTML"
        )
        return

    if raw.startswith("idcol_"):
        col_num = int(raw[len("idcol_"):])
        data   = await state.get_data()
        lookup = data.get('gs_lookup', {})
        lookup['row_search_col']  = col_num
        lookup['data_start_row']  = lookup.get('col_search_row', 1) + 1
        await state.update_data(gs_lookup=lookup)
        await callback.message.edit_text(
            LOOKUP_BTN_STEPS['row_search_field']['prompt'],
            reply_markup=_lookup_btn_kb('row_search_field'),
            parse_mode="HTML"
        )
        return

    # ── LOOKUP_BTN_STEPS fields (steps 3-6) ───────────────────
    field_key = None
    value     = None
    for key in LOOKUP_BTN_STEPS:
        if raw.startswith(key + "_"):
            field_key = key
            value     = raw[len(key) + 1:]
            break

    if field_key is None:
        return

    data   = await state.get_data()
    lookup = data.get('gs_lookup', {})
    lookup[field_key] = value

    next_key = LOOKUP_BTN_STEPS[field_key]['next']

    if next_key is not None:
        await state.update_data(gs_lookup=lookup)
        await callback.message.edit_text(
            LOOKUP_BTN_STEPS[next_key]['prompt'],
            reply_markup=_lookup_btn_kb(next_key),
            parse_mode="HTML"
        )
    else:
        await state.update_data(gs_lookup=lookup)
        await state.set_state(IntegrationStates.waiting_lookup_aliases)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="gs_lkp_skip_aliases"))
        await callback.message.edit_text(
            "📝 <b>Псевдонимы — необязательно</b>\n\n"
            "Если название магазина или товара <b>в боте отличается</b> от того, "
            "что написано в таблице — задай соответствие.\n\n"
            "Формат — <b>по одной паре в строке</b>:\n"
            "<code>Название в боте → Значение в таблице</code>\n\n"
            "<b>Например:</b>\n"
            "<code>Хуавей DNS → 2905</code>\n"
            "<code>Nova 14 → Nova 14i</code>\n\n"
            "Разделители: <code>→</code>  или  <code>-&gt;</code>  или  <code>:</code>\n\n"
            "Если названия <b>совпадают</b> — нажми «Пропустить».\n"
            "Псевдонимы можно добавить или изменить позже в настройках экспорта.",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )


@integration_router.message(IntegrationStates.waiting_lookup_aliases)
async def gs_lookup_alias_input(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    lookup = data.get('gs_lookup', {})
    aliases = _parse_aliases(text)
    lookup['aliases'] = aliases
    await state.update_data(gs_lookup=lookup)
    await state.set_state(None)
    await _ask_schedule(message, state)


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
    user_id = msg.chat.id
    current_db = await _get_db(user_id, state)

    exp_id = await current_db.add_integration_export(
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


# ═══════════════════════════════════════════════════════════
#  IMPORT WIZARD
# ═══════════════════════════════════════════════════════════

IMPORT_TYPE_LABELS = {
    'products':  '🛍 Товары (название, категория, цена)',
    'inventory': '📦 Остатки (магазин, товар, количество)',
    'sales':     '💰 Продажи (дата, товар, магазин, кол-во, цена)',
    'staff':     '👥 Сотрудники (имя, магазин, телефон)',
    'plans':     '📋 Планы (тип, метрика, цель, период, магазин, продавец)',
}

IMPORT_COL_HINTS = {
    'products':  'Колонки: <b>A — Название</b>, <b>B — Категория</b>, <b>C — Цена</b>',
    'inventory': 'Колонки: <b>A — Магазин</b>, <b>B — Товар</b>, <b>C — Количество</b>',
    'sales':     (
        'Колонки: <b>A — Дата</b> (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД), <b>B — Товар</b>,'
        ' <b>C — Магазин</b>, <b>D — Количество</b>, <b>E — Цена</b>\n'
        '⚠️ Товар должен существовать в системе. Остатки не проверяются.'
    ),
    'staff':     (
        'Колонки: <b>A — Имя Фамилия</b>, <b>B — Магазин</b>, <b>C — Телефон</b>\n'
        '⚠️ Обновляет магазин/телефон для существующих сотрудников (поиск по имени).'
    ),
    'plans':     (
        'Колонки: <b>A — Тип</b> (продавец/магазин), <b>B — Метрика</b> (оборот/количество),'
        ' <b>C — Цель</b>, <b>D — Период</b> (неделя/месяц), <b>E — Магазин</b>, <b>F — Продавец</b>\n'
        '⚠️ Для типа «продавец» продавец должен быть зарегистрирован в системе.'
    ),
}


@integration_router.callback_query(F.data.startswith("gs_import_"))
async def gs_import_menu(callback: CallbackQuery, state: FSMContext):
    """Выбор типа импорта из Google Sheets"""
    conn_id = int(callback.data.split("_")[2])
    if not check_integrations_permission(callback.from_user.id):
        await callback.answer()
        await callback.message.edit_text(
            "🔒 <b>Импорт из Google Таблиц — тариф «Стандарт» и выше</b>\n\n"
            "Для доступа к интеграциям выберите подходящий тариф:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Выбрать тариф",
                                      callback_data="subscription_plans")],
                [_back(f"gs_conn_{conn_id}")],
            ])
        )
        return
    current_db = await get_db(callback.from_user.id, state)
    conn = await current_db.get_integration_connection(conn_id)
    if not conn:
        await callback.answer("❌ Подключение не найдено", show_alert=True)
        return
    await callback.answer()
    await state.update_data(gs_import_conn_id=conn_id)
    kb = InlineKeyboardBuilder()
    for imp_type, label in IMPORT_TYPE_LABELS.items():
        kb.row(InlineKeyboardButton(
            text=label,
            callback_data=f"gs_imptyp_{imp_type}_{conn_id}"
        ))
    kb.row(_back(f"gs_conn_{conn_id}"))
    await callback.message.edit_text(
        "📥 <b>Импорт из Google Sheets</b>\n\n"
        "Выберите тип данных для импорта:\n\n"
        "• <b>Товары</b> — добавит новые товары (дубли пропускаются)\n"
        "• <b>Остатки</b> — установит кол-во остатков по магазинам\n"
        "• <b>Продажи</b> — импортирует историческую запись продаж\n"
        "• <b>Сотрудники</b> — обновит магазин/телефон существующих\n"
        "• <b>Планы</b> — создаст планы продаж из таблицы\n\n"
        "⚠️ Первая строка считается заголовком (можно изменить).",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )


@integration_router.callback_query(F.data.startswith("gs_imptyp_"))
async def gs_import_type_selected(callback: CallbackQuery, state: FSMContext):
    """Выбран тип импорта — запрашиваем имя листа"""
    parts = callback.data.split("_")
    imp_type = parts[2]
    conn_id = int(parts[3])
    if not check_integrations_permission(callback.from_user.id):
        await callback.answer("🔒 Требуется тариф «Стандарт» и выше", show_alert=True)
        return
    await callback.answer()
    await state.update_data(gs_import_type=imp_type, gs_import_conn_id=conn_id)
    hint = IMPORT_COL_HINTS.get(imp_type, '')
    await fsm_edit(
        state, callback.message,
        f"📥 <b>Импорт: {IMPORT_TYPE_LABELS.get(imp_type, imp_type)}</b>\n\n"
        f"{hint}\n\n"
        f"Введите <b>название листа</b> (вкладки) в таблице Google Sheets:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_back(f"gs_import_{conn_id}")]]
        ),
        parse_mode="HTML"
    )
    await state.set_state(GSImportStates.waiting_sheet_name)


@integration_router.message(GSImportStates.waiting_sheet_name)
async def gs_import_sheet_name(message: Message, state: FSMContext):
    """Получено имя листа — запрашиваем номер строки заголовков"""
    sheet_name = message.text.strip()
    data = await state.get_data()
    conn_id = data.get('gs_import_conn_id', 0)
    _kb = InlineKeyboardMarkup(
        inline_keyboard=[[_back(f"gs_import_{conn_id}")]]
    )
    if not sheet_name:
        await fsm_edit(state, message, "⚠️ Имя листа не может быть пустым. Введите снова:",
                       reply_markup=_kb, parse_mode="HTML")
        return
    await state.update_data(gs_import_sheet=sheet_name)
    await fsm_edit(
        state, message,
        f"📥 Лист: <code>{he(sheet_name)}</code>\n\n"
        f"Введите <b>номер строки с заголовками</b> (обычно <b>1</b>).\n"
        f"Данные будут импортированы начиная со следующей строки:",
        reply_markup=_kb,
        parse_mode="HTML"
    )
    await state.set_state(GSImportStates.waiting_header_row)


@integration_router.message(GSImportStates.waiting_header_row)
async def gs_import_header_row(message: Message, state: FSMContext):
    """Получен номер строки заголовков — запускаем импорт"""
    raw = message.text.strip()
    data = await state.get_data()
    conn_id = data.get('gs_import_conn_id', 0)
    imp_type = data.get('gs_import_type', 'products')
    sheet_name = data.get('gs_import_sheet', 'Sheet1')
    _kb = InlineKeyboardMarkup(
        inline_keyboard=[[_back(f"gs_import_{conn_id}")]]
    )
    try:
        header_row = int(raw)
        if header_row < 1:
            raise ValueError
    except ValueError:
        await fsm_edit(state, message,
                       "⚠️ Введите корректный номер строки (например, 1):",
                       reply_markup=_kb, parse_mode="HTML")
        return

    await fsm_edit(
        state, message,
        f"⏳ <b>Импорт из Google Sheets...</b>\n\n"
        f"📋 Лист: <code>{he(sheet_name)}</code>\n"
        f"📊 Тип: {IMPORT_TYPE_LABELS.get(imp_type, imp_type)}\n\n"
        f"Пожалуйста, подождите.",
        parse_mode="HTML"
    )
    await clear_state_keep_org(state)

    current_db = await get_db(message.from_user.id, state)
    try:
        result = await integration_manager.run_import(
            db=current_db,
            conn_id=conn_id,
            import_type=imp_type,
            sheet_name=sheet_name,
            header_row=header_row,
        )
        imported = result['imported']
        skipped = result['skipped']
        total = result.get('total', imported + skipped)
        errors = result.get('errors', [])
        headers = result.get('headers', [])

        summary = (
            f"✅ <b>Импорт завершён</b>\n\n"
            f"📊 Тип: {IMPORT_TYPE_LABELS.get(imp_type, imp_type)}\n"
            f"📋 Лист: <code>{he(sheet_name)}</code>\n"
            f"📥 Строк в таблице: {total}\n"
            f"✅ Импортировано: {imported}\n"
            f"⏭ Пропущено (дубли/не найдено): {skipped}\n"
        )
        if headers:
            summary += f"\n<b>Заголовки листа:</b> {he(', '.join(str(h) for h in headers[:8]))}\n"
        if errors:
            summary += f"\n⚠️ Ошибки ({len(errors)}):\n"
            for err in errors[:5]:
                summary += f"  • {he(str(err))}\n"
    except Exception as e:
        logger.error(f"gs_import error: {e}")
        summary = f"❌ <b>Ошибка импорта:</b>\n{he(str(e))}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Ещё импорт",
                              callback_data=f"gs_import_{conn_id}")],
        [_back(f"gs_conn_{conn_id}")],
    ])
    try:
        anchor_id = data.get('anchor_msg_id')
        if anchor_id:
            await _edit_anchor(message.bot, message.chat.id, anchor_id,
                               summary, reply_markup=kb)
        else:
            await message.answer(summary, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(summary, reply_markup=kb, parse_mode="HTML")
