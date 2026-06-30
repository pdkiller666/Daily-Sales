---
name: Deploy requires system Chromium
description: deploy.sh hard-fails without system Chromium because smoke tests run with WEB_SMOKE_REQUIRE_BROWSER=1
---

# Деплой жёстко требует системный Chromium

`deploy.sh` (шаг 0) запускает `test_web_smoke.py` с `WEB_SMOKE_REQUIRE_BROWSER=1`.
В этом режиме отсутствие системного Chromium или playwright → **жёсткий fail (exit 1)**,
деплой останавливается. Раньше был тихий SKIP (exit 0) — его убрали намеренно, чтобы
регрессия логина/2FA не уехала незамеченной, если хост потеряет браузер.

**Why:** браузерные smoke-проверки (вход, 2FA, Канбан, POS и т.д.) — единственный
защитный барьер перед пушем в прод; тихий SKIP делал зелёный деплой ложно-успешным.

**How to apply:** на свежем/сброшенном окружении ПЕРЕД первым `bash deploy.sh` поставить
браузер: в code_execution вызвать `installSystemDependencies(['chromium'])`. Локальные
dev-прогоны `python3 test_web_smoke.py` БЕЗ флага сохраняют graceful SKIP (opt-out).
Постоянное решение (всегда иметь браузер на хосте деплоя) — предмет отдельной задачи.
