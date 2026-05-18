"""
Обработчики для административных функций
"""
import os
import sqlite3
import calendar
import logging
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from utils import he

from database import Database
from keyboards import main_menu, back_button, create_selection_keyboard
from states import AdminUserStates, UserProfileStates, AdminManagementStates, AdminNotificationStates, SearchStates
from pagination_utils import paginate, page_nav_row, PAGE_SIZE_USERS, PAGE_SIZE_ORGS, PAGE_SIZE_BTN
from env_manager import env_manager
from message_utils import safe_edit_message, safe_answer_callback, fsm_edit

# Создаем роутер для административных функций
admin_router = Router()

from db_utils import (get_db, clear_state_keep_org, is_any_admin, is_org_owner,
                       get_user_org_scope, get_role_display_label, get_user_org_role,
                       get_user_full_scope, get_user_custom_title,
                       invalidate_admin_cache, invalidate_scope_cache)
from keyboards import safe_cb, resolve_cb_name
from tenant_manager import tenant_manager

# Получаем ID администратора
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)

async def _render_orgs_page(callback: CallbackQuery, page: int = 0):
    """Показывает страницу N списка организаций."""
    orgs = tenant_manager.get_all_organizations()
    await callback.answer()

    if not orgs:
        await callback.message.edit_text(
            "🏢 <b>Список организаций</b>\n\n❌ Организации не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("system_admin_panel")]]),
            parse_mode="HTML"
        )
        return

    page_items, has_prev, has_next, total_pages, page = paginate(orgs, page, PAGE_SIZE_ORGS)

    text = f"🏢 <b>Список всех организаций:</b> {len(orgs)} шт."
    if total_pages > 1:
        text += f" · стр. {page + 1}/{total_pages}"
    text += "\n\n"

    builder = InlineKeyboardBuilder()
    for org in page_items:
        org_id, name, db_path, owner_id, invite_code, plan, is_active, created = org
        status = "✅" if is_active else "❌"
        users = tenant_manager.get_org_users(org_id)
        text += f"{status} <b>{he(name)}</b> (ID: {org_id})\n"
        text += f"👥 {len(users)} | 💳 {plan or 'Бесплатный'} | 📅 {created[:10] if created else '—'}\n\n"
        builder.row(
            InlineKeyboardButton(text=f"⚙️ {name[:30]}", callback_data=f"select_org_{org_id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"delete_org_{org_id}")
        )

    nav = page_nav_row("orgs_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(back_button("system_admin_panel"))

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@admin_router.callback_query(F.data == "list_all_orgs")
async def list_all_orgs_handler(callback: CallbackQuery, state: FSMContext):
    """Список всех организаций для супер-администратора"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    await _render_orgs_page(callback, page=0)


@admin_router.callback_query(F.data.startswith("orgs_pg_"))
async def orgs_list_page(callback: CallbackQuery, state: FSMContext):
    """Навигация по страницам списка организаций"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    try:
        page = int(callback.data.replace("orgs_pg_", ""))
    except ValueError:
        page = 0
    await _render_orgs_page(callback, page=page)


@admin_router.callback_query(F.data.startswith("delete_org_"))
async def delete_org_confirm(callback: CallbackQuery, state: FSMContext):
    """Запрос подтверждения удаления организации"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    org_id = int(callback.data.replace("delete_org_", ""))

    conn = sqlite3.connect('data/main.db')
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM organizations WHERE id = ?", (org_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await callback.answer("❌ Организация не найдена", show_alert=True)
        return

    org_name = row[0]
    users = tenant_manager.get_org_users(org_id)

    await callback.answer()
    await callback.message.edit_text(
        f"⚠️ <b>Удаление организации</b>\n\n"
        f"Вы собираетесь удалить:\n"
        f"🏢 <b>{he(org_name)}</b> (ID: {org_id})\n"
        f"👥 Сотрудников: {len(users)}\n\n"
        f"❗ Это действие нельзя отменить.\n"
        f"База данных организации и все её данные будут удалены навсегда.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Да, удалить «{org_name}»", callback_data=f"confirm_delete_org_{org_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="list_all_orgs")]
        ]),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data.startswith("confirm_delete_org_"))
async def execute_delete_org(callback: CallbackQuery, state: FSMContext):
    """Выполнение удаления организации"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    org_id = int(callback.data.replace("confirm_delete_org_", ""))

    success, result = tenant_manager.delete_organization(org_id)

    if success:
        data = await state.get_data()
        if data.get("selected_org_id") == org_id:
            await state.update_data(selected_org_id=None, selected_org_name=None, selected_org_db=None)

        await callback.answer()
        await callback.message.edit_text(
            f"✅ <b>Организация «{result}» успешно удалена.</b>\n\n"
            f"База данных организации и все записи удалены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏢 Все организации", callback_data="list_all_orgs")],
                [back_button("system_admin_panel")]
            ]),
            parse_mode="HTML"
        )
    else:
        await callback.answer(f"❌ Ошибка: {result}", show_alert=True)

