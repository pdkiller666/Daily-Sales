"""
AI/LLM utility module — DeepSeek → Gemini → OpenRouter fallback chain.

Usage:
    from web.ai_utils import ask_llm, ask_llm_with_tools, is_configured

    result = await ask_llm("Объясни эти данные...", system="Ты аналитик продаж.")
    if result is None:
        # все провайдеры недоступны / ключи не заданы

    # Tool-calling (multi-turn) variant for the chat AI assistant:
    result = await ask_llm_with_tools(user_question, system_prompt, db_instance)

Ключи в env vars: DEEPSEEK_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY
Порядок попыток: DeepSeek → Gemini → OpenRouter (первый ответивший побеждает).

Устойчивость к сбоям:
- каждый провайдер обёрнут в try/except — исключение → переход к следующему
- если все упали → ask_llm() возвращает None, роуты отдают {"ok": False, "error": "..."}
- пустой ответ (empty string) тоже считается сбоем и вызывает fallback
- ошибка парсинга ответа (KeyError, IndexError) → logged + fallback
"""
import json
import logging
import os
import re
import threading
import time

import aiohttp

logger = logging.getLogger(__name__)

_DEEPSEEK_KEY    = os.getenv("DEEPSEEK_API_KEY", "")
_GEMINI_KEY      = os.getenv("GEMINI_API_KEY", "")
_OPENROUTER_KEY  = os.getenv("OPENROUTER_API_KEY", "")

_TIMEOUT = aiohttp.ClientTimeout(total=28)
_DEFAULT_SYSTEM = (
    "Ты — аналитик продаж для розничных магазинов. "
    "Опирайся строго на предоставленные данные — не придумывай числа и факты, которых нет в запросе. "
    "Если данных недостаточно для уверенного вывода — скажи об этом прямо. "
    "Пиши по-русски, кратко, без markdown-разметки и без заголовков. Максимум 5 предложений."
)


def _reload_keys() -> None:
    """Перечитывает ключи из env (актуально если ключи добавили без рестарта)."""
    global _DEEPSEEK_KEY, _GEMINI_KEY, _OPENROUTER_KEY
    _DEEPSEEK_KEY   = os.getenv("DEEPSEEK_API_KEY", "")
    _GEMINI_KEY     = os.getenv("GEMINI_API_KEY", "")
    _OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")


# ─── Circuit breaker ─────────────────────────────────────────────────────────
# Если провайдер упал — пропускаем его на _CB_TTL секунд, не ждём 28с таймаут.
# После успеха — немедленно сбрасываем состояние (half-open → closed).

_CB_TTL  = 180.0  # секунд в открытом состоянии (провайдер пропускается)
_CB_LOCK = threading.Lock()
_circuit: dict[str, float] = {}  # provider_name → monotonic timestamp "открыт до"


def _cb_is_open(name: str) -> bool:
    """True если провайдер недавно падал и ещё в карантине."""
    with _CB_LOCK:
        return _circuit.get(name, 0.0) > time.monotonic()


def _cb_failure(name: str) -> None:
    """Зафиксировать сбой провайдера — откроет circuit на _CB_TTL секунд."""
    with _CB_LOCK:
        _circuit[name] = time.monotonic() + _CB_TTL
    logger.warning("circuit_breaker: %s → OPEN for %.0fs", name, _CB_TTL)


def _cb_success(name: str) -> None:
    """Зафиксировать успех провайдера — закрыть circuit."""
    with _CB_LOCK:
        was_open = _circuit.pop(name, None)
    if was_open:
        logger.info("circuit_breaker: %s → CLOSED (recovered)", name)


def get_circuit_state() -> dict[str, dict]:
    """Вернуть текущее состояние circuit breaker (для /admin/ai-limits)."""
    now = time.monotonic()
    with _CB_LOCK:
        return {
            name: {
                "open": ts > now,
                "open_for_sec": max(0.0, round(ts - now, 1)),
            }
            for name, ts in _circuit.items()
        }


def is_configured() -> bool:
    """Возвращает True если хотя бы один API-ключ задан И kill-switch не активен."""
    try:
        from web.rate_store import get_ai_enabled
        if not get_ai_enabled():
            return False
    except Exception:
        pass
    _reload_keys()
    return bool(_DEEPSEEK_KEY or _GEMINI_KEY or _OPENROUTER_KEY)


# ─── Token usage tracking ─────────────────────────────────────────────────────

_token_lock = threading.Lock()
_token_stats: dict = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_calls": 0,
    "by_provider": {
        "deepseek":    {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0},
        "gemini":      {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0},
        "openrouter":  {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0},
    },
}

