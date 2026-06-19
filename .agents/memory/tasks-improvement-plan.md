---
name: Tasks module improvement plan
description: Полный план улучшения модуля задач — 4 фазы, 28 задач; выполняется последовательно с аудитом и деплоем после каждой фазы
---

## Статус

| Фаза | Задача | Статус |
|---|---|---|
| 1.1 | Создание задач из бота (FSM-визард) | ⏳ В работе |
| 1.2 | Kanban-доска в веб | ⬜ |
| 1.3 | История изменений задачи (task_history) | ⬜ |
| 1.4 | Дублирование задачи («Создать похожую») | ⬜ |
| 1.5 | Настраиваемые напоминания (за N часов) | ⬜ |
| 1.6 | Excel-экспорт задач | ⬜ |
| 1.7 | Bulk-операции в веб | ⬜ |
| 1.8 | Оценка выполнения (⭐1-5) | ⬜ |
| 2.1 | Добавить tasks_pro в биллинг | ⬜ |
| 2.2 | Дашборд аналитики задач (/tasks/analytics) | ⬜ |
| 2.3 | Шаблоны задач | ⬜ |
| 2.4 | Авто-задачи из событий (низкий остаток) | ⬜ |
| 2.5 | Пул незанятых задач (самоназначение) | ⬜ |
| 3.1 | AI-чеклист в форме задачи | ⬜ |
| 3.2 | AI-описание задачи | ⬜ |
| 3.3 | AI-декомпозиция (цель → список задач) | ⬜ |
| 3.4 | AI-создание задачи из бота (натуральный язык) | ⬜ |
| 3.5 | AI-предиктор просрочки (APScheduler 11:00) | ⬜ |
| 3.6 | AI-дайджест (APScheduler вторник 08:00) | ⬜ |
| 3.7 | AI-анализ отчёта при review | ⬜ |
| 4.1 | @упоминания в комментариях | ⬜ |
| 4.2 | Наблюдатель задачи | ⬜ |
| 4.3 | Estimated hours + time tracking | ⬜ |
| 4.4 | AI-шаблоны из истории | ⬜ |

## Биллинг: новые единицы

- **Модуль `tasks_pro`** (249₽/мес): kanban, аналитика, шаблоны, авто-задачи, bulk, export, история
- **Расширение `tasks_ai`** (под `ai_assistant`, 199₽/мес): AI-чеклист, AI-описание, AI-декомпозиция, AI-создание из бота, AI-анализ отчёта
- **Расширение `tasks_digest`** (под `ai_assistant`, 149₽/мес): еженедельный AI-дайджест + предиктор просрочки
- Обновить пакеты: `team_bundle` ← tasks_pro; `all_in_one` ← tasks_pro + tasks_ai + tasks_digest

## Правила выполнения

- После каждой задачи: аудит → тест → deploy.sh → обновить AGENT_HANDOFF.md + этот файл
- Ничего не ломать: новые таблицы через ALTER TABLE IF NOT EXISTS, новые роуты append-only
- Биллинг-гейт: tasks_pro фичи через has_module(tg_id, 'tasks_pro'), AI через has_extension
- Базовые задачи (список, детали, статус, чеклист, комментарии) — всегда бесплатны

## Архитектурные решения

- task_history: новая таблица в org_*.db — (id, task_id, user_id, action, old_val, new_val, created_at)
- task_ratings: новая таблица в org_*.db — (task_id, rated_by, rating INT 1-5, note TEXT, rated_at)
- task_templates: новая таблица в org_*.db — (id, title, description, priority, checklist_json, topic_id, assign_mode, created_by)
- task_watchers: новая таблица в org_*.db — (task_id, user_id) UNIQUE
- Напоминания: новая колонка tasks.reminder_hours (INT NULL) + APScheduler check
- Excel-экспорт: /tasks/export.xlsx (аналог salary/export.xlsx)