@admin_router.callback_query(F.data == "system_admin_panel")
@admin_router.callback_query(F.data == "admin_menu")
async def system_admin_panel_handler(callback: CallbackQuery, state: FSMContext):
    """Меню системного администратора"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    from keyboards import system_admin_menu
    await callback.answer()
    await callback.message.edit_text(
        "🔧 <b>Системная панель</b>\n\nВыберите раздел для управления:",
        reply_markup=system_admin_menu(),
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "system_admin_menu")
async def system_admin_menu_back_handler(callback: CallbackQuery, state: FSMContext):
    """Обработчик для кнопки 'Назад' к системной панели"""
    await system_admin_panel_handler(callback, state)


@admin_router.callback_query(F.data == "run_system_tests")
async def run_system_tests_handler(callback: CallbackQuery, state: FSMContext):
    """Запуск всех тестов из системной панели (только супер-админ)"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.answer()

    await callback.message.edit_text(
        "🧪 <b>Запуск тестов...</b>\n\n⏳ Выполняется проверка, подождите.",
        parse_mode="HTML"
    )

    import asyncio, sys

    TEST_SUITES = [
        ("📦 Импорты модулей", "test_imports.py"),
        ("🔁 Callback-хендлеры", "test_callbacks.py"),
        ("🧩 Сценарии (БД, логика)", "test_scenarios.py"),
    ]

    def _extract_summary(out: str, returncode: int) -> str:
        keywords = ["Итог:", "Всего тестов:", "Прошло:", "Провалено:", "🎉", "проблем обнаружено"]
        lines = [l.strip() for l in out.splitlines() if any(kw in l for kw in keywords)]
        if lines:
            return " | ".join(lines[:3])
        return "OK" if returncode == 0 else "ОШИБКА"

    results = []
    all_ok = True
    for label, filename in TEST_SUITES:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, filename,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
            except asyncio.TimeoutError:
                proc.kill()
                results.append(f"⏱ {label}: таймаут (>90 сек)")
                all_ok = False
                continue

            ok = proc.returncode == 0
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            summary = _extract_summary(out, proc.returncode)
            icon = "✅" if ok else "❌"
            block = f"{icon} <b>{he(label)}</b>\n    {he(summary)}"
            if not ok:
                all_ok = False
                # Показываем детали: последние строки stdout + stderr
                detail_lines = [l for l in out.splitlines() if l.strip()][-15:]
                if err.strip():
                    detail_lines += ["— stderr —"] + [l for l in err.splitlines() if l.strip()][-10:]
                if detail_lines:
                    detail_str = "\n".join(detail_lines)
                    block += f"\n<pre>{he(detail_str[:1200])}</pre>"
            results.append(block)
        except Exception as e:
            results.append(f"❌ <b>{he(label)}</b>\n    Ошибка запуска: {he(str(e))}")
            all_ok = False

    status_line = "🎉 Все тесты прошли успешно!" if all_ok else "⚠️ Есть провалы — требуется проверка."
    text = f"🧪 <b>Результаты тестирования</b>\n\n" + "\n\n".join(results) + f"\n\n{status_line}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Запустить снова", callback_data="run_system_tests")],
        [back_button("system_admin_panel")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


_ADMIN_USERS_COLS = "id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, timezone, created_at, username"


async def _collect_admin_users(user_id: int, state: FSMContext):
    """Получает список пользователей и контекст для рендеринга.
    Возвращает (users, title, back_target, show_admin_management, current_db, is_super_user, data)."""
    is_super_user = env_manager.is_super_admin(user_id)
    data = await state.get_data()
    selected_org_id = data.get("selected_org_id")
    selected_org_db = data.get("selected_org_db")

    def _read_users(db_path):
        try:
            c = sqlite3.connect(db_path)
            rows = c.execute(f"SELECT {_ADMIN_USERS_COLS} FROM users").fetchall()
            c.close()
            return rows
        except Exception:
            return []

    def _merge_unique(base, extra):
        existing = {u[1] for u in base}
        for row in extra:
            if row[1] not in existing:
                base.append(row)
                existing.add(row[1])

    current_db = None

    if is_super_user:
        if selected_org_id is None:
            users = _read_users('data/main.db')
            _merge_unique(users, _read_users('data/shop_bot.db'))
            tenants_dir = 'data/tenants'
            if os.path.exists(tenants_dir):
                for f in os.listdir(tenants_dir):
                    if f.endswith('.db'):
                        _merge_unique(users, _read_users(os.path.join(tenants_dir, f)))
            back_target = "system_admin_panel"
            show_admin_management = True
            title = "👥 <b>Все пользователи системы</b>"
        elif selected_org_id == 0:
            users = [u for u in _read_users('data/shop_bot.db') if u[1] == user_id]
            back_target = "admin_management"
            show_admin_management = False
            title = "👤 <b>Личный кабинет</b>"
        else:
            users = tenant_manager.get_org_users(selected_org_id)
            back_target = "admin_management"
            show_admin_management = True
            org_name = data.get("selected_org_name", "Организация")
            title = f"👥 <b>Сотрудники: {he(org_name)}</b>"
    else:
        current_db = await get_db(user_id, state)
        all_users = current_db.get_all_users()
        is_personal_mode = (selected_org_db == "data/shop_bot.db") if selected_org_db else False

        if is_personal_mode or selected_org_db is None:
            users = [u for u in all_users if u[1] == user_id]
            show_admin_management = False
            title = "👤 <b>Мой профиль</b>"
        else:
            users = [u for u in all_users if not env_manager.is_super_admin(u[1])]
            show_admin_management = True
            title = "👥 <b>Управление сотрудниками</b>"

            from filter_utils import ADMIN_FILTER_KEY, empty_filter
            _af = data.get(ADMIN_FILTER_KEY, empty_filter())
            if _af.get("shops"):
                users = [u for u in users if u[8] in _af["shops"]]
            elif _af.get("cities"):
                users = [u for u in users if u[9] in _af["cities"]]
            elif _af.get("networks"):
                users = [u for u in users if u[7] in _af["networks"]]

        back_target = "admin_management"

    return users, title, back_target, show_admin_management, current_db, is_super_user, data


def _build_admin_users_content(users, page, title, back_target, show_admin_management, *,
                                query="", data=None, current_db=None, uid=None, is_super_user=False):
    """Строит (text, markup) для списка пользователей с опциональным поиском."""
    MAX_SEARCH = 20

    if query:
        q = query.lower().lstrip('@')
        filtered = [u for u in users
                    if q in f"{u[2] or ''} {u[3] or ''} {u[4] or ''} {u[5] or ''} {u[8] or ''} {('@' + u[12]) if len(u) > 12 and u[12] else ''}".lower()]
        overflow = max(0, len(filtered) - MAX_SEARCH)
        page_items = filtered[:MAX_SEARCH]
        has_prev = has_next = False
        total_pages = 1
    else:
        filtered = users
        overflow = 0
        page_items, has_prev, has_next, total_pages, page = paginate(filtered, page, PAGE_SIZE_USERS)

    builder = InlineKeyboardBuilder()
    for user in page_items:
        u_id, t_id, f_name, l_name = user[0], user[1], user[2], user[3]
        s_name = user[8] if len(user) > 8 else None
        uname = user[12] if len(user) > 12 else None
        display_name = f"{f_name or '?'}"
        if l_name:
            display_name += f" {l_name}"
        if uname:
            display_name += f" @{uname}"
        elif s_name:
            display_name += f" ({s_name})"
        builder.add(InlineKeyboardButton(text=display_name, callback_data=f"admin_user_{t_id}"))
    builder.adjust(1)

    if not query:
        nav = page_nav_row("au_pg_", page, has_prev, has_next, total_pages)
        if nav:
            builder.row(*nav)

    if show_admin_management:
        try:
            from filter_utils import ADMIN_FILTER_KEY, empty_filter, filter_button_text, get_available_filter_values, has_anything_to_filter
            if not is_super_user and current_db and uid:
                _sc, _sv = get_user_org_scope(uid)
                _avail = get_available_filter_values(current_db, _sc, _sv)
                if has_anything_to_filter(_avail):
                    _af = (data or {}).get(ADMIN_FILTER_KEY, empty_filter())
                    builder.row(InlineKeyboardButton(
                        text=filter_button_text(_af),
                        callback_data="flt_open_admin_users"
                    ))
        except Exception:
            pass
        builder.row(InlineKeyboardButton(text="⚙️ Управление администраторами", callback_data="manage_admins"))

    builder.row(InlineKeyboardButton(text="🔍 Найти", callback_data="adm_usr_srch_start"))
    if query:
        builder.row(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data="adm_usr_srch_cancel"))
    builder.row(back_button(back_target))

    total_count = len(filtered)
    pg_info = f" · стр. {page + 1}/{total_pages}" if not query and total_pages > 1 else ""

    if query:
        if not filtered:
            q_line = f"\n\n🔍 По запросу «{he(query)}» ничего не найдено, попробуйте другой запрос"
            text = f"{title}{q_line}"
        elif overflow:
            q_line = f"\n\n🔍 «{he(query)}» — найдено: {total_count}, показаны первые {MAX_SEARCH}"
            text = f"{title}{q_line}\n\nВыберите пользователя:"
        else:
            q_line = f"\n\n🔍 «{he(query)}» — найдено: {total_count}"
            text = f"{title}{q_line}\n\nВыберите пользователя:"
    else:
        text = f"{title}\n\nВсего: {total_count}{pg_info}\n\nВыберите пользователя:"

    return text, builder.as_markup()


