"""
Дашборд-сводка для главного меню.
Для администратора — сводка по организации сегодня (с учётом зоны ответственности).
Для продавца — зарплата/мотивация/планы за текущий месяц.
"""
import asyncio
import json as _json
import logging
import sqlite3
from datetime import date, datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db_utils import get_db, is_any_admin, get_user_org_scope, get_role_display_label, get_user_org_role, get_user_custom_title
from message_utils import safe_edit_message
from hints import hint_suffix, maybe_send_welcome
from utils import he, format_price
from database import Database

logger = logging.getLogger(__name__)
router = Router()

_PERIOD_LABELS = {
    'monthly': 'Месяц',
    'weekly':  'Неделя',
    'daily':   'День',
    'quarter': 'Квартал',
}

_METRIC_LABELS = {
    'turnover':  'Оборот (₽)',
    'quantity':  'Количество (шт)',
}

MONTH_NAMES_RU = {
    1: 'январь', 2: 'февраль', 3: 'март', 4: 'апрель',
    5: 'май', 6: 'июнь', 7: 'июль', 8: 'август',
    9: 'сентябрь', 10: 'октябрь', 11: 'ноябрь', 12: 'декабрь',
}

_PLANS_PER_PAGE = 4  # планов на одну страницу пагинации дашборда

_SCOPE_ICONS = {
    'shop':    '🏪',
    'city':    '🏙️',
    'network': '🌐',
}

_SCOPE_NAMES = {
    'shop':    'Магазин',
    'city':    'Город',
    'network': 'Торговая сеть',
}


_METRIC_SHORT = {'turnover': 'Оборот', 'quantity': 'Кол-во'}
_CONTEST_TYPE_LABEL = {
    'top_seller': '🏅 Лучший продавец',
    'target':     '🎯 Выполни план',
    'team':       '👥 Командный',
}
_REWARD_TYPE_LABEL = {'money': '💵', 'gift': '🎁', 'certificate': '🎟'}


async def _contest_block(contests: list, today_str: str,
                         current_db=None, telegram_id: int = None) -> str:
    """Развёрнутый блок конкурсов для дашборда.

    contests — список из get_contests(status='active').
    Колонки: 0:id 1:title 3:contest_type 4:metric_type 5:target_value
             6:reward_type 7:reward_value 8:start_date 9:end_date
             22:reward_mode 23:individual_targets
    current_db / telegram_id — если переданы, показывается прогресс пользователя.
    """
    if not contests:
        return ""
    today_dt = datetime.strptime(today_str, '%Y-%m-%d').date()
    text = f"🏆 <b>Конкурсы</b> · активных <b>{len(contests)}</b>\n\n"
    for c in contests[:4]:
        try:
            title        = he(c[1] or "Без названия")
            ctype_label  = _CONTEST_TYPE_LABEL.get(c[3], '🥊')
            metric_short = _METRIC_SHORT.get(c[4], c[4] or '?')
            target       = c[5]
            reward_mode  = c[22] if len(c) > 22 else 'total'
            end_date_str = c[9]

            # дедлайн
            try:
                end_dt    = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                days_left = (end_dt - today_dt).days
                if days_left < 0:
                    deadline_str = "завершён"
                elif days_left == 0:
                    deadline_str = "сегодня последний день"
                else:
                    deadline_str = f"до {end_dt.strftime('%d.%m')} · {days_left} дн."
            except Exception:
                deadline_str = end_date_str or "?"

            text += f"  {ctype_label}: <b>{title}</b>\n"
            text += f"  📅 {deadline_str}\n"

            if reward_mode == 'per_sale':
                # Режим бонуса за продажу
                text += f"  💵 Бонус за каждую продажу\n"
                if current_db is not None and telegram_id is not None:
                    try:
                        results = await current_db.compute_contest_results(c[0])
                        user_row = next(
                            (r for r in results if r.get('telegram_id') == telegram_id), None
                        )
                        if user_row is not None:
                            bonus = user_row.get('reward', 0)
                            qty = int(user_row.get('actual', 0))
                            plan_pct = user_row.get('plan_pct', 0)
                            pct_note = f" · план {plan_pct:.0f}%" if plan_pct > 0 else ""
                            text += f"  💵 накоплено: {bonus:,.0f}₽ ({qty} шт.){pct_note}\n"
                    except Exception:
                        pass
            else:
                # Режим итогового приза
                if c[4] == 'turnover':
                    target_str = f"{float(target):,.0f} ₽" if target else "?"
                else:
                    target_str = f"{int(float(target))} шт." if target else "?"
                reward_val = c[7]
                try:
                    reward_str = (f"{float(reward_val):,.0f} ₽"
                                  if c[6] in ('fixed', 'money') else he(str(reward_val)))
                except (TypeError, ValueError):
                    reward_str = he(str(reward_val)) if reward_val else "?"

                text += f"  📊 {metric_short} · цель: {target_str}\n"
                text += f"  🏅 Приз: {reward_str}\n"

                # Прогресс-бар пользователя
                if current_db is not None and telegram_id is not None:
                    try:
                        results = await current_db.compute_contest_results(c[0])
                        user_row = next(
                            (r for r in results if r.get('telegram_id') == telegram_id), None
                        )
                        if user_row is not None:
                            actual  = float(user_row.get('actual', 0) or 0)
                            eff_tgt = float(user_row.get('individual_target', target) or target or 0)
                            pct     = round(actual / eff_tgt * 100, 1) if eff_tgt > 0 else 0.0
                            bar     = _progress_bar(pct)
                            if c[4] == 'turnover':
                                actual_str = f"{actual:,.0f} ₽"
                            else:
                                actual_str = f"{int(actual)} шт."
                            text += f"  [{bar}] {pct:.0f}% · {actual_str}\n"
                        else:
                            text += f"  [{_progress_bar(0)}] 0%\n"
                    except Exception:
                        pass

            text += "\n"
        except Exception:
            text += f"  🥊 <b>{he(str(c[1]))}</b>\n\n"
    if len(contests) > 4:
        text += f"  ···  ещё {len(contests) - 4} конкурсов\n\n"
    return text


