import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("weblogin"))
async def cmd_weblogin(message: Message):
    """Генерирует одноразовый код входа в веб-интерфейс и отправляет его пользователю."""
    telegram_id = message.from_user.id
    try:
        from web_login_codes import generate_code
        from keyboards import _get_web_interface_url
        code = generate_code(telegram_id)
        web_url = _get_web_interface_url()
        if web_url:
            login_url = f"{web_url.rstrip('/')}/auth/code/auto?c={code}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Войти в веб-интерфейс", url=login_url)],
            ])
            await message.answer(
                f"🌐 <b>Веб-интерфейс</b>\n\n"
                f"Нажмите кнопку ниже для входа.\n"
                f"⏱ Ссылка одноразовая, действует <b>5 минут</b>.",
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await message.answer(
                f"🔐 <b>Код входа в веб-интерфейс:</b>\n\n"
                f"<code>{code}</code>\n\n"
                f"⏱ Действует <b>5 минут</b> · Одноразовый\n\n"
                f"Введите на странице /auth/code",
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"cmd_weblogin error for {message.from_user.id}: {e}")
        await message.answer("❌ Не удалось создать ссылку. Попробуйте позже.")


@router.callback_query(F.data == "web_open_hub")
async def cb_web_open_hub(callback: CallbackQuery):
    """Генерирует magic link для входа в веб-интерфейс и показывает кнопку."""
    telegram_id = callback.from_user.id
    try:
        from keyboards import _get_web_interface_url
        web_url = _get_web_interface_url()
        if not web_url:
            await callback.answer("Веб-интерфейс не настроен", show_alert=True)
            return

        from web_login_codes import generate_code
        code = generate_code(telegram_id)
        login_url = f"{web_url.rstrip('/')}/auth/code/auto?c={code}"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Войти в веб-интерфейс", url=login_url)],
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")],
        ])
        await callback.message.edit_text(
            f"🌐 <b>Веб-интерфейс</b>\n\n"
            f"Нажмите кнопку ниже для входа.\n"
            f"⏱ Ссылка одноразовая, действует <b>5 минут</b>.\n\n"
            f"<i>Каждое нажатие генерирует новую ссылку — старые сгорают.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"cb_web_open_hub error for {telegram_id}: {e}")
        await callback.answer("❌ Ошибка. Попробуйте ещё раз.", show_alert=True)
