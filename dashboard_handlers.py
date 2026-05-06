"""
Дашборд-сводка для главного меню.
Для администратора — сводка по организации сегодня (с учётом зоны ответственности).
Для продавца — зарплата/мотивация/планы за текущий месяц.
"""
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


def _contest_block(contests: list, today_str: str) -> str:
    """Развёрнутый блок конкурсов для дашборда.

    contests — список из get_contests(status='active').
    Колонки: 0:id 1:title 3:contest_type 4:metric_type 5:target_value
             6:reward_type 7:reward_value 8:start_date 9:end_date
    """
    if not contests:
        return ""
    today_dt = datetime.strptime(today_str, '%Y-%m-%d').date()
    text = f"🏆 <b>Конкурсы</b> · активных <b>{len(contests)}</b>\n\n"
    for c in contests[:4]:          # не больше 4, чтобы не перегружать дашборд
        try:
            title        = he(c[1] or "Без названия")
            ctype_label  = _CONTEST_TYPE_LABEL.get(c[3], '🥊')
            metric_short = _METRIC_SHORT.get(c[4], c[4] or '?')
            target       = c[5]
            reward_icon  = _REWARD_TYPE_LABEL.get(c[6], '🎁')
            reward_val   = c[7]
            end_date_str = c[9]

            # цель
            if c[4] == 'turnover':
                target_str = f"{float(target):,.0f} ₽" if target else "?"
            else:
                target_str = f"{int(float(target))} шт." if target else "?"

            # приз
            try:
                reward_str = f"{float(reward_val):,.0f} ₽" if c[6] == 'money' else he(str(reward_val))
            except (TypeError, ValueError):
                reward_str = he(str(reward_val)) if reward_val else "?"

            # дедлайн
            try:
                end_dt   = datetime.strptime(end_date_str, '%Y-%m-%d').date()
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
            text += f"  📊 {metric_short} · цель: {target_str}\n"
            text += f"  📅 {deadline_str}\n"
            text += f"  {reward_icon} Приз: {reward_str}\n\n"
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
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        base = '''
            SELECT u.shop_name,
                   COUNT(DISTINCT s.id),
                   COALESCE(SUM(s.quantity), 0),
                   COALESCE(SUM(s.total_price), 0.0)
            FROM sales s
            JOIN users u ON s.user_id = u.id
            WHERE s.sale_date BETWEEN ? AND ?
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
        conn = sqlite3.connect(db_file)
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


def _low_stock_items(db_file: str, scope_type: str, scope_values: list,
                     threshold: int = 5, limit: int = 5) -> list:
    """Конкретные товары с низким остатком (для single-масштаба): [(name, qty), ...]."""
    try:
        conn = sqlite3.connect(db_file)
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
        conn = sqlite3.connect(db_file)
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
            cursor.execute(
                f'SELECT DISTINCT shop_name FROM users WHERE {field} IN ({ph}) AND shop_name IS NOT NULL',
                vals
            )
            shops = [r[0] for r in cursor.fetchall()]
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
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        base = '''
            SELECT u.first_name, u.last_name, u.shop_name
            FROM work_schedule ws
            JOIN users u ON u.id = ws.user_id
            WHERE ws.work_date = ?
        '''
        params = [today]
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
        conn = sqlite3.connect(db_file)
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


def build_admin_dashboard(current_db, today: str, now_str: str,
                          user_id: int = 0, telegram_id: int = 0,
                          scope_type: str = None, scope_values: list = None,
                          scope_value: str = None, period: str = 'today') -> str:
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

    try:
        summary = current_db.get_sales_summary(start_date=start_date, end_date=today, **scope_kwargs)
        total_sales   = int(summary[0] or 0) if summary else 0
        total_qty     = int(summary[1] or 0) if summary else 0
        total_revenue = float(summary[2] or 0.0) if summary else 0.0
    except Exception:
        total_sales = total_qty = 0
        total_revenue = 0.0

    low_stock      = _low_stock_count(current_db.db_file, scope_type=scope_type, scope_values=scope_values)
    today_earnings = _today_total_earnings(current_db.db_file, today, scope_type=scope_type, scope_values=scope_values)

    plans_progress = []
    try:
        plans_progress = current_db.get_plans_progress()
        if scope_type == 'shop' and len(scope_values) == 1:
            sv = scope_values[0]
            plans_progress = [
                (p, a, pct) for p, a, pct in plans_progress
                if p[4] == 'seller' or (p[4] == 'shop' and p[6] == sv)
            ]
    except Exception:
        pass

    active_contests = []
    try:
        active_contests = current_db.get_contests(status='active') or []
    except Exception:
        pass

    salary = worked_days = daily_rate = 0.0
    motivations = contest_rewards = 0.0
    if user_id:
        try:
            daily_rate  = current_db.get_salary_rate(user_id)
            worked_days = current_db.get_worked_days_count(user_id, year, month)
            salary      = daily_rate * worked_days
        except Exception:
            pass
        try:
            earnings    = current_db.get_seller_total_earnings(
                user_id, start_date=month_start, end_date=today
            )
            motivations = earnings.get('total_earnings', 0.0)
        except Exception:
            pass
        try:
            contest_rewards = current_db.get_user_contest_rewards(telegram_id, month_start, today)
        except Exception:
            pass

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
        text += (f"• Оклад: {worked_days} смен × {daily_rate:,.0f} ₽"
                 f" = <b>{salary:,.0f} ₽</b>\n")
    elif user_id:
        text += "• Оклад: не установлен\n"
    else:
        text += "• Данные недоступны\n"
    text += f"• Мотивация: <b>+{motivations:,.0f} ₽</b>\n"
    if contest_rewards > 0:
        text += f"• Призы конкурсов: <b>+{contest_rewards:,.0f} ₽</b>\n"
    text += f"• Итого: <b>{salary + motivations + contest_rewards:,.0f} ₽</b>\n\n"

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
        shop_rows = _per_shop_breakdown(
            current_db.db_file, start_date, today, scope_type, scope_values, limit=7
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
        items = _low_stock_items(current_db.db_file, scope_type, scope_values, limit=5)
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
    if plans_progress:
        MAX_BARS = 8 if scale == 'single' else 6
        for plan_row, actual, pct in plans_progress[:MAX_BARS]:
            try:
                text += _plan_summary_line(plan_row, actual, pct) + "\n\n"
            except Exception:
                pass
        if len(plans_progress) > MAX_BARS:
            rest = plans_progress[MAX_BARS:]
            on_track = sum(1 for _, _, p in rest if p >= 75)
            text += f"  ···  ещё {len(rest)} планов · ✅ {on_track} в графике\n\n"
    else:
        text += "• Активных планов нет\n\n"

    # ── Конкурсы ─────────────────────────────────────────────────────────────
    if active_contests:
        text += _contest_block(active_contests, today)

    # ── Команда сегодня ───────────────────────────────────────────────────────
    text += "👥 <b>Команда сегодня</b>\n"
    if scale == 'single':
        staff_on_shift = _on_shift_details(
            current_db.db_file, today, scope_type=scope_type, scope_values=scope_values
        )
        if staff_on_shift:
            text += f"• На смене: <b>{len(staff_on_shift)} чел.</b>\n"
            for fn, ln, sn in staff_on_shift:
                pname = f"{he(ln)} {he(fn)}".strip()
                text += f"  — {pname}\n"
        else:
            text += "• Никто ещё не отмечен\n"
    elif scale == 'wide':
        by_shop = _staff_by_shop(current_db.db_file, today, scope_type, scope_values)
        total_staff = sum(cnt for _, cnt in by_shop)
        if total_staff:
            text += f"• На смене: <b>{total_staff} чел.</b>\n"
            for sn, cnt in by_shop[:6]:
                text += f"  {he(sn)}: {cnt}\n"
            if len(by_shop) > 6:
                text += f"  ···  ещё {len(by_shop) - 6} магазинов\n"
        else:
            text += "• Никто ещё не отмечен\n"
    else:
        staff_total = len(_on_shift_details(current_db.db_file, today))
        if staff_total:
            text += f"• На смене: <b>{staff_total} чел.</b>\n"
        else:
            text += "• Никто ещё не отмечен\n"

    return text


def build_user_dashboard(current_db, user_id: int, telegram_id: int,
                         today: str, now_str: str, period: str = 'today') -> str:
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

    try:
        daily_rate  = current_db.get_salary_rate(user_id)
        worked_days = current_db.get_worked_days_count(user_id, year, month)
        salary      = daily_rate * worked_days
    except Exception:
        daily_rate = worked_days = salary = 0.0

    try:
        earnings    = current_db.get_seller_total_earnings(
            user_id, start_date=month_start, end_date=today
        )
        motivations = earnings.get('total_earnings', 0.0)
    except Exception:
        motivations = 0.0

    try:
        period_earnings = current_db.get_seller_total_earnings(
            user_id, start_date=sales_start, end_date=today
        )
        period_motivations = period_earnings.get('total_earnings', 0.0)
    except Exception:
        period_motivations = 0.0

    try:
        period_summary = current_db.get_sales_summary(
            start_date=sales_start, end_date=today, user_id=user_id
        )
        period_sales = int(period_summary[0] or 0) if period_summary else 0
        period_qty   = int(period_summary[1] or 0) if period_summary else 0
        period_rev   = float(period_summary[2] or 0.0) if period_summary else 0.0
    except Exception:
        period_sales = period_qty = 0
        period_rev = 0.0

    try:
        contest_rewards = current_db.get_user_contest_rewards(telegram_id, month_start, today)
    except Exception:
        contest_rewards = 0.0

    month_ru = MONTH_NAMES_RU.get(month, str(month))

    text  = f"📊 <b>МОЙ ДАШБОРД</b>\n"
    text += f"━━━━━━━━━━━━━━━━━━━━\n"
    text += f"📅 {now_str}\n\n"

    text += f"💰 <b>Моя зарплата — {month_ru} {year}</b>\n"
    if daily_rate > 0:
        text += (f"• Оклад: {worked_days} смен × {daily_rate:,.0f} ₽"
                 f" = <b>{salary:,.0f} ₽</b>\n")
    else:
        text += "• Оклад: не установлен\n"
    text += f"• Мотивация (месяц): <b>+{motivations:,.0f} ₽</b>\n"
    if contest_rewards > 0:
        text += f"• Призы конкурсов: <b>+{contest_rewards:,.0f} ₽</b>\n"
    text += f"• Итого: <b>{salary + motivations + contest_rewards:,.0f} ₽</b>\n\n"

    text += f"🛒 <b>Мои продажи · {period_label}</b>\n"
    text += f"• Транзакций: <b>{period_sales}</b>\n"
    text += f"• Продано: <b>{period_qty} шт.</b>\n"
    text += f"• Выручка: <b>{period_rev:,.0f} ₽</b>\n"
    if period_motivations > 0:
        text += f"• Мотивация: <b>+{period_motivations:,.0f} ₽</b>\n"
    text += "\n"

    # Планы продавца
    plans_progress = []
    try:
        plans_progress = current_db.get_user_plans_progress(telegram_id)
    except Exception:
        pass

    if plans_progress:
        text += "📋 <b>Мои планы</b>\n\n"
        for plan_row, actual, pct in plans_progress:
            text += _plan_summary_line(plan_row, actual, pct) + "\n\n"

    # Конкурсы
    try:
        user_contests = current_db.get_contests(status='active') or []
        if user_contests:
            text += _contest_block(user_contests, today)
    except Exception:
        pass

    return text


def build_admin_daily_text(current_db, yesterday: str, shop_name: str | None,
                           scope_type: str = None, scope_values: list = None,
                           scope_value: str = None) -> str:
    """Текст для ежедневного отчёта администратору.
    shop_name — для обратной совместимости; scope_type/scope_values — новый способ."""
    if scope_values is None and scope_value:
        scope_values = [scope_value]
    scope_values = scope_values or []

    if not scope_type and shop_name:
        scope_type = 'shop'
        scope_values = [shop_name]

    scope_kwargs = _scope_filter_kwargs(scope_type, scope_values)

    summary = current_db.get_sales_summary(
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
    return msg


def build_user_daily_text(current_db, user_id: int,
                          yesterday: str, shop_name: str | None) -> str:
    summary = current_db.get_sales_summary(
        start_date=yesterday, end_date=yesterday, shop_name=shop_name
    )
    total_sales   = int(summary[0] or 0) if summary else 0
    total_qty     = int(summary[1] or 0) if summary else 0
    total_revenue = float(summary[2] or 0.0) if summary else 0.0

    try:
        earnings    = current_db.get_seller_total_earnings(
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


def _dashboard_period_kb(period: str) -> InlineKeyboardMarkup:
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
    return InlineKeyboardMarkup(inline_keyboard=[buttons, [
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"dash_p_{period}")
    ]])


async def _render_dashboard(callback: CallbackQuery, state: FSMContext, period: str = 'today'):
    from env_manager import env_manager as _env
    is_super_admin = _env.is_super_admin(callback.from_user.id)
    current_db = await get_db(callback.from_user.id, state)
    user = current_db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Сначала завершите регистрацию", show_alert=True)
        return
    await callback.answer()

    from timezone_utils import get_current_user_time
    _user_tz = current_db.get_user_timezone(callback.from_user.id)
    _now_local = get_current_user_time(_user_tz)
    today   = date.today().isoformat()
    now_str = _now_local.strftime("%d.%m.%Y · %H:%M")

    if is_any_admin(callback.from_user.id) or is_super_admin:
        scope_type, scope_values = get_user_org_scope(callback.from_user.id)
        text = build_admin_dashboard(
            current_db, today, now_str, user[0], callback.from_user.id,
            scope_type=scope_type, scope_values=scope_values, period=period
        )
    else:
        text = build_user_dashboard(current_db, user[0], callback.from_user.id, today, now_str, period=period)

    text += hint_suffix(current_db, user[0], 'first_dashboard')
    await safe_edit_message(callback.message, text, parse_mode="HTML",
                            reply_markup=_dashboard_period_kb(period))


@router.callback_query(lambda c: c.data == "dashboard")
async def show_dashboard(callback: CallbackQuery, state: FSMContext):
    await _render_dashboard(callback, state, period='today')


@router.callback_query(F.data.startswith("dash_p_"))
async def show_dashboard_period(callback: CallbackQuery, state: FSMContext):
    period = callback.data.replace("dash_p_", "")
    if period not in ('today', 'week', 'month'):
        period = 'today'
    await _render_dashboard(callback, state, period=period)