def _plan_summary_line(plan, actual: float, percent: float) -> str:
    """Строка плана в формате как в «Мои планы»"""
    period     = _PERIOD_LABELS.get(plan[1], plan[1] or '?')
    metric_lbl = _METRIC_LABELS.get(plan[2], plan[2] or '?')
    target     = plan[3]
    target_type = plan[4]
    filter_type = plan[7]
    filter_val  = plan[8]

    if target_type == 'seller':
        fn  = plan[12] or ""
        ln  = plan[13] or ""
        who = f"{fn} {ln}".strip() or f"id={plan[5]}"
    else:
        who = plan[6] or "Все магазины"

    if filter_type == 'category':
        try:
            cats = _json.loads(filter_val) if filter_val else []
            if isinstance(cats, list) and cats:
                scope = " · кат. «" + he(", ".join(cats[:2])) + ("…" if len(cats) > 2 else "") + "»"
            else:
                scope = f" · кат. «{he(str(filter_val))}»"
        except (ValueError, TypeError):
            scope = f" · кат. «{he(str(filter_val))}»"
    elif filter_type == 'product':
        scope = " · отд. товары"
    else:
        scope = ""

    if plan[2] == 'turnover':
        actual_str = f"{format_price(actual)}₽"
        target_str = f"{format_price(target)}₽"
    else:
        actual_str = f"{int(actual)} шт"
        target_str = f"{int(target)} шт"

    bar = _progress_bar(percent)
    return (
        f"📌 <b>{he(who)}</b> · {period} · {metric_lbl}{scope}\n"
        f"{bar} {percent:.0f}%\n"
        f"  {actual_str} из {target_str}"
    )


def _progress_bar(percent: float, width: int = 8) -> str:
    filled = min(int(percent / 100 * width), width)
    if percent >= 100:
        fill_char = '🟩'
    elif percent >= 50:
        fill_char = '🟨'
    else:
        fill_char = '🟥'
    return fill_char * filled + '⬜' * (width - filled)


def _scope_filter_kwargs(scope_type: str, scope_values: list) -> dict:
    """Преобразует scope в kwargs для методов get_sales_summary / get_sales_ranking.

    scope_values — list[str]. Если один элемент — одиночный фильтр;
    если несколько — используются plural-параметры (shop_names/cities/trade_networks).
    """
    if not scope_type or scope_type == 'all' or not scope_values:
        return {}
    if scope_type == 'shop':
        return {'shop_name': scope_values[0]} if len(scope_values) == 1 else {'shop_names': scope_values}
    if scope_type == 'city':
        return {'city': scope_values[0]} if len(scope_values) == 1 else {'cities': scope_values}
    if scope_type == 'network':
        return {'trade_network': scope_values[0]} if len(scope_values) == 1 else {'trade_networks': scope_values}
    return {}


def _get_dashboard_scale(scope_type: str, scope_values: list) -> str:
    """Определяет масштаб дашборда по scope.

    'single' — один магазин: максимум деталей (имена, товары, каждый план).
    'wide'   — город/сеть/несколько магазинов: сводный вид + таблица по магазинам.
    'org'    — весь орг без фильтра: только агрегаты + топ-3 магазина.
    """
    if scope_type == 'shop':
        return 'single' if len(scope_values) == 1 else 'wide'
    if scope_type in ('city', 'network'):
        return 'wide'
    return 'org'


def _per_shop_breakdown(db_file: str, start_date: str, end_date: str,
                        scope_type: str, scope_values: list, limit: int = 7) -> list:
    """Разбивка по магазинам: [(shop_name, txn, qty, revenue), ...] по убыванию выручки."""
    try:
        conn = Database(db_file).get_connection()
        cursor = conn.cursor()
        base = '''
            SELECT u.shop_name,
                   COUNT(DISTINCT s.id),
                   COALESCE(SUM(s.quantity_sold), 0),
                   COALESCE(SUM(s.quantity_sold * s.sale_price), 0.0)
            FROM sales s
            JOIN users u ON s.user_id = u.id
            WHERE date(s.sale_date) BETWEEN ? AND ?
              AND u.shop_name IS NOT NULL AND u.shop_name != ""
        '''
        params = [start_date, end_date]
        vals = scope_values or []
        if scope_type == 'shop' and vals:
            ph = ','.join('?' * len(vals))
            base += f' AND u.shop_name IN ({ph})'
            params += vals
        elif scope_type == 'city' and vals:
            ph = ','.join('?' * len(vals))
            base += f' AND u.city IN ({ph})'
            params += vals
        elif scope_type == 'network' and vals:
            ph = ','.join('?' * len(vals))
            base += f' AND u.trade_network IN ({ph})'
            params += vals
        base += ' GROUP BY u.shop_name ORDER BY 4 DESC'
        cursor.execute(base, params)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _staff_by_shop(db_file: str, today: str,
                   scope_type: str, scope_values: list) -> list:
    """Количество сотрудников на смене по каждому магазину: [(shop_name, count), ...]."""
    try:
        conn = Database(db_file).get_connection()
        cursor = conn.cursor()
        base = '''
            SELECT u.shop_name, COUNT(DISTINCT ws.user_id)
            FROM work_schedule ws
            JOIN users u ON u.id = ws.user_id
            WHERE ws.work_date = ?
              AND u.shop_name IS NOT NULL AND u.shop_name != ""
        '''
        params = [today]
        vals = scope_values or []
        if scope_type == 'shop' and vals:
            ph = ','.join('?' * len(vals))
            base += f' AND u.shop_name IN ({ph})'
            params += vals
        elif scope_type == 'city' and vals:
            ph = ','.join('?' * len(vals))
            base += f' AND u.city IN ({ph})'
            params += vals
        elif scope_type == 'network' and vals:
            ph = ','.join('?' * len(vals))
            base += f' AND u.trade_network IN ({ph})'
            params += vals
        base += ' GROUP BY u.shop_name ORDER BY 2 DESC'
        cursor.execute(base, params)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _staff_by_shop_with_names(db_file: str, today: str,
                               scope_type: str = None, scope_values: list = None) -> list:
    """Сотрудники на смене, сгруппированные по магазинам.

    Возвращает [(shop_name, [(first_name, last_name), ...]), ...]
    отсортированный по убыванию количества сотрудников.
    """
    try:
        conn = Database(db_file).get_connection()
        cursor = conn.cursor()
        base = '''
            SELECT u.shop_name, u.first_name, u.last_name
            FROM work_schedule ws
            JOIN users u ON u.id = ws.user_id
            WHERE ws.work_date = ?
              AND u.shop_name IS NOT NULL AND u.shop_name != ""
              AND NOT EXISTS (
                  SELECT 1 FROM absence_records ar
                  WHERE ar.user_id = u.id
                    AND ar.status = 'approved'
                    AND ar.start_date <= ?
                    AND ar.end_date >= ?
              )
        '''
        params = [today, today, today]
        vals = scope_values or []
        if scope_type and vals:
            ph = ','.join('?' * len(vals))
            if scope_type == 'shop':
                base += f' AND u.shop_name IN ({ph})'
            elif scope_type == 'city':
                base += f' AND u.city IN ({ph})'
            elif scope_type == 'network':
                base += f' AND u.trade_network IN ({ph})'
            params.extend(vals)
        base += ' ORDER BY u.shop_name, u.last_name, u.first_name'
        cursor.execute(base, params)
        rows = cursor.fetchall()
        conn.close()
        grouped: dict = {}
        for sn, fn, ln in rows:
            grouped.setdefault(sn, []).append((fn or '', ln or ''))
        return sorted(grouped.items(), key=lambda x: len(x[1]), reverse=True)
    except Exception:
        return []