async def _render_admin_users_page(callback: CallbackQuery, state: FSMContext, page: int = 0):
    """Рендерит страницу N списка пользователей с пагинацией."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    await state.update_data(admin_edit_user_id=None, admin_delete_db_path=None)

    users, title, back_target, show_admin_management, current_db, is_super_user, data = \
        await _collect_admin_users(callback.from_user.id, state)

    await state.update_data(anchor_msg_id=callback.message.message_id)

    if not users:
        await callback.message.edit_text(
            f"{title}\n\n❌ Пользователи отсутствуют.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(back_target)]]),
            parse_mode="HTML"
        )
        return

    text, markup = _build_admin_users_content(
        users, page, title, back_target, show_admin_management,
        data=data, current_db=current_db, uid=callback.from_user.id, is_super_user=is_super_user
    )
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@admin_router.callback_query(F.data == "admin_users")
async def admin_users_menu(callback: CallbackQuery, state: FSMContext):
    """Меню управления пользователями"""
    await _render_admin_users_page(callback, state, page=0)


@admin_router.callback_query(F.data.startswith("au_pg_"))
async def admin_users_page(callback: CallbackQuery, state: FSMContext):
    """Навигация по страницам списка пользователей"""
    await callback.answer()
    try:
        page = int(callback.data.replace("au_pg_", ""))
    except ValueError:
        page = 0
    await _render_admin_users_page(callback, state, page=page)


@admin_router.callback_query(F.data == "adm_usr_srch_start")
async def adm_usr_srch_start(callback: CallbackQuery, state: FSMContext):
    """Запуск поиска по списку сотрудников."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await state.set_state(SearchStates.org_users)
    await callback.message.edit_text(
        "🔍 <b>Поиск сотрудника</b>\n\nВведите имя, фамилию или название магазина:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="adm_usr_srch_cancel")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@admin_router.callback_query(F.data == "adm_usr_srch_cancel")
async def adm_usr_srch_cancel(callback: CallbackQuery, state: FSMContext):
    """Сброс поиска — возвращает полный список сотрудников."""
    await state.set_state(None)
    await _render_admin_users_page(callback, state, page=0)


@admin_router.message(SearchStates.org_users)
async def adm_usr_srch_process(message: Message, state: FSMContext):
    """Обрабатывает поисковый запрос по списку сотрудников."""
    if not is_any_admin(message.from_user.id):
        return
    query = (message.text or "").strip()
    await state.set_state(None)
    users, title, back_target, show_admin_management, current_db, is_super_user, data = \
        await _collect_admin_users(message.from_user.id, state)
    text, markup = _build_admin_users_content(
        users, 0, title, back_target, show_admin_management,
        query=query, data=data, current_db=current_db,
        uid=message.from_user.id, is_super_user=is_super_user
    )
    await fsm_edit(state, message, text, reply_markup=markup, parse_mode="HTML")


@admin_router.callback_query(F.data == "admin_confirm_delete")
async def admin_confirm_delete_handler(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления пользователя (для совместимости)"""
    await admin_delete_user_confirm(callback, state)

@admin_router.callback_query(F.data == "admin_delete_user")
async def admin_delete_user_handler(callback: CallbackQuery, state: FSMContext):
    """Начало процесса удаления (совместимость)"""
    await admin_delete_user_confirm(callback, state)

@admin_router.callback_query(F.data == "manage_admins")
async def manage_admins_handler(callback: CallbackQuery, state: FSMContext):
    """Меню управления администраторами"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ разрещен только супер-администратору!", show_alert=True)
        return
    
    await callback.answer()
    admins = env_manager.get_admin_ids()
    
    text = "🛡️ <b>Управление администраторами</b>\n\n"
    if not admins:
        text += "Список администраторов пуст."
    else:
        text += "Список текущих администраторов:\n"
        for i, admin_id in enumerate(admins, 1):
            text += f"{i}. <code>{admin_id}</code>\n"
            
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="➕ Добавить администратора", callback_data="add_admin_start"))
    builder.add(InlineKeyboardButton(text="➖ Удалить администратора", callback_data="remove_admin_start"))
    builder.add(back_button("admin_users"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "add_admin_start")
async def add_admin_start(callback: CallbackQuery, state: FSMContext):
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    # Получаем список пользователей для выбора кнопками
    users = []
    # 1. main.db
    try:
        conn = sqlite3.connect('data/main.db')
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id, first_name, last_name, shop_name FROM users")
        users.extend(cursor.fetchall())
        conn.close()
    except Exception:
        pass
    
    # 2. shop_bot.db
    try:
        conn = sqlite3.connect('data/shop_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id, first_name, last_name, shop_name FROM users")
        rows = cursor.fetchall()
        existing_ids = [u[0] for u in users]
        for row in rows:
            if row[0] not in existing_ids:
                users.append(row)
        conn.close()
    except Exception:
        pass

    page_items, has_prev, has_next, total_pages, page = paginate(users, 0, PAGE_SIZE_BTN)
    pg_line = f"\n<i>Стр. 1/{total_pages} · всего: {len(users)}</i>" if total_pages > 1 else ""
    builder = InlineKeyboardBuilder()
    for t_id, f_name, l_name, s_name in page_items:
        display = f"{f_name or ''} {l_name or ''}".strip() or str(t_id)
        if s_name: display += f" ({s_name})"
        builder.row(InlineKeyboardButton(text=display, callback_data=f"add_admin_id_{t_id}"))
    nav = page_nav_row("add_admin_pg_", 0, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(back_button("manage_admins"))

    await state.update_data(add_admin_users=[(t, f, l, s) for t, f, l, s in users],
                             anchor_msg_id=callback.message.message_id)
    await callback.answer()
    await callback.message.edit_text(
        f"➕ <b>Добавление администратора</b>{pg_line}\n\nВыберите пользователя или <b>введите Telegram ID</b>:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(AdminManagementStates.waiting_for_admin_id)


@admin_router.callback_query(AdminManagementStates.waiting_for_admin_id,
                              F.data.startswith("add_admin_pg_"))
async def add_admin_page(callback: CallbackQuery, state: FSMContext):
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    page = int(callback.data.removeprefix("add_admin_pg_"))
    data = await state.get_data()
    users = data.get('add_admin_users', [])
    page_items, has_prev, has_next, total_pages, page = paginate(users, page, PAGE_SIZE_BTN)
    pg_line = f"\n<i>Стр. {page+1}/{total_pages} · всего: {len(users)}</i>" if total_pages > 1 else ""
    builder = InlineKeyboardBuilder()
    for t_id, f_name, l_name, s_name in page_items:
        display = f"{f_name or ''} {l_name or ''}".strip() or str(t_id)
        if s_name: display += f" ({s_name})"
        builder.row(InlineKeyboardButton(text=display, callback_data=f"add_admin_id_{t_id}"))
    nav = page_nav_row("add_admin_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)
    builder.row(back_button("manage_admins"))
    await callback.message.edit_text(
        f"➕ <b>Добавление администратора</b>{pg_line}\n\nВыберите пользователя или <b>введите Telegram ID</b>:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("add_admin_id_"))
async def process_add_admin_callback(callback: CallbackQuery, state: FSMContext):
    new_admin_id = int(callback.data.replace("add_admin_id_", ""))
    current_admins = env_manager.get_admin_ids()
    
    if new_admin_id in current_admins:
        await callback.answer(f"ℹ️ Пользователь {new_admin_id} уже является администратором.", show_alert=True)
        await clear_state_keep_org(state)
        return

    if env_manager.add_admin_id(new_admin_id):
        await callback.answer()
        await callback.message.edit_text(
            f"✅ Пользователь <code>{new_admin_id}</code> назначен администратором.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_admins")]]),
            parse_mode="HTML"
        )
    else:
        await callback.answer("❌ Ошибка при сохранении настроек.", show_alert=True)
    await clear_state_keep_org(state)

@admin_router.message(AdminManagementStates.waiting_for_admin_id)
async def process_add_admin(message: Message, state: FSMContext):
    _back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_admins")]])
    try:
        new_admin_id = int(message.text.strip())
        if env_manager.add_admin_id(new_admin_id):
            await fsm_edit(state, message,
                           f"✅ Пользователь <code>{new_admin_id}</code> назначен администратором.",
                           reply_markup=_back_kb)
        else:
            await fsm_edit(state, message,
                           "❌ Не удалось добавить администратора (возможно, он уже в списке).",
                           reply_markup=_back_kb)
        await clear_state_keep_org(state)
    except ValueError:
        await fsm_edit(state, message, "❌ Пожалуйста, введите корректный числовой Telegram ID.", reply_markup=_back_kb)

@admin_router.callback_query(F.data == "remove_admin_start")
async def remove_admin_start(callback: CallbackQuery, state: FSMContext):
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    admins = env_manager.get_admin_ids()
    if not admins:
        await callback.answer("❌ Список администраторов пуст!", show_alert=True)
        return

    await callback.answer()
    builder = InlineKeyboardBuilder()
    for admin_id in admins:
        builder.add(InlineKeyboardButton(text=f"❌ Удалить {admin_id}", callback_data=f"remove_admin_{admin_id}"))
    
    builder.add(back_button("manage_admins"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        "➖ <b>Удаление администратора</b>\n\nВыберите ID для удаления:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data.startswith("remove_admin_"))
async def process_remove_admin(callback: CallbackQuery, state: FSMContext):
    admin_id = int(callback.data.replace("remove_admin_", ""))
    if env_manager.remove_admin_id(admin_id):
        await callback.answer(f"✅ Администратор {admin_id} удален")
        # Возвращаемся в меню
        # Вызываем функцию напрямую для обновления сообщения
        await manage_admins_handler(callback, state)
    else:
        await callback.answer("❌ Ошибка при удалении администратора", show_alert=True)

@admin_router.callback_query(F.data == "admin_management")
async def admin_management_menu_handler(callback: CallbackQuery, state: FSMContext):
    """Обработчик меню управления"""
    user_id = callback.from_user.id
    
    is_super = env_manager.is_super_admin(user_id)
    is_admin = (is_super or is_any_admin(user_id))
    
    if not is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return

    await callback.answer()
    if is_super:
        user_data = await state.get_data()
        selected_org_id = user_data.get('selected_org_id')
        
        # Если selected_org_id не задан в стейте, показываем меню выбора
        if selected_org_id is None:
            # Предлагаем выбрать организацию
            try:
                conn = sqlite3.connect('data/main.db')
                cursor = conn.cursor()
                cursor.execute("SELECT id, name FROM organizations WHERE is_active = 1")
                orgs = cursor.fetchall()
                conn.close()
            except Exception as e:
                orgs = []
            
            builder = InlineKeyboardBuilder()
            builder.add(InlineKeyboardButton(text="👤 Личный кабинет (Основная БД)", callback_data="select_org_0"))
            
            for org_id, org_name in orgs:
                builder.add(InlineKeyboardButton(text=f"🏢 {org_name}", callback_data=f"select_org_{org_id}"))
            
            builder.add(back_button("main_menu"))
            builder.adjust(1)
            
            await callback.message.edit_text(
                "🏢 <b>Выбор организации</b>\n\nВыберите организацию для управления или личный кабинет:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            return
    else:
        # Для org-админов определяем контекст по их организации; для личных — Личный кабинет
        _db_path = tenant_manager.get_user_db_path(callback.from_user.id)
        if _db_path != 'data/shop_bot.db':
            try:
                _conn = sqlite3.connect(tenant_manager.main_db_path)
                _cur = _conn.cursor()
                _cur.execute(
                    "SELECT m.org_id, o.name FROM user_org_mapping m "
                    "JOIN organizations o ON m.org_id = o.id "
                    "WHERE m.telegram_id = ?", (callback.from_user.id,)
                )
                _row = _cur.fetchone()
                _conn.close()
                if _row:
                    await state.update_data(selected_org_id=_row[0], selected_org_name=_row[1], selected_org_db=_db_path)
                else:
                    await state.update_data(selected_org_id=0, selected_org_name="Личный кабинет", selected_org_db="data/shop_bot.db")
            except Exception:
                await state.update_data(selected_org_id=0, selected_org_name="Личный кабинет", selected_org_db="data/shop_bot.db")
        else:
            await state.update_data(selected_org_id=0, selected_org_name="Личный кабинет", selected_org_db="data/shop_bot.db")

    from keyboards import admin_management_menu
    # Добавляем информацию о выбранной организации
    user_data = await state.get_data()
    org_name = user_data.get('selected_org_name', 'Личный кабинет')
    
    text = (
        f"⚙️ <b>Управление: {he(org_name)}</b>\n\n"
        "Здесь собраны все инструменты для управления:"
    )
    
    keyboard = admin_management_menu(callback.from_user.id)
    # Если это супер-админ, добавляем кнопку смены организации и генерации приглашения
    if is_super:
        new_keyboard = list(keyboard.inline_keyboard)
        # Получаем текущие данные организации
        user_data = await state.get_data()
        selected_org_id = user_data.get('selected_org_id')
        
        # Добавляем кнопку генерации приглашения, если выбрана реальная организация (org_id > 0)
        if selected_org_id and selected_org_id > 0:
            has_invite_btn = any(any(btn.callback_data == "generate_invite" for btn in row) for row in new_keyboard)
            if not has_invite_btn:
                # Вставляем перед кнопкой "Назад"
                new_keyboard.insert(-1, [InlineKeyboardButton(text="📩 Создать приглашение", callback_data="generate_invite")])
        
        # Проверяем кнопку смены организации
        has_change_btn = any(any(btn.callback_data == "change_org_context" for btn in row) for row in new_keyboard)
        if not has_change_btn:
            if len(new_keyboard) > 0:
                new_keyboard.insert(-1, [InlineKeyboardButton(text="🔄 Сменить организацию", callback_data="change_org_context")])
            else:
                new_keyboard.append([InlineKeyboardButton(text="🔄 Сменить организацию", callback_data="change_org_context")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=new_keyboard)

    elif is_admin:
        user_data = await state.get_data()
        _org_id = user_data.get('selected_org_id', 0)
        if _org_id and _org_id > 0:
            _new_kb = list(keyboard.inline_keyboard)
            _has_invite = any(any(btn.callback_data == "generate_invite" for btn in row) for row in _new_kb)
            if not _has_invite:
                _new_kb.insert(-1, [InlineKeyboardButton(text="📩 Создать приглашение", callback_data="generate_invite")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=_new_kb)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "select_org_0")
async def select_personal_org(callback: CallbackQuery, state: FSMContext):
    """Быстрый выбор личного кабинета"""
    await state.update_data(selected_org_id=0, selected_org_name="Личный кабинет", selected_org_db="data/shop_bot.db")
    await callback.answer("✅ Выбран Личный кабинет")
    await admin_management_menu_handler(callback, state)

@admin_router.callback_query(F.data == "change_org_context")
async def change_org_context_handler(callback: CallbackQuery, state: FSMContext):
    """Сброс контекста организации для супер-админа"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ разрещен только супер-администратору!", show_alert=True)
        return
    await state.update_data(selected_org_id=None, selected_org_name=None, selected_org_db=None)
    await admin_management_menu_handler(callback, state)

@admin_router.callback_query(F.data == "generate_invite")
async def generate_invite_handler(callback: CallbackQuery, state: FSMContext):
    """Генерация кода приглашения"""
    user_id = callback.from_user.id
    is_super = env_manager.is_super_admin(user_id)
    
    org_id = None
    if is_super:
        data = await state.get_data()
        org_id = data.get('selected_org_id')
    else:
        # Для обычных админов получаем их организацию
        conn = sqlite3.connect(tenant_manager.main_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT org_id FROM user_org_mapping WHERE telegram_id = ?", (user_id,))
        mapping = cursor.fetchone()
        conn.close()
        if mapping:
            org_id = mapping[0]
            
    if not org_id or org_id == 0:
        await callback.answer("❌ В личном режиме приглашения не поддерживаются", show_alert=True)
        return
        
    invite_code = tenant_manager.generate_invite_code(org_id)
    if invite_code:
        await callback.answer()
        await callback.message.edit_text(
            f"📩 <b>Код приглашения создан!</b>\n\n"
            f"Код: <code>{invite_code}</code>\n\n"
            f"Отправьте этот код сотруднику. Он должен будет выбрать '🔗 Войти по приглашению' при регистрации.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_management")]]),
            parse_mode="HTML"
        )
    else:
        await callback.answer("❌ Ошибка при генерации кода", show_alert=True)

@admin_router.callback_query(F.data.startswith("select_org_"))
async def select_org_handler(callback: CallbackQuery, state: FSMContext):
    """Выбор организации для супер-администратора"""
    org_id_str = callback.data.replace("select_org_", "")
    try:
        org_id = int(org_id_str)
    except ValueError:
        await callback.answer("❌ Ошибка выбора организации")
        return
    
    if org_id == 0:
        # Личный кабинет (shop_bot.db для работы с данными)
        await state.update_data(selected_org_id=0, selected_org_name="Личный кабинет", selected_org_db="data/shop_bot.db")
        await callback.answer("✅ Выбран Личный кабинет")
    else:
        conn = sqlite3.connect('data/main.db')
        cursor = conn.cursor()
        cursor.execute("SELECT name, db_path FROM organizations WHERE id = ?", (org_id,))
        org = cursor.fetchone()
        conn.close()
        
        if not org:
            await callback.answer("❌ Организация не найдена", show_alert=True)
            return
            
        org_name, db_path = org
        await state.update_data(selected_org_id=org_id, selected_org_name=org_name, selected_org_db=db_path)
        await callback.answer(f"✅ Выбрана организация: {org_name}")
    
    await admin_management_menu_handler(callback, state)

@admin_router.callback_query(F.data.startswith("admin_user_"))
async def admin_user_details(callback: CallbackQuery, state: FSMContext):
    """Детали пользователя для администратора"""
    caller_is_admin = is_any_admin(callback.from_user.id)
    if not caller_is_admin:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    try:
        telegram_id = int(callback.data.replace("admin_user_", ""))
    except ValueError:
        await callback.answer("❌ Ошибка в данных пользователя!", show_alert=True)
        return
    
    is_super_user = env_manager.is_super_admin(callback.from_user.id)
    user = None

    # 1. Ищем в текущем контексте администратора (тенант для org-admin, shop_bot.db для личного)
    try:
        ctx_db = await get_db(callback.from_user.id, state)
        user = ctx_db.get_user(telegram_id)
        if user:
            await state.update_data(admin_delete_db_path=ctx_db.db_file)
    except Exception as e:
        logging.error(f"Error searching context db: {e}")

    # 2. Ищем в main.db (личные пользователи и суп-админ)
    if not user:
        try:
            conn = sqlite3.connect('data/main.db')
            cursor = conn.cursor()
            cursor.execute("SELECT id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, timezone, created_at, username FROM users WHERE telegram_id = ?", (telegram_id,))
            user = cursor.fetchone()
            if user:
                await state.update_data(admin_delete_db_path='data/main.db')
            conn.close()
        except Exception as e:
            logging.error(f"Error searching in main.db: {e}")

    # 3. Ищем в shop_bot.db
    if not user:
        try:
            conn = sqlite3.connect('data/shop_bot.db')
            cursor = conn.cursor()
            cursor.execute("SELECT id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, timezone, created_at, username FROM users WHERE telegram_id = ?", (telegram_id,))
            user = cursor.fetchone()
            if user:
                await state.update_data(admin_delete_db_path='data/shop_bot.db')
            conn.close()
        except Exception as e:
            logging.error(f"Error searching in shop_bot.db: {e}")

    # 4. Последний шанс для супер-админа — сканируем все тенанты
    if not user and is_super_user:
        tenants_dir = 'data/tenants'
        if os.path.exists(tenants_dir):
            for f in os.listdir(tenants_dir):
                if f.endswith('.db'):
                    p = os.path.join(tenants_dir, f)
                    try:
                        t_db = Database(p)
                        user = t_db.get_user(telegram_id)
                        if user:
                            await state.update_data(admin_delete_db_path=p)
                            break
                    except Exception:
                        continue

    if not user:
        await callback.answer("❌ Пользователь не найден ни в одной базе данных", show_alert=True)
        return
        
    u_id, telegram_id, first_name, last_name, middle_name, phone, email, trade_network, shop_name, city, timezone, created_at = user[:12]
    username = user[12] if len(user) > 12 else None
    
    await state.update_data(admin_edit_user_id=telegram_id)
    await callback.answer()
    
    # Определяем роль и зону ответственности
    if env_manager.is_super_admin(telegram_id):
        user_role = "👑 Глобальный суперадмин"
        scope_line = ""
    else:
        org_role = get_user_org_role(telegram_id)
        target_scope_type, target_scope_values, custom_title = get_user_full_scope(telegram_id)
        user_role = get_role_display_label(org_role or 'user', target_scope_type, target_scope_values, custom_title)
        scope_line = ""
        if org_role == 'admin' and target_scope_type and target_scope_type != 'all' and target_scope_values:
            scope_icons = {'shop': '🏪', 'city': '🏙️', 'network': '🌐'}
            scope_names = {'shop': 'Магазин', 'city': 'Город', 'network': 'Сеть'}
            vals_str = ', '.join(he(v) for v in target_scope_values[:3])
            if len(target_scope_values) > 3:
                vals_str += f' +{len(target_scope_values) - 3}'
            scope_line = (
                f"\n{scope_icons.get(target_scope_type,'📍')} "
                f"<b>Зона ({scope_names.get(target_scope_type,'')}):</b> {vals_str}"
            )
        if org_role in ('admin', 'owner') and custom_title:
            scope_line += f"\n🎖️ <b>Должность:</b> {he(custom_title)}"

    # Статус заморозки
    _is_active = tenant_manager.get_user_org_is_active(telegram_id)
    frozen_badge = "\n⛔ <b>ЗАМОРОЖЕН</b> (нет доступа к организации)" if _is_active is False else ""

    message_text = (
        f"👤 Пользователь: {he(first_name)} {he(last_name)}{frozen_badge}\n\n"
        f"🎖️ Роль: <b>{user_role}</b>{scope_line}\n"
        f"🔗 Telegram ID: {telegram_id}\n"
        f"👤 ФИО: {he(first_name)} {he(last_name)}"
    )
    
    if middle_name:
        message_text += f" {he(middle_name)}"
    
    if username:
        message_text += f"\n📱 <a href='tg://resolve?domain={he(username)}'>@{he(username)}</a>"
    
    message_text += f"\n📞 Телефон: {phone or 'не указан'}"
    message_text += f"\n📧 Email: {email or 'не указан'}"
    message_text += f"\n🏢 Торговая сеть: {he(trade_network) if trade_network else 'не указана'}"
    message_text += f"\n🏪 Магазин: {he(shop_name) if shop_name else 'не указан'}"
    message_text += f"\n🏙️ Город: {he(city) if city else 'не указан'}"
    
    if telegram_id:
        message_text += f"\n\n🔗 <b>Telegram:</b> <a href='tg://user?id={telegram_id}'>Написать пользователю</a>"
    
    # Кнопку смены роли: недоступно для глобального суперадмина и самого себя
    caller_is_super = env_manager.is_super_admin(callback.from_user.id)
    caller_is_owner = is_org_owner(callback.from_user.id)
    target_org_role_for_buttons = get_user_org_role(telegram_id)
    target_is_owner = target_org_role_for_buttons == 'owner'

    can_change_role = False
    if telegram_id != callback.from_user.id and not env_manager.is_super_admin(telegram_id):
        if caller_is_super:
            can_change_role = True
        elif caller_is_owner and not target_is_owner:
            can_change_role = True
        elif caller_is_admin and not is_org_owner(callback.from_user.id):
            can_change_role = (target_org_role_for_buttons == 'user')

    # Может ли вызывающий ставить название должности этому пользователю
    can_set_title = (
        (caller_is_super or caller_is_owner) and
        not env_manager.is_super_admin(telegram_id) and
        target_org_role_for_buttons in ('admin', 'owner')
    )

    # Проверяем is_active пользователя в org_mapping
    user_is_active = tenant_manager.get_user_org_is_active(telegram_id)

    buttons = [
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="admin_edit_user")],
    ]
    if can_change_role and user_is_active is not False:
        buttons.append([InlineKeyboardButton(text="🎖️ Изменить роль", callback_data="adm_role_menu")])
    if can_set_title and user_is_active is not False:
        buttons.append([InlineKeyboardButton(text="🏷️ Название должности", callback_data="adm_title_start")])
    if telegram_id != callback.from_user.id:
        if user_is_active is False:
            # Пользователь заморожен — предлагаем восстановить
            buttons.append([InlineKeyboardButton(text="🔄 Восстановить в организацию", callback_data="adm_restore_confirm")])
        else:
            buttons.append([InlineKeyboardButton(text="🚪 Исключить из орга", callback_data="adm_kick_confirm")])
    if caller_is_super and not env_manager.is_super_admin(telegram_id) and telegram_id != callback.from_user.id:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить полностью", callback_data="admin_delete_user")])
    buttons.append([back_button("admin_users")])

    await callback.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "adm_role_menu")
async def adm_role_menu(callback: CallbackQuery, state: FSMContext):
    """Меню выбора новой роли для пользователя."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    await callback.answer()

    caller_is_super = env_manager.is_super_admin(callback.from_user.id)
    caller_is_owner = is_org_owner(callback.from_user.id)

    # Текущая роль цели
    current_role = get_user_org_role(telegram_id)
    current_scope_t, current_scope_v = get_user_org_scope(telegram_id)
    current_ctitle = get_user_custom_title(telegram_id)
    current_label = get_role_display_label(current_role or 'user', current_scope_t, current_scope_v, current_ctitle)

    buttons = [
        [InlineKeyboardButton(text="👤 Сотрудник", callback_data="adm_role_set_user")],
    ]
    if caller_is_super or caller_is_owner:
        buttons.append([InlineKeyboardButton(text="🛡️ Администратор (выбор зон)", callback_data="adm_role_set_admin")])
    if caller_is_super:
        buttons.append([InlineKeyboardButton(text="👑 Директор", callback_data="adm_role_set_owner")])
    # Название должности — для admin/owner, доступно напрямую из этого меню
    if current_role in ('admin', 'owner'):
        buttons.append([InlineKeyboardButton(text="🏷️ Изменить название должности", callback_data="adm_title_start")])
    buttons.append([back_button(f"admin_user_{telegram_id}")])

    await callback.message.edit_text(
        f"🎖️ <b>Изменить роль пользователя</b>\n\n"
        f"Текущая роль: <b>{current_label}</b>\n\n"
        f"Выберите новую роль:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data == "adm_role_set_user")
async def adm_role_set_user(callback: CallbackQuery, state: FSMContext):
    """Назначает роль «Сотрудник» (user) напрямую."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return
    if env_manager.is_super_admin(telegram_id):
        await callback.answer("❌ Роль глобального суперадмина изменить нельзя.", show_alert=True)
        return
    await callback.answer()
    success, result = tenant_manager.change_user_role(telegram_id, 'user')
    invalidate_admin_cache(telegram_id)
    invalidate_scope_cache(telegram_id)
    if success:
        await callback.message.edit_text(
            "✅ Роль изменена на: <b>👤 Сотрудник</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"❌ Ошибка: {result}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
        )


@admin_router.callback_query(F.data == "adm_role_set_owner")
async def adm_role_set_owner(callback: CallbackQuery, state: FSMContext):
    """Назначает роль «Директор» (owner) — только глобальный суперадмин."""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Только глобальный суперадмин может назначить директора.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return
    if env_manager.is_super_admin(telegram_id):
        await callback.answer("❌ Роль глобального суперадмина изменить нельзя.", show_alert=True)
        return
    await callback.answer()
    success, result = tenant_manager.change_user_role(telegram_id, 'owner')
    invalidate_admin_cache(telegram_id)
    invalidate_scope_cache(telegram_id)
    if success:
        await callback.message.edit_text(
            "✅ Роль изменена на: <b>👑 Директор</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"❌ Ошибка: {result}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
        )


@admin_router.callback_query(F.data == "adm_role_set_admin")
async def adm_role_set_admin_scope_menu(callback: CallbackQuery, state: FSMContext):
    """Шаг 1 — выбор типа зоны ответственности для администратора."""
    if not (is_org_owner(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Только директор или суперадмин может назначать администраторов.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return
    if env_manager.is_super_admin(telegram_id):
        await callback.answer("❌ Роль глобального суперадмина изменить нельзя.", show_alert=True)
        return
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="🌐 Весь орг (Зам. директора)", callback_data="adm_scope_all")],
        [InlineKeyboardButton(text="🏪 Конкретный магазин", callback_data="adm_scope_type_shop")],
        [InlineKeyboardButton(text="🏙️ Город", callback_data="adm_scope_type_city")],
        [InlineKeyboardButton(text="🌐 Торговая сеть", callback_data="adm_scope_type_network")],
        [back_button("adm_role_menu")],
    ]
    await callback.message.edit_text(
        "🛡️ <b>Зона ответственности администратора</b>\n\n"
        "Выберите, какую зону будет курировать этот администратор:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data == "adm_scope_all")
async def adm_scope_all(callback: CallbackQuery, state: FSMContext):
    """Назначает admin с полным доступом (зам. директора)."""
    if not (is_org_owner(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return
    await callback.answer()
    success, result = tenant_manager.change_user_role(telegram_id, 'admin', scope_type='all')
    invalidate_admin_cache(telegram_id)
    invalidate_scope_cache(telegram_id)
    if success:
        await callback.message.edit_text(
            "✅ Роль изменена на: <b>🛡️ Зам. директора</b> (весь орг)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"❌ Ошибка: {result}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
        )


async def _render_scope_list(message, state: FSMContext, current_db, scope_type: str, page: int = 0):
    """Рендерит экран мульти-выбора зоны ответственности с чекбоксами и пагинацией."""
    import sqlite3 as _sq3
    data = await state.get_data()
    selections: list = list(data.get('adm_scope_sels', []))

    field_map = {'shop': 'shop_name', 'city': 'city', 'network': 'trade_network'}
    icon_map  = {'shop': '🏪', 'city': '🏙️', 'network': '🌐'}
    name_map  = {'shop': 'магазин(ы)', 'city': 'город(а)', 'network': 'сеть(и)'}
    cb_prefix = {'shop': 'adm_t_s_', 'city': 'adm_t_c_', 'network': 'adm_t_n_'}

    field  = field_map[scope_type]
    icon   = icon_map[scope_type]
    prefix = cb_prefix[scope_type]

    try:
        conn = _sq3.connect(current_db.db_file)
        rows = conn.execute(
            f"SELECT DISTINCT {field} FROM users WHERE {field} IS NOT NULL AND {field} != '' ORDER BY {field}"
        ).fetchall()
        conn.close()
        values = [r[0] for r in rows]
    except Exception:
        values = []

    if not values:
        await message.edit_text(
            f"❌ Нет данных для выбора ({name_map.get(scope_type,'зоны')}).\n"
            "Сначала заполните профили сотрудников.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("adm_role_set_admin")]]),
            parse_mode="HTML"
        )
        return

    page_items, has_prev, has_next, total_pages, page = paginate(values, page, PAGE_SIZE_BTN)
    await state.update_data(adm_scope_pg=page)

    builder = InlineKeyboardBuilder()
    for val in page_items:
        mark = "✅" if val in selections else "☐"
        cb = safe_cb(prefix, val)
        builder.row(InlineKeyboardButton(text=f"{mark} {icon} {val}", callback_data=cb))

    nav = page_nav_row("adm_scope_pg_", page, has_prev, has_next, total_pages)
    if nav:
        builder.row(*nav)

    n = len(selections)
    if n > 0:
        builder.row(InlineKeyboardButton(text=f"✅ Подтвердить ({n} выбрано)",
                                         callback_data="adm_scope_submit"))
    builder.row(back_button("adm_role_set_admin"))

    pg_line = f"\n<i>Стр. {page+1}/{total_pages} · всего: {len(values)}</i>" if total_pages > 1 else ""
    await message.edit_text(
        f"🛡️ <b>Выберите {name_map.get(scope_type, 'зону')}:</b>{pg_line}\n\n"
        f"Можно выбрать несколько — выбрано: <b>{n}</b>\n"
        f"Нажмите элемент для выбора / снятия выбора.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data.startswith("adm_scope_type_"))
async def adm_scope_type_select(callback: CallbackQuery, state: FSMContext):
    """Шаг 2 — выбираем тип зоны, сбрасываем выборку и показываем мульти-список."""
    if not (is_org_owner(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    if not data.get('admin_edit_user_id'):
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    scope_type = callback.data.removeprefix("adm_scope_type_")
    await state.update_data(adm_scope_type=scope_type, adm_scope_sels=[])
    await callback.answer()

    current_db = await get_db(callback.from_user.id, state)
    await _render_scope_list(callback.message, state, current_db, scope_type)


async def _toggle_scope_value(callback: CallbackQuery, state: FSMContext,
                               scope_type: str, prefix: str):
    """Общая функция — переключает одно значение в adm_scope_sels."""
    if not (is_org_owner(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    if not data.get('admin_edit_user_id'):
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    partial = callback.data.removeprefix(prefix)

    current_db = await get_db(callback.from_user.id, state)
    try:
        import sqlite3 as _sq3
        field_map = {'shop': 'shop_name', 'city': 'city', 'network': 'trade_network'}
        field = field_map[scope_type]
        conn = _sq3.connect(current_db.db_file)
        candidates = [r[0] for r in conn.execute(
            f"SELECT DISTINCT {field} FROM users WHERE {field} IS NOT NULL AND {field} != ''"
        ).fetchall()]
        conn.close()
    except Exception:
        candidates = []

    full_val = resolve_cb_name(partial, candidates) if candidates else partial

    sels: list = list(data.get('adm_scope_sels', []))
    if full_val in sels:
        sels.remove(full_val)
    else:
        sels.append(full_val)
    data2 = await state.get_data()
    page = data2.get('adm_scope_pg', 0)
    await state.update_data(adm_scope_sels=sels)
    await callback.answer()
    await _render_scope_list(callback.message, state, current_db, scope_type, page=page)


@admin_router.callback_query(F.data.startswith("adm_scope_pg_"))
async def adm_scope_page(callback: CallbackQuery, state: FSMContext):
    """Пагинация в списке зон ответственности."""
    if not (is_org_owner(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    page = int(callback.data.removeprefix("adm_scope_pg_"))
    data = await state.get_data()
    scope_type = data.get('adm_scope_type', 'shop')
    current_db = await get_db(callback.from_user.id, state)
    await callback.answer()
    await _render_scope_list(callback.message, state, current_db, scope_type, page=page)


@admin_router.callback_query(F.data.startswith("adm_t_s_"))
async def adm_toggle_shop(callback: CallbackQuery, state: FSMContext):
    await _toggle_scope_value(callback, state, 'shop', "adm_t_s_")


@admin_router.callback_query(F.data.startswith("adm_t_c_"))
async def adm_toggle_city(callback: CallbackQuery, state: FSMContext):
    await _toggle_scope_value(callback, state, 'city', "adm_t_c_")


@admin_router.callback_query(F.data.startswith("adm_t_n_"))
async def adm_toggle_net(callback: CallbackQuery, state: FSMContext):
    await _toggle_scope_value(callback, state, 'network', "adm_t_n_")


@admin_router.callback_query(F.data == "adm_scope_submit")
async def adm_scope_submit(callback: CallbackQuery, state: FSMContext):
    """Применяет роль admin с выбранным списком зон, затем предлагает задать название должности."""
    if not (is_org_owner(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    scope_type   = data.get('adm_scope_type')
    scope_values: list = data.get('adm_scope_sels', [])

    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return
    if not scope_values:
        await callback.answer("⚠️ Выберите хотя бы одно значение!", show_alert=True)
        return

    await callback.answer()

    success, result = tenant_manager.change_user_role(
        telegram_id, 'admin', scope_type=scope_type, scope_value=scope_values
    )
    invalidate_admin_cache(telegram_id)
    invalidate_scope_cache(telegram_id)

    if not success:
        await callback.message.edit_text(
            f"❌ Ошибка: {result}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
        )
        return

    scope_icons_ru = {'shop': '🏪', 'city': '🏙️', 'network': '🌐'}
    vals_preview = ', '.join(scope_values[:3])
    if len(scope_values) > 3:
        vals_preview += f' +{len(scope_values) - 3}'
    icon = scope_icons_ru.get(scope_type, '📍')

    await callback.message.edit_text(
        f"✅ Роль <b>Администратор</b> назначена.\n"
        f"{icon} Зона: <b>{vals_preview}</b>\n\n"
        f"🏷️ Хотите задать пользовательское название должности?\n"
        f"<i>Например: Супервайзер, РОП, Куратор регионов…</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Задать название", callback_data="adm_title_start")],
            [InlineKeyboardButton(text="Пропустить →", callback_data=f"admin_user_{telegram_id}")],
        ]),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data == "adm_title_start")
async def adm_title_start(callback: CallbackQuery, state: FSMContext):
    """Запускает FSM для ввода пользовательского названия должности."""
    if not (is_org_owner(callback.from_user.id) or env_manager.is_super_admin(callback.from_user.id)):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    current_title = get_user_custom_title(telegram_id) or ''
    await callback.answer()
    await state.set_state(AdminUserStates.waiting_for_admin_title)
    hint = f"\n\nТекущее: <b>{he(current_title)}</b>" if current_title else ""
    await callback.message.edit_text(
        f"🏷️ <b>Название должности</b>{hint}\n\n"
        "Введите новое название (до 50 символов).\n"
        "Примеры: <i>Супервайзер, РОП, Куратор, Региональный менеджер…</i>\n\n"
        "Отправьте <b>«-»</b> чтобы сбросить название и вернуть стандартное.",
        parse_mode="HTML"
    )


@admin_router.message(AdminUserStates.waiting_for_admin_title)
async def adm_title_entered(message, state: FSMContext):
    """Сохраняет введённое название должности."""
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')

    raw = message.text.strip() if message.text else ''
    if raw == '-':
        title = None
        status_text = "✅ Название должности сброшено (используется стандартное)."
    elif not raw:
        await message.answer("❌ Введите название или «-» для сброса.")
        return
    else:
        title = raw[:50]
        status_text = f"✅ Название должности установлено: <b>{he(title)}</b>"

    if telegram_id:
        tenant_manager.set_user_title(telegram_id, title)

    await fsm_edit(message, state, status_text, parse_mode="HTML")
    await clear_state_keep_org(state)

@admin_router.callback_query(F.data == "admin_edit_user_final_confirm")
async def admin_edit_user_final_confirm(callback: CallbackQuery, state: FSMContext):
    """Перенаправляем через экран подтверждения — bypass небезопасен"""
    await admin_delete_user_confirm(callback, state)

@admin_router.callback_query(F.data == "adm_kick_confirm")
async def adm_kick_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение исключения пользователя из организации (без удаления данных)."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')

    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    if telegram_id == callback.from_user.id:
        await callback.answer("❌ Нельзя исключить самого себя.", show_alert=True)
        return

    user = None
    if db_path and os.path.exists(db_path):
        user = Database(db_path).get_user(telegram_id)

    user_name = f"{he(user[2])} {he(user[3])}" if user else str(telegram_id)

    await callback.answer()
    await callback.message.edit_text(
        f"🚪 <b>Заморозить в организации</b>\n\n"
        f"Пользователь: <b>{user_name}</b>\n"
        f"Telegram ID: <code>{telegram_id}</code>\n\n"
        f"Пользователь <b>потеряет доступ</b> к организации, "
        f"но все его продажи и история сохранятся в отчётах.\n"
        f"Восстановить можно в любой момент через карточку сотрудника.\n\n"
        f"Продолжить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, заморозить", callback_data="adm_kick_final"),
             InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_{telegram_id}")]
        ]),
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "adm_kick_final")
async def adm_kick_final(callback: CallbackQuery, state: FSMContext):
    """Заморозка пользователя в орге — устанавливает is_active=0 в user_org_mapping."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')

    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    await callback.answer()

    success, result = tenant_manager.remove_user_from_org(telegram_id)

    if success:
        invalidate_admin_cache(telegram_id)
        invalidate_scope_cache(telegram_id)
        await callback.message.edit_text(
            f"✅ Пользователь <code>{telegram_id}</code> заморожен.\n\n"
            f"Его продажи и история сохранены в отчётах.\n"
            f"Пользователь не сможет войти по инвайт-коду — восстановить можно в карточке сотрудника.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_users")]]),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"❌ Ошибка при исключении: {result}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_users")]]),
            parse_mode="HTML"
        )
    await state.update_data(admin_edit_user_id=None, admin_delete_db_path=None)


