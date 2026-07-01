"""AI Network Insights — /ai-insights page and POST /api/ai/network-insights."""
import logging
import sqlite3
import datetime as _dt
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

_SHOP_BOT_DB = "data/shop_bot.db"
_MAIN_DB = "data/main.db"


def _api_csrf_ok(request: Request) -> bool:
    """Same-origin guard: X-Requested-With или совпадение Origin с host."""
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return True
    origin = request.headers.get("origin")
    if not origin:
        return False
    host = request.headers.get("host", "")
    try:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == host
    except Exception:
        return False


def _get_limits(tg_id: int) -> tuple[int, bool]:
    """Возвращает (дневной_лимит, is_high_limit) для пользователя.

    Приоритет: кастомный лимит super-admin > high_limit extension > base_daily_limit.
    """
    from billing_utils import has_extension
    from web.rate_store import get_ai_rate_limits, get_custom_ai_limit
    custom = get_custom_ai_limit(tg_id)
    if custom is not None:
        return custom, False
    base, high = get_ai_rate_limits()
    if has_extension(tg_id, "ai_high_limit"):
        return high, True
    return base, False


def _get_owner_orgs(tg_id: int) -> list[dict]:
    """Return list of orgs where tg_id is owner, with existing DB files."""
    import os
    result = []
    try:
        conn = sqlite3.connect(_MAIN_DB)
        try:
            rows = conn.execute(
                """SELECT o.id, o.name, o.db_path
                   FROM organizations o
                   JOIN user_org_mapping m ON m.org_id = o.id
                   WHERE m.telegram_id = ? AND m.role = 'owner' AND o.is_active = 1
                   ORDER BY o.name""",
                (tg_id,)
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            if row[2] and os.path.exists(row[2]) and row[2] != "data/shop_bot.db":
                result.append({
                    "id": row[0],
                    "name": row[1] or f"org#{row[0]}",
                    "org_db": row[2],
                })
    except Exception as exc:
        logger.error("_get_owner_orgs error: %s", exc)
    return result


def _get_shops_for_org(org_db: str) -> list[str]:
    """Return list of shop names from the org DB (non-empty, non-system names)."""
    try:
        from database import Database
        db = Database(org_db)
        shops = db.get_all_shops(include_system=False)
        names = []
        for row in (shops or []):
            name = row[0] if isinstance(row, (list, tuple)) else row
            if name and str(name).strip():
                names.append(str(name).strip())
        return names
    except Exception as exc:
        logger.error("_get_shops_for_org error for %s: %s", org_db, exc)
        return []


def _get_org_summary(org_db: str) -> dict:
    """Aggregate current + prev month revenue, top category, plan %, seller count for one org."""
    try:
        from database import Database
        db = Database(org_db)
        today = _dt.date.today()
        month_start = today.replace(day=1).isoformat()
        today_str = today.isoformat()

        prev_end = (today.replace(day=1) - _dt.timedelta(days=1))
        prev_start = prev_end.replace(day=1).isoformat()
        prev_end_str = prev_end.isoformat()

        cur_summary = db.get_sales_summary(start_date=month_start, end_date=today_str) or (0, 0, 0, 0)
        prev_summary = db.get_sales_summary(start_date=prev_start, end_date=prev_end_str) or (0, 0, 0, 0)

        revenue_month = float(cur_summary[2] or 0)
        revenue_prev = float(prev_summary[2] or 0)

        top_category = "—"
        seller_count = 0
        plan_pct = 0

        try:
            conn = db.get_connection()
            try:
                cat_row = conn.execute(
                    """SELECT p.category, SUM(s.quantity_sold * s.sale_price) as rev
                       FROM sales s JOIN products p ON p.id = s.product_id
                       WHERE s.sale_date >= ? AND s.sale_date <= ?
                       GROUP BY p.category ORDER BY rev DESC LIMIT 1""",
                    (month_start, today_str)
                ).fetchone()
                if cat_row and cat_row[0]:
                    top_category = str(cat_row[0])

                sc_row = conn.execute(
                    "SELECT COUNT(DISTINCT telegram_id) FROM users WHERE is_active = 1"
                ).fetchone()
                seller_count = int(sc_row[0] or 0) if sc_row else 0

                if revenue_month > 0:
                    plan_row = conn.execute(
                        """SELECT SUM(target_amount) FROM sales_plans
                           WHERE target_type = 'global' AND is_active = 1"""
                    ).fetchone()
                    if plan_row and plan_row[0] and float(plan_row[0]) > 0:
                        plan_pct = round(revenue_month / float(plan_row[0]) * 100, 1)
            finally:
                conn.close()
        except Exception:
            pass

        return {
            "revenue_month": revenue_month,
            "revenue_prev": revenue_prev,
            "top_category": top_category,
            "plan_pct": plan_pct,
            "seller_count": seller_count,
        }
    except Exception as exc:
        logger.error("_get_org_summary error for %s: %s", org_db, exc)
        return {
            "revenue_month": 0,
            "revenue_prev": 0,
            "top_category": "—",
            "plan_pct": 0,
            "seller_count": 0,
        }


def _get_shop_summary(org_db: str, shop_name: str) -> dict:
    """Aggregate revenue, top category, plan %, seller count for one shop within an org."""
    try:
        from database import Database
        db = Database(org_db)
        today = _dt.date.today()
        month_start = today.replace(day=1).isoformat()
        today_str = today.isoformat()

        prev_end = (today.replace(day=1) - _dt.timedelta(days=1))
        prev_start = prev_end.replace(day=1).isoformat()
        prev_end_str = prev_end.isoformat()

        top_category = "—"
        seller_count = 0
        plan_pct = 0
        revenue_month = 0.0
        revenue_prev = 0.0

        try:
            conn = db.get_connection()
            try:
                rev_row = conn.execute(
                    """SELECT SUM(s.quantity_sold * s.sale_price)
                       FROM sales s
                       WHERE s.shop_name = ? AND s.sale_date >= ? AND s.sale_date <= ?""",
                    (shop_name, month_start, today_str)
                ).fetchone()
                revenue_month = float(rev_row[0] or 0) if rev_row else 0.0

                rev_prev_row = conn.execute(
                    """SELECT SUM(s.quantity_sold * s.sale_price)
                       FROM sales s
                       WHERE s.shop_name = ? AND s.sale_date >= ? AND s.sale_date <= ?""",
                    (shop_name, prev_start, prev_end_str)
                ).fetchone()
                revenue_prev = float(rev_prev_row[0] or 0) if rev_prev_row else 0.0

                cat_row = conn.execute(
                    """SELECT p.category, SUM(s.quantity_sold * s.sale_price) as rev
                       FROM sales s JOIN products p ON p.id = s.product_id
                       WHERE s.shop_name = ? AND s.sale_date >= ? AND s.sale_date <= ?
                       GROUP BY p.category ORDER BY rev DESC LIMIT 1""",
                    (shop_name, month_start, today_str)
                ).fetchone()
                if cat_row and cat_row[0]:
                    top_category = str(cat_row[0])

                sc_row = conn.execute(
                    "SELECT COUNT(DISTINCT telegram_id) FROM users WHERE shop_name = ? AND is_active = 1",
                    (shop_name,)
                ).fetchone()
                seller_count = int(sc_row[0] or 0) if sc_row else 0

                if revenue_month > 0:
                    plan_row = conn.execute(
                        """SELECT SUM(target_amount) FROM sales_plans
                           WHERE target_type = 'shop' AND scope_value = ? AND is_active = 1""",
                        (shop_name,)
                    ).fetchone()
                    if not (plan_row and plan_row[0] and float(plan_row[0]) > 0):
                        plan_row = conn.execute(
                            """SELECT SUM(target_amount) FROM sales_plans
                               WHERE target_type = 'global' AND is_active = 1"""
                        ).fetchone()
                    if plan_row and plan_row[0] and float(plan_row[0]) > 0:
                        plan_pct = round(revenue_month / float(plan_row[0]) * 100, 1)
            finally:
                conn.close()
        except Exception:
            pass

        return {
            "revenue_month": revenue_month,
            "revenue_prev": revenue_prev,
            "top_category": top_category,
            "plan_pct": plan_pct,
            "seller_count": seller_count,
        }
    except Exception as exc:
        logger.error("_get_shop_summary error for %s/%s: %s", org_db, shop_name, exc)
        return {
            "revenue_month": 0,
            "revenue_prev": 0,
            "top_category": "—",
            "plan_pct": 0,
            "seller_count": 0,
        }


def _build_network_prompt(orgs: list) -> str:
    lines = []
    for o in orgs:
        delta = o["revenue_month"] - o["revenue_prev"]
        delta_pct = round(delta / o["revenue_prev"] * 100, 1) if o["revenue_prev"] else 0
        lines.append(
            f"• {o['name']}: {o['revenue_month']:,.0f} ₽ ({delta_pct:+.1f}% к пред. мес.) "
            f"| план {o['plan_pct']}% | топ-категория: {o['top_category']} "
            f"| продавцов: {o['seller_count']}"
        )

    return f"""Ты бизнес-аналитик розничной сети. Проанализируй магазины:

{chr(10).join(lines)}

Напиши структурированный отчёт:
1. Лидеры и аутсайдеры этого месяца (с объяснением)
2. Общие тренды и паттерны по сети
3. Конкретные возможности для роста (2-3 пункта)
4. Риски, требующие внимания

Отвечай по-русски, конкретно. Ссылайся на названия магазинов."""


def _get_cached_insights(tg_id: int) -> dict | None:
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT insights_text, generated_at FROM ai_insights_cache WHERE tg_id = ?",
                (tg_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return {"insights_text": row[0], "generated_at": row[1]}
    except Exception as exc:
        logger.error("_get_cached_insights error: %s", exc)
    return None


def _save_cached_insights(tg_id: int, text: str) -> None:
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB)
        try:
            conn.execute(
                """INSERT INTO ai_insights_cache (tg_id, insights_text, generated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(tg_id) DO UPDATE SET
                     insights_text = excluded.insights_text,
                     generated_at  = excluded.generated_at""",
                (tg_id, text)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("_save_cached_insights error: %s", exc)


_RU_MONTHS_SHORT = [
    "", "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def _fmt_money(v: float) -> str:
    """Format a float as '48 500 ₽' (space-separated thousands, no decimals)."""
    return f"{int(v):,}".replace(",", "\u00a0") + "\u00a0₽"


def _format_week_label(ws_str: str, we_str: str, value: float) -> str:
    """Return e.g. 'Нед. 18–24 мая: 48 500 ₽' for a sparkline bar tooltip."""
    try:
        ws_d = _dt.date.fromisoformat(ws_str)
        we_d = _dt.date.fromisoformat(we_str)
        d1, m1 = ws_d.day, _RU_MONTHS_SHORT[ws_d.month]
        d2, m2 = we_d.day, _RU_MONTHS_SHORT[we_d.month]
        if ws_d.month == we_d.month:
            period = f"{d1}–{d2}\u00a0{m2}"
        else:
            period = f"{d1}\u00a0{m1}\u00a0–\u00a0{d2}\u00a0{m2}"
        return f"Нед.\u00a0{period}: {_fmt_money(value)}"
    except Exception:
        return _fmt_money(value)


def _make_sparkline_svg(values: list[float], labels: list[str] | None = None) -> str:
    """Generate a tiny inline SVG bar sparkline (6 bars, oldest→newest left→right).

    If *labels* is provided (same length as *values*), each bar gets an SVG
    <title> child so browsers show a native tooltip on hover.
    """
    n = len(values)
    if n == 0:
        return ""
    bar_w, gap, h = 6, 2, 20
    w = n * bar_w + (n - 1) * gap
    max_val = max(values) if max(values) > 0 else 1
    bars = []
    for i, v in enumerate(values):
        bar_h = max(2, round(v / max_val * h))
        x = i * (bar_w + gap)
        y = h - bar_h
        color = "#6366f1" if i == n - 1 else "#94a3b8"
        title = ""
        if labels and i < len(labels):
            escaped = labels[i].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            title = f"<title>{escaped}</title>"
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" '
            f'fill="{color}" rx="1">{title}</rect>'
        )
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:inline-block;vertical-align:middle">'
        f'{"".join(bars)}</svg>'
    )


def _sparkline_labels_for_today(today, values: list[float]) -> list[str]:
    """Generate tooltip labels for sparkline bars using only today's date + cached values.

    Produces the same (ws, we) date ranges as _compute_shop_sparkline_values without
    touching the DB, so cached entries can have their SVGs upgraded in-place.
    """
    labels: list[str] = []
    for weeks_back in range(len(values), 0, -1):
        ws = (today - _dt.timedelta(days=today.weekday() + 7 * weeks_back)).isoformat()
        we = (today - _dt.timedelta(days=today.weekday() + 7 * (weeks_back - 1) + 1)).isoformat()
        v = values[len(values) - weeks_back] if weeks_back <= len(values) else 0.0
        labels.append(_format_week_label(ws, we, v))
    return labels


def _compute_shop_sparkline_values(conn, shop_name: str, today) -> tuple[list[float], list[str]]:
    """Return (values, labels) for 6 weeks oldest→newest for a single shop.

    *labels* are formatted as 'Нед. 18–24 мая: 48 500 ₽' for tooltip display.
    """
    values: list[float] = []
    labels: list[str] = []
    for weeks_back in range(6, 0, -1):
        ws = (today - _dt.timedelta(days=today.weekday() + 7 * weeks_back)).isoformat()
        we = (today - _dt.timedelta(days=today.weekday() + 7 * (weeks_back - 1) + 1)).isoformat()
        row = conn.execute(
            "SELECT SUM(quantity_sold * sale_price) FROM sales "
            "WHERE shop_name=? AND sale_date>=? AND sale_date<=?",
            (shop_name, ws, we),
        ).fetchone()
        v = float(row[0] or 0) if row else 0.0
        values.append(v)
        labels.append(_format_week_label(ws, we, v))
    return values, labels


def _compute_shop_weekly_breakdown(org_db: str) -> list[dict]:
    """Compute per-shop weekly revenue breakdown on demand (used when not cached)."""
    import datetime as _dt2
    try:
        from database import Database
        db = Database(org_db)
        today = _dt2.date.today()
        week_start = (today - _dt2.timedelta(days=today.weekday() + 7)).isoformat()
        week_end   = (today - _dt2.timedelta(days=today.weekday() + 1)).isoformat()
        prev_start = (today - _dt2.timedelta(days=today.weekday() + 14)).isoformat()
        prev_end   = (today - _dt2.timedelta(days=today.weekday() + 8)).isoformat()

        shops_raw = db.get_all_shops(include_system=False)
        shop_names = []
        for sr in (shops_raw or []):
            sn = sr[0] if isinstance(sr, (list, tuple)) else sr
            if sn and str(sn).strip():
                shop_names.append(str(sn).strip())
        if len(shop_names) < 2:
            return []

        conn = db.get_connection()
        try:
            breakdown = []
            for shop_name in shop_names:
                sw = conn.execute(
                    "SELECT SUM(quantity_sold * sale_price) FROM sales WHERE shop_name=? AND sale_date>=? AND sale_date<=?",
                    (shop_name, week_start, week_end),
                ).fetchone()
                sp = conn.execute(
                    "SELECT SUM(quantity_sold * sale_price) FROM sales WHERE shop_name=? AND sale_date>=? AND sale_date<=?",
                    (shop_name, prev_start, prev_end),
                ).fetchone()
                sparkline_vals, sparkline_labels = _compute_shop_sparkline_values(conn, shop_name, today)
                breakdown.append({
                    "name": shop_name,
                    "week_revenue": float(sw[0] or 0) if sw else 0.0,
                    "prev_revenue": float(sp[0] or 0) if sp else 0.0,
                    "sparkline_vals": sparkline_vals,
                    "sparkline_svg": _make_sparkline_svg(sparkline_vals, sparkline_labels),
                })
        finally:
            conn.close()
        breakdown.sort(key=lambda x: x["week_revenue"], reverse=True)
        return breakdown
    except Exception as exc:
        logger.error("_compute_shop_weekly_breakdown error for %s: %s", org_db, exc)
        return []


def _enrich_digest_shop_breakdown(digest: dict) -> dict:
    """Attach shop_breakdown list (with delta_pct + sparkline_svg) to a digest dict.

    Tries cached shop_breakdown_json first; falls back to on-demand computation.
    Adds delta_pct and sparkline_svg for each shop entry.
    """
    import json as _json
    raw: list[dict] = []
    cached_json = digest.get("shop_breakdown_json")
    if cached_json:
        try:
            raw = _json.loads(cached_json)
        except Exception:
            raw = []
    if not raw:
        raw = _compute_shop_weekly_breakdown(digest["org_db"])
        if raw:
            try:
                import json as _json2
                _json2_str = _json2.dumps(raw, ensure_ascii=False)
                _conn = sqlite3.connect(_SHOP_BOT_DB)
                try:
                    _conn.execute(
                        "UPDATE ai_weekly_digest_cache SET shop_breakdown_json=? WHERE org_db=?",
                        (_json2_str, digest["org_db"]),
                    )
                    _conn.commit()
                finally:
                    _conn.close()
            except Exception as _e:
                logger.debug("shop_breakdown backfill write failed: %s", _e)

    today = _dt.date.today()
    for entry in raw:
        prev = entry.get("prev_revenue", 0)
        cur = entry.get("week_revenue", 0)
        if prev > 0:
            entry["delta_pct"] = round((cur - prev) / prev * 100, 1)
        else:
            entry["delta_pct"] = None
        if entry.get("sparkline_svg") and "<title>" not in entry["sparkline_svg"]:
            vals = entry.get("sparkline_vals") or []
            if vals:
                try:
                    lbls = _sparkline_labels_for_today(today, vals)
                    entry["sparkline_svg"] = _make_sparkline_svg(vals, lbls)
                except Exception:
                    pass

    needs_sparkline = [e for e in raw if not e.get("sparkline_svg")]
    if needs_sparkline:
        enriched_ok = False
        try:
            from database import Database
            db = Database(digest["org_db"])
            conn = db.get_connection()
            try:
                for entry in needs_sparkline:
                    vals, lbls = _compute_shop_sparkline_values(conn, entry["name"], today)
                    entry["sparkline_vals"] = vals
                    entry["sparkline_svg"] = _make_sparkline_svg(vals, lbls)
                enriched_ok = True
            finally:
                conn.close()
        except Exception as exc:
            logger.error("sparkline enrich error for %s: %s", digest.get("org_db"), exc)
            for entry in needs_sparkline:
                entry.setdefault("sparkline_svg", "")
        if enriched_ok:
            try:
                import json as _json_sp
                _sp_str = _json_sp.dumps(raw, ensure_ascii=False)
                _sp_conn = sqlite3.connect(_SHOP_BOT_DB)
                try:
                    _sp_conn.execute(
                        "UPDATE ai_weekly_digest_cache SET shop_breakdown_json=? WHERE org_db=?",
                        (_sp_str, digest["org_db"]),
                    )
                    _sp_conn.commit()
                finally:
                    _sp_conn.close()
            except Exception as _sp_e:
                logger.debug("sparkline persist-back failed: %s", _sp_e)

    digest["shop_breakdown"] = raw if len(raw) >= 2 else []
    return digest


def _get_digests_for_orgs(org_dbs: list[str]) -> list[dict]:
    """Return cached weekly digests for the given list of org_db paths, newest first."""
    if not org_dbs:
        return []
    results = []
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB)
        try:
            placeholders = ",".join("?" * len(org_dbs))
            rows = conn.execute(
                f"SELECT org_db, digest_text, generated_at, shop_breakdown_json, shops_included_json FROM ai_weekly_digest_cache WHERE org_db IN ({placeholders}) ORDER BY generated_at DESC",
                org_dbs,
            ).fetchall()
        finally:
            conn.close()
        import json as _json_web
        for row in rows:
            d = {
                "org_db": row[0],
                "digest_text": row[1],
                "generated_at": row[2],
                "shop_breakdown_json": row[3],
                "shops_included_json": row[4] if len(row) > 4 else None,
            }
            # Parse shops_included snapshot
            try:
                d["shops_included"] = _json_web.loads(d["shops_included_json"]) if d["shops_included_json"] else []
            except Exception:
                d["shops_included"] = []
            _enrich_digest_shop_breakdown(d)
            results.append(d)
    except Exception as exc:
        logger.error("_get_digests_for_orgs error: %s", exc)
    return results


