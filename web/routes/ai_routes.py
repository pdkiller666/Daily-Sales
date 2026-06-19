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
        return JSONResponse({"ok": True, "text": answer})

    except Exception as exc:
        logger.error("ai_analyze_plan error: %s", exc)
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)
