"""
web/ai_tools.py — Read-only data providers for the AI assistant tool-calling layer.

Each tool:
  - receives params dict + Database instance bound to the current org_db
  - returns a short formatted string (the "tool result" fed back to the LLM)
  - is strictly read-only and strictly org-scoped (no cross-tenant access)

Public API:
    TOOLS          — dict[name, {description, fn}]
    get_tools_description() -> str
    call_tool(name, params, db) -> str
    get_tool_stats(date_str=None) -> dict   — usage counters for admin/reporting
    reset_tool_stats() -> None              — clear all counters (testing only)
"""
import logging
import datetime as _dt
import threading
from collections import defaultdict

logger = logging.getLogger(__name__)

_SHOP_BOT_DB = "data/shop_bot.db"

# ── Usage counters ────────────────────────────────────────────────────────────
# Key: (tool_name, org_db, date_str)  →  int call count
# In-memory write-through cache. Persisted to ai_tool_stats in shop_bot.db on
# every call so counts survive server restarts. Stats functions read from DB
# (persistent history) and overlay in-memory values (catches writes not yet
# flushed by the background thread).

_stats_lock: threading.Lock = threading.Lock()
_stats: dict[tuple[str, str, str], int] = defaultdict(int)


def _persist_call(tool_name: str, org_db: str, date_str: str) -> None:
    """Write one call increment to shop_bot.db. Runs in a daemon thread."""
    try:
        from database import Database
        Database(_SHOP_BOT_DB).record_ai_tool_call(tool_name, org_db, date_str)
    except Exception as exc:
        logger.warning("ai_tool_stats persist failed: %s", exc)


def _record_call(tool_name: str, org_db: str) -> None:
    """Increment the counter for tool_name × org_db × today (UTC).

    Writes to both the in-memory dict (immediate, thread-safe) and the
    persistent DB table ai_tool_stats in shop_bot.db (background thread).
    """
    date_str = _dt.date.today().isoformat()
    with _stats_lock:
        _stats[(tool_name, org_db, date_str)] += 1
    threading.Thread(
        target=_persist_call, args=(tool_name, org_db, date_str), daemon=True
    ).start()


def _get_shop_bot_db():
    from database import Database
    return Database(_SHOP_BOT_DB)


def get_tool_stats(date_str: str | None = None) -> dict:
    """Return usage counters for a specific date, merging DB + in-memory.

    DB is the source of truth for historical data; in-memory is overlaid to
    capture any calls not yet flushed by the background persist thread.

    Returns a dict:
      {
        "date": "2026-06-16",          # filter date (today if omitted)
        "by_tool": {"get_inventory": 12, "get_tasks": 5, ...},
        "by_org":  {"data/tenants/org_7.db": {"get_inventory": 3, ...}, ...},
        "total_calls": 42,
        "source": "db+memory",
      }
    """
    if date_str is None:
        date_str = _dt.date.today().isoformat()

    combined: dict[tuple[str, str], int] = {}

    try:
        db_rows = _get_shop_bot_db().get_ai_tool_stats_db(date_str)
        combined.update(db_rows)
    except Exception as exc:
        logger.warning("get_tool_stats: DB read failed: %s", exc)

    with _stats_lock:
        snapshot = dict(_stats)
    for (tool, org, day), count in snapshot.items():
        if day != date_str:
            continue
        key = (tool, org)
        combined[key] = max(combined.get(key, 0), count)

    by_tool: dict[str, int] = defaultdict(int)
    by_org: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (tool, org), count in combined.items():
        by_tool[tool] += count
        by_org[org][tool] += count

    return {
        "date": date_str,
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True)),
        "by_org": {org: dict(tools) for org, tools in by_org.items()},
        "total_calls": sum(by_tool.values()),
        "source": "db+memory",
    }


def get_tool_stats_all_dates() -> dict[str, dict]:
    """Return stats for all dates, merging DB + in-memory. Used for history."""
    combined: dict[tuple[str, str, str], int] = {}

    try:
        db_rows = _get_shop_bot_db().get_ai_tool_stats_all_dates_db()
        combined.update(db_rows)
    except Exception as exc:
        logger.warning("get_tool_stats_all_dates: DB read failed: %s", exc)

    with _stats_lock:
        snapshot = dict(_stats)
    for key, count in snapshot.items():
        combined[key] = max(combined.get(key, 0), count)

    per_date_tool: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_date_org: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    for (tool, org, day), count in combined.items():
        per_date_tool[day][tool] += count
        per_date_org[day][org][tool] += count

    all_days = sorted(set(per_date_tool) | set(per_date_org), reverse=True)
    return {
        day: {
            "by_tool": dict(sorted(per_date_tool[day].items(), key=lambda kv: kv[1], reverse=True)),
            "by_org": {
                org: dict(tools)
                for org, tools in per_date_org[day].items()
            },
            "total_calls": sum(per_date_tool[day].values()),
        }
        for day in all_days
    }


def reset_tool_stats() -> None:
    """Clear in-memory counters. Intended for testing only. Does NOT clear DB."""
    with _stats_lock:
        _stats.clear()


# ── Tool implementations ──────────────────────────────────────────────────────

def _tool_get_products(db, params: dict) -> str:
    """Search/list products by name, article, barcode, or category."""
    query    = str(params.get("query", "")).strip()
    category = str(params.get("category", "")).strip()
    limit    = min(int(params.get("limit", 20)), 50)
    try:
        conn = db.get_connection()
        if query:
            rows = conn.execute(
                "SELECT name, category, price, article, barcode FROM products "
                "WHERE name LIKE ? OR UPPER(article) LIKE ? OR barcode LIKE ? "
                "ORDER BY name LIMIT ?",
                (f"%{query}%", f"%{query.upper()}%", f"%{query}%", limit),
            ).fetchall()
        elif category:
            rows = conn.execute(
                "SELECT name, category, price, article, barcode FROM products "
                "WHERE category = ? ORDER BY name LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, category, price, article, barcode FROM products "
                "ORDER BY name LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        if not rows:
            return "Товары не найдены."
        lines = []
        for name, cat, price, art, bc in rows:
            parts = [f"{name} | {cat} | {int(price):,} ₽"]
            if art:
                parts.append(f"арт. {art}")
            if bc:
                parts.append(f"ш/к {bc}")
            lines.append("  - " + " | ".join(parts))
        return f"Товары ({len(rows)}):\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_products: %s", exc)
        return "Ошибка получения списка товаров."


