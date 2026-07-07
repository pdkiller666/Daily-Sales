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
from returns_handlers import returns_router
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
from clients_handlers import clients_router as clients_bot_router
from services_handlers import services_router as services_bot_router
import scheduler_module
from jobs.registry import register_inline_jobs
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
dp.include_router(returns_router)
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
dp.include_router(clients_bot_router)
dp.include_router(services_bot_router)

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


# ─────────────────────────────────────────────────────────────────────────────
# Schedule Index (roadmap 2.1) — событийная модель вместо O(N)-обхода орг каждую минуту.
# Вместо того чтобы 4 минутных джоба открывали КАЖДУЮ org-базу раз в минуту,
# строим индекс {notif_type: {(hhmm_local, tz_name): [entry, ...]}} раз в TTL
# (или при dirty-инвалидации). Минутный джоб делает O(уникальных (hhmm,tz)) поиск.
# entry = (db_path, user_id, telegram_id, first_name, shop_name, threshold)
_SCHED_NOTIF_TYPES = ('sales', 'payment', 'daily_report', 'low_stock')
_sched_index: dict = {}            # {notif_type: {(hhmm, tz_name): [entry, ...]}}
_sched_index_ts: float = 0.0       # monotonic; 0 == ещё не строился
_sched_index_dirty: bool = True    # старт «грязный» → первый джоб построит индекс
_SCHED_INDEX_TTL = 900.0           # 15 мин — backstop-перестройка


def _rebuild_sched_index() -> None:
    """Скан всех org DB → построение индекса расписания уведомлений.
    Синхронная (вызывать через asyncio.to_thread). Никогда не бросает наружу."""
    global _sched_index, _sched_index_ts
    new_index: dict = {nt: {} for nt in _SCHED_NOTIF_TYPES}
    # db_paths за пределами per-path try: фатальная ошибка здесь → raise,
    # чтобы _ensure_sched_index() оставил прежний индекс и пометил dirty для ретрая.
    db_paths = _get_scheduler_db_paths()
    for path in db_paths:
        try:
            cur = Database(path)
            tz_cache: dict = {}  # telegram_id → tz_name (в пределах одной БД)
            for nt in _SCHED_NOTIF_TYPES:
                try:
                    users = cur.get_users_for_notifications(nt)
                except Exception:
                    continue
                for u in users:
                    try:
                        user_id, telegram_id, first_name, shop_name, threshold, ntime = u
                    except Exception:
                        continue
                    if not telegram_id or not ntime:
                        continue
                    tz_name = tz_cache.get(telegram_id)
                    if tz_name is None:
                        try:
                            tz_name = cur.get_user_timezone(telegram_id) or 'UTC'
                        except Exception:
                            tz_name = 'UTC'
                        tz_cache[telegram_id] = tz_name
                    key = (ntime, tz_name)
                    new_index[nt].setdefault(key, []).append(
                        (path, user_id, telegram_id, first_name, shop_name, threshold)
                    )
        except Exception as e:
            logging.error(f"_rebuild_sched_index: БД {path}: {e}")
    # Публикуем индекс атомарной переподвязкой ссылок (не трогаем dirty —
    # им владеет _ensure_sched_index, иначе инвалидация во время сборки теряется).
    _sched_index = new_index
    _sched_index_ts = _time.monotonic()


async def _ensure_sched_index() -> None:
    """Перестроить индекс при необходимости (dirty / не строился / истёк TTL).
    dirty снимается ДО сборки: инвалидация во время сборки снова поставит dirty,
    и следующий цикл перестроит индекс (не теряем изменения настроек)."""
    global _sched_index_dirty
    need = (
        _sched_index_dirty
        or _sched_index_ts == 0.0
        or (_time.monotonic() - _sched_index_ts > _SCHED_INDEX_TTL)
    )
    if not need:
        return
    _sched_index_dirty = False  # снять ДО сборки — инвалидация во время неё переставит флаг
    try:
        await asyncio.to_thread(_rebuild_sched_index)
    except Exception as e:
        _sched_index_dirty = True  # сборка упала → повторить в следующем цикле, прежний индекс жив
        logging.error(f"_ensure_sched_index: {e}")


def _get_sched_hits(notif_type: str, current_utc) -> list:
    """Вернуть entries, у которых (hhmm, tz) совпадает с current_utc.
    O(уникальных (hhmm,tz) пар) — без открытия БД."""
    type_idx = _sched_index.get(notif_type)
    if not type_idx:
        return []
    hits: list = []
    for (hhmm, tz_name), entries in type_idx.items():
        try:
            if current_utc.astimezone(pytz.timezone(tz_name)).strftime('%H:%M') == hhmm:
                hits.extend(entries)
        except Exception:
            continue
    return hits


def _invalidate_sched_index() -> None:
    """Пометить индекс «грязным» — следующий минутный джоб перестроит его.
    Безопасно вызывать из любого потока/контекста (атомарная установка флага)."""
    global _sched_index_dirty
    _sched_index_dirty = True


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

        await _ensure_sched_index()
        shop_bot_db = Database('data/shop_bot.db')
        current_utc = datetime.now(pytz.UTC)
        hits = _get_sched_hits('payment', current_utc)
        db_by_path: dict = {}

        for entry in hits:
            await asyncio.sleep(0)  # уступаем event loop между пользователями
            try:
                path, user_id, telegram_id, first_name, shop_name, threshold = entry

                if not telegram_id:
                    continue

                current_db = db_by_path.get(path)
                if current_db is None:
                    current_db = Database(path)
                    db_by_path[path] = current_db

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
                logging.error(f"send_payment_alerts: пропуск записи: {e}")
                continue
    except Exception as e:
        logging.error(f"Ошибка в send_payment_alerts: {e}")

async def send_sales_alerts(bot: Bot):
    """Отправка уведомлений о новых продажах по всем организациям"""
    try:
        from datetime import datetime, timedelta
        import pytz

        await _ensure_sched_index()
        current_utc = datetime.now(pytz.UTC)
        hits = _get_sched_hits('sales', current_utc)
        db_by_path: dict = {}

        for entry in hits:
            await asyncio.sleep(0)
            try:
                path, user_id, telegram_id, first_name, shop_name, threshold = entry
                if not telegram_id: continue

                current_db = db_by_path.get(path)
                if current_db is None:
                    current_db = Database(path)
                    db_by_path[path] = current_db

                # Gate: smart_alerts extension required per user
                try:
                    from billing_utils import has_extension as _hex_sa
                    if not _hex_sa(int(telegram_id), "smart_alerts"):
                        continue
                except Exception:
                    pass

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
            except Exception as e:
                logging.error(f"send_sales_alerts: пропуск записи: {e}")
                continue
    except Exception as e:
        logging.error(f"Error in send_sales_alerts: {e}")

async def send_personalized_notifications(bot: Bot):
    """Отправка уведомлений о низких остатках по всем организациям"""
    try:
        from datetime import datetime
        import pytz

        await _ensure_sched_index()
        current_utc = datetime.now(pytz.UTC)
        hits = _get_sched_hits('low_stock', current_utc)
        db_by_path: dict = {}

        for entry in hits:
            await asyncio.sleep(0)
            try:
                path, user_id, telegram_id, first_name, shop_name, threshold = entry
                if not telegram_id: continue

                current_db = db_by_path.get(path)
                if current_db is None:
                    current_db = Database(path)
                    db_by_path[path] = current_db

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

                    # Авто-задачи при низком остатке (tasks_pro, если включено)
                    try:
                        from billing_utils import has_module as _has_module
                        ns = await asyncio.to_thread(current_db.get_notification_settings, user_id)
                        if ns.get('auto_tasks_low_stock') and _has_module(telegram_id, 'tasks_pro'):
                            for item in low_stock_items[:20]:
                                _pname, _qty = item[0], item[1]
                                _shop = item[2] if len(item) > 2 else (shop_name or '')
                                await asyncio.to_thread(
                                    current_db.create_auto_low_stock_task,
                                    _pname, _shop or '', int(_qty or 0), user_id
                                )
                    except Exception as _at_err:
                        logging.debug("auto_tasks_low_stock: %s", _at_err)
            except Exception as e:
                logging.error(f"send_personalized_notifications: пропуск записи: {e}")
                continue
    except Exception as e:
        logging.error(f"Error in send_personalized_notifications: {e}")