@admin_router.callback_query(F.data == "adm_restore_confirm")
async def adm_restore_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение восстановления пользователя в организацию."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')

    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    user = None
    if db_path and os.path.exists(db_path):
        user = Database(db_path).get_user(telegram_id)

    user_name = f"{he(user[2])} {he(user[3])}" if user else str(telegram_id)

    await callback.answer()
    await callback.message.edit_text(
        f"🔄 <b>Восстановить в организацию</b>\n\n"
        f"Пользователь: <b>{user_name}</b>\n"
        f"Telegram ID: <code>{telegram_id}</code>\n\n"
        f"Пользователь снова получит доступ к организации.\n\n"
        f"Продолжить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, восстановить", callback_data="adm_restore_final"),
             InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_{telegram_id}")]
        ]),
        parse_mode="HTML"
    )


@admin_router.callback_query(F.data == "adm_restore_final")
async def adm_restore_final(callback: CallbackQuery, state: FSMContext):
    """Восстановление пользователя в организацию (is_active=1)."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')

    if not telegram_id:
        await callback.answer("❌ Контекст потерян.", show_alert=True)
        return

    await callback.answer()

    success, result = tenant_manager.restore_user_to_org(telegram_id)

    if success:
        invalidate_admin_cache(telegram_id)
        invalidate_scope_cache(telegram_id)
        await callback.message.edit_text(
            f"✅ Пользователь <code>{telegram_id}</code> восстановлен в организацию.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_users")]]),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"❌ Ошибка при восстановлении: {he(result)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_users")]]),
            parse_mode="HTML"
        )
    await state.update_data(admin_edit_user_id=None, admin_delete_db_path=None)


@admin_router.callback_query(F.data == "admin_edit_user")
async def admin_edit_user_menu(callback: CallbackQuery, state: FSMContext):
    """Меню редактирования пользователя"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    
    if not telegram_id or not db_path:
        await callback.answer("❌ Контекст редактирования потерян!", show_alert=True)
        return
    
    await callback.answer()
    await callback.message.edit_text(
        "✏️ <b>Что хотите изменить?</b>\n\nВыберите поле для редактирования:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Имя", callback_data="admin_edit_name"),
             InlineKeyboardButton(text="📞 Телефон", callback_data="admin_edit_phone")],
            [InlineKeyboardButton(text="📧 Email", callback_data="admin_edit_email"),
             InlineKeyboardButton(text="🏢 Торг. сеть", callback_data="admin_edit_network")],
            [InlineKeyboardButton(text="🏪 Магазин", callback_data="admin_edit_shop"),
             InlineKeyboardButton(text="🏙️ Город", callback_data="admin_edit_city")],
            [InlineKeyboardButton(text="🕐 Часовой пояс", callback_data="admin_edit_timezone")],
            [back_button(f"admin_user_{telegram_id}")]
        ]),
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "admin_delete_user")
async def admin_delete_user_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение полного удаления пользователя — только для суперадмина"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Полное удаление доступно только суперадмину.", show_alert=True)
        return
    
    current_db = await get_db(callback.from_user.id, state)
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    
    if not telegram_id:
        await callback.answer("❌ Данные пользователя не найдены!", show_alert=True)
        return
    
    user = current_db.get_user(telegram_id)
    if not user:
        await callback.answer("❌ Пользователь не найден!", show_alert=True)
        return
    
    first_name, last_name = user[2], user[3]
    
    await callback.answer()
    await callback.message.edit_text(
        f"⚠️ Подтверждение удаления\n\n"
        f"Пользователь: {he(first_name)} {he(last_name)}\n"
        f"Telegram ID: {telegram_id}\n\n"
        f"❗ Вы действительно хотите удалить этого пользователя?\n"
        f"Это действие необратимо!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data="admin_edit_user_final"),
             InlineKeyboardButton(text="❌ Отмена", callback_data="admin_users")]
        ])
    )