def _tool_get_product_by_code(db, params: dict) -> str:
    """Find a product by barcode or article."""
    code = str(params.get("code", "")).strip()
    if not code:
        return "Укажите параметр code (штрихкод или артикул)."
    try:
        row = db.get_product_by_barcode(code) or db.get_product_by_article(code)
        if not row:
            return f"Товар с кодом «{code}» не найден."
        name   = row[1]
        cat    = row[2]
        price  = row[3]
        art    = row[7] if len(row) > 7 else ""
        bc     = row[8] if len(row) > 8 else ""
        desc   = row[6] if len(row) > 6 else ""
        result = f"Товар: {name}\nКатегория: {cat}\nЦена: {int(price):,} ₽"
        if art:
            result += f"\nАртикул: {art}"
        if bc:
            result += f"\nШтрихкод: {bc}"
        if desc:
            result += f"\nОписание: {str(desc)[:200]}"
        return result
    except Exception as exc:
        logger.warning("ai_tool get_product_by_code: %s", exc)
        return "Ошибка поиска товара."


def _tool_get_inventory(db, params: dict) -> str:
    """Get stock levels, optionally filtered by shop."""
    shop = str(params.get("shop", "")).strip() or None
    try:
        rows = db.get_all_inventory(shop_name=shop)
        if not rows:
            return "Остатки не найдены."
        lines = []
        for r in rows[:30]:
            qty       = r[3]
            name      = r[6]
            shop_name = r[2]
            cat       = r[7]
            lines.append(f"  - {name} ({cat}) | {shop_name}: {qty} шт.")
        total  = len(rows)
        suffix = f" (первые 30 из {total})" if total > 30 else ""
        return f"Остатки{suffix}:\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_inventory: %s", exc)
        return "Ошибка получения остатков."


def _tool_get_low_stock(db, params: dict) -> str:
    """Get items at or below a stock threshold."""
    threshold = int(params.get("threshold", 5))
    try:
        conn = db.get_connection()
        rows = conn.execute(
            """SELECT p.name, p.category, i.shop_name, i.quantity
               FROM inventory i
               JOIN products p ON i.product_id = p.id
               WHERE i.quantity <= ?
               ORDER BY i.quantity ASC LIMIT 30""",
            (threshold,),
        ).fetchall()
        conn.close()
        if not rows:
            return f"Нет товаров с остатком ≤ {threshold} шт."
        lines = [f"  - {r[0]} ({r[1]}) | {r[2]}: {r[3]} шт." for r in rows]
        return f"Товары с низким остатком (≤{threshold} шт.):\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_low_stock: %s", exc)
        return "Ошибка получения данных о низких остатках."


def _tool_get_sales_period(db, params: dict) -> str:
    """Get sales summary + top products for a date range."""
    today     = _dt.date.today()
    date_from = str(params.get("date_from", today.replace(day=1).isoformat()))
    date_to   = str(params.get("date_to",   today.isoformat()))
    shop      = str(params.get("shop", "")).strip() or None
    try:
        summary = db.get_sales_summary(
            start_date=date_from, end_date=date_to, shop_name=shop
        )
        if not summary or not summary[0]:
            return "Нет данных о продажах за указанный период."
        cnt, qty, rev, avg = (
            int(summary[0] or 0), int(summary[1] or 0),
            float(summary[2] or 0), float(summary[3] or 0),
        )
        result = (
            f"Продажи {date_from} — {date_to}"
            + (f" | Магазин: {shop}" if shop else "")
            + f":\n  Транзакций: {cnt}, единиц: {qty}, "
            f"выручка: {int(rev):,} ₽, средний чек: {int(avg):,} ₽"
        )
        try:
            conn = db.get_connection()
            sql  = (
                "SELECT p.name, SUM(s.quantity_sold) AS qty, "
                "SUM(s.quantity_sold * s.sale_price) AS rev "
                "FROM sales s JOIN products p ON s.product_id = p.id "
                "WHERE date(s.sale_date) >= ? AND date(s.sale_date) <= ?"
            )
            sql_params: list = [date_from, date_to]
            if shop:
                sql += " AND s.shop_name = ?"
                sql_params.append(shop)
            sql += " GROUP BY p.id ORDER BY rev DESC LIMIT 5"
            top = conn.execute(sql, sql_params).fetchall()
            conn.close()
            if top:
                lines = [
                    f"  {i+1}. {r[0]}: {int(r[1])} шт., {int(r[2]):,} ₽"
                    for i, r in enumerate(top)
                ]
                result += "\nТоп товаров:\n" + "\n".join(lines)
        except Exception:
            pass
        return result
    except Exception as exc:
        logger.warning("ai_tool get_sales_period: %s", exc)
        return "Ошибка получения данных о продажах."


