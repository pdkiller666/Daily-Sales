---
name: Dashboard в главном меню
description: Лёгкая сводка дня в заголовке main_menu_callback — архитектурные решения
---

## Реализация
`_quick_menu_summary(db, user_row, is_admin, tid)` в `handlers.py` (перед `main_menu_callback`).

## Ключевые решения

**Why sync DB в async:** `get_sales_summary()` и `get_low_stock_items_for_user()` — синхронные методы Database. Прямой вызов в async-функции OK (SQLite достаточно быстр для одного запроса). Аналогичный паттерн используется во всём `dashboard_handlers.py`.

**Why try/except на всё:** Main menu ОБЯЗАН открываться всегда. Любая ошибка в сводке → пустая строка, меню открывается без неё.

**Scope для admin:** Использует `get_user_org_scope(tid)` + `_scope_filter_kwargs()` из `dashboard_handlers.py` — тот же механизм, что и в полном дашборде. Импортируется локально внутри функции.

**Когда строка скрыта:** cnt == 0 AND нет малых остатков → возвращает `""`, заголовок остаётся `🏪 Главное меню` без лишних строк.

**Структура текста:** `f"🏪 <b>Главное меню</b>{_summary}{_banner}"` — сводка перед баннером подписки, parse_mode="HTML" обязателен.