def _get_digest_for_org(org_db: str) -> dict | None:
    """Return the cached weekly digest for a single org_db, or None."""
    rows = _get_digests_for_orgs([org_db])
    return rows[0] if rows else None


def _has_verified_admin_email(org_db: str, owner_tg_id: int) -> bool:
    """Return True if any admin/owner in the org has a verified email in web_credentials."""
    try:
        from database import Database
        org_inst = Database(org_db)
        admin_tg_ids = org_inst.get_all_admins_telegram_ids()
        if owner_tg_id and owner_tg_id not in admin_tg_ids:
            admin_tg_ids.append(owner_tg_id)
        if not admin_tg_ids:
            return True  # fail open
        conn = sqlite3.connect(_SHOP_BOT_DB)
        try:
            placeholders = ",".join("?" * len(admin_tg_ids))
            row = conn.execute(
                f"SELECT COUNT(*) FROM web_credentials "
                f"WHERE telegram_id IN ({placeholders}) AND email_verified = 1",
                admin_tg_ids,
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row[0] > 0)
    except Exception as exc:
        logger.error("_has_verified_admin_email error: %s", exc)
        return True  # fail open — don't show false warnings on error


def _get_ai_alert_history(org_db: str) -> list[dict]:
    """Return the last 10 AI alert/digest log entries for an org DB."""
    if not org_db:
        return []
    try:
        from database import Database
        db = Database(org_db)
        return db.get_ai_alerts_log(limit=10)
    except Exception as exc:
        logger.error("_get_ai_alert_history error for %s: %s", org_db, exc)
        return []