@admin_router.callback_query(F.data == "admin_edit_user_final")
async def admin_delete_user_final(callback: CallbackQuery, state: FSMContext):
    """Окончательное удаление пользователя"""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path_to_delete = data.get('admin_delete_db_path')
    
    if not telegram_id:
        await callback.answer("❌ Данные пользователя не найдены в стейте!", show_alert=True)
        return
    
    # Определяем БД для удаления
    if db_path_to_delete and os.path.exists(db_path_to_delete):
        current_db = Database(db_path_to_delete)
    else:
        # Пытаемся найти пользователя заново, если путь в стейте пуст
        current_db = None
        # 1. shop_bot.db
        shop_db = Database('data/shop_bot.db')
        if shop_db.get_user(telegram_id):
            current_db = shop_db
        # 2. Tenants
        if not current_db:
            tenants_dir = 'data/tenants'
            if os.path.exists(tenants_dir):
                for f in os.listdir(tenants_dir):
                    if f.endswith('.db'):
                        p = os.path.join(tenants_dir, f)
                        t_db = Database(p)
                        if t_db.get_user(telegram_id):
                            current_db = t_db
                            break
        
    if not current_db:
        logging.error(f"FINAL DELETE ERROR: User {telegram_id} not found in any DB")
        await callback.answer("❌ Ошибка: пользователь не найден в базах данных", show_alert=True)
        return

    await callback.answer()
    try:
        user = current_db.get_user(telegram_id)
        user_name = f"{user[2]} {user[3]}" if user else "Неизвестный пользователь"
        
        if current_db.delete_user(telegram_id):
            # Чистим user_org_mapping и запись из main.db
            try:
                main_conn = sqlite3.connect('data/main.db')
                main_cursor = main_conn.cursor()
                main_cursor.execute("DELETE FROM user_org_mapping WHERE telegram_id = ?", (telegram_id,))
                if current_db.db_file != 'data/main.db':
                    main_cursor.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
                main_conn.commit()
                main_conn.close()
            except Exception as _e:
                logging.error(f"Ошибка очистки при удалении {telegram_id}: {_e}")
            await callback.message.edit_text(
                f"✅ Пользователь '{user_name}' успешно удален из {os.path.basename(current_db.db_file)}!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_users")]])
            )
        else:
            await callback.message.edit_text(
                f"❌ Ошибка при удалении пользователя '{user_name}'!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_users")]])
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Произошла ошибка: {str(e)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_users")]])
        )
    finally:
        await state.update_data(admin_edit_user_id=None, admin_delete_db_path=None)

