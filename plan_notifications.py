"""
Модуль для отправки уведомлений пользователям об изменениях в тарифных планах
"""
import asyncio
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from env_manager import env_manager
from utils import he

async def send_plan_deletion_notifications(bot: Bot, affected_users, plan_name):
    """Отправка уведомлений о удалении тарифного плана"""
    if not affected_users:
        return
    
    for user in affected_users:
        try:
            telegram_id = user[1]  # telegram_id находится во втором поле
            first_name = user[2]   # first_name в третьем
            last_name = user[3]    # last_name в четвертом
            plan_type = user[11]   # plan_type
            end_date = user[13]    # end_date
            
            # Парсим дату окончания
            end_date_formatted = end_date[:10] if end_date else "неизвестно"
            
            text = f"⚠️ <b>Важное уведомление о вашей подписке</b>\n\n"
            text += f"Здравствуйте, {he(first_name)}!\n\n"
            text += f"Сообщаем вам, что тарифный план <b>'{he(plan_name)}'</b> был удален из системы.\n\n"
            text += f"🔒 <b>Что это означает для вас:</b>\n"
            text += f"• Ваша текущая подписка остается активной до {end_date_formatted}\n"
            text += f"• Все функции и лимиты сохраняются до окончания срока\n"
            text += f"• После окончания подписки продление будет недоступно\n\n"
            text += f"💡 <b>Рекомендуем:</b>\n"
            text += f"• Ознакомиться с новыми доступными тарифами\n"
            text += f"• Выбрать подходящий план заранее\n"
            text += f"• Обратиться в поддержку при вопросах\n\n"
            text += f"Приносим извинения за неудобства."
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Посмотреть тарифы", callback_data="subscription_plans")],
                [InlineKeyboardButton(text="📞 Поддержка", callback_data="support_contact")]
            ])
            
            await bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
            # Небольшая пауза между отправками
            await asyncio.sleep(0.5)
            
        except Exception:
            pass

async def send_plan_deactivation_notifications(bot: Bot, affected_users, plan_name):
    """Отправка уведомлений о деактивации тарифного плана"""
    if not affected_users:
        return
    
    for user in affected_users:
        try:
            telegram_id = user[1]  # telegram_id находится во втором поле
            first_name = user[2]   # first_name в третьем
            last_name = user[3]    # last_name в четвертом
            plan_type = user[11]   # plan_type
            end_date = user[13]    # end_date
            
            # Парсим дату окончания
            end_date_formatted = end_date[:10] if end_date else "неизвестно"
            
            text = f"📢 <b>Уведомление об изменении тарифа</b>\n\n"
            text += f"Здравствуйте, {he(first_name)}!\n\n"
            text += f"Сообщаем вам, что тарифный план <b>'{he(plan_name)}'</b> был временно деактивирован.\n\n"
            text += f"🔒 <b>Что это означает для вас:</b>\n"
            text += f"• Ваша текущая подписка остается активной до {end_date_formatted}\n"
            text += f"• Все функции и лимиты сохраняются до окончания срока\n"
            text += f"• Новые подписки на этот тариф временно недоступны\n"
            text += f"• Автопродление отключено\n\n"
            text += f"💡 <b>Рекомендуем:</b>\n"
            text += f"• Следить за обновлениями о восстановлении тарифа\n"
            text += f"• Рассмотреть альтернативные планы\n"
            text += f"• Обратиться в поддержку для получения информации\n\n"
            text += f"Мы постараемся решить вопрос в кратчайшие сроки."
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Посмотреть тарифы", callback_data="subscription_plans")],
                [InlineKeyboardButton(text="📞 Поддержка", callback_data="support_contact")]
            ])
            
            await bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
            # Небольшая пауза между отправками
            await asyncio.sleep(0.5)
            
        except Exception:
            pass

async def send_plan_reactivation_notifications(bot: Bot, affected_users, plan_name):
    """Отправка уведомлений о восстановлении тарифного плана"""
    if not affected_users:
        return
    
    for user in affected_users:
        try:
            telegram_id = user[1]  # telegram_id находится во втором поле
            first_name = user[2]   # first_name в третьем
            
            text = f"✅ <b>Хорошие новости!</b>\n\n"
            text += f"Здравствуйте, {he(first_name)}!\n\n"
            text += f"Сообщаем вам, что тарифный план <b>'{he(plan_name)}'</b> снова доступен!\n\n"
            text += f"🎉 <b>Что изменилось:</b>\n"
            text += f"• Тариф восстановлен и доступен для новых подписок\n"
            text += f"• Автопродление снова работает\n"
            text += f"• Все функции плана полностью активны\n\n"
            text += f"💎 Теперь вы снова можете продлевать подписку на этот тариф!"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Продлить подписку", callback_data="subscription_plans")],
                [InlineKeyboardButton(text="ℹ️ Подробнее о тарифе", callback_data=f"plan_details_{plan_name}")]
            ])
            
            await bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
            # Небольшая пауза между отправками
            await asyncio.sleep(0.5)
            
        except Exception:
            pass

async def send_admin_notification(bot: Bot, action_type, plan_name, affected_count, admin_id):
    """Отправка уведомления администратору о выполненном действии"""
    try:
        if action_type == "delete":
            action_text = "удален"
            icon = "🗑"
        elif action_type == "deactivate":
            action_text = "деактивирован"
            icon = "❌"
        elif action_type == "activate":
            action_text = "активирован"
            icon = "✅"
        else:
            action_text = "изменен"
            icon = "🔄"
        
        text = f"{icon} <b>Действие выполнено</b>\n\n"
        text += f"Тариф <b>'{he(plan_name)}'</b> {action_text}\n"
        
        if affected_count > 0:
            text += f"👥 Уведомления отправлены {affected_count} пользователям\n"
            text += f"📩 Все затронутые пользователи проинформированы об изменениях"
        else:
            text += f"📭 Пользователей с активными подписками не найдено"
        
        await bot.send_message(
            chat_id=admin_id,
            text=text,
            parse_mode="HTML"
        )
        
    except Exception:
        pass