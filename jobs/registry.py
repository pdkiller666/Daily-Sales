"""
jobs/registry.py — APScheduler inline-джобы, вынесенные из main.py (roadmap 1.4).

Все джобы, которые исторически объявлялись прямо внутри main(), собраны здесь
в функцию register_inline_jobs(). Поведение идентично: тот же код, те же триггеры.
Замыкания на main.py переданы явными аргументами (bot, daily_backup_task,
_get_scheduler_db_paths, ADMIN_CHAT_ID) во избежание циклического импорта.
"""
import os
import re
import asyncio
import logging

from database import Database
from notif_utils import add_read_btn
from backup_manager import BackupManager
from apscheduler.triggers.cron import CronTrigger


def register_inline_jobs(
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
):
    """Регистрирует все inline-джобы в переданном scheduler.

    Вызывается из main() ПОСЛЕ создания scheduler и регистрации
    импортируемых джобов, но ДО scheduler.start().

    Аргументы-функции (send_*, auto_*, daily_backup_task) — это
    модульные джобы, объявленные в main.py; передаются явно, чтобы
    избежать циклического импорта.
    """
    async def shift_start_notifier():
        """Отправляет Telegram + Web Push сотруднику ровно в момент начала его смены.
        Запускается каждую минуту; деdup через notification_history (тип shift_start).
        """
        try:
            from datetime import datetime
            import pytz
            from zoneinfo import ZoneInfo

            now_utc = datetime.now(pytz.UTC)
            today_utc = now_utc.strftime('%Y-%m-%d')

            db_paths = _get_scheduler_db_paths()
            for path in db_paths:
                await asyncio.sleep(0)
                # shop_bot.db не содержит расписаний смен — пропускаем
                if path == 'data/shop_bot.db':
                    continue
                if not os.path.exists(path):
                    continue
                current_db = Database(path)
                try:
                    # Собираем все уникальные часовые пояса пользователей в этой орг
                    conn = await asyncio.to_thread(current_db.get_connection)
                    tz_rows = conn.execute(
                        "SELECT DISTINCT timezone FROM users WHERE timezone IS NOT NULL AND timezone != ''"
                    ).fetchall() or []
                    conn.close()
                    if not tz_rows:
                        continue

                    # Для каждого уникального часового пояса — вычисляем локальное время
                    # и строим remind_map для всех возможных значений shift_remind_minutes
                    processed_uids: set = set()
                    for (tz_str,) in tz_rows:
                        try:
                            local_now = now_utc.astimezone(ZoneInfo(tz_str))
                        except Exception:
                            continue

                        # remind_map: {minutes_offset: (target_time_str, target_date_str)}
                        # 0 = точно в момент начала, 15/30/60 = за N минут до
                        from datetime import timedelta as _td
                        remind_map = {}
                        for _offset in (0, 15, 30, 60):
                            _target = local_now + _td(minutes=_offset)
                            remind_map[_offset] = (
                                _target.strftime('%H:%M'),
                                _target.strftime('%Y-%m-%d'),
                            )

                        shifts = await asyncio.to_thread(
                            current_db.get_shifts_for_reminders, remind_map
                        )
                        for user_id, telegram_id, start_time, end_time, remind_min in shifts:
                            if not telegram_id or user_id in processed_uids:
                                continue
                            # Проверяем, что timezone пользователя совпадает с tz_str
                            user_tz = await asyncio.to_thread(current_db.get_user_timezone, telegram_id)
                            if user_tz != tz_str:
                                continue

                            processed_uids.add(user_id)

                            # Dedup: проверяем, не было ли уже отправлено сегодня
                            _target_date = remind_map[remind_min][1]
                            try:
                                _conn = await asyncio.to_thread(current_db.get_connection)
                                _already = _conn.execute(
                                    """SELECT 1 FROM notification_history
                                       WHERE user_id = ? AND notification_type = 'shift_start'
                                         AND created_at >= ?""",
                                    (user_id, _target_date + ' 00:00:00')
                                ).fetchone()
                                _conn.close()
                                if _already:
                                    continue
                            except Exception:
                                pass

                            # Формируем текст уведомления
                            if remind_min > 0:
                                _remind_label = f"{remind_min} мин." if remind_min < 60 else "1 час"
                                if end_time:
                                    text = f"⏰ <b>Смена через {_remind_label}</b>\n{start_time} → {end_time}"
                                else:
                                    text = f"⏰ <b>Смена через {_remind_label}</b>\nНачало: {start_time}"
                                push_title = f"⏰ Смена через {_remind_label}"
                            else:
                                if end_time:
                                    text = f"🕐 <b>Твоя смена сегодня</b>\n{start_time} → {end_time}"
                                else:
                                    text = f"🕐 <b>Твоя смена сегодня</b>\nНачало: {start_time}"
                                push_title = "🕐 Начало смены"

                            try:
                                await bot.send_message(
                                    int(telegram_id), text,
                                    parse_mode="HTML",
                                    reply_markup=add_read_btn(),
                                )
                                await asyncio.sleep(0.05)
                                await asyncio.to_thread(
                                    current_db.add_notification_to_history,
                                    user_id, 'shift_start', text
                                )
                                try:
                                    from web.push_utils import apush
                                    await apush(int(telegram_id), push_title, re.sub(r'<[^>]+>', '', text).strip(), "/schedule")
                                except Exception:
                                    pass
                            except Exception as _send_err:
                                logging.warning(f"shift_start_notifier: skip {telegram_id}: {_send_err}")
                except Exception as _db_err:
                    logging.error(f"shift_start_notifier db={path}: {_db_err}")
        except Exception as _e:
            logging.error(f"shift_start_notifier: {_e}")

    scheduler.add_job(
        shift_start_notifier,
        CronTrigger(minute='*', second=42),
        id='shift_start_notifier',
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

    # Еженедельный рейтинг продавцов — каждый понедельник в 09:00 UTC
    scheduler.add_job(
        send_weekly_ranking_notification,
        CronTrigger(day_of_week='mon', hour=9, minute=0),
        args=[bot],
        id='weekly_ranking_notification',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Ежемесячный рейтинг продавцов — 1-го числа каждого месяца в 09:05 UTC
    scheduler.add_job(
        send_monthly_ranking_notification,
        CronTrigger(day=1, hour=9, minute=5),
        args=[bot],
        id='monthly_ranking_notification',
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
            # Также чистим ai_org_usage_log (per-org chat quota) — тот же период
            try:
                _cur2 = _conn.execute(
                    "DELETE FROM ai_org_usage_log WHERE usage_date < date('now', ?)",
                    (f'-{_retention_days} days',)
                )
                _deleted_org = _cur2.rowcount
            except Exception:
                _deleted_org = 0
            _conn.commit()
            _conn.close()
            logging.info(
                "prune_ai_usage_log: удалено %d строк (usage_log) + %d (org_usage_log) старше %d дней",
                _deleted, _deleted_org, _retention_days,
            )
            try:
                from web.rate_store import prune_ai_history_logs as _prune_hist
                _prune_hist(30)
            except Exception as _ph_err:
                logging.debug("prune_ai_history_logs: %s", _ph_err)
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

    # Очистка старых записей ai_cost_log — ежедневно в 03:35 UTC (хранить 90 дней)
    _AI_COST_LOG_RETENTION_DAYS = 90

    async def prune_ai_cost_log_job():
        try:
            import sqlite3 as _sqlite3
            _db_path = 'data/rate_limits.db'
            if not os.path.exists(_db_path):
                return
            _conn = _sqlite3.connect(_db_path, timeout=5, check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            # ai_cost_log может не существовать на старых инстансах — игнорируем
            try:
                _cur = _conn.execute(
                    "DELETE FROM ai_cost_log WHERE date < date('now', ?)",
                    (f'-{_AI_COST_LOG_RETENTION_DAYS} days',)
                )
                _deleted = _cur.rowcount
                _conn.commit()
                logging.info("prune_ai_cost_log: удалено %d строк старше %d дней", _deleted, _AI_COST_LOG_RETENTION_DAYS)
            except Exception:
                pass
            finally:
                _conn.close()
        except Exception as _e:
            logging.error("prune_ai_cost_log_job error: %s", _e)

    scheduler.add_job(
        prune_ai_cost_log_job,
        CronTrigger(hour=3, minute=35),
        id='prune_ai_cost_log',
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

    # Retention чата — ежедневно в 03:45 UTC
    async def prune_chat_messages_job():
        """Удаляет сообщения чата и ЛС старше chat_retention_days дней во всех орг-БД.
        Если chat_retention_days = 0 (или не задано) — ничего не делает."""
        try:
            from database import Database as _Database
            import glob as _glob
            _shop_db = _Database('data/shop_bot.db')
            _settings = _shop_db.get_payment_settings()
            try:
                _days = int(_settings.get('chat_retention_days', '0'))
            except (ValueError, TypeError):
                _days = 0
            if _days < 7:
                return  # 0 = отключено; < 7 — не трогаем (минимум неделя)
            _tenant_dir = 'data/tenants'
            _total_chat = 0
            _total_dm = 0
            _org_count = 0
            for _path in sorted(_glob.glob(os.path.join(_tenant_dir, 'org_*.db'))):
                try:
                    _org_db = _Database(_path)
                    _res = _org_db.prune_old_chat_messages(_days)
                    _total_chat += _res.get('chat', 0)
                    _total_dm   += _res.get('dm', 0)
                    _org_count  += 1
                except Exception as _oe:
                    logging.warning('prune_chat_messages_job[%s]: %s', _path, _oe)
            logging.info(
                'prune_chat: %d орг, удалено %d сообщений + %d ЛС старше %d дней',
                _org_count, _total_chat, _total_dm, _days,
            )
        except Exception as _e:
            logging.error('prune_chat_messages_job error: %s', _e)

    scheduler.add_job(
        prune_chat_messages_job,
        CronTrigger(hour=3, minute=45),
        id='prune_chat_messages',
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
        import datetime as _dt
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

        def _local_today(tz_str: str) -> str:
            """Текущая дата в заданной timezone (YYYY-MM-DD)."""
            try:
                from zoneinfo import ZoneInfo
                return _dt.datetime.now(ZoneInfo(tz_str)).date().isoformat()
            except Exception:
                return _dt.date.today().isoformat()

        async def _notify_team(db, task, notif_type: str, text: str, push_title: str):
            """Уведомить всех участников командной задачи (assign_all / assigned_shop)."""
            try:
                if task.get('assign_all'):
                    tg_ids = db.get_org_all_member_tg_ids()
                elif task.get('assigned_shop'):
                    tg_ids = db.get_org_shop_member_tg_ids(task['assigned_shop'])
                else:
                    return
                title = task.get('title', '—')
                seen = set()
                for _tg in tg_ids:
                    if not _tg or _tg in seen:
                        continue
                    seen.add(_tg)
                    _push(_tg, text)
                    try:
                        _conn = db.get_connection()
                        _row = _conn.execute(
                            "SELECT id FROM users WHERE telegram_id = ?", (_tg,)
                        ).fetchone()
                        _conn.close()
                        if _row:
                            db.add_notification_to_history(
                                _row[0], notif_type, f"{push_title}: {title}")
                    except Exception:
                        pass
                    try:
                        from web.push_utils import send_web_push
                        await asyncio.to_thread(
                            send_web_push, int(_tg), push_title, title, "/tasks")
                    except Exception:
                        pass
            except Exception as _te:
                logging.error(f"check_task_deadlines _notify_team: {_te}")

        try:
            from database import Database
            db_paths = _get_scheduler_db_paths()
            for db_path in db_paths:
                try:
                    db = Database(db_path)
                    # Определить локальную дату из timezone владельца org
                    _tz = db.get_org_owner_timezone()
                    _today = _local_today(_tz)
                    _yesterday = (
                        _dt.date.fromisoformat(_today) - _dt.timedelta(days=1)
                    ).isoformat()

                    # Дедлайн сегодня — Telegram + колокольчик + Web Push
                    for t in db.get_tasks_with_deadline_today(local_date=_today):
                        title = t.get('title', '—')
                        _is_team = t.get('assign_all') or t.get('assigned_shop')
                        if _is_team:
                            await _notify_team(
                                db, t, 'task_deadline',
                                f"📋 <b>Срок задачи сегодня!</b>\n<b>{title}</b>\n\n"
                                f"🌐 Откройте веб-кабинет для деталей.",
                                "📋 Срок задачи сегодня"
                            )
                        else:
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
                                    await asyncio.to_thread(
                                        send_web_push, int(t['assigned_tg']),
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
                                await asyncio.to_thread(
                                    send_web_push, int(t['creator_tg']),
                                    "📋 Срок задачи сегодня", title, "/tasks")
                            except Exception:
                                pass

                    # Задачи, ставшие просроченными вчера — по 1 уведомлению на задачу
                    for t in db.get_overdue_tasks(local_date=_today):
                        if t.get('deadline') != _yesterday:
                            continue
                        title = t.get('title', '—')
                        _is_team = t.get('assign_all') or t.get('assigned_shop')
                        if _is_team:
                            await _notify_team(
                                db, t, 'task_overdue',
                                f"⚠️ <b>Задача просрочена!</b>\n<b>{title}</b>\n\n"
                                f"🌐 Откройте веб-кабинет.",
                                "⚠️ Задача просрочена"
                            )
                        else:
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
                                    await asyncio.to_thread(
                                        send_web_push, int(t['assigned_tg']),
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
                                await asyncio.to_thread(
                                    send_web_push, int(t['creator_tg']),
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

    # Напоминания о задачах — каждую минуту
    async def check_task_reminders():
        """Отправляет напоминания о задачах по расписанию пользователей."""
        import os as _os
        import json as _json
        import urllib.request as _ureq
        import threading as _th
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        def _push(tg_id, text, task_id):
            if not tg_id:
                return
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": tg_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": {
                        "inline_keyboard": [[{"text": "📋 Открыть задачу", "callback_data": f"task_open_{task_id}"}]]
                    },
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                _th.Thread(target=lambda: _ureq.urlopen(req, timeout=10), daemon=True).start()
            except Exception:
                pass

        try:
            from database import Database
            _db_paths = _get_scheduler_db_paths()
            for _db_path in _db_paths:
                try:
                    _db = Database(_db_path)
                    for rem in _db.get_due_task_reminders():
                        _title = rem['title']
                        _tg_id = rem['telegram_id']
                        _task_id = rem['task_id']
                        _user_id = rem['user_id']
                        _task_status = rem.get('status', '')
                        if _task_status in ('done', 'cancelled'):
                            _db.mark_task_reminder_sent(rem['id'])
                            continue
                        msg = (
                            f"⏰ <b>Напоминание о задаче</b>\n\n"
                            f"📋 {_title}\n\n"
                            f"Вы установили напоминание об этой задаче."
                        )
                        _push(_tg_id, msg, _task_id)
                        try:
                            _db.add_notification_to_history(
                                _user_id, 'task_reminder',
                                f"⏰ Напоминание: {_title}")
                        except Exception:
                            pass
                        try:
                            from web.push_utils import send_web_push
                            await asyncio.to_thread(
                                send_web_push, int(_tg_id),
                                "⏰ Напоминание о задаче", _title, "/tasks"
                            )
                        except Exception:
                            pass
                        _db.mark_task_reminder_sent(rem['id'])
                except Exception as _de:
                    logging.error(f"check_task_reminders db={_db_path}: {_de}")
        except Exception as _e:
            logging.error(f"check_task_reminders: {_e}")

    scheduler.add_job(
        check_task_reminders,
        CronTrigger(minute='*', second=30),
        id='check_task_reminders',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # SLA-контроль задач + дедлайн-правила автоматизаций — каждый час
    async def check_task_sla():
        """Пересчёт SLA-статусов, авто-эскалация нарушителей руководителю и
        срабатывание правил deadline_approaching/deadline_passed. Идемпотентно."""
        try:
            from database import Database
            from task_automation import sweep_sla_for_db
        except Exception as _imp:
            logging.error("check_task_sla import: %s", _imp)
            return
        try:
            _db_paths = _get_scheduler_db_paths()
            for _db_path in _db_paths:
                try:
                    _db = Database(_db_path)
                    # тяжёлый синхронный проход — не держим event loop
                    await asyncio.to_thread(sweep_sla_for_db, _db, logging)
                except Exception as _de:
                    logging.error("check_task_sla db=%s: %s", _db_path, _de)
        except Exception as _e:
            logging.error("check_task_sla: %s", _e)

    scheduler.add_job(
        check_task_sla,
        CronTrigger(minute=5),
        id='check_task_sla',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
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
                ai_text = await ask_llm(prompt, system=system, max_tokens=400, feature="digest")
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

                    # Root-cause: разбивка за вчера по категориям и продавцам + дневной тренд за 7 дней
                    try:
                        _cat_breakdown_y = db.get_sales_by_category_for_period(yesterday, yesterday)
                    except Exception:
                        _cat_breakdown_y = []
                    try:
                        _sel_breakdown_y = db.get_sales_by_seller_for_period(yesterday, yesterday)
                    except Exception:
                        _sel_breakdown_y = []
                    try:
                        _daily_breakdown_7d = db.get_daily_sales_for_period(week_ago, yesterday)
                    except Exception:
                        _daily_breakdown_7d = []

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
                                category_breakdown=_cat_breakdown_y or None,
                                seller_breakdown=_sel_breakdown_y or None,
                                daily_breakdown=_daily_breakdown_7d or None,
                            )
                            if extra_lines:
                                prompt += "\nДополнительно: " + " ".join(extra_lines)
                            ai_text = await ask_llm(prompt, max_tokens=350, feature="alerts")
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
                    if ai_text:
                        try:
                            from web.rate_store import log_ai_digest as _log_dg
                            import json as _jdg
                            _snap = _jdg.dumps({"org": org_name, "y_rev": round(y_rev), "avg_7d": round(avg_7d), "drop_pct": round(drop_pct, 1)}, ensure_ascii=False)
                            _log_dg("smart_alerts", db_path, ai_text, _snap)
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

    # ─── Phase 3.5: AI-предиктор просрочки задач — ежедневно в 11:00 UTC ─────
    async def ai_task_overdue_predictor():
        """AI-анализ задач с близким дедлайном (48ч): шлёт предупреждение admin."""
        try:
            from web.ai_utils import ask_llm, is_configured
            from billing_utils import has_module as _hm, has_extension as _he
            from database import Database
            import datetime as _dt, os as _os, json as _json, urllib.request as _ureq, threading as _th
        except Exception as _ie:
            logging.warning("ai_task_overdue_predictor: import error: %s", _ie)
            return

        if not is_configured():
            return

        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        def _push(tg_id, text):
            if not tg_id or int(tg_id) <= 0:
                return
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": tg_id, "text": text, "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": [[{"text": "✅ Прочитано", "callback_data": "notif_read"}]]}
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                _th.Thread(target=lambda: _ureq.urlopen(req, timeout=10), daemon=True).start()
            except Exception:
                pass

        try:
            now = _dt.datetime.utcnow()
            horizon = (now + _dt.timedelta(hours=48)).date().isoformat()
            today = now.date().isoformat()
            db_paths = _get_scheduler_db_paths()
            for db_path in db_paths:
                try:
                    db = Database(db_path)
                    admin_ids = db.get_all_admins_telegram_ids()
                    if not admin_ids:
                        continue
                    # Billing gate: tasks_ai extension on any admin
                    try:
                        if not any(_hm(int(tid), 'tasks_pro') and _he(int(tid), 'tasks_ai')
                                   for tid in admin_ids[:3] if tid and int(tid) > 0):
                            continue
                    except Exception:
                        continue

                    conn = db.get_connection()
                    try:
                        at_risk = conn.execute("""
                            SELECT t.title, t.deadline, t.priority, u.first_name, u.last_name
                            FROM tasks t
                            LEFT JOIN users u ON u.id = t.assigned_to
                            WHERE t.status NOT IN ('done','cancelled')
                              AND t.deadline IS NOT NULL
                              AND t.deadline <= ?
                              AND t.deadline >= ?
                            ORDER BY t.deadline ASC LIMIT 10
                        """, (horizon, today)).fetchall()
                    finally:
                        conn.close()

                    if not at_risk:
                        continue

                    task_lines = "\n".join(
                        f"- {r[0]} (дедлайн: {r[1]}, приоритет: {r[2] or 'normal'}, "
                        f"исполнитель: {((r[3] or '') + ' ' + (r[4] or '')).strip() or 'не назначен'})"
                        for r in at_risk
                    )
                    prompt = (
                        f"Следующие задачи должны быть выполнены в ближайшие 48 часов:\n{task_lines}\n\n"
                        "Кратко (2-3 предложения) предупреди менеджера о риске просрочки "
                        "и дай 1-2 конкретные рекомендации. Без markdown."
                    )
                    system = "Ты — менеджер проектов розничного магазина. Пиши чётко, по-русски, без markdown."
                    ai_text = await ask_llm(prompt, system=system, max_tokens=200,
                                           temperature=0.3, feature="task_overdue_predict")
                    if ai_text:
                        try:
                            from web.rate_store import log_ai_digest as _log_dg
                            import json as _jdg
                            _snap = _jdg.dumps({"at_risk_count": len(at_risk)}, ensure_ascii=False)
                            _log_dg("task_overdue_predictor", db_path, ai_text, _snap)
                        except Exception:
                            pass
                    if not ai_text:
                        ai_text = f"⚠️ Через 48 часов истекает срок {len(at_risk)} задач."

                    msg = (
                        f"🔮 <b>AI-предиктор задач</b>\n\n"
                        f"Задач под риском просрочки: <b>{len(at_risk)}</b>\n\n"
                        f"{ai_text}"
                    )
                    for tid in admin_ids[:5]:
                        if tid and int(tid) > 0:
                            _push(int(tid), msg)
                            await asyncio.sleep(0.05)
                except Exception as _de:
                    logging.warning("ai_task_overdue_predictor db=%s: %s", db_path, _de)
        except Exception as _e:
            logging.error("ai_task_overdue_predictor: %s", _e)

    scheduler.add_job(
        ai_task_overdue_predictor,
        CronTrigger(hour=11, minute=0),
        id='ai_task_overdue_predictor',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # ─── Phase 3.6: AI-дайджест задач — еженедельно по вторникам в 08:00 UTC ─
    async def ai_task_digest():
        """Еженедельный AI-дайджест: статистика задач + AI-резюме для admin (tasks_ai extension)."""
        try:
            from web.ai_utils import ask_llm, is_configured
            from billing_utils import has_module as _hm, has_extension as _he
            from database import Database
            import datetime as _dt, os as _os, json as _json, urllib.request as _ureq, threading as _th
        except Exception as _ie:
            logging.warning("ai_task_digest: import error: %s", _ie)
            return

        if not is_configured():
            return

        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        def _push(tg_id, text):
            if not tg_id or int(tg_id) <= 0:
                return
            try:
                url = f"https://api.telegram.org/bot{_token}/sendMessage"
                payload = _json.dumps({
                    "chat_id": tg_id, "text": text, "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": [[{"text": "✅ Прочитано", "callback_data": "notif_read"}]]}
                }).encode()
                req = _ureq.Request(url, data=payload, headers={"Content-Type": "application/json"})
                _th.Thread(target=lambda: _ureq.urlopen(req, timeout=10), daemon=True).start()
            except Exception:
                pass

        try:
            today = _dt.date.today()
            week_ago = (today - _dt.timedelta(days=7)).isoformat()
            today_str = today.isoformat()
            db_paths = _get_scheduler_db_paths()
            for db_path in db_paths:
                try:
                    db = Database(db_path)
                    admin_ids = db.get_all_admins_telegram_ids()
                    if not admin_ids:
                        continue
                    try:
                        if not any(_hm(int(tid), 'tasks_pro') and _he(int(tid), 'tasks_ai')
                                   for tid in admin_ids[:3] if tid and int(tid) > 0):
                            continue
                    except Exception:
                        continue

                    conn = db.get_connection()
                    try:
                        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                        new_this_week = conn.execute(
                            "SELECT COUNT(*) FROM tasks WHERE created_at >= ?", (week_ago,)
                        ).fetchone()[0]
                        done_this_week = conn.execute(
                            "SELECT COUNT(*) FROM tasks WHERE status='done' AND updated_at >= ?", (week_ago,)
                        ).fetchone()[0]
                        overdue = conn.execute(
                            "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','cancelled') "
                            "AND deadline IS NOT NULL AND deadline < ?", (today_str,)
                        ).fetchone()[0]
                        in_progress = conn.execute(
                            "SELECT COUNT(*) FROM tasks WHERE status='in_progress'"
                        ).fetchone()[0]
                    finally:
                        conn.close()

                    if total == 0:
                        continue

                    prompt = (
                        f"Статистика задач за прошедшую неделю:\n"
                        f"- Всего задач: {total}\n"
                        f"- Новых за неделю: {new_this_week}\n"
                        f"- Выполнено за неделю: {done_this_week}\n"
                        f"- В работе сейчас: {in_progress}\n"
                        f"- Просрочено: {overdue}\n\n"
                        "Напиши краткое резюме (3-4 предложения) для менеджера: "
                        "как прошла неделя, что вызывает беспокойство, 1 совет. "
                        "Без markdown, по-русски."
                    )
                    system = "Ты — менеджер проектов розничного магазина. Пиши чётко, по-русски."
                    ai_text = await ask_llm(prompt, system=system, max_tokens=250,
                                           temperature=0.4, feature="task_digest")
                    if ai_text:
                        try:
                            from web.rate_store import log_ai_digest as _log_dg
                            import json as _jdg
                            _snap = _jdg.dumps({"total": total, "new": new_this_week, "done": done_this_week, "overdue": overdue}, ensure_ascii=False)
                            _log_dg("task_digest", db_path, ai_text, _snap)
                        except Exception:
                            pass
                    if not ai_text:
                        ai_text = f"За неделю создано {new_this_week} задач, выполнено {done_this_week}."

                    period = f"{(today - _dt.timedelta(days=7)).strftime('%d.%m')}–{today.strftime('%d.%m.%Y')}"
                    msg = (
                        f"📊 <b>AI-дайджест задач</b> | {period}\n\n"
                        f"📋 Всего: <b>{total}</b>  ·  🆕 Новых: <b>{new_this_week}</b>\n"
                        f"✅ Выполнено: <b>{done_this_week}</b>  ·  ⚠️ Просрочено: <b>{overdue}</b>\n\n"
                        f"{ai_text}"
                    )
                    for tid in admin_ids[:5]:
                        if tid and int(tid) > 0:
                            _push(int(tid), msg)
                            await asyncio.sleep(0.05)
                except Exception as _de:
                    logging.warning("ai_task_digest db=%s: %s", db_path, _de)
        except Exception as _e:
            logging.error("ai_task_digest: %s", _e)

    scheduler.add_job(
        ai_task_digest,
        CronTrigger(day_of_week='tue', hour=8, minute=0),
        id='ai_task_digest',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Утренний AI-брифинг в чат — ежедневно в 07:00 UTC (10:00 МСК)
    async def ai_morning_briefing():
        """Постит утренний брифинг за вчера в AI-тему чата каждой орги (расширение ai_chat_assistant)."""
        try:
            from web.ai_utils import ask_llm, build_morning_briefing_prompt, is_configured
            from billing_utils import has_extension as _hex_ext
            from database import Database
            import datetime as _dt
        except Exception as _imp_err:
            logging.warning(f"ai_morning_briefing: import error: {_imp_err}")
            return

        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        week_ago  = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()

        db_paths = _get_scheduler_db_paths()
        for db_path in db_paths:
            try:
                db = Database(db_path)

                # Gate: требует расширения ai_chat_assistant у любого из первых 3 админов
                try:
                    admin_ids = db.get_all_admins_telegram_ids()
                except Exception:
                    continue
                if not admin_ids:
                    continue
                try:
                    if not any(_hex_ext(int(tid), "ai_chat_assistant") for tid in admin_ids[:3] if tid and int(tid) > 0):
                        continue
                except Exception as _gate_err:
                    logging.warning(f"ai_morning_briefing billing gate: {_gate_err}")
                    continue

                # Данные за вчера и среднее за 7 дней
                try:
                    y_summary = db.get_sales_summary(start_date=yesterday, end_date=yesterday) or (0, 0, 0, 0)
                    w_summary = db.get_sales_summary(start_date=week_ago, end_date=yesterday)  or (0, 0, 0, 0)
                    y_rev  = float(y_summary[2] or 0)
                    w_rev  = float(w_summary[2] or 0)
                    avg_7d = w_rev / 7 if w_rev > 0 else 0
                except Exception:
                    continue

                if y_rev == 0 and avg_7d == 0:
                    continue  # нет данных — не постим

                # Вспомогательные данные для контекста
                try:
                    _top_products = db.get_top_products_month(3)
                except Exception:
                    _top_products = []
                try:
                    _top_sellers = db.get_active_sellers_month(3)
                except Exception:
                    _top_sellers = []
                try:
                    _cat_breakdown = db.get_sales_by_category_for_period(yesterday, yesterday)
                except Exception:
                    _cat_breakdown = []

                org_name = db.db_file.replace("\\", "/").split("/")[-1].replace(".db", "").replace("org_", "")

                # Генерируем текст брифинга через LLM
                briefing_text: str | None = None
                if is_configured():
                    try:
                        prompt = build_morning_briefing_prompt(
                            org_name=org_name,
                            yesterday_revenue=y_rev,
                            avg_7d=avg_7d,
                            top_products=_top_products or None,
                            top_sellers=_top_sellers or None,
                            category_breakdown=_cat_breakdown or None,
                        )
                        briefing_text = await ask_llm(prompt, max_tokens=300, feature="briefing")
                        if briefing_text:
                            try:
                                from web.rate_store import log_ai_digest as _log_dg
                                import json as _jdg
                                _snap = _jdg.dumps({"org": org_name, "y_rev": round(y_rev), "avg_7d": round(avg_7d)}, ensure_ascii=False)
                                _log_dg("morning_briefing", db_path, briefing_text, _snap)
                            except Exception:
                                pass
                    except Exception as _ai_err:
                        logging.warning(f"ai_morning_briefing LLM error: {_ai_err}")

                # Fallback без LLM
                if not briefing_text:
                    import datetime as _dt2
                    _yd_str = _dt2.date.fromisoformat(yesterday).strftime("%d.%m.%Y")
                    if y_rev > 0:
                        _diff = f" ({(y_rev - avg_7d) / avg_7d * 100:+.0f}% к среднему)" if avg_7d > 0 else ""
                        briefing_text = f"📅 Итоги {_yd_str}: {int(y_rev):,} ₽{_diff}."
                        if _cat_breakdown:
                            top_cat = _cat_breakdown[0]
                            if isinstance(top_cat, dict):
                                briefing_text += f" Топ категория: {top_cat.get('category','—')} — {int(top_cat.get('revenue',0)):,} ₽."
                    else:
                        briefing_text = f"📅 {_yd_str}: продаж не зафиксировано. Средняя за неделю: {int(avg_7d):,} ₽."

                # Получаем или создаём AI-тему чата
                try:
                    ai_topic_id = await asyncio.to_thread(db.ensure_ai_topic)
                except Exception:
                    continue

                # Постим брифинг в чат (user_id=0 = AI-бот)
                try:
                    _date_prefix = _dt.date.today().strftime("%d.%m")
                    full_text = f"☀️ <b>Утренний брифинг {_date_prefix}</b>\n\n{briefing_text}"
                    await asyncio.to_thread(
                        db.add_chat_message, 0, full_text, '', '', '', 0, ai_topic_id
                    )
                    logging.info(f"ai_morning_briefing: posted to org={db_path} topic={ai_topic_id}")
                except Exception as _post_err:
                    logging.warning(f"ai_morning_briefing: post error org={db_path}: {_post_err}")

            except Exception as _org_err:
                logging.warning(f"ai_morning_briefing: org={db_path}: {_org_err}")

        logging.info("ai_morning_briefing: done")

    scheduler.add_job(
        ai_morning_briefing,
        CronTrigger(hour=7, minute=0),   # 07:00 UTC = 10:00 МСК
        id='ai_morning_briefing',
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

                    # Разбивка по точкам продаж внутри орги (если ≥2 магазинов)
                    _shop_breakdown: list[dict] = []
                    try:
                        _shops_raw = db.get_all_shops(include_system=False)
                        _shop_names = []
                        for _sr in (_shops_raw or []):
                            _sn = _sr[0] if isinstance(_sr, (list, tuple)) else _sr
                            if _sn and str(_sn).strip():
                                _shop_names.append(str(_sn).strip())
                        # Respect owner's shop filter (empty list = all shops)
                        _digest_shop_filter = _digest_cfg.get("digest_shop_filter") or []
                        if _digest_shop_filter:
                            _shop_names = [s for s in _shop_names if s in _digest_shop_filter]
                        if len(_shop_names) >= 2:
                            _shop_conn = db.get_connection()
                            try:
                                for _shop_name in _shop_names:
                                    _sw_row = _shop_conn.execute(
                                        """SELECT SUM(quantity_sold * sale_price)
                                           FROM sales
                                           WHERE shop_name = ? AND sale_date >= ? AND sale_date <= ?""",
                                        (_shop_name, week_start, week_end),
                                    ).fetchone()
                                    _sp_row = _shop_conn.execute(
                                        """SELECT SUM(quantity_sold * sale_price)
                                           FROM sales
                                           WHERE shop_name = ? AND sale_date >= ? AND sale_date <= ?""",
                                        (_shop_name, prev_start, prev_end),
                                    ).fetchone()
                                    _shop_breakdown.append({
                                        "name": _shop_name,
                                        "week_revenue": float(_sw_row[0] or 0) if _sw_row else 0.0,
                                        "prev_revenue": float(_sp_row[0] or 0) if _sp_row else 0.0,
                                    })
                            finally:
                                _shop_conn.close()
                            # Sort by current week revenue descending
                            _shop_breakdown.sort(key=lambda x: x["week_revenue"], reverse=True)
                    except Exception as _shop_err:
                        logging.debug(f"ai_weekly_digest shop_breakdown: {_shop_err}")

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
                                shop_breakdown=_shop_breakdown or None,
                            )
                            ai_text = await ask_llm(prompt, max_tokens=400, feature="digest")
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
                        if _shop_breakdown and len(_shop_breakdown) >= 2:
                            _sb_parts = []
                            for _sb in _shop_breakdown[:5]:
                                _sb_rev = _sb.get("week_revenue", 0)
                                _sb_prev = _sb.get("prev_revenue", 0)
                                if _sb_prev > 0:
                                    _sb_diff = (_sb_rev - _sb_prev) / _sb_prev * 100
                                    _sb_arrow = "▲" if _sb_diff >= 0 else "▼"
                                    _sb_parts.append(f"{_sb['name']}: {int(_sb_rev):,} ₽ ({_sb_arrow}{abs(_sb_diff):.0f}%)")
                                else:
                                    _sb_parts.append(f"{_sb['name']}: {int(_sb_rev):,} ₽")
                            lines.append("🏪 По точкам: " + "; ".join(_sb_parts))
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
                    if ai_text:
                        try:
                            from web.rate_store import log_ai_digest as _log_dg
                            import json as _jdg
                            _snap = _jdg.dumps({"org": org_name, "week_rev": round(week_rev), "prev_week_rev": round(prev_week_rev)}, ensure_ascii=False)
                            _log_dg("weekly_digest", db_path, ai_text, _snap)
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
                        import json as _json_sb
                        _sdb = _sq3.connect("data/shop_bot.db")
                        _sb_json = _json_sb.dumps(_shop_breakdown, ensure_ascii=False) if _shop_breakdown and len(_shop_breakdown) >= 2 else None
                        _si_json = _json_sb.dumps(_shop_names, ensure_ascii=False) if _shop_names else None
                        _sdb.execute(
                            """INSERT INTO ai_weekly_digest_cache (org_db, digest_text, generated_at, shop_breakdown_json, shops_included_json)
                               VALUES (?, ?, datetime('now'), ?, ?)
                               ON CONFLICT(org_db) DO UPDATE SET
                                 digest_text          = excluded.digest_text,
                                 generated_at         = excluded.generated_at,
                                 shop_breakdown_json  = excluded.shop_breakdown_json,
                                 shops_included_json  = excluded.shops_included_json""",
                            (db_path, ai_text or msg, _sb_json, _si_json),
                        )
                        _sdb.commit()
                        _sdb.close()
                        # Immediately compute full breakdown (with sparklines) so the web
                        # cabinet never serves a sparkline-less cache row.  The Friday
                        # backfill job remains as a safety-net for any orgs missed here.
                        try:
                            from web.routes.ai_insights import _compute_shop_weekly_breakdown as _csb
                            _full_bd = _csb(db_path)
                            if _full_bd:
                                _full_json = _json_sb.dumps(_full_bd, ensure_ascii=False)
                                _upd2 = _sq3.connect("data/shop_bot.db")
                                try:
                                    _upd2.execute(
                                        "UPDATE ai_weekly_digest_cache SET shop_breakdown_json=? WHERE org_db=?",
                                        (_full_json, db_path),
                                    )
                                    _upd2.commit()
                                finally:
                                    _upd2.close()
                                logging.debug(
                                    "ai_weekly_digest: inline breakdown filled %s (%d shops)",
                                    db_path, len(_full_bd),
                                )
                        except Exception as _bd_err:
                            logging.debug("ai_weekly_digest: inline breakdown: %s", _bd_err)
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

    # ── AI-советник по закупкам/неликвиду (суббота 09:00 МСК = 06:00 UTC) ───
    async def ai_procurement_advisor():
        """Еженедельный AI-отчёт по закупкам и залежалым товарам — владельцам."""
        import os as _os, json as _json, urllib.request as _ureq
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        import datetime as _dt
        _now_utc = _dt.datetime.utcnow()
        # Суббота = 5, 06:xx UTC = 09:xx МСК
        if _now_utc.weekday() != 5 or _now_utc.hour != 6:
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
            from web.ai_utils import ask_llm, build_procurement_advisor_prompt, is_configured
        except Exception as _imp_err:
            logging.warning(f"ai_procurement_advisor: import error: {_imp_err}")
            return

        try:
            from database import Database
            db_paths = _get_scheduler_db_paths()

            for db_path in db_paths:
                try:
                    db = Database(db_path)

                    # Gate: ai_smart_alerts extension required
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
                        continue  # fail-closed: при ошибке биллинга — пропускаем орг

                    org_name = db.db_file.replace("\\", "/").split("/")[-1].replace(".db", "").replace("org_", "")

                    # Собираем данные склада
                    try:
                        turnover_rows = await asyncio.to_thread(db.get_inventory_turnover, None, 30)
                    except Exception:
                        turnover_rows = []
                    try:
                        dead_stock_rows = await asyncio.to_thread(db.get_dead_stock, None, 30)
                    except Exception:
                        dead_stock_rows = []

                    # Пропускаем если нет никаких данных по складу
                    if not turnover_rows and not dead_stock_rows:
                        continue

                    ai_text: str | None = None
                    if is_configured():
                        try:
                            prompt = build_procurement_advisor_prompt(
                                org_name=org_name,
                                turnover_rows=turnover_rows,
                                dead_stock_rows=dead_stock_rows,
                            )
                            ai_text = await ask_llm(prompt, max_tokens=350, feature="procurement")
                        except Exception as _ai_err:
                            logging.warning(f"ai_procurement_advisor LLM error: {_ai_err}")

                    if ai_text:
                        msg = f"📦 <b>Советник по закупкам</b>\n\n{ai_text}"
                    else:
                        # Fallback без LLM
                        _crit = [r for r in turnover_rows if r[8] is not None and r[8] <= 7]
                        _warn = [r for r in turnover_rows if r[8] is not None and 7 < r[8] <= 14]
                        lines_fb = ["📦 <b>Советник по закупкам</b>"]
                        if _crit:
                            lines_fb.append(f"🔴 Критический запас: " + ", ".join(r[1] for r in _crit[:3]))
                        if _warn:
                            lines_fb.append(f"🟡 Пополнить в ближайшие 2 недели: " + ", ".join(r[1] for r in _warn[:3]))
                        if dead_stock_rows:
                            total_frozen = sum(int(r[5] * r[3]) for r in dead_stock_rows)
                            lines_fb.append(f"📉 Неликвид: {len(dead_stock_rows)} позиций, заморожено ≈{total_frozen:,} ₽")
                        if not _crit and not _warn and not dead_stock_rows:
                            lines_fb.append("✅ Критических запасов и неликвида нет — всё в норме.")
                        msg = "\n".join(lines_fb)

                    import re as _re
                    _plain = _re.sub(r"<[^>]+>", "", msg).strip()

                    # Лог в историю орг
                    try:
                        db.add_ai_alert_log('procurement', _plain)
                    except Exception:
                        pass
                    if ai_text:
                        try:
                            from web.rate_store import log_ai_digest as _log_dg
                            import json as _jdg
                            _snap = _jdg.dumps({"org": org_name, "turnover_items": len(turnover_rows), "dead_stock_items": len(dead_stock_rows)}, ensure_ascii=False)
                            _log_dg("procurement_advisor", db_path, ai_text, _snap)
                        except Exception:
                            pass

                    # Telegram — владельцам/администраторам
                    for tg_id in admin_ids[:3]:
                        if tg_id and tg_id > 0:
                            _send_tg(tg_id, msg)

                    # Email — при включённом email-дайджесте
                    try:
                        _cfg = db.get_ai_alert_settings()
                    except Exception:
                        _cfg = {}
                    if _cfg.get("digest_email_enabled", False):
                        try:
                            from web.email_utils import send_procurement_advisor_email as _send_proc_email, is_configured as _email_ok
                            import sqlite3 as _sq3pr
                            if _email_ok():
                                _sdb_pr = _sq3pr.connect("data/shop_bot.db")
                                for _tid in admin_ids[:3]:
                                    if not _tid or int(_tid) <= 0:
                                        continue
                                    _wc = _sdb_pr.execute(
                                        "SELECT email FROM web_credentials WHERE telegram_id=? AND is_verified=1 LIMIT 1",
                                        (int(_tid),)
                                    ).fetchone()
                                    if _wc and _wc[0]:
                                        import anyio as _anyio
                                        await _anyio.to_thread.run_sync(
                                            lambda _e=_wc[0]: _send_proc_email(_e, _plain)
                                        )
                                _sdb_pr.close()
                        except Exception as _email_err:
                            logging.debug(f"ai_procurement_advisor email: {_email_err}")

                except Exception as _db_err:
                    logging.warning(f"ai_procurement_advisor db={db_path}: {_db_err}")
        except Exception as _e:
            logging.error(f"ai_procurement_advisor: {_e}")

    scheduler.add_job(
        ai_procurement_advisor,
        CronTrigger(minute=15),
        id='ai_procurement_advisor',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # ── Персональный коуч продавцу (суббота 09:10 МСК = 06:10 UTC) ──────────
    async def ai_seller_coach():
        """Еженедельный персональный коуч-совет каждому продавцу."""
        import os as _os, json as _json, urllib.request as _ureq
        _token = _os.environ.get("BOT_TOKEN", "")
        if not _token:
            return

        import datetime as _dt
        _now_utc = _dt.datetime.utcnow()
        # Суббота = 5, 06:xx UTC = 09:xx МСК
        if _now_utc.weekday() != 5 or _now_utc.hour != 6:
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
            from web.ai_utils import ask_llm, build_seller_coach_prompt, is_configured
        except Exception as _imp_err:
            logging.warning(f"ai_seller_coach: import error: {_imp_err}")
            return

        try:
            from database import Database
            db_paths = _get_scheduler_db_paths()

            for db_path in db_paths:
                try:
                    db = Database(db_path)

                    # Gate: ai_smart_alerts extension required у владельца
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
                        continue  # fail-closed: при ошибке биллинга — пропускаем орг

                    # Выручка за прошедшую неделю
                    week_start = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
                    week_end = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

                    try:
                        ranking = await asyncio.to_thread(
                            db.get_sales_ranking, week_start, week_end
                        )
                    except Exception:
                        ranking = []

                    # Статистика команды (только по тем, кто имел продажи)
                    total_sellers_with_sales = len(ranking)
                    team_total_rev = sum(float(r[4] or 0) for r in ranking)
                    team_avg_rev = team_total_rev / total_sellers_with_sales if total_sellers_with_sales > 0 else 0.0
                    team_total_trans = sum(int(r[5] or 0) for r in ranking)
                    team_avg_check = team_total_rev / team_total_trans if team_total_trans > 0 else 0.0

                    # Карта user_db_id → (rank, row) для быстрого поиска
                    ranking_by_uid: dict = {}
                    for _rank_i, _r in enumerate(ranking, 1):
                        ranking_by_uid[_r[7]] = (_rank_i, _r)

                    # Все пользователи орги — коуч идёт каждому, не только продававшим
                    try:
                        all_users = await asyncio.to_thread(db.get_all_users)
                    except Exception:
                        all_users = []

                    if not all_users:
                        continue

                    # Топ-продукт за неделю (один для всей орги)
                    try:
                        _top_prods = await asyncio.to_thread(db.get_top_products_month, 1)
                        top_product_name = _top_prods[0][0] if _top_prods else None
                    except Exception:
                        top_product_name = None

                    # users cols: id[0] telegram_id[1] first_name[2] last_name[3] ... shop_name[8]
                    total_all_sellers = len(all_users)
                    for u in all_users:
                        user_db_id = u[0]
                        tg_id = u[1]
                        if not tg_id or int(tg_id) <= 0:
                            continue  # email-only (tg_id < 0) или нет TG

                        fn = (u[2] or "").strip()
                        ln = (u[3] or "").strip()
                        seller_name = f"{fn} {ln}".strip() or (u[8] or "") or f"Сотрудник"

                        if user_db_id in ranking_by_uid:
                            rank_idx, seller_row = ranking_by_uid[user_db_id]
                            seller_rev = float(seller_row[4] or 0)
                            seller_trans = int(seller_row[5] or 0)
                        else:
                            # Продавец не имел продаж за неделю — показываем нуль
                            rank_idx = total_sellers_with_sales + 1
                            seller_rev = 0.0
                            seller_trans = 0

                        seller_avg_check = seller_rev / seller_trans if seller_trans > 0 else 0.0

                        ai_text: str | None = None
                        if is_configured():
                            try:
                                prompt = build_seller_coach_prompt(
                                    seller_name=fn or seller_name,
                                    rank=rank_idx,
                                    total_sellers=total_all_sellers,
                                    week_revenue=seller_rev,
                                    team_avg_revenue=team_avg_rev,
                                    avg_check=seller_avg_check,
                                    team_avg_check=team_avg_check,
                                    total_transactions=seller_trans,
                                    top_product_name=top_product_name,
                                )
                                ai_text = await ask_llm(prompt, max_tokens=250, feature="seller_coach")
                            except Exception as _ai_err:
                                logging.warning(f"ai_seller_coach LLM error seller={user_db_id}: {_ai_err}")

                        if ai_text:
                            try:
                                from web.rate_store import log_ai_digest as _log_dg
                                import json as _jdg
                                _snap = _jdg.dumps({"seller": fn or seller_name, "rank": rank_idx, "rev": round(seller_rev), "team_avg": round(team_avg_rev)}, ensure_ascii=False)
                                _log_dg("seller_coach", db_path, ai_text, _snap)
                            except Exception:
                                pass
                            msg = f"⭐ <b>Твои итоги недели, {fn or seller_name}!</b>\n\n{ai_text}"
                        else:
                            # Fallback без LLM
                            diff_str = ""
                            if team_avg_rev > 0:
                                diff_pct = (seller_rev - team_avg_rev) / team_avg_rev * 100
                                arrow = "▲" if diff_pct >= 0 else "▼"
                                diff_str = f" {arrow}{abs(diff_pct):.0f}% к средней по команде"
                            elif seller_rev == 0:
                                diff_str = " — продаж на этой неделе не было"
                            rank_str = f"{rank_idx} из {total_all_sellers}" if seller_rev > 0 else f"вне рейтинга (нет продаж)"
                            check_str = f"{int(seller_avg_check):,} ₽" if seller_avg_check > 0 else "—"
                            msg = (
                                f"⭐ <b>Итоги недели, {fn or seller_name}!</b>\n\n"
                                f"Выручка: {int(seller_rev):,} ₽{diff_str}\n"
                                f"Место в рейтинге: {rank_str}\n"
                                f"Транзакций: {seller_trans}, средний чек: {check_str}"
                                + (f"\n💡 Средний чек команды: {int(team_avg_check):,} ₽" if team_avg_check > 0 else "")
                            )

                        import re as _re
                        _plain = _re.sub(r"<[^>]+>", "", msg).strip()

                        _send_tg(tg_id, msg)

                        # Уведомление в историю
                        try:
                            _nc2 = db.get_connection()
                            _nr2 = _nc2.execute("SELECT id FROM users WHERE id = ?", (user_db_id,)).fetchone()
                            _nc2.close()
                            if _nr2:
                                db.add_notification_to_history(_nr2[0], 'personal', _plain[:500])
                        except Exception:
                            pass

                        # Web Push продавцу
                        try:
                            from web.push_utils import apush as _apush
                            await _apush(int(tg_id), "⭐ Твои итоги недели", _plain[:120], "/dashboard")
                        except Exception:
                            pass

                        # Email — если есть верифицированный email
                        try:
                            from web.email_utils import send_seller_coach_email as _send_coach_email, is_configured as _email_ok
                            import sqlite3 as _sq3sc
                            if _email_ok():
                                _sdb_sc = _sq3sc.connect("data/shop_bot.db")
                                _wc_sc = _sdb_sc.execute(
                                    "SELECT email FROM web_credentials WHERE telegram_id=? AND is_verified=1 LIMIT 1",
                                    (int(tg_id),)
                                ).fetchone()
                                _sdb_sc.close()
                                if _wc_sc and _wc_sc[0]:
                                    import anyio as _anyio
                                    await _anyio.to_thread.run_sync(
                                        lambda _e=_wc_sc[0], _n=fn or seller_name: _send_coach_email(_e, _n, _plain)
                                    )
                        except Exception as _coach_email_err:
                            logging.debug(f"ai_seller_coach email tg={tg_id}: {_coach_email_err}")

                except Exception as _db_err:
                    logging.warning(f"ai_seller_coach db={db_path}: {_db_err}")
        except Exception as _e:
            logging.error(f"ai_seller_coach: {_e}")

    scheduler.add_job(
        ai_seller_coach,
        CronTrigger(minute=20),
        id='ai_seller_coach',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # ─── Shop breakdown backfill — каждую пятницу в 06:00 UTC ──────────────────
    async def backfill_shop_breakdown():
        """Заполняет shop_breakdown_json для всех org, где он ещё не вычислен."""
        import sqlite3 as _sq3, json as _json, os as _os
        _SBOT = "data/shop_bot.db"
        _orgs_filled = 0
        _orgs_skipped = 0
        _orgs_error = 0
        _total = 0
        try:
            try:
                conn = _sq3.connect(_SBOT)
                try:
                    rows = conn.execute(
                        "SELECT org_db FROM ai_weekly_digest_cache WHERE shop_breakdown_json IS NULL"
                    ).fetchall()
                finally:
                    conn.close()
            except Exception as _e:
                logging.error("backfill_shop_breakdown: cannot read cache: %s", _e)
                rows = []

            _total = len(rows)
            if rows:
                from web.routes.ai_insights import _compute_shop_weekly_breakdown
                for (org_db,) in rows:
                    try:
                        if not org_db or not _os.path.exists(org_db):
                            _orgs_skipped += 1
                            continue
                        breakdown = _compute_shop_weekly_breakdown(org_db)
                        if not breakdown:
                            _orgs_skipped += 1
                            continue
                        breakdown_json = _json.dumps(breakdown, ensure_ascii=False)
                        upd_conn = _sq3.connect(_SBOT)
                        try:
                            upd_conn.execute(
                                "UPDATE ai_weekly_digest_cache SET shop_breakdown_json=? WHERE org_db=? AND shop_breakdown_json IS NULL",
                                (breakdown_json, org_db),
                            )
                            upd_conn.commit()
                        finally:
                            upd_conn.close()
                        logging.info("backfill_shop_breakdown: filled %s (%d shops)", org_db, len(breakdown))
                        _orgs_filled += 1
                    except Exception as _org_err:
                        logging.warning("backfill_shop_breakdown: org_db=%s error: %s", org_db, _org_err)
                        _orgs_error += 1
        finally:
            logging.info(
                "backfill_shop_breakdown: done — filled=%d skipped=%d error=%d / total=%d",
                _orgs_filled, _orgs_skipped, _orgs_error, _total,
            )
            try:
                _details = (
                    f"filled={_orgs_filled} skipped={_orgs_skipped} "
                    f"error={_orgs_error} total={_total}"
                )
                Database(_SBOT).add_admin_audit(
                    actor_tg_id=None,
                    actor_name="scheduler",
                    action="backfill_shop_breakdown",
                    target="ai_weekly_digest_cache",
                    details=_details,
                    ip="",
                )
            except Exception as _audit_err:
                logging.warning("backfill_shop_breakdown: audit write failed: %s", _audit_err)

    scheduler.add_job(
        backfill_shop_breakdown,
        CronTrigger(day_of_week='fri', hour=6, minute=0),
        id='backfill_shop_breakdown',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
