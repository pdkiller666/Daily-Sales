"""
Обработчики для управления уведомлениями
"""
import os
import asyncio
import logging
import sqlite3
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db_utils import clear_state_keep_org, is_any_admin, maybe_refresh_username, wrap_db
from database import Database
from keyboards import back_button, home_button, generate_calendar, safe_cb, resolve_cb_name
from pagination_utils import page_nav_row
from states import NotificationStates
from message_utils import fsm_edit
from env_manager import env_manager
from utils import he
from reports_access_control import get_subscription_offer_message
from notif_utils import add_read_btn

# Создаем роутер
notifications_router = Router()

from db_utils import get_db
from tenant_manager import tenant_manager

@notifications_router.callback_query(F.data == "notifications_menu")
async def notifications_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню управления уведомлениями"""
    # Получаем корректную БД для пользователя/организации
    current_db = await get_db(callback.from_user.id, state)

    is_super = env_manager.is_super_admin(callback.from_user.id)
    user = await current_db.get_user(callback.from_user.id)
    
    # Если супер-админ, создаем запись в БД если её нет
    if is_super and not user:
        await current_db.add_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "Admin",
            last_name=callback.from_user.last_name or "",
            shop_name=None,
            trade_network=None,
            city=None,
            phone="000",
            username=callback.from_user.username
        )
        user = await current_db.get_user(callback.from_user.id)

    if not user:
        await callback.answer("❌ Сначала завершите регистрацию через /start", show_alert=True)
        return

    maybe_refresh_username(
        current_db, callback.from_user.id, callback.from_user.username,
        stored_username=user[12] if len(user) > 12 else None,
    )
    await callback.answer()
    user_id = user[0]
    # Вычисляем роль один раз — используется и в проверке подписки, и в построении меню
    is_admin = is_any_admin(callback.from_user.id)
    from subscription_utils import check_notifications_permission

    if not is_super and not check_notifications_permission(callback.from_user.id):
        message = get_subscription_offer_message("Система уведомлений", is_admin)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Оформить подписку", callback_data="subscription_menu")],
            [InlineKeyboardButton(text="📋 Посмотреть тарифы", callback_data="subscription_plans")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(message, reply_markup=keyboard, parse_mode="HTML")
        return

    settings = await current_db.get_notification_settings(user_id)
    history = await current_db.get_notification_history(user_id)
    # item[4] = is_read (item[3] = message — это был баг, всегда давал 0 непрочитанных)
    unread_count = sum(1 for item in history if not item[4])
    
    text = "📥 <b>Управление уведомлениями</b>\n\n"
    text += "📱 <b>Текущие настройки:</b>\n"
    text += f"• Низкие остатки: {'✅' if settings['low_stock_alerts'] else '❌'}\n"
    text += f"• Ежедневные отчеты: {'✅' if settings['daily_reports'] else '❌'}\n"
    text += f"• Уведомления о продажах: {'✅' if settings['sales_alerts'] else '❌'}\n"
    text += f"• Продажи коллег по смене: {'✅' if settings.get('shift_sale_alerts', True) else '❌'}\n"
    text += f"• Платежные уведомления: {'✅' if settings['payment_alerts'] else '❌'}\n"
    text += f"• Админ уведомления: {'✅' if settings['admin_notifications'] else '❌'}\n"
    text += f"• Порог остатков: {settings['stock_threshold']} шт.\n"
    text += f"• Время уведомлений: {settings['notification_time']}\n\n"
    
    if unread_count > 0:
        text += f"📬 У вас {unread_count} непрочитанных уведомлений\n\n"

    keyboard_buttons = [
        [InlineKeyboardButton(text="⚙️ Настройки уведомлений", callback_data="notification_settings")],
        [InlineKeyboardButton(text="📋 История уведомлений", callback_data="notification_history")],
    ]
    
    if is_admin:
        keyboard_buttons.append([InlineKeyboardButton(text="📅 Запланированные уведомления", callback_data="view_scheduled_notifications")])
    
    keyboard_buttons.append([back_button("user_profile")])
    keyboard_buttons.append([home_button()])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@notifications_router.callback_query(F.data == "admin_send_notification")
async def admin_send_notification_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса отправки уведомления администратором"""
    is_admin = is_any_admin(callback.from_user.id)
    if not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
        
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "📨 <b>Отправка уведомления</b>\n\nВведите текст сообщения, которое вы хотите отправить всем пользователям:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]),
        parse_mode="HTML"
    )
    await state.set_state(NotificationStates.waiting_for_admin_message)

@notifications_router.message(NotificationStates.waiting_for_admin_message)
async def process_admin_notification_text(message: Message, state: FSMContext):
    """Обработка текста уведомления — переход к выбору получателей."""
    notification_text = message.text.strip() if message.text else ""
    if not notification_text:
        await fsm_edit(state, message,
                       "❌ Текст уведомления не может быть пустым. Введите текст сообщения:",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]))
        return
    await state.update_data(admin_notification_text=notification_text)
    is_super = env_manager.is_super_admin(message.from_user.id)
    await fsm_edit(state, message,
                   "👥 <b>Выберите получателей</b>\n\nКому отправить уведомление?",
                   reply_markup=_build_rcpt_selection_kb(is_super))


# ── Helpers: recipient selection ──────────────────────────────────────────────