def _tool_get_recent_transactions(db, params: dict) -> str:
    """Get individual recent sales transactions."""
    today     = _dt.date.today()
    date_from = str(params.get("date_from", today.isoformat()))
    date_to   = str(params.get("date_to",   today.isoformat()))
    shop      = str(params.get("shop", "")).strip() or None
    seller    = str(params.get("seller", "")).strip() or None
    limit     = min(int(params.get("limit", 20)), 50)
    try:
        conn = db.get_connection()
        sql = (
            "SELECT s.id, p.name, s.quantity_sold, s.sale_price, "
            "s.quantity_sold * s.sale_price AS total, "
            "s.shop_name, s.sale_date, "
            "u.first_name || ' ' || u.last_name AS seller_name "
            "FROM sales s "
            "JOIN products p ON s.product_id = p.id "
            "JOIN users u ON s.user_id = u.id "
            "WHERE date(s.sale_date) >= ? AND date(s.sale_date) <= ?"
        )
        sql_params: list = [date_from, date_to]
        if shop:
            sql += " AND s.shop_name = ?"
            sql_params.append(shop)
        if seller:
            sql += " AND (u.first_name LIKE ? OR u.last_name LIKE ?)"
            sql_params.extend([f"%{seller}%", f"%{seller}%"])
        sql += " ORDER BY s.sale_date DESC, s.id DESC LIMIT ?"
        sql_params.append(limit)
        rows = conn.execute(sql, sql_params).fetchall()
        conn.close()
        if not rows:
            return f"Нет транзакций за {date_from}—{date_to}."
        lines = []
        for r in rows:
            sale_id, name, qty, price, total, shop_name, date, seller_name = r
            date_short = str(date)[:16].replace("T", " ")
            lines.append(
                f"  #{sale_id} {date_short} | {name} × {qty} шт. "
                f"× {int(price):,} ₽ = {int(total):,} ₽ | {seller_name} | {shop_name}"
            )
        return f"Транзакции {date_from}—{date_to} ({len(rows)} шт.):\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_recent_transactions: %s", exc)
        return "Ошибка получения транзакций."


def _tool_get_salary_summary(db, params: dict) -> str:
    """Team salary summary for a given month."""
    today = _dt.date.today()
    year  = int(params.get("year",  today.year))
    month = int(params.get("month", today.month))
    try:
        rows = db.get_team_salary_summary(year, month)
        if not rows:
            return f"Нет данных о зарплатах за {month:02d}.{year}."
        lines       = []
        total_total = 0.0
        for r in rows:
            # tuple: user_id[0] fn[1] ln[2] daily_rate[3] worked_days[4]
            #        base_salary[5] shop_name[6] tg_id[7] adj_sum[8]
            name  = f"{r[1] or ''} {r[2] or ''}".strip() or "—"
            base  = float(r[5] or 0)
            adj   = float(r[8] or 0)
            total = base + adj
            total_total += total
            shop  = r[6] or ""
            shop_part = f" ({shop})" if shop else ""
            adj_part  = (
                f" + {int(adj):,} ₽ корр." if adj > 0
                else (f" — {int(-adj):,} ₽ штраф" if adj < 0 else "")
            )
            lines.append(f"  - {name}{shop_part}: {int(base):,} ₽{adj_part} = {int(total):,} ₽")
        return (
            f"Зарплата команды за {month:02d}.{year} (итого {int(total_total):,} ₽):\n"
            + "\n".join(lines[:20])
        )
    except Exception as exc:
        logger.warning("ai_tool get_salary_summary: %s", exc)
        return "Ошибка получения данных о зарплатах."


def _tool_get_absences(db, params: dict) -> str:
    """List absences (vacations, sick leaves, etc.) for a given month."""
    today = _dt.date.today()
    year  = int(params.get("year",  today.year))
    month = int(params.get("month", today.month))
    try:
        rows = db.get_all_absences_admin(year, month)
        if not rows:
            return f"Нет отсутствий за {month:02d}.{year}."
        _TYPES  = {"vacation": "Отпуск", "sick": "Больничный",
                   "absence": "Отгул", "other": "Прочее"}
        _STATUS = {"approved": "одобрено", "pending": "ожидает",
                   "rejected": "отклонено"}
        lines = []
        for r in rows[:20]:
            # tuple: id[0] user_id[1] type[2] start[3] end[4] status[5] is_paid[6]
            #        comment[7] admin_comment[8] created_at[9] fn[10] ln[11] shop[12]
            name   = f"{r[10] or ''} {r[11] or ''}".strip() or "—"
            atype  = _TYPES.get(r[2], r[2])
            status = _STATUS.get(r[5], r[5])
            paid   = " (оплач.)" if r[6] else ""
            lines.append(f"  - {name}: {atype}{paid} {r[3]}—{r[4]} [{status}]")
        total  = len(rows)
        suffix = f" (первые 20 из {total})" if total > 20 else ""
        return f"Отсутствия {month:02d}.{year}{suffix}:\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_absences: %s", exc)
        return "Ошибка получения данных об отсутствиях."


def _tool_get_tasks(db, params: dict) -> str:
    """List org tasks, optionally filtered by status.

    get_tasks() returns list of dicts (see database.py).
    Status values: new / in_progress / review / done
    Priority values: urgent / high / normal / low
    """
    status = str(params.get("status", "")).strip() or None
    limit  = min(int(params.get("limit", 15)), 30)
    try:
        rows = db.get_tasks(status=status, is_admin=True)
        if not rows:
            return "Задачи не найдены."
        _PRIORITY = {
            "urgent": "🔴 срочный", "high": "🟠 высокий",
            "normal": "🟡 обычный", "low": "🟢 низкий",
        }
        _STATUS = {
            "new": "новая", "in_progress": "в работе",
            "review": "на проверке", "done": "выполнена",
        }
        lines = []
        for r in rows[:limit]:
            title    = r.get("title", "—")
            prio     = _PRIORITY.get(r.get("priority", ""), r.get("priority", "—"))
            st       = _STATUS.get(r.get("status", ""), r.get("status", "—"))
            deadline = r.get("deadline", "")
            dl_part  = f" | дедлайн {deadline}" if deadline else ""
            assignee = r.get("assigned_name", "").strip()
            a_part   = f" | {assignee}" if assignee else ""
            lines.append(f"  - [{st}] {title}{dl_part} | {prio}{a_part}")
        total  = len(rows)
        suffix = f" (первые {limit} из {total})" if total > limit else f" ({total})"
        return f"Задачи{suffix}:\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_tasks: %s", exc)
        return "Ошибка получения задач."


