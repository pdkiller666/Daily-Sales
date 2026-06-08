"""
AI/LLM utility module — DeepSeek → Gemini → OpenRouter fallback chain.

Usage:
    from web.ai_utils import ask_llm, is_configured

    result = await ask_llm("Объясни эти данные...", system="Ты аналитик продаж.")
    if result is None:
        # все провайдеры недоступны / ключи не заданы

Ключи в env vars: DEEPSEEK_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY
Порядок попыток: DeepSeek → Gemini → OpenRouter (первый ответивший побеждает).
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


def is_configured() -> bool:
    """Возвращает True если хотя бы один API-ключ задан."""
    return bool(_DEEPSEEK_KEY or _GEMINI_KEY or _OPENROUTER_KEY)


def _reload_keys() -> None:
    """Перечитывает ключи из env (вызывается при смене env без рестарта)."""
    global _DEEPSEEK_KEY, _GEMINI_KEY, _OPENROUTER_KEY
    _DEEPSEEK_KEY   = os.getenv("DEEPSEEK_API_KEY", "")
    _GEMINI_KEY     = os.getenv("GEMINI_API_KEY", "")
    _OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")


# ─── Provider implementations ────────────────────────────────────────────────

async def _ask_deepseek(prompt: str, system: str, max_tokens: int) -> str:
    url = "https://api.deepseek.com/v1/chat/completions"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {_DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()


async def _ask_gemini(prompt: str, system: str, max_tokens: int) -> str:
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={_GEMINI_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()


async def _ask_openrouter(prompt: str, system: str, max_tokens: int) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": "deepseek/deepseek-chat-v3-0324:free",
        "messages": messages,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {_OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://dailysales.app",
        "X-Title": "DailySales",
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()


# ─── Public API ──────────────────────────────────────────────────────────────

async def ask_llm(
    prompt: str,
    system: str = "",
    max_tokens: int = 500,
) -> str | None:
    """Попробовать DeepSeek → Gemini → OpenRouter.
    Возвращает текст первого успешного ответа или None если все провайдеры недоступны.
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
            result = await fn(prompt, sys_prompt, max_tokens)
            if result:
                logger.info("ask_llm: response from %s (%d chars)", name, len(result))
                return result
        except Exception as exc:
            logger.warning("ask_llm: provider %s failed — %s", name, exc)

    logger.warning("ask_llm: all %d provider(s) failed", len(providers))
    return None


# ─── Prompt builders ─────────────────────────────────────────────────────────

def build_report_explain_prompt(
    period_label: str,
    total_transactions: int,
    total_qty: int,
    total_revenue: float,
    avg_check: float,
    growth_pct: float | None,
    top_items: list[dict],  # [{label, revenue, pct}]
) -> str:
    top_str = ""
    if top_items:
        lines = [f"  {i+1}. {t['label']} — {int(t['revenue']):,} ₽ ({t.get('pct_total', 0):.1f}%)" for i, t in enumerate(top_items[:5])]
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
) -> str:
    trend_str = ""
    if trend_pct is not None:
        direction = "рост" if trend_pct >= 0 else "снижение"
        trend_str = f" Тренд последних дней: {direction} {abs(trend_pct):.1f}%."

    return (
        f"Данные за последние {period_days} дней: "
        f"средняя дневная выручка {int(avg_daily):,} ₽, "
        f"суммарная выручка {int(total_revenue):,} ₽."
        f"{trend_str}"
        f" Лучший день недели по продажам: {best_dow or 'нет данных'}.\n\n"
        "Дай краткий прогноз на следующие 7 дней: ожидаемый диапазон выручки, "
        "на какие дни делать акцент, что важно учесть. Максимум 3 предложения."
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
