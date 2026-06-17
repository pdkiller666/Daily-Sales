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
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    try:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == host
    except Exception:
        return False


def _get_limits(tg_id: int) -> tuple[int, bool]:
    from billing_utils import has_extension
    from web.rate_store import get_ai_rate_limits
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
        rows = conn.execute(
            """SELECT o.id, o.name, o.db_path
               FROM organizations o
               JOIN user_org_mapping m ON m.org_id = o.id
               WHERE m.telegram_id = ? AND m.role = 'owner' AND o.is_active = 1
               ORDER BY o.name""",
            (tg_id,)
        ).fetchall()
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
            cat_row = conn.execute(
                """SELECT p.category, SUM(s.quantity * s.price) as rev
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
        row = conn.execute(
            "SELECT insights_text, generated_at FROM ai_insights_cache WHERE tg_id = ?",
            (tg_id,)
        ).fetchone()
        conn.close()
        if row:
            return {"insights_text": row[0], "generated_at": row[1]}
    except Exception as exc:
        logger.error("_get_cached_insights error: %s", exc)
    return None


def _save_cached_insights(tg_id: int, text: str) -> None:
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB)
        conn.execute(
            """INSERT INTO ai_insights_cache (tg_id, insights_text, generated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(tg_id) DO UPDATE SET
                 insights_text = excluded.insights_text,
                 generated_at  = excluded.generated_at""",
            (tg_id, text)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("_save_cached_insights error: %s", exc)


def _get_digests_for_orgs(org_dbs: list[str]) -> list[dict]:
    """Return cached weekly digests for the given list of org_db paths, newest first."""
    if not org_dbs:
        return []
    results = []
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB)
        placeholders = ",".join("?" * len(org_dbs))
        rows = conn.execute(
            f"SELECT org_db, digest_text, generated_at FROM ai_weekly_digest_cache WHERE org_db IN ({placeholders}) ORDER BY generated_at DESC",
            org_dbs,
        ).fetchall()
        conn.close()
        for row in rows:
            results.append({
                "org_db": row[0],
                "digest_text": row[1],
                "generated_at": row[2],
            })
    except Exception as exc:
        logger.error("_get_digests_for_orgs error: %s", exc)
    return results


def _get_digest_for_org(org_db: str) -> dict | None:
    """Return the cached weekly digest for a single org_db, or None."""
    rows = _get_digests_for_orgs([org_db])
    return rows[0] if rows else None


@router.get("/ai-insights")
def ai_insights_page(request: Request):
    from web.auth import get_session_user
    from billing_utils import has_module, has_extension

    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login")

    tg_id = int(user["sub"])
    has_module_ok = has_module(tg_id, "ai_assistant")
    has_ext_ok = has_extension(tg_id, "ai_network_insights")
    has_alerts_ext = has_extension(tg_id, "ai_smart_alerts")

    user_orgs = _get_owner_orgs(tg_id)
    is_network = len(user_orgs) >= 2

    cached = _get_cached_insights(tg_id) if has_ext_ok and is_network else None

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
            # Annotate with org name
            db_to_name = {o["org_db"]: o["name"] for o in user_orgs}
            for d in raw:
                d["org_name"] = db_to_name.get(d["org_db"], "")
            weekly_digests = raw

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "ai_insights/index.html", {
        "user": user,
        "has_module": has_module_ok,
        "has_extension": has_ext_ok,
        "has_alerts_ext": has_alerts_ext,
        "is_network": is_network,
        "org_count": len(user_orgs),
        "last_report": cached,
        "weekly_digests": weekly_digests,
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
    for d in raw:
        d["org_name"] = db_to_name.get(d["org_db"], "")

    return JSONResponse({"ok": True, "digests": raw})


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
    if len(user_orgs) < 2:
        return JSONResponse({"ok": False, "error": "single_org"}, status_code=400)

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": "quota",
            "message": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    org_summaries = []
    for org in user_orgs:
        summary = _get_org_summary(org["org_db"])
        summary["name"] = org["name"]
        org_summaries.append(summary)

    prompt = _build_network_prompt(org_summaries)
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

    generated_at = _dt.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    return JSONResponse({"ok": True, "insights": answer, "generated_at": generated_at})