def _tool_get_work_schedule(db, params: dict) -> str:
    """Show work schedule (who is working which days) for the team in a given month."""
    today = _dt.date.today()
    year  = int(params.get("year",  today.year))
    month = int(params.get("month", today.month))
    try:
        conn = db.get_connection()
        month_start = f"{year}-{month:02d}-01"
        month_end   = f"{year}-{month:02d}-31"
        rows = conn.execute(
            """SELECT u.first_name, u.last_name, u.shop_name,
                      COUNT(ws.id) AS worked_days,
                      GROUP_CONCAT(ws.work_date ORDER BY ws.work_date) AS dates
               FROM users u
               LEFT JOIN work_schedule ws
                  ON ws.user_id = u.id
                  AND ws.work_date >= ? AND ws.work_date <= ?
               WHERE u.is_active = 1
               GROUP BY u.id
               ORDER BY u.last_name, u.first_name""",
            (month_start, month_end),
        ).fetchall()
        conn.close()
        if not rows:
            return f"Нет данных о расписании за {month:02d}.{year}."
        lines = []
        for r in rows:
            fn, ln, shop, worked_days, dates_str = r
            name      = f"{fn or ''} {ln or ''}".strip() or "—"
            shop_part = f" ({shop})" if shop else ""
            if worked_days:
                dates_list = (dates_str or "").split(",")
                days_fmt   = ", ".join(d[8:] for d in dates_list if len(d) >= 10)
                lines.append(f"  - {name}{shop_part}: {worked_days} смен — числа: {days_fmt}")
            else:
                lines.append(f"  - {name}{shop_part}: 0 смен (нет записей)")
        return f"График работы за {month:02d}.{year}:\n" + "\n".join(lines[:25])
    except Exception as exc:
        logger.warning("ai_tool get_work_schedule: %s", exc)
        return "Ошибка получения графика работы."


def _tool_get_expenses(db, params: dict) -> str:
    """Return all expense-related data the system tracks for a given month.

    DailySales does not have a dedicated expenses module, so this tool
    aggregates the closest equivalents:
      - Total salary costs (base + bonuses/penalties) — the main tracked expense
      - Breakdown of salary adjustments (bonuses, penalties, deductions)
    Operational expenses (rent, utilities, procurement costs) are not tracked
    in this system.
    """
    today = _dt.date.today()
    year  = int(params.get("year",  today.year))
    month = int(params.get("month", today.month))
    try:
        # 1. Salary totals
        salary_rows = db.get_team_salary_summary(year, month)
        total_base  = sum(float(r[5] or 0) for r in salary_rows)
        total_adj   = sum(float(r[8] or 0) for r in salary_rows)
        total_wages = total_base + total_adj

        # 2. Individual adjustments breakdown
        conn = db.get_connection()
        adj_rows = conn.execute(
            """SELECT u.first_name, u.last_name, sa.amount, sa.comment
               FROM salary_adjustments sa
               JOIN users u ON u.id = sa.user_id
               WHERE sa.year = ? AND sa.month = ?
               ORDER BY sa.amount ASC
               LIMIT 30""",
            (year, month),
        ).fetchall()
        conn.close()

        lines = [f"Расходы организации за {month:02d}.{year}:\n"]
        lines.append(f"  Зарплатный фонд: {int(total_wages):,} ₽")
        lines.append(f"    из них оклады: {int(total_base):,} ₽")
        if total_adj:
            adj_sign = "+" if total_adj > 0 else ""
            lines.append(f"    корректировки: {adj_sign}{int(total_adj):,} ₽")

        if adj_rows:
            lines.append("\nКорректировки:")
            for fn, ln, amount, comment in adj_rows:
                name     = f"{fn or ''} {ln or ''}".strip() or "—"
                sign     = "+" if float(amount) >= 0 else ""
                com_part = f" — {comment}" if comment else ""
                lines.append(f"  - {name}: {sign}{int(float(amount)):,} ₽{com_part}")

        lines.append(
            "\n(Учёт операционных расходов — аренда, закупки, коммунальные — "
            "в системе не ведётся.)"
        )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_expenses: %s", exc)
        return "Ошибка получения данных о расходах."


def _tool_get_salary_adjustments(db, params: dict) -> str:
    """List all salary bonuses / penalties across the team for a given month."""
    today = _dt.date.today()
    year  = int(params.get("year",  today.year))
    month = int(params.get("month", today.month))
    try:
        conn = db.get_connection()
        rows = conn.execute(
            """SELECT u.first_name, u.last_name, u.shop_name,
                      sa.amount, sa.comment, sa.created_at
               FROM salary_adjustments sa
               JOIN users u ON u.id = sa.user_id
               WHERE sa.year = ? AND sa.month = ?
               ORDER BY sa.created_at DESC
               LIMIT 50""",
            (year, month),
        ).fetchall()
        conn.close()
        if not rows:
            return f"Нет корректировок зарплат за {month:02d}.{year}."
        lines = []
        total_pos, total_neg = 0.0, 0.0
        for fn, ln, shop, amount, comment, created_at in rows:
            name      = f"{fn or ''} {ln or ''}".strip() or "—"
            shop_part = f" ({shop})" if shop else ""
            sign      = "+" if float(amount) >= 0 else ""
            date_short = str(created_at)[:10] if created_at else ""
            comment_s  = f" — {comment}" if comment else ""
            lines.append(f"  - {name}{shop_part}: {sign}{int(float(amount)):,} ₽{comment_s} [{date_short}]")
            if float(amount) >= 0:
                total_pos += float(amount)
            else:
                total_neg += float(amount)
        summary = f"Итого: +{int(total_pos):,} ₽ бонусов, {int(total_neg):,} ₽ штрафов"
        return (
            f"Корректировки зарплат {month:02d}.{year} ({len(rows)} записей):\n"
            + "\n".join(lines) + f"\n{summary}"
        )
    except Exception as exc:
        logger.warning("ai_tool get_salary_adjustments: %s", exc)
        return "Ошибка получения данных о корректировках."


