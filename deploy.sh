#!/bin/bash
set -e

# ─── Использование ──────────────────────────────────────────────────────────
# По умолчанию: пуш на GitHub + Amvera напрямую
#   bash deploy.sh
#   bash deploy.sh "Сообщение коммита"
#
# Только GitHub (без прямого пуша на Amvera):
#   bash deploy.sh "Сообщение" --no-amvera

GITHUB_DIR="/tmp/github-deploy"
SOURCE_DIR="/home/runner/workspace"

# ─── Парсим аргументы ────────────────────────────────────────────────────────
WITH_AMVERA=true
SKIP_TESTS=false
COMMIT_MSG=""
for arg in "$@"; do
  if [ "$arg" = "--no-amvera" ]; then
    WITH_AMVERA=false
  elif [ "$arg" = "--skip-tests" ]; then
    SKIP_TESTS=true
  else
    COMMIT_MSG="$arg"
  fi
done
COMMIT_MSG="${COMMIT_MSG:-Обновление $(date '+%Y-%m-%d %H:%M')}"

# ─── Учётные данные Amvera ───────────────────────────────────────────────────
# Значения берутся ТОЛЬКО из Replit Secrets (AMVERA_USER, AMVERA_PASS).
# Захардкоженный fallback намеренно убран — это защищает от деплоя task-агентами.
if [ -z "$AMVERA_USER" ] || [ -z "$AMVERA_PASS" ]; then
  echo "❌ AMVERA_USER или AMVERA_PASS не заданы в Secrets. Деплой прерван."
  exit 1
fi

# ─── Инициализация GitHub-репозитория ───────────────────────────────────────
if [ ! -d "$GITHUB_DIR/.git" ]; then
  mkdir -p "$GITHUB_DIR"
  cd "$GITHUB_DIR"
  git init
  git config user.email "deploy@bot"
  git config user.name "Deploy"
  git remote add origin "https://pdkiller666:${GITHUB_TOKEN}@github.com/pdkiller666/Daily-Sales.git"
fi

if $WITH_AMVERA; then
  echo "=== Деплой: GitHub + Amvera ==="
else
  echo "=== Деплой: только GitHub (--no-amvera) ==="
fi

# ─── Тесты авто-сборки changelog (защитный барьер) ───────────────────────────
# Прогоняем юнит-тесты ДО самой сборки: если кто-то сломал логику в
# scripts/build_changelog.py, деплой упадёт ЗДЕСЬ, до того как испорченный
# changelog попадёт в production. Тесты автономны (без сети/LLM/реальных файлов).
echo "0. Тесты авто-сборки changelog..."
if ( cd "$SOURCE_DIR" && python3 test_build_changelog.py ) > /tmp/changelog_tests.log 2>&1; then
  echo "   ✓ changelog tests OK"
else
  echo "   ❌ Тесты авто-сборки changelog НЕ прошли — деплой остановлен."
  echo "   ── Вывод тестов ──────────────────────────────────────────────"
  cat /tmp/changelog_tests.log
  exit 1
fi

# ─── Браузерные smoke-тесты веб-кабинета (защитный барьер) ────────────────────
# Поднимают реальный веб-кабинет в headless-Chromium с теми же CSP-заголовками,
# что и в проде, и проверяют ключевые кнопки/переходы (тёмная тема, канбан,
# карточка товара, push, PDF ценника). Именно регрессия CSP однажды молча
# сломала эти кнопки — этот барьер ловит подобное ДО деплоя.
# Тест автономен (свежие БД во временном каталоге, ничего не пишет в data/).
# В деплое выставляем WEB_SMOKE_REQUIRE_BROWSER=1 → отсутствие Chromium/playwright
# даёт ЖЁСТКИЙ провал (exit 1), а не тихий SKIP: иначе регрессия логина уехала бы
# незамеченной, если хост вдруг потеряет Chromium. Реальный провал проверки тоже
# останавливает деплой. Экстренный обход: bash deploy.sh "msg" --skip-tests
if $SKIP_TESTS; then
  echo "0. Браузерные smoke-тесты: ⚠️  ПРОПУЩЕНО (--skip-tests)"
