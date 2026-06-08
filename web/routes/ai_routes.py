"""AI/LLM API endpoints — explain report, product description, sales forecast."""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai")

# Simple per-user rate limit: 20 requests / hour (in-memory, resets on restart)
import time as _time
from collections import defaultdict as _defaultdict

_ai_rate: dict = _defaultdict(list)
_AI_LIMIT = 20
_AI_WINDOW = 3600


def _ai_rate_ok(key: str) -> bool:
    now = _time.monotonic()
    _ai_rate[key] = [t for t in _ai_rate[key] if now - t < _AI_WINDOW]
    if len(_ai_rate[key]) >= _AI_LIMIT:
        return False
    _ai_rate[key].append(now)
    return True


# ─── 1. Объяснить отчёт ──────────────────────────────────────────────────────

@router.post("/explain-report")
async def ai_explain_report(request: Request):
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_report_explain_prompt, is_configured

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)
    if not _ai_rate_ok(f"ai:{user['sub']}"):
        return JSONResponse({"ok": False, "error": "Слишком много запросов. Подождите немного."}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    try:
        period_label = body.get("period_label", "текущий период")
        summary = body.get("summary", [0, 0, 0, 0])
        growth_pct = body.get("growth_pct")
        top_items = body.get("top_items", [])

        prompt = build_report_explain_prompt(
            period_label=period_label,
            total_transactions=int(summary[0] or 0),
            total_qty=int(summary[1] or 0),
            total_revenue=float(summary[2] or 0),
            avg_check=float(summary[3] or 0),
            growth_pct=float(growth_pct) if growth_pct is not None else None,
            top_items=top_items,
        )

        result = await ask_llm(prompt, max_tokens=400)
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось получить ответ от AI. Попробуйте позже."})

        return JSONResponse({"ok": True, "text": result})

    except Exception as exc:
        logger.error("ai_explain_report error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 2. Описание товара ───────────────────────────────────────────────────────

@router.post("/product-description")
async def ai_product_description(request: Request):
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_product_description_prompt, is_configured

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)
    if not _ai_rate_ok(f"ai:{user['sub']}"):
        return JSONResponse({"ok": False, "error": "Слишком много запросов."}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Укажите название товара"}, status_code=400)

    category = str(body.get("category", "")).strip()
    price = float(body.get("price", 0) or 0)

    try:
        prompt = build_product_description_prompt(name, category, price)
        result = await ask_llm(prompt, max_tokens=200)
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось сгенерировать описание."})
        return JSONResponse({"ok": True, "text": result})
    except Exception as exc:
        logger.error("ai_product_description error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 3. Прогноз продаж ───────────────────────────────────────────────────────

@router.post("/sales-forecast")
async def ai_sales_forecast(request: Request):
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_sales_forecast_prompt, is_configured

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)
    if not _ai_rate_ok(f"ai:{user['sub']}"):
        return JSONResponse({"ok": False, "error": "Слишком много запросов."}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    try:
        daily_data = body.get("daily_data", [])
        period_days = int(body.get("period_days", len(daily_data) or 30))
        total_revenue = float(body.get("total_revenue", sum(daily_data)))
        best_dow = str(body.get("best_dow", ""))

        # Compute avg and trend
        non_zero = [v for v in daily_data if v > 0]
        avg_daily = total_revenue / period_days if period_days > 0 else 0
        trend_pct: float | None = None
        if len(daily_data) >= 6:
            first_half = sum(daily_data[:len(daily_data)//2])
            second_half = sum(daily_data[len(daily_data)//2:])
            if first_half > 0:
                trend_pct = round((second_half - first_half) / first_half * 100, 1)

        if not non_zero or total_revenue < 100:
            return JSONResponse({"ok": False, "error": "Недостаточно данных для прогноза."})

        prompt = build_sales_forecast_prompt(
            period_days=period_days,
            avg_daily=avg_daily,
            trend_pct=trend_pct,
            best_dow=best_dow,
            total_revenue=total_revenue,
        )
        result = await ask_llm(prompt, max_tokens=300)
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось построить прогноз."})
        return JSONResponse({"ok": True, "text": result})
    except Exception as exc:
        logger.error("ai_sales_forecast error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)