def _tool_get_motivation_rates(db, params: dict) -> str:
    """Get product-level commission/motivation rates (bonus per sale)."""
    try:
        rows = db.get_all_product_motivations()
        if not rows:
            return "Мотивационные ставки не настроены."
        _TYPE = {"percent": "%", "fixed": "₽/шт.", "flat": "₽"}
        lines = []
        for r in rows:
            # id[0] name[1] motivation_type[2] motivation_value[3]
            # creator_fn[4] creator_ln[5] created_at[6]
            name  = r[1] or "—"
            mtype = r[2]
            mval  = r[3]
            if mtype and mval is not None:
                unit = _TYPE.get(mtype, mtype)
                lines.append(f"  - {name}: {float(mval):g} {unit}")
            else:
                lines.append(f"  - {name}: не задана")
        total_set = sum(1 for r in rows if r[2] and r[3] is not None)
        return (
            f"Ставки мотивации ({total_set} из {len(rows)} товаров):\n"
            + "\n".join(lines[:30])
        )
    except Exception as exc:
        logger.warning("ai_tool get_motivation_rates: %s", exc)
        return "Ошибка получения ставок мотивации."


def _tool_get_transaction_by_id(db, params: dict) -> str:
    """Look up a single sales transaction / receipt by its ID."""
    sale_id = params.get("id") or params.get("sale_id")
    if not sale_id:
        return "Укажите параметр id (номер чека/транзакции)."
    try:
        sale_id = int(sale_id)
        conn = db.get_connection()
        row = conn.execute(
            """SELECT s.id, p.name, s.quantity_sold, s.sale_price,
                      s.quantity_sold * s.sale_price AS total,
                      s.shop_name, s.sale_date,
                      u.first_name || ' ' || u.last_name AS seller_name,
                      s.comment
               FROM sales s
               JOIN products p ON s.product_id = p.id
               JOIN users u ON s.user_id = u.id
               WHERE s.id = ?""",
            (sale_id,),
        ).fetchone()
        conn.close()
        if not row:
            return f"Транзакция #{sale_id} не найдена."
        sid, name, qty, price, total, shop, date, seller, comment = row
        date_s   = str(date)[:16].replace("T", " ")
        result   = (
            f"Чек #{sid}\n"
            f"  Дата: {date_s}\n"
            f"  Товар: {name}\n"
            f"  Количество: {qty} шт. × {int(price):,} ₽ = {int(total):,} ₽\n"
            f"  Магазин: {shop}\n"
            f"  Продавец: {seller}"
        )
        if comment:
            result += f"\n  Комментарий: {comment}"
        return result
    except ValueError:
        return "Параметр id должен быть числом."
    except Exception as exc:
        logger.warning("ai_tool get_transaction_by_id: %s", exc)
        return "Ошибка получения данных о транзакции."


def _tool_get_rankings(db, params: dict) -> str:
    """Seller, shop, or city rankings for a period."""
    rtype     = str(params.get("type", "sellers")).strip()
    today     = _dt.date.today()
    date_from = str(params.get("date_from", today.replace(day=1).isoformat()))
    date_to   = str(params.get("date_to",   today.isoformat()))
    limit     = min(int(params.get("limit", 10)), 20)
    try:
        if rtype == "shops":
            rows = db.get_shop_ranking(start_date=date_from, end_date=date_to)
            if not rows:
                return "Нет данных для рейтинга магазинов."
            lines = [
                f"  {i+1}. {r[0]}: {int(r[2] or 0):,} ₽ ({int(r[1] or 0)} шт.)"
                for i, r in enumerate(rows[:limit])
            ]
            return f"Рейтинг магазинов {date_from}—{date_to}:\n" + "\n".join(lines)
        elif rtype == "cities":
            rows = db.get_city_ranking(start_date=date_from, end_date=date_to)
            if not rows:
                return "Нет данных для рейтинга городов."
            lines = [
                f"  {i+1}. {r[0]}: {int(r[2] or 0):,} ₽"
                for i, r in enumerate(rows[:limit])
            ]
            return f"Рейтинг городов {date_from}—{date_to}:\n" + "\n".join(lines)
        else:
            rows = db.get_sales_ranking(start_date=date_from, end_date=date_to)
            if not rows:
                return "Нет данных для рейтинга продавцов."
            lines = []
            for i, r in enumerate(rows[:limit]):
                # fn[0] ln[1] shop[2] qty[3] rev[4] cnt[5] earnings[6] user_db_id[7] username[8]
                name  = f"{r[0] or ''} {r[1] or ''}".strip() or r[8] or "—"
                shop  = f" ({r[2]})" if r[2] else ""
                lines.append(
                    f"  {i+1}. {name}{shop}: {int(r[4] or 0):,} ₽ ({int(r[3] or 0)} шт.)"
                )
            return f"Рейтинг продавцов {date_from}—{date_to}:\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_rankings: %s", exc)
        return "Ошибка получения рейтинга."


# ── New tools: daily breakdown, period comparison, categories, seller, plans ──

def _tool_get_daily_sales(db, params: dict) -> str:
    """Day-by-day sales breakdown for trend analysis."""
    today     = _dt.date.today()
    date_from = str(params.get("date_from", (today - _dt.timedelta(days=6)).isoformat()))
    date_to   = str(params.get("date_to",   today.isoformat()))
    shop      = str(params.get("shop", "")).strip() or None
    try:
        conn = db.get_connection()
        sql = (
            "SELECT date(s.sale_date) AS day, COUNT(*) AS cnt, "
            "SUM(s.quantity_sold) AS qty, SUM(s.quantity_sold * s.sale_price) AS rev "
            "FROM sales s WHERE date(s.sale_date) >= ? AND date(s.sale_date) <= ?"
        )
        sql_params: list = [date_from, date_to]
        if shop:
            sql += " AND s.shop_name = ?"
            sql_params.append(shop)
        sql += " GROUP BY date(s.sale_date) ORDER BY day"
        rows = conn.execute(sql, sql_params).fetchall()
        conn.close()
        if not rows:
            return f"Нет продаж за период {date_from} — {date_to}."
        _RU_DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        lines = []
        total_rev = 0.0
        for day, cnt, qty, rev in rows:
            rev = float(rev or 0)
            total_rev += rev
            try:
                wd = _dt.date.fromisoformat(str(day)).weekday()
                day_label = f"{_RU_DAYS[wd]} {day}"
            except Exception:
                day_label = str(day)
            lines.append(f"  {day_label}: {int(rev):,} ₽ ({int(qty or 0)} шт., {int(cnt)} чеков)")
        shop_part = f" | {shop}" if shop else ""
        return (
            f"Динамика по дням {date_from}—{date_to}{shop_part} "
            f"(итого {int(total_rev):,} ₽):\n" + "\n".join(lines)
        )
    except Exception as exc:
        logger.warning("ai_tool get_daily_sales: %s", exc)
        return "Ошибка получения данных по дням."


