"""AI/LLM API endpoints — explain report, product description, sales forecast."""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai")


def _api_csrf_ok(request: Request) -> bool:
    """Same-origin guard for JSON API endpoints.
    Returns False only when Origin header is present but doesn't match the host.
    Same-site fetch calls either omit Origin or match the host.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    try:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == host
    except Exception:
        return False

# Simple per-user rate limit (in-memory, resets on restart)
# Base: 20 req/hour. With ai_high_limit extension: 50 req/hour.
import time as _time
from collections import defaultdict as _defaultdict

_ai_rate: dict = _defaultdict(list)
_AI_LIMIT_BASE = 20
_AI_LIMIT_HIGH = 50
_AI_WINDOW = 3600


def _ai_rate_ok(key: str, high_limit: bool = False) -> bool:
    limit = _AI_LIMIT_HIGH if high_limit else _AI_LIMIT_BASE
    now = _time.monotonic()
    _ai_rate[key] = [t for t in _ai_rate[key] if now - t < _AI_WINDOW]
    if len(_ai_rate[key]) >= limit:
        return False
    _ai_rate[key].append(now)
    return True


# ─── 1. Объяснить отчёт ──────────────────────────────────────────────────────

@router.post("/explain-report")
async def ai_explain_report(request: Request):
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_report_explain_prompt, is_configured

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module, has_extension
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)
    high_limit = has_extension(tg_id, "ai_high_limit")
    if not _ai_rate_ok(f"ai:{user['sub']}", high_limit=high_limit):
        limit_val = _AI_LIMIT_HIGH if high_limit else _AI_LIMIT_BASE
        return JSONResponse({"ok": False, "error": f"Превышен лимит запросов ({limit_val}/час). Подождите немного."}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    try:
        period_label = body.get("period_label", "текущий период")
        summary = body.get("summary", [0, 0, 0, 0])
        growth_pct = body.get("growth_pct")
        top_items = body.get("top_items", [])
        focus = str(body.get("focus", "summary"))
        shop = str(body.get("shop", ""))
        group_by = str(body.get("group_by", ""))
        prev_revenue_raw = body.get("prev_revenue")
        prev_revenue = float(prev_revenue_raw) if prev_revenue_raw is not None else None

        prompt = build_report_explain_prompt(
            period_label=period_label,
            total_transactions=int(summary[0] or 0),
            total_qty=int(summary[1] or 0),
            total_revenue=float(summary[2] or 0),
            avg_check=float(summary[3] or 0),
            growth_pct=float(growth_pct) if growth_pct is not None else None,
            top_items=top_items,
            focus=focus,
            shop=shop,
            group_by=group_by,
            prev_revenue=prev_revenue,
        )

        result = await ask_llm(prompt, max_tokens=450)
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось получить ответ от AI. Попробуйте позже."})

        return JSONResponse({"ok": True, "text": result})

    except Exception as exc:
        logger.error("ai_explain_report error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 2. Описание товара ───────────────────────────────────────────────────────

@router.post("/product-description")
async def ai_product_description(request: Request):
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_product_description_prompt, is_configured

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module, has_extension
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)
    high_limit = has_extension(tg_id, "ai_high_limit")
    if not _ai_rate_ok(f"ai:{user['sub']}", high_limit=high_limit):
        limit_val = _AI_LIMIT_HIGH if high_limit else _AI_LIMIT_BASE
        return JSONResponse({"ok": False, "error": f"Превышен лимит запросов ({limit_val}/час)."}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Укажите название товара"}, status_code=400)

    category = str(body.get("category", "")).strip()
    price = float(body.get("price", 0) or 0)
    style = str(body.get("style", "technical"))

    try:
        system_by_style = {
            "technical":  ("Ты — эксперт по товарным карточкам. Знаешь технические характеристики популярных товаров. "
                           "Пиши конкретно: называй точные цифры и параметры. Без markdown, без эмодзи, без заголовков. "
                           "Только русский язык."),
            "marketing":  ("Ты — опытный копирайтер розничного магазина. Пишешь убедительные, "
                           "живые описания товаров, которые побуждают к покупке. "
                           "Без markdown, без эмодзи, без заголовков. Только русский язык."),
            "short":      ("Ты — редактор ценников. Пишешь очень краткие, чёткие описания. "
                           "Без markdown, без эмодзи. Только русский язык."),
        }
        system = system_by_style.get(style, system_by_style["technical"])
        prompt = build_product_description_prompt(name, category, price, style=style)
        result = await ask_llm(prompt, system=system, max_tokens=400)
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось сгенерировать описание."})
        return JSONResponse({"ok": True, "text": result})
    except Exception as exc:
        logger.error("ai_product_description error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 3. Прогноз продаж ───────────────────────────────────────────────────────

@router.post("/sales-forecast")
async def ai_sales_forecast(request: Request):
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_sales_forecast_prompt, is_configured

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module, has_extension
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not has_extension(tg_id, "ai_forecast"):
        return JSONResponse({"ok": False, "error": "Расширение «Прогноз продаж» не подключено. Перейдите в Подписка → Расширения."}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)
    high_limit = has_extension(tg_id, "ai_high_limit")
    if not _ai_rate_ok(f"ai:{user['sub']}", high_limit=high_limit):
        limit_val = _AI_LIMIT_HIGH if high_limit else _AI_LIMIT_BASE
        return JSONResponse({"ok": False, "error": f"Превышен лимит запросов ({limit_val}/час)."}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    try:
        import datetime as _dt
        daily_data  = [float(v) for v in body.get("daily_data", [])]
        daily_dates = body.get("daily_dates", [])
        period_days = int(body.get("period_days", len(daily_data) or 30))
        total_revenue = float(body.get("total_revenue", sum(daily_data)))

        non_zero = [v for v in daily_data if v > 0]
        avg_daily = total_revenue / period_days if period_days > 0 else 0

        # Trend: second half vs first half
        trend_pct: float | None = None
        if len(daily_data) >= 6:
            mid = len(daily_data) // 2
            first_half = sum(daily_data[:mid])
            second_half = sum(daily_data[mid:])
            if first_half > 0:
                trend_pct = round((second_half - first_half) / first_half * 100, 1)

        if not non_zero or total_revenue < 100:
            return JSONResponse({"ok": False, "error": "Недостаточно данных для прогноза."})

        # Best day of week from real dates
        DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        best_dow = ""
        dow_totals: dict[int, float] = {}
        dow_counts: dict[int, int] = {}
        if daily_dates and len(daily_dates) == len(daily_data):
            for iso, rev in zip(daily_dates, daily_data):
                if rev > 0:
                    try:
                        dow = _dt.date.fromisoformat(iso).weekday()
                        dow_totals[dow] = dow_totals.get(dow, 0) + rev
                        dow_counts[dow] = dow_counts.get(dow, 0) + 1
                    except Exception:
                        pass
            if dow_totals:
                best_dow = DOW_RU[max(dow_totals, key=lambda k: dow_totals[k] / max(dow_counts[k], 1))]

        # Extra stats for richer prompt
        max_day = max(daily_data) if daily_data else 0
        min_nonzero = min(non_zero) if non_zero else 0
        zero_days = len(daily_data) - len(non_zero)
        last7 = daily_data[-7:] if len(daily_data) >= 7 else daily_data

        horizon = int(body.get("horizon", 7))
        if horizon not in (7, 14, 30):
            horizon = 7
        scenario = str(body.get("scenario", "realistic"))
        shop = str(body.get("shop", ""))

        prompt = build_sales_forecast_prompt(
            period_days=period_days,
            avg_daily=avg_daily,
            trend_pct=trend_pct,
            best_dow=best_dow,
            total_revenue=total_revenue,
            max_day=max_day,
            min_nonzero=min_nonzero,
            zero_days=zero_days,
            last7=last7,
            horizon=horizon,
            scenario=scenario,
            shop=shop,
        )
        horizon_str = {7: "неделю", 14: "14 дней", 30: "месяц"}.get(horizon, "неделю")
        system = (
            "Ты — аналитик продаж розничного магазина. "
            "Дай конкретный прогноз на основе предоставленных данных. "
            f"Пиши на русском, без markdown, цифры в рублях. "
            f"Строго 3 предложения: 1) диапазон выручки на {horizon_str}, 2) на какие дни акцент, 3) главный риск."
        )
        result = await ask_llm(prompt, system=system, max_tokens=350)
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось построить прогноз."})
        return JSONResponse({"ok": True, "text": result})
    except Exception as exc:
        logger.error("ai_sales_forecast error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)
