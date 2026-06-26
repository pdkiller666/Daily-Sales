---
name: Web cabinet browser smoke tests
description: How the Playwright browser smoke tests for the web cabinet run, what they cover, and the env tricks needed to make them pass.
---

# Браузерные smoke-тесты веб-кабинета (`test_web_smoke.py`)

Root-level standalone тест (как остальные `test_*.py`, без pytest). Поднимает
реальный FastAPI веб-кабинет в фоновом uvicorn-потоке и открывает его настоящим
headless-Chromium через Playwright. Покрывает: тёмная тема (шапка + шторка
«Ещё»), клик по строке товара → карточка, быстрый перенос канбана, наличие
push-кнопок, скачивание PDF ценника, живость inline-JS и Alpine (т.е. CSP не
режет скрипты). Подключён барьером в `deploy.sh` (шаг «0. Браузерные
smoke-тесты…») — реальный провал (exit 1) останавливает деплой; отсутствие
браузера/playwright = SKIP (exit 0), деплой продолжается.

## Почему именно так (нетривиальные решения)
- **Chromium**: бандл-браузер Playwright НЕ работает (нет `libnspr4.so`).
  Запускать системный nix-Chromium: `p.chromium.launch(executable_path=<путь>,
  args=["--no-sandbox"])`. Nix-пакеты `chromium`/`nss`/`nspr` уже в `replit.nix`.
  `playwright` ставится через pip (НЕ в requirements.txt — Amvera не нужен);
  deploy.sh сам доустанавливает при отсутствии (самовосстановление контейнера).
- **Супер-админ зашит**: `env_manager.is_super_admin` хардкод `"921098636"`
  (НЕ env). Этот tg_id → `billing_utils.has_module()` возвращает True для ВСЕХ
  модулей → полный доступ (канбан/tasks_pro и т.п.) без настройки биллинга.
  Тест использует его как пользователя сессии.
- **Изоляция**: harness делает `chdir` во временный каталог ДО импорта
  `web.*`/`database`/`tenant_manager` (пути `data/...` и секреты читаются на
  импорте). Свежие SQLite-БД, в рабочие `data/` ничего не пишется.
- **Аутентификация**: `create_session_token(tg, name, org_db, role="owner")`,
  кука `web_session`. Требует `BOT_TOKEN` в env на момент импорта `web.auth`.
- **Шторка «Ещё» — мобильная фича** (на десктопе `sm:hidden`). Проверять на
  мобильном вьюпорте (390×844), открывать через
  `document.dispatchEvent(new Event('ds-open-more'))`, кнопка темы = `#ds-dark-toggle`.
- **Канбан quick-move** скрыт до hover (`opacity-0 group-hover`): сначала
  `card.hover()`, затем клик `button[onclick^='moveCard']`; успех = тост
  `#kanban-toast` с текстом «Статус изменён».

## Найденный и исправленный баг
PDF ценника возвращал **500 для товара с кириллическим именем**:
`Content-Disposition: filename="Тестовый.pdf"` нельзя закодировать в latin-1
(Starlette кодирует заголовки latin-1). Фикс в `web/routes/products.py`:
ASCII-fallback `filename=` + RFC 5987 `filename*=UTF-8''<quote(name)>`.
**Правило:** любой `Content-Disposition` с пользовательским именем файла обязан
быть latin-1-safe (ASCII-имя + `filename*`), иначе кириллица роняет ответ.
