# Настройка деплоя: GitHub + Amvera из Replit

## Требования

Перед запуском `deploy.sh` в **Replit Secrets** должны быть три переменные:

| Secret | Назначение |
|---|---|
| `GITHUB_TOKEN` | Personal Access Token GitHub (scope: `Contents → write` или классический `repo`) |
| `AMVERA_USER` | Логин аккаунта Amvera |
| `AMVERA_PASS` | Пароль аккаунта Amvera |

Статус добавления: ✅ настроено 2026-07-27.

## Команды деплоя

```bash
# Полный деплой (GitHub + Amvera) с тестами:
bash deploy.sh "Сообщение коммита"

# Только GitHub (без Amvera):
bash deploy.sh "Сообщение" --no-amvera

# Пропустить браузерные smoke-тесты (экстренный режим):
bash deploy.sh "Сообщение" --skip-tests
```

## Что делает deploy.sh

| Шаг | Действие |
|---|---|
| 0 | Юнит-тесты сборки changelog (`test_build_changelog.py`) |
| 0 | Браузерные smoke-тесты (`test_web_smoke.py`, Playwright/Chromium) |
| 0a | Сборка changelog из фрагментов `web/changelog.d/*.md` |
| 0b | Сборка Tailwind CSS → `static/app.css` (cache-bust `?v=`) |
| 1 | Rsync исходников в `/tmp/github-deploy` и `/tmp/amvera-deploy` |
| 2 | Git commit + push на GitHub (branch `main`) |
| 3 | Git commit + push на Amvera (branch `master`) |
| 3v | Верификация: `git ls-remote` Amvera — remote hash должен совпасть |
| 4 | Обновление `AGENT_HANDOFF.md` (номер сессии, хэши) |

## Gotchas (важно)

- **URL-энкодинг**: `AMVERA_PASS` и `AMVERA_USER` со спецсимволами (`:`, `@`, `/`) обязаны URL-кодироваться перед вставкой в git remote URL. `deploy.sh` делает это через `python3 -c urllib.parse.quote`.
- **Проверка remote hash**: деплой сверяет local HEAD vs remote hash через `git ls-remote`. Если они совпадают — push не нужен (Ловушка 2 из `.agents/memory/amvera-deploy-auth.md`).
- **Tailwind CDN vs build**: standalone-шаблоны (`landing`, `auth`, `errors`) несут свой `<head>` с CDN; все остальные используют собранный `static/app.css`.

## Логи

| Файл | Содержимое |
|---|---|
| `/tmp/changelog_tests.log` | Результат юнит-тестов changelog |
| `/tmp/web_smoke_tests.log` | Результат браузерных smoke-тестов |
| `/tmp/web_smoke_pipinstall.log` | Установка playwright (если потребовалась) |

## Репозитории

- **GitHub**: `https://github.com/pdkiller666/Daily-Sales` (branch `main`)
- **Amvera**: `https://git.msk0.amvera.ru/pdkiller666/dailysalesdeploy` (branch `master`)