# Approximate USD cost per 1M tokens by provider (prompt_$/1M, completion_$/1M)
_PROVIDER_RATES: dict[str, tuple[float, float]] = {
    "deepseek":   (0.14,  0.28),   # deepseek-chat V3
    "gemini":     (0.075, 0.30),   # gemini-2.5-flash
    "openrouter": (0.14,  0.28),   # default: deepseek-chat via OpenRouter
}


def _accumulate_tokens(prompt: int, completion: int, provider: str = "", feature: str = "") -> None:
    """Накапливает токены в module-level счётчике (thread-safe) и персистирует в БД.

    Args:
        prompt:     число prompt-токенов
        completion: число completion-токенов
        provider:   имя провайдера ('deepseek', 'gemini', 'openrouter')
        feature:    функция ('report', 'prodesc', 'forecast', 'plan', 'chat', 'summary', 'alerts', ...)
    """
    key = provider.lower() if provider.lower() in _PROVIDER_RATES else ""
    rate_p, rate_c = _PROVIDER_RATES.get(key or "deepseek", _PROVIDER_RATES["deepseek"])
    cost_usd = (prompt * rate_p + completion * rate_c) / 1_000_000

    with _token_lock:
        _token_stats["prompt_tokens"] += prompt
        _token_stats["completion_tokens"] += completion
        _token_stats["total_calls"] += 1
        if key:
            _token_stats["by_provider"][key]["prompt_tokens"] += prompt
            _token_stats["by_provider"][key]["completion_tokens"] += completion
            _token_stats["by_provider"][key]["calls"] += 1

    # Persist to DB — survives server restarts; errors are non-critical and swallowed
    try:
        import datetime as _dt_acc
        _date_str = _dt_acc.datetime.utcnow().strftime("%Y-%m-%d")
        from web.rate_store import persist_token_cost as _persist_cost
        _persist_cost(_date_str, key or "unknown", prompt, completion, cost_usd)
        if feature:
            from web.rate_store import persist_token_cost_feature as _persist_feat
            _persist_feat(_date_str, feature, prompt, completion, cost_usd)
    except Exception:
        pass


def get_token_stats() -> dict:
    """Возвращает накопленную статистику токенов + приблизительную стоимость в USD.

    Стоимость считается по тарифам каждого провайдера отдельно:
      DeepSeek:   $0.14/1M prompt, $0.28/1M completion
      Gemini:     $0.075/1M prompt, $0.30/1M completion
      OpenRouter: $0.14/1M prompt, $0.28/1M completion (deepseek-chat tier)

    Неатрибутированные токены (провайдер не определён) — по ценам DeepSeek.
    """
    with _token_lock:
        snap = dict(_token_stats)
        by_prov = {k: dict(v) for k, v in _token_stats["by_provider"].items()}

    # Cost per provider
    cost_usd = 0.0
    attributed_prompt = 0
    attributed_completion = 0
    provider_breakdown: list[dict] = []
    for prov, data in by_prov.items():
        p, c = data["prompt_tokens"], data["completion_tokens"]
        attributed_prompt += p
        attributed_completion += c
        rate_p, rate_c = _PROVIDER_RATES[prov]
        prov_cost = (p * rate_p + c * rate_c) / 1_000_000
        cost_usd += prov_cost
        provider_breakdown.append({
            "provider":         prov,
            "prompt_tokens":    p,
            "completion_tokens": c,
            "total_tokens":     p + c,
            "calls":            data["calls"],
            "cost_usd":         round(prov_cost, 6),
        })

    # Unattributed tokens → DeepSeek fallback rates
    unattr_prompt = snap["prompt_tokens"] - attributed_prompt
    unattr_comp   = snap["completion_tokens"] - attributed_completion
    if unattr_prompt > 0 or unattr_comp > 0:
        rate_p, rate_c = _PROVIDER_RATES["deepseek"]
        cost_usd += (unattr_prompt * rate_p + unattr_comp * rate_c) / 1_000_000

    total_p = snap["prompt_tokens"]
    total_c = snap["completion_tokens"]
    return {
        "prompt_tokens":      total_p,
        "completion_tokens":  total_c,
        "total_tokens":       total_p + total_c,
        "total_calls":        snap["total_calls"],
        "cost_usd":           round(cost_usd, 6),
        "by_provider":        provider_breakdown,
    }


# ─── Provider implementations ────────────────────────────────────────────────