def _low_stock_items(db_file: str, scope_type: str, scope_values: list,
                     threshold: int = 5, limit: int = 5) -> list:
    """Конкретные товары с низким остатком (для single-масштаба): [(name, qty), ...]."""
    try:
        conn = Database(db_file).get_connection()
        cursor = conn.cursor()
        vals = scope_values or []
        if scope_type == 'shop' and vals:
            ph = ','.join('?' * len(vals))
            cursor.execute(f'''
                SELECT p.name, i.quantity
                FROM inventory i
                JOIN products p ON i.product_id = p.id
                WHERE i.quantity <= ? AND i.shop_name IN ({ph})
                ORDER BY i.quantity ASC
                LIMIT {limit}
            ''', [threshold] + vals)
        else:
            cursor.execute(f'''
                SELECT p.name, i.quantity
                FROM inventory i
                JOIN products p ON i.product_id = p.id
                WHERE i.quantity <= ?
                ORDER BY i.quantity ASC
                LIMIT {limit}
            ''', (threshold,))
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _plans_summary_text(plans_progress: list) -> str:
    """Краткая строка статуса планов: «5 активных · ✅ 4 в графике · ⚠️ 1 отстаёт»."""
    total = len(plans_progress)
    if total == 0:
        return "• Активных планов нет\n"
    on_track = sum(1 for _, _, pct in plans_progress if pct >= 75)
    behind   = total - on_track
    line = f"• Активных: <b>{total}</b>"
    if on_track:
        line += f" · ✅ в графике: <b>{on_track}</b>"
    if behind:
        line += f" · ⚠️ отстаёт: <b>{behind}</b>"
    return line + "\n"


def _low_stock_count(db_file: str, threshold: int = 5,
                     scope_type: str = None, scope_values: list = None) -> int:
    """Количество позиций с низким остатком с учётом scope (поддерживает multi-scope)."""
    try:
        conn = Database(db_file).get_connection()
        cursor = conn.cursor()
        vals = scope_values or []
        if scope_type == 'shop' and vals:
            ph = ','.join('?' * len(vals))
            cursor.execute(
                f'SELECT COUNT(*) FROM inventory WHERE quantity <= ? AND shop_name IN ({ph})',
                [threshold] + vals
            )
        elif scope_type in ('city', 'network') and vals:
            field = 'city' if scope_type == 'city' else 'trade_network'
            ph = ','.join('?' * len(vals))
            shops_set = set()
            cursor.execute(
                f'SELECT DISTINCT shop_name FROM users WHERE {field} IN ({ph}) AND shop_name IS NOT NULL',
                vals
            )
            shops_set.update(r[0] for r in cursor.fetchall())
            try:
                cursor.execute(
                    f'SELECT DISTINCT name FROM shops WHERE {field} IN ({ph}) AND name IS NOT NULL AND name != \'\'',
                    vals
                )
                shops_set.update(r[0] for r in cursor.fetchall())
            except Exception:
                pass
            shops = list(shops_set)
            if shops:
                sph = ','.join('?' * len(shops))
                cursor.execute(
                    f'SELECT COUNT(*) FROM inventory WHERE quantity <= ? AND shop_name IN ({sph})',
                    [threshold] + shops
                )
            else:
                conn.close()
                return 0
        else:
            cursor.execute('SELECT COUNT(*) FROM inventory WHERE quantity <= ?', (threshold,))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def _on_shift_details(db_file: str, today: str,
                      scope_type: str = None, scope_values: list = None) -> list:
    """Список сотрудников на смене сегодня с учётом scope (поддерживает multi-scope)."""
    try:
        conn = Database(db_file).get_connection()
        cursor = conn.cursor()
        base = '''
            SELECT u.first_name, u.last_name, u.shop_name
            FROM work_schedule ws
            JOIN users u ON u.id = ws.user_id
            WHERE ws.work_date = ?
              AND NOT EXISTS (
                  SELECT 1 FROM absence_records ar
                  WHERE ar.user_id = u.id
                    AND ar.status = 'approved'
                    AND ar.start_date <= ?
                    AND ar.end_date >= ?
              )
        '''
        params = [today, today, today]
        vals = scope_values or []
        if scope_type and vals:
            ph = ','.join('?' * len(vals))
            if scope_type == 'shop':
                base += f' AND u.shop_name IN ({ph})'
            elif scope_type == 'city':
                base += f' AND u.city IN ({ph})'
            elif scope_type == 'network':
                base += f' AND u.trade_network IN ({ph})'
            params.extend(vals)
        base += ' ORDER BY u.last_name, u.first_name'
        cursor.execute(base, params)
        result = cursor.fetchall()
        conn.close()
        return result
    except Exception:
        return []