def _build_rcpt_selection_kb(is_super: bool) -> InlineKeyboardMarkup:
    """Клавиатура выбора получателей уведомления."""
    rows = [[InlineKeyboardButton(text="👥 Всем пользователям", callback_data="ntf_rcpt_all")]]
    if not is_super:
        rows.append([InlineKeyboardButton(text="🏪 По магазину",         callback_data="ntf_rcpt_shop")])
        rows.append([InlineKeyboardButton(text="🎭 По роли",             callback_data="ntf_rcpt_role")])
        rows.append([InlineKeyboardButton(text="🏙 По городу",           callback_data="ntf_rcpt_city")])
        rows.append([InlineKeyboardButton(text="🌐 По торговой сети",    callback_data="ntf_rcpt_network")])
        rows.append([InlineKeyboardButton(text="👤 Выбрать конкретных",  callback_data="ntf_rcpt_pick")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_management")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _count_notif_recipients(db, rcpt_type: str, rcpt_filter=None) -> int:
    """Подсчёт получателей уведомления с учётом фильтра (admin_notifications=1)."""
    try:
        if rcpt_type == 'users':
            return len(rcpt_filter) if isinstance(rcpt_filter, list) else 0
        recipients = await db.get_users_for_notifications('admin')
        opted = [r for r in recipients if r[1]]
        if rcpt_type == 'shop':
            return len([r for r in opted if r[3] == rcpt_filter])
        if rcpt_type == 'role':
            from db_utils import get_user_org_role as _gor
            return len([r for r in opted if _gor(r[1]) == rcpt_filter])
        if rcpt_type == 'city':
            city_tids = {u[1] for u in await db.get_all_users(city=rcpt_filter) if u[1]}
            return len([r for r in opted if r[1] in city_tids])
        if rcpt_type == 'network':
            net_tids = {u[1] for u in await db.get_all_users(trade_network=rcpt_filter) if u[1]}
            return len([r for r in opted if r[1] in net_tids])
        return len(opted)
    except Exception:
        return 0


async def _show_notif_send_preview(callback: CallbackQuery, state: FSMContext, current_db=None):
    """Показывает превью уведомления с числом получателей перед отправкой."""
    data = await state.get_data()
    text = data.get('admin_notification_text', '')
    rcpt_label = data.get('ntf_rcpt_label', 'Всем пользователям')
    count_str = ''
    if current_db is not None:
        try:
            cnt = await _count_notif_recipients(
                current_db,
                data.get('ntf_rcpt_type', 'all'),
                data.get('ntf_rcpt_filter')
            )
            count_str = f' — {cnt} польз.'
        except Exception:
            pass
    preview = (
        f"📨 <b>Предпросмотр уведомления</b>\n\n"
        f"👥 <b>Получатели:</b> {he(rcpt_label)}{count_str}\n\n"
        f"<b>Текст:</b>\n{he(text)}"
    )
    await callback.message.edit_text(
        preview,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить сейчас", callback_data="admin_confirm_send_now")],
            [InlineKeyboardButton(text="📅 Запланировать",    callback_data="admin_schedule_notification")],
            [InlineKeyboardButton(text="↩️ Изменить получателей", callback_data="ntf_change_rcpt")],
            [InlineKeyboardButton(text="❌ Отмена",            callback_data="admin_management")],
        ]),
        parse_mode="HTML"
    )


_ROLE_LABELS = {'owner': 'Директора', 'admin': 'Администраторы', 'user': 'Сотрудники'}


# ── Recipient selection callbacks ─────────────────────────────────────────────

@notifications_router.callback_query(F.data == "ntf_rcpt_all")
async def ntf_rcpt_all(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(ntf_rcpt_type='all', ntf_rcpt_filter=None, ntf_rcpt_label='Всем пользователям')
    current_db = await get_db(callback.from_user.id, state)
    await _show_notif_send_preview(callback, state, current_db)


@notifications_router.callback_query(F.data == "ntf_rcpt_shop")
async def ntf_rcpt_shop(callback: CallbackQuery, state: FSMContext):
    """Показывает список магазинов для выбора получателей."""
    current_db = await get_db(callback.from_user.id, state)
    shops = await current_db.get_all_shops()
    if not shops:
        await callback.answer("❌ Магазины не найдены.", show_alert=True)
        return
    await callback.answer()
    rows = []
    for shop in shops:
        sname = shop[0] if isinstance(shop, (list, tuple)) else str(shop)
        rows.append([InlineKeyboardButton(text=f"🏪 {sname}", callback_data=safe_cb(sname, prefix='ntf_rcpt_s_'))])
    rows.append([back_button("ntf_change_rcpt")])
    await callback.message.edit_text(
        "🏪 <b>Выберите магазин</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.startswith("ntf_rcpt_s_"))
async def ntf_rcpt_shop_selected(callback: CallbackQuery, state: FSMContext):
    """Магазин выбран — сохраняем фильтр и показываем превью."""
    await callback.answer()
    shop_name = resolve_cb_name(callback.data.removeprefix("ntf_rcpt_s_"))
    await state.update_data(
        ntf_rcpt_type='shop',
        ntf_rcpt_filter=shop_name,
        ntf_rcpt_label=f'Магазин «{shop_name}»'
    )
    current_db = await get_db(callback.from_user.id, state)
    await _show_notif_send_preview(callback, state, current_db)


@notifications_router.callback_query(F.data == "ntf_rcpt_role")
async def ntf_rcpt_role(callback: CallbackQuery, state: FSMContext):
    """Показывает выбор роли получателей."""
    await callback.answer()
    await callback.message.edit_text(
        "🎭 <b>Выберите роль получателей</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Директора",        callback_data="ntf_rcpt_r_owner")],
            [InlineKeyboardButton(text="🛡️ Администраторы",  callback_data="ntf_rcpt_r_admin")],
            [InlineKeyboardButton(text="👤 Сотрудники",       callback_data="ntf_rcpt_r_user")],
            [back_button("ntf_change_rcpt")],
        ]),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.in_({"ntf_rcpt_r_owner", "ntf_rcpt_r_admin", "ntf_rcpt_r_user"}))
async def ntf_rcpt_role_selected(callback: CallbackQuery, state: FSMContext):
    """Роль выбрана — сохраняем фильтр и показываем превью."""
    await callback.answer()
    role = callback.data.removeprefix("ntf_rcpt_r_")
    label = _ROLE_LABELS.get(role, role)
    await state.update_data(ntf_rcpt_type='role', ntf_rcpt_filter=role, ntf_rcpt_label=label)
    current_db = await get_db(callback.from_user.id, state)
    await _show_notif_send_preview(callback, state, current_db)


# ── По городу ─────────────────────────────────────────────────────────────────