async def send_daily_reports(bot: Bot):
    """Отправка ежедневных отчетов по всем организациям"""
    try:
        from datetime import datetime, timedelta
        import pytz
        from db_utils import is_any_admin
        from dashboard_handlers import build_admin_daily_text, build_user_daily_text

        await _ensure_sched_index()
        current_utc = datetime.now(pytz.UTC)
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        hits = _get_sched_hits('daily_report', current_utc)
        db_by_path: dict = {}

        for entry in hits:
            await asyncio.sleep(0)
            try:
                path, user_id, telegram_id, first_name, shop_name, threshold = entry
                if not telegram_id:
                    continue

                current_db = db_by_path.get(path)
                if current_db is None:
                    current_db = wrap_db(Database(path))
                    db_by_path[path] = current_db

                # Для администраторов: один магический код на весь блок сообщения.
                # Используется и для seller deep-links внутри текста, и для кнопки.
                _admin_staff_link_base = None
                _admin_button_url = None
                if is_any_admin(telegram_id):
                    try:
                        from keyboards import _get_web_interface_url as _gwiu_dr
                        from urllib.parse import quote as _uq_dr
                        _wu_dr = _gwiu_dr()
                        if _wu_dr:
                            from web_login_codes import generate_code as _gc_dr
                            _code_dr = _gc_dr(telegram_id)
                            _base_dr = f"{_wu_dr.rstrip('/')}/auth/code/auto?c={_code_dr}&next="
                            _admin_staff_link_base = _base_dr
                            _nxt_dr = f"/reports?period=custom&date_from={yesterday}&date_to={yesterday}"
                            _admin_button_url = f"{_base_dr}{_uq_dr(_nxt_dr, safe='')}"
                    except Exception:
                        pass

                try:
                    if is_any_admin(telegram_id):
                        from db_utils import get_user_org_scope
                        _sct, _scv = get_user_org_scope(telegram_id)
                        message = await build_admin_daily_text(
                            current_db, yesterday, shop_name,
                            scope_type=_sct, scope_values=_scv,
                            staff_link_base=_admin_staff_link_base,
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
                    _daily_markup = add_read_btn()
                    if _admin_button_url:
                        try:
                            from aiogram.types import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
                            _daily_markup = add_read_btn(_IKM(inline_keyboard=[
                                [_IKB(text="🌐 Отчёт в вебе", url=_admin_button_url)]
                            ]))
                        except Exception:
                            pass
                    await bot.send_message(telegram_id, message, parse_mode="HTML", reply_markup=_daily_markup)
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
            except Exception as e:
                logging.error(f"send_daily_reports: пропуск записи: {e}")
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
                        # Filter out users on approved leave at send time
                        try:
                            _today_str = now_utc.strftime("%Y-%m-%d")
                            _absent_ids = await asyncio.to_thread(
                                send_db.get_absent_user_ids_today, _today_str
                            )
                            if _absent_ids:
                                recipients = [r for r in recipients if r[0] not in _absent_ids]
                        except Exception:
                            pass
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


async def send_weekly_ranking_notification(bot: Bot):
    """Еженедельный рейтинг топ-продавцов по всем организациям (понедельник, 09:00 UTC)."""
    try:
        import datetime as _dt
        from db_utils import is_any_admin

        today = _dt.date.today()
        week_start = (today - _dt.timedelta(days=7)).isoformat()
        week_end = today.isoformat()
        label = f"{(today - _dt.timedelta(days=7)).strftime('%d.%m')}–{today.strftime('%d.%m.%Y')}"

        db_paths = _get_scheduler_db_paths()
        for path in db_paths:
            await asyncio.sleep(0)
            current_db = Database(path)
            try:
                admin_ids = current_db.get_all_admins_telegram_ids()
                if not admin_ids:
                    continue

                ranking = current_db.get_sales_ranking(start_date=week_start, end_date=week_end)
                if not ranking:
                    continue

                medals = ["🥇", "🥈", "🥉"]
                text = f"🏆 <b>Еженедельный рейтинг продавцов</b>\n📅 {label}\n\n"
                for i, row in enumerate(ranking[:5]):
                    fn, ln, sn, qty, revenue = row[0], row[1], row[2], row[3], row[4]
                    medal = medals[i] if i < 3 else f"{i + 1}."
                    name = f"{he(fn or '')} {he(ln or '')}".strip() or "—"
                    text += f"{medal} <b>{name}</b>\n"
                    if sn:
                        text += f"   🏪 {he(sn)}\n"
                    text += f"   📦 {qty} шт. · 💰 {revenue:,.0f} ₽\n\n"

                for tg_id in admin_ids:
                    if not tg_id or int(tg_id) <= 0:
                        continue
                    try:
                        _markup = add_read_btn()
                        if is_any_admin(int(tg_id)):
                            try:
                                from keyboards import _get_web_interface_url as _gwiu_wr
                                from urllib.parse import quote as _uq_wr
                                from aiogram.types import InlineKeyboardMarkup as _IKM_wr, InlineKeyboardButton as _IKB_wr
                                _wu_wr = _gwiu_wr()
                                if _wu_wr:
                                    from web_login_codes import generate_code as _gc_wr
                                    _code_wr = _gc_wr(int(tg_id))
                                    _nxt_wr = f"/rankings?tab=sellers&period=custom&date_from={week_start}&date_to={week_end}"
                                    _lurl_wr = f"{_wu_wr.rstrip('/')}/auth/code/auto?c={_code_wr}&next={_uq_wr(_nxt_wr, safe='')}"
                                    _markup = add_read_btn(_IKM_wr(inline_keyboard=[
                                        [_IKB_wr(text="🌐 Рейтинг в вебе", url=_lurl_wr)]
                                    ]))
                            except Exception:
                                pass
                        await bot.send_message(int(tg_id), text, parse_mode="HTML", reply_markup=_markup)
                        await asyncio.sleep(0.05)
                    except Exception as _send_err:
                        logging.warning(f"send_weekly_ranking_notification: skip {tg_id}: {_send_err}")
            except Exception:
                continue
    except Exception as e:
        logging.error(f"Error in send_weekly_ranking_notification: {e}")


async def send_monthly_ranking_notification(bot: Bot):
    """Ежемесячный рейтинг топ-продавцов за прошлый месяц (1-е числа, 09:05 UTC)."""
    try:
        import datetime as _dt
        from db_utils import is_any_admin

        today = _dt.date.today()
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - _dt.timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        month_start = last_month_start.isoformat()
        month_end = last_month_end.isoformat()
        months_ru = {
            1: "январь", 2: "февраль", 3: "март", 4: "апрель",
            5: "май", 6: "июнь", 7: "июль", 8: "август",
            9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
        }
        label = f"{months_ru.get(last_month_start.month, '')} {last_month_start.year}"

        db_paths = _get_scheduler_db_paths()
        for path in db_paths:
            await asyncio.sleep(0)
            current_db = Database(path)
            try:
                admin_ids = current_db.get_all_admins_telegram_ids()
                if not admin_ids:
                    continue

                ranking = current_db.get_sales_ranking(start_date=month_start, end_date=month_end)
                if not ranking:
                    continue

                medals = ["🥇", "🥈", "🥉"]
                text = f"🏆 <b>Рейтинг продавцов за {he(label)}</b>\n\n"
                for i, row in enumerate(ranking[:5]):
                    fn, ln, sn, qty, revenue = row[0], row[1], row[2], row[3], row[4]
                    medal = medals[i] if i < 3 else f"{i + 1}."
                    name = f"{he(fn or '')} {he(ln or '')}".strip() or "—"
                    text += f"{medal} <b>{name}</b>\n"
                    if sn:
                        text += f"   🏪 {he(sn)}\n"
                    text += f"   📦 {qty} шт. · 💰 {revenue:,.0f} ₽\n\n"

                for tg_id in admin_ids:
                    if not tg_id or int(tg_id) <= 0:
                        continue
                    try:
                        _markup = add_read_btn()
                        if is_any_admin(int(tg_id)):
                            try:
                                from keyboards import _get_web_interface_url as _gwiu_mr
                                from urllib.parse import quote as _uq_mr
                                from aiogram.types import InlineKeyboardMarkup as _IKM_mr, InlineKeyboardButton as _IKB_mr
                                _wu_mr = _gwiu_mr()
                                if _wu_mr:
                                    from web_login_codes import generate_code as _gc_mr
                                    _code_mr = _gc_mr(int(tg_id))
                                    _nxt_mr = f"/rankings?tab=sellers&period=custom&date_from={month_start}&date_to={month_end}"
                                    _lurl_mr = f"{_wu_mr.rstrip('/')}/auth/code/auto?c={_code_mr}&next={_uq_mr(_nxt_mr, safe='')}"
                                    _markup = add_read_btn(_IKM_mr(inline_keyboard=[
                                        [_IKB_mr(text="🌐 Рейтинг в вебе", url=_lurl_mr)]
                                    ]))
                            except Exception:
                                pass
                        await bot.send_message(int(tg_id), text, parse_mode="HTML", reply_markup=_markup)
                        await asyncio.sleep(0.05)
                    except Exception as _send_err:
                        logging.warning(f"send_monthly_ranking_notification: skip {tg_id}: {_send_err}")
            except Exception:
                continue
    except Exception as e:
        logging.error(f"Error in send_monthly_ranking_notification: {e}")


async def send_appointment_reminders(bot: Bot):
    """Напоминания о записях за 1 час до начала."""
    try:
        from database import Database
        from tenant_manager import tenant_manager
        import glob as _glob
        from datetime import datetime as _dt, timedelta as _td

        now = _dt.utcnow()
        window_start = (now + _td(minutes=55)).strftime("%Y-%m-%d %H:%M")
        window_end   = (now + _td(minutes=65)).strftime("%Y-%m-%d %H:%M")

        org_dbs = _glob.glob("data/tenants/org_*.db")
        for db_path in org_dbs:
            try:
                db = Database(db_path)
                conn = db.get_connection()
                rows = conn.execute(
                    "SELECT a.id, sv.name, c.first_name||' '||c.last_name, c.phone, "
                    "u.telegram_id, a.start_time "
                    "FROM appointments a "
                    "LEFT JOIN services sv ON a.service_id=sv.id "
                    "LEFT JOIN clients c ON a.client_id=c.id "
                    "LEFT JOIN users u ON a.staff_user_id=u.id "
                    "WHERE a.status IN ('planned','confirmed') "
                    "AND a.start_time BETWEEN ? AND ?",
                    (window_start, window_end),
                ).fetchall()
                conn.close()
                for r in rows:
                    appt_id, svc_name, client_name, phone, staff_tg, start = r
                    if not staff_tg:
                        continue
                    try:
                        from utils import he as _he
                        await bot.send_message(
                            chat_id=staff_tg,
                            text=(
                                f"⏰ <b>Напоминание о записи</b>\n\n"
                                f"Через ~1 час: <b>{_he(svc_name or '—')}</b>\n"
                                f"Клиент: {_he((client_name or '—').strip())}"
                                + (f" · {_he(phone)}" if phone else "") + f"\n"
                                f"Время: {(start or '')[:16]}"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception as e:
        logging.error(f"send_appointment_reminders: {e}")


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

    # Напоминания о записях (Услуги) — каждые 5 минут
    scheduler.add_job(
        send_appointment_reminders,
        CronTrigger(minute='*/5', second=30),
        args=[bot],
        id='send_appointment_reminders',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )

    # APScheduler inline-джобы вынесены в jobs/registry.py (roadmap 1.4)
    register_inline_jobs(
        scheduler,
        bot,
        daily_backup_task,
        _get_scheduler_db_paths,
        ADMIN_CHAT_ID,
        send_trial_expired_upsell,
        send_weekly_ranking_notification,
        send_monthly_ranking_notification,
        auto_finish_contests,
        auto_reject_stale_payments,
    )

    scheduler.start()

    # Регистрируем cron-задачи из интеграций Google Sheets
    try:
        from integration.manager import integration_manager
        await integration_manager.schedule_exports(scheduler, _get_scheduler_db_paths)
    except Exception as _ie:
        logging.warning(f"Integration schedule_exports: {_ie}")

    # Регистрируем cron-задачи автосинка мотивации (для подключений с auto_sync=true)
    try:
        from integration.manager import integration_manager
        await integration_manager.schedule_motiv_syncs(scheduler, _get_scheduler_db_paths)
    except Exception as _mse:
        logging.warning(f"Integration schedule_motiv_syncs: {_mse}")

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