def _today_total_earnings(db_file: str, today: str,
                          scope_type: str = None, scope_values: list = None) -> float:
    """Суммарные мотивационные выплаты за сегодня с учётом scope (поддерживает multi-scope)."""
    try:
        conn = Database(db_file).get_connection()
        cursor = conn.cursor()
        vals = scope_values or []
        if scope_type == 'shop' and vals:
            ph = ','.join('?' * len(vals))
            cursor.execute(f'''
                SELECT COALESCE(SUM(se.commission_amount), 0.0)
                FROM seller_earnings se
                JOIN sales s ON s.id = se.sale_id
                WHERE s.sale_date = ? AND s.shop_name IN ({ph})
            ''', [today] + vals)
        elif scope_type in ('city', 'network') and vals:
            field = 'city' if scope_type == 'city' else 'trade_network'
            ph = ','.join('?' * len(vals))
            cursor.execute(f'''
                SELECT COALESCE(SUM(se.commission_amount), 0.0)
                FROM seller_earnings se
                JOIN sales s ON s.id = se.sale_id
                JOIN users u ON s.user_id = u.id
                WHERE s.sale_date = ? AND u.{field} IN ({ph})
            ''', [today] + vals)
        else:
            cursor.execute('''
                SELECT COALESCE(SUM(se.commission_amount), 0.0)
                FROM seller_earnings se
                JOIN sales s ON s.id = se.sale_id
                WHERE s.sale_date = ?
            ''', (today,))
        result = cursor.fetchone()
        conn.close()
        return float(result[0]) if result else 0.0
    except Exception:
        return 0.0