@notifications_router.callback_query(F.data == "ntf_rcpt_city")
async def ntf_rcpt_city(callback: CallbackQuery, state: FSMContext):
    """Показывает список городов для выбора получателей."""
    current_db = await get_db(callback.from_user.id, state)
    cities = await current_db.get_all_cities()
    if not cities:
        await callback.answer("❌ Города не найдены.", show_alert=True)
        return
    await callback.answer()
    rows = [[InlineKeyboardButton(text=f"🏙 {c}", callback_data=safe_cb(c, prefix='ntf_rcpt_ci_'))]
            for c in sorted(cities)]
    rows.append([back_button("ntf_change_rcpt")])
    await callback.message.edit_text(
        "🏙 <b>Выберите город</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.startswith("ntf_rcpt_ci_"))
async def ntf_rcpt_city_selected(callback: CallbackQuery, state: FSMContext):
    """Город выбран — сохраняем фильтр и показываем превью."""
    await callback.answer()
    city = resolve_cb_name(callback.data.removeprefix("ntf_rcpt_ci_"))
    await state.update_data(ntf_rcpt_type='city', ntf_rcpt_filter=city,
                            ntf_rcpt_label=f'Город «{city}»')
    current_db = await get_db(callback.from_user.id, state)
    await _show_notif_send_preview(callback, state, current_db)


# ── По торговой сети ──────────────────────────────────────────────────────────

@notifications_router.callback_query(F.data == "ntf_rcpt_network")
async def ntf_rcpt_network(callback: CallbackQuery, state: FSMContext):
    """Показывает список торговых сетей для выбора получателей."""
    current_db = await get_db(callback.from_user.id, state)
    networks = await current_db.get_all_trade_networks()
    if not networks:
        await callback.answer("❌ Торговые сети не найдены.", show_alert=True)
        return
    await callback.answer()
    rows = [[InlineKeyboardButton(text=f"🌐 {nw}", callback_data=safe_cb(nw, prefix='ntf_rcpt_nw_'))]
            for nw in sorted(networks)]
    rows.append([back_button("ntf_change_rcpt")])
    await callback.message.edit_text(
        "🌐 <b>Выберите торговую сеть</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.startswith("ntf_rcpt_nw_"))
async def ntf_rcpt_network_selected(callback: CallbackQuery, state: FSMContext):
    """Торговая сеть выбрана — сохраняем фильтр и показываем превью."""
    await callback.answer()
    network = resolve_cb_name(callback.data.removeprefix("ntf_rcpt_nw_"))
    await state.update_data(ntf_rcpt_type='network', ntf_rcpt_filter=network,
                            ntf_rcpt_label=f'Сеть «{network}»')
    current_db = await get_db(callback.from_user.id, state)
    await _show_notif_send_preview(callback, state, current_db)


# ── Выбрать конкретных пользователей (мультивыбор) ───────────────────────────

_NTF_PICK_PAGE_SIZE = 8


async def _show_user_picker(callback: CallbackQuery, state: FSMContext, page: int = 0):
    """Рисует экран выбора конкретных получателей с чекбоксами и пагинацией."""
    current_db = await get_db(callback.from_user.id, state)
    all_users_raw = await current_db.get_all_users()
    # Фильтруем: только с telegram_id, без системных
    SKIP = {'', 'System', 'Системный'}
    users = [u for u in all_users_raw
             if u[1] and str(u[8] or '') not in SKIP and str(u[2] or '') not in SKIP]

    data = await state.get_data()
    selected: list = data.get('ntf_selected_tids', [])
    sel_set = set(selected)

    total = len(users)
    total_pages = max(1, (total + _NTF_PICK_PAGE_SIZE - 1) // _NTF_PICK_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_users = users[page * _NTF_PICK_PAGE_SIZE:(page + 1) * _NTF_PICK_PAGE_SIZE]

    rows = []
    for u in page_users:
        uid     = u[0]
        tid     = u[1]
        fname   = u[2] or ''
        lname   = u[3] or ''
        shop    = u[8] or ''
        name    = f"{fname} {lname}".strip() or f"id{uid}"
        shop_s  = f" · {shop}" if shop else ""
        mark    = "✅" if tid in sel_set else "⬜"
        btn_txt = f"{mark} {name}{shop_s}"
        rows.append([InlineKeyboardButton(text=btn_txt[:60],
                                          callback_data=f"ntf_usr_tog_{tid}")])

    # Кнопки управления выбором
    rows.append([
        InlineKeyboardButton(text="✅ Все",  callback_data=f"ntf_usr_all_{page}"),
        InlineKeyboardButton(text="⬜ Сбросить", callback_data=f"ntf_usr_none_{page}"),
    ])

    # Пагинация
    nav = page_nav_row("ntf_usr_pg_", page, page > 0, page < total_pages - 1, total_pages)
    if nav:
        rows.append(nav)

    n_sel = len(sel_set)
    done_txt = f"✅ Готово ({n_sel} чел.)" if n_sel else "✅ Готово"
    rows.append([InlineKeyboardButton(text=done_txt, callback_data="ntf_usr_done")])
    rows.append([back_button("ntf_change_rcpt")])

    header = (
        f"👤 <b>Выберите получателей</b>\n"
        f"Всего: {total} · Выбрано: {n_sel}"
        + (f" · стр. {page+1}/{total_pages}" if total_pages > 1 else "")
    )
    await callback.message.edit_text(
        header,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )
    await state.update_data(ntf_pick_page=page)


@notifications_router.callback_query(F.data == "ntf_rcpt_pick")
async def ntf_rcpt_pick(callback: CallbackQuery, state: FSMContext):
    """Открывает экран мультивыбора пользователей."""
    await callback.answer()
    data = await state.get_data()
    if 'ntf_selected_tids' not in data:
        await state.update_data(ntf_selected_tids=[])
    await _show_user_picker(callback, state, page=0)


@notifications_router.callback_query(F.data.startswith("ntf_usr_tog_"))
async def ntf_usr_toggle(callback: CallbackQuery, state: FSMContext):
    """Переключает выбор конкретного пользователя."""
    await callback.answer()
    try:
        tid = int(callback.data.removeprefix("ntf_usr_tog_"))
    except ValueError:
        return
    data = await state.get_data()
    selected: list = data.get('ntf_selected_tids', [])
    if tid in selected:
        selected.remove(tid)
    else:
        selected.append(tid)
    page = data.get('ntf_pick_page', 0)
    await state.update_data(ntf_selected_tids=selected)
    await _show_user_picker(callback, state, page=page)


@notifications_router.callback_query(F.data.startswith("ntf_usr_pg_"))
async def ntf_usr_page(callback: CallbackQuery, state: FSMContext):
    """Пагинация в экране выбора пользователей."""
    await callback.answer()
    try:
        page = int(callback.data.removeprefix("ntf_usr_pg_"))
    except ValueError:
        page = 0
    await _show_user_picker(callback, state, page=page)


@notifications_router.callback_query(F.data.startswith("ntf_usr_all_"))
async def ntf_usr_select_all(callback: CallbackQuery, state: FSMContext):
    """Выбрать всех пользователей на текущей странице."""
    await callback.answer()
    try:
        page = int(callback.data.removeprefix("ntf_usr_all_"))
    except ValueError:
        page = 0
    current_db = await get_db(callback.from_user.id, state)
    all_users_raw = await current_db.get_all_users()
    SKIP = {'', 'System', 'Системный'}
    users = [u for u in all_users_raw
             if u[1] and str(u[8] or '') not in SKIP and str(u[2] or '') not in SKIP]
    page_tids = [u[1] for u in users[page * _NTF_PICK_PAGE_SIZE:(page + 1) * _NTF_PICK_PAGE_SIZE]]

    data = await state.get_data()
    selected: list = data.get('ntf_selected_tids', [])
    sel_set = set(selected)
    for tid in page_tids:
        sel_set.add(tid)
    await state.update_data(ntf_selected_tids=list(sel_set))
    await _show_user_picker(callback, state, page=page)


@notifications_router.callback_query(F.data.startswith("ntf_usr_none_"))
async def ntf_usr_deselect_all(callback: CallbackQuery, state: FSMContext):
    """Снять выбор у всех пользователей на текущей странице."""
    await callback.answer()
    try:
        page = int(callback.data.removeprefix("ntf_usr_none_"))
    except ValueError:
        page = 0
    current_db = await get_db(callback.from_user.id, state)
    all_users_raw = await current_db.get_all_users()
    SKIP = {'', 'System', 'Системный'}
    users = [u for u in all_users_raw
             if u[1] and str(u[8] or '') not in SKIP and str(u[2] or '') not in SKIP]
    page_tids = set(u[1] for u in users[page * _NTF_PICK_PAGE_SIZE:(page + 1) * _NTF_PICK_PAGE_SIZE])

    data = await state.get_data()
    selected: list = [t for t in data.get('ntf_selected_tids', []) if t not in page_tids]
    await state.update_data(ntf_selected_tids=selected)
    await _show_user_picker(callback, state, page=page)


@notifications_router.callback_query(F.data == "ntf_usr_done")
async def ntf_usr_done(callback: CallbackQuery, state: FSMContext):
    """Подтверждение выбора конкретных получателей — переходим к превью."""
    data = await state.get_data()
    selected: list = data.get('ntf_selected_tids', [])
    if not selected:
        await callback.answer("⚠️ Выберите хотя бы одного получателя!", show_alert=True)
        return
    await callback.answer()
    n = len(selected)
    label = f"{n} польз." if n != 1 else "1 польз."
    await state.update_data(
        ntf_rcpt_type='users',
        ntf_rcpt_filter=selected,
        ntf_rcpt_label=f'Конкретные получатели ({label})'
    )
    current_db = await get_db(callback.from_user.id, state)
    await _show_notif_send_preview(callback, state, current_db)


# ── Изменить получателей ──────────────────────────────────────────────────────

@notifications_router.callback_query(F.data == "ntf_change_rcpt")
async def ntf_change_rcpt(callback: CallbackQuery, state: FSMContext):
    """Возврат к экрану выбора получателей."""
    await callback.answer()
    is_super = env_manager.is_super_admin(callback.from_user.id)
    await callback.message.edit_text(
        "👥 <b>Выберите получателей</b>\n\nКому отправить уведомление?",
        reply_markup=_build_rcpt_selection_kb(is_super),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data == "admin_confirm_send_now")
async def admin_confirm_send_now(callback: CallbackQuery, state: FSMContext):
    """Подтверждение немедленной отправки уведомления"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    data = await state.get_data()
    text = data.get('admin_notification_text')

    if not text:
        await callback.answer("❌ Ошибка: текст сообщения не найден", show_alert=True)
        return

    rcpt_type   = data.get('ntf_rcpt_type', 'all')
    rcpt_filter = data.get('ntf_rcpt_filter')
    rcpt_label  = data.get('ntf_rcpt_label', 'всем пользователям')

    await callback.message.edit_text(f"⏳ Отправка уведомлений ({rcpt_label})...")
    
    # Определяем, по какой БД рассылать
    count = 0
    try:
        from bot_holder import get_bot as _get_bot
        bot = _get_bot()
        if not bot:
            raise RuntimeError("bot not initialized")
        is_super = env_manager.is_super_admin(callback.from_user.id)
        
        base_dir = os.getcwd()
        data_dir = os.path.join(base_dir, 'data')
        
        logging.info(f"BROADCAST DEBUG: Starting broadcast. Super-admin: {is_super}")

        # Только супер-admin рассылает по ВСЕМ организациям.
        # Обычный org-admin рассылает только по своей БД.
        if is_super:
            db_paths = []
            # 1. shop_bot.db
            shop_bot_path = os.path.join(data_dir, 'shop_bot.db')
            if os.path.exists(shop_bot_path):
                db_paths.append(shop_bot_path)

            # 2. Все .db файлы в data (кроме main.db)
            if os.path.exists(data_dir):
                for f in os.listdir(data_dir):
                    if f.endswith('.db') and f not in ['main.db', 'shop_bot.db']:
                        db_paths.append(os.path.join(data_dir, f))

            # 3. Папка tenants
            tenants_dir = os.path.join(data_dir, 'tenants')
            if os.path.exists(tenants_dir) and os.path.isdir(tenants_dir):
                for f in os.listdir(tenants_dir):
                    if f.endswith('.db'):
                        db_paths.append(os.path.join(tenants_dir, f))

            # 4. Маппинг через tenant_manager
            try:
                for org in tenant_manager.get_all_organizations():
                    org_db = org[2]
                    if org_db:
                        path = org_db if os.path.isabs(org_db) else os.path.join(base_dir, org_db)
                        if os.path.exists(path):
                            db_paths.append(path)
            except Exception as e:
                logging.error(f"BROADCAST ERROR: Failed to read orgs: {e}")

            # Нормализация и дедупликация путей
            db_paths = list(set(os.path.normpath(p) for p in db_paths))
        else:
            # Org-admin: только своя БД (тенант или shop_bot.db)
            org_db = await get_db(callback.from_user.id, state)
            db_paths = [os.path.normpath(org_db.db_file)]
        logging.info(f"BROADCAST DEBUG: DB paths for scan: {db_paths}")

        # Собираем пользователей с учётом фильтра
        # get_users_for_notifications: (user_id[0], telegram_id[1], first_name[2], shop_name[3], ...)
        # get_all_users SELECT *: id[0], telegram_id[1], first_name[2], last_name[3], ..., city[9], ...
        target_by_db: dict = {}
        for path in db_paths:
            try:
                if not os.path.exists(path):
                    continue
                path_db = wrap_db(Database(path))

                if rcpt_type == 'users' and rcpt_filter:
                    # Конкретные получатели — без проверки admin_notifications
                    target_tids = set(int(t) for t in rcpt_filter)
                    all_u = await path_db.get_all_users()
                    filtered = [(u[0], u[1]) for u in all_u if u[1] and int(u[1]) in target_tids]
                else:
                    recipients = await path_db.get_users_for_notifications('admin')
                    opted = [r for r in recipients if r[1]]
                    if rcpt_type == 'shop' and rcpt_filter:
                        filtered = [(r[0], r[1]) for r in opted if r[3] == rcpt_filter]
                    elif rcpt_type == 'role' and rcpt_filter:
                        from db_utils import get_user_org_role as _gor
                        filtered = [(r[0], r[1]) for r in opted if _gor(r[1]) == rcpt_filter]
                    elif rcpt_type == 'city' and rcpt_filter:
                        city_tids = {u[1] for u in await path_db.get_all_users(city=rcpt_filter) if u[1]}
                        filtered = [(r[0], r[1]) for r in opted if r[1] in city_tids]
                    elif rcpt_type == 'network' and rcpt_filter:
                        net_tids = {u[1] for u in await path_db.get_all_users(trade_network=rcpt_filter) if u[1]}
                        filtered = [(r[0], r[1]) for r in opted if r[1] in net_tids]
                    else:
                        filtered = [(r[0], r[1]) for r in opted]

                if filtered:
                    target_by_db[path] = (path_db, filtered)
            except Exception as e:
                logging.error(f"BROADCAST ERROR: Database {path} error: {e}")

        total_opted_in = sum(len(v[1]) for v in target_by_db.values())
        logging.info(f"BROADCAST DEBUG: Total opted-in users: {total_opted_in}")

        # Отправка с дедупликацией по telegram_id
        seen_tids: set = set()
        for path, (path_db, recipients) in target_by_db.items():
            for uid_internal, tid in recipients:
                try:
                    tid_int = int(tid)
                except (ValueError, TypeError):
                    continue
                if tid_int in seen_tids:
                    continue
                seen_tids.add(tid_int)
                try:
                    await bot.send_message(tid_int, f"🔔 <b>Уведомление от администратора</b>\n\n{he(text)}", parse_mode="HTML", reply_markup=add_read_btn())
                    count += 1
                    await path_db.add_notification_to_history(uid_internal, 'admin', text)
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logging.error(f"BROADCAST ERROR: Failed to send to {tid_int}: {e}")
                
    except Exception as e:
        logging.error(f"BROADCAST CRITICAL ERROR: {e}")
    
    final_text = f"✅ Уведомление успешно отправлено {count} пользователям!"
    await callback.message.edit_text(
        final_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]])
    )
    await clear_state_keep_org(state)

@notifications_router.callback_query(F.data == "admin_schedule_notification")
async def admin_schedule_notification_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса планирования уведомления"""
    await callback.answer()
    await callback.message.edit_text(
        "📅 <b>Планирование уведомления</b>\n\nВведите время отправки в формате ДД.ММ.ГГГГ ЧЧ:ММ (напр. 02.02.2026 09:00):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]),
        parse_mode="HTML"
    )
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(NotificationStates.waiting_for_schedule_time)

@notifications_router.message(NotificationStates.waiting_for_schedule_time)
async def process_schedule_time(message: Message, state: FSMContext):
    """Обработка времени планирования"""
    import datetime
    import uuid
    from timezone_utils import get_utc_time, get_current_user_time
    time_text = message.text.strip()
    try:
        schedule_time_naive = datetime.datetime.strptime(time_text, "%d.%m.%Y %H:%M")

        data = await state.get_data()
        text = data.get('admin_notification_text')
        if not text:
            await fsm_edit(state, message, "❌ Текст уведомления не найден. Начните заново.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]))
            await clear_state_keep_org(state)
            return

        current_db = await get_db(message.from_user.id, state)
        admin_user = await current_db.get_user(message.from_user.id)
        if not admin_user:
            await fsm_edit(state, message, "❌ Профиль администратора не найден.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]))
            await clear_state_keep_org(state)
            return

        # Интерпретируем введённое время в timezone администратора → конвертируем в UTC
        admin_tz = await current_db.get_user_timezone(message.from_user.id)
        schedule_time_utc = get_utc_time(schedule_time_naive, admin_tz)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if schedule_time_utc <= now_utc:
            await fsm_edit(state, message, "❌ Время должно быть в будущем! Введите заново:")
            return

        _MAX_SCHEDULED = 20
        _all_pending = await current_db.get_scheduled_notifications(status='pending')
        _my_pending = [n for n in _all_pending if n[2] == admin_user[0]]
        if len(_my_pending) >= _MAX_SCHEDULED:
            await fsm_edit(state, message,
                f"❌ Достигнут лимит запланированных уведомлений ({_MAX_SCHEDULED}). "
                f"Удалите некоторые из существующих перед созданием нового.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]))
            await clear_state_keep_org(state)
            return

        import json as _njson
        _rcpt_type   = data.get('ntf_rcpt_type', 'all')
        _rcpt_filter = data.get('ntf_rcpt_filter')
        _rcpt_label  = data.get('ntf_rcpt_label', 'всем пользователям')
        _rcpt_info   = {'type': _rcpt_type}
        if _rcpt_filter:
            _rcpt_info['filter'] = _rcpt_filter
        recipients_list_json = _njson.dumps(_rcpt_info, ensure_ascii=False)

        is_super = env_manager.is_super_admin(message.from_user.id)
        recipients_type = 'all' if is_super else 'org'
        job_id = str(uuid.uuid4())
        await current_db.add_scheduled_notification(
            job_id=job_id,
            created_by=admin_user[0],
            notification_text=text,
            recipients_type=recipients_type,
            recipients_list=recipients_list_json,
            scheduled_datetime=schedule_time_utc.strftime('%Y-%m-%dT%H:%M:%S')
        )
        await fsm_edit(state, message,
            f"✅ <b>Уведомление запланировано!</b>\n\n"
            f"📅 Время: {time_text} ({admin_tz})\n"
            f"👥 Получатели: {_rcpt_label}\n"
            f"💬 Текст: {he(text)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]))
        await clear_state_keep_org(state)
    except ValueError:
        await fsm_edit(state, message, "❌ Неверный формат. Используйте ДД.ММ.ГГГГ ЧЧ:ММ (напр. 02.02.2026 09:00)")

@notifications_router.callback_query(F.data == "view_scheduled_notifications")
async def view_scheduled_notifications(callback: CallbackQuery, state: FSMContext):
    """Просмотр запланированных уведомлений"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    await callback.answer()
    current_db = await get_db(callback.from_user.id, state)
    notifications = await current_db.get_scheduled_notifications(status='pending')

    if not notifications:
        await callback.message.edit_text(
            "📅 <b>Запланированные уведомления</b>\n\nНет запланированных уведомлений.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notifications_menu")]]),
            parse_mode="HTML"
        )
        return

    from timezone_utils import format_user_datetime
    admin_tz = await current_db.get_user_timezone(callback.from_user.id)

    total = len(notifications)
    shown = notifications[:10]
    text = f"📅 <b>Запланированные уведомления</b> ({total})\n\n"
    buttons = []
    for notif in shown:
        notif_id = notif[0]
        notif_text = notif[3] or ""
        scheduled_dt_raw = notif[6] or ""
        scheduled_dt = format_user_datetime(scheduled_dt_raw, admin_tz, '%d.%m.%Y %H:%M') if scheduled_dt_raw else "—"
        creator_name = he(f"{notif[-2] or ''} {notif[-1] or ''}".strip() or "Администратор")
        preview = he(notif_text[:50] + ("…" if len(notif_text) > 50 else ""))
        text += f"🕐 {scheduled_dt}\n👤 {creator_name}\n💬 {preview}\n\n"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Удалить #{notif_id}",
            callback_data=f"del_sched_notif_{notif_id}"
        )])
    if total > 10:
        text += f"<i>…и ещё {total - 10} уведомлений. Удалите часть для просмотра остальных.</i>\n"

    buttons.append([back_button("notifications_menu")])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.startswith("del_sched_notif_"))
async def delete_scheduled_notification(callback: CallbackQuery, state: FSMContext):
    """Удаление запланированного уведомления"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    try:
        notif_id = int(callback.data.replace("del_sched_notif_", ""))
    except ValueError:
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return
    current_db = await get_db(callback.from_user.id, state)
    deleted = await current_db.delete_scheduled_notification(notif_id)
    if deleted:
        await callback.answer("✅ Уведомление удалено")
    else:
        await callback.answer("⚠️ Уведомление не найдено")
    await view_scheduled_notifications(callback, state)


@notifications_router.callback_query(F.data == "notification_settings")
async def notification_settings_menu(callback: CallbackQuery, state: FSMContext):
    """Меню настроек уведомлений"""
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    user_id = user[0]
    settings = await current_db.get_notification_settings(user_id)
    is_admin = is_any_admin(callback.from_user.id)
    
    text = "⚙️ <b>Настройки уведомлений</b>\n\n"
    text += "Выберите тип уведомлений для настройки:\n\n"
    
    keyboard_buttons = [
        [InlineKeyboardButton(text=f"📦 Низкие остатки {'✅' if settings['low_stock_alerts'] else '❌'}", callback_data="toggle_low_stock")],
        [InlineKeyboardButton(text=f"📊 Ежедневные отчеты {'✅' if settings['daily_reports'] else '❌'}", callback_data="toggle_daily_reports")],
        [InlineKeyboardButton(text=f"💰 Уведомления о продажах {'✅' if settings['sales_alerts'] else '❌'}", callback_data="toggle_sales_alerts")],
        [InlineKeyboardButton(text=f"👥 Продажи коллег по смене {'✅' if settings.get('shift_sale_alerts', True) else '❌'}", callback_data="toggle_shift_sale")],
    ]
    
    if is_admin:
        keyboard_buttons.extend([
            [InlineKeyboardButton(text=f"💳 Платежные уведомления {'✅' if settings['payment_alerts'] else '❌'}", callback_data="toggle_payment_alerts")],
            [InlineKeyboardButton(text=f"🔧 Админ уведомления {'✅' if settings['admin_notifications'] else '❌'}", callback_data="toggle_admin_notifications")],
        ])
    
    keyboard_buttons.extend([
        [InlineKeyboardButton(text=f"📏 Порог остатков ({settings['stock_threshold']} шт.)", callback_data="set_stock_threshold")],
        [InlineKeyboardButton(text=f"⏰ Время уведомлений ({settings['notification_time']})", callback_data="set_notification_time")],
    ])

    keyboard_buttons.append([back_button("notifications_menu")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@notifications_router.callback_query(F.data == "set_notification_time")
async def set_notification_time_start(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    await callback.answer()
    await state.update_data(user_id=user[0])
    text = "⏰ <b>Установка времени уведомлений</b>\n\nВведите время в формате ЧЧ:ММ (напр. 09:00):"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]]), parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(NotificationStates.waiting_for_time)

@notifications_router.callback_query(F.data == "noop_salary_header")
async def noop_salary_header(callback: CallbackQuery):
    await callback.answer()


@notifications_router.callback_query(F.data.in_(["toggle_low_stock", "toggle_daily_reports", "toggle_sales_alerts", "toggle_payment_alerts", "toggle_admin_notifications", "toggle_shift_sale", "toggle_plan_coeff", "toggle_plan_coeff_cap"]))
async def toggle_notification_setting(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    user_id = user[0]
    setting_mapping = {
        'toggle_low_stock': 'low_stock_alerts',
        'toggle_daily_reports': 'daily_reports',
        'toggle_sales_alerts': 'sales_alerts',
        'toggle_payment_alerts': 'payment_alerts',
        'toggle_admin_notifications': 'admin_notifications',
        'toggle_shift_sale': 'shift_sale_alerts',
        'toggle_plan_coeff': 'plan_coeff_enabled',
        'toggle_plan_coeff_cap': 'plan_coeff_cap',
    }
    
    setting_type = setting_mapping.get(callback.data)
    if not setting_type:
        await callback.answer("❌ Неизвестная настройка", show_alert=True)
        return
    
    if setting_type in ['payment_alerts', 'admin_notifications']:
        is_super = env_manager.is_super_admin(callback.from_user.id)
        if not (is_super or is_any_admin(callback.from_user.id)):
            await callback.answer("❌ Только для администраторов!", show_alert=True)
            return
    
    settings = await current_db.get_notification_settings(user_id)
    new_value = not settings.get(setting_type, False)
    await current_db.update_notification_settings(user_id, **{setting_type: new_value})
    
    await callback.answer(f"✅ Настройка изменена")
    await notification_settings_menu(callback, state)

@notifications_router.callback_query(F.data == "set_stock_threshold")
async def set_stock_threshold_start(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    await callback.answer()
    await state.update_data(user_id=user[0])
    text = "📏 <b>Установка порога остатков</b>\n\nВведите число (напр. 5):"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]]), parse_mode="HTML")
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(NotificationStates.waiting_for_threshold)

@notifications_router.message(NotificationStates.waiting_for_threshold)
async def process_stock_threshold(message: Message, state: FSMContext):
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]])
    try:
        threshold = int(message.text.strip())
        if threshold < 1:
            await fsm_edit(state, message, "❌ Должно быть больше 0", reply_markup=_back_kb)
            return
        data = await state.get_data()
        current_db = await get_db(message.from_user.id, state)
        await current_db.update_notification_settings(data['user_id'], stock_threshold=threshold)
        await fsm_edit(state, message, f"✅ Порог установлен: {threshold} шт.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", callback_data="notification_settings")]]))
        await clear_state_keep_org(state)
    except ValueError:
        await fsm_edit(state, message, "❌ Введите корректное число", reply_markup=_back_kb)