else
  echo "0. Браузерные smoke-тесты веб-кабинета..."
  # Чистим зомби-процессы Chromium от предыдущих прерванных запусков.
  pkill -f 'chrome[-]linux' 2>/dev/null || true
  rm -rf /tmp/playwright_chromiumdev_profile-* 2>/dev/null || true
  sleep 1
  if ! ( cd "$SOURCE_DIR" && python3 -c "import playwright" ) >/dev/null 2>&1; then
    # Самовосстановление после пересборки контейнера: тихо доустановить playwright.
    ( cd "$SOURCE_DIR" && pip install -q playwright ) >/tmp/web_smoke_pipinstall.log 2>&1 \
      || echo "   ⚠️  не удалось установить playwright — деплой остановится (SKIP запрещён)"
  fi
  if ( cd "$SOURCE_DIR" && WEB_SMOKE_REQUIRE_BROWSER=1 timeout 60 python3 test_web_smoke.py ) > /tmp/web_smoke_tests.log 2>&1; then
    # Показываем каждую проверку и итоговую строку с подсчётом.
    grep -E "✓|✅|✗|SKIP|⚠️|Провалено|прошли" /tmp/web_smoke_tests.log | sed 's/^/   /' || true
  else
    echo "   ❌ Браузерные smoke-тесты НЕ прошли — деплой остановлен."
    echo "   ── Провалившиеся проверки ────────────────────────────────────"
    grep -E "✗|❌|Провалено" /tmp/web_smoke_tests.log | sed 's/^/   /' || true
    echo "   ── Полный вывод тестов ───────────────────────────────────────"
    cat /tmp/web_smoke_tests.log
    exit 1
  fi
fi

# ─── Авто-сборка changelog (фрагменты + git-история) ─────────────────────────
# Собирает «Что нового» ДО синхронизации, чтобы обновлённый changelog.py и
# поднятая версия уехали в этот же деплой. Источники: ручные фрагменты
# web/changelog.d/*.md И заголовки смерженных коммитов после прошлой сборки
# (маркер web/changelog.d/.last_commit). Запускается в рабочем репозитории —
# именно там доступна богатая по-задачам git-история. Нечего собрать → no-op.
# Ошибка скрипта не должна валить деплой (|| true при set -e).
echo "0. Сборка changelog (фрагменты + git)..."
( cd "$SOURCE_DIR" && python3 scripts/build_changelog.py ) \
  || echo "   ⚠️  build_changelog.py завершился с ошибкой — продолжаю деплой"

# ─── Сборка Tailwind CSS (статический app.css вместо CDN-рантайма) ────────────
# Пересобирает web/static/app.css по актуальным классам в шаблонах и проставляет
# контент-хэш в <link ...?v=> в base.html (cache-bust без бампа SW). Ошибка сборки
# НЕ валит деплой — уедет уже закоммиченный app.css.
echo "0b. Сборка Tailwind CSS (статический app.css)..."
if ( cd "$SOURCE_DIR" && timeout 120 npx --yes tailwindcss@3.4.17 -c tailwind.config.js \
       -i web/static/tailwind.input.css -o web/static/app.css --minify >/dev/null 2>&1 ); then
  CSS_HASH=$(md5sum "$SOURCE_DIR/web/static/app.css" | cut -c1-10)
  # Cache-bust во ВСЕХ шаблонах со ссылкой на app.css (base.html + standalone:
  # landing/auth/errors). Иначе при immutable-кэше standalone-страницы зависнут
  # на старом CSS, пока base-страницы обновятся → визуальное расхождение.
  grep -rl "/static/app.css?v=" "$SOURCE_DIR/web/templates" \
    | xargs sed -i -E "s#/static/app\.css\?v=[a-f0-9]+#/static/app.css?v=${CSS_HASH}#"
  echo "   ✓ app.css собран, cache-bust v=${CSS_HASH} (все шаблоны)"
