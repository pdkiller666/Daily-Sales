"""
Главный файл Telegram бота для управления товарами и продажами
"""
import os
import sys
import asyncio
import logging
import pytz
from aiogram import Bot, Dispatcher
from sqlite_storage import SQLiteStorage
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import Database
from notif_utils import add_read_btn
from handlers import router as main_router
from products_handlers import products_router
from sales_handlers import sales_router
from reports_handlers import reports_router
from admin_handlers import admin_router
from inventory_handlers import inventory_router
from contacts_handlers import contacts_router
from subscription_router import subscription_router
from notifications_handlers import notifications_router
from payment_system_admin import payment_system_router
from payment_admin_handlers import payment_admin_router
from backup_handlers import backup_router
from backup_manager import BackupManager
from restart_manager import restart_manager
from commission_handlers import commission_router
from earnings_handlers import earnings_router
from sales_plans_handlers import sales_plans_router
from salary_handlers import salary_router
from contests_handlers import contests_router
from dashboard_handlers import router as dashboard_router
from filter_handlers import filter_router
from integration_handlers import integration_router
import scheduler_module
from utils import he

# Загружаем переменные окружения
load_dotenv('data/.env', override=False)  # os.environ имеет приоритет (Amvera/Docker)

# Получаем токен бота и ID админа
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0').split(',')[0].strip() or 0)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Создаем бота и диспетчер
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=SQLiteStorage("data/fsm_storage.db"))

from aiogram import F as _F
from aiogram.types import CallbackQuery as _CQ, ErrorEvent

@dp.callback_query(_F.data == "pg_noop")
async def pg_noop_handler(callback: _CQ) -> None:
    """Кнопка пагинации «текущая страница» — ничего не делать."""
    await callback.answer()

@dp.errors()
async def global_error_handler(event: ErrorEvent) -> bool:
    """Глобальный перехватчик ошибок — логирует исключение и отвечает пользователю."""
    error = event.exception
    update = event.update

    # Устаревший callback (>10 мин) — нормальное поведение, не засоряем лог
    if isinstance(error, TelegramBadRequest) and "query is too old" in str(error):
        logging.debug(f"Игнорируем устаревший callback: {error}")
        return True

    # Сообщение не изменилось — нормальное поведение при повторном нажатии
    if isinstance(error, TelegramBadRequest) and "message is not modified" in str(error).lower():
        try:
            if update.callback_query:
                await update.callback_query.answer()
        except Exception:
            pass
        return True

    logging.error(f"Необработанное исключение: {type(error).__name__}: {error}", exc_info=True)
    try:
        if update.callback_query:
            try:
                await update.callback_query.answer(
                    "⚠️ Произошла ошибка. Попробуйте ещё раз или вернитесь в главное меню.",
                    show_alert=True,
                )
            except Exception:
                pass
        elif update.message:
            try:
                await update.message.answer(
                    "⚠️ Произошла ошибка. Попробуйте ещё раз или нажмите /menu"
                )
            except Exception:
                pass
    except Exception:
        pass
    return True

# Регистрируем роутеры
dp.include_router(main_router)
dp.include_router(products_router)
dp.include_router(sales_router)
dp.include_router(reports_router)
dp.include_router(admin_router)
dp.include_router(inventory_router)
dp.include_router(contacts_router)
dp.include_router(subscription_router)
dp.include_router(notifications_router)
dp.include_router(payment_system_router)
dp.include_router(payment_admin_router)
dp.include_router(backup_router)
dp.include_router(commission_router)
dp.include_router(earnings_router)
dp.include_router(sales_plans_router)
dp.include_router(salary_router)
dp.include_router(contests_router)
dp.include_router(dashboard_router)
dp.include_router(filter_router)
dp.include_router(integration_router)

# Создаем папку data если не существует
if not os.path.exists('data'):
    os.makedirs('data')

from database import Database
from tenant_manager import tenant_manager

async def get_db_for_user(telegram_id):
    """Фабрика для получения экземпляра БД конкретного пользователя"""
    db_path = tenant_manager.get_user_db_path(telegram_id)
    return Database(db_path)

# Инициализация основной базы данных
db = Database('data/main.db')
db.create_tables()

# Инициализация shop_bot.db
shop_db = Database('data/shop_bot.db')
shop_db.create_tables()

# Синхронизация платёжных реквизитов из .env в БД
def _sync_payment_settings():
    card = os.getenv('PAYMENT_CARD_NUMBER', '').strip()
    name = os.getenv('PAYMENT_RECIPIENT_NAME', '').strip()
    bank = os.getenv('PAYMENT_BANK_NAME', '').strip()
    if card or name or bank:
        import sqlite3
        conn = sqlite3.connect('data/shop_bot.db')
        cursor = conn.cursor()
        if card:
            cursor.execute("INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES ('card_number', ?, CURRENT_TIMESTAMP)", (card,))
        if name:
            cursor.execute("INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES ('recipient_name', ?, CURRENT_TIMESTAMP)", (name,))
        if bank:
            cursor.execute("INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES ('bank_name', ?, CURRENT_TIMESTAMP)", (bank,))
        conn.commit()
        conn.close()

_sync_payment_settings()

# --- DB-paths cache for scheduler functions (TTL = 5 min) --- #
import time as _time
_db_paths_cache: list = []
_db_paths_ts: float = 0.0
_DB_PATHS_TTL = 300.0  # seconds


def _get_scheduler_db_paths() -> list:
    """Return cached list of all tenant DB paths, refresh every 5 min."""
    global _db_paths_cache, _db_paths_ts
    now = _time.monotonic()
    if now - _db_paths_ts < _DB_PATHS_TTL and _db_paths_cache:
        return _db_paths_cache
    paths = ['data/shop_bot.db']
    if os.path.exists('data/tenants'):
        for f in os.listdir('data/tenants'):
            if f.endswith('.db'):
                paths.append(os.path.join('data/tenants', f))
    _db_paths_cache = paths
    _db_paths_ts = now
    return paths


_TRIAL_FEATURES_LOST = (
    "• 📊 Экспорт отчётов в Excel\n"
    "• 📈 Расширенная аналитика и рейтинги\n"
    "• 🔔 Push-уведомления\n"
    "• 📗 Интеграция с Google Таблицами\n"
    "• Без ограничений: товары, магазины, продажи"
)

def _sub_markup():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return add_read_btn(InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="subscription_plans")
    ]]))


async def send_trial_expired_upsell(bot: Bot):
    """Upsell-пуш при истечении пробного периода. Запускается ежечасно."""
    EXPIRED_THRESHOLD = -1  # специальный ключ дедупликации для «триал истёк»
    try:
        from aiogram.utils.text_decorations import html_decoration as hd
        shop_bot_db = Database('data/shop_bot.db')
        expired = await asyncio.to_thread(shop_bot_db.get_recently_expired_trials)

        for telegram_id, first_name, plan_type, end_date, shop_user_id in expired:
            await asyncio.sleep(0)
            if not telegram_id:
                continue
            if await asyncio.to_thread(shop_bot_db.has_sent_reminder, shop_user_id, EXPIRED_THRESHOLD, end_date):
                continue

            name = hd.quote(first_name) if first_name else "Пользователь"
            upsell = (
                f"🎁 <b>{name}, ваш пробный период завершён!</b>\n\n"
                "Спасибо, что протестировали все возможности бота. "
                "Аккаунт переведён на <b>бесплатный тариф</b>.\n\n"
                "❌ <b>На бесплатном тарифе недоступно:</b>\n"
                f"{_TRIAL_FEATURES_LOST}\n\n"
                "💎 Оформите подписку — и ничего не потеряете!"
            )
            try:
                await bot.send_message(
                    telegram_id, upsell,
                    parse_mode="HTML",
                    reply_markup=_sub_markup(),
                )
                await asyncio.to_thread(shop_bot_db.mark_reminder_sent, shop_user_id, EXPIRED_THRESHOLD, end_date)
                logging.info(f"Trial expired upsell sent → {telegram_id}")
            except Exception as send_err:
                logging.error(f"send_trial_expired_upsell → {telegram_id}: {send_err}")
    except Exception as e:
        logging.error(f"send_trial_expired_upsell: {e}")


async def send_payment_alerts(bot: Bot):
    """Отправка напоминаний об истечении подписки: за 14, 7, 3 и 1 день.
    Каждое напоминание отправляется ровно один раз (дедупликация через subscription_reminder_log).
    Для пробных подписок — специальные сообщения с перечнем потерь."""
    THRESHOLDS = [14, 7, 3, 1]  # дни до истечения — в порядке убывания
    try:
        from datetime import datetime
        import pytz

        shop_bot_db = Database('data/shop_bot.db')
        db_paths = _get_scheduler_db_paths()
        current_utc = datetime.now(pytz.UTC)

        for path in db_paths:
            await asyncio.sleep(0)  # уступаем event loop между DB-файлами
            current_db = Database(path)
            try:
                users = await asyncio.to_thread(current_db.get_users_for_notifications, 'payment')

                for user_data in users:
                    await asyncio.sleep(0)  # уступаем event loop между пользователями
                    user_id, telegram_id, first_name, shop_name, threshold, notification_time_str = user_data

                    if not telegram_id or not notification_time_str:
                        continue

                    user_timezone = await asyncio.to_thread(current_db.get_user_timezone, telegram_id)
                    try:
                        user_tz = pytz.timezone(user_timezone)
                        user_now = current_utc.astimezone(user_tz)
                        if user_now.strftime('%H:%M') != notification_time_str:
                            continue
                    except Exception:
                        continue

                    shop_bot_user = await asyncio.to_thread(shop_bot_db.get_user, telegram_id)
                    if not shop_bot_user:
                        continue
                    subscription = await asyncio.to_thread(shop_bot_db.get_user_subscription, shop_bot_user[0])
                    if not subscription:
                        continue

                    plan_type = subscription[2]
                    end_date = subscription[4]
                    is_trial = bool(subscription[5]) if len(subscription) > 5 else False
                    shop_user_id = shop_bot_user[0]

                    # Бессрочные подписки (Бесплатный) не напоминаем
                    if end_date.startswith('9999'):
                        continue

                    try:
                        end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                        days_remaining = (end_dt - current_utc).days
                    except Exception:
                        continue

                    # Для каждого порога — отправляем первый неотправленный
                    for t in THRESHOLDS:
                        if days_remaining <= t:
                            if await asyncio.to_thread(shop_bot_db.has_sent_reminder, shop_user_id, t, end_date):
                                continue  # этот порог уже отправлен, проверяем следующий

                            # ── Формируем сообщение ──────────────────────────────────
                            if is_trial:
                                # Пробный период — специальные сообщения с upsell
                                if days_remaining <= 0:
                                    reminder = (
                                        "🚨 <b>Пробный период завершается!</b>\n\n"
                                        f"📅 Окончание: {end_dt.strftime('%d.%m.%Y')}\n\n"
                                        "После окончания будет недоступно:\n"
                                        f"{_TRIAL_FEATURES_LOST}\n\n"
                                        "💎 Оформите подписку прямо сейчас!"
                                    )
                                elif days_remaining == 1:
                                    reminder = (
                                        "⚠️ <b>Пробный период заканчивается завтра!</b>\n\n"
                                        f"📅 Окончание: {end_dt.strftime('%d.%m.%Y')}\n\n"
                                        "С этого момента будет недоступно:\n"
                                        f"{_TRIAL_FEATURES_LOST}\n\n"
                                        "💎 Успейте оформить подписку!"
                                    )
                                elif days_remaining <= 3:
                                    reminder = (
                                        f"⏰ <b>Пробный период заканчивается через {days_remaining} дн.</b>\n\n"
                                        f"📅 Окончание: {end_dt.strftime('%d.%m.%Y')}\n\n"
                                        "Оцените, что останется недоступным:\n"
                                        f"{_TRIAL_FEATURES_LOST}\n\n"
                                        "💎 Выберите тариф и продолжайте без ограничений!"
                                    )
                                else:
                                    reminder = (
                                        f"⏰ <b>Пробный период заканчивается через {days_remaining} дн.</b>\n\n"
                                        f"📅 Окончание: {end_dt.strftime('%d.%m.%Y')}\n\n"
                                        "Используйте оставшееся время по максимуму — "
                                        "все премиум-функции доступны прямо сейчас."
                                    )
                                markup = _sub_markup()
                            else:
                                # Обычная платная подписка — стандартное напоминание
                                if days_remaining <= 0:
                                    urgency, days_text = "🚨", "уже истекла!"
                                elif days_remaining == 1:
                                    urgency, days_text = "⚠️", "истекает <b>завтра</b>!"
                                else:
                                    urgency, days_text = "⏰", f"истекает через <b>{days_remaining} дн.</b>"
                                reminder = (
                                    f"💰 <b>Уведомление о подписке</b>\n\n"
                                    f"{urgency} Ваша подписка <b>{plan_type}</b> {days_text}\n"
                                    f"📅 Дата окончания: {end_dt.strftime('%d.%m.%Y')}\n\n"
                                    "💡 Пожалуйста, продлите подписку вовремя."
                                )
                                markup = add_read_btn()

                            try:
                                await bot.send_message(telegram_id, reminder, parse_mode="HTML", reply_markup=markup)
                                await asyncio.to_thread(shop_bot_db.mark_reminder_sent, shop_user_id, t, end_date)
                                await asyncio.to_thread(current_db.add_notification_to_history, user_id, 'payment', reminder)
                            except Exception as send_err:
                                logging.error(f"Ошибка отправки напоминания {telegram_id}: {send_err}")
                            break  # отправляем только самый срочный непосланный порог
            except Exception as e:
                logging.error(f"Ошибка при обработке БД {path}: {e}")
    except Exception as e:
        logging.error(f"Ошибка в send_payment_alerts: {e}")

async def send_sales_alerts(bot: Bot):
    """Отправка уведомлений о новых продажах по всем организациям"""
    try:
        from datetime import datetime, timedelta
        import pytz

        db_paths = _get_scheduler_db_paths()
        current_utc = datetime.now(pytz.UTC)

        for path in db_paths:
            await asyncio.sleep(0)
            current_db = Database(path)
            try:
                users = await asyncio.to_thread(current_db.get_users_for_notifications, 'sales')
                for user_data in users:
                    await asyncio.sleep(0)
                    user_id, telegram_id, first_name, shop_name, threshold, notification_time_str = user_data
                    if not telegram_id or not notification_time_str: continue

                    user_timezone = await asyncio.to_thread(current_db.get_user_timezone, telegram_id)
                    try:
                        user_tz = pytz.timezone(user_timezone)
                        if current_utc.astimezone(user_tz).strftime('%H:%M') != notification_time_str: continue
                    except Exception: continue

                    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
                    today = datetime.now().isoformat()
                    recent_sales = await asyncio.to_thread(current_db.get_user_sales_by_date, user_id, yesterday, today)

                    if recent_sales:
                        message = f"🛍️ <b>Уведомление о новых продажах</b>\n\n"
                        if shop_name: message += f"🏪 Магазин: {he(shop_name)}\n\n"
                        message += f"📈 За последние 24 часа:\n\n"

                        for sale in recent_sales[:5]:
                            product_name = sale[7] if len(sale) > 7 else "Неизвестный товар"
                            total_amount = sale[3] * (sale[4] or 0)
                            message += f"• {he(product_name)}: {sale[3]} шт. ({total_amount:,.2f} ₽)\n"

                        await bot.send_message(telegram_id, message, parse_mode="HTML", reply_markup=add_read_btn())
                        await asyncio.to_thread(current_db.add_notification_to_history, user_id, 'sales', message)
            except Exception: continue
    except Exception as e:
        logging.error(f"Error in send_sales_alerts: {e}")

async def send_personalized_notifications(bot: Bot):
    """Отправка уведомлений о низких остатках по всем организациям"""
    try:
        from datetime import datetime
        import pytz

        db_paths = _get_scheduler_db_paths()
        current_utc = datetime.now(pytz.UTC)

        for path in db_paths:
            await asyncio.sleep(0)
            current_db = Database(path)
            try:
                users = await asyncio.to_thread(current_db.get_users_for_notifications, 'low_stock')
                for user_data in users:
                    await asyncio.sleep(0)
                    user_id, telegram_id, first_name, shop_name, threshold, notification_time_str = user_data
                    if not telegram_id or not notification_time_str: continue

                    user_timezone = await asyncio.to_thread(current_db.get_user_timezone, telegram_id)
                    try:
                        user_tz = pytz.timezone(user_timezone)
                        if current_utc.astimezone(user_tz).strftime('%H:%M') != notification_time_str: continue
                    except Exception: continue

                    low_stock_items = await asyncio.to_thread(current_db.get_low_stock_items_for_user, user_id, shop_name, threshold or 5)
                    if low_stock_items:
                        message = f"📦 <b>Уведомление о низких остатках</b>\n\n"
                        if shop_name: message += f"🏪 Магазин: {he(shop_name)}\n\n"
                        for item in low_stock_items[:10]:
                            message += f"⚠️ <b>{he(item[0])}</b>: {item[1]} шт.\n"
                        
                        await bot.send_message(telegram_id, message, parse_mode="HTML", reply_markup=add_read_btn())
                        await asyncio.to_thread(current_db.add_notification_to_history, user_id, 'low_stock', message)
            except Exception: continue
    except Exception as e:
        logging.error(f"Error in send_personalized_notifications: {e}")

async def send_daily_reports(bot: Bot):
    """Отправка ежедневных отчетов по всем организациям"""
    try:
        from datetime import datetime, timedelta
        import pytz
        from db_utils import is_any_admin
        from dashboard_handlers import build_admin_daily_text, build_user_daily_text

        db_paths = _get_scheduler_db_paths()
        current_utc = datetime.now(pytz.UTC)
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

        for path in db_paths:
            await asyncio.sleep(0)
            current_db = Database(path)
            try:
                users = await asyncio.to_thread(current_db.get_users_for_notifications, 'daily_report')
                for user_data in users:
                    await asyncio.sleep(0)
                    user_id, telegram_id, first_name, shop_name, threshold, notification_time_str = user_data
                    if not telegram_id or not notification_time_str:
                        continue

                    user_timezone = await asyncio.to_thread(current_db.get_user_timezone, telegram_id)
                    try:
                        user_tz = pytz.timezone(user_timezone)
                        if current_utc.astimezone(user_tz).strftime('%H:%M') != notification_time_str:
                            continue
                    except Exception:
                        continue

                    try:
                        if is_any_admin(telegram_id):
                            from db_utils import get_user_org_scope
                            _sct, _scv = get_user_org_scope(telegram_id)
                            message = await asyncio.to_thread(
                                build_admin_daily_text,
                                current_db, yesterday, shop_name,
                                scope_type=_sct, scope_values=_scv
                            )
                        else:
                            message = await asyncio.to_thread(
                                build_user_daily_text,
                                current_db, user_id, telegram_id, yesterday, shop_name
                            )
                    except Exception as e:
                        logging.error(f"send_daily_reports: ошибка формирования текста для {telegram_id}: {e}")
                        sales = await asyncio.to_thread(current_db.get_user_sales_by_date, user_id, yesterday, yesterday)
                        message = f"📊 <b>Ежедневный отчёт за {yesterday}</b>\n\n"
                        if shop_name:
                            message += f"🏪 Магазин: {he(shop_name)}\n\n"
                        if sales:
                            total_revenue = sum(s[4] for s in sales if s[4])
                            message += f"📦 Продано: {sum(s[3] for s in sales)} шт.\n💰 Выручка: {total_revenue:,.2f} ₽"
                        else:
                            message += "ℹ️ Продаж не было."

                    await bot.send_message(telegram_id, message, parse_mode="HTML", reply_markup=add_read_btn())
                    await asyncio.to_thread(current_db.add_notification_to_history, user_id, 'daily_report', message)
            except Exception:
                continue
    except Exception as e:
        logging.error(f"Error in send_daily_reports: {e}")