@admin_router.callback_query(F.data.startswith("admin_edit_"))
async def admin_edit_user_field(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования поля пользователя администратором.
    Для магазина и торговой сети — выбор из существующих в БД организации."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    field_mapping = {
        "admin_edit_name": ("имя", AdminUserStates.waiting_for_new_name),
        "admin_edit_phone": ("телефон", AdminUserStates.waiting_for_new_phone),
        "admin_edit_email": ("email", AdminUserStates.waiting_for_new_email),
        "admin_edit_network": ("торговую сеть", AdminUserStates.waiting_for_new_network),
        "admin_edit_shop": ("магазин", AdminUserStates.waiting_for_new_shop),
        "admin_edit_city": ("город", AdminUserStates.waiting_for_new_city),
        "admin_edit_timezone": ("часовой пояс", AdminUserStates.editing_timezone)
    }

    field_info = field_mapping.get(callback.data)
    if not field_info:
        await callback.answer("❌ Неизвестное поле для редактирования", show_alert=True)
        return

    field_name, next_state = field_info
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)

    # Часовой пояс — через клавиатуру
    if callback.data == "admin_edit_timezone":
        from keyboards import timezone_keyboard
        await callback.message.edit_text(
            "🕐 Выберите новый часовой пояс для пользователя:",
            reply_markup=timezone_keyboard()
        )
        return

    back_kb = [[back_button("admin_edit_user")]]

    # Магазин — выпадающий список из БД организации
    if callback.data == "admin_edit_shop":
        current_db = await get_db(callback.from_user.id, state)
        shops = current_db.get_all_shops()
        if shops:
            shop_buttons = [
                [InlineKeyboardButton(text=f"🏪 {s}", callback_data=safe_cb("adm_shop_pick_", s))]
                for s in shops
            ]
            shop_buttons.append([InlineKeyboardButton(text="➕ Ввести новый магазин", callback_data="adm_shop_new")])
            shop_buttons += back_kb
            await callback.message.edit_text(
                "🏪 <b>Выберите магазин</b> из существующих в организации\nили введите новый:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=shop_buttons),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                "🏪 Магазинов в базе нет. Введите название нового магазина:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=back_kb),
            )
            await state.set_state(next_state)
        return

    # Торговая сеть — выпадающий список из БД организации
    if callback.data == "admin_edit_network":
        current_db = await get_db(callback.from_user.id, state)
        networks = current_db.get_all_trade_networks()
        if networks:
            net_buttons = [
                [InlineKeyboardButton(text=f"🏢 {n}", callback_data=safe_cb("adm_net_pick_", n))]
                for n in networks
            ]
            net_buttons.append([InlineKeyboardButton(text="➕ Ввести новую сеть", callback_data="adm_net_new")])
            net_buttons += back_kb
            await callback.message.edit_text(
                "🏢 <b>Выберите торговую сеть</b> из существующих в организации\nили введите новую:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=net_buttons),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                "🏢 Торговых сетей в базе нет. Введите название новой:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=back_kb),
            )
            await state.set_state(next_state)
        return

    # Остальные поля — свободный текст
    await callback.message.edit_text(
        f"✏️ Введите новое значение для поля: <b>{field_name}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=back_kb),
        parse_mode="HTML"
    )
    await state.set_state(next_state)