def _tool_get_sales_comparison(db, params: dict) -> str:
    """Compare key metrics between two time periods."""
    today = _dt.date.today()
    week_start = today - _dt.timedelta(days=today.weekday())
    p1_from = str(params.get("period1_from", week_start.isoformat()))
    p1_to   = str(params.get("period1_to",   today.isoformat()))
    try:
        d1_from = _dt.date.fromisoformat(p1_from)
        d1_to   = _dt.date.fromisoformat(p1_to)
        span    = max((d1_to - d1_from).days, 0)
        p2_to_d = d1_from - _dt.timedelta(days=1)
        p2_from_d = p2_to_d - _dt.timedelta(days=span)
        p2_from = str(params.get("period2_from", p2_from_d.isoformat()))
        p2_to   = str(params.get("period2_to",   p2_to_d.isoformat()))
    except Exception:
        p2_from = (today - _dt.timedelta(days=14)).isoformat()
        p2_to   = (today - _dt.timedelta(days=8)).isoformat()
    shop = str(params.get("shop", "")).strip() or None

    def _fetch(df, dt):
        try:
            s = db.get_sales_summary(start_date=df, end_date=dt, shop_name=shop)
            if not s or not s[0]:
                return (0, 0, 0.0)
            return (int(s[0] or 0), int(s[1] or 0), float(s[2] or 0))
        except Exception:
            return (0, 0, 0.0)

    cnt1, qty1, rev1 = _fetch(p1_from, p1_to)
    cnt2, qty2, rev2 = _fetch(p2_from, p2_to)

    def _pct(a, b):
        if b == 0:
            return "н/д"
        d = (a - b) / b * 100
        return f"+{d:.1f}%" if d >= 0 else f"{d:.1f}%"

    shop_part = f" | {shop}" if shop else ""
    return (
        f"Сравнение периодов{shop_part}:\n"
        f"  Период 1 ({p1_from}—{p1_to}): {int(rev1):,} ₽, {qty1} шт., {cnt1} чеков\n"
        f"  Период 2 ({p2_from}—{p2_to}): {int(rev2):,} ₽, {qty2} шт., {cnt2} чеков\n"
        f"  Изменение → выручка: {_pct(rev1, rev2)} | единиц: {_pct(qty1, qty2)} | чеков: {_pct(cnt1, cnt2)}"
    )


def _tool_get_category_breakdown(db, params: dict) -> str:
    """Sales breakdown by product category for a period."""
    today     = _dt.date.today()
    date_from = str(params.get("date_from", today.replace(day=1).isoformat()))
    date_to   = str(params.get("date_to",   today.isoformat()))
    shop      = str(params.get("shop", "")).strip() or None
    try:
        conn = db.get_connection()
        sql = (
            "SELECT p.category, COUNT(*) AS cnt, SUM(s.quantity_sold) AS qty, "
            "SUM(s.quantity_sold * s.sale_price) AS rev "
            "FROM sales s JOIN products p ON s.product_id = p.id "
            "WHERE date(s.sale_date) >= ? AND date(s.sale_date) <= ?"
        )
        sql_params: list = [date_from, date_to]
        if shop:
            sql += " AND s.shop_name = ?"
            sql_params.append(shop)
        sql += " GROUP BY p.category ORDER BY rev DESC LIMIT 20"
        rows = conn.execute(sql, sql_params).fetchall()
        conn.close()
        if not rows:
            return f"Нет данных по категориям за {date_from}—{date_to}."
        total_rev = sum(float(r[3] or 0) for r in rows)
        lines = []
        for cat, cnt, qty, rev in rows:
            rev = float(rev or 0)
            share = rev / total_rev * 100 if total_rev > 0 else 0
            lines.append(f"  - {cat or 'Без категории'}: {int(rev):,} ₽ ({share:.1f}%) | {int(qty or 0)} шт.")
        shop_part = f" | {shop}" if shop else ""
        return (
            f"Продажи по категориям {date_from}—{date_to}{shop_part} "
            f"(итого {int(total_rev):,} ₽):\n" + "\n".join(lines)
        )
    except Exception as exc:
        logger.warning("ai_tool get_category_breakdown: %s", exc)
        return "Ошибка получения данных по категориям."


