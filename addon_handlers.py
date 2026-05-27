"""
Надстройки (add-ons) к подписке: дополнительные магазины и товары на 30 дней.
"""
import logging
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from keyboards import back_button
from message_utils import fsm_edit
from db_utils import clear_state_keep_org
from states import AddonStates

logger = logging.getLogger(__name__)
addon_router = Router()

ADDON_INFO = {
    'extra_shops': {
        'label': '🏪 Дополнительный магазин',
        'description': '+1 к лимиту магазинов на 30 дней',
        'price': 150,
        'days': 30,
        'addon_key': 'shops',
    },
    'extra_products': {
        'label': '📦 +100 товаров',
        'description': '+100 к лимиту товаров на 30 дней',
        'price': 100,
        'days': 30,
        'addon_key': 'products',
    },
}

_ADDON_LABEL = {
    'addon_shops_1': '🏪 Доп. магазин (+1)',
    'addon_products_1': '📦 +100 товаров',
}


@addon_router.callback_query(F.data == "subscription_addons")
async def addons_menu(callback: CallbackQuery, state: FSMContext):
    """Меню надстроек"""
    await callback.answer()
    telegram_id = callback.from_user.id

    from database import Database
    db = Database('data/shop_bot.db')
    totals = db.get_addon_totals(telegram_id)

    extra_shops = totals.get('extra_shops', 0)
    extra_prods = totals.get('extra_products', 0)

    text = (
        "➕ <b>Надстройки (Add-ons)</b>\n\n"
        "Расширьте возможности без смены тарифа.\n"
        "Надстройки суммируются с лимитами текущего тарифа на 30 дней.\n\n"
        "<b>Ваши активные надстройки:</b>\n"
        f"• Доп. магазины: <b>+{extra_shops}</b>\n"
        f"• Доп. товары: <b>+{extra_prods * 100}</b>\n\n"
        "<b>Доступно для покупки:</b>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🏪 +1 магазин — 150₽/30 дней",
            callback_data="addon_buy_shops_1"
        )],
        [InlineKeyboardButton(
            text="📦 +100 товаров — 100₽/30 дней",
            callback_data="addon_buy_products_1"
        )],
        [back_button("subscription_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@addon_router.callback_query(F.data.startswith("addon_buy_"))
async def addon_buy(callback: CallbackQuery, state: FSMContext):
    """Показ реквизитов для покупки надстройки"""
    await callback.answer()
    # addon_buy_shops_1 → addon_type='shops', qty=1
    # addon_buy_products_1 → addon_type='products', qty=1
    rest = callback.data[len("addon_buy_"):]
    parts = rest.rsplit('_', 1)
    if len(parts) != 2:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    addon_key = parts[0]   # 'shops' or 'products'
    try:
        qty = int(parts[1])
    except ValueError:
        qty = 1

    # Нормализуем ключ для ADDON_INFO
    full_key = f"extra_{addon_key}"
    if full_key not in ADDON_INFO:
        await callback.answer("❌ Неизвестная надстройка", show_alert=True)
        return

    info = ADDON_INFO[full_key]
    total_price = info['price'] * qty
    plan_type = f"addon_{addon_key}_{qty}"   # e.g. addon_shops_1

    # Получаем реквизиты оплаты
    from database import Database
    db = Database('data/shop_bot.db')
    payment_settings = db.get_payment_settings()
    card_number = payment_settings.get('card_number', 'Не указан')
    recipient = payment_settings.get('recipient_name', 'Не указан')
    bank_name = payment_settings.get('bank_name', '')
    provider = db.get_payment_provider()

    await state.update_data(
        addon_plan_type=plan_type,
        addon_amount=total_price,
        anchor_msg_id=callback.message.message_id
    )

    if provider == 'yookassa':
        try:
            _db2 = Database('data/shop_bot.db')
            user_id = _db2.get_user_id(callback.from_user.id)
            if user_id:
                from payment_provider import create_yookassa_payment
                pay_url = await create_yookassa_payment(user_id, plan_type, total_price, _db2)
                if pay_url:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
                        [back_button("subscription_addons")]
                    ])
                    await callback.message.edit_text(
                        f"➕ <b>{info['label']}</b>\n\n"
                        f"📋 {info['description']}\n"
                        f"💰 <b>Стоимость:</b> {total_price}₽\n\n"
                        "Нажмите кнопку для оплаты через ЮKassa:",
                        reply_markup=kb,
                        parse_mode="HTML"
                    )
                    return
        except Exception:
            pass

    # СБП — показываем реквизиты
    text = (
        f"➕ <b>{info['label']}</b>\n\n"
        f"📋 {info['description']}\n"
        f"💰 <b>Стоимость:</b> {total_price}₽\n\n"
        "💳 <b>Реквизиты для оплаты (СБП):</b>\n"
        f"• Номер: <code>{card_number}</code>\n"
        f"• Получатель: {recipient}\n"
    )
    if bank_name:
        text += f"• Банк: {bank_name}\n"
    text += (
        f"\n<b>Сумма перевода: {total_price}₽</b>\n\n"
        "После оплаты нажмите кнопку ниже и отправьте скриншот чека:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📎 Прикрепить чек об оплате",
            callback_data="addon_upload_proof"
        )],
        [back_button("subscription_addons")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@addon_router.callback_query(F.data == "addon_upload_proof")
async def addon_upload_proof_start(callback: CallbackQuery, state: FSMContext):
    """Запрос скриншота оплаты надстройки"""
    await callback.answer()
    data = await state.get_data()
    if not data.get('addon_plan_type'):
        await callback.answer("❌ Выберите надстройку снова", show_alert=True)
        return

    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(AddonStates.waiting_for_proof)

    await callback.message.edit_text(
        "📎 <b>Отправьте скриншот чека об оплате</b> (фото):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="subscription_addons")]
        ]),
        parse_mode="HTML"
    )