async def _ask_deepseek(prompt: str, system: str, max_tokens: int, temperature: float = 0.2, feature: str = "") -> str:
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
            usage = data.get("usage") or {}
            pt = int(usage.get("prompt_tokens", 0))
            ct = int(usage.get("completion_tokens", 0))
            logger.debug("DeepSeek usage: prompt=%d completion=%d", pt, ct)
            if pt or ct:
                _accumulate_tokens(pt, ct, provider="deepseek", feature=feature)
            return content


async def _ask_gemini(prompt: str, system: str, max_tokens: int, temperature: float = 0.2, feature: str = "") -> str:
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
                    usage = data.get("usageMetadata") or {}
                    pt = int(usage.get("promptTokenCount", 0))
                    ct = int(usage.get("candidatesTokenCount", 0))
                    logger.debug("Gemini usage: prompt=%d completion=%d", pt, ct)
                    if pt or ct:
                        _accumulate_tokens(pt, ct, provider="gemini", feature=feature)
                    return content
        except (aiohttp.ClientResponseError, ValueError):
            raise
        except Exception:
            raise

    raise RuntimeError("All Gemini model aliases exhausted")


async def _ask_openrouter(prompt: str, system: str, max_tokens: int, temperature: float = 0.2, feature: str = "") -> str:
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
                    usage = data.get("usage") or {}
                    pt = int(usage.get("prompt_tokens", 0))
                    ct = int(usage.get("completion_tokens", 0))
                    logger.debug("OpenRouter usage: prompt=%d completion=%d", pt, ct)
                    if pt or ct:
                        _accumulate_tokens(pt, ct, provider="openrouter", feature=feature)
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

def _is_ai_enabled() -> bool:
    """Проверяет kill-switch: env var AI_ENABLED=0 или ai_rate_config.ai_enabled=0."""
    try:
        from web.rate_store import get_ai_enabled
        return get_ai_enabled()
    except Exception:
        return True  # fail-open если rate_store недоступен


async def ask_llm(
    prompt: str,
    system: str = "",
    max_tokens: int = 500,
    temperature: float = 0.2,
    feature: str = "",
) -> str | None:
    """Попробовать DeepSeek → Gemini → OpenRouter.

    Возвращает текст первого успешного ответа или None если все провайдеры
    недоступны / ключи не заданы / kill-switch активен.

    Гарантии:
    - никогда не бросает исключений наружу
    - пустой ответ от провайдера → переход к следующему
    - timeout 28 с на провайдера
    """
    if not _is_ai_enabled():
        logger.debug("ask_llm: AI kill-switch is active — blocking request")
        return None

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
        if _cb_is_open(name):
            logger.debug("ask_llm: circuit OPEN for %s — skipping", name)
            continue
        try:
            result = await fn(prompt, sys_prompt, max_tokens, temperature, feature)  # type: ignore[operator]
            if result and len(result.strip()) >= 20:
                logger.info("ask_llm: OK from %s (%d chars)", name, len(result))
                _cb_success(name)
                return result
            if result:
                logger.warning("ask_llm: %s returned suspiciously short response (%d chars) — skipping", name, len(result))
            else:
                logger.warning("ask_llm: %s returned empty string", name)
            _cb_failure(name)
        except Exception as exc:
            logger.warning("ask_llm: provider %s failed — %s: %s", name, type(exc).__name__, exc)
            _cb_failure(name)

    logger.warning("ask_llm: all %d provider(s) failed or returned empty", len(providers))
    return None


async def ask_llm_with_usage(
    prompt: str,
    system: str = "",
    max_tokens: int = 500,
    temperature: float = 0.2,
) -> tuple[str | None, dict]:
    """Обёртка над ask_llm, возвращающая текст + метаданные токенов.

    Returns:
        (text, usage) где usage = {"prompt_tokens": int, "completion_tokens": int,
                                    "total_tokens": int, "provider": str}
        Если AI выключен или все провайдеры упали — (None, {})

    Совместимость: ask_llm остаётся без изменений; эта функция — опциональный
    вариант для вызовов, которым нужна детальная статистика токенов.
    """
    stats_before = get_token_stats()
    text = await ask_llm(prompt, system=system, max_tokens=max_tokens, temperature=temperature)
    if text is None:
        return None, {}
    stats_after = get_token_stats()
    delta_prompt = stats_after["prompt_tokens"] - stats_before["prompt_tokens"]
    delta_comp   = stats_after["completion_tokens"] - stats_before["completion_tokens"]
    # Figure out which provider was used by looking at by_provider deltas
    provider = ""
    for entry_a, entry_b in zip(
        stats_before["by_provider"],
        stats_after["by_provider"],
    ):
        if entry_b["calls"] > entry_a["calls"]:
            provider = entry_b["provider"]
            break
    usage = {
        "prompt_tokens":     delta_prompt,
        "completion_tokens": delta_comp,
        "total_tokens":      delta_prompt + delta_comp,
        "provider":          provider,
    }
    return text, usage


