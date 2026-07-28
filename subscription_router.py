"""
Роутер для обработчиков подписок
"""

from aiogram import Router
from aiogram.filters import StateFilter, Command
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from subscription_handlers import (
    subscription_menu, subscription_plans, start_subscription_purchase,
    upload_payment_proof, process_payment_proof, subscription_limits,
    handle_scheduled_purchase, handle_immediate_purchase,
    enter_promocode, process_promocode, proceed_to_payment,
    check_yookassa_payment, buy_modules, start_module_purchase,
)
from payment_admin_handlers import (
    pending_payments_menu, pending_payments_command,
    view_payment_request, show_payment_proof,
    confirm_payment_request, reject_payment_request
)
from states import SubscriptionStates

# Создаем роутер
subscription_router = Router()

# Обработчики для пользователей
subscription_router.callback_query.register(subscription_menu, lambda c: c.data == "subscription_menu")
subscription_router.callback_query.register(subscription_plans, lambda c: c.data == "subscription_plans")
subscription_router.callback_query.register(subscription_limits, lambda c: c.data == "subscription_limits")
subscription_router.callback_query.register(start_subscription_purchase, lambda c: c.data.startswith("subscribe_"))
subscription_router.callback_query.register(handle_scheduled_purchase, lambda c: c.data.startswith("schedule_"))
subscription_router.callback_query.register(handle_immediate_purchase, lambda c: c.data.startswith("immediate_"))
subscription_router.callback_query.register(upload_payment_proof, lambda c: c.data.startswith("upload_payment_proof_"))

# Самостоятельное подключение модулей/пакетов (G1)
subscription_router.callback_query.register(buy_modules, lambda c: c.data == "buy_modules")
subscription_router.callback_query.register(start_module_purchase, lambda c: c.data.startswith("buymod_") or c.data.startswith("buybnd_"))

# Обработчики промокодов
subscription_router.callback_query.register(enter_promocode, lambda c: c.data.startswith("enter_promocode_"))
subscription_router.callback_query.register(proceed_to_payment, lambda c: c.data.startswith("proceed_payment_"))

# Обработчики для загрузки чека и промокодов
subscription_router.message.register(
    process_payment_proof, 
    StateFilter(SubscriptionStates.waiting_payment_proof)
)
subscription_router.message.register(
    process_promocode,
    StateFilter(SubscriptionStates.waiting_for_promocode)
)

# ЮKassa: проверка статуса платежа пользователем
subscription_router.callback_query.register(check_yookassa_payment, lambda c: c.data.startswith("yk_check_"))

# Административные обработчики
# Команда /pending_payments (текст в чате) — для супер-админа
subscription_router.message.register(pending_payments_command, Command("pending_payments"))
subscription_router.callback_query.register(pending_payments_menu, lambda c: c.data == "pending_payments")
subscription_router.callback_query.register(view_payment_request, lambda c: c.data.startswith("view_payment_"))
subscription_router.callback_query.register(show_payment_proof, lambda c: c.data.startswith("show_payment_proof_"))
subscription_router.callback_query.register(confirm_payment_request, lambda c: c.data.startswith("confirm_payment_"))
subscription_router.callback_query.register(reject_payment_request, lambda c: c.data.startswith("reject_payment_"))