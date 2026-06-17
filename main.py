"""
Главный файл Telegram бота для управления товарами и продажами
"""
import os
import sys
import asyncio
import logging
import re
import pytz
from aiogram import Bot, Dispatcher
from sqlite_storage import SQLiteStorage
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import Database
from db_utils import wrap_db
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
from referral_handlers import referral_router
from addon_handlers import addon_router
from web_auth_handlers import router as web_auth_router
from absence_handlers import absence_router
from tasks_handlers import tasks_router as tasks_bot_router
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

import bot_holder as _bot_holder
_bot_holder.set_bot(bot)

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
dp.include_router(referral_router)
dp.include_router(addon_router)
dp.include_router(web_auth_router)
dp.include_router(absence_router)
dp.include_router(tasks_bot_router)

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

# One-time migration: legacy plans → modular billing grants
try:
    from migrate_to_modules import run_startup_migration
    run_startup_migration()
except Exception as _mig_err:
    print(f"[migration] Skipped due to error: {_mig_err}")

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
                                await asyncio.sleep(0.05)
                                await asyncio.to_thread(shop_bot_db.mark_reminder_sent, shop_user_id, t, end_date)
                                await asyncio.to_thread(current_db.add_notification_to_history, user_id, 'payment', reminder)
                                try:
                                    from web.push_utils import send_web_push
                                    _pb = re.sub(r'<[^>]+>', '', reminder)[:120].strip()
                                    await asyncio.to_thread(send_web_push, telegram_id, "💳 Подписка", _pb, "/settings")
                                except Exception:
                                    pass
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

                    # Gate: smart_alerts extension required per user
                    try:
                        from billing_utils import has_extension as _hex_sa
                        if not _hex_sa(int(telegram_id), "smart_alerts"):
                            continue
                    except Exception:
                        pass

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

                        try:
                            await bot.send_message(telegram_id, message, parse_mode="HTML", reply_markup=add_read_btn())
                            await asyncio.sleep(0.05)
                            await asyncio.to_thread(current_db.add_notification_to_history, user_id, 'sales', message)
                            try:
                                from web.push_utils import send_web_push
                                _pb = re.sub(r'<[^>]+>', '', message)[:120].strip()
                                await asyncio.to_thread(send_web_push, telegram_id, "🛍️ Уведомление о продажах", _pb, "/sales")
                            except Exception:
                                pass
                        except Exception as _send_err:
                            logging.warning(f"send_sales_alerts: skip {telegram_id}: {_send_err}")
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
                        
                        try:
                            await bot.send_message(telegram_id, message, parse_mode="HTML", reply_markup=add_read_btn())
                            await asyncio.sleep(0.05)
                            await asyncio.to_thread(current_db.add_notification_to_history, user_id, 'low_stock', message)
                            try:
                                from web.push_utils import send_web_push
                                _pb = re.sub(r'<[^>]+>', '', message)[:120].strip()
                                await asyncio.to_thread(send_web_push, telegram_id, "📦 Низкий остаток", _pb, "/inventory")
                            except Exception:
                                pass
                        except Exception as _send_err:
                            logging.warning(f"send_personalized_notifications: skip {telegram_id}: {_send_err}")
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
            current_db = wrap_db(Database(path))
            try:
                users = await current_db.get_users_for_notifications('daily_report')
                for user_data in users:
                    await asyncio.sleep(0)
                    user_id, telegram_id, first_name, shop_name, threshold, notification_time_str = user_data
                    if not telegram_id or not notification_time_str:
                        continue

                    user_timezone = await current_db.get_user_timezone(telegram_id)
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
                            message = await build_admin_daily_text(
                                current_db, yesterday, shop_name,
                                scope_type=_sct, scope_values=_scv
                            )
                        else:
                            message = await build_user_daily_text(
                                current_db, user_id, yesterday, shop_name
                            )
                    except Exception as e:
                        logging.error(f"send_daily_reports: ошибка формирования текста для {telegram_id}: {e}")
                        sales = await current_db.get_user_sales_by_date(user_id, yesterday, yesterday)
                        message = f"📊 <b>Ежедневный отчёт за {yesterday}</b>\n\n"
                        if shop_name:
                            message += f"🏪 Магазин: {he(shop_name)}\n\n"
                        if sales:
                            total_revenue = sum(s[4] for s in sales if s[4])
                            message += f"📦 Продано: {sum(s[3] for s in sales)} шт.\n💰 Выручка: {total_revenue:,.2f} ₽"
                        else:
                            message += "ℹ️ Продаж не было."

                    try:
                        await bot.send_message(telegram_id, message, parse_mode="HTML", reply_markup=add_read_btn())
                        await asyncio.sleep(0.05)
                        await current_db.add_notification_to_history(user_id, 'daily_report', message)
                        try:
                            from web.push_utils import send_web_push
                            from web.routes.api import _notif_url as _nu
                            _pb = re.sub(r'<[^>]+>', '', message)[:120].strip()
                            _push_url = _nu("daily_report", message)
                            await asyncio.to_thread(send_web_push, telegram_id, "📊 Ежедневный отчёт", _pb, _push_url)
                        except Exception:
                            pass
                    except Exception as _send_err:
                        logging.warning(f"send_daily_reports: skip {telegram_id}: {_send_err}")
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
                    recipients_list_raw = notif[5]
                    scheduled_dt = notif[6]

                    try:
                        scheduled_utc = datetime.fromisoformat(str(scheduled_dt).replace('Z', '+00:00'))
                        if scheduled_utc.tzinfo is None:
                            scheduled_utc = scheduled_utc.replace(tzinfo=pytz.UTC)
                    except Exception:
                        continue

                    if scheduled_utc > now_utc:
                        continue

                    # Разбираем фильтр получателей из recipients_list (JSON)
                    import json as _sn_json
                    _rcpt_info = {}
                    if recipients_list_raw:
                        try:
                            _parsed = _sn_json.loads(recipients_list_raw)
                            if isinstance(_parsed, dict):
                                _rcpt_info = _parsed
                        except Exception:
                            pass
                    _rcpt_type   = _rcpt_info.get('type', 'all')
                    _rcpt_filter = _rcpt_info.get('filter')

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
                        # Применяем фильтр по типу получателей
                        if _rcpt_type == 'users' and _rcpt_filter:
                            # Конкретные получатели — без проверки admin_notifications
                            target_tids = set(int(t) for t in _rcpt_filter)
                            all_u = await asyncio.to_thread(send_db.get_all_users)
                            recipients = [(u[0], u[1]) for u in all_u if u[1] and int(u[1]) in target_tids]
                        else:
                            recipients = await asyncio.to_thread(send_db.get_users_for_notifications, 'admin')
                            if _rcpt_type == 'shop' and _rcpt_filter:
                                recipients = [r for r in recipients if r[3] == _rcpt_filter]
                            elif _rcpt_type == 'role' and _rcpt_filter:
                                from db_utils import get_user_org_role as _gor
                                recipients = [r for r in recipients if _gor(r[1]) == _rcpt_filter]
                            elif _rcpt_type == 'city' and _rcpt_filter:
                                city_u = await asyncio.to_thread(send_db.get_all_users, None, _rcpt_filter)
                                city_tids = {u[1] for u in city_u if u[1]}
                                recipients = [r for r in recipients if r[1] in city_tids]
                            elif _rcpt_type == 'network' and _rcpt_filter:
                                net_u = await asyncio.to_thread(send_db.get_all_users, None, None, _rcpt_filter)
                                net_tids = {u[1] for u in net_u if u[1]}
                                recipients = [r for r in recipients if r[1] in net_tids]
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
                                try:
                                    from web.push_utils import send_web_push
                                    _pb = re.sub(r'<[^>]+>', '', notif_text)[:120].strip()
                                    await asyncio.to_thread(send_web_push, tid_int, "🔔 Уведомление", _pb, "/dashboard")
                                except Exception:
                                    pass
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

                    try:
                        reward_mode = contest[22] if len(contest) > 22 else 'total'
                        results = current_db.compute_contest_results(contest_id)
                        winners = [r for r in results if r.get('is_winner')]

                        # Записать призы победителей в salary_adjustments (всегда, идемпотентно)
                        winners_with_reward = [w for w in winners if w.get('reward', 0) > 0]
                        if winners_with_reward and current_db.mark_contest_salary_paid(contest_id):
                            _end_date = contest[9] or ''
                            try:
                                _sal_year = int(_end_date[:4])
                                _sal_month = int(_end_date[5:7])
                            except Exception:
                                from datetime import date as _ddate
                                _sal_year, _sal_month = _ddate.today().year, _ddate.today().month
                            for _w in winners_with_reward:
                                try:
                                    current_db.add_salary_adjustment(
                                        _w['user_id'], _sal_year, _sal_month, _w['reward'],
                                        f"🏆 Приз конкурса «{title}»", None
                                    )
                                except Exception as _we:
                                    logging.error(
                                        f"auto_finish_contests salary_adj "
                                        f"winner {_w.get('user_id')} contest #{contest_id}: {_we}"
                                    )
                            logging.info(
                                f"Конкурс #{contest_id}: записано {len(winners_with_reward)} "
                                f"призов в зарплату ({path})"
                            )

                        if not notify_on_end:
                            continue

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
                                _uid = winner.get('user_id')
                                if _uid:
                                    try:
                                        current_db.add_notification_to_history(_uid, 'admin', text)
                                    except Exception:
                                        pass
                                try:
                                    from web.push_utils import send_web_push
                                    _pb = re.sub(r'<[^>]+>', '', text)[:120].strip()
                                    await asyncio.to_thread(send_web_push, int(tg_id), "🏆 Конкурс завершён!", _pb, "/dashboard")
                                except Exception:
                                    pass
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
                parse_mode="HTML",
                reply_markup=add_read_btn(),
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
        id='personalized_sales_alerts',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Задача платежных уведомлений — каждую минуту, старт на секунде 12
    scheduler.add_job(
        send_payment_alerts,
        CronTrigger(minute='*', second=12),
        args=[bot],
        id='personalized_payment_alerts',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Задача ежедневных отчетов — каждую минуту, старт на секунде 24
    scheduler.add_job(
        send_daily_reports,
        CronTrigger(minute='*', second=24),
        args=[bot],
        id='personalized_daily_reports',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Задача проверки остатков — каждую минуту, старт на секунде 36
    scheduler.add_job(
        send_personalized_notifications,
        CronTrigger(minute='*', second=36),
        args=[bot],
        id='personalized_stock_check',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Задача выполнения запланированных уведомлений — каждую минуту, старт на секунде 48
    scheduler.add_job(
        check_scheduled_notifications,
        CronTrigger(minute='*', second=48),
        args=[bot],
        id='check_scheduled_notifications',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Upsell при истечении пробного периода — каждый час в 05 минут
    scheduler.add_job(
        send_trial_expired_upsell,
        CronTrigger(hour='*', minute=5),
        args=[bot],
        id='trial_expired_upsell',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Автозавершение конкурсов каждый час в начале часа
    scheduler.add_job(
        auto_finish_contests,
        CronTrigger(hour='*', minute=0),
        args=[bot],
        id='auto_finish_contests',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Авто-отклонение просроченных заявок СБП (>72ч) — ежедневно в 10:15
    scheduler.add_job(
        auto_reject_stale_payments,
        CronTrigger(hour=10, minute=15),
        args=[bot],
        id='auto_reject_stale_payments',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Добавляем задачу ежедневного резервного копирования в 03:00
    backup_manager = BackupManager()

    async def backup_job():
        await daily_backup_task(backup_manager)

    scheduler.add_job(
        backup_job,
        CronTrigger(hour=3, minute=0),
        id='daily_backup',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Очистка FSM-хранилища — еженедельно в воскресенье в 04:30
    # Удаляет строки без активного состояния и данных, обновлявшиеся более 30 дней назад
    async def cleanup_fsm_storage():
        try:
            import sqlite3 as _sqlite3
            _conn = _sqlite3.connect("data/fsm_storage.db", timeout=10)
            deleted = _conn.execute(
                "DELETE FROM fsm_data "
                "WHERE state IS NULL AND data = '{}' "
                "AND (updated_at IS NULL OR updated_at < datetime('now', '-30 days'))"
            ).rowcount
            _conn.commit()
            _conn.close()
            if deleted > 0:
                logging.info(f"FSM cleanup: удалено {deleted} устаревших idle-записей")
        except Exception as _fsm_err:
            logging.error(f"FSM cleanup error: {_fsm_err}")

    scheduler.add_job(
        cleanup_fsm_storage,
        CronTrigger(day_of_week='sun', hour=4, minute=30),
        id='fsm_storage_cleanup',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Очистка старых записей ai_tool_stats — ежедневно в 03:15 UTC
    _AI_TOOL_STATS_RETENTION_DAYS_DEFAULT = 90

    async def prune_ai_tool_stats_job():
        try:
            from database import Database as _Database
            _db = _Database('data/shop_bot.db')
            _settings = _db.get_payment_settings()
            try:
                _retention_days = int(_settings.get("ai_stats_retention_days", _AI_TOOL_STATS_RETENTION_DAYS_DEFAULT))
                if _retention_days < 7:
                    _retention_days = _AI_TOOL_STATS_RETENTION_DAYS_DEFAULT
            except (ValueError, TypeError):
                _retention_days = _AI_TOOL_STATS_RETENTION_DAYS_DEFAULT
            deleted = _db.prune_ai_tool_stats(_retention_days)
            logging.info(
                "prune_ai_tool_stats: удалено %d строк старше %d дней",
                deleted, _retention_days,
            )
        except Exception as _e:
            logging.error("prune_ai_tool_stats_job error: %s", _e)

    scheduler.add_job(
        prune_ai_tool_stats_job,
        CronTrigger(hour=3, minute=15),
        id='prune_ai_tool_stats',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Очистка старых записей ai_usage_log — ежедневно в 03:30 UTC
    _AI_USAGE_LOG_RETENTION_DAYS_DEFAULT = 90

    async def prune_ai_usage_log_job():
        try:
            import sqlite3 as _sqlite3
            _retention_days = _AI_USAGE_LOG_RETENTION_DAYS_DEFAULT
            try:
                _env_days = os.getenv('AI_USAGE_LOG_RETENTION_DAYS', '')
                if _env_days.strip():
                    _parsed = int(_env_days.strip())
                    if _parsed >= 7:
                        _retention_days = _parsed
            except (ValueError, TypeError):
                pass
            _db_path = 'data/rate_limits.db'
            if not os.path.exists(_db_path):
                return
            _conn = _sqlite3.connect(_db_path, timeout=5, check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            _cur = _conn.execute(
                "DELETE FROM ai_usage_log WHERE usage_date < date('now', ?)",
                (f'-{_retention_days} days',)
            )
            _deleted = _cur.rowcount
            _conn.commit()
            _conn.close()
            logging.info(
                "prune_ai_usage_log: удалено %d строк старше %d дней",
                _deleted, _retention_days,
            )
        except Exception as _e:
            logging.error("prune_ai_usage_log_job error: %s", _e)

    scheduler.add_job(
        prune_ai_usage_log_job,
        CronTrigger(hour=3, minute=30),
        id='prune_ai_usage_log',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Авто-архивация AI-сессий — ежедневно в 03:20 UTC
    AI_SESSION_ARCHIVE_DAYS = 30

    async def auto_archive_ai_sessions():
        """Вставляет авто-разрыв для AI DM/chat сессий без активности > 30 дней."""
        import glob as _glob
        count_dm = 0
        count_chat = 0
        try:
            from database import Database as _Database
            org_dbs = _glob.glob('data/tenants/org_*.db')
            for _db_path in org_dbs:
                try:
                    _db = _Database(_db_path)
                    count_dm += _db.archive_old_ai_dm_sessions(
                        days=AI_SESSION_ARCHIVE_DAYS
                    )
                    try:
                        _ai_tid = _db.get_ai_topic_id()
                        if _ai_tid:
                            if _db.archive_old_ai_chat_session(
                                _ai_tid, days=AI_SESSION_ARCHIVE_DAYS
                            ):
                                count_chat += 1
                    except Exception:
                        pass
                except Exception as _de:
                    logging.error("auto_archive_ai_sessions db=%s: %s", _db_path, _de)
        except Exception as _e:
            logging.error("auto_archive_ai_sessions: %s", _e)
        logging.info(
            "auto_archive_ai_sessions: %d DM-тредов, %d тем архивировано",
            count_dm, count_chat,
        )

    scheduler.add_job(
        auto_archive_ai_sessions,
        CronTrigger(hour=3, minute=20),
        id='auto_archive_ai_sessions',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Дедлайны задач — ежедневно в 09:10
    async def check_task_deadlines():
        """Напоминания о задачах с дедлайном сегодня и просроченных задачах."""
        import os as _os
        import json as _json
        import urllib.request as _ureq
        import threading as _th
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        def _push(tg_id, text):
            if not tg_id:
                return
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": tg_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": {
                        "inline_keyboard": [[{"text": "✅ Прочитано", "callback_data": "notif_read"}]]
                    },
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                _th.Thread(target=lambda: _ureq.urlopen(req, timeout=10), daemon=True).start()
            except Exception:
                pass

        try:
            from database import Database
            import datetime as _dt
            _yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
            db_paths = _get_scheduler_db_paths()
            for db_path in db_paths:
                try:
                    db = Database(db_path)
                    # Дедлайн сегодня — Telegram + колокольчик + Web Push
                    for t in db.get_tasks_with_deadline_today():
                        title = t.get('title', '—')
                        if t.get('assigned_tg') and t.get('assigned_to'):
                            _push(t['assigned_tg'],
                                  f"📋 <b>Срок задачи сегодня!</b>\n<b>{title}</b>\n\n"
                                  f"🌐 Откройте веб-кабинет для деталей.")
                            try:
                                db.add_notification_to_history(
                                    t['assigned_to'], 'task_deadline',
                                    f"📋 Срок задачи сегодня: {title}")
                            except Exception:
                                pass
                            try:
                                from web.push_utils import send_web_push
                                await asyncio.to_thread(send_web_push, int(t['assigned_tg']),
                                                        "📋 Срок задачи сегодня", title, "/tasks")
                            except Exception:
                                pass
                        if t.get('creator_tg') and t.get('creator_tg') != t.get('assigned_tg') and t.get('created_by'):
                            _push(t['creator_tg'],
                                  f"📋 <b>Срок задачи сегодня</b>\n<b>{title}</b>")
                            try:
                                db.add_notification_to_history(
                                    t['created_by'], 'task_deadline',
                                    f"📋 Срок задачи сегодня: {title}")
                            except Exception:
                                pass
                            try:
                                from web.push_utils import send_web_push
                                await asyncio.to_thread(send_web_push, int(t['creator_tg']),
                                                        "📋 Срок задачи сегодня", title, "/tasks")
                            except Exception:
                                pass
                    # Задачи, ставшие просроченными вчера — по 1 уведомлению на задачу
                    for t in db.get_overdue_tasks():
                        if t.get('deadline') != _yesterday:
                            continue
                        title = t.get('title', '—')
                        if t.get('assigned_tg') and t.get('assigned_to'):
                            _push(t['assigned_tg'],
                                  f"⚠️ <b>Задача просрочена!</b>\n<b>{title}</b>\n\n"
                                  f"🌐 Откройте веб-кабинет.")
                            try:
                                db.add_notification_to_history(
                                    t['assigned_to'], 'task_overdue',
                                    f"⚠️ Задача просрочена: {title}")
                            except Exception:
                                pass
                            try:
                                from web.push_utils import send_web_push
                                await asyncio.to_thread(send_web_push, int(t['assigned_tg']),
                                                        "⚠️ Задача просрочена", title, "/tasks")
                            except Exception:
                                pass
                        if t.get('creator_tg') and t.get('creator_tg') != t.get('assigned_tg') and t.get('created_by'):
                            _push(t['creator_tg'],
                                  f"⚠️ <b>Задача просрочена</b>\n<b>{title}</b>")
                            try:
                                db.add_notification_to_history(
                                    t['created_by'], 'task_overdue',
                                    f"⚠️ Задача просрочена: {title}")
                            except Exception:
                                pass
                            try:
                                from web.push_utils import send_web_push
                                await asyncio.to_thread(send_web_push, int(t['creator_tg']),
                                                        "⚠️ Задача просрочена", title, "/tasks")
                            except Exception:
                                pass
                except Exception as _de:
                    logging.error(f"check_task_deadlines db={db_path}: {_de}")
        except Exception as _e:
            logging.error(f"check_task_deadlines: {_e}")

    scheduler.add_job(
        check_task_deadlines,
        CronTrigger(hour=9, minute=10),
        id='check_task_deadlines',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # AI инсайты сети — каждый понедельник в 09:00 UTC
    async def send_weekly_network_insights():
        """Для каждого владельца сети с ai_network_insights — отправить недельный дайджест."""
        import os as _os, json as _json, sqlite3 as _sq, datetime as _dt, urllib.request as _ureq
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return
        try:
            from web.ai_utils import ask_llm, is_configured
            from billing_utils import has_extension as _has_ext, has_module as _has_mod
        except Exception as _imp_err:
            logging.warning(f"send_weekly_network_insights: import error: {_imp_err}")
            return

        if not is_configured():
            return

        def _send_tg(tg_id, text):
            if not tg_id:
                return
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": tg_id, "text": text, "parse_mode": "HTML",
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                import threading as _th
                _th.Thread(target=lambda: _ureq.urlopen(req, timeout=10), daemon=True).start()
            except Exception:
                pass

        try:
            conn = _sq.connect("data/main.db")
            owner_rows = conn.execute(
                """SELECT DISTINCT m.telegram_id
                   FROM user_org_mapping m
                   JOIN organizations o ON o.id = m.org_id
                   WHERE m.role = 'owner' AND o.is_active = 1"""
            ).fetchall()
            all_orgs_rows = conn.execute(
                """SELECT o.db_path, o.name, o.id, m.telegram_id
                   FROM organizations o
                   JOIN user_org_mapping m ON m.org_id = o.id
                   WHERE m.role = 'owner' AND o.is_active = 1"""
            ).fetchall()
            conn.close()
        except Exception as _db_err:
            logging.error(f"send_weekly_network_insights main.db: {_db_err}")
            return

        import os as _os2
        owner_orgs: dict[int, list[dict]] = {}
        for db_path, org_name, org_id, tg_id in all_orgs_rows:
            if db_path and _os2.path.exists(db_path) and db_path != "data/shop_bot.db":
                owner_orgs.setdefault(tg_id, []).append({"org_db": db_path, "name": org_name or f"org#{org_id}"})

        import datetime as _dt
        _now_utc = _dt.datetime.utcnow()
        _current_utc_weekday = _now_utc.weekday()  # 0=Mon
        _current_utc_hour = _now_utc.hour

        for tg_id, in owner_rows:
            try:
                if not tg_id or tg_id <= 0:
                    continue
                if not _has_mod(tg_id, "ai_assistant"):
                    continue
                if not _has_ext(tg_id, "ai_network_insights"):
                    continue

                # Per-owner delivery schedule check
                try:
                    from database import Database as _DbPrefs
                    _sb = _DbPrefs('data/shop_bot.db')
                    _prefs = _sb.get_network_digest_prefs(int(tg_id))
                except Exception:
                    _prefs = {"weekday": 0, "hour_msk": 12}
                _msk_hour = int(_prefs.get("hour_msk", 12))
                _weekday_msk = int(_prefs.get("weekday", 0))
                _expected_utc_hour = (_msk_hour - 3) % 24
                # If MSK hour < 3, UTC day is one day earlier (MSK+3 wraps to next day)
                if _msk_hour < 3:
                    _expected_utc_weekday = (_weekday_msk - 1) % 7
                else:
                    _expected_utc_weekday = _weekday_msk
                if _current_utc_weekday != _expected_utc_weekday or _current_utc_hour != _expected_utc_hour:
                    continue  # not this owner's preferred time
                orgs = owner_orgs.get(tg_id, [])
                if len(orgs) < 2:
                    continue

                from database import Database
                today = _dt.date.today()
                week_start = (today - _dt.timedelta(days=7)).isoformat()
                today_str = today.isoformat()

                org_summaries = []
                for org in orgs:
                    try:
                        db = Database(org["org_db"])
                        cur = db.get_sales_summary(start_date=week_start, end_date=today_str) or (0, 0, 0, 0)
                        prev_end = (today - _dt.timedelta(days=8))
                        prev_start = (today - _dt.timedelta(days=14)).isoformat()
                        prev = db.get_sales_summary(start_date=prev_start, end_date=prev_end.isoformat()) or (0, 0, 0, 0)
                        org_summaries.append({
                            "name": org["name"],
                            "revenue_month": float(cur[2] or 0),
                            "revenue_prev": float(prev[2] or 0),
                            "top_category": "—",
                            "plan_pct": 0,
                            "seller_count": 0,
                        })
                    except Exception:
                        pass

                if len(org_summaries) < 2:
                    continue

                lines = []
                for o in org_summaries:
                    delta_pct = round((o["revenue_month"] - o["revenue_prev"]) / o["revenue_prev"] * 100, 1) if o["revenue_prev"] else 0
                    lines.append(f"• {o['name']}: {o['revenue_month']:,.0f} ₽ ({delta_pct:+.1f}% к пред. неделе)")

                prompt = (
                    "Ты — бизнес-аналитик розничной сети. Проанализируй недельные данные:\n\n"
                    + "\n".join(lines)
                    + "\n\nСоставь краткий недельный дайджест (3-4 предложения): лидеры, аутсайдеры, главный вывод."
                )
                system = "Пиши по-русски, кратко, без markdown. Ссылайся на названия магазинов."
                ai_text = await ask_llm(prompt, system=system, max_tokens=400)
                if not ai_text:
                    continue

                msg = f"🌐 <b>AI-дайджест сети — {today.strftime('%d.%m.%Y')}</b>\n\n{ai_text}"
                _send_tg(tg_id, msg)

                try:
                    from web.push_utils import send_web_push
                    push_body = ai_text[:120] + "…" if len(ai_text) > 120 else ai_text
                    await asyncio.to_thread(
                        send_web_push, int(tg_id),
                        f"🌐 AI-дайджест сети — {today.strftime('%d.%m.%Y')}",
                        push_body, "/ai-insights"
                    )
                except Exception as _push_err:
                    logging.warning(f"send_weekly_network_insights push={tg_id}: {_push_err}")

                try:
                    conn2 = _sq.connect("data/shop_bot.db")
                    conn2.execute(
                        """INSERT INTO ai_insights_cache (tg_id, insights_text, generated_at)
                           VALUES (?, ?, datetime('now'))
                           ON CONFLICT(tg_id) DO UPDATE SET
                             insights_text = excluded.insights_text,
                             generated_at  = excluded.generated_at""",
                        (tg_id, ai_text)
                    )
                    conn2.commit()
                    conn2.close()
                except Exception:
                    pass

            except Exception as _owner_err:
                logging.warning(f"send_weekly_network_insights owner={tg_id}: {_owner_err}")

        logging.info("send_weekly_network_insights: done")

    scheduler.add_job(
        send_weekly_network_insights,
        CronTrigger(hour='*', minute=0),
        id='ai_network_insights',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # AI умные алерты — каждый час в :05, час отправки настраивается per-org (МСК)
    async def ai_smart_alerts():
        """Анализирует падения выручки по каждой орг и отправляет алерты через LLM."""
        import os as _os, json as _json, urllib.request as _ureq
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        try:
            from web.ai_utils import ask_llm, build_smart_alert_prompt, is_configured
        except Exception as _imp_err:
            logging.warning(f"ai_smart_alerts: cannot import ai_utils: {_imp_err}")
            return

        def _send_tg(tg_id, text):
            if not tg_id:
                return
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": tg_id, "text": text, "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": [[{"text": "✅ Прочитано", "callback_data": "notif_read"}]]}
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                import threading as _th
                _th.Thread(target=lambda: _ureq.urlopen(req, timeout=10), daemon=True).start()
            except Exception:
                pass

        try:
            from database import Database
            import datetime as _dt
            current_utc_hour = _dt.datetime.utcnow().hour
            db_paths = _get_scheduler_db_paths()
            yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
            week_ago  = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()

            for db_path in db_paths:
                try:
                    db = Database(db_path)

                    # Читаем per-org настройки алертов
                    try:
                        alert_cfg = db.get_ai_alert_settings()
                    except Exception:
                        alert_cfg = {"enabled": True, "threshold_pct": 35, "alert_hour_msk": 10, "metrics": ["revenue"]}

                    if not alert_cfg.get("enabled", True):
                        continue  # алерты отключены для этой орг

                    # Проверяем: сейчас UTC-час совпадает с нужным? (МСК = UTC+3)
                    msk_hour = int(alert_cfg.get("alert_hour_msk", 10))
                    expected_utc_hour = (msk_hour - 3) % 24
                    if current_utc_hour != expected_utc_hour:
                        continue

                    threshold = int(alert_cfg.get("threshold_pct", 35))
                    metrics = alert_cfg.get("metrics", ["revenue"])

                    # Выручка за вчера
                    y_summary  = db.get_sales_summary(start_date=yesterday, end_date=yesterday) or (0, 0, 0, 0)
                    w_summary  = db.get_sales_summary(start_date=week_ago, end_date=yesterday)  or (0, 0, 0, 0)
                    y_rev  = float(y_summary[2] or 0)
                    w_rev  = float(w_summary[2] or 0)
                    y_cnt  = int(y_summary[0] or 0)
                    w_cnt  = int(w_summary[0] or 0)
                    y_avg  = float(y_summary[3] or 0)
                    avg_7d = w_rev / 7 if w_rev > 0 else 0
                    avg_7d_cnt = w_cnt / 7 if w_cnt > 0 else 0

                    # Определяем нужно ли слать алерт
                    zero_yesterday = y_rev == 0 and avg_7d > 0
                    drop_pct = ((avg_7d - y_rev) / avg_7d * 100) if avg_7d > 0 else 0

                    revenue_alert = zero_yesterday or ("revenue" in metrics and drop_pct >= threshold)
                    avg_check_alert = ("avg_check" in metrics and avg_7d_cnt > 0 and y_cnt > 0
                                       and y_avg > 0 and w_cnt > 0)
                    if avg_check_alert:
                        avg_check_7d = (w_rev / w_cnt) if w_cnt > 0 else 0
                        avg_check_drop = ((avg_check_7d - y_avg) / avg_check_7d * 100) if avg_check_7d > 0 else 0
                        avg_check_alert = avg_check_drop >= threshold
                    else:
                        avg_check_alert = False
                    txn_alert = ("transactions" in metrics and avg_7d_cnt > 0 and y_cnt > 0
                                 and ((avg_7d_cnt - y_cnt) / avg_7d_cnt * 100) >= threshold)

                    if not (revenue_alert or avg_check_alert or txn_alert):
                        continue  # всё нормально

                    # Получаем telegram_id владельцев/админов через штатный метод
                    admin_ids = db.get_all_admins_telegram_ids()
                    if not admin_ids:
                        continue

                    # Gate: ai_smart_alerts extension required (check org owner)
                    # fail-closed: при любом сбое биллинга — пропускаем орг, не шлём алерт
                    try:
                        from billing_utils import has_extension as _hex_ext
                        if not any(_hex_ext(int(tid), "ai_smart_alerts") for tid in admin_ids[:3] if tid and int(tid) > 0):
                            continue
                    except Exception as _gate_err:
                        logging.warning(f"ai_smart_alerts billing gate error, skipping org: {_gate_err}")
                        continue

                    org_name = db.db_file.replace("\\", "/").split("/")[-1].replace(".db", "").replace("org_", "")

                    # Формируем дополнительный контекст для алерта
                    extra_lines = []
                    if avg_check_alert:
                        extra_lines.append(f"Средний чек упал на {int(avg_check_drop)}%: {int(y_avg):,} ₽ vs {int(avg_check_7d):,} ₽ среднее 7д.")
                    if txn_alert:
                        txn_drop = (avg_7d_cnt - y_cnt) / avg_7d_cnt * 100 if avg_7d_cnt > 0 else 0
                        extra_lines.append(f"Транзакций упало на {int(txn_drop)}%: {y_cnt} вчера vs {avg_7d_cnt:.1f} среднее 7д.")

                    _digest_context = alert_cfg.get("digest_context", ["products", "sellers", "plans"])

                    # Fetch rich context only for blocks the owner has enabled
                    if "products" in _digest_context:
                        try:
                            _top_products = db.get_top_products_month(3)
                        except Exception:
                            _top_products = []
                    else:
                        _top_products = []
                    if "sellers" in _digest_context:
                        try:
                            _top_sellers = db.get_active_sellers_month(3)
                        except Exception:
                            _top_sellers = []
                    else:
                        _top_sellers = []
                    if "plans" in _digest_context:
                        try:
                            _plans = db.get_plans_with_progress()
                        except Exception:
                            _plans = []
                    else:
                        _plans = []

                    ai_text: str | None = None
                    if is_configured():
                        try:
                            prompt = build_smart_alert_prompt(
                                org_name=org_name,
                                yesterday_revenue=y_rev,
                                avg_7d=avg_7d,
                                drop_pct=drop_pct,
                                zero_yesterday=zero_yesterday,
                                top_products=_top_products,
                                top_sellers=_top_sellers,
                                plans=_plans,
                                digest_context=_digest_context,
                            )
                            if extra_lines:
                                prompt += "\nДополнительно: " + " ".join(extra_lines)
                            ai_text = await ask_llm(prompt, max_tokens=350)
                        except Exception as _ai_err:
                            logging.warning(f"ai_smart_alerts LLM error: {_ai_err}")

                    if ai_text:
                        msg = f"🤖 <b>AI-алерт</b>\n\n{ai_text}"
                    elif zero_yesterday:
                        msg = f"⚠️ <b>Нет продаж вчера</b>\nСредняя за 7 дней: {int(avg_7d):,} ₽"
                    else:
                        msg = (f"📉 <b>Падение выручки на {int(drop_pct)}%</b>\n"
                               f"Вчера: {int(y_rev):,} ₽ | Среднее 7д: {int(avg_7d):,} ₽")
                        if extra_lines:
                            msg += "\n" + "\n".join(extra_lines)

                    _alert_push_ok = alert_cfg.get("alert_push_enabled", True)
                    import re as _re
                    _plain_alert = _re.sub(r"<[^>]+>", "", msg).strip()
                    _push_alert_body = _plain_alert[:120] + ("…" if len(_plain_alert) > 120 else "")

                    # Сохраняем алерт в историю org БД
                    try:
                        db.add_ai_alert_log('alert', _plain_alert)
                    except Exception:
                        pass

                    # Telegram — только администраторам (максимум 3)
                    for tg_id in admin_ids[:3]:
                        if tg_id and tg_id > 0:
                            _send_tg(tg_id, msg)
                            try:
                                _nc = db.get_connection()
                                _nr = _nc.execute("SELECT id FROM users WHERE telegram_id = ?", (int(tg_id),)).fetchone()
                                _nc.close()
                                if _nr:
                                    db.add_notification_to_history(_nr[0], 'admin', _push_alert_body)
                            except Exception:
                                pass

                    # Web Push — всем пользователям орги с push-подпиской
                    if _alert_push_ok:
                        try:
                            from web.push_utils import apush_bulk as _apush_bulk
                            _all_tids = [int(u[1]) for u in (db.get_all_users() or []) if u[1] and int(u[1]) > 0]
                            if _all_tids:
                                await _apush_bulk(_all_tids, "🤖 AI-алерт", _push_alert_body, "/ai-insights")
                        except Exception as _push_err:
                            logging.debug(f"ai_smart_alerts push_bulk: {_push_err}")

                    # Email — администраторам орги у которых есть email в web_credentials
                    if alert_cfg.get("alert_email_enabled", False):
                        try:
                            from web.email_utils import send_smart_alert_email as _send_alert_email, is_configured as _email_ok
                            import sqlite3 as _sq3e
                            if _email_ok():
                                _sdb_e = _sq3e.connect("data/shop_bot.db")
                                for _tid in admin_ids[:3]:
                                    if not _tid or int(_tid) <= 0:
                                        continue
                                    _wc = _sdb_e.execute(
                                        "SELECT email FROM web_credentials WHERE telegram_id=? AND is_verified=1 LIMIT 1",
                                        (int(_tid),)
                                    ).fetchone()
                                    if _wc and _wc[0]:
                                        import anyio as _anyio
                                        await _anyio.to_thread.run_sync(
                                            lambda _e=_wc[0]: _send_alert_email(_e, _plain_alert)
                                        )
                                _sdb_e.close()
                        except Exception as _email_err:
                            logging.debug(f"ai_smart_alerts email: {_email_err}")

                except Exception as _db_err:
                    logging.warning(f"ai_smart_alerts db={db_path}: {_db_err}")
        except Exception as _e:
            logging.error(f"ai_smart_alerts: {_e}")

    scheduler.add_job(
        ai_smart_alerts,
        CronTrigger(hour='7,19', minute=5),   # 2 раза в день: 07:05 и 19:05 UTC (10:05 и 22:05 МСК)
        id='ai_smart_alerts',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Проверка аномального роста AI-запросов — ежедневно в 08:00 UTC
    async def ai_anomaly_check():
        """Сравнивает суммарные AI-запросы за вчера с порогом anomaly_daily_threshold.
        Если превышен — отправляет Telegram-сообщение суперадмину на ADMIN_CHAT_ID.
        """
        import os as _os, json as _json, urllib.request as _ureq
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token or not ADMIN_CHAT_ID:
            return
        try:
            from web.rate_store import get_anomaly_threshold, get_ai_yesterday_total
            threshold = get_anomaly_threshold()
            yesterday_total = get_ai_yesterday_total()
            if yesterday_total <= threshold:
                logging.debug(
                    "ai_anomaly_check: yesterday=%d, threshold=%d — OK",
                    yesterday_total, threshold,
                )
                return
            import datetime as _dt
            yesterday_str = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%d.%m.%Y")
            text = (
                f"⚠️ <b>AI-аномалия: всплеск запросов</b>\n\n"
                f"📅 Дата: <b>{yesterday_str}</b>\n"
                f"📊 Запросов за день: <b>{yesterday_total:,}</b>\n"
                f"🚨 Порог: <b>{threshold:,}</b>\n\n"
                "Проверьте /admin/ai-limits — возможно, пора скорректировать лимиты или включить kill-switch."
            )
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": ADMIN_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                _ureq.urlopen(req, timeout=10)
                logging.info(
                    "ai_anomaly_check: alert sent (yesterday=%d > threshold=%d)",
                    yesterday_total, threshold,
                )
            except Exception as _send_err:
                logging.error("ai_anomaly_check: failed to send Telegram alert: %s", _send_err)
        except Exception as _e:
            logging.error("ai_anomaly_check: %s", _e)

    scheduler.add_job(
        ai_anomaly_check,
        CronTrigger(hour=8, minute=0),   # 08:00 UTC ежедневно
        id='ai_anomaly_check',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # AI еженедельный позитивный дайджест — запускается каждый час, проверяет per-org расписание
    async def ai_weekly_digest():
        """Отправляет позитивный AI-дайджест по итогам недели: топ-товары, лидеры продаж, планы."""
        import os as _os, json as _json, urllib.request as _ureq
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        try:
            from web.ai_utils import ask_llm, build_weekly_digest_prompt, is_configured
        except Exception as _imp_err:
            logging.warning(f"ai_weekly_digest: cannot import ai_utils: {_imp_err}")
            return

        def _send_tg(tg_id, text):
            if not tg_id:
                return
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": tg_id, "text": text, "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": [[{"text": "✅ Прочитано", "callback_data": "notif_read"}]]}
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                import threading as _th
                _th.Thread(target=lambda: _ureq.urlopen(req, timeout=10), daemon=True).start()
            except Exception:
                pass

        try:
            from database import Database
            import datetime as _dt
            _now_utc = _dt.datetime.utcnow()
            _current_utc_weekday = _now_utc.weekday()  # 0=Mon
            _current_utc_hour = _now_utc.hour

            today = _dt.date.today()
            week_start = (today - _dt.timedelta(days=7)).isoformat()
            week_end   = (today - _dt.timedelta(days=1)).isoformat()
            prev_start = (today - _dt.timedelta(days=14)).isoformat()
            prev_end   = (today - _dt.timedelta(days=8)).isoformat()

            db_paths = _get_scheduler_db_paths()

            for db_path in db_paths:
                try:
                    db = Database(db_path)

                    # Gate: ai_smart_alerts extension required (same billing extension)
                    try:
                        admin_ids = db.get_all_admins_telegram_ids()
                    except Exception:
                        continue
                    if not admin_ids:
                        continue

                    try:
                        from billing_utils import has_extension as _hex_ext
                        if not any(_hex_ext(int(tid), "ai_smart_alerts") for tid in admin_ids[:3] if tid and int(tid) > 0):
                            continue
                    except Exception:
                        pass

                    # Per-org digest schedule check
                    try:
                        _digest_cfg = db.get_ai_alert_settings()
                    except Exception:
                        _digest_cfg = {"digest_enabled": True, "digest_day_of_week": 0, "digest_hour_msk": 9}

                    if not _digest_cfg.get("digest_enabled", True):
                        continue  # дайджест отключён для этой орг

                    _digest_msk_hour = int(_digest_cfg.get("digest_hour_msk", 9))
                    _digest_weekday_msk = int(_digest_cfg.get("digest_day_of_week", 0))
                    _digest_expected_utc_hour = (_digest_msk_hour - 3) % 24
                    # Если MSK час < 3, UTC-день на одни сутки раньше
                    if _digest_msk_hour < 3:
                        _digest_expected_utc_weekday = (_digest_weekday_msk - 1) % 7
                    else:
                        _digest_expected_utc_weekday = _digest_weekday_msk
                    if _current_utc_weekday != _digest_expected_utc_weekday or _current_utc_hour != _digest_expected_utc_hour:
                        continue  # не то время для этой орг

                    # Выручка за прошлую неделю и позапрошлую для сравнения
                    w_summary  = db.get_sales_summary(start_date=week_start, end_date=week_end)  or (0, 0, 0, 0)
                    pw_summary = db.get_sales_summary(start_date=prev_start, end_date=prev_end) or (0, 0, 0, 0)
                    week_rev      = float(w_summary[2] or 0)
                    prev_week_rev = float(pw_summary[2] or 0)

                    # Нет данных за неделю — пропускаем
                    if week_rev == 0:
                        continue

                    # Топ товары, продавцы, планы за прошедшую неделю
                    try:
                        _top_products = db.get_top_products_month(3)
                    except Exception:
                        _top_products = []
                    try:
                        _top_sellers = db.get_active_sellers_month(3)
                    except Exception:
                        _top_sellers = []
                    try:
                        _plans = db.get_plans_with_progress()
                    except Exception:
                        _plans = []

                    # Категорийный разбор и дневной тренд за прошедшую неделю
                    try:
                        _category_breakdown = db.get_sales_by_category_for_period(week_start, week_end)
                    except Exception:
                        _category_breakdown = []
                    try:
                        _daily_data = db.get_daily_sales_for_period(week_start, week_end)
                        import datetime as _dt2
                        _daily_map: dict[int, float] = {}
                        for _d in _daily_data:
                            _wday = _dt2.date.fromisoformat(_d["date"]).weekday()
                            _daily_map[_wday] = _d["amount"]
                        _daily_revenues = [_daily_map.get(i, 0.0) for i in range(7)]
                    except Exception:
                        _daily_revenues = []

                    org_name = db.db_file.replace("\\", "/").split("/")[-1].replace(".db", "").replace("org_", "")

                    ai_text: str | None = None
                    if is_configured():
                        try:
                            prompt = build_weekly_digest_prompt(
                                org_name=org_name,
                                week_revenue=week_rev,
                                prev_week_revenue=prev_week_rev,
                                top_products=_top_products,
                                top_sellers=_top_sellers,
                                plans=_plans,
                                category_breakdown=_category_breakdown or None,
                                daily_revenues=_daily_revenues or None,
                            )
                            ai_text = await ask_llm(prompt, max_tokens=400)
                        except Exception as _ai_err:
                            logging.warning(f"ai_weekly_digest LLM error: {_ai_err}")

                    if ai_text:
                        msg = f"📊 <b>AI-дайджест недели</b>\n\n{ai_text}"
                    else:
                        # Fallback без LLM: структурированный текст
                        _DOW_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
                        lines = [f"📊 <b>Итоги недели</b>: {int(week_rev):,} ₽"]
                        if prev_week_rev > 0:
                            diff = (week_rev - prev_week_rev) / prev_week_rev * 100
                            arrow = "▲" if diff >= 0 else "▼"
                            lines.append(f"{arrow} {abs(diff):.0f}% к прошлой неделе")
                        if _top_products:
                            name, qty, rev = _top_products[0][0], _top_products[0][1], _top_products[0][2]
                            lines.append(f"🏆 Топ товар: {name} — {int(qty)} шт., {int(rev):,} ₽")
                        if _top_sellers:
                            fn, ln = _top_sellers[0][0] or "", _top_sellers[0][1] or ""
                            seller = f"{fn} {ln}".strip() or _top_sellers[0][2] or "—"
                            lines.append(f"⭐ Лидер продаж: {seller} — {int(_top_sellers[0][3]):,} ₽")
                        if _category_breakdown:
                            top_cat = _category_breakdown[0]
                            lines.append(f"📦 Топ категория: {top_cat['category']} — {int(top_cat['revenue']):,} ₽")
                        if _daily_revenues and len(_daily_revenues) == 7 and max(_daily_revenues) > 0:
                            _best = _daily_revenues.index(max(_daily_revenues))
                            lines.append(f"📅 Лучший день: {_DOW_RU[_best]} ({int(max(_daily_revenues)):,} ₽)")
                        msg = "\n".join(lines)

                    # Strip HTML tags for plain-text push body
                    import re as _re
                    _plain_body = _re.sub(r"<[^>]+>", "", msg).strip()
                    _push_body = _plain_body[:120] + ("…" if len(_plain_body) > 120 else "")

                    # Сохраняем дайджест в историю org БД
                    try:
                        db.add_ai_alert_log('digest', _plain_body)
                    except Exception:
                        pass

                    _push_ok = _digest_cfg.get("digest_push_enabled", True)

                    # Telegram — только администраторам (максимум 3)
                    for tg_id in admin_ids[:3]:
                        if tg_id and tg_id > 0:
                            _send_tg(tg_id, msg)
                            try:
                                _nc = db.get_connection()
                                _nr = _nc.execute("SELECT id FROM users WHERE telegram_id = ?", (int(tg_id),)).fetchone()
                                _nc.close()
                                if _nr:
                                    db.add_notification_to_history(_nr[0], 'admin', _push_body)
                            except Exception:
                                pass

                    # Web Push — всем пользователям орги с push-подпиской
                    if _push_ok:
                        try:
                            from web.push_utils import apush_bulk as _apush_bulk
                            _all_tids = [int(u[1]) for u in (db.get_all_users() or []) if u[1] and int(u[1]) > 0]
                            if _all_tids:
                                await _apush_bulk(_all_tids, "📊 AI-дайджест недели", _push_body, "/ai-insights")
                        except Exception as _push_err:
                            logging.debug(f"ai_weekly_digest push_bulk: {_push_err}")

                    # Email — администраторам орги у которых есть email в web_credentials
                    if _digest_cfg.get("digest_email_enabled", False):
                        try:
                            from web.email_utils import send_weekly_digest_email as _send_digest_email, is_configured as _email_ok
                            import sqlite3 as _sq3de
                            if _email_ok():
                                _sdb_de = _sq3de.connect("data/shop_bot.db")
                                for _tid in admin_ids[:3]:
                                    if not _tid or int(_tid) <= 0:
                                        continue
                                    _wc = _sdb_de.execute(
                                        "SELECT email FROM web_credentials WHERE telegram_id=? AND is_verified=1 LIMIT 1",
                                        (int(_tid),)
                                    ).fetchone()
                                    if _wc and _wc[0]:
                                        import anyio as _anyio
                                        await _anyio.to_thread.run_sync(
                                            lambda _e=_wc[0]: _send_digest_email(_e, _plain_body)
                                        )
                                _sdb_de.close()
                        except Exception as _email_err:
                            logging.debug(f"ai_weekly_digest email: {_email_err}")

                    # Сохраняем дайджест в shop_bot.db для отображения в веб-кабинете
                    try:
                        import sqlite3 as _sq3
                        _sdb = _sq3.connect("data/shop_bot.db")
                        _sdb.execute(
                            """INSERT INTO ai_weekly_digest_cache (org_db, digest_text, generated_at)
                               VALUES (?, ?, datetime('now'))
                               ON CONFLICT(org_db) DO UPDATE SET
                                 digest_text  = excluded.digest_text,
                                 generated_at = excluded.generated_at""",
                            (db_path, ai_text or msg),
                        )
                        _sdb.commit()
                        _sdb.close()
                    except Exception as _save_err:
                        logging.warning(f"ai_weekly_digest cache save: {_save_err}")

                except Exception as _db_err:
                    logging.warning(f"ai_weekly_digest db={db_path}: {_db_err}")
        except Exception as _e:
            logging.error(f"ai_weekly_digest: {_e}")

    scheduler.add_job(
        ai_weekly_digest,
        CronTrigger(minute=5),
        id='ai_weekly_digest',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.start()

    # Регистрируем cron-задачи из интеграций Google Sheets
    try:
        from integration.manager import integration_manager
        await integration_manager.schedule_exports(scheduler, _get_scheduler_db_paths)
    except Exception as _ie:
        logging.warning(f"Integration schedule_exports: {_ie}")

    logging.info("Бот запущен")

    # Сохраняем username бота для генерации deep-link
    try:
        _me = await bot.get_me()
        import bot_holder as _bh
        _bh.set_username(_me.username)
        logging.info(f"Bot username: @{_me.username}")
    except Exception as _me_err:
        logging.warning(f"Не удалось получить username бота: {_me_err}")

    # Проверяем, нужно ли отправить post-restart сообщение
    asyncio.create_task(send_post_restart_start(bot))

    # Запускаем веб-интерфейс на порту 8080 вместе с ботом
    try:
        import uvicorn
        from web.app import create_web_app
        web_app = create_web_app()
        _web_port = int(os.getenv('WEB_PORT', '5000'))
        web_config = uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=_web_port,
            log_level="warning",
            access_log=False,
        )
        web_server = uvicorn.Server(web_config)
        logging.info(f"Веб-интерфейс запущен на порту {_web_port}")
        await asyncio.gather(
            dp.start_polling(bot),
            web_server.serve(),
        )
    except KeyboardInterrupt:
        logging.info("Получен сигнал остановки")
    except Exception as _web_err:
        logging.error(f"Ошибка веб-сервера: {_web_err}")
        try:
            await dp.start_polling(bot)
        except KeyboardInterrupt:
            pass
    finally:
        await bot.session.close()
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