@notifications_router.message(NotificationStates.waiting_for_time)
async def process_notification_time(message: Message, state: FSMContext):
    import re
    time_text = message.text.strip()
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("notification_settings")]])
    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', time_text):
        await fsm_edit(state, message, "❌ Неверный формат. Используйте ЧЧ:ММ (напр. 09:00)", reply_markup=_back_kb)
        return
    data = await state.get_data()
    try:
        current_db = await get_db(message.from_user.id, state)
        await current_db.update_notification_settings(data['user_id'], notification_time=time_text)
        await fsm_edit(state, message, f"✅ Время установлено: {time_text}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", callback_data="notification_settings")]]))
    except Exception as e:
        logging.error(f"process_notification_time: DB error: {e}")
        await fsm_edit(state, message, "❌ Ошибка при сохранении времени. Попробуйте позже.", reply_markup=_back_kb)
    await clear_state_keep_org(state)

NOTIF_HIST_PAGE_SIZE = 10


def _fmt_date_display(d: str) -> str:
    """'YYYY-MM-DD' → 'DD.MM.YYYY'"""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(d, '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        return d


@notifications_router.callback_query(F.data == "notification_history")
async def notification_history_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(nh_period='week', nh_offset=0, nh_page=0,
                             nh_start=None, nh_end=None)
    await show_notification_history(callback, state)


@notifications_router.callback_query(F.data.startswith("nhper_"))
async def notif_hist_period_handler(callback: CallbackQuery, state: FSMContext):
    """Switch period: nhper_week / nhper_month / nhper_all / nhper_custom."""
    period = callback.data[len("nhper_"):]
    if period == 'custom':
        await callback.answer()
        from datetime import date as _date
        today = _date.today()
        await state.update_data(nh_cal_picking='start')
        await callback.message.edit_text(
            "📅 <b>Выберите начальную дату:</b>",
            reply_markup=generate_calendar(today.year, today.month,
                                           cancel_callback="notification_history",
                                           prefix="nfcal_"),
            parse_mode="HTML"
        )
        await state.set_state(NotificationStates.nfcal_choosing_start)
        return
    await callback.answer()
    await state.update_data(nh_period=period, nh_offset=0, nh_page=0)
    await show_notification_history(callback, state)


@notifications_router.callback_query(F.data.startswith("nhoff_"))
async def notif_hist_offset_handler(callback: CallbackQuery, state: FSMContext):
    """Offset navigation within week/month periods (◀️ Старше / Новее ▶️)."""
    await callback.answer()
    offset = int(callback.data[len("nhoff_"):])
    await state.update_data(nh_offset=offset, nh_page=0)
    await show_notification_history(callback, state)


@notifications_router.callback_query(F.data.startswith("nhpg_"))
async def notif_hist_page_handler(callback: CallbackQuery, state: FSMContext):
    """Page navigation within current period."""
    await callback.answer()
    page = int(callback.data[len("nhpg_"):])
    await state.update_data(nh_page=page)
    await show_notification_history(callback, state)


# ── Calendar handlers (prefix nfcal_) ─────────────────────────────────────────

@notifications_router.callback_query(F.data.startswith("nfcal_nav_"))
async def nfcal_nav_handler(callback: CallbackQuery, state: FSMContext):
    """Navigate calendar months."""
    await callback.answer()
    parts = callback.data[len("nfcal_nav_"):].split("_")
    year, month = int(parts[0]), int(parts[1])
    data = await state.get_data()
    picking = data.get('nh_cal_picking', 'start')
    start_sel = data.get('nh_start', '')
    if picking == 'end' and start_sel:
        prompt = f"📅 <b>Выберите конечную дату:</b>\n✅ Начало: {_fmt_date_display(start_sel)}"
    else:
        prompt = "📅 <b>Выберите начальную дату:</b>"
    await callback.message.edit_text(
        prompt,
        reply_markup=generate_calendar(year, month,
                                       cancel_callback="notification_history",
                                       prefix="nfcal_"),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.startswith("nfcal_date_"),
                                      NotificationStates.nfcal_choosing_start)
async def nfcal_start_date(callback: CallbackQuery, state: FSMContext):
    """Start date selected — show calendar for end date."""
    await callback.answer()
    start_date = callback.data[len("nfcal_date_"):]
    await state.update_data(nh_start=start_date, nh_cal_picking='end')
    year, month = int(start_date[:4]), int(start_date[5:7])
    await callback.message.edit_text(
        f"📅 <b>Выберите конечную дату:</b>\n✅ Начало: {_fmt_date_display(start_date)}",
        reply_markup=generate_calendar(year, month,
                                       cancel_callback="notification_history",
                                       prefix="nfcal_"),
        parse_mode="HTML"
    )
    await state.set_state(NotificationStates.nfcal_choosing_end)


@notifications_router.callback_query(F.data.startswith("nfcal_date_"),
                                      NotificationStates.nfcal_choosing_end)
async def nfcal_end_date(callback: CallbackQuery, state: FSMContext):
    """End date selected — show filtered history."""
    end_date = callback.data[len("nfcal_date_"):]
    data = await state.get_data()
    start_date = data.get('nh_start', '')
    if end_date < start_date:
        await callback.answer("❌ Конечная дата не может быть раньше начальной!",
                               show_alert=True)
        return
    await callback.answer()
    await state.update_data(nh_end=end_date, nh_period='custom', nh_page=0)
    await clear_state_keep_org(state)
    await show_notification_history(callback, state)


# ── Core history display ───────────────────────────────────────────────────────

async def show_notification_history(callback: CallbackQuery, state: FSMContext):
    from datetime import datetime, timedelta, timezone
    from timezone_utils import format_user_datetime

    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    user_id = user[0]
    user_timezone = await current_db.get_user_timezone(callback.from_user.id)
    data = await state.get_data()

    period     = data.get('nh_period', 'week')
    offset     = int(data.get('nh_offset', 0))
    page       = int(data.get('nh_page', 0))
    start_date = data.get('nh_start')
    end_date   = data.get('nh_end')

    now = datetime.now(timezone.utc)
    if period == 'week':
        end_dt   = now - timedelta(days=7 * offset)
        start_dt = end_dt - timedelta(days=7)
        start_str    = start_dt.strftime('%Y-%m-%d')
        end_str      = (end_dt - timedelta(seconds=1)).strftime('%Y-%m-%d')
        period_label = (f"{start_dt.strftime('%d.%m.%Y')} — "
                        f"{(end_dt - timedelta(seconds=1)).strftime('%d.%m.%Y')}")
    elif period == 'month':
        end_dt   = now - timedelta(days=30 * offset)
        start_dt = end_dt - timedelta(days=30)
        start_str    = start_dt.strftime('%Y-%m-%d')
        end_str      = (end_dt - timedelta(seconds=1)).strftime('%Y-%m-%d')
        period_label = (f"{start_dt.strftime('%d.%m.%Y')} — "
                        f"{(end_dt - timedelta(seconds=1)).strftime('%d.%m.%Y')}")
    elif period == 'custom' and start_date and end_date:
        start_str    = start_date
        end_str      = end_date
        period_label = f"{_fmt_date_display(start_date)} — {_fmt_date_display(end_date)}"
    else:  # 'all'
        start_str    = None
        end_str      = None
        period_label = "Все время"

    rows, total = await current_db.get_notification_history_paged(
        user_id, start_str, end_str,
        limit=NOTIF_HIST_PAGE_SIZE,
        offset=page * NOTIF_HIST_PAGE_SIZE,
    )

    total_pages = max(1, (total + NOTIF_HIST_PAGE_SIZE - 1) // NOTIF_HIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    # ── Text ─────────────────────────────────────────────────────────────────
    text = "📋 <b>История уведомлений</b>\n"
    text += f"📅 {period_label}"
    if total > 0:
        text += f" · {total} шт."
    text += "\n\n"

    if not rows:
        text += "❌ Нет уведомлений за этот период."
    else:
        for item in rows:
            status     = "✅" if item[4] else "🔵"
            date_str   = format_user_datetime(item[5], user_timezone, '%d.%m %H:%M')
            notif_type = (item[2] or '').replace('_', ' ')
            preview    = (item[3] or '')[:80]
            text += f"{status} <b>{notif_type}</b> · {date_str}\n{preview}\n\n"

    # ── Keyboard ──────────────────────────────────────────────────────────────
    keyboard_buttons: list = []

    # Page navigation
    has_prev = page > 0
    has_next = (page + 1) < total_pages
    if total_pages > 1:
        nav_row = page_nav_row("nhpg_", page, has_prev, has_next, total_pages)
        if nav_row:
            keyboard_buttons.append(nav_row)

    # Period offset navigation (for week/month only)
    if period in ('week', 'month'):
        offset_row = [InlineKeyboardButton(
            text="◀️ Старше", callback_data=f"nhoff_{offset + 1}")]
        if offset > 0:
            offset_row.append(InlineKeyboardButton(
                text="Новее ▶️", callback_data=f"nhoff_{offset - 1}"))
        keyboard_buttons.append(offset_row)

    # Period type switcher
    period_row = []
    if period != 'week':
        period_row.append(InlineKeyboardButton(text="7 дн.", callback_data="nhper_week"))
    if period != 'month':
        period_row.append(InlineKeyboardButton(text="30 дн.", callback_data="nhper_month"))
    if period != 'all':
        period_row.append(InlineKeyboardButton(text="Всё", callback_data="nhper_all"))
    period_row.append(InlineKeyboardButton(text="📅 Период", callback_data="nhper_custom"))
    keyboard_buttons.append(period_row)

    keyboard_buttons.append([
        InlineKeyboardButton(text="✅ Прочитать все", callback_data="mark_all_read"),
        InlineKeyboardButton(text="🗑 Очистить",      callback_data="cleanup_notifications_menu"),
    ])
    keyboard_buttons.append([back_button("notifications_menu")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
        parse_mode="HTML"
    )


@notifications_router.callback_query(F.data == "mark_all_read")
async def mark_all_notifications_read(callback: CallbackQuery, state: FSMContext):
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if user:
        await current_db.mark_notifications_as_read(user[0])
    await callback.answer("✅ Прочитано")
    await show_notification_history(callback, state)


@notifications_router.callback_query(F.data == "cleanup_notifications_menu")
async def cleanup_notifications_menu(callback: CallbackQuery):
    await callback.answer()
    text = "🗑 <b>Очистка истории</b>\n\nВыберите период:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Старше недели",  callback_data="cleanup_confirm:week")],
        [InlineKeyboardButton(text="🗑 Старше месяца",  callback_data="cleanup_confirm:month")],
        [InlineKeyboardButton(text="🗑 Все",             callback_data="cleanup_confirm:all")],
        [back_button("notification_history")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@notifications_router.callback_query(F.data.startswith("cleanup_confirm:"))
async def cleanup_notifications_confirm(callback: CallbackQuery):
    await callback.answer()
    period = callback.data.split(':')[1]
    period_labels = {'week': 'уведомления старше недели',
                     'month': 'уведомления старше месяца',
                     'all': 'ВСЕ уведомления'}
    label = period_labels.get(period, period)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"cleanup_execute:{period}"),
        InlineKeyboardButton(text="❌ Нет",          callback_data="notification_history"),
    ]])
    await callback.message.edit_text(
        f"🗑 <b>Удалить {label}?</b>\n\nЭто действие нельзя отменить.",
        reply_markup=keyboard, parse_mode="HTML"
    )


@notifications_router.callback_query(F.data.startswith("cleanup_execute:"))
async def cleanup_notifications_execute(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split(':')[1]
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if user:
        count = await current_db.delete_old_notifications(user[0], period)
        await callback.answer(f"✅ Удалено: {count}")
    await state.update_data(nh_period='week', nh_offset=0, nh_page=0,
                             nh_start=None, nh_end=None)
    await show_notification_history(callback, state)


@notifications_router.callback_query(F.data == "notif_read")
async def notif_read_handler(callback: CallbackQuery):
    """Кнопка «✅ Прочитано»: удаляет сообщение уведомления из чата.
    Уведомление уже сохранено в notification_history — дополнительных действий не нужно.
    """
    await callback.answer("✅ Прочитано")
    try:
        await callback.message.delete()
    except Exception:
        pass