def _resolve_network_units(tg_id: int, user_orgs: list[dict]) -> tuple[list[dict], str, int]:
    """Determine comparison units (multi-org or intra-org shops).

    Returns (units, mode, shop_count) where:
      - units: list of dicts with 'name' and 'org_db' (and optionally 'shop_name')
      - mode: 'network' (multi-org) or 'intra_org' (shops within one org)
      - shop_count: total number of comparable units
    """
    if len(user_orgs) >= 2:
        return user_orgs, "network", len(user_orgs)

    if len(user_orgs) == 1:
        org = user_orgs[0]
        shops = _get_shops_for_org(org["org_db"])
        if len(shops) >= 2:
            units = [
                {"name": s, "org_db": org["org_db"], "shop_name": s}
                for s in shops
            ]
            return units, "intra_org", len(shops)
        return [], "intra_org", len(shops)

    return [], "network", 0


def _compute_unit_summaries_for_orgs(user_orgs: list[dict]) -> tuple[str, list[dict]]:
    """Compute (mode, unit_summaries) for the given owner-orgs list.

    unit_summaries dicts include 'org_db', 'name', 'delta_pct', and all metric fields.
    """
    units, mode, _ = _resolve_network_units(0, user_orgs)
    summaries: list[dict] = []
    for unit in units:
        if mode == "intra_org":
            s = _get_shop_summary(unit["org_db"], unit["shop_name"])
        else:
            s = _get_org_summary(unit["org_db"])
        s["name"] = unit["name"]
        s["org_db"] = unit["org_db"]
        rev_prev = s.get("revenue_prev", 0)
        rev_cur = s.get("revenue_month", 0)
        s["delta_pct"] = round((rev_cur - rev_prev) / rev_prev * 100, 1) if rev_prev > 0 else None
        summaries.append(s)
    return mode, summaries