async def build_admin_dashboard(current_db, today: str, now_str: str,
                                user_id: int = 0, telegram_id: int = 0,
                                scope_type: str = None, scope_values: list = None,
                                scope_value: str = None, period: str = 'today',
                                plans_page: int = 0) -> tuple:
    """Дашборд администратора с фильтрацией по зоне ответственности.

    scope_type: None/'all' — весь орг; 'shop'/'city'/'network' — конкретная зона.
    scope_values: list[str] — список значений зоны (поддерживает multi-scope).
    scope_value: str — обратная совместимость (одиночное значение).
    period: 'today'/'week'/'month' — временной период для продаж.
    """
    # Нормализация: старый code может передать scope_value (str)
    if scope_values is None and scope_value:
        scope_values = [scope_value]
    scope_values = scope_values or []

    today_dt    = datetime.strptime(today, '%Y-%m-%d')
    year        = today_dt.year
    month       = today_dt.month
    month_start = f"{year}-{month:02d}-01"
    week_start  = (today_dt.date() - timedelta(days=today_dt.weekday())).isoformat()

    _period_labels = {'today': 'Сегодня', 'week': 'Неделя', 'month': 'Месяц'}
    period_label = _period_labels.get(period, 'Сегодня')
    if period == 'week':
        start_date = week_start
    elif period == 'month':
        start_date = month_start
    else:
        start_date = today

    scale        = _get_dashboard_scale(scope_type, scope_values)
    scope_kwargs = _scope_filter_kwargs(scope_type, scope_values)

    # ── Параллельные запросы к БД + sync-функции через to_thread ────────────
    _base_tasks = [
        current_db.get_sales_summary(start_date=start_date, end_date=today, **scope_kwargs),
        current_db.get_plans_progress(today_dt.date()),
        current_db.get_contests(status='active'),
        asyncio.to_thread(_low_stock_count, current_db.db_file,
                          scope_type=scope_type, scope_values=scope_values),
        asyncio.to_thread(_today_total_earnings, current_db.db_file, today,
                          scope_type=scope_type, scope_values=scope_values),
    ]
    _salary_tasks = (
        [
            current_db.get_salary_rate(user_id),
            current_db.get_worked_days_count(user_id, year, month),
            current_db.get_seller_total_earnings(user_id, start_date=month_start, end_date=today),
            current_db.get_user_contest_rewards(telegram_id, month_start, today),
            current_db.get_paid_absence_days_count(user_id, year, month),
            current_db.get_salary_adjustments_sum(user_id, year, month),
        ]
        if user_id else []
    )
    _results = await asyncio.gather(*_base_tasks, *_salary_tasks, return_exceptions=True)

    _r = lambda i, default=None: _results[i] if not isinstance(_results[i], Exception) else default

    _summary       = _r(0)
    total_sales    = int(_summary[0] or 0) if _summary else 0
    total_qty      = int(_summary[1] or 0) if _summary else 0
    total_revenue  = float(_summary[2] or 0.0) if _summary else 0.0

    plans_progress = _r(1, []) or []
    if scope_type == 'shop' and len(scope_values) == 1:
        sv = scope_values[0]
        plans_progress = [
            (p, a, pct) for p, a, pct in plans_progress
            if p[4] == 'seller' or (p[4] == 'shop' and p[6] == sv)
        ]

    active_contests = _r(2, []) or []
    low_stock       = _r(3, 0) or 0
    today_earnings  = _r(4, 0.0) or 0.0

    salary = worked_days = daily_rate = 0.0
    motivations = contest_rewards = adj_sum_val = 0.0
    paid_absence_days = 0
    if user_id:
        daily_rate        = _r(5, 0.0) or 0.0
        worked_days       = _r(6, 0) or 0
        _earn             = _r(7, {}) or {}
        motivations       = _earn.get('total_earnings', 0.0)
        contest_rewards   = _r(8, 0.0) or 0.0
        paid_absence_days = _r(9, 0) or 0
        adj_sum_val       = _r(10, 0.0) or 0.0
        salary            = daily_rate * (worked_days + paid_absence_days)

    month_ru = MONTH_NAMES_RU.get(month, str(month))

    try:
        role = get_user_org_role(telegram_id) or 'admin'
        custom_title = get_user_custom_title(telegram_id)
        role_label = get_role_display_label(role, scope_type, scope_values, custom_title)
    except Exception:
        role_label = '🛡️ Администратор'

    # ── Заголовок ────────────────────────────────────────────────────────────
    text  = f"📊 <b>ДАШБОРД</b> · {role_label}\n"
    text += f"━━━━━━━━━━━━━━━━━━━━\n"
    text += f"📅 {now_str}\n"

    if scope_type and scope_type != 'all' and scope_values:
        icon = _SCOPE_ICONS.get(scope_type, '📍')
        sname = _SCOPE_NAMES.get(scope_type, 'Зона')
        vals_str = ', '.join(he(v) for v in scope_values[:3])
        if len(scope_values) > 3:
            vals_str += f' +{len(scope_values) - 3}'
        text += f"{icon} <b>{sname}:</b> {vals_str}\n"

    text += "\n"

    # ── Зарплата (общая для всех масштабов) ──────────────────────────────────
    text += f"💰 <b>Моя зарплата — {month_ru} {year}</b>\n"
    if user_id and daily_rate > 0:
        if paid_absence_days:
            text += (f"• Оклад: ({worked_days}+{paid_absence_days} оплач.) × {daily_rate:,.0f} ₽"
                     f" = <b>{salary:,.0f} ₽</b>\n")
        else:
            text += (f"• Оклад: {worked_days} смен × {daily_rate:,.0f} ₽"
                     f" = <b>{salary:,.0f} ₽</b>\n")
    elif user_id:
        text += "• Оклад: не установлен\n"
    else:
        text += "• Данные недоступны\n"
    text += f"• Мотивация: <b>+{motivations:,.0f} ₽</b>\n"
    if adj_sum_val != 0:
        sign = '+' if adj_sum_val > 0 else ''
        text += f"• Корректировки: <b>{sign}{adj_sum_val:,.0f} ₽</b>\n"
    if contest_rewards > 0:
        text += f"• Призы конкурсов: <b>+{contest_rewards:,.0f} ₽</b>\n"
    text += f"• Итого: <b>{salary + motivations + adj_sum_val + contest_rewards:,.0f} ₽</b>\n\n"

    # ── Продажи ──────────────────────────────────────────────────────────────
    sales_scope_label = {
        'single': f'Магазин · {period_label}',
        'wide':   f'Зона · {period_label}',
        'org':    f'Организация · {period_label}',
    }.get(scale, period_label)

    text += f"🛒 <b>Продажи · {sales_scope_label}</b>\n"
    text += f"• Транзакций: <b>{total_sales}</b>\n"
    text += f"• Продано: <b>{total_qty} шт.</b>\n"
    text += f"• Выручка: <b>{total_revenue:,.0f} ₽</b>\n"
    if today_earnings > 0:
        text += f"• Мотивация (выплачено): <b>{today_earnings:,.0f} ₽</b>\n"
    text += "\n"

    # ── Разбивка по магазинам (wide / org) ───────────────────────────────────
    if scale in ('wide', 'org'):
        shop_rows = await asyncio.to_thread(
            _per_shop_breakdown,
            current_db.db_file, start_date, today, scope_type, scope_values, 7
        )
        if shop_rows:
            if scale == 'org':
                text += "🏆 <b>Топ-3 магазина</b>\n"
                medals = ['🥇', '🥈', '🥉']
                for i, (sn, txn, qty, rev) in enumerate(shop_rows[:3]):
                    text += f"  {medals[i]} {he(sn)}: <b>{rev:,.0f} ₽</b> · {txn} тр.\n"
                if len(shop_rows) > 3:
                    text += f"  ···  всего магазинов: {len(shop_rows)}\n"
            else:
                text += "🏪 <b>По магазинам</b>\n"
                for sn, txn, qty, rev in shop_rows[:7]:
                    text += f"  • {he(sn)}: {txn} тр. · <b>{rev:,.0f} ₽</b>\n"
                if len(shop_rows) > 7:
                    text += f"  ···  ещё {len(shop_rows) - 7} магазинов\n"
            text += "\n"

    # ── Остатки ───────────────────────────────────────────────────────────────
    text += "⚠️ <b>Остатки</b>\n"
    if scale == 'single' and low_stock:
        items = await asyncio.to_thread(_low_stock_items, current_db.db_file, scope_type, scope_values, 5)
        if items:
            for iname, iqty in items:
                text += f"  • {he(iname)}: <b>{iqty} шт.</b>\n"
            if low_stock > len(items):
                text += f"  ···  ещё {low_stock - len(items)} позиций\n"
        else:
            text += f"• Заканчивается товаров: <b>{low_stock}</b>\n"
    elif low_stock:
        text += f"• Заканчивается товаров: <b>{low_stock}</b>\n"
    else:
        text += "• Всё в норме ✅\n"
    text += "\n"

    # ── Планы продаж ─────────────────────────────────────────────────────────
    text += "📋 <b>Планы продаж</b>\n\n"
    total_plan_pages = 1
    if plans_progress:
        total_plan_pages = max(1, (len(plans_progress) + _PLANS_PER_PAGE - 1) // _PLANS_PER_PAGE)
        plans_page = max(0, min(plans_page, total_plan_pages - 1))
        page_start = plans_page * _PLANS_PER_PAGE
        page_end   = page_start + _PLANS_PER_PAGE
        for plan_row, actual, pct in plans_progress[page_start:page_end]:
            try:
                text += _plan_summary_line(plan_row, actual, pct) + "\n\n"
            except Exception:
                pass
        if total_plan_pages > 1:
            text += f"<i>Стр. {plans_page + 1} из {total_plan_pages} · всего {len(plans_progress)} планов</i>\n\n"
    else:
        text += "• Активных планов нет\n\n"

    # ── Конкурсы ─────────────────────────────────────────────────────────────
    if active_contests:
        text += await _contest_block(active_contests, today,
                                    current_db=current_db, telegram_id=telegram_id)

    # ── Команда сегодня ───────────────────────────────────────────────────────
    text += "👥 <b>Команда сегодня</b>\n"
    if scale == 'single':
        staff_on_shift = await asyncio.to_thread(
            _on_shift_details, current_db.db_file, today, scope_type, scope_values
        )
        if staff_on_shift:
            text += f"• На смене: <b>{len(staff_on_shift)} чел.</b>\n"
            for fn, ln, sn in staff_on_shift:
                pname = f"{he(ln)} {he(fn)}".strip()
                text += f"  — {pname}\n"
        else:
            text += "• Никто ещё не отмечен\n"
    elif scale == 'wide':
        by_shop = await asyncio.to_thread(_staff_by_shop_with_names, current_db.db_file, today, scope_type, scope_values)
        total_staff = sum(len(names) for _, names in by_shop)
        if total_staff:
            text += f"• На смене: <b>{total_staff} чел.</b>\n"
            for sn, names in by_shop[:8]:
                name_parts = [he((ln + ' ' + fn).strip() or fn) for fn, ln in names[:4]]
                names_str = ", ".join(p for p in name_parts if p)
                if len(names) > 4:
                    names_str += f" и ещё {len(names) - 4}"
                text += f"  {he(sn)}: {len(names)} ({names_str})\n"
            if len(by_shop) > 8:
                text += f"  ···  ещё {len(by_shop) - 8} магазинов\n"
        else:
            text += "• Никто ещё не отмечен\n"
    else:
        by_shop = await asyncio.to_thread(_staff_by_shop_with_names, current_db.db_file, today, scope_type, scope_values)
        total_staff = sum(len(names) for _, names in by_shop)
        if total_staff:
            text += f"• На смене: <b>{total_staff} чел.</b>\n"
            for sn, names in by_shop[:8]:
                name_parts = [he((ln + ' ' + fn).strip() or fn) for fn, ln in names[:4]]
                names_str = ", ".join(p for p in name_parts if p)
                if len(names) > 4:
                    names_str += f" и ещё {len(names) - 4}"
                text += f"  {he(sn)}: {len(names)} ({names_str})\n"
            if len(by_shop) > 8:
                text += f"  ···  ещё {len(by_shop) - 8} магазинов\n"
        else:
            text += "• Никто ещё не отмечен\n"

    return text, total_plan_pages


async def build_user_dashboard(current_db, user_id: int, telegram_id: int,
                               today: str, now_str: str, period: str = 'today',
                               plans_page: int = 0) -> tuple:
    today_dt  = datetime.strptime(today, '%Y-%m-%d')
    year      = today_dt.year
    month     = today_dt.month
    month_start = f"{year}-{month:02d}-01"
    week_start  = (today_dt.date() - timedelta(days=today_dt.weekday())).isoformat()

    _period_labels = {'today': 'Сегодня', 'week': 'Неделя', 'month': 'Месяц'}
    period_label = _period_labels.get(period, 'Сегодня')
    if period == 'week':
        sales_start = week_start
    elif period == 'month':
        sales_start = month_start
    else:
        sales_start = today

    # ── Параллельные запросы к БД (asyncio.gather) ───────────────────────────
    _ures = await asyncio.gather(
        current_db.get_salary_rate(user_id),
        current_db.get_worked_days_count(user_id, year, month),
        current_db.get_seller_total_earnings(user_id, start_date=month_start, end_date=today),
        current_db.get_seller_total_earnings(user_id, start_date=sales_start, end_date=today),
        current_db.get_sales_summary(start_date=sales_start, end_date=today, user_id=user_id),
        current_db.get_user_contest_rewards(telegram_id, month_start, today),
        current_db.get_user_plans_progress(telegram_id, today_dt.date()),
        current_db.get_contests(status='active'),
        current_db.get_paid_absence_days_count(user_id, year, month),
        current_db.get_salary_adjustments_sum(user_id, year, month),
        return_exceptions=True,
    )

    _ur = lambda i, default=None: _ures[i] if not isinstance(_ures[i], Exception) else default

    daily_rate        = _ur(0, 0.0) or 0.0
    worked_days       = _ur(1, 0) or 0
    _earn_m           = _ur(2, {}) or {}
    motivations       = _earn_m.get('total_earnings', 0.0)
    _earn_p           = _ur(3, {}) or {}
    period_motivations = _earn_p.get('total_earnings', 0.0)
    _psumm            = _ur(4)
    period_sales      = int(_psumm[0] or 0) if _psumm else 0
    period_qty        = int(_psumm[1] or 0) if _psumm else 0
    period_rev        = float(_psumm[2] or 0.0) if _psumm else 0.0
    contest_rewards   = _ur(5, 0.0) or 0.0
    plans_progress    = _ur(6, []) or []
    user_contests     = _ur(7, []) or []
    paid_absence_days = _ur(8, 0) or 0
    adj_sum_val       = _ur(9, 0.0) or 0.0
    salary            = daily_rate * (worked_days + paid_absence_days)

    month_ru = MONTH_NAMES_RU.get(month, str(month))

    text  = f"📊 <b>МОЙ ДАШБОРД</b>\n"
    text += f"━━━━━━━━━━━━━━━━━━━━\n"
    text += f"📅 {now_str}\n\n"

    text += f"💰 <b>Моя зарплата — {month_ru} {year}</b>\n"
    if daily_rate > 0:
        if paid_absence_days:
            text += (f"• Оклад: ({worked_days}+{paid_absence_days} оплач.) × {daily_rate:,.0f} ₽"
                     f" = <b>{salary:,.0f} ₽</b>\n")
        else:
            text += (f"• Оклад: {worked_days} смен × {daily_rate:,.0f} ₽"
                     f" = <b>{salary:,.0f} ₽</b>\n")
    else:
        text += "• Оклад: не установлен\n"
    text += f"• Мотивация (месяц): <b>+{motivations:,.0f} ₽</b>\n"
    if adj_sum_val != 0:
        sign = '+' if adj_sum_val > 0 else ''
        text += f"• Корректировки: <b>{sign}{adj_sum_val:,.0f} ₽</b>\n"
    if contest_rewards > 0:
        text += f"• Призы конкурсов: <b>+{contest_rewards:,.0f} ₽</b>\n"
    text += f"• Итого: <b>{salary + motivations + adj_sum_val + contest_rewards:,.0f} ₽</b>\n\n"

    text += f"🛒 <b>Мои продажи · {period_label}</b>\n"
    text += f"• Транзакций: <b>{period_sales}</b>\n"
    text += f"• Продано: <b>{period_qty} шт.</b>\n"
    text += f"• Выручка: <b>{period_rev:,.0f} ₽</b>\n"
    if period_motivations > 0:
        text += f"• Мотивация: <b>+{period_motivations:,.0f} ₽</b>\n"
    text += "\n"

    total_plan_pages = 1
    if plans_progress:
        total_plan_pages = max(1, (len(plans_progress) + _PLANS_PER_PAGE - 1) // _PLANS_PER_PAGE)
        plans_page = max(0, min(plans_page, total_plan_pages - 1))
        page_start = plans_page * _PLANS_PER_PAGE
        page_end   = page_start + _PLANS_PER_PAGE
        text += "📋 <b>Мои планы</b>\n\n"
        for plan_row, actual, pct in plans_progress[page_start:page_end]:
            text += _plan_summary_line(plan_row, actual, pct) + "\n\n"
        if total_plan_pages > 1:
            text += f"<i>Стр. {plans_page + 1} из {total_plan_pages} · всего {len(plans_progress)} планов</i>\n\n"

    if user_contests:
        try:
            text += await _contest_block(user_contests, today,
                                         current_db=current_db, telegram_id=telegram_id)
        except Exception:
            pass

    return text, total_plan_pages


async def build_admin_daily_text(current_db, yesterday: str, shop_name: str | None,
                                 scope_type: str = None, scope_values: list = None,
                                 scope_value: str = None,
                                 telegram_id: int | None = None,
                                 staff_link_base: str | None = None) -> str:
    """Текст для ежедневного отчёта администратору.
    shop_name — для обратной совместимости; scope_type/scope_values — новый способ.
    staff_link_base — готовый префикс «{web_url}/auth/code/auto?c={code}&next=»,
    генерируется снаружи чтобы один код использовался и для seller-ссылок, и для кнопки.
    telegram_id — устаревший способ; если staff_link_base не передан, код генерируется
    внутри функции (но тогда кнопка «Отчёт в вебе» НЕ должна генерировать второй код)."""
    if scope_values is None and scope_value:
        scope_values = [scope_value]
    scope_values = scope_values or []

    if not scope_type and shop_name:
        scope_type = 'shop'
        scope_values = [shop_name]

    scope_kwargs = _scope_filter_kwargs(scope_type, scope_values)

    summary = await current_db.get_sales_summary(
        start_date=yesterday, end_date=yesterday, **scope_kwargs
    )
    total_sales   = int(summary[0] or 0) if summary else 0
    total_qty     = int(summary[1] or 0) if summary else 0
    total_revenue = float(summary[2] or 0.0) if summary else 0.0

    msg = f"📊 <b>Итоги вчера ({yesterday})</b>\n"
    if scope_type and scope_type != 'all' and scope_values:
        icon = _SCOPE_ICONS.get(scope_type, '📍')
        vals_str = ', '.join(he(v) for v in scope_values[:3])
        msg += f"{icon} {vals_str}\n"
    msg += f"• Транзакций: {total_sales}\n"
    msg += f"• Продано: {total_qty} шт.\n"
    msg += f"• Выручка: {total_revenue:,.0f} ₽\n"

    # ── Топ продавцов с deep-links ──────────────────────────────────────────
    try:
        ranking = await current_db.get_sales_ranking(
            yesterday, yesterday, **scope_kwargs
        )
        if ranking:
            _view_year  = int(yesterday[:4])
            _view_month = int(yesterday[5:7])

            # Предпочитаем готовый staff_link_base (код уже сгенерирован снаружи).
            # Если не передан — генерируем сами из telegram_id (legacy path).
            _resolved_base = staff_link_base
            if not _resolved_base and telegram_id:
                try:
                    from keyboards import _get_web_interface_url as _gwiu
                    _wu = _gwiu()
                    if _wu:
                        from web_login_codes import generate_code as _gc
                        _code = _gc(telegram_id)
                        _resolved_base = f"{_wu.rstrip('/')}/auth/code/auto?c={_code}&next="
                except Exception:
                    pass

            from urllib.parse import quote as _uq
            medals = ['🥇', '🥈', '🥉', '4.', '5.']
            msg += "\n🏆 <b>Топ продавцов</b>\n"
            for i, row in enumerate(ranking[:5]):
                fn   = row[0] or ''
                ln   = row[1] or ''
                qty  = int(row[3] or 0)
                rev  = float(row[4] or 0.0)
                uid  = row[7] if len(row) > 7 else None
                medal = medals[i] if i < len(medals) else f"{i+1}."

                if _resolved_base and uid:
                    _nxt = _uq(f"/staff/{uid}?year={_view_year}&month={_view_month}", safe='')
                    name_str = f"<a href='{_resolved_base}{_nxt}'>{he(fn)} {he(ln)}</a>"
                else:
                    name_str = f"{he(fn)} {he(ln)}"

                msg += f"{medal} {name_str} — {qty} шт. · {rev:,.0f} ₽\n"
    except Exception:
        pass

    return msg


async def build_user_daily_text(current_db, user_id: int,
                                yesterday: str, shop_name: str | None) -> str:
    summary = await current_db.get_sales_summary(
        start_date=yesterday, end_date=yesterday, shop_name=shop_name
    )
    total_sales   = int(summary[0] or 0) if summary else 0
    total_qty     = int(summary[1] or 0) if summary else 0
    total_revenue = float(summary[2] or 0.0) if summary else 0.0

    try:
        earnings    = await current_db.get_seller_total_earnings(
            user_id, start_date=yesterday, end_date=yesterday
        )
        motivations = earnings.get('total_earnings', 0.0)
    except Exception:
        motivations = 0.0

    msg = f"📊 <b>Итоги вчера ({yesterday})</b>\n"
    if shop_name:
        msg += f"🏪 Магазин: {he(shop_name)}\n"
    msg += f"• Транзакций: {total_sales}\n"
    msg += f"• Продано: {total_qty} шт.\n"
    msg += f"• Выручка: {total_revenue:,.0f} ₽\n"
    msg += f"• Мотивация: {motivations:,.0f} ₽\n"
    return msg


def _dashboard_period_kb(period: str, plans_page: int = 0,
                         plans_total: int = 1,
                         web_btn: InlineKeyboardButton | None = None) -> InlineKeyboardMarkup:
    """Клавиатура переключения периода дашборда."""
    periods = [
        ('today', '📅 Сегодня'),
        ('week',  '📆 Неделя'),
        ('month', '🗓 Месяц'),
    ]
    buttons = []
    for p, label in periods:
        text_label = f"• {label}" if p == period else label
        buttons.append(InlineKeyboardButton(text=text_label, callback_data=f"dash_p_{p}"))
    rows = [buttons]
    if plans_total > 1:
        nav = []
        if plans_page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data="dash_pp_p"))
        nav.append(InlineKeyboardButton(
            text=f"📋 {plans_page + 1}/{plans_total}", callback_data="dash_pp_i"))
        if plans_page < plans_total - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data="dash_pp_n"))
        rows.append(nav)
    if web_btn:
        rows.append([web_btn])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"dash_p_{period}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_dashboard(callback: CallbackQuery, state: FSMContext,
                             period: str = 'today', plans_page: int | None = None):
    from env_manager import env_manager as _env
    is_super_admin = _env.is_super_admin(callback.from_user.id)
    # Отвечаем немедленно — кнопка разблокируется, пока строится дашборд
    await callback.answer("⏳ Загрузка...")
    current_db = await get_db(callback.from_user.id, state)
    user = await current_db.get_user(callback.from_user.id)
    if not user:
        await callback.message.edit_text("❌ Сначала завершите регистрацию через /start")
        return

    from timezone_utils import get_current_user_time
    _user_tz = await current_db.get_user_timezone(callback.from_user.id)
    _now_local = get_current_user_time(_user_tz)
    today   = _now_local.date().isoformat()
    now_str = _now_local.strftime("%d.%m.%Y · %H:%M")

    # Читаем сохранённую страницу планов из FSM (если не передана явно)
    if plans_page is None:
        _data = await state.get_data()
        plans_page = _data.get('dash_plans_pg', 0)

    if is_any_admin(callback.from_user.id) or is_super_admin:
        scope_type, scope_values = get_user_org_scope(callback.from_user.id)
        text, total_plan_pages = await build_admin_dashboard(
            current_db, today, now_str, user[0], callback.from_user.id,
            scope_type=scope_type, scope_values=scope_values, period=period,
            plans_page=plans_page
        )
    else:
        text, total_plan_pages = await build_user_dashboard(
            current_db, user[0], callback.from_user.id, today, now_str,
            period=period, plans_page=plans_page
        )

    # Нормализуем страницу и сохраняем в FSM
    plans_page = max(0, min(plans_page, total_plan_pages - 1))
    await state.update_data(dash_plans_pg=plans_page, dash_period=period)

    text += hint_suffix(current_db, user[0], 'first_dashboard')

    _web_btn = None
    if is_any_admin(callback.from_user.id) or is_super_admin:
        try:
            from keyboards import _get_web_interface_url as _gwiu
            from urllib.parse import quote as _uq
            _wu = _gwiu()
            if _wu:
                from web_login_codes import generate_code as _gc
                _code = _gc(callback.from_user.id)
                _today_d = _now_local.date()
                if period == 'week':
                    _df = (_today_d - timedelta(days=6)).isoformat()
                elif period == 'month':
                    _df = _today_d.replace(day=1).isoformat()
                else:
                    _df = _today_d.isoformat()
                _dt = _today_d.isoformat()
                _nxt = f"/reports?period=custom&date_from={_df}&date_to={_dt}"
                _lurl = f"{_wu.rstrip('/')}/auth/code/auto?c={_code}&next={_uq(_nxt, safe='')}"
                _web_btn = InlineKeyboardButton(text="🌐 Отчёт в вебе", url=_lurl)
        except Exception:
            pass

    await safe_edit_message(callback, text, parse_mode="HTML",
                            reply_markup=_dashboard_period_kb(period, plans_page, total_plan_pages,
                                                              web_btn=_web_btn))


