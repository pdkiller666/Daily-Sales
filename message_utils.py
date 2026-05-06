"""
Утилиты для работы с сообщениями
"""
import logging
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext


async def safe_edit_message(callback: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    """Безопасное редактирование сообщения с обработкой ошибок"""
    try:
        if callback.message:
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            await callback.answer()
        else:
            await callback.answer(text, show_alert=True)
    except TelegramBadRequest as e:
        error_text = str(e).lower()
        if "message is not modified" in error_text:
            await callback.answer("Данные уже актуальны", show_alert=False)
        elif "there is no text in the message to edit" in error_text:
            await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            await callback.answer()
        else:
            logging.error(f"Ошибка редактирования сообщения: {e}")
            await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            await callback.answer()
    except Exception as e:
        logging.error(f"Неожиданная ошибка при редактировании сообщения: {e}")
        try:
            await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            await callback.answer()
        except Exception:
            pass


async def safe_answer_callback(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    """Безопасный ответ на callback с обработкой ошибок"""
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as e:
        logging.error(f"Ошибка ответа на callback: {e}")


async def delete_message_safe(message: Message):
    """Безопасное удаление сообщения"""
    try:
        await message.delete()
    except Exception as e:
        logging.error(f"Ошибка удаления сообщения: {e}")


async def fsm_edit(
    state: FSMContext,
    message: Message,
    text: str,
    reply_markup=None,
    parse_mode: str = "HTML",
) -> None:
    """
    Хелпер для FSM text-input шагов.

    Логика:
      1. Удаляет сообщение пользователя.
      2. Редактирует «якорное» сообщение бота, ID которого хранится в state['anchor_msg_id'].
      3. Если якорь отсутствует или редактирование провалилось — отправляет новое сообщение
         и обновляет anchor_msg_id в state.

    Использование:
      В callback-точке входа FSM:
          await state.update_data(anchor_msg_id=callback.message.message_id)
      В каждом message-хендлере FSM:
          await fsm_edit(state, message, "Текст подсказки", reply_markup=kb)
    """
    await delete_message_safe(message)

    data = await state.get_data()
    anchor_id = data.get("anchor_msg_id")

    if anchor_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=anchor_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                logging.error(f"fsm_edit: не удалось отредактировать якорь #{anchor_id}: {e}")

    # Fallback: якоря нет или редактирование провалилось
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    await state.update_data(anchor_msg_id=sent.message_id)