def _tool_get_seller_stats(db, params: dict) -> str:
    """Deep-dive stats for a specific seller: sales, top products."""
    seller = str(params.get("seller", "")).strip()
    if not seller:
        return "Укажите параметр seller (имя или фамилия продавца)."
    today     = _dt.date.today()
    date_from = str(params.get("date_from", today.replace(day=1).isoformat()))
    date_to   = str(params.get("date_to",   today.isoformat()))
    try:
        conn = db.get_connection()
        rows = conn.execute(
            """SELECT u.id, u.first_name, u.last_name, u.shop_name
               FROM users u
               WHERE (u.first_name LIKE ? OR u.last_name LIKE ?
                      OR (u.first_name || ' ' || u.last_name) LIKE ?)
               LIMIT 5""",
            (f"%{seller}%", f"%{seller}%", f"%{seller}%"),
        ).fetchall()
        if not rows:
            conn.close()
            return f"Продавец «{seller}» не найден."
        uid, fn, ln, shop = rows[0]
        full_name = f"{fn or ''} {ln or ''}".strip() or seller

        stats = conn.execute(
            """SELECT COUNT(*) AS cnt, SUM(s.quantity_sold) AS qty,
                      SUM(s.quantity_sold * s.sale_price) AS rev
               FROM sales s
               WHERE s.user_id = ? AND date(s.sale_date) >= ? AND date(s.sale_date) <= ?""",
            (uid, date_from, date_to),
        ).fetchone()

        top = conn.execute(
            """SELECT p.name, SUM(s.quantity_sold) AS qty,
                      SUM(s.quantity_sold * s.sale_price) AS rev
               FROM sales s JOIN products p ON s.product_id = p.id
               WHERE s.user_id = ? AND date(s.sale_date) >= ? AND date(s.sale_date) <= ?
               GROUP BY p.id ORDER BY rev DESC LIMIT 4""",
            (uid, date_from, date_to),
        ).fetchall()
        conn.close()

        cnt = int(stats[0] or 0) if stats else 0
        qty = int(stats[1] or 0) if stats else 0
        rev = float(stats[2] or 0) if stats else 0.0
        avg = rev / cnt if cnt > 0 else 0

        shop_part = f" ({shop})" if shop else ""
        result = (
            f"Продавец: {full_name}{shop_part} | {date_from}—{date_to}\n"
            f"  Чеков: {cnt} | Единиц: {qty} | Выручка: {int(rev):,} ₽ | Средний чек: {int(avg):,} ₽"
        )
        if top:
            top_lines = [
                f"  {i+1}. {r[0]}: {int(r[1])} шт., {int(float(r[2] or 0)):,} ₽"
                for i, r in enumerate(top)
            ]
            result += "\nТоп товары:\n" + "\n".join(top_lines)
        return result
    except Exception as exc:
        logger.warning("ai_tool get_seller_stats: %s", exc)
        return "Ошибка получения данных о продавце."