def _digest_metrics(org_db: str, mode: str, unit_summaries: list[dict], user_orgs: list[dict]) -> tuple[list[dict], str]:
    """Return (metrics_list, metrics_mode) for a single digest entry.

    For intra_org mode: returns per-shop summaries for that org.
    For network mode: returns the single org-level summary that matches org_db.
    Falls back to computing fresh summaries when unit_summaries is empty (e.g. owner
    has only one org/shop so the main network table was skipped).
    """
    if mode == "intra_org":
        metrics = [u for u in unit_summaries if u.get("org_db") == org_db]
        if not metrics and user_orgs:
            # unit_summaries may be empty if <2 shops were found at page load; recompute
            shops = _get_shops_for_org(org_db)
            for shop_name in shops:
                s = _get_shop_summary(org_db, shop_name)
                s["name"] = shop_name
                s["org_db"] = org_db
                rev_prev = s.get("revenue_prev", 0)
                rev_cur = s.get("revenue_month", 0)
                s["delta_pct"] = round((rev_cur - rev_prev) / rev_prev * 100, 1) if rev_prev > 0 else None
                metrics.append(s)
        return metrics, "intra_org"
    else:
        org_sum = next((u for u in unit_summaries if u.get("org_db") == org_db), None)
        if org_sum is None:
            # Digest org not in unit_summaries (e.g. single-org owner) — compute on demand
            s = _get_org_summary(org_db)
            org_name = next((o["name"] for o in user_orgs if o["org_db"] == org_db), "")
            s["name"] = org_name
            s["org_db"] = org_db
            rev_prev = s.get("revenue_prev", 0)
            rev_cur = s.get("revenue_month", 0)
            s["delta_pct"] = round((rev_cur - rev_prev) / rev_prev * 100, 1) if rev_prev > 0 else None
            org_sum = s
        return [org_sum], "network"


