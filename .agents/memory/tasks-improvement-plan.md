---
name: Tasks module improvement plan
description: Полный план улучшения модуля задач — 4 фазы, 28 задач; выполняется последовательно с аудитом и деплоем после каждой фазы
---

## Статус

| Фаза | Задача | Статус |
|---|---|---|
| 1.1 | Создание задач из бота (FSM-визард) | ✅ DONE |
| 1.2 | Kanban-доска в веб | ✅ DONE |
| 1.3 | История изменений задачи (task_history) | ✅ DONE |
| 1.4 | Дублирование задачи («Создать похожую») | ✅ DONE |
| 1.5 | Настраиваемые напоминания (за N часов) | ✅ DONE |
| 1.6 | Excel-экспорт задач | ✅ DONE |
| 1.7 | Bulk-операции в веб | ✅ DONE |
| 1.8 | Оценка выполнения (⭐1-5) | ✅ DONE |
| 2.1 | Добавить tasks_pro в биллинг | ✅ DONE |
| 2.2 | Дашборд аналитики задач (/tasks/analytics) | ✅ DONE |
| 2.3 | Шаблоны задач | ✅ DONE |
| 2.4 | Авто-задачи из событий (низкий остаток) | ✅ DONE |
| 2.5 | Пул незанятых задач (самоназначение) | ✅ DONE |
| 3.1 | AI-чеклист в форме задачи | ✅ DONE |
| 3.2 | AI-описание задачи | ✅ DONE |
| 3.3 | AI-декомпозиция (цель → список задач) | ✅ DONE |
| 3.4 | AI-создание задачи из бота (натуральный язык) | ✅ DONE |
| 3.5 | AI-предиктор просрочки (APScheduler 11:00) | ✅ DONE |
| 3.6 | AI-дайджест (APScheduler вторник 08:00) | ✅ DONE |
| 3.7 | AI-анализ отчёта при review | ✅ DONE |
| 4.1 | @упоминания в комментариях | ✅ DONE |
| 4.2 | Наблюдатель задачи (task_watchers) | ✅ DONE |
| 4.3 | Estimated hours + time tracking | ✅ DONE |
| 4.4 | AI-шаблоны из истории | ✅ DONE |

## ВСЕ 28 ЗАДАЧ ВЫПОЛНЕНЫ И ЗАДЕПЛОЕНЫ ✅

### Последний деплой Phase 4.1-4.4: Amvera 7553810

## Биллинг: новые единицы

- **Модуль `tasks_pro`** (249₽/мес): kanban, аналитика, шаблоны, авто-задачи, bulk, export, история
- **Расширение `tasks_ai`** (под `ai_assistant`, 199₽/мес): AI-чеклист, AI-описание, AI-декомпозиция, AI-создание из бота, AI-анализ отчёта
- **Расширение `tasks_digest`** (под `ai_assistant`, 149₽/мес): еженедельный AI-дайджест + предиктор просрочки
- Обновить пакеты: `team_bundle` ← tasks_pro; `all_in_one` ← tasks_pro + tasks_ai + tasks_digest

## Архитектурные решения (реализованные)

- task_history: (id, task_id, user_id, action, old_val, new_val, created_at) в org_*.db
- task_ratings: (task_id, rated_by, rating INT 1-5, note TEXT, rated_at) в org_*.db
- task_templates: (id, title, description, priority, checklist_json, topic_id, created_by) в org_*.db
- task_watchers: (task_id, user_id) UNIQUE в org_*.db
- task_time_logs: (id, task_id, user_id, minutes, note, created_at) в org_*.db
- tasks.estimated_hours REAL NULL — ALTER TABLE добавлена
- Напоминания: task_reminders таблица + APScheduler check + колонка tasks.reminder_hours
- Excel-экспорт: /tasks/export.xlsx
- @mentions: regex @username в comment route → уведомления Telegram+Push+notif_history
- Watch/unwatch: /tasks/{id}/watch и /tasks/{id}/unwatch с watchers в comment-notifications
- Time tracking: /tasks/{id}/log_time, /tasks/{id}/log_time/{lid}/delete, gate tasks_pro
- AI-шаблон: POST /api/ai/task-suggest-template → JSON template → saveTemplate() POST form