# ── Выбор магазина / торговой сети из списка при редактировании чужого профиля ─

@admin_router.callback_query(F.data.startswith("adm_shop_pick_"))
async def adm_shop_pick(callback: CallbackQuery, state: FSMContext):
    """Администратор выбрал существующий магазин для редактируемого пользователя."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    shop_raw = callback.data.removeprefix("adm_shop_pick_")
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    current_db = await get_db(callback.from_user.id, state)
    shop_name = resolve_cb_name(shop_raw, current_db.get_all_shops() or [])
    await callback.answer()
    _back = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    if not telegram_id or not db_path:
        await callback.message.edit_text("❌ Контекст редактирования потерян.", reply_markup=_back)
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE users SET shop_name = ? WHERE telegram_id = ?", (shop_name, telegram_id))
        conn.commit()
        conn.close()
        await callback.message.edit_text(
            f"✅ Магазин обновлён: <b>{he(shop_name)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}", reply_markup=_back)
    await clear_state_keep_org(state)

@admin_router.callback_query(F.data == "adm_shop_new")
async def adm_shop_new(callback: CallbackQuery, state: FSMContext):
    """Администратор хочет ввести новое название магазина вручную."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    await callback.answer()
    from subscription_utils import check_shop_limit
    from env_manager import env_manager as _em
    if not _em.is_super_admin(callback.from_user.id):
        ok, msg = check_shop_limit(callback.from_user.id)
        if not ok:
            await callback.message.edit_text(
                f"🚫 <b>Лимит магазинов исчерпан</b>\n\n{msg}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Подписка", callback_data="subscription_menu")],
                    [back_button("admin_edit_user")],
                ]),
                parse_mode="HTML",
            )
            return
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🏪 Введите название нового магазина (2–30 символов):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]]),
    )
    await state.set_state(AdminUserStates.waiting_for_new_shop)

