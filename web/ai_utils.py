"""
AI/LLM utility module — DeepSeek → Gemini → OpenRouter fallback chain.

Usage:
    from web.ai_utils import ask_llm, is_configured

    result = await ask_llm("Объясни эти данные...", system="Ты аналитик продаж.")
    if result is None:
        # все провайдеры недоступны / ключи не заданы

Ключи в env vars: DEEPSEEK_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY
Порядок попыток: DeepSeek → Gemini → OpenRouter (первый ответивший побеждает).

Устойчивость к сбоям:
- каждый провайдер обёрнут в try/except — исключение → переход к следующему
- если все упали → ask_llm() возвращает None, роуты отдают {"ok": False, "error": "..."}
- пустой ответ (empty string) тоже считается сбоем и вызывает fallback
- ошибка парсинга ответа (KeyError, IndexError) → logged + fallback
"""
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

_DEEPSEEK_KEY    = os.getenv("DEEPSEEK_API_KEY", "")
_GEMINI_KEY      = os.getenv("GEMINI_API_KEY", "")
_OPENROUTER_KEY  = os.getenv("OPENROUTER_API_KEY", "")

_TIMEOUT = aiohttp.ClientTimeout(total=28)
_DEFAULT_SYSTEM = (
    "Ты — аналитик продаж для розничных магазинов. "
    "Отвечай кратко, по делу, на русском языке. "
    "Не используй markdown-разметку. Максимум 5 предложений."
)


def _reload_keys() -> None:
    """Перечитывает ключи из env (актуально если ключи добавили без рестарта)."""
    global _DEEPSEEK_KEY, _GEMINI_KEY, _OPENROUTER_KEY
    _DEEPSEEK_KEY   = os.getenv("DEEPSEEK_API_KEY", "")
    _GEMINI_KEY     = os.getenv("GEMINI_API_KEY", "")
    _OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")


def is_configured() -> bool:
    """Возвращает True если хотя бы один API-ключ задан."""
    _reload_keys()  # всегда актуальные ключи без рестарта
    return bool(_DEEPSEEK_KEY or _GEMINI_KEY or _OPENROUTER_KEY)


# ─── Provider implementations ────────────────────────────────────────────────

async def _ask_deepseek(prompt: str, system: str, max_tokens: int, temperature: float = 0.2) -> str:
    url = "https://api.deepseek.com/v1/chat/completions"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {_DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise ValueError(f"DeepSeek returned empty choices: {data}")
            content = choices[0].get("message", {}).get("content", "").strip()
            if not content:
                raise ValueError("DeepSeek returned empty content")
            return content


async def _ask_gemini(prompt: str, system: str, max_tokens: int, temperature: float = 0.2) -> str:
    """
    Gemini REST API v1beta.
    Рабочая модель: gemini-flash-latest (alias → всегда актуальная Flash-версия).
    Резерв:       gemini-2.5-flash-lite (самая дешёвая 2.5-серия).
    """
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    base = "https://generativelanguage.googleapis.com/v1beta/models"
    gen_cfg = {"maxOutputTokens": max_tokens, "temperature": temperature}
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": gen_cfg,
    }

    for model_id in ("gemini-flash-latest", "gemini-2.5-flash-lite"):
        url = f"{base}/{model_id}:generateContent?key={_GEMINI_KEY}"
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 404:
                        logger.debug("Gemini model %s not found, trying next", model_id)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    candidates = data.get("candidates") or []
                    if not candidates:
                        # Может быть заблокировано safety filters
                        block = data.get("promptFeedback", {}).get("blockReason", "unknown")
                        raise ValueError(f"Gemini returned no candidates (block={block})")
                    parts = candidates[0].get("content", {}).get("parts") or []
                    if not parts:
                        raise ValueError("Gemini candidate has no parts")
                    content = parts[0].get("text", "").strip()
                    if not content:
                        raise ValueError("Gemini returned empty text")
                    return content
        except (aiohttp.ClientResponseError, ValueError):
            raise
        except Exception:
            raise

    raise RuntimeError("All Gemini model aliases exhausted")


