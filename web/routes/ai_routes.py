"""AI/LLM API endpoints — explain report, product description, sales forecast, plan analysis, quota."""
import hashlib as _hashlib
import json as _json
import logging
import datetime as _dt
from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai")

# ─── Plan-analysis in-memory TTL cache ────────────────────────────────────────
# Key: (org_db, plan_id) → {"text": str, "expires_at": datetime, "cached_at": datetime}
AI_PLAN_CACHE_TTL: int = 7200  # seconds; 2 hours default

_plan_analysis_cache: dict[tuple, dict] = {}


def get_plan_analysis_cache(org_db: str, plan_id: int) -> dict | None:
    """Return cached plan analysis dict {text, cached_at} or None if missing/expired."""
    key = (org_db, plan_id)
    entry = _plan_analysis_cache.get(key)
    if not entry:
        return None
    if _dt.datetime.utcnow() > entry["expires_at"]:
        _plan_analysis_cache.pop(key, None)
        return None
    return {"text": entry["text"], "cached_at": entry["cached_at"]}


def set_plan_analysis_cache(org_db: str, plan_id: int, text: str) -> None:
    """Store plan analysis result in TTL cache."""
    now = _dt.datetime.utcnow()
    _plan_analysis_cache[(org_db, plan_id)] = {
        "text": text,
        "cached_at": now,
        "expires_at": now + _dt.timedelta(seconds=AI_PLAN_CACHE_TTL),
    }


def invalidate_plan_analysis_cache(org_db: str, plan_id: int) -> None:
    """Remove a cached entry (e.g. after plan edit)."""
    _plan_analysis_cache.pop((org_db, plan_id), None)


# ─── Generic AI response cache (report / product-desc / forecast) ──────────
# Кэш хранит детерминированные ответы LLM в памяти с TTL.
# Попадание в кэш → ответ без вызова LLM и без расхода квоты.

_AI_RESPONSE_CACHE: dict[str, dict] = {}

_CACHE_TTL_SEC: dict[str, int] = {
    "report":   7_200,   # 2 ч — отчёт стабилен в течение дня
    "prodesc":  86_400,  # 24 ч — описание товара почти не меняется
    "forecast": 3_600,   # 1 ч  — прогноз достаточно свеж
}


def _resp_cache_get(key: str) -> str | None:
    entry = _AI_RESPONSE_CACHE.get(key)
    if not entry:
        return None
    if _dt.datetime.utcnow() > entry["expires_at"]:
        _AI_RESPONSE_CACHE.pop(key, None)
        return None
    return entry["text"]


def _resp_cache_set(key: str, text: str, ttl: int) -> None:
    _AI_RESPONSE_CACHE[key] = {
        "text": text,
        "expires_at": _dt.datetime.utcnow() + _dt.timedelta(seconds=ttl),
    }