# ─── Multi-provider messages helper ──────────────────────────────────────────

async def _ask_with_messages(
    messages: list[dict],
    max_tokens: int = 400,
    temperature: float = 0.2,
) -> str | None:
    """Send a messages array (OpenAI-style) to the first available provider.

    For Gemini (which does not support the messages API) the conversation is
    flattened into a single text prompt.
    """
    if not _is_ai_enabled():
        logger.debug("_ask_with_messages: AI kill-switch is active — blocking request")
        return None

    _reload_keys()

    async def _deepseek(msgs: list[dict]) -> str:
        url = "https://api.deepseek.com/v1/chat/completions"
        payload = {
            "model": "deepseek-chat",
            "messages": msgs,
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
                    raise ValueError("DeepSeek empty choices")
                content = choices[0].get("message", {}).get("content", "").strip()
                if not content:
                    raise ValueError("DeepSeek empty content")
                return content

    async def _gemini(msgs: list[dict]) -> str:
        parts = []
        for m in msgs:
            role = m.get("role", "user")
            text = m.get("content", "")
            if role == "system":
                parts.append(f"[Системная инструкция]: {text}")
            elif role == "assistant":
                parts.append(f"[Ассистент]: {text}")
            else:
                parts.append(f"[Пользователь]: {text}")
        flat = "\n\n".join(parts)
        base = "https://generativelanguage.googleapis.com/v1beta/models"
        gen_cfg = {"maxOutputTokens": max_tokens, "temperature": temperature}
        payload = {
            "contents": [{"parts": [{"text": flat}]}],
            "generationConfig": gen_cfg,
        }
        for model_id in ("gemini-flash-latest", "gemini-2.5-flash-lite"):
            url = f"{base}/{model_id}:generateContent?key={_GEMINI_KEY}"
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 404:
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    candidates = data.get("candidates") or []
                    if not candidates:
                        raise ValueError("Gemini no candidates")
                    parts_resp = candidates[0].get("content", {}).get("parts") or []
                    if not parts_resp:
                        raise ValueError("Gemini no parts")
                    content = parts_resp[0].get("text", "").strip()
                    if not content:
                        raise ValueError("Gemini empty text")
                    return content
        raise RuntimeError("Gemini models exhausted")

    async def _openrouter(msgs: list[dict]) -> str:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {_OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://dailysales.app",
            "X-Title": "DailySales",
        }
        or_models = [
            "deepseek/deepseek-chat",
            "meta-llama/llama-3.3-70b-instruct:free",
            "openai/gpt-4o-mini",
        ]
        last_err: Exception | None = None
        for model in or_models:
            payload = {
                "model": model, "messages": msgs,
                "max_tokens": max_tokens, "temperature": temperature,
            }
            try:
                async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        if resp.status in (404, 429, 503):
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        content = choices[0].get("message", {}).get("content", "").strip()
                        if content:
                            return content
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"OpenRouter exhausted: {last_err}")

    providers: list[tuple[str, object]] = []
    if _DEEPSEEK_KEY:
        providers.append(("DeepSeek", _deepseek))
    if _GEMINI_KEY:
        providers.append(("Gemini", _gemini))
    if _OPENROUTER_KEY:
        providers.append(("OpenRouter", _openrouter))

    if not providers:
        return None

    for name, fn in providers:
        if _cb_is_open(name):
            logger.debug("_ask_with_messages: circuit OPEN for %s — skipping", name)
            continue
        try:
            result = await fn(messages)  # type: ignore[operator]
            if result:
                _cb_success(name)
                return result
        except Exception as exc:
            logger.warning("_ask_with_messages: %s failed — %s", name, exc)
            _cb_failure(name)

    return None


# ─── Tool-calling loop ────────────────────────────────────────────────────────

_TOOL_CALL_RE = re.compile(
    r'TOOL_CALL:\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\})', re.DOTALL
)
# Fallback 1: TOOL_CALL с кавычками/обратными тиками или '=' вместо ':'
_TOOL_CALL_LOOSE_RE = re.compile(
    r'[`"\']?TOOL_CALL[`"\']?\s*[:=]\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\})',
    re.DOTALL | re.IGNORECASE,
)
# Fallback 2: голый JSON-объект с ключом "tool" — когда модель не добавила префикс
_TOOL_CALL_BARE_RE = re.compile(
    r'\{\s*"tool"\s*:\s*"[^"]{1,64}"\s*,\s*"params"\s*:\s*(\{[^{}]*\})\s*\}',
    re.DOTALL,
)