@addon_router.message(AddonStates.waiting_for_proof)
async def addon_proof_photo(message: Message, state: FSMContext):
    """Приём скриншота оплаты надстройки"""
    if not message.photo:
        await fsm_edit(
            state, message,
            "📎 Нужно прислать <b>фото</b> чека об оплате:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="subscription_addons")]
            ])
        )
        return

    data = await state.get_data()
    plan_type = data.get('addon_plan_type')
    amount = float(data.get('addon_amount', 0))

    if not plan_type:
        await fsm_edit(
            state, message,
            "❌ Данные потеряны. Выберите надстройку заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Надстройки", callback_data="subscription_addons")]
            ])
        )
        await clear_state_keep_org(state)
        return

    file_id = message.photo[-1].file_id

    from database import Database
    db = Database('data/shop_bot.db')
    user_id = db.get_user_id(message.from_user.id)
    if not user_id:
        await fsm_edit(
            state, message,
            "❌ Пользователь не найден. Попробуйте через /start.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Надстройки", callback_data="subscription_addons")]
            ])
        )
        await clear_state_keep_org(state)
        return

    try:
        success = db.create_payment_request(user_id, plan_type, amount, file_id)
    except Exception as e:
        logger.error(f"addon_proof_photo: create_payment_request error: {e}")
        success = False

    label = _ADDON_LABEL.get(plan_type, plan_type)

    if success:
        try:
            from subscription_handlers import notify_admins_about_payment_request
            await notify_admins_about_payment_request(message.bot, user_id, plan_type, amount)
        except Exception:
            pass

        await fsm_edit(
            state, message,
            f"✅ <b>Заявка отправлена!</b>\n\n"
            f"➕ <b>Надстройка:</b> {label}\n"
            f"💰 <b>Сумма:</b> {amount:.0f}₽\n\n"
            "⏳ Заявка обрабатывается администратором. "
            "После подтверждения надстройка активируется автоматически.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои лимиты", callback_data="subscription_limits")],
                [InlineKeyboardButton(text="🔙 К подписке", callback_data="subscription_menu")]
            ])
        )
    else:
        await fsm_edit(
            state, message,
            "❌ Ошибка при создании заявки. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Надстройки", callback_data="subscription_addons")]
            ])
        )

    await clear_state_keep_org(state)
