---
name: AI prompt engineering conventions
description: Устоявшиеся решения по промптам, температуре и token budget для всех AI-фич
---

## Temperature по задаче

| Feature | Temperature | Причина |
|---|---|---|
| report / forecast / plan | 0.1 | аналитика, максимальная детерминированность |
| prodesc technical / short | 0.2 / 0.3 | фактические описания |
| prodesc marketing | 0.5 | творческий копирайтинг |
| AI-чат (ask_llm_with_tools) | 0.3 | разговорный, но не галлюцинирующий |

**Why:** низкая temperature снижает галлюцинации в аналитических задачах; для чата нужна чуть большая вариативность.

## Anti-hallucination clause (обязательно для всех промптов)

Каждый промпт должен содержать одно из:
- `"Опирайся строго на предоставленные данные — не придумывай числа и факты, которых нет."`
- `"Используй только данные из контекста — не упоминай то, чего нет выше."`

_DEFAULT_SYSTEM уже содержит это правило и применяется как запасной вариант.

## Min-length retry в ask_llm

Ответ < 20 символов → `_cb_failure(name)` + переход к следующему провайдеру.
Это ловит мусорные ответы типа "OK", "—", "." без засчитывания успешного вызова.

## Адаптивный max_tokens для report endpoint

```python
focus_tokens = {"summary": 380, "products": 460, "risks": 420, "actions": 530}
adaptive = min(focus_tokens.get(focus, 420) + len(top_items) * 12, 600)
```

"actions" нужен максимум токенов (4 действия, каждое на отдельной строке).

## System prompts по эндпоинту

- **report**: явный `_REPORT_SYSTEM` inline в try-блоке — не использует `_DEFAULT_SYSTEM`
- **forecast**: system строится в ai_routes.py с `horizon_str`; 3 чёткие структурные части
- **plan**: `_PLAN_SYSTEM` inline в try-блоке; prompt без "Ты аналитик" (уже в system)
- **prodesc**: `system_by_style` dict + `temperature_by_style` dict; выбор по style из запроса

## AI-чат system prompt (_build_ai_system_prompt)

Ключевые правила (в порядке важности):
1. Anti-hallucination: "не придумывай, скажи прямо если нет данных"
2. Формат: "без markdown-разметки, без заголовков"
3. Tool guidance: "используй инструменты для конкретных цифр"
4. Период по умолчанию: не передавать month/year если пользователь не назвал явно

## Reliability warning в forecast

Если `zero_days / period_days > 0.3` → добавить в prompt:
`"Внимание: {zero_days} из {period_days} дней — без продаж. Прогноз менее надёжен."`