def _body_hash(body: dict) -> str:
    """Стабильный MD5-хэш тела запроса (sort_keys для стабильности)."""
    return _hashlib.md5(_json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _api_csrf_ok(request: Request) -> bool:
    """Same-origin guard for JSON API endpoints.
    Принимает запрос если:
    - присутствует заголовок X-Requested-With: XMLHttpRequest (наши fetch-вызовы), или
    - заголовок Origin совпадает с host.
    Отклоняет если Origin отсутствует и X-Requested-With не задан.
    """
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


# ─── 0. Quota ─────────────────────────────────────────────────────────────────

@router.get("/quota")
async def ai_quota(request: Request):
    """Возвращает текущее использование AI пользователя за сегодня (UTC)."""
    from web.auth import get_session_user
    from web.rate_store import get_ai_daily_usage
    import datetime as _dt

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    tg_id = int(user["sub"])
    try:
        limit, is_high = _get_limits(tg_id)
        used = get_ai_daily_usage(tg_id)
        now = _dt.datetime.utcnow()
        tomorrow = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        resets_in = int((tomorrow - now).total_seconds())
        return JSONResponse({
            "ok": True,
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
            "resets_in": resets_in,
            "high_limit": is_high,
        })
    except Exception as exc:
        logger.error("ai_quota error: %s", exc)
        return JSONResponse({"ok": False, "error": "Ошибка"}, status_code=500)


# ─── 1. Объяснить отчёт ──────────────────────────────────────────────────────

@router.post("/explain-report")
async def ai_explain_report(request: Request):
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_report_explain_prompt, is_configured
    from web.rate_store import check_and_increment_ai

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    # Кэш — попадание не расходует квоту
    _ck = f"report:{_body_hash(body)}"
    _cached = _resp_cache_get(_ck)
    if _cached:
        return JSONResponse({"ok": True, "text": _cached})

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

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

        _REPORT_SYSTEM = (
            "Ты — аналитик продаж розничного магазина. "
            "Опирайся строго на предоставленные данные — не придумывай числа и факты, которых нет. "
            "Называй конкретные цифры из запроса. Пиши по-русски, без markdown, без заголовков."
        )
        _focus_tokens = {"summary": 380, "products": 460, "risks": 420, "actions": 530}
        _adaptive_tokens = min(_focus_tokens.get(focus, 420) + len(top_items) * 12, 600)
        result = await ask_llm(prompt, system=_REPORT_SYSTEM, max_tokens=_adaptive_tokens, feature="report")
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось получить ответ от AI. Попробуйте позже."})

        _resp_cache_set(_ck, result, _CACHE_TTL_SEC["report"])
        try:
            from web.rate_store import log_ai_request as _log_req
            _smry = f"{period_label} | {shop or '—'} | фокус={focus}"
            _log_req("report", tg_id, user.get("org_db"), _smry, result)
        except Exception:
            pass
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
    from web.rate_store import check_and_increment_ai

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)

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

    # Кэш — попадание не расходует квоту
    _ck = f"prodesc:{_body_hash({'n': name, 'c': category, 'p': price, 's': style})}"
    _cached = _resp_cache_get(_ck)
    if _cached:
        return JSONResponse({"ok": True, "text": _cached})

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    try:
        system_by_style = {
            "technical":  ("Ты — эксперт по товарным карточкам. "
                           "Пиши точно и фактически: называй реальные диапазоны характеристик, "
                           "вытекающие из названия и категории. "
                           "Не придумывай конкретные цифры, если они не следуют из названия. "
                           "Без markdown, без эмодзи, без заголовков. Только русский язык."),
            "marketing":  ("Ты — опытный копирайтер розничного магазина. "
                           "Пишешь живые, убеждающие описания товаров — с эмоцией и выгодой для покупателя. "
                           "Избегай канцелярита и штампов. "
                           "Без markdown, без эмодзи, без заголовков. Только русский язык."),
            "short":      ("Ты — редактор ценников. Пишешь предельно краткие, ёмкие описания: "
                           "тип товара + 1–2 главных параметра + ключевое преимущество. "
                           "Без markdown, без эмодзи. Только русский язык."),
        }
        temperature_by_style = {"technical": 0.2, "marketing": 0.5, "short": 0.3}
        system = system_by_style.get(style, system_by_style["technical"])
        temperature = temperature_by_style.get(style, 0.2)
        prompt = build_product_description_prompt(name, category, price, style=style)
        result = await ask_llm(prompt, system=system, max_tokens=400, temperature=temperature, feature="prodesc")
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось сгенерировать описание."})
        _resp_cache_set(_ck, result, _CACHE_TTL_SEC["prodesc"])
        try:
            from web.rate_store import log_ai_request as _log_req
            _smry = f"{name[:50]} [{category[:30]}] стиль={style}"
            _log_req("prodesc", tg_id, user.get("org_db"), _smry, result)
        except Exception:
            pass
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
    from web.rate_store import check_and_increment_ai

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

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    # Кэш — попадание не расходует квоту
    _ck = f"forecast:{_body_hash(body)}"
    _cached = _resp_cache_get(_ck)
    if _cached:
        return JSONResponse({"ok": True, "text": _cached})

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    try:
        import datetime as _dt
        daily_data  = [float(v) for v in body.get("daily_data", [])]
        daily_dates = body.get("daily_dates", [])
        period_days = int(body.get("period_days", len(daily_data) or 30))
        total_revenue = float(body.get("total_revenue", sum(daily_data)))

        non_zero = [v for v in daily_data if v > 0]
        avg_daily = total_revenue / period_days if period_days > 0 else 0

        trend_pct: float | None = None
        if len(daily_data) >= 6:
            mid = len(daily_data) // 2
            first_half = sum(daily_data[:mid])
            second_half = sum(daily_data[mid:])
            if first_half > 0:
                trend_pct = round((second_half - first_half) / first_half * 100, 1)

        if not non_zero or total_revenue < 100:
            return JSONResponse({"ok": False, "error": "Недостаточно данных для прогноза."})

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
            "Дай конкретный прогноз строго на основе предоставленных данных — не придумывай. "
            f"Пиши по-русски, без markdown, суммы в рублях. "
            f"Формат — ровно 3 предложения: "
            f"1) диапазон выручки на {horizon_str} с числовым обоснованием; "
            f"2) на какие дни недели сделать акцент и почему; "
            f"3) главный риск или фактор неопределённости."
        )
        result = await ask_llm(prompt, system=system, max_tokens=350, temperature=0.1, feature="forecast")
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось построить прогноз."})
        _resp_cache_set(_ck, result, _CACHE_TTL_SEC["forecast"])
        try:
            from web.rate_store import log_ai_request as _log_req
            _smry = f"горизонт={horizon}д сценарий={scenario}"
            _log_req("forecast", tg_id, user.get("org_db"), _smry, result)
        except Exception:
            pass
        return JSONResponse({"ok": True, "text": result})
    except Exception as exc:
        logger.error("ai_sales_forecast error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 4. Разбор невыполнения плана ────────────────────────────────────────────

def _format_daily(daily: list, is_revenue: bool = True) -> str:
    if not daily:
        return "  (нет данных)"
    unit = "₽" if is_revenue else "шт"
    lines = []
    DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for d in daily:
        try:
            dow = DOW_RU[_dt.date.fromisoformat(d["date"]).weekday()]
        except Exception:
            dow = "  "
        val = f"{d['amount']:,.0f} {unit}"
        lines.append(f"  {d['date']} ({dow}): {val}  [{d['count']} прод.]")
    return "\n".join(lines)


def _format_by_seller(sellers: list, is_revenue: bool = True) -> str:
    if not sellers:
        return "  (нет данных)"
    unit = "₽" if is_revenue else "шт"
    return "\n".join(
        f"  {i+1}. {s['name']}: {s['amount']:,.0f} {unit}"
        for i, s in enumerate(sellers[:8])
    )


def _format_by_category(cats: list, is_revenue: bool = True) -> str:
    if not cats:
        return "  (нет данных)"
    lines = []
    for c in cats[:10]:
        rev = f"{c['revenue']:,.0f} ₽"
        qty = f"{int(c['quantity'])} шт"
        if is_revenue:
            lines.append(f"  {c['category']}: {rev} ({qty})")
        else:
            lines.append(f"  {c['category']}: {qty} ({rev})")
    return "\n".join(lines)


@router.post("/analyze-plan")
async def ai_analyze_plan(request: Request, plan_id: int = Form(...)):
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, is_configured
    from web.rate_store import check_and_increment_ai
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    from billing_utils import has_module, has_extension
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not has_extension(tg_id, "ai_plan_analysis"):
        return JSONResponse({"ok": False, "error": "Расширение «AI-разбор планов» не подключено. Перейдите в Подписка → Расширения."}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    try:
        org_db = user.get("org_db")
        db = get_web_db(tg_id, org_db)

        plan = db.get_sales_plan_by_id(plan_id)
        if not plan:
            return JSONResponse({"ok": False, "error": "План не найден"}, status_code=404)

        today = _dt.date.today()
        if plan["plan_type"] == "weekly":
            date_from = (today - _dt.timedelta(days=today.weekday())).isoformat()
            period_label = "текущая неделя"
        else:
            date_from = today.replace(day=1).isoformat()
            period_label = "текущий месяц"
        date_to = today.isoformat()

        is_revenue = plan["metric_type"] == "turnover"
        metric_label = "выручка (₽)" if is_revenue else "количество продаж (шт)"
        unit = "₽" if is_revenue else "шт"

        # Scope filter (seller or shop)
        scope_kwargs: dict = {}
        if plan["target_type"] == "shop" and plan["shop_name"]:
            scope_kwargs["shop_name"] = plan["shop_name"]
        elif plan["target_type"] == "seller" and plan["user_id"]:
            scope_kwargs["user_id"] = plan["user_id"]

        # Plan data filter (category / product) — must match calculate_plan_actual logic
        filter_kwargs: dict = {}
        if plan.get("filter_type") and plan.get("filter_value"):
            filter_kwargs["filter_type"] = plan["filter_type"]
            filter_kwargs["filter_value"] = plan["filter_value"]

        daily_sales = db.get_daily_sales_for_period(
            date_from, date_to,
            metric_type=plan["metric_type"],
            **scope_kwargs, **filter_kwargs
        )
        by_seller = db.get_sales_by_seller_for_period(
            date_from, date_to,
            shop_name=scope_kwargs.get("shop_name"),
            metric_type=plan["metric_type"],
            **filter_kwargs
        )
        by_category = db.get_sales_by_category_for_period(
            date_from, date_to,
            **scope_kwargs, **filter_kwargs
        )

        actual_total = sum(d["amount"] for d in daily_sales)
        plan_target = plan["target_value"]
        gap = plan_target - actual_total
        achievement = round(actual_total / plan_target * 100, 1) if plan_target else 0

        scope_note = ""
        if plan["target_type"] == "shop" and plan["shop_name"]:
            scope_note = f"Магазин: {plan['shop_name']}"
        elif plan["target_type"] == "seller" and plan["target_who"]:
            scope_note = f"Продавец: {plan['target_who']}"
        if plan.get("filter_type") == "category" and plan.get("filter_value"):
            scope_note += f" | Фильтр по категории"
        elif plan.get("filter_type") == "product" and plan.get("filter_value"):
            scope_note += f" | Фильтр по товарам"

        _PLAN_SYSTEM = (
            "Ты — бизнес-аналитик розничного магазина. "
            "Опирайся строго на данные ниже — называй конкретные цифры, дни, продавцов. "
            "Не придумывай. Пиши по-русски, без markdown, без заголовков."
        )

        prompt = (
            f"Проанализируй невыполнение плана продаж.\n\n"
            f"Метрика: {metric_label}\n"
            f"План: {plan_target:,.0f} {unit} | Факт: {actual_total:,.0f} {unit} | "
            f"Выполнение: {achievement}% | Разрыв: {gap:,.0f} {unit}\n"
            f"Период: {period_label} ({date_from} — {date_to})\n"
            + (f"{scope_note}\n" if scope_note else "")
            + f"\nПродажи по дням:\n{_format_daily(daily_sales, is_revenue)}\n"
            f"\nПо продавцам (топ):\n{_format_by_seller(by_seller, is_revenue)}\n"
            f"\nПо категориям:\n{_format_by_category(by_category, is_revenue)}\n\n"
            f"Напиши разбор строго по этим данным:\n"
            f"1. Главная причина невыполнения — назови конкретный день, продавца или категорию с цифрой.\n"
            f"2. Проблемные зоны — укажи 2–3 конкретные точки провала из данных выше.\n"
            f"3. Рекомендации — 2–3 действия, каждое с конкретным числовым ориентиром.\n\n"
            f"Не упоминай данные, которых нет выше."
        )

        answer = await ask_llm(prompt, system=_PLAN_SYSTEM, max_tokens=600, temperature=0.1, feature="plan")
        if not answer:
            return JSONResponse({"ok": False, "error": "AI не смог построить анализ. Попробуйте позже."})

        set_plan_analysis_cache(org_db or "", plan_id, answer)
        try:
            from web.rate_store import log_ai_request as _log_req
            _plan_name = (plan.get("name") or plan.get("target_who") or f"#{plan_id}")[:50]
            _smry = f"план {_plan_name} ({period_label} {achievement}%)"
            _log_req("plan", tg_id, org_db, _smry, answer)
        except Exception:
            pass
        return JSONResponse({"ok": True, "text": answer})

    except Exception as exc:
        logger.error("ai_analyze_plan error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 5. Семантический поиск товаров ──────────────────────────────────────────

_SEARCH_CACHE_TTL = 1800  # 30 мин

@router.post("/product-search")
async def ai_product_search(request: Request):
    """Семантический поиск товаров по запросу на естественном языке."""
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_product_search_prompt, is_configured
    from web.rate_store import check_and_increment_ai
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    query = str(body.get("query", "")).strip()
    if not query or len(query) < 2:
        return JSONResponse({"ok": False, "error": "Введите запрос (минимум 2 символа)"}, status_code=400)
    if len(query) > 200:
        query = query[:200]

    org_db = user.get("org_db")

    # Кэш по (org_db, query) — не расходует квоту
    _ck = f"prodsearch:{_body_hash({'o': org_db or '', 'q': query.lower()})}"
    _cached = _resp_cache_get(_ck)
    if _cached:
        import json as _j
        try:
            return JSONResponse({"ok": True, **_j.loads(_cached)})
        except Exception:
            pass

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    try:
        db = get_web_db(tg_id, org_db)
        # Загружаем каталог (не более 80 позиций чтобы не переполнить контекст)
        products = db.get_all_products() or []
        if not products:
            return JSONResponse({"ok": False, "error": "В каталоге нет товаров"})

        # products: id[0] name[1] category[2] price[3] ...
        catalog_lines = []
        for p in products[:80]:
            catalog_lines.append(f"{p[0]}|{p[1]}|{p[2]}|{int(p[3] or 0)} ₽")
        products_text = "\n".join(catalog_lines)

        prompt = build_product_search_prompt(query, products_text)
        _SEARCH_SYSTEM = (
            "Ты — помощник по поиску товаров в каталоге розничного магазина. "
            "Отвечай строго JSON. Без markdown. Без пояснений вне JSON."
        )
        raw = await ask_llm(prompt, system=_SEARCH_SYSTEM, max_tokens=300, temperature=0.0, feature="prodsearch")
        if not raw:
            return JSONResponse({"ok": False, "error": "Не удалось выполнить поиск. Попробуйте позже."})

        # Парсим JSON ответ LLM
        import json as _j
        import re as _re
        _json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        parsed = {}
        if _json_match:
            try:
                parsed = _j.loads(_json_match.group())
            except Exception:
                pass
        ids = [int(x) for x in (parsed.get("ids") or []) if str(x).isdigit()]
        note = str(parsed.get("note") or "")

        # Обогащаем ответ данными о найденных товарах
        id_set = set(ids)
        found_products = [
            {"id": p[0], "name": p[1], "category": p[2], "price": float(p[3] or 0)}
            for p in products if p[0] in id_set
        ]
        # Сохраняем порядок из ids
        id_order = {pid: i for i, pid in enumerate(ids)}
        found_products.sort(key=lambda p: id_order.get(p["id"], 999))

        result_data = {"ids": ids, "products": found_products, "note": note}
        _resp_cache_set(_ck, _j.dumps(result_data, ensure_ascii=False), _SEARCH_CACHE_TTL)
        return JSONResponse({"ok": True, **result_data})

    except Exception as exc:
        logger.error("ai_product_search error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 6. Анализ совместных покупок (cross-sell) ───────────────────────────────

_CROSS_SELL_CACHE: dict[str, dict] = {}
_CROSS_SELL_TTL = 3600  # 1 час


def _cross_sell_cache_get(key: str) -> str | None:
    entry = _CROSS_SELL_CACHE.get(key)
    if not entry:
        return None
    if _dt.datetime.utcnow() > entry["expires_at"]:
        _CROSS_SELL_CACHE.pop(key, None)
        return None
    return entry["text"]


def _cross_sell_cache_set(key: str, text: str) -> None:
    _CROSS_SELL_CACHE[key] = {
        "text": text,
        "expires_at": _dt.datetime.utcnow() + _dt.timedelta(seconds=_CROSS_SELL_TTL),
    }


@router.post("/cross-sell")
async def ai_cross_sell(request: Request):
    """Анализ совместных покупок и рекомендации по допродаже."""
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_cross_sell_prompt, is_configured
    from web.rate_store import check_and_increment_ai
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)

    org_db = user.get("org_db")
    _ck = f"crosssell:{org_db or 'default'}:{_dt.date.today().isoformat()}"

    cached = _cross_sell_cache_get(_ck)
    if cached:
        return JSONResponse({"ok": True, "text": cached, "cached": True})

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    try:
        db = get_web_db(tg_id, org_db)
        conn = db.get_connection()
        try:
            # Товары, купленные одним пользователем в один день
            rows = conn.execute("""
                SELECT p1.name, p2.name, COUNT(*) AS freq
                FROM sales s1
                JOIN sales s2
                    ON s1.user_id = s2.user_id
                    AND date(s1.sale_date) = date(s2.sale_date)
                    AND s1.product_id < s2.product_id
                JOIN products p1 ON s1.product_id = p1.id
                JOIN products p2 ON s2.product_id = p2.id
                WHERE s1.sale_date >= date('now', '-60 days')
                GROUP BY s1.product_id, s2.product_id
                ORDER BY freq DESC
                LIMIT 15
            """).fetchall()
        finally:
            conn.close()

        pairs = [(r[0], r[1], r[2]) for r in rows]

        prompt = build_cross_sell_prompt(pairs)
        _CS_SYSTEM = (
            "Ты — эксперт по мерчандайзингу и розничным продажам. "
            "Анализируй данные о совместных покупках и давай конкретные практические советы. "
            "Пиши по-русски. Без markdown и заголовков."
        )
        result = await ask_llm(prompt, system=_CS_SYSTEM, max_tokens=450, temperature=0.2, feature="crosssell")
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось выполнить анализ. Попробуйте позже."})

        _cross_sell_cache_set(_ck, result)
        return JSONResponse({"ok": True, "text": result, "pairs": pairs[:10], "cached": False})

    except Exception as exc:
        logger.error("ai_cross_sell error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 7. AI-рекомендация по цене товара ───────────────────────────────────────

_PRICE_ADVICE_CACHE: dict[str, dict] = {}
_PRICE_ADVICE_TTL = 14400  # 4 часа


def _price_cache_get(key: str) -> str | None:
    entry = _PRICE_ADVICE_CACHE.get(key)
    if not entry:
        return None
    if _dt.datetime.utcnow() > entry["expires_at"]:
        _PRICE_ADVICE_CACHE.pop(key, None)
        return None
    return entry["text"]


def _price_cache_set(key: str, text: str) -> None:
    _PRICE_ADVICE_CACHE[key] = {
        "text": text,
        "expires_at": _dt.datetime.utcnow() + _dt.timedelta(seconds=_PRICE_ADVICE_TTL),
    }


@router.post("/price-advice")
async def ai_price_advice(request: Request):
    """AI-рекомендация по оптимизации цены товара (поднять / снизить / оставить)."""
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_price_advice_prompt, is_configured
    from web.rate_store import check_and_increment_ai
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    from billing_utils import has_module
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    product_id = int(body.get("product_id", 0))
    if not product_id:
        return JSONResponse({"ok": False, "error": "Укажите product_id"}, status_code=400)

    org_db = user.get("org_db")
    _ck = f"priceadvice:{org_db or 'default'}:{product_id}:{_dt.date.today().isoformat()}"
    cached = _price_cache_get(_ck)
    if cached:
        return JSONResponse({"ok": True, "text": cached, "cached": True})

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    try:
        db = get_web_db(tg_id, org_db)
        product = db.get_product(product_id)
        if not product:
            return JSONResponse({"ok": False, "error": "Товар не найден"}, status_code=404)

        # products: id[0] name[1] category[2] price[3] created_at[4] ... old_price[9]
        p_name = product[1]
        p_cat = product[2]
        p_price = float(product[3] or 0)
        p_old_price = float(product[9]) if len(product) > 9 and product[9] else None

        def _get_sales_stats(days: int) -> dict:
            try:
                conn = db.get_connection()
                try:
                    row = conn.execute("""
                        SELECT
                            COALESCE(SUM(quantity_sold), 0),
                            COALESCE(SUM(quantity_sold * sale_price), 0),
                            COUNT(DISTINCT date(sale_date)),
                            COUNT(*)
                        FROM sales
                        WHERE product_id = ?
                          AND sale_date >= date('now', '-' || ? || ' days')
                    """, (product_id, days)).fetchone()
                finally:
                    conn.close()
                total_qty = int(row[0] or 0)
                total_rev = float(row[1] or 0)
                days_with_sales = int(row[2] or 0)
                transactions = int(row[3] or 0)
                avg_per_day = round(total_qty / days, 2) if days > 0 else 0
                return {
                    "total_qty": total_qty,
                    "total_rev": total_rev,
                    "days_with_sales": days_with_sales,
                    "avg_per_day": avg_per_day,
                    "transactions": transactions,
                }
            except Exception:
                return {"total_qty": 0, "total_rev": 0, "days_with_sales": 0, "avg_per_day": 0, "transactions": 0}

        sales_30 = _get_sales_stats(30)
        sales_90 = _get_sales_stats(90)

        prompt = build_price_advice_prompt(
            product_name=p_name,
            category=p_cat,
            current_price=p_price,
            old_price=p_old_price,
            sales_30=sales_30,
            sales_90=sales_90,
        )
        _PRICE_SYSTEM = (
            "Ты — консультант по ценообразованию в розничном магазине. "
            "Анализируй скорость продаж и давай конкретную рекомендацию по цене. "
            "Если данных мало — скажи об этом, но всё равно дай осторожный вывод. "
            "Пиши по-русски. Без markdown. Без заголовков."
        )
        result = await ask_llm(prompt, system=_PRICE_SYSTEM, max_tokens=300, temperature=0.1, feature="priceadvice")
        if not result:
            return JSONResponse({"ok": False, "error": "Не удалось получить рекомендацию. Попробуйте позже."})

        _price_cache_set(_ck, result)
        return JSONResponse({
            "ok": True, "text": result, "cached": False,
            "product": {"name": p_name, "price": p_price, "old_price": p_old_price},
            "sales_30": sales_30, "sales_90": sales_90,
        })

    except Exception as exc:
        logger.error("ai_price_advice error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── 8. Plan target hint ──────────────────────────────────────────────────────

_PLAN_HINT_CACHE: dict[str, dict] = {}
_PLAN_HINT_TTL = 7200  # 2 h


def _plhint_cache_get(key: str) -> dict | None:
    entry = _PLAN_HINT_CACHE.get(key)
    if not entry:
        return None
    if _dt.datetime.utcnow() > entry["expires_at"]:
        _PLAN_HINT_CACHE.pop(key, None)
        return None
    return entry


def _plhint_cache_set(key: str, text: str, suggestion: float) -> None:
    _PLAN_HINT_CACHE[key] = {
        "text": text,
        "suggestion": suggestion,
        "expires_at": _dt.datetime.utcnow() + _dt.timedelta(seconds=_PLAN_HINT_TTL),
    }


def _query_plan_history(db, target_type: str, seller_id: int | None,
                        shop_name: str | None, plan_type: str, metric_type: str,
                        filter_type: str = "all", filter_value: str | None = None) -> list:
    """Query last 3–4 complete periods (months or weeks) for the given target.

    filter_type/filter_value mirror the plan's own product scope:
      - 'all'      → no product filter (default)
      - 'category' → filter_value = JSON list of category names
      - 'product'  → filter_value = JSON list of product ids (int)
    """
    import json as _json
    metric_expr = (
        "COALESCE(SUM(s.sale_price * s.quantity_sold), 0)"
        if metric_type == "turnover"
        else "COALESCE(SUM(s.quantity_sold), 0)"
    )
    extra_conds: list[str] = []
    params: list = []

    if plan_type == "monthly":
        period_fmt = "strftime('%Y-%m', s.sale_date)"
        lookback = "-4 months"
        limit = 4
        _MONTH_RU = {
            "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель",
            "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август",
            "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь",
        }
        def fmt_period(raw: str) -> str:
            try:
                y, m = raw.split("-")
                return f"{_MONTH_RU.get(m, m)} {y}"
            except Exception:
                return raw
    else:
        period_fmt = "strftime('%Y-%W', s.sale_date)"
        lookback = "-8 weeks"
        limit = 8
        def fmt_period(raw: str) -> str:
            return f"Неделя {raw}"

    if target_type == "seller" and seller_id:
        extra_conds.append("s.user_id = ?")
        params.append(int(seller_id))
    elif target_type == "shop" and shop_name:
        extra_conds.append("s.shop_name = ?")
        params.append(shop_name)
    else:
        return []

    # Product filter (mirrors plan filter logic)
    join_clause = ""
    if filter_type == "category" and filter_value:
        try:
            cats = _json.loads(filter_value)
            if isinstance(cats, list) and cats:
                join_clause = "LEFT JOIN products p ON s.product_id = p.id"
                extra_conds.append(f"p.category IN ({','.join('?'*len(cats))})")
                params.extend(cats)
        except Exception:
            pass
    elif filter_type == "product" and filter_value:
        try:
            ids = _json.loads(filter_value)
            if isinstance(ids, list) and ids:
                extra_conds.append(f"s.product_id IN ({','.join('?'*len(ids))})")
                params.extend(ids)
        except Exception:
            pass

    where = " AND ".join(extra_conds) if extra_conds else "1=1"

    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT {period_fmt} AS period,
                       {metric_expr} AS value
                FROM sales s
                {join_clause}
                WHERE date(s.sale_date) >= date('now', ?)
                  AND {where}
                GROUP BY period
                ORDER BY period DESC
                LIMIT ?""",
            [lookback] + params + [limit],
        )
        rows = cursor.fetchall()
        conn.close()
        import datetime as _dtt
        now_period = _dtt.datetime.utcnow().strftime("%Y-%m" if plan_type == "monthly" else "%Y-%W")
        result = []
        for r in rows:
            period_raw = r[0] or ""
            if period_raw == now_period:
                continue
            result.append({"period": fmt_period(period_raw), "value": float(r[1] or 0)})
        return result[:4]
    except Exception as _e:
        logger.error("_query_plan_history error: %s", _e)
        return []


@router.post("/plan-target-hint")
async def ai_plan_target_hint(request: Request):
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, build_plan_target_hint_prompt, is_configured
    from web.rate_store import check_and_increment_ai
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    from billing_utils import has_module
    tg_id = int(user["sub"])
    if not has_module(tg_id, "ai_assistant"):
        return JSONResponse({"ok": False, "error": "Модуль AI-помощника не подключён"}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)

    target_type = str(body.get("target_type", "")).strip()
    seller_id_raw = body.get("seller_id")
    shop_name = str(body.get("shop_name", "")).strip()
    plan_type = str(body.get("plan_type", "monthly")).strip()
    metric_type = str(body.get("metric_type", "turnover")).strip()
    who = str(body.get("who", "")).strip()
    filter_type = str(body.get("filter_type", "all")).strip()
    filter_categories = body.get("filter_categories", [])
    filter_products = body.get("filter_products", [])
    # Build filter_value JSON matching plan storage format
    import json as _bj
    filter_value: str | None = None
    if filter_type == "category" and isinstance(filter_categories, list) and filter_categories:
        filter_value = _bj.dumps(filter_categories, ensure_ascii=False)
    elif filter_type == "product" and isinstance(filter_products, list) and filter_products:
        filter_value = _bj.dumps([int(p) for p in filter_products if str(p).isdigit()])

    if target_type not in ("seller", "shop"):
        return JSONResponse({"ok": False, "error": "Укажите тип цели (seller/shop)"}, status_code=400)
    if plan_type not in ("weekly", "monthly"):
        plan_type = "monthly"
    if metric_type not in ("turnover", "quantity"):
        metric_type = "turnover"

    seller_id: int | None = None
    if target_type == "seller":
        try:
            seller_id = int(seller_id_raw)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "Укажите продавца"}, status_code=400)
    elif not shop_name:
        return JSONResponse({"ok": False, "error": "Укажите магазин"}, status_code=400)

    org_db = user.get("org_db")
    today_month = _dt.datetime.utcnow().strftime("%Y-%m")
    # Normalize filter_value for cache key (use first 32 chars of its hash)
    import hashlib as _hl
    _fv_hash = _hl.md5((filter_value or "").encode()).hexdigest()[:8] if filter_value else "all"
    scope_key = f"{seller_id or shop_name}:{plan_type}:{metric_type}:{filter_type}:{_fv_hash}"
    _ck = f"planhint:{org_db}:{scope_key}:{today_month}"

    # Build human-readable filter label for UI display
    _filter_label = ""
    if filter_type == "category" and isinstance(filter_categories, list) and filter_categories:
        _filter_label = "По категориям: " + ", ".join(str(c) for c in filter_categories)
    elif filter_type == "product" and isinstance(filter_products, list) and filter_products:
        n = len(filter_products)
        _filter_label = f"По товарам: {n} " + ("товар" if n == 1 else "товара" if 2 <= n <= 4 else "товаров")

    cached = _plhint_cache_get(_ck)
    if cached:
        return JSONResponse({"ok": True, "text": cached["text"], "suggestion": cached["suggestion"], "cached": True,
                             "filter_type": filter_type, "filter_label": _filter_label})

    limit, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, limit):
        return JSONResponse({
            "ok": False,
            "error": f"Превышен дневной лимит запросов ({limit}/день). Сброс в полночь UTC."
        }, status_code=429)

    try:
        import anyio
        db = get_web_db(tg_id, org_db)
        history = await anyio.to_thread.run_sync(
            lambda: _query_plan_history(
                db, target_type, seller_id, shop_name, plan_type, metric_type,
                filter_type=filter_type, filter_value=filter_value,
            )
        )

        if not history:
            avg = 0.0
        else:
            avg = sum(h["value"] for h in history) / len(history)
        suggestion = round(avg * 1.1)
        if metric_type == "turnover" and suggestion > 1000:
            suggestion = round(suggestion / 1000) * 1000

        if not who:
            who = shop_name if target_type == "shop" else f"продавец #{seller_id}"

        prompt = build_plan_target_hint_prompt(
            target_type=target_type,
            who=who,
            plan_type=plan_type,
            metric_type=metric_type,
            history=history,
            suggestion=float(suggestion),
        )
        _HINT_SYSTEM = (
            "Ты — аналитик продаж розничного магазина. "
            "Используй только предоставленные исторические данные. "
            "Давай конкретный числовой диапазон цели. "
            "Пиши по-русски. Без markdown. Без заголовков. Ровно 2 предложения."
        )
        result = await ask_llm(prompt, system=_HINT_SYSTEM, max_tokens=200, temperature=0.1, feature="planhint")
        if not result:
            if avg > 0:
                unit = "₽" if metric_type == "turnover" else "шт"
                result = (
                    f"На основе последних периодов средний показатель составил "
                    f"{avg:,.0f} {unit}. Рекомендуется поставить цель "
                    f"{suggestion:,.0f} {unit} (+10% к среднему)."
                )
            else:
                return JSONResponse({"ok": False, "error": "Недостаточно данных для подсказки."})

        _plhint_cache_set(_ck, result, float(suggestion))
        return JSONResponse({"ok": True, "text": result, "suggestion": float(suggestion), "cached": False,
                             "filter_type": filter_type, "filter_label": _filter_label})

    except Exception as exc:
        logger.error("ai_plan_target_hint error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── Tasks AI: чеклист ────────────────────────────────────────────────────────

@router.post("/task-checklist")
async def ai_task_checklist(request: Request):
    """Генерация чеклиста для задачи (tasks_ai extension)."""
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, is_configured
    from web.rate_store import check_and_increment_ai
    from billing_utils import has_extension, has_module

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    tg_id = int(user["sub"])

    if not has_module(tg_id, "tasks_pro") or not has_extension(tg_id, "tasks_ai"):
        return JSONResponse({"ok": False, "error": "Требуется расширение «AI для задач»."}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен."}, status_code=503)
    _limit_cl, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, _limit_cl):
        return JSONResponse({"ok": False, "error": "Дневной лимит AI исчерпан."}, status_code=429)

    try:
        body = await request.json()
        title = (body.get("title") or "").strip()[:200]
        description = (body.get("description") or "").strip()[:500]
        if not title:
            return JSONResponse({"ok": False, "error": "Укажите название задачи."})

        context = f"Задача: {title}"
        if description:
            context += f"\nОписание: {description}"

        prompt = (
            f"{context}\n\n"
            "Сгенерируй чеклист из 3–8 конкретных и понятных шагов для выполнения этой задачи. "
            "Верни ТОЛЬКО пронумерованный список, по одному пункту на строке (1. текст, 2. текст…). "
            "Без вступлений, без заключений, без markdown."
        )
        system = (
            "Ты — помощник по управлению задачами в розничном магазине. "
            "Генерируй практичные, конкретные шаги. Пиши по-русски. "
            "Отвечай ТОЛЬКО списком — без вступлений, заголовков и пояснений."
        )
        result = await ask_llm(prompt, system=system, max_tokens=400, temperature=0.3, feature="task_checklist")
        if not result:
            return JSONResponse({"ok": False, "error": "AI не ответил. Попробуйте ещё раз."})

        # Парсим пронумерованный список
        items = []
        for line in result.splitlines():
            line = line.strip()
            if not line:
                continue
            # Убираем номер: "1. ", "1) ", "• ", "- "
            import re as _re
            clean = _re.sub(r'^[\d]+[.)]\s*', '', line).strip()
            clean = _re.sub(r'^[-•*]\s*', '', clean).strip()
            if clean:
                items.append(clean)

        if not items:
            return JSONResponse({"ok": False, "error": "Не удалось разобрать ответ AI."})

        return JSONResponse({"ok": True, "items": items})

    except Exception as exc:
        logger.error("ai_task_checklist error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── Tasks AI: описание ───────────────────────────────────────────────────────

@router.post("/task-description")
async def ai_task_description(request: Request):
    """Генерация описания задачи (tasks_ai extension)."""
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, is_configured
    from web.rate_store import check_and_increment_ai
    from billing_utils import has_extension, has_module

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    tg_id = int(user["sub"])

    if not has_module(tg_id, "tasks_pro") or not has_extension(tg_id, "tasks_ai"):
        return JSONResponse({"ok": False, "error": "Требуется расширение «AI для задач»."}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен."}, status_code=503)
    _limit_desc, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, _limit_desc):
        return JSONResponse({"ok": False, "error": "Дневной лимит AI исчерпан."}, status_code=429)

    try:
        body = await request.json()
        title = (body.get("title") or "").strip()[:200]
        if not title:
            return JSONResponse({"ok": False, "error": "Укажите название задачи."})

        prompt = (
            f"Задача: {title}\n\n"
            "Напиши краткое описание этой задачи для сотрудника розничного магазина. "
            "3–5 предложений: что нужно сделать, зачем это важно, на что обратить внимание. "
            "Без списков, без markdown, просто текст."
        )
        system = (
            "Ты — менеджер розничного магазина. Пиши чётко и профессионально, по-русски. "
            "Максимум 5 предложений. Без markdown."
        )
        result = await ask_llm(prompt, system=system, max_tokens=250, temperature=0.4, feature="task_description")
        if not result:
            return JSONResponse({"ok": False, "error": "AI не ответил. Попробуйте ещё раз."})

        return JSONResponse({"ok": True, "text": result.strip()})

    except Exception as exc:
        logger.error("ai_task_description error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── Tasks AI: декомпозиция цели ─────────────────────────────────────────────

@router.post("/task-decompose")
async def ai_task_decompose(request: Request):
    """Декомпозиция цели в список подзадач (tasks_ai extension)."""
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, is_configured
    from web.rate_store import check_and_increment_ai
    from billing_utils import has_extension, has_module

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    tg_id = int(user["sub"])

    if not has_module(tg_id, "tasks_pro") or not has_extension(tg_id, "tasks_ai"):
        return JSONResponse({"ok": False, "error": "Требуется расширение «AI для задач»."}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен."}, status_code=503)
    _limit_dc, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, _limit_dc):
        return JSONResponse({"ok": False, "error": "Дневной лимит AI исчерпан."}, status_code=429)

    try:
        body = await request.json()
        goal = (body.get("goal") or "").strip()[:500]
        if not goal:
            return JSONResponse({"ok": False, "error": "Укажите цель."})

        prompt = (
            f"Цель: {goal}\n\n"
            "Разложи эту цель на 3–7 конкретных задач для команды розничного магазина. "
            "Верни JSON-массив объектов: [{\"title\": \"...\", \"description\": \"...\", \"priority\": \"normal|high|urgent\"}]. "
            "ТОЛЬКО JSON, без пояснений и markdown."
        )
        system = (
            "Ты — менеджер проектов розничного магазина. "
            "Декомпозируй цели в конкретные задачи. "
            "Возвращай ТОЛЬКО валидный JSON-массив. Пиши по-русски."
        )
        result = await ask_llm(prompt, system=system, max_tokens=600, temperature=0.3, feature="task_decompose")
        if not result:
            return JSONResponse({"ok": False, "error": "AI не ответил. Попробуйте ещё раз."})

        # Парсим JSON из ответа
        import re as _re
        import json as _json
        json_match = _re.search(r'\[.*\]', result, _re.DOTALL)
        if not json_match:
            return JSONResponse({"ok": False, "error": "Не удалось разобрать ответ AI."})
        tasks = _json.loads(json_match.group())
        # Нормализуем
        valid_priorities = {'normal', 'high', 'urgent', 'low'}
        normalized = []
        for t in tasks:
            if isinstance(t, dict) and t.get('title'):
                normalized.append({
                    'title': str(t['title'])[:200],
                    'description': str(t.get('description', ''))[:500],
                    'priority': t.get('priority', 'normal') if t.get('priority') in valid_priorities else 'normal',
                })
        if not normalized:
            return JSONResponse({"ok": False, "error": "AI вернул пустой список."})
        return JSONResponse({"ok": True, "tasks": normalized})

    except Exception as exc:
        logger.error("ai_task_decompose error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── Tasks AI: анализ выполнения (review) ─────────────────────────────────────

@router.post("/task-review")
async def ai_task_review(request: Request):
    """AI-анализ выполнения задачи (tasks_ai extension)."""
    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    from web.auth import get_session_user
    from web.ai_utils import ask_llm, is_configured
    from web.rate_store import check_and_increment_ai
    from billing_utils import has_extension, has_module

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    tg_id = int(user["sub"])

    if not has_module(tg_id, "tasks_pro") or not has_extension(tg_id, "tasks_ai"):
        return JSONResponse({"ok": False, "error": "Требуется расширение «AI для задач»."}, status_code=403)
    if not is_configured():
        return JSONResponse({"ok": False, "error": "AI не настроен."}, status_code=503)
    _limit_rv, _ = _get_limits(tg_id)
    if not check_and_increment_ai(tg_id, _limit_rv):
        return JSONResponse({"ok": False, "error": "Дневной лимит AI исчерпан."}, status_code=429)

    try:
        body = await request.json()
        title = (body.get("title") or "").strip()[:200]
        description = (body.get("description") or "").strip()[:500]
        comments = (body.get("comments") or "").strip()[:1000]
        rating = body.get("rating")

        if not title:
            return JSONResponse({"ok": False, "error": "Нет данных о задаче."})

        context = f"Задача: {title}"
        if description:
            context += f"\nОписание: {description}"
        if comments:
            context += f"\nОтчёт/комментарии исполнителя: {comments}"
        if rating:
            context += f"\nОценка выполнения: {rating}/5"

        prompt = (
            f"{context}\n\n"
            "Проанализируй выполнение задачи. Дай краткую оценку (2-4 предложения): "
            "что было сделано хорошо, что можно улучшить в следующий раз, "
            "если есть комментарии — учти их в анализе. "
            "Без markdown, по-русски."
        )
        system = (
            "Ты — наставник в розничном магазине. "
            "Давай конструктивную, конкретную обратную связь. "
            "Пиши по-русски, без markdown."
        )
        result = await ask_llm(prompt, system=system, max_tokens=300, temperature=0.4,
                               feature="task_review")
        if not result:
            return JSONResponse({"ok": False, "error": "AI не ответил. Попробуйте ещё раз."})

        return JSONResponse({"ok": True, "text": result.strip()})

    except Exception as exc:
        logger.error("ai_task_review error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)


# ─── AI-шаблон из задачи (Phase 4.4) ────────────────────────────────────────

@router.post("/task-suggest-template")
async def ai_task_suggest_template(request: Request):
    """
    Генерирует оптимизированный шаблон на основе выполненной задачи.
    Gate: tasks_ai (tasks_pro + tasks_ai extension).
    """
    from web.auth import get_session_user
    from billing_utils import has_module as _hm, has_extension as _he
    from web.ai_utils import ask_llm
    from web.rate_store import check_and_increment_ai

    if not _api_csrf_ok(request):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    telegram_id = int(user["sub"])
    if not _hm(telegram_id, 'tasks_pro') or not _he(telegram_id, 'tasks_ai'):
        return JSONResponse({"ok": False, "error": "Требуется tasks_ai"}, status_code=403)

    limit, _ = _get_limits(telegram_id)
    if not check_and_increment_ai(telegram_id, limit):
        return JSONResponse({"ok": False, "error": "Лимит AI-запросов исчерпан"}, status_code=429)

    try:
        body = await request.json()
        title = (body.get("title") or "").strip()[:300]
        description = (body.get("description") or "").strip()[:1000]
        checklist = body.get("checklist") or []
        comments = (body.get("comments") or "").strip()[:800]
        history = (body.get("history") or "").strip()[:500]

        if not title:
            return JSONResponse({"ok": False, "error": "Нет данных задачи."})

        context = f"Завершённая задача: «{title}»"
        if description:
            context += f"\nОписание: {description}"
        if checklist:
            cl_text = "; ".join(str(c) for c in checklist[:15])
            context += f"\nЧеклист: {cl_text}"
        if comments:
            context += f"\nКомментарии: {comments}"
        if history:
            context += f"\nИстория: {history}"

        prompt = (
            f"{context}\n\n"
            "На основе этой задачи создай шаблон для повторного использования. "
            "Ответь ТОЛЬКО в формате JSON (без markdown, без кода, только объект):\n"
            '{"title": "название шаблона", "description": "описание", '
            '"checklist": ["пункт 1", "пункт 2", "..."], "priority": "normal"}\n'
            "priority: low / normal / high / urgent. Максимум 10 пунктов чеклиста. "
            "Сделай описание и чеклист универсальными для повторного использования."
        )
        system = (
            "Ты — помощник по управлению задачами в розничном магазине. "
            "Создавай практичные, переиспользуемые шаблоны. "
            "Отвечай строго JSON-объектом, без markdown, по-русски."
        )
        result = await ask_llm(prompt, system=system, max_tokens=500, temperature=0.3,
                               feature="task_suggest_template")
        if not result:
            return JSONResponse({"ok": False, "error": "AI не ответил. Попробуйте ещё раз."})

        import json as _json
        result = result.strip()
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        try:
            parsed = _json.loads(result)
        except Exception:
            return JSONResponse({"ok": False, "error": "AI вернул некорректный ответ."})

        return JSONResponse({"ok": True, "template": parsed})

    except Exception as exc:
        logger.error("ai_task_suggest_template error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)