else
  echo "   ⚠️  Tailwind build не удался — деплой с уже закоммиченным app.css"
fi

# ─── Синхронизация файлов ───────────────────────────────────────────────────
echo "1. Синхронизация файлов..."

python3 - <<PYEOF
import os, shutil, sys

SRC = "/home/runner/workspace"
WITH_AMVERA = $([ "$WITH_AMVERA" = true ] && echo True || echo False)

# .local — только для Replit, не нужен нигде
# AGENT_HANDOFF.md / replit.md → GitHub: да, Amvera: нет

AMVERA_DST = "/tmp/amvera-deploy"
GITHUB_DST  = "/tmp/github-deploy"

EXCLUDE_DIRS = {
    '.git', 'node_modules', '.config', '.local', '.cache',
    '.upm', '.pythonlibs', 'dist', '__pycache__',
    'artifacts', 'attached_assets', 'screenshots',
    'data', 'venv', '.venv',
}

EXCLUDE_FILES = {
    '.replit', 'replit.nix',
    '.env', '.env.local', '.env.production',
}

AMVERA_ONLY_EXCLUDE_FILES = {
    'AGENT_HANDOFF.md',
    'replit.md',
    'PROJECT_MAP.md',
    'README.md',
    'UI_MAP.md',
    '.gitignore',
    '.env.example',
    'deploy.sh',
}

AMVERA_ONLY_EXCLUDE_DIRS = {
    '.github',
    'web/changelog.d',
}

EXCLUDE_SUBPATHS = {'data/.env'}
EXCLUDE_EXT = {'.log', '.pyc', '.pyo', '.db', '.db-shm', '.db-wal', '.pkl'}

def should_exclude(rel_path, amvera=False):
    norm = rel_path.replace('\\\\', '/')
    if norm in EXCLUDE_SUBPATHS:
        return True
    parts = norm.split('/')
    for i in range(len(parts)):
        segment = '/'.join(parts[:i+1])
        if parts[i] in EXCLUDE_DIRS or segment in EXCLUDE_DIRS:
            return True
        if amvera and (parts[i] in AMVERA_ONLY_EXCLUDE_DIRS or segment in AMVERA_ONLY_EXCLUDE_DIRS):
            return True
    name = parts[-1]
    if name in EXCLUDE_FILES:
        return True
    if amvera and name in AMVERA_ONLY_EXCLUDE_FILES:
        return True
    ext = os.path.splitext(name)[1]
    if ext in EXCLUDE_EXT:
        return True
    return False

def clean_dst(dst_root):
    """Удаляем из dst_root всё, кроме .git — чтобы stale-файлы не всплывали в коммите."""
    if not os.path.isdir(dst_root):
        return
    for item in os.listdir(dst_root):
        if item == '.git':
            continue
        item_path = os.path.join(dst_root, item)
        if os.path.isdir(item_path):
            shutil.rmtree(item_path)
        else:
            os.remove(item_path)