async def check_scheduled_notifications(bot: Bot):
    """Отправка запланированных уведомлений, время которых наступило"""
    try:
        from datetime import datetime
        import pytz
        db_paths = _get_scheduler_db_paths()
        now_utc = datetime.now(pytz.UTC)

        for path in db_paths:
            await asyncio.sleep(0)
            if not os.path.exists(path):
                continue
            current_db = Database(path)
            try:
                pending = await asyncio.to_thread(current_db.get_scheduled_notifications, status='pending')
                for notif in pending:
                    notif_id = notif[0]
                    job_id = notif[1]
                    notif_text = notif[3]
                    recipients_type = notif[4]
                    scheduled_dt = notif[6]

                    try:
                        scheduled_utc = datetime.fromisoformat(str(scheduled_dt).replace('Z', '+00:00'))
                    except Exception:
                        continue

                    if scheduled_utc > now_utc:
                        continue

                    if recipients_type == 'all':
                        # Рассылка по всем БД (только для super-admin)
                        all_paths = _get_scheduler_db_paths()
                    else:
                        all_paths = [path]

                    send_count = 0
                    seen_tids: set = set()
                    for send_path in all_paths:
                        await asyncio.sleep(0)
                        if not os.path.exists(send_path):
                            continue
                        send_db = Database(send_path)
                        recipients = await asyncio.to_thread(send_db.get_users_for_notifications, 'admin')
                        for user_data in recipients:
                            uid_internal, telegram_id = user_data[0], user_data[1]
                            if not telegram_id:
                                continue
                            try:
                                tid_int = int(telegram_id)
                            except (ValueError, TypeError):
                                continue
                            if tid_int in seen_tids:
                                continue
                            seen_tids.add(tid_int)
                            try:
                                await bot.send_message(
                                    tid_int,
                                    f"🔔 <b>Уведомление от администратора</b>\n\n{notif_text}",
                                    parse_mode="HTML",
                                    reply_markup=add_read_btn()
                                )
                                send_db.add_notification_to_history(uid_internal, 'admin', notif_text)
                                send_count += 1
                                await asyncio.sleep(0.05)
                            except Exception as e:
                                logging.error(f"Scheduled notif send error to {tid_int}: {e}")

                    current_db.update_scheduled_notification_status(job_id, 'sent')
                    logging.info(f"Scheduled notification #{notif_id} sent to {send_count} users from {path}")
            except Exception as e:
                logging.error(f"check_scheduled_notifications error for {path}: {e}")
    except Exception as e:
        logging.error(f"check_scheduled_notifications critical error: {e}")


async def auto_finish_contests(bot: Bot):
    """Автозавершение конкурсов по истечению даты окончания"""
    try:
        from datetime import date
        today = date.today().isoformat()
        db_paths = _get_scheduler_db_paths()

        for path in db_paths:
            if not os.path.exists(path):
                continue
            current_db = Database(path)
            try:
                active = current_db.get_contests(status='active')
                for contest in active:
                    contest_id = contest[0]
                    title = contest[1]
                    end_date = contest[9]
                    notify_on_end = contest[18]

                    if not end_date or end_date >= today:
                        continue

                    current_db.update_contest_status(contest_id, 'finished')
                    logging.info(
                        f"Конкурс #{contest_id} «{title}» автозавершён "
                        f"(end_date={end_date}), БД: {path}"
                    )

                    if not notify_on_end:
                        continue

                    try:
                        reward_mode = contest[22] if len(contest) > 22 else 'total'
                        results = current_db.compute_contest_results(contest_id)
                        winners = [r for r in results if r.get('is_winner')]
                        notified = 0
                        for winner in winners:
                            tg_id = winner.get('telegram_id')
                            if not tg_id:
                                continue
                            fname = winner.get('first_name', '')
                            actual = winner.get('actual', 0)
                            reward = winner.get('reward', 0)
                            if reward_mode == 'per_sale':
                                qty = int(actual)
                                plan_pct = winner.get('plan_pct', 0)
                                plan_note = f"\n📊 Выполнение плана: {plan_pct:.0f}%" if plan_pct > 0 else ""
                                text = (
                                    f"🏆 <b>Конкурс завершён!</b>\n\n"
                                    f"<b>{he(title)}</b>\n\n"
                                    f"Ваши продажи по конкурсу: <b>{qty} шт.</b>\n"
                                    f"💵 Бонус: <b>{reward:,.2f} ₽</b>{plan_note}"
                                )
                            else:
                                text = (
                                    f"🏆 <b>Конкурс завершён!</b>\n\n"
                                    f"Поздравляем, <b>{he(fname)}</b>!\n"
                                    f"Вы победили в конкурсе «<b>{he(title)}</b>».\n\n"
                                    f"📊 Ваш результат: <b>{actual:,.2f}</b>\n"
                                    f"🎁 Награда: <b>{reward:,.2f} ₽</b>"
                                )
                            try:
                                await bot.send_message(int(tg_id), text, parse_mode="HTML", reply_markup=add_read_btn())
                                notified += 1
                                await asyncio.sleep(0.05)
                            except Exception as e:
                                logging.error(
                                    f"auto_finish_contests: ошибка уведомления "
                                    f"победителя {tg_id}: {e}"
                                )
                        logging.info(
                            f"Конкурс #{contest_id}: уведомлено {notified} победителей"
                        )
                    except Exception as e:
                        logging.error(
                            f"auto_finish_contests: ошибка расчёта результатов "
                            f"конкурса #{contest_id}: {e}"
                        )
            except Exception as e:
                logging.error(f"auto_finish_contests: ошибка при обработке {path}: {e}")
    except Exception as e:
        logging.error(f"auto_finish_contests критическая ошибка: {e}")