async def _ask_openrouter(prompt: str, system: str, max_tokens: int, temperature: float = 0.2) -> str:
    """
    OpenRouter API — пробует модели по порядку внутри провайдера.
    Платные (дешёвые): deepseek/deepseek-chat, openai/gpt-4o-mini.
    Бесплатный tier: meta-llama/llama-3.3-70b-instruct:free (если лимит не исчерпан).
    """
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {_OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://dailysales.app",
        "X-Title": "DailySales",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    or_models = [
        "deepseek/deepseek-chat",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-4o-mini",
        "meta-llama/llama-3.2-3b-instruct:free",
    ]

    last_err: Exception | None = None
    for model in or_models:
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status in (429, 503):
                        logger.debug("OpenRouter model %s rate-limited (%d), trying next", model, resp.status)
                        continue
                    if resp.status == 404:
                        logger.debug("OpenRouter model %s not found, trying next", model)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        logger.debug("OpenRouter model %s returned empty choices", model)
                        continue
                    content = choices[0].get("message", {}).get("content", "").strip()
                    if not content:
                        logger.debug("OpenRouter model %s returned empty content", model)
                        continue
                    logger.debug("OpenRouter: got response from model=%s", model)
                    return content
        except aiohttp.ClientResponseError as exc:
            last_err = exc
            logger.debug("OpenRouter model %s error: %s", model, exc)
            continue
        except Exception as exc:
            last_err = exc
            logger.debug("OpenRouter model %s exception: %s", model, exc)
            continue

    raise RuntimeError(f"All OpenRouter models exhausted. Last error: {last_err}")


# ─── Public API ──────────────────────────────────────────────────────────────

async def ask_llm(
    prompt: str,
    system: str = "",
    max_tokens: int = 500,
    temperature: float = 0.2,
) -> str | None:
    """Попробовать DeepSeek → Gemini → OpenRouter.

    Возвращает текст первого успешного ответа или None если все провайдеры
    недоступны / ключи не заданы.

    Гарантии:
    - никогда не бросает исключений наружу
    - пустой ответ от провайдера → переход к следующему
    - timeout 28 с на провайдера
    """
    _reload_keys()
    sys_prompt = system or _DEFAULT_SYSTEM

    providers: list[tuple[str, object]] = []
    if _DEEPSEEK_KEY:
        providers.append(("DeepSeek", _ask_deepseek))
    if _GEMINI_KEY:
        providers.append(("Gemini", _ask_gemini))
    if _OPENROUTER_KEY:
        providers.append(("OpenRouter", _ask_openrouter))

    if not providers:
        logger.debug("ask_llm: no API keys configured")
        return None

    for name, fn in providers:
        try:
            result = await fn(prompt, sys_prompt, max_tokens, temperature)  # type: ignore[operator]
            if result:
                logger.info("ask_llm: OK from %s (%d chars)", name, len(result))
                return result
            logger.warning("ask_llm: %s returned empty string", name)
        except Exception as exc:
            logger.warning("ask_llm: provider %s failed — %s: %s", name, type(exc).__name__, exc)

    logger.warning("ask_llm: all %d provider(s) failed or returned empty", len(providers))
    return None


# ─── Prompt builders ─────────────────────────────────────────────────────────

def build_report_explain_prompt(
    period_label: str,
    total_transactions: int,
    total_qty: int,
    total_revenue: float,
    avg_check: float,
    growth_pct: float | None,
    top_items: list[dict],
) -> str:
    top_str = ""
    if top_items:
        lines = [
            f"  {i+1}. {t['label']} — {int(t['revenue']):,} ₽ ({t.get('pct_total', 0):.1f}%)"
            for i, t in enumerate(top_items[:5])
        ]
        top_str = "\nТоп позиций:\n" + "\n".join(lines)

    growth_str = ""
    if growth_pct is not None:
        direction = "выросла" if growth_pct >= 0 else "упала"
        growth_str = f"\nДинамика: {direction} на {abs(growth_pct):.1f}% по сравнению с предыдущим периодом."

    return (
        f"Период: {period_label}\n"
        f"Транзакций: {total_transactions}, продано единиц: {total_qty}, "
        f"выручка: {int(total_revenue):,} ₽, средний чек: {int(avg_check):,} ₽."
        f"{growth_str}"
        f"{top_str}\n\n"
        "Дай краткий аналитический комментарий: что хорошо, что насторожило, "
        "что можно сделать для улучшения. Максимум 4 предложения."
    )


def build_product_description_prompt(name: str, category: str, price: float) -> str:
    price_str = f"{int(price):,} ₽" if price > 0 else "цена не указана"
    cat_str = f", категория: {category}" if category else ""
    return (
        f"Напиши краткое описание товара для интернет-магазина.\n"
        f"Товар: {name}{cat_str}, цена: {price_str}.\n"
        "Описание: 2–3 предложения, без технических характеристик, акцент на выгоде для покупателя. "
        "Не используй список, только связный текст. Без кавычек и форматирования."
    )


def build_sales_forecast_prompt(
    period_days: int,
    avg_daily: float,
    trend_pct: float | None,
    best_dow: str,
    total_revenue: float,
    max_day: float = 0,
    min_nonzero: float = 0,
    zero_days: int = 0,
    last7: list | None = None,
) -> str:
    trend_str = ""
    if trend_pct is not None:
        direction = "рост" if trend_pct >= 0 else "снижение"
        trend_str = f"\nТренд: {direction} на {abs(trend_pct):.1f}% (вторая половина периода vs первая)."

    last7_str = ""
    if last7:
        vals = ", ".join(f"{int(v):,} ₽" for v in last7)
        last7_str = f"\nПоследние {len(last7)} дней (день за днём): {vals}."

    range_str = ""
    if max_day > 0 and min_nonzero > 0:
        range_str = f"\nДиапазон дней: лучший {int(max_day):,} ₽, худший (с продажами) {int(min_nonzero):,} ₽."

    zero_str = f"\nДней без продаж: {zero_days}." if zero_days > 0 else ""
    dow_str = f"\nЛучший день недели: {best_dow}." if best_dow else ""

    return (
        f"Период анализа: {period_days} дней. "
        f"Суммарная выручка: {int(total_revenue):,} ₽. "
        f"Средняя в день: {int(avg_daily):,} ₽."
        f"{trend_str}"
        f"{last7_str}"
        f"{range_str}"
        f"{zero_str}"
        f"{dow_str}"
        "\n\nСделай прогноз продаж на следующие 7 дней."
    )


def build_smart_alert_prompt(
    org_name: str,
    yesterday_revenue: float,
    avg_7d: float,
    drop_pct: float,
    zero_yesterday: bool,
) -> str:
    if zero_yesterday:
        return (
            f"Магазин «{org_name}»: вчера не было ни одной продажи. "
            f"Средняя выручка за последние 7 дней: {int(avg_7d):,} ₽.\n"
            "Напиши короткое тревожное уведомление для владельца магазина: "
            "1–2 предложения, без паники, с призывом проверить причины."
        )
    return (
        f"Магазин «{org_name}»: вчерашняя выручка {int(yesterday_revenue):,} ₽ "
        f"на {abs(drop_pct):.0f}% ниже средней за 7 дней ({int(avg_7d):,} ₽).\n"
        "Напиши короткое уведомление для владельца: 1–2 предложения, "
        "отметь падение и предложи 1 конкретный шаг для проверки."
    )