@router.get("/ai-insights")
def ai_insights_page(request: Request, saved: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from billing_utils import has_module, has_extension

    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login")

    tg_id = int(user["sub"])
    has_module_ok = has_module(tg_id, "ai_assistant")
    has_ext_ok = has_extension(tg_id, "ai_network_insights")
    has_alerts_ext = has_extension(tg_id, "ai_smart_alerts")

    role = user.get("role", "")
    is_admin = role in ("owner", "admin", "super_admin")

    user_orgs = _get_owner_orgs(tg_id)
    units, mode, shop_count = _resolve_network_units(tg_id, user_orgs)
    is_network = shop_count >= 2

    cached = _get_cached_insights(tg_id) if has_ext_ok and is_network else None

    # Collect per-unit summaries for the breakdown table (eager, no AI call needed)
    unit_summaries: list[dict] = []
    if is_network:
        for unit in units:
            if mode == "intra_org":
                s = _get_shop_summary(unit["org_db"], unit["shop_name"])
            else:
                s = _get_org_summary(unit["org_db"])
            s["name"] = unit["name"]
            s["org_db"] = unit["org_db"]
            rev_prev = s.get("revenue_prev", 0)
            rev_cur = s.get("revenue_month", 0)
            if rev_prev > 0:
                s["delta_pct"] = round((rev_cur - rev_prev) / rev_prev * 100, 1)
            else:
                s["delta_pct"] = None
            unit_summaries.append(s)

    unit_summaries.sort(key=lambda x: x.get("revenue_month", 0), reverse=True)

    # Fetch weekly digests for all owner orgs (available to any ai_assistant user)
    weekly_digests: list[dict] = []
    if has_module_ok and (has_alerts_ext or has_ext_ok):
        org_dbs = [o["org_db"] for o in user_orgs]
        # Also include session org if not already listed
        session_org_db = user.get("org_db", "")
        if session_org_db and session_org_db not in org_dbs:
            org_dbs.append(session_org_db)
        if org_dbs:
            raw = _get_digests_for_orgs(org_dbs)

            # Pre-warm unit_summaries with any digest orgs not yet computed.
            # This ensures _digest_metrics() always hits the fast lookup path
            # and never falls back to per-digest DB queries (critical for owners
            # with many shops where each fallback is 4+ SQLite reads).
            _cached_org_dbs = {u["org_db"] for u in unit_summaries}
            _digest_org_dbs = {d["org_db"] for d in raw}
            for _obd in _digest_org_dbs - _cached_org_dbs:
                if mode == "intra_org":
                    _shops = _get_shops_for_org(_obd)
                    for _shop_name in _shops:
                        _s = _get_shop_summary(_obd, _shop_name)
                        _s["name"] = _shop_name
                        _s["org_db"] = _obd
                        _rp = _s.get("revenue_prev", 0)
                        _rc = _s.get("revenue_month", 0)
                        _s["delta_pct"] = round((_rc - _rp) / _rp * 100, 1) if _rp > 0 else None
                        unit_summaries.append(_s)
                else:
                    _s = _get_org_summary(_obd)
                    _s["name"] = next((o["name"] for o in user_orgs if o["org_db"] == _obd), "")
                    _s["org_db"] = _obd
                    _rp = _s.get("revenue_prev", 0)
                    _rc = _s.get("revenue_month", 0)
                    _s["delta_pct"] = round((_rc - _rp) / _rp * 100, 1) if _rp > 0 else None
                    unit_summaries.append(_s)

            # Annotate with org name and per-unit metrics (now always from cache)
            db_to_name = {o["org_db"]: o["name"] for o in user_orgs}
            for d in raw:
                d["org_name"] = db_to_name.get(d["org_db"], "")
                d["metrics"], d["metrics_mode"] = _digest_metrics(
                    d["org_db"], mode, unit_summaries, user_orgs
                )
            weekly_digests = raw

    # Alert history from org DB (for ai_smart_alerts users)
    alert_history: list[dict] = []
    if has_module_ok and has_alerts_ext:
        session_org_db = user.get("org_db", "")
        if session_org_db:
            alert_history = _get_ai_alert_history(session_org_db)

    # AI alert settings (admin only)
    ai_alert_settings: dict | None = None
    digest_all_shops: list[str] = []
    if is_admin and has_alerts_ext:
        try:
            from database import Database as _Db
            _db = _Db(user.get("org_db", ""))
            ai_alert_settings = _db.get_ai_alert_settings()
            _shops_raw = _db.get_all_shops(include_system=False) or []
            for _sr in _shops_raw:
                _sn = _sr[0] if isinstance(_sr, (list, tuple)) else _sr
                if _sn and str(_sn).strip():
                    digest_all_shops.append(str(_sn).strip())
        except Exception:
            ai_alert_settings = {
                "enabled": True, "threshold_pct": 35, "alert_hour_msk": 10,
                "metrics": ["revenue"], "digest_context": ["products", "sellers", "plans"],
                "digest_enabled": True, "digest_day_of_week": 0, "digest_hour_msk": 9,
                "digest_push_enabled": True, "alert_push_enabled": True,
                "digest_shop_filter": [],
            }

    # Warn if email alerts are on but no admin has a verified email
    email_missing_warning = False
    if is_admin and ai_alert_settings:
        if ai_alert_settings.get("alert_email_enabled") or ai_alert_settings.get("digest_email_enabled"):
            session_org_db = user.get("org_db", "")
            if session_org_db:
                email_missing_warning = not _has_verified_admin_email(session_org_db, tg_id)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "ai_insights/index.html", {
        "user": user,
        "has_module": has_module_ok,
        "has_extension": has_ext_ok,
        "has_alerts_ext": has_alerts_ext,
        "is_network": is_network,
        "org_count": len(user_orgs),
        "shop_count": shop_count,
        "mode": mode,
        "unit_summaries": unit_summaries,
        "last_report": cached,
        "weekly_digests": weekly_digests,
        "alert_history": alert_history,
        "ai_alert_settings": ai_alert_settings,
        "digest_all_shops": digest_all_shops,
        "is_admin": is_admin,
        "csrf_token": get_csrf_token(request),
        "saved": saved == "1",
        "email_missing_warning": email_missing_warning,
        "today_label": _dt.date.today().strftime("%d.%m.%Y"),
    })


