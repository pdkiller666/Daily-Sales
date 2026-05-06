"""
Обработчики для управления резервными копиями базы данных
"""
import re
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from backup_manager import BackupManager
from keyboards import back_button, safe_cb, resolve_cb_name
from env_manager import env_manager
from db_utils import clear_state_keep_org

backup_router = Router()
backup_manager = BackupManager()


class BackupStates(StatesGroup):
    waiting_for_restore_confirmation = State()


def _backup_label(filename):
    """Извлекает читаемый label из имени бэкап-файла.

    backup_{label}_{YYYY-MM-DD_HH-MM-SS}.db → label
    """
    m = re.match(r'^backup_(.+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.db$', filename)
    return m.group(1) if m else filename


@backup_router.callback_query(F.data == "backup_management")
async def backup_management_menu(callback: CallbackQuery):
    """Главное меню управления резервными копиями"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    backups = backup_manager.get_backup_list()

    text = "💾 <b>Управление резервными копиями</b>\n\n"
    text += f"📊 Всего копий: {len(backups)}\n"

    if backups:
        latest = backups[0]
        text += f"🕐 Последняя копия: {latest['created'].strftime('%d.%m.%Y %H:%M')}\n"
        text += f"💽 Размер: {latest['size_mb']} MB\n"
        text += f"📅 Возраст: {latest['age_days']} дней\n"
    else:
        text += "⚠️ Резервные копии отсутствуют\n"

    text += "\n<i>Автоматическое создание: ежедневно в 03:00\nАвтоочистка: старше 30 дней</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Создать копию", callback_data="create_backup")],
        [InlineKeyboardButton(text="📋 Список копий", callback_data="list_backups")],
        [InlineKeyboardButton(text="🗑 Очистить старые", callback_data="cleanup_backups")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="backup_settings")],
        [back_button("system_admin_panel")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@backup_router.callback_query(F.data == "create_backup")
async def create_backup_manual(callback: CallbackQuery):
    """Создание резервной копии всех БД вручную"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "💾 Создание резервных копий всех баз данных...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("backup_management")]])
    )

    results = backup_manager.backup_all_tenants()

    text = "📊 <b>Результат создания копий:</b>\n\n"
    text += "\n".join(results)

    cleanup_success, cleanup_message = backup_manager.cleanup_old_backups()
    text += f"\n\n🗑 <b>Очистка:</b> {cleanup_message}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список копий", callback_data="list_backups")],
        [back_button("backup_management")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@backup_router.callback_query(F.data == "list_backups")
async def list_backups(callback: CallbackQuery):
    """Список всех резервных копий"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    backups = backup_manager.get_backup_list()

    if not backups:
        text = "📋 <b>Список резервных копий</b>\n\n❌ Резервные копии отсутствуют"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Создать копию", callback_data="create_backup")],
            [back_button("backup_management")]
        ])
    else:
        text = f"📋 <b>Список резервных копий ({len(backups)})</b>\n\n"

        builder = InlineKeyboardBuilder()

        for backup in backups[:15]:
            created_str = backup['created'].strftime('%d.%m %H:%M')
            label = _backup_label(backup['filename'])

            text += f"📁 <code>{backup['filename']}</code>\n"
            text += f"   🏷 {label} | 📅 {created_str} | {backup['size_mb']} MB\n\n"

            builder.add(InlineKeyboardButton(
                text=f"{label[:20]} {created_str}",
                callback_data=safe_cb("bi:", backup['filename'])
            ))

        builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="backup_management"))
        builder.adjust(2, 1)
        keyboard = builder.as_markup()

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@backup_router.callback_query(F.data.startswith("bi:"))
async def backup_info(callback: CallbackQuery):
    """Детальная информация о резервной копии"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    partial = callback.data[3:]
    backups = backup_manager.get_backup_list()
    backup_filename = resolve_cb_name(partial, [b['filename'] for b in backups])

    info = backup_manager.get_backup_info(backup_filename)

    if not info:
        await callback.answer("❌ Информация о копии недоступна", show_alert=True)
        return

    await callback.answer()
    text = "📄 <b>Информация о копии</b>\n\n"
    text += f"📁 <code>{info['filename']}</code>\n"
    text += f"🏷 Label: {_backup_label(info['filename'])}\n"
    text += f"📅 Создана: {info['created'].strftime('%d.%m.%Y %H:%M:%S')}\n"
    text += f"💽 Размер: {info['size_mb']} MB\n"

    if 'integrity' in info:
        integrity_emoji = "✅" if info['integrity'] else "❌"
        text += f"🔍 Целостность: {integrity_emoji}\n"

        if info['integrity']:
            text += f"📊 Таблиц: {info['tables_count']}\n"
            text += f"📈 Записей: {info['total_records']}\n"
        else:
            text += f"⚠️ Ошибка: {info.get('error', 'Неизвестная ошибка')}\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Восстановить", callback_data=safe_cb("rb:", backup_filename))],
        [InlineKeyboardButton(text="📋 К списку", callback_data="list_backups")],
        [back_button("backup_management")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@backup_router.callback_query(F.data.startswith("rb:"))
async def restore_backup_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение восстановления из резервной копии"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    partial = callback.data[3:]
    backups = backup_manager.get_backup_list()
    backup_filename = resolve_cb_name(partial, [b['filename'] for b in backups])

    await state.update_data(backup_filename=backup_filename)

    text = "⚠️ <b>Восстановление базы данных</b>\n\n"
    text += f"Вы собираетесь восстановить:\n"
    text += f"📁 <code>{backup_filename}</code>\n"
    text += f"🏷 Label: {_backup_label(backup_filename)}\n\n"
    text += "❗ <b>ВНИМАНИЕ:</b>\n"
    text += "• Соответствующая БД будет заменена\n"
    text += "• Изменения после создания копии будут потеряны\n"
    text += "• Перед восстановлением будет создана копия текущей БД\n"
    text += "• Бот потребует перезапуска после восстановления\n\n"
    text += "Подтвердите восстановление:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, восстановить", callback_data="confirm_restore")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=safe_cb("bi:", backup_filename))]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(BackupStates.waiting_for_restore_confirmation)


