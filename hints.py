"""Система онбординга и контекстных подсказок.

Логика:
- Каждая подсказка показывается ровно один раз (хранится в user_hints_seen).
- hint_suffix() — добавляет короткую подсказку в конец текста при первом визите.
- maybe_send_welcome() — отправляет popup при первом входе (попап закрывается кнопкой).
"""

from __future__ import annotations
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


HINT_TEXTS: dict[str, str] = {
    'first_sale': (
        "💡 <i>Добавьте несколько товаров в корзину — продажа оформляется одним чеком. "
        "⭐ Избранное и 🔄 Недавние помогают быстро найти частые позиции.</i>"
    ),
    'first_reports': (
        "💡 <i>Переключайте период кнопками вверху экрана. "
        "Кнопка 🔍 Фильтр позволяет смотреть данные по конкретному магазину или городу.</i>"
    ),
    'first_products': (
        "💡 <i>Добавляйте товары вручную или загружайте сразу из Excel через 📊 Импорт. "
        "Остатки по магазинам — в разделе 📦 Склад.</i>"
    ),
    'first_dashboard': (
        "💡 <i>Переключайте период — Сегодня / Неделя / Месяц. "
        "Нажмите на строку смены, чтобы увидеть её подробности.</i>"
    ),
    'first_plans': (
        "💡 <i>Планы ставятся по продавцу или по магазину на неделю / месяц. "
        "При достижении 50%, 75% и 100% цели — автоматическое уведомление.</i>"
    ),
    'first_contests': (
        "💡 <i>Конкурс завершается автоматически в указанную дату. "
        "Победитель определяется по обороту или количеству с учётом выбранных магазинов.</i>"
    ),
    'first_rankings': (
        "💡 <i>Рейтинги обновляются в реальном времени. "
        "Если вы не в топ-10 — ваша позиция всё равно показывается внизу списка.</i>"
    ),
}

WELCOME_USER_TEXT = (
    "👋 <b>Добро пожаловать в ShopBot!</b>\n\n"
    "Вот что вам доступно:\n\n"
    "💰 <b>Продажи</b> — фиксируйте каждую продажу, корзина поддерживает несколько позиций\n"
    "📊 <b>Отчёты</b> — аналитика за сегодня, неделю и месяц\n"
    "📦 <b>Склад</b> — остатки товаров по магазинам\n"
    "🔔 <b>Уведомления</b> — напоминания о продажах, остатках и планах\n"
    "👤 <b>Профиль</b> — часовой пояс, контакты, ваш магазин\n\n"
    "Всё управляется кнопками — ничего лишнего. Удачных продаж! 🚀"
)

WELCOME_ADMIN_TEXT = (
    "🔑 <b>Вы — администратор организации!</b>\n\n"
    "Помимо стандартных функций вам доступно:\n\n"
    "👥 <b>Сотрудники</b> — инвайт-код, роли, доступ по магазину / городу / сети\n"
    "📊 <b>Дашборд</b> — сводка по всем магазинам за любой период\n"
    "🏆 <b>Рейтинги</b> — лучшие продавцы, магазины и города\n"
    "📋 <b>Планы продаж</b> — цели по обороту / количеству с уведомлениями о прогрессе\n"
    "🎯 <b>Конкурсы</b> — мотивируйте команду соревнованием с денежными призами\n\n"
    "Начните с приглашения сотрудников через инвайт-код в разделе «Сотрудники». 🚀"
)


def _dismiss_kb(hint_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Понятно!", callback_data=f"hint_dismiss:{hint_key}")]
    ])


def hint_suffix(db, user_id: int, hint_key: str) -> str:
    """Возвращает текст подсказки и помечает её как показанную.

    При первом визите → "\n\n💡 <i>...</i>".
    При повторных визитах → "" (экран остаётся чистым).
    Весь код завёрнут в try/except — подсказки никогда не ломают основной экран.
    Поддерживает как Database, так и AsyncDatabase (через sync-доступ к _db).
    """
    try:
        _sync = getattr(db, '_db', db)
        if _sync.has_seen_hint(user_id, hint_key):
            return ""
        _sync.mark_hint_seen(user_id, hint_key)
        text = HINT_TEXTS.get(hint_key, "")
        return f"\n\n{text}" if text else ""
    except Exception:
        return ""


async def maybe_send_welcome(message_or_callback, db, user_id: int, is_admin: bool) -> None:
    """Отправляет онбординг-попап при первом входе — ровно один раз.

    message_or_callback — aiogram Message или CallbackQuery.
    is_admin — True для владельцев / администраторов организации.
    Поддерживает как Database, так и AsyncDatabase (через sync-доступ к _db).
    """
    from aiogram.types import Message as _Msg
    hint_key = "welcome_admin" if is_admin else "welcome_user"
    try:
        _sync = getattr(db, '_db', db)
        if _sync.has_seen_hint(user_id, hint_key):
            return
        _sync.mark_hint_seen(user_id, hint_key)
        text = WELCOME_ADMIN_TEXT if is_admin else WELCOME_USER_TEXT
        if isinstance(message_or_callback, _Msg):
            await message_or_callback.answer(
                text, parse_mode="HTML", reply_markup=_dismiss_kb(hint_key)
            )
        else:
            await message_or_callback.message.answer(
                text, parse_mode="HTML", reply_markup=_dismiss_kb(hint_key)
            )
    except Exception:
        pass