def _extract_tool_call(response: str) -> dict | None:
    """Извлечь tool-call JSON из ответа LLM.

    Порядок попыток:
    1. Стандартный формат: ``TOOL_CALL: {...}``
    2. Нестандартный разделитель / кавычки вокруг TOOL_CALL
    3. Голый JSON-объект ``{"tool": "...", "params": {...}}``
    Возвращает спарсенный dict или None если ничего не найдено.
    """
    # 1) стандарт
    m = _TOOL_CALL_RE.search(response)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # 2) loose prefix
    m2 = _TOOL_CALL_LOOSE_RE.search(response)
    if m2:
        try:
            return json.loads(m2.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # 3) bare JSON — строим dict вручную из совпадения
    m3 = _TOOL_CALL_BARE_RE.search(response)
    if m3:
        try:
            # group(0) = весь объект, group(1) = params dict
            return json.loads(m3.group(0))
        except (json.JSONDecodeError, ValueError):
            pass

    return None

_TOOL_SYSTEM_APPENDIX = """\

{tools_description}

Как работать с инструментами:
- Если для ответа нужны данные, которых нет в контексте, ответь ТОЛЬКО одной строкой:
  TOOL_CALL: {{"tool": "название_инструмента", "params": {{"ключ": "значение"}}}}
- Если данных уже достаточно или вопрос не требует инструментов — отвечай сразу.
- Вызывай по ОДНОМУ инструменту за раз. Не придумывай данные — если их нет, скажи честно.
- ПЕРИОД: если пользователь НЕ назвал месяц/год явно — НЕ передавай параметры
  month и year, инструмент сам возьмёт ТЕКУЩИЙ месяц (см. сегодняшнюю дату выше).
  Никогда не угадывай прошлый месяц. month/year указывай только когда период
  назван в вопросе («за апрель», «в прошлом месяце» и т.п.).
- Инструмент зарплаты возвращает сводку по ВСЕЙ команде за месяц, а не лично по спрашивающему.
- Ответы только про данные ЭТОЙ организации. Ответ: 2–4 предложения."""


async def ask_llm_with_tools(
    user_question: str,
    system: str,
    db,
    max_rounds: int = 3,
    max_tokens: int = 450,
    history: list[dict] | None = None,
    temperature: float = 0.3,
) -> str | None:
    """Multi-turn tool-calling loop for the chat AI assistant.

    Protocol:
      1. Sends system (with tool descriptions appended) + user question.
      2. If the response contains TOOL_CALL: {...} → calls the tool, appends result.
      3. Repeats up to *max_rounds* times, then returns the final text answer.

    Returns None only when all LLM providers fail or kill-switch is active.
    """
    if not _is_ai_enabled():
        logger.debug("ask_llm_with_tools: AI kill-switch is active — blocking request")
        return None

    from web.ai_tools import call_tool, get_tools_description

    _reload_keys()

    tools_desc  = get_tools_description()
    full_system = system + _TOOL_SYSTEM_APPENDIX.format(tools_description=tools_desc)

    messages: list[dict] = [{"role": "system", "content": full_system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_question})

    for round_idx in range(max_rounds + 1):
        response = await _ask_with_messages(messages, max_tokens=max_tokens, temperature=temperature)
        if not response:
            return None

        call_data = _extract_tool_call(response)
        if call_data is None or round_idx >= max_rounds:
            clean = _TOOL_CALL_RE.sub("", response).strip()
            return clean or response

        tool_name   = str(call_data.get("tool", ""))
        tool_params = dict(call_data.get("params", {}))

        if not tool_name:
            clean = _TOOL_CALL_RE.sub("", response).strip()
            return clean or response

        import anyio
        tool_result = await anyio.to_thread.run_sync(
            lambda tn=tool_name, tp=tool_params: call_tool(tn, tp, db)
        )
        logger.info(
            "ask_llm_with_tools: round %d tool=%s params=%s result_len=%d",
            round_idx + 1, tool_name, tool_params, len(tool_result),
        )

        messages.append({"role": "assistant", "content": response})
        messages.append({
            "role": "user",
            "content": (
                f"TOOL_RESULT [{tool_name}]:\n{tool_result}\n\n"
                "Теперь ответь на исходный вопрос, используя полученные данные. "
                "Если нужен ещё один инструмент — вызови его. "
                "Иначе — дай финальный ответ (2–4 предложения)."
            ),
        })

    # Исчерпали max_rounds, но LLM продолжал вызывать инструменты без финального ответа.
    # Возвращаем последний tool_result как контекст с просьбой подвести итог.
    logger.warning("ask_llm_with_tools: max_rounds=%d exhausted — forcing final answer", max_rounds)
    messages.append({
        "role": "user",
        "content": "Дай краткий финальный ответ на основе полученных данных. Не вызывай инструменты.",
    })
    fallback = await _ask_with_messages(messages, max_tokens=max_tokens)
    if fallback:
        return _TOOL_CALL_RE.sub("", fallback).strip() or fallback
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
    focus: str = "summary",
    shop: str = "",
    group_by: str = "",
    prev_revenue: float | None = None,
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
    elif prev_revenue is not None and prev_revenue > 0 and total_revenue > 0:
        diff_pct = (total_revenue - prev_revenue) / prev_revenue * 100
        direction = "выросла" if diff_pct >= 0 else "упала"
        growth_str = f"\nДинамика: {direction} на {abs(diff_pct):.1f}% по сравнению с предыдущим периодом."

    context_str = ""
    if shop:
        context_str += f"\nМагазин: {shop}."
    if group_by and group_by not in ("product", ""):
        group_labels = {"category": "по категориям", "seller": "по продавцам", "shop": "по магазинам"}
        context_str += f"\nГруппировка: {group_labels.get(group_by, group_by)}."

    focus_instructions = {
        "summary":  (
            "Дай конкретный аналитический комментарий, ссылаясь на приведённые цифры: "
            "что хорошо (назови конкретный показатель), что насторожило, что можно улучшить. "
            "Максимум 4 предложения."
        ),
        "products": (
            "Сфокусируйся на топ-позициях из данных выше: какие товары лидируют и почему это важно, "
            "что стоит усилить или дозаказать, что тормозит продажи. "
            "Не упоминай товары, которых нет в списке. Максимум 4 предложения."
        ),
        "risks":    (
            "Определи 2–3 конкретных риска, основанных только на приведённых данных: "
            "отрицательная динамика, падение среднего чека, высокая концентрация на одной позиции и т.п. "
            "Для каждого риска — 1 конкретный тревожный сигнал. Максимум 4 предложения."
        ),
        "actions":  (
            "Предложи 3–4 конкретных действия для роста продаж в следующем периоде. "
            "Каждое действие — отдельная строка, начинается с глагола (Проверить / Усилить / Сократить / Запустить). "
            "Основывайся на данных выше — не придумывай общих советов."
        ),
    }
    instruction = focus_instructions.get(focus, focus_instructions["summary"])

    return (
        f"Период: {period_label}\n"
        f"Транзакций: {total_transactions}, продано единиц: {total_qty}, "
        f"выручка: {int(total_revenue):,} ₽, средний чек: {int(avg_check):,} ₽."
        f"{growth_str}"
        f"{context_str}"
        f"{top_str}\n\n"
        f"{instruction}\n\n"
        f"Опирайся только на цифры и факты из запроса — не придумывай данных, которых нет."
    )


def build_product_description_prompt(name: str, category: str, price: float, style: str = "technical") -> str:
    price_str = f"{int(price):,} ₽" if price > 0 else "цена не указана"
    cat_str = f"Категория: {category}." if category else ""

    if style == "marketing":
        return (
            f"Напиши продающее описание товара для розничного магазина.\n"
            f"Название: {name}. {cat_str} Цена: {price_str}.\n\n"
            "Требования:\n"
            "1. Начни с яркого преимущества или выгоды для покупателя.\n"
            "2. Используй эмоциональные, убеждающие формулировки.\n"
            "3. Упомяни 2–3 ключевые характеристики товара.\n"
            "4. Заверши призывом к покупке или подчёркиванием ценности.\n"
            "Без markdown, без кавычек, без заголовков. Объём: 3–5 предложений."
        )
    elif style == "short":
        return (
            f"Напиши очень краткое описание товара для ценника.\n"
            f"Название: {name}. {cat_str} Цена: {price_str}.\n\n"
            "Требования: 1–2 предложения, только самое главное. "
            "Назови тип товара и 1–2 ключевых параметра. "
            "Без markdown, без кавычек."
        )
    else:
        return (
            f"Напиши описание товара для карточки в розничном магазине.\n"
            f"Название: {name}. {cat_str} Цена: {price_str}.\n\n"
            "Требования к описанию:\n"
            "1. Определи тип товара и его назначение по названию.\n"
            "2. Укажи 3–5 ключевых характеристик, которые логически следуют из названия и категории товара "
            "(процессор, экран, объём памяти, материал, мощность и т.п.). "
            "Если точное значение неизвестно — используй типичные диапазоны для данного класса товара.\n"
            "3. Не придумывай конкретные цифры (частоту, ёмкость, размер), если они не вытекают из названия — "
            "в таком случае описывай функционально, без выдуманных значений.\n"
            "4. Добавь 1–2 предложения о главных преимуществах для покупателя.\n"
            "Формат: сначала характеристики, потом предложение о выгоде. "
            "Без markdown, без кавычек, без заголовков. Объём: 4–6 предложений."
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
    horizon: int = 7,
    scenario: str = "realistic",
    shop: str = "",
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
    shop_str = f"\nМагазин: {shop}." if shop else ""

    scenario_instructions = {
        "realistic":    f"Дай реалистичный прогноз на следующие {horizon} дней, опираясь на текущий тренд и среднюю.",
        "optimistic":   f"Дай оптимистичный прогноз на следующие {horizon} дней — предположи, что положительный тренд усилится.",
        "conservative": f"Дай консервативный прогноз на следующие {horizon} дней — ориентируйся на нижнюю границу диапазона.",
    }
    scenario_str = scenario_instructions.get(scenario, scenario_instructions["realistic"])

    # Предупреждение о надёжности прогноза при большом числе нулевых дней
    reliability_str = ""
    if zero_days > 0 and period_days > 0 and zero_days / period_days > 0.3:
        reliability_str = f"\nВнимание: {zero_days} из {period_days} дней — без продаж. Прогноз менее надёжен."

    return (
        f"Период анализа: {period_days} дней. "
        f"Суммарная выручка: {int(total_revenue):,} ₽. "
        f"Средняя в день: {int(avg_daily):,} ₽."
        f"{shop_str}"
        f"{trend_str}"
        f"{last7_str}"
        f"{range_str}"
        f"{zero_str}"
        f"{dow_str}"
        f"{reliability_str}"
        f"\n\n{scenario_str} "
        f"Опирайся строго на приведённые данные."
    )


def build_weekly_digest_prompt(
    org_name: str,
    week_revenue: float,
    prev_week_revenue: float,
    top_products: list | None = None,
    top_sellers: list | None = None,
    plans: list | None = None,
    category_breakdown: list | None = None,
    daily_revenues: list | None = None,
) -> str:
    """Промпт для еженедельного позитивного дайджеста (понедельник 09:00 МСК).

    Отправляется всегда при наличии данных за неделю — не только при падениях.
    Акцент: что продавалось хорошо, кто лидировал, выполнение планов.

    Args:
        category_breakdown: list of dicts {"category", "revenue"} or (category, revenue) tuples,
            sorted by revenue desc.
        daily_revenues: list of 7 floats (Mon=0 … Sun=6), 0.0 for days with no sales.
    """
    _DOW_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

    growth_str = ""
    if prev_week_revenue > 0:
        diff_pct = (week_revenue - prev_week_revenue) / prev_week_revenue * 100
        direction = "выросла" if diff_pct >= 0 else "снизилась"
        growth_str = f" ({direction} на {abs(diff_pct):.0f}% vs прошлая неделя)"

    lines: list[str] = [
        f"Магазин «{org_name}». Итоги недели: выручка {int(week_revenue):,} ₽{growth_str}."
    ]

    if top_products:
        parts = []
        for row in top_products[:3]:
            name, qty, rev = row[0], row[1], row[2]
            parts.append(f"{name} — {int(qty)} шт., {int(rev):,} ₽")
        lines.append("Топ товары недели: " + "; ".join(parts) + ".")

    if top_sellers:
        parts = []
        for row in top_sellers[:3]:
            fn, ln, shop, rev = row[0] or "", row[1] or "", row[2] or "", row[3]
            seller = f"{fn} {ln}".strip() or shop or "—"
            parts.append(f"{seller} — {int(rev):,} ₽")
        lines.append("Лидеры продаж: " + "; ".join(parts) + ".")

    if plans:
        plan_parts = []
        for p in plans[:3]:
            pct = p.get("pct", 0)
            label = p.get("label", "план")
            plan_parts.append(f"{label} — {pct}%")
        lines.append("Выполнение планов: " + "; ".join(plan_parts) + ".")

    if category_breakdown:
        cat_parts = []
        for item in category_breakdown[:3]:
            if isinstance(item, dict):
                cat_name, cat_rev = item.get("category", "—"), item.get("revenue", 0)
            else:
                cat_name, cat_rev = item[0], item[1]
            cat_parts.append(f"{cat_name} — {int(cat_rev):,} ₽")
        lines.append("По категориям: " + "; ".join(cat_parts) + ".")

    if daily_revenues and len(daily_revenues) == 7 and max(daily_revenues) > 0:
        best_idx = daily_revenues.index(max(daily_revenues))
        worst_idx = daily_revenues.index(min(daily_revenues))
        trend_str = (
            f"Лучший день недели — {_DOW_RU[best_idx]} ({int(max(daily_revenues)):,} ₽)"
        )
        if best_idx != worst_idx:
            trend_str += f", слабый — {_DOW_RU[worst_idx]} ({int(min(daily_revenues)):,} ₽)"
        lines.append(trend_str + ".")

    data_block = " ".join(lines)

    return (
        f"{data_block}\n\n"
        "Напиши короткий позитивный дайджест для владельца магазина (3–4 предложения): "
        "отметь, что продавалось хорошо и какая категория лидировала, "
        "кто из продавцов отличился, в какой день была лучшая динамика, "
        "и дай 1 конкретный совет по развитию на следующую неделю. "
        "Тон — дружелюбный, поддерживающий, без паники даже если есть небольшое снижение. "
        "Опирайся только на данные выше — не упоминай продавцов или товары, "
        "если они не приведены в контексте. Не придумывай цифры."
    )


def build_smart_alert_prompt(
    org_name: str,
    yesterday_revenue: float,
    avg_7d: float,
    drop_pct: float,
    zero_yesterday: bool,
    top_products: list | None = None,
    top_sellers: list | None = None,
    plans: list | None = None,
    digest_context: list | None = None,
) -> str:
    # digest_context controls which supplementary blocks to include;
    # None means all blocks are enabled (backward-compatible default)
    if digest_context is None:
        digest_context = ["products", "sellers", "plans"]

    # Build rich context block from optional supplementary data
    ctx_lines: list[str] = []

    if top_products and "products" in digest_context:
        parts = []
        for row in top_products[:3]:
            name, qty, rev = row[0], row[1], row[2]
            parts.append(f"{name} — {int(qty)} шт., {int(rev):,} ₽")
        ctx_lines.append("Топ товары (месяц): " + "; ".join(parts) + ".")

    if top_sellers and "sellers" in digest_context:
        parts = []
        for row in top_sellers[:3]:
            fn, ln, shop, rev = row[0] or "", row[1] or "", row[2] or "", row[3]
            seller = f"{fn} {ln}".strip() or shop or "—"
            parts.append(f"{seller} {int(rev):,} ₽")
        ctx_lines.append("Топ продавцы (месяц): " + "; ".join(parts) + ".")

    if plans and "plans" in digest_context:
        plan_parts = []
        for p in plans[:3]:
            pct = p.get("pct", 0)
            label = p.get("label", "план")
            plan_parts.append(f"{label} — {pct}%")
        ctx_lines.append("Планы: " + "; ".join(plan_parts) + ".")

    ctx_block = ("\nКонтекст: " + " ".join(ctx_lines)) if ctx_lines else ""

    if zero_yesterday:
        return (
            f"Магазин «{org_name}»: вчера не было ни одной продажи. "
            f"Средняя выручка за последние 7 дней: {int(avg_7d):,} ₽.{ctx_block}\n\n"
            "Напиши короткое уведомление для владельца (ровно 2 предложения): "
            "1) Констатируй факт нулевых продаж и сумму потенциально упущенной выручки (от средней). "
            "2) Предложи 1 конкретный первый шаг: позвонить продавцу / проверить расписание / "
            "проверить работу кассы. Без паники, деловой тон."
        )
    return (
        f"Магазин «{org_name}»: вчерашняя выручка {int(yesterday_revenue):,} ₽ "
        f"на {abs(drop_pct):.0f}% ниже средней за 7 дней ({int(avg_7d):,} ₽). "
        f"Разрыв: {int(avg_7d - yesterday_revenue):,} ₽.{ctx_block}\n\n"
        "Напиши уведомление для владельца (ровно 2 предложения): "
        "1) Назови конкретную сумму падения и процент отклонения от нормы. "
        "2) Предложи 1 конкретное действие для диагностики — сравни с аналогичным днём прошлой недели, "
        "проверь конкретную категорию или поговори с продавцами. "
        "Не придумывай данные, которых нет выше."
    )