@router.callback_query(lambda c: c.data == "dashboard")
async def show_dashboard(callback: CallbackQuery, state: FSMContext):
    await _render_dashboard(callback, state, period='today')


@router.callback_query(F.data.startswith("dash_p_"))
async def show_dashboard_period(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    period = callback.data.replace("dash_p_", "")
    if period not in ('today', 'week', 'month'):
        period = 'today'
    # Смена периода — сбрасываем страницу планов на первую
    await _render_dashboard(callback, state, period=period, plans_page=0)


@router.callback_query(F.data == "dash_pp_n")
async def dash_plans_next(callback: CallbackQuery, state: FSMContext):
    """Следующая страница планов в дашборде."""
    await callback.answer()
    _data = await state.get_data()
    period = _data.get('dash_period', 'today')
    plans_page = _data.get('dash_plans_pg', 0) + 1
    await _render_dashboard(callback, state, period=period, plans_page=plans_page)


@router.callback_query(F.data == "dash_pp_p")
async def dash_plans_prev(callback: CallbackQuery, state: FSMContext):
    """Предыдущая страница планов в дашборде."""
    await callback.answer()
    _data = await state.get_data()
    period = _data.get('dash_period', 'today')
    plans_page = max(0, _data.get('dash_plans_pg', 0) - 1)
    await _render_dashboard(callback, state, period=period, plans_page=plans_page)


@router.callback_query(F.data == "dash_pp_i")
async def dash_plans_indicator(callback: CallbackQuery):
    """Индикатор страницы планов — noop."""
    await callback.answer()
