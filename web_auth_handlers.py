import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("weblogin"))
async def cmd_weblogin(message: Message):
    """Генерирует одноразовый код входа в веб-интерфейс и отправляет его пользователю."""
    telegram_id = message.from_user.id
    try:
        from web_login_codes import generate_code
        code = generate_code(telegram_id)
        await message.answer(
            f"🔐 <b>Код входа в веб-интерфейс</b>\n\n"
            f"Ваш код:\n"
            f"<code>{code}</code>\n\n"
            f"⏱ Действует <b>5 минут</b>\n"
            f"🔒 Одноразовый — после использования сгорает\n\n"
            f"Введите этот код на странице входа в DailySales.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"cmd_weblogin error for {telegram_id}: {e}")
        await message.answer("❌ Не удалось создать код. Попробуйте чуть позже.")