@backup_router.callback_query(F.data == "confirm_restore")
async def restore_backup_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнение восстановления из резервной копии"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    data = await state.get_data()
    backup_filename = data.get('backup_filename')

    if not backup_filename:
        await callback.message.edit_text(
            "❌ Ошибка: данные о копии потеряны",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("backup_management")]])
        )
        await clear_state_keep_org(state)
        return

    await callback.message.edit_text(
        "🔄 Восстановление базы данных...\n\nПожалуйста, ждите..."
    )

    success, message = backup_manager.restore_backup(backup_filename)

    status_emoji = "✅" if success else "❌"
    text = f"{status_emoji} <b>Результат восстановления</b>\n\n{message}"

    if success:
        text += "\n\n🔄 <b>Перезапуск бота...</b>\nПодождите 5-10 секунд"

        await callback.message.edit_text(text, parse_mode="HTML")

        user_chat_id = callback.from_user.id
        await clear_state_keep_org(state)

        import asyncio
        from restart_manager import restart_manager
        restart_manager.post_restart_chat_id = user_chat_id
        asyncio.create_task(restart_manager.schedule_restart("Database restore"))
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список копий", callback_data="list_backups")],
            [back_button("backup_management")]
        ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        await clear_state_keep_org(state)


@backup_router.callback_query(F.data == "cleanup_backups")
async def cleanup_old_backups(callback: CallbackQuery):
    """Очистка старых резервных копий"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "🗑 Очистка старых копий...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("backup_management")]])
    )

    success, message = backup_manager.cleanup_old_backups(30)

    status_emoji = "✅" if success else "❌"
    text = f"{status_emoji} <b>Результат очистки</b>\n\n{message}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список копий", callback_data="list_backups")],
        [back_button("backup_management")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@backup_router.callback_query(F.data == "backup_settings")
async def backup_settings(callback: CallbackQuery):
    """Настройки резервного копирования"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    text = "⚙️ <b>Настройки резервного копирования</b>\n\n"
    text += "📅 <b>Автоматическое создание:</b> Ежедневно в 03:00\n"
    text += "🗑 <b>Автоочистка:</b> Копии старше 30 дней\n"
    text += "📁 <b>Расположение:</b> data/backup/\n"
    text += "💾 <b>Формат:</b> SQLite (.db)\n"
    text += "🔄 <b>Метод:</b> SQLite BACKUP API\n"
    text += "🗂 <b>БД в бэкапе:</b> main.db + shop_bot.db + все тенанты\n\n"
    text += "<i>Настройки фиксированы для обеспечения стабильности</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Тест системы", callback_data="test_backup_system")],
        [back_button("backup_management")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@backup_router.callback_query(F.data == "test_backup_system")
async def test_backup_system(callback: CallbackQuery):
    """Тестирование системы резервного копирования"""
    if not env_manager.is_super_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "🧪 Тестирование системы резервного копирования...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button("backup_settings")]])
    )

    test_results = []
    failures = 0

    # Тест 1: Создание копий всех БД
    results = backup_manager.backup_all_tenants()
    test_results.append("📦 Создание копий всех БД:")
    for res in results:
        test_results.append(f"   {res}")
        if '❌' in res:
            failures += 1

    # Тест 2: Получение списка
    backups = backup_manager.get_backup_list()
    list_ok = len(backups) > 0
    if not list_ok:
        failures += 1
    test_results.append(f"📋 Получение списка: {'✅' if list_ok else '❌'} (всего {len(backups)})")

    # Тест 3: Проверка целостности последней копии
    if backups:
        latest_backup = backups[0]
        info = backup_manager.get_backup_info(latest_backup['filename'])
        integrity_ok = info and info.get('integrity', False)
        if not integrity_ok:
            failures += 1
        test_results.append(f"🔍 Целостность последней копии: {'✅' if integrity_ok else '❌'}")

    # Тест 4: Старые копии
    old_count = len([b for b in backups if b['age_days'] > 30])
    test_results.append(f"🗑 Копий для очистки (>30 дней): {old_count}")

    overall = "✅ Все проверки пройдены" if failures == 0 else f"⚠️ Обнаружено проблем: {failures}"

    text = "🧪 <b>Результаты тестирования</b>\n\n"
    text += "\n".join(test_results)
    text += f"\n\n{overall}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [back_button("backup_settings")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