@admin_router.callback_query(F.data.startswith("adm_net_pick_"))
async def adm_net_pick(callback: CallbackQuery, state: FSMContext):
    """Администратор выбрал существующую торговую сеть для редактируемого пользователя."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    net_raw = callback.data.removeprefix("adm_net_pick_")
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    current_db = await get_db(callback.from_user.id, state)
    net_name = resolve_cb_name(net_raw, current_db.get_all_trade_networks() or [])
    await callback.answer()
    _back = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    if not telegram_id or not db_path:
        await callback.message.edit_text("❌ Контекст редактирования потерян.", reply_markup=_back)
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE users SET trade_network = ? WHERE telegram_id = ?", (net_name, telegram_id))
        conn.commit()
        conn.close()
        await callback.message.edit_text(
            f"✅ Торговая сеть обновлена: <b>{he(net_name)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}", reply_markup=_back)
    await clear_state_keep_org(state)

@admin_router.callback_query(F.data == "adm_net_new")
async def adm_net_new(callback: CallbackQuery, state: FSMContext):
    """Администратор хочет ввести новое название торговой сети вручную."""
    if not is_any_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(anchor_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        "🏢 Введите название новой торговой сети (2–30 символов):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]]),
    )
    await state.set_state(AdminUserStates.waiting_for_new_network)

@admin_router.message(AdminUserStates.waiting_for_new_name)
async def process_admin_edit_name(message: Message, state: FSMContext):
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    
    if not telegram_id or not db_path:
        await fsm_edit(state, message, "❌ Ошибка: контекст пользователя потерян.", reply_markup=_edit_kb)
        await clear_state_keep_org(state)
        return

    new_name = message.text.strip()
    parts = new_name.split(maxsplit=1)
    f_name = parts[0]
    l_name = parts[1] if len(parts) > 1 else ""

    if len(f_name) < 2 or len(f_name) > 50:
        await fsm_edit(state, message, "⚠️ Имя: от 2 до 50 символов. Введите снова:",
                       reply_markup=_edit_kb)
        return
    if l_name and len(l_name) > 50:
        await fsm_edit(state, message, "⚠️ Фамилия: до 50 символов. Введите снова:",
                       reply_markup=_edit_kb)
        return

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET first_name = ?, last_name = ? WHERE telegram_id = ?", (f_name, l_name, telegram_id))
        conn.commit()
        conn.close()
        await fsm_edit(state, message, f"✅ Имя пользователя обновлено на: {new_name}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]))
    except Exception as e:
        await fsm_edit(state, message, f"❌ Ошибка обновления: {e}", reply_markup=_edit_kb)
    await clear_state_keep_org(state)

@admin_router.message(AdminUserStates.waiting_for_new_phone)
async def process_admin_edit_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    new_value = message.text.strip()
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET phone = ? WHERE telegram_id = ?", (new_value, telegram_id))
        conn.commit()
        conn.close()
        await fsm_edit(state, message, f"✅ Телефон обновлен: {new_value}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]))
    except Exception as e:
        await fsm_edit(state, message, f"❌ Ошибка: {e}", reply_markup=_edit_kb)
    await clear_state_keep_org(state)

@admin_router.message(AdminUserStates.waiting_for_new_email)
async def process_admin_edit_email(message: Message, state: FSMContext):
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    new_value = message.text.strip()
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET email = ? WHERE telegram_id = ?", (new_value, telegram_id))
        conn.commit()
        conn.close()
        await fsm_edit(state, message, f"✅ Email обновлен: {new_value}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]))
    except Exception as e:
        await fsm_edit(state, message, f"❌ Ошибка: {e}", reply_markup=_edit_kb)
    await clear_state_keep_org(state)

@admin_router.message(AdminUserStates.waiting_for_new_network)
async def process_admin_edit_network(message: Message, state: FSMContext):
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    new_value = message.text.strip()
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    if len(new_value) < 2 or len(new_value) > 30:
        await fsm_edit(state, message, "⚠️ Название торговой сети: от 2 до 30 символов. Введите снова:",
                       reply_markup=_edit_kb)
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET trade_network = ? WHERE telegram_id = ?", (new_value, telegram_id))
        conn.commit()
        conn.close()
        await fsm_edit(state, message, f"✅ Торговая сеть обновлена: {new_value}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]))
    except Exception as e:
        await fsm_edit(state, message, f"❌ Ошибка: {e}", reply_markup=_edit_kb)
    await clear_state_keep_org(state)

@admin_router.message(AdminUserStates.waiting_for_new_shop)
async def process_admin_edit_shop(message: Message, state: FSMContext):
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    new_value = message.text.strip()
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    if len(new_value) < 2 or len(new_value) > 30:
        await fsm_edit(state, message, "⚠️ Название магазина: от 2 до 30 символов. Введите снова:",
                       reply_markup=_edit_kb)
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET shop_name = ? WHERE telegram_id = ?", (new_value, telegram_id))
        conn.commit()
        conn.close()
        await fsm_edit(state, message, f"✅ Магазин обновлен: {new_value}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]))
    except Exception as e:
        await fsm_edit(state, message, f"❌ Ошибка: {e}", reply_markup=_edit_kb)
    await clear_state_keep_org(state)

@admin_router.message(AdminUserStates.waiting_for_new_city)
async def process_admin_edit_city(message: Message, state: FSMContext):
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    new_value = message.text.strip()
    _edit_kb = InlineKeyboardMarkup(inline_keyboard=[[back_button("admin_edit_user")]])
    if len(new_value) < 2 or len(new_value) > 30:
        await fsm_edit(state, message, "⚠️ Название города: от 2 до 30 символов. Введите снова:",
                       reply_markup=_edit_kb)
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET city = ? WHERE telegram_id = ?", (new_value, telegram_id))
        conn.commit()
        conn.close()
        await fsm_edit(state, message, f"✅ Город обновлен: {new_value}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]))
    except Exception as e:
        await fsm_edit(state, message, f"❌ Ошибка: {e}", reply_markup=_edit_kb)
    await clear_state_keep_org(state)

@admin_router.callback_query(F.data.startswith("set_timezone_"))
async def process_admin_edit_timezone(callback: CallbackQuery, state: FSMContext):
    # Проверяем, находимся ли мы в режиме редактирования пользователя администратором
    data = await state.get_data()
    telegram_id = data.get('admin_edit_user_id')
    db_path = data.get('admin_delete_db_path')
    
    if not telegram_id or not db_path:
        await callback.answer()
        return

    new_tz = callback.data.replace("set_timezone_", "")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET timezone = ? WHERE telegram_id = ?", (new_tz, telegram_id))
        conn.commit()
        conn.close()
        await callback.answer()
        await callback.message.edit_text(f"✅ Часовой пояс обновлен на: {new_tz}", 
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(f"admin_user_{telegram_id}")]]))
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
    await clear_state_keep_org(state)