async def check_low_stock(bot: Bot):
    """Проверка низких остатков товаров (старая функция для совместимости)"""
    await send_personalized_notifications(bot)

async def daily_backup_task(backup_manager):
    """Ежедневная задача резервного копирования всех БД"""
    try:
        results = backup_manager.backup_all_tenants()
        backup_manager.cleanup_old_backups(30)
        logging.info(f"Ежедневное резервное копирование завершено: {', '.join(results)}")
    except Exception as e:
        logging.error(f"Критическая ошибка при резервном копировании: {e}")

async def restart_bot():
    """Функция для безопасного перезапуска бота"""
    try:
        logging.info("Инициирован перезапуск бота...")
        await asyncio.sleep(2)  # Даём время завершить текущие операции
        
        # Останавливаем текущий процесс и запускаем новый
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logging.error(f"Ошибка при перезапуске бота: {e}")

async def send_post_restart_start(bot):
    """Отправка уведомления о завершении перезапуска"""
    try:
        # Ждем полной загрузки бота
        await asyncio.sleep(2)
        
        # Загружаем данные о перезапуске из файла
        restart_data = restart_manager.load_restart_data()
        
        if restart_data and restart_data.get('chat_id'):
            chat_id = restart_data['chat_id']
            reason = restart_data.get('reason', 'Unknown')
            
            # Отправляем уведомление о завершении перезапуска
            await bot.send_message(
                chat_id,
                f"✅ <b>Перезапуск завершен</b>\n\nПричина: {reason}\nБаза данных восстановлена. Бот готов к работе.",
                parse_mode="HTML"
            )
            
            # Автоматически показываем главное меню
            await asyncio.sleep(1)  # Небольшая пауза между сообщениями
            
            # Импортируем и вызываем функцию главного меню
            from keyboards import main_menu
            from database import Database
            
            db = Database('data/main.db')
            user_data = db.get_user(chat_id)
            
            if user_data:
                user_shop = user_data[8] if user_data[8] else "Не указан"
                welcome_text = f"🏠 Главное меню\nМагазин: {user_shop}"
                
                await bot.send_message(
                    chat_id,
                    welcome_text,
                    reply_markup=main_menu(chat_id, user_shop)
                )
            
            logging.info(f"Отправлено уведомление о завершении перезапуска пользователю {chat_id}")
            
    except Exception as e:
        logging.error(f"Ошибка при отправке post-restart сообщения: {e}")