def sync_to(dst_root, amvera=False):
    clean_dst(dst_root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(SRC):
        rel_dir = os.path.relpath(dirpath, SRC)
        if rel_dir == '.':
            rel_dir = ''
        dirnames[:] = [
            d for d in dirnames
            if not should_exclude(os.path.join(rel_dir, d) if rel_dir else d, amvera)
        ]
        for filename in filenames:
            rel_file = os.path.join(rel_dir, filename) if rel_dir else filename
            if should_exclude(rel_file, amvera):
                continue
            src_file = os.path.join(dirpath, filename)
            dst_file = os.path.join(dst_root, rel_file)
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            shutil.copy2(src_file, dst_file)
            count += 1
    return count

github_count = sync_to(GITHUB_DST, amvera=False)
print(f"  GitHub: {github_count} файлов (с AGENT_HANDOFF.md, без .local, без data/)")

if WITH_AMVERA:
    if not os.path.isdir(AMVERA_DST + "/.git"):
        import subprocess
        os.makedirs(AMVERA_DST, exist_ok=True)
        subprocess.run(["git", "init"], cwd=AMVERA_DST, check=True)
        subprocess.run(["git", "config", "user.email", "deploy@bot"], cwd=AMVERA_DST, check=True)
        subprocess.run(["git", "config", "user.name", "Deploy"], cwd=AMVERA_DST, check=True)
        subprocess.run(["git", "remote", "add", "amvera", "https://git.msk0.amvera.ru/pdkiller666/dailysalesdeploy"], cwd=AMVERA_DST, check=True)
    amvera_count = sync_to(AMVERA_DST, amvera=True)
    print(f"  Amvera: {amvera_count} файлов (без AGENT_HANDOFF.md, .local, data/)")
PYEOF

# ─── Деплой на GitHub ───────────────────────────────────────────────────────
echo "2. Отправка на GitHub..."
cd "$GITHUB_DIR"
# Сначала получаем актуальную историю из GitHub — чтобы новые коммиты
# строились поверх существующей истории, а не создавали orphan-цепочку.
git remote set-url origin "https://pdkiller666:${GITHUB_TOKEN}@github.com/pdkiller666/Daily-Sales.git"
git fetch origin main 2>/dev/null || git fetch origin master 2>/dev/null || true
git reset --soft origin/main 2>/dev/null || git reset --soft origin/master 2>/dev/null || true
git add -A
if git diff --cached --quiet; then
  echo "   GitHub: нет изменений."
else
  git commit -m "$COMMIT_MSG"
  git push origin HEAD:main 2>/dev/null || git push origin HEAD:master
  echo "   GitHub: ✅ отправлено!"
fi

# ─── Деплой на Amvera ───────────────────────────────────────────────────────
if $WITH_AMVERA; then
  echo "3. Отправка на Amvera..."
  DEPLOY_DIR="/tmp/amvera-deploy"
  cd "$DEPLOY_DIR"

  # Явно удаляем .md файлы которые не должны быть на Amvera
  rm -f AGENT_HANDOFF.md PROJECT_MAP.md README.md replit.md

  # Подтягиваем историю с Amvera-remote чтобы наш новый коммит строился поверх
  # предыдущего — иначе после --force Amvera не может найти старый хэш при сборке.
  # URL-энкодинг учётных данных ОБЯЗАТЕЛЕН: пароль/логин могут содержать
  # спецсимволы (`:`, `@`, `/`, `#`), которые ломают разбор URL — git примет
  # текст после `:` за номер порта («Port number was not a decimal number»)
  # или оборвёт строку на `@`. Кодируем оба компонента перед вставкой в URL.
  AMVERA_REMOTE_URL=$(python3 -c "
import os, urllib.parse
u = urllib.parse.quote(os.environ['AMVERA_USER'], safe='')
p = urllib.parse.quote(os.environ['AMVERA_PASS'], safe='')
print(f'https://{u}:{p}@git.msk0.amvera.ru/pdkiller666/dailysalesdeploy')
")
  git remote set-url amvera "$AMVERA_REMOTE_URL"
  git fetch amvera master 2>/dev/null || true
  # Если remote/master существует — выставляем HEAD поверх него (soft: файлы в stage остаются)
  if git rev-parse amvera/master >/dev/null 2>&1; then
    git reset --soft amvera/master
  fi

  git add -A

  REMOTE_HASH=$(git ls-remote amvera refs/heads/master 2>/dev/null | awk '{print $1}')
  HAS_LOCAL_CHANGES=false
  git diff --cached --quiet || HAS_LOCAL_CHANGES=true
  # Если нет закоммиченных данных (первый запуск) — тоже считаем как изменение
  git rev-parse HEAD >/dev/null 2>&1 || HAS_LOCAL_CHANGES=true

  # Коммитим только при наличии новых изменений в файлах.
  if $HAS_LOCAL_CHANGES; then
    git commit -m "$COMMIT_MSG"
  fi

  # Решаем, нужен ли push. КРИТИЧНО: сравниваем локальный HEAD с REMOTE-хэшем,
  # а НЕ только с локальным состоянием. Иначе сценарий «прошлый push упал по
  # auth» оставляет коммит локально → следующий запуск видит «файлы == HEAD» и
  # молча рапортует «нет изменений», хотя на Amvera ничего не уехало (был
  # реальный инцидент с потерей фикса на проде). Если REMOTE_HASH пуст (нет
  # доступа/первый деплой) — тоже пушим.
  LOCAL_HASH=$(git rev-parse HEAD 2>/dev/null || echo "")
  NEED_PUSH=false
  if $HAS_LOCAL_CHANGES; then NEED_PUSH=true; fi
  if [ -n "$LOCAL_HASH" ] && [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then NEED_PUSH=true; fi

  if $NEED_PUSH; then
    # Пушим без --force: коммит строится поверх предыдущего → история цела →
    # Amvera всегда находит предыдущий хэш и не делает лишний full-clone.
    git push amvera HEAD:master
    echo "   Amvera: ✅ отправлено!"
    # Верификация: проверяем что Amvera remote видит наш хэш
    LOCAL_HASH=$(git rev-parse HEAD)
    REMOTE_HASH=$(git ls-remote amvera refs/heads/master 2>/dev/null | awk '{print $1}')
    if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
      echo "   Amvera verify: ✅ remote hash совпадает ($REMOTE_HASH)"
    else
      echo "   Amvera verify: ⚠️  расхождение! local=$LOCAL_HASH remote=${REMOTE_HASH:-не найден}"
    fi
  else
    echo "   Amvera: нет изменений (local HEAD == remote)."
  fi
  echo ""
  echo "=== Готово! GitHub + Amvera обновлены ==="
else
  echo ""
  echo "=== Готово! Только GitHub обновлён ==="
fi

# ─── Авто-обновление AGENT_HANDOFF.md ───────────────────────────────────────
echo "4. Обновление AGENT_HANDOFF.md..."

HANDOFF="$SOURCE_DIR/AGENT_HANDOFF.md"
TODAY=$(date '+%Y-%m-%d')

# Хэши: GitHub из github-deploy, Amvera из amvera-deploy (если деплоился)
GH_HASH=$(cd "$GITHUB_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
if $WITH_AMVERA && [ -d "/tmp/amvera-deploy/.git" ]; then
  AV_HASH=$(cd "/tmp/amvera-deploy" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
else
  # Если Amvera не деплоился — берём предыдущий хэш из файла
  AV_HASH=$(grep -oP "Amvera \`\K[0-9a-f]+" "$HANDOFF" | head -1 || echo "unknown")
fi

# Текущий номер сессии из файла → +1
CUR_SESSION=$(grep -oP "сессия \K\d+" "$HANDOFF" | head -1 || echo "43")
NEW_SESSION=$((CUR_SESSION + 1))

# Патчим строку «Последнее обновление»
sed -i "s/> Последнее обновление: .*/> Последнее обновление: $TODAY (сессия $NEW_SESSION)/" "$HANDOFF"

# Патчим строку «Последний деплой» (заменяем всю строку целиком)
sed -i "s|^\*\*Последний деплой:\*\*.*|**Последний деплой:** GitHub \`$GH_HASH\` · Amvera \`$AV_HASH\` ($TODAY, сессия $NEW_SESSION). Оба хэша верифицированы через \`git ls-remote\`.|" "$HANDOFF"

echo "   AGENT_HANDOFF.md → сессия $NEW_SESSION · GitHub $GH_HASH · Amvera $AV_HASH"

# Пушим обновлённый AGENT_HANDOFF.md на GitHub
cd "$GITHUB_DIR"
cp "$HANDOFF" "$GITHUB_DIR/AGENT_HANDOFF.md"
git add AGENT_HANDOFF.md
if git diff --cached --quiet; then
  echo "   AGENT_HANDOFF.md: без изменений, пуш не нужен."
else
  git commit -m "auto: AGENT_HANDOFF сессия $NEW_SESSION ($TODAY)"
  git push origin HEAD:main 2>/dev/null || git push origin HEAD:master
  echo "   AGENT_HANDOFF.md: ✅ запушен на GitHub"
fi
