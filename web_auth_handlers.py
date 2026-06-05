import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()

SHOP_BOT_DB = 'data/shop_bot.db'


class SetWebLoginState(StatesGroup):
    waiting_email = State()


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


@router.message(Command("setweblogin"))
async def cmd_setweblogin(message: Message, state: FSMContext):
    """Привязывает Telegram-аккаунт к существующему email-аккаунту DailySales."""
    await state.set_state(SetWebLoginState.waiting_email)
    await message.answer(
        "🔗 <b>Привязка Telegram к email-аккаунту</b>\n\n"
        "Введите email, с которым вы зарегистрировались в веб-кабинете DailySales.\n\n"
        "Это позволит входить в веб-интерфейс как через Telegram, так и по email + паролю.\n\n"
        "<i>Напишите /cancel для отмены.</i>",
        parse_mode="HTML",
    )


@router.message(SetWebLoginState.waiting_email)
async def process_setweblogin_email(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, введите email текстом.")
        return

    if message.text.strip().lower() == '/cancel':
        await state.clear()
        await message.answer("Отменено.")
        return

    email = message.text.strip().lower()
    if '@' not in email or '.' not in email.split('@')[-1]:
        await message.answer(
            "❌ Некорректный email-адрес. Попробуйте ещё раз или напишите /cancel."
        )
        return

    telegram_id = message.from_user.id
    try:
        from database import Database
        db = Database(SHOP_BOT_DB)
        result = db.link_web_credential_to_telegram(email, telegram_id)
    except Exception as exc:
        logger.error("process_setweblogin_email: %s", exc)
        result = 'error'

    await state.clear()

    if result == 'ok':
        await message.answer(
            f"✅ <b>Email привязан!</b>\n\n"
            f"Теперь вы можете входить в веб-кабинет DailySales как через Telegram, "
            f"так и по адресу <code>{email}</code> с вашим паролем.",
            parse_mode="HTML",
        )
    elif result == 'not_found':
        await message.answer(
            "❌ <b>Email не найден.</b>\n\n"
            "Убедитесь, что вы уже зарегистрировались в веб-кабинете по этому адресу. "
            "Если нет — перейдите на страницу регистрации: /register",
            parse_mode="HTML",
        )
    elif result == 'already_linked':
        await message.answer(
            "⚠️ <b>Этот email уже привязан к другому Telegram-аккаунту.</b>\n\n"
            "Обратитесь к администратору, если это ошибка.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "❌ Ошибка сервера. Попробуйте позже или обратитесь к администратору."
        )


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