async def auto_reject_stale_payments(bot: Bot):
    """Авто-отклонение pending-заявок СБП старше 72 часов без обработки."""
    try:
        db = Database('data/shop_bot.db')
        stale = db.get_stale_pending_payments(hours=72)
        for req_id, _user_id, plan_type, amount, telegram_id, first_name, _last_name in stale:
            db.reject_payment_request(req_id, admin_id=0)
            try:
                from utils import he as _he
                name = _he(first_name or '')
                await bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"❌ <b>{name}, ваша заявка на оплату отклонена</b>\n\n"
                        f"Тариф: <b>{_he(plan_type)}</b> · {amount:,.0f} ₽\n\n"
                        "Причина: заявка не была обработана администратором в течение 72 часов.\n\n"
                        "Попробуйте оформить заявку повторно или выберите другой способ оплаты."
                    ),
                    parse_mode="HTML",
                    reply_markup=_sub_markup(),
                )
                logging.info(f"auto_reject: заявка #{req_id} отклонена (telegram_id={telegram_id})")
            except Exception as _send_err:
                logging.error(f"auto_reject: ошибка отправки telegram_id={telegram_id}: {_send_err}")
    except Exception as e:
        logging.error(f"auto_reject_stale_payments: {e}")


async def main():
    """Основная функция запуска бота"""
    # Создаем планировщик для уведомлений
    scheduler = AsyncIOScheduler(
        job_defaults={
            'misfire_grace_time': 60,  # до 60 с опоздания — всё равно запустить
            'coalesce': True,          # пропущенные повторы схлопываются в один запуск
            'max_instances': 1,        # не допускаем параллельного запуска одного задания
        }
    )

    # Сохраняем scheduler в модуле для доступа из других файлов
    scheduler_module.set_scheduler(scheduler)
    
    # Задача уведомлений о продажах — каждую минуту, старт на секунде 0
    scheduler.add_job(
        send_sales_alerts,
        CronTrigger(minute='*', second=0),
        args=[bot],
        id='personalized_sales_alerts'
    )

    # Задача платежных уведомлений — каждую минуту, старт на секунде 12
    scheduler.add_job(
        send_payment_alerts,
        CronTrigger(minute='*', second=12),
        args=[bot],
        id='personalized_payment_alerts'
    )

    # Задача ежедневных отчетов — каждую минуту, старт на секунде 24
    scheduler.add_job(
        send_daily_reports,
        CronTrigger(minute='*', second=24),
        args=[bot],
        id='personalized_daily_reports'
    )

    # Задача проверки остатков — каждую минуту, старт на секунде 36
    scheduler.add_job(
        send_personalized_notifications,
        CronTrigger(minute='*', second=36),
        args=[bot],
        id='personalized_stock_check'
    )

    # Задача выполнения запланированных уведомлений — каждую минуту, старт на секунде 48
    scheduler.add_job(
        check_scheduled_notifications,
        CronTrigger(minute='*', second=48),
        args=[bot],
        id='check_scheduled_notifications'
    )

    # Upsell при истечении пробного периода — каждый час в 05 минут
    scheduler.add_job(
        send_trial_expired_upsell,
        CronTrigger(hour='*', minute=5),
        args=[bot],
        id='trial_expired_upsell'
    )

    # Автозавершение конкурсов каждый час в начале часа
    scheduler.add_job(
        auto_finish_contests,
        CronTrigger(hour='*', minute=0),
        args=[bot],
        id='auto_finish_contests'
    )

    # Авто-отклонение просроченных заявок СБП (>72ч) — ежедневно в 10:15
    scheduler.add_job(
        auto_reject_stale_payments,
        CronTrigger(hour=10, minute=15),
        args=[bot],
        id='auto_reject_stale_payments'
    )

    # Добавляем задачу ежедневного резервного копирования в 03:00
    backup_manager = BackupManager()
    
    async def backup_job():
        await daily_backup_task(backup_manager)
    
    scheduler.add_job(
        backup_job,
        CronTrigger(hour=3, minute=0),
        id='daily_backup'
    )
    
    scheduler.start()

    # Регистрируем cron-задачи из интеграций Google Sheets
    try:
        from integration.manager import integration_manager
        await integration_manager.schedule_exports(scheduler, _get_scheduler_db_paths)
    except Exception as _ie:
        logging.warning(f"Integration schedule_exports: {_ie}")

    logging.info("Бот запущен")
    
    # Проверяем, нужно ли отправить post-restart сообщение
    asyncio.create_task(send_post_restart_start(bot))
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logging.info("Получен сигнал остановки")
    finally:
        await bot.session.close()
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