def _tool_get_plans_detail(db, params: dict) -> str:
    """Active plans with per-shop progress bars."""
    try:
        today = _dt.date.today()
        conn  = db.get_connection()
        plans = conn.execute(
            """SELECT p.id, p.name, p.target_value, p.metric,
                      p.start_date, p.end_date, p.shop_name
               FROM plans p
               WHERE date(p.start_date) <= ? AND date(p.end_date) >= ?
               ORDER BY p.shop_name NULLS FIRST, p.name""",
            (today.isoformat(), today.isoformat()),
        ).fetchall()

        if not plans:
            conn.close()
            rows = db.get_plans_with_progress()
            if not rows:
                return "Активных планов нет."
            lines = []
            for p in rows:
                unit = "руб." if "выручка" in p.get("label", "") else "шт."
                pct  = p.get("pct", 0)
                bar  = "█" * min(int(pct // 10), 10) + "░" * max(0, 10 - int(pct // 10))
                lines.append(
                    f"  - {p['label']}: {int(p['current']):,} / {int(p['target']):,} {unit} — {pct}% [{bar}]"
                )
            return "Планы (текущие):\n" + "\n".join(lines)

        lines = []
        for plan_id, name, target, metric, start, end, shop_name in plans:
            is_rev = metric and "выручка" in str(metric).lower()
            sql = (
                "SELECT SUM(s.quantity_sold * s.sale_price) AS rev, SUM(s.quantity_sold) AS qty "
                "FROM sales s WHERE date(s.sale_date) >= ? AND date(s.sale_date) <= ?"
            )
            sql_params: list = [str(start)[:10], str(end)[:10]]
            if shop_name:
                sql += " AND s.shop_name = ?"
                sql_params.append(shop_name)
            row = conn.execute(sql, sql_params).fetchone()
            current = float((row[0] if is_rev else row[1]) or 0) if row else 0.0
            target_v = float(target or 0)
            pct = int(current / target_v * 100) if target_v > 0 else 0
            unit = "руб." if is_rev else "шт."
            scope = f"[{shop_name}]" if shop_name else "[вся орг.]"
            bar = "█" * min(pct // 10, 10) + "░" * max(0, 10 - pct // 10)
            lines.append(
                f"  - {name} {scope}: {int(current):,}/{int(target_v):,} {unit} — {pct}% [{bar}]"
            )
        conn.close()
        return "Планы продаж (разбивка по магазинам):\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("ai_tool get_plans_detail: %s", exc)
        return "Ошибка получения данных о планах."


# ── Tool registry ─────────────────────────────────────────────────────────────

TOOLS: dict[str, dict] = {
    "get_products": {
        "description": (
            "Поиск/список товаров каталога. "
            "Параметры: query (поиск по названию/артикулу/штрихкоду), "
            "category (фильтр по категории), limit (до 50)."
        ),
        "fn": _tool_get_products,
    },
    "get_product_by_code": {
        "description": (
            "Найти конкретный товар по штрихкоду или артикулу. "
            "Параметры: code (штрихкод или артикул товара)."
        ),
        "fn": _tool_get_product_by_code,
    },
    "get_inventory": {
        "description": (
            "Остатки товаров на складах/в магазинах. "
            "Параметры: shop (название магазина, необязательно — без него все магазины)."
        ),
        "fn": _tool_get_inventory,
    },
    "get_low_stock": {
        "description": (
            "Товары с критически низким остатком. "
            "Параметры: threshold (порог в штуках, по умолчанию 5)."
        ),
        "fn": _tool_get_low_stock,
    },
    "get_sales_period": {
        "description": (
            "Сводка продаж за произвольный период (итоги + топ товаров). "
            "Параметры: date_from (ГГГГ-ММ-ДД), date_to (ГГГГ-ММ-ДД), "
            "shop (название магазина, необязательно)."
        ),
        "fn": _tool_get_sales_period,
    },
    "get_recent_transactions": {
        "description": (
            "Отдельные чеки/транзакции продаж за период (список записей). "
            "Параметры: date_from (ГГГГ-ММ-ДД), date_to (ГГГГ-ММ-ДД), "
            "shop (магазин, необязательно), seller (имя продавца, необязательно), "
            "limit (до 50, по умолчанию 20)."
        ),
        "fn": _tool_get_recent_transactions,
    },
    "get_salary_summary": {
        "description": (
            "Сводка зарплат сотрудников за месяц (оклад + корректировки/штрафы/бонусы). "
            "Параметры: year (год, напр. 2026), month (месяц 1–12)."
        ),
        "fn": _tool_get_salary_summary,
    },
    "get_absences": {
        "description": (
            "Отсутствия сотрудников (отпуска, больничные, отгулы) за месяц. "
            "Параметры: year (год), month (месяц 1–12)."
        ),
        "fn": _tool_get_absences,
    },
    "get_tasks": {
        "description": (
            "Список задач организации. "
            "Параметры: status (new/in_progress/review/done, необязательно), "
            "limit (до 30, по умолчанию 15)."
        ),
        "fn": _tool_get_tasks,
    },
    "get_work_schedule": {
        "description": (
            "График рабочих смен команды за месяц (кто сколько дней работал и в какие числа). "
            "Параметры: year (год), month (месяц 1–12)."
        ),
        "fn": _tool_get_work_schedule,
    },
    "get_expenses": {
        "description": (
            "Расходы организации за месяц: зарплатный фонд (оклады + корректировки/бонусы/штрафы). "
            "Отдельного учёта операционных расходов в системе нет — только зарплатные. "
            "Параметры: year (год), month (месяц 1–12)."
        ),
        "fn": _tool_get_expenses,
    },
    "get_salary_adjustments": {
        "description": (
            "Корректировки зарплат команды за месяц (бонусы, штрафы, премии, удержания). "
            "Параметры: year (год), month (месяц 1–12)."
        ),
        "fn": _tool_get_salary_adjustments,
    },
    "get_motivation_rates": {
        "description": (
            "Ставки мотивации/комиссий по товарам (сколько продавец получает с продажи каждого товара). "
            "Параметры: нет."
        ),
        "fn": _tool_get_motivation_rates,
    },
    "get_transaction_by_id": {
        "description": (
            "Найти конкретный чек/транзакцию продажи по её номеру (ID). "
            "Параметры: id (номер чека/транзакции)."
        ),
        "fn": _tool_get_transaction_by_id,
    },
    "get_rankings": {
        "description": (
            "Рейтинг продавцов, магазинов или городов за период. "
            "Параметры: type (sellers/shops/cities), "
            "date_from (ГГГГ-ММ-ДД), date_to (ГГГГ-ММ-ДД), limit."
        ),
        "fn": _tool_get_rankings,
    },
    "get_daily_sales": {
        "description": (
            "Динамика продаж по дням — сколько выручки, единиц и чеков было КАЖДЫЙ ДЕНЬ периода. "
            "Используй для анализа трендов: рост/падение, какой день лучший/худший. "
            "Параметры: date_from (ГГГГ-ММ-ДД), date_to (ГГГГ-ММ-ДД, макс. 31 день), "
            "shop (магазин, необязательно)."
        ),
        "fn": _tool_get_daily_sales,
    },
    "get_sales_comparison": {
        "description": (
            "Сравнение двух периодов: выручка, количество единиц и чеков — и процент изменения. "
            "Используй когда нужно сравнить эту неделю с прошлой, этот месяц с прошлым, и т.д. "
            "Параметры: period1_from, period1_to, period2_from, period2_to (ГГГГ-ММ-ДД), "
            "shop (необязательно). "
            "По умолчанию: period1 = текущая неделя, period2 = предыдущий аналогичный период."
        ),
        "fn": _tool_get_sales_comparison,
    },
    "get_category_breakdown": {
        "description": (
            "Разбивка продаж по категориям товаров за период: доля каждой категории в выручке. "
            "Используй для анализа структуры продаж, выявления самых прибыльных категорий. "
            "Параметры: date_from (ГГГГ-ММ-ДД), date_to (ГГГГ-ММ-ДД), shop (необязательно)."
        ),
        "fn": _tool_get_category_breakdown,
    },
    "get_seller_stats": {
        "description": (
            "Детальная статистика конкретного продавца: выручка, количество чеков, "
            "топ-товары за период. Используй когда спрашивают о конкретном сотруднике. "
            "Параметры: seller (имя или фамилия), date_from (ГГГГ-ММ-ДД), date_to (ГГГГ-ММ-ДД)."
        ),
        "fn": _tool_get_seller_stats,
    },
    "get_plans_detail": {
        "description": (
            "Планы продаж с разбивкой по магазинам и прогресс-барами. "
            "Более детально, чем базовые планы в системном промпте — показывает каждый магазин. "
            "Параметры: нет."
        ),
        "fn": _tool_get_plans_detail,
    },
}


_TOOLS_DESC_CACHE: str | None = None


def get_tools_description() -> str:
    """Format tool list for inclusion in the system prompt.

    Result is cached at module level — TOOLS dict never changes at runtime.
    """
    global _TOOLS_DESC_CACHE
    if _TOOLS_DESC_CACHE is None:
        lines = ["Доступные инструменты для получения данных организации:"]
        for name, meta in TOOLS.items():
            lines.append(f"  [{name}] — {meta['description']}")
        _TOOLS_DESC_CACHE = "\n".join(lines)
    return _TOOLS_DESC_CACHE


def call_tool(name: str, params: dict, db) -> str:
    """Dispatch a tool call. Returns a formatted string result.

    Side-effect: increments the in-memory usage counter for this tool,
    keyed by (tool_name, org_db, today_utc). Counters are accessible via
    get_tool_stats() and exposed at GET /admin/ai-tool-stats.
    """
    tool = TOOLS.get(name)
    if not tool:
        available = ", ".join(TOOLS.keys())
        return f"Инструмент «{name}» не найден. Доступные: {available}."

    org_db: str = ""
    try:
        org_db = str(getattr(db, "db_file", "") or "")
    except Exception:
        pass
    _record_call(name, org_db)

    try:
        return tool["fn"](db, params)
    except Exception as exc:
        logger.error("call_tool(%s) crashed: %s", name, exc)
        return f"Ошибка при вызове инструмента {name}."