@router.get("/api/ai/weekly-digest")
def get_weekly_digest(request: Request):
    """Return cached weekly digests for the current user's orgs as JSON (no page reload)."""
    from web.auth import get_session_user
    from billing_utils import has_module, has_extension

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "no_module"}, status_code=403)

    has_alerts_ext = has_extension(tg_id, "ai_smart_alerts")
    has_ext_ok = has_extension(tg_id, "ai_network_insights")
    if not (has_alerts_ext or has_ext_ok):
        return JSONResponse({"ok": False, "error": "no_extension"}, status_code=403)

    user_orgs = _get_owner_orgs(tg_id)
    org_dbs = [o["org_db"] for o in user_orgs]
    session_org_db = user.get("org_db", "")
    if session_org_db and session_org_db not in org_dbs:
        org_dbs.append(session_org_db)

    raw = _get_digests_for_orgs(org_dbs)
    db_to_name = {o["org_db"]: o["name"] for o in user_orgs}

    # Compute unit summaries once for metrics annotation
    mode, unit_summaries = _compute_unit_summaries_for_orgs(user_orgs)

    # Pre-warm: compute summaries for digest orgs not already in unit_summaries,
    # so _digest_metrics() never falls back to per-digest DB queries.
    _cached_org_dbs = {u["org_db"] for u in unit_summaries}
    _digest_org_dbs = {d["org_db"] for d in raw}
    for _obd in _digest_org_dbs - _cached_org_dbs:
        if mode == "intra_org":
            _shops = _get_shops_for_org(_obd)
            for _shop_name in _shops:
                _s = _get_shop_summary(_obd, _shop_name)
                _s["name"] = _shop_name
                _s["org_db"] = _obd
                _rp = _s.get("revenue_prev", 0)
                _rc = _s.get("revenue_month", 0)
                _s["delta_pct"] = round((_rc - _rp) / _rp * 100, 1) if _rp > 0 else None
                unit_summaries.append(_s)
        else:
            _s = _get_org_summary(_obd)
            _s["name"] = next((o["name"] for o in user_orgs if o["org_db"] == _obd), "")
            _s["org_db"] = _obd
            _rp = _s.get("revenue_prev", 0)
            _rc = _s.get("revenue_month", 0)
            _s["delta_pct"] = round((_rc - _rp) / _rp * 100, 1) if _rp > 0 else None
            unit_summaries.append(_s)

    for d in raw:
        d["org_name"] = db_to_name.get(d["org_db"], "")
        metrics, metrics_mode = _digest_metrics(d["org_db"], mode, unit_summaries, user_orgs)
        d["metrics"] = metrics
        d["metrics_mode"] = metrics_mode

    return JSONResponse({"ok": True, "digests": raw})


