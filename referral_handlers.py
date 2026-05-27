"""
Реферальная программа: владелец получает +30 дней за каждого нового клиента по реф-ссылке.
Deep-link: /start ref_TELEGRAMID
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from keyboards import back_button
from env_manager import env_manager

logger = logging.getLogger(__name__)
referral_router = Router()


@referral_router.callback_query(F.data == "subscription_referral")
async def referral_menu(callback: CallbackQuery, state: FSMContext):
    """Реферальная программа"""
    await callback.answer()
    telegram_id = callback.from_user.id

    from database import Database
    db = Database('data/shop_bot.db')

    total, applied, bonus_days = db.get_referral_stats(telegram_id)

    try:
        bot_info = await callback.bot.get_me()
        bot_username = bot_info.username
        ref_link = f"https://t.me/{bot_username}?start=ref_{telegram_id}"
    except Exception:
        ref_link = f"/start ref_{telegram_id}"

    text = (
        "🔗 <b>Реферальная программа</b>\n\n"
        "Поделитесь ссылкой — за каждого нового клиента (создавшего организацию), "
        "который перейдёт по вашей ссылке, вы получаете <b>+30 дней</b> к подписке.\n\n"
        f"🔗 <b>Ваша ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Перешли по ссылке: <b>{total}</b>\n"
        f"• Подтверждено (орг создана): <b>{applied}</b>\n"
        f"• Заработано дней: <b>+{bonus_days}</b>\n\n"
        f"<i>Бонус начисляется автоматически при создании организации приглашённым.</i>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("subscription_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