@router.get("/api/ai/alert-history")
def get_alert_history(request: Request):
    """Return last 10 AI alert log entries for the current user's org as JSON."""
    from web.auth import get_session_user
    from billing_utils import has_module, has_extension

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "no_module"}, status_code=403)
    if not has_extension(tg_id, "ai_smart_alerts"):
        return JSONResponse({"ok": False, "error": "no_extension"}, status_code=403)

    session_org_db = user.get("org_db", "")
    entries = _get_ai_alert_history(session_org_db) if session_org_db else []
    return JSONResponse({"ok": True, "entries": entries})


@router.post("/api/ai/network-insights")
async def generate_network_insights(request: Request):
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)

    from web.auth import get_session_user
    from web.ai_utils import ask_llm, is_configured
    from web.rate_store import check_and_increment_ai
    from billing_utils import has_module, has_extension

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    tg_id = int(user["sub"])

    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "no_module"}, status_code=403)
    if not has_extension(tg_id, "ai_network_insights"):
        return JSONResponse({"ok": False, "error": "no_extension"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен на сервере"}, status_code=503)

    user_orgs = _get_owner_orgs(tg_id)
    units, mode, shop_count = _resolve_network_units(tg_id, user_orgs)

    if shop_count < 2:
        return JSONResponse({"ok": False, "error": "single_org"}, status_code=400)

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": "quota",
            "message": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    unit_summaries = []
    for unit in units:
        if mode == "intra_org":
            summary = _get_shop_summary(unit["org_db"], unit["shop_name"])
        else:
            summary = _get_org_summary(unit["org_db"])
        summary["name"] = unit["name"]
        unit_summaries.append(summary)

    prompt = _build_network_prompt(unit_summaries)
    system = (
        "Ты — бизнес-аналитик розничной сети магазинов. "
        "Пиши по-русски, структурированно. Без markdown. "
        "Конкретные цифры, названия магазинов, чёткие рекомендации."
    )

    try:
        answer = await ask_llm(prompt, system=system, max_tokens=800)
        if not answer:
            return JSONResponse({"ok": False, "error": "Не удалось получить ответ от AI. Попробуйте позже."})
    except Exception as exc:
        logger.error("generate_network_insights LLM error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка AI"}, status_code=500)

    _save_cached_insights(tg_id, answer)

    # Email delivery — send to owner if digest_email_enabled and verified email exists
    try:
        from web.email_utils import send_weekly_digest_email as _send_digest, is_configured as _email_ok
        if _email_ok() and user_orgs:
            from database import Database as _Database
            _first_db = _Database(user_orgs[0]["org_db"])
            _cfg = _first_db.get_ai_alert_settings()
            if _cfg.get("digest_email_enabled", False):
                _conn = sqlite3.connect(_SHOP_BOT_DB)
                try:
                    _wc = _conn.execute(
                        "SELECT email FROM web_credentials WHERE telegram_id=? AND is_verified=1 LIMIT 1",
                        (tg_id,)
                    ).fetchone()
                finally:
                    _conn.close()
                if _wc and _wc[0]:
                    import anyio as _anyio
                    await _anyio.to_thread.run_sync(
                        lambda _e=_wc[0]: _send_digest(_e, answer)
                    )
    except Exception as _email_err:
        logger.debug("generate_network_insights email: %s", _email_err)

    generated_at = _dt.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    return JSONResponse({"ok": True, "insights": answer, "generated_at": generated_at})


# ─── Личная история AI-запросов ───────────────────────────────────────────────

_HISTORY_FEATURE_LABELS = {
    "report":      "Объяснить отчёт",
    "prodesc":     "Описание товара",
    "forecast":    "Прогноз продаж",
    "plan":        "Анализ плана",
    "crosssell":   "Кросс-продажи",
    "priceadvice": "Совет по цене",
    "planhint":    "Цель плана",
}

_HISTORY_FEATURE_ICONS = {
    "report":      "📊",
    "prodesc":     "🏷️",
    "forecast":    "📈",
    "plan":        "🎯",
    "crosssell":   "🔗",
    "priceadvice": "💰",
    "planhint":    "🎯",
}

# URL для кнопки «Перейти» — None означает вычислить из input_summary
_HISTORY_FEATURE_REFS = {
    "report":      "/reports",
    "prodesc":     "/products",
    "forecast":    "/reports",
    "plan":        "/plans",
    "crosssell":   "/products",
    "priceadvice": None,   # строится из «#id» в input_summary
    "planhint":    "/plans",
}

_DIGEST_JOB_LABELS = {
    "smart_alerts":            "AI-алерт",
    "weekly_digest":           "Недельный дайджест",
    "morning_briefing":        "Утренний брифинг",
    "task_digest":             "Дайджест задач",
    "task_overdue_predictor":  "Предиктор просрочки",
    "procurement_advisor":     "Советник закупок",
    "seller_coach":            "Коуч продавца",
    "anomaly_check":           "Проверка аномалий",
}

_DIGEST_JOB_ICONS = {
    "smart_alerts":            "🚨",
    "weekly_digest":           "📊",
    "morning_briefing":        "☀️",
    "task_digest":             "📋",
    "task_overdue_predictor":  "🔮",
    "procurement_advisor":     "📦",
    "seller_coach":            "⭐",
    "anomaly_check":           "🔍",
}


@router.get("/ai/history")
def ai_history_page(request: Request):
    """Личный журнал AI-запросов текущего пользователя."""
    import re as _re
    import json as _json
    from web.auth import get_session_user
    from web.rate_store import get_ai_request_log, get_ai_digest_log
    from fastapi.responses import RedirectResponse as _RR

    user = get_session_user(request)
    if not user:
        return _RR("/login", status_code=303)

    tg_id  = int(user["sub"])
    org_db = user.get("org_db")
    days   = int(request.query_params.get("days", 30))
    if days not in (7, 14, 30):
        days = 30
    feature = request.query_params.get("feature", "")
    tab = request.query_params.get("tab", "requests")
    if tab not in ("requests", "digests"):
        tab = "requests"

    entries = []
    try:
        rows = get_ai_request_log(days=days, feature=feature or None, tg_id=tg_id, limit=100)
        for r in rows:
            r["feature_label"] = _HISTORY_FEATURE_LABELS.get(r["feature"], r["feature"])
            r["icon"] = _HISTORY_FEATURE_ICONS.get(r["feature"], "🤖")
            # Вычисляем ссылку «Перейти»
            ref = _HISTORY_FEATURE_REFS.get(r["feature"])
            if r["feature"] == "priceadvice":
                m = _re.match(r"^#(\d+)", r.get("input_summary") or "")
                ref = f"/products/{m.group(1)}" if m else "/products"
            r["ref_url"] = ref
        entries = rows
    except Exception as exc:
        logger.error("ai_history_page requests error: %s", exc)

    digest_entries = []
    try:
        if org_db:
            drows = get_ai_digest_log(days=days, org_db=org_db, limit=50)
            for d in drows:
                d["job_label"] = _DIGEST_JOB_LABELS.get(d["job_type"], d["job_type"])
                d["icon"] = _DIGEST_JOB_ICONS.get(d["job_type"], "🤖")
                if d.get("data_snapshot_json"):
                    try:
                        d["snapshot"] = _json.loads(d["data_snapshot_json"])
                    except Exception:
                        d["snapshot"] = None
                else:
                    d["snapshot"] = None
            digest_entries = drows
    except Exception as exc:
        logger.error("ai_history_page digests error: %s", exc)

    tpl = request.app.state.templates
    return tpl.TemplateResponse(request, "ai_history_user.html", {
        "user":              user,
        "entries":           entries,
        "digest_entries":    digest_entries,
        "days":              days,
        "feature":           feature,
        "tab":               tab,
        "feature_labels":    _HISTORY_FEATURE_LABELS,
        "digest_job_labels": _DIGEST_JOB_LABELS,
        "total":             len(entries),
        "digest_total":      len(digest_entries),
    })
