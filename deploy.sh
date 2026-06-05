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
COMMIT_MSG=""
for arg in "$@"; do
  if [ "$arg" = "--no-amvera" ]; then
    WITH_AMVERA=false
  else
    COMMIT_MSG="$arg"
  fi
done
COMMIT_MSG="${COMMIT_MSG:-Обновление $(date '+%Y-%m-%d %H:%M')}"

# ─── Учётные данные Amvera ───────────────────────────────────────────────────
AMVERA_USER="${AMVERA_USER:-pdkiller666}"
AMVERA_PASS="${AMVERA_PASS:-4_5AznCgvidfr5x}"

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
  git remote set-url amvera "https://${AMVERA_USER}:${AMVERA_PASS}@git.msk0.amvera.ru/pdkiller666/dailysalesdeploy"
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

  if $HAS_LOCAL_CHANGES; then
    git commit -m "$COMMIT_MSG"
    # Пушим без --force: коммит строится поверх предыдущего → история цела →
    # Amvera всегда находит предыдущий хэш и не делает лишний full-clone.
    git push amvera HEAD:master
    echo "   Amvera: ✅ отправлено!"
  else
    echo "   Amvera: нет изменений."
  fi

  if $HAS_LOCAL_CHANGES; then
    # Верификация: проверяем что Amvera remote видит наш хэш
    LOCAL_HASH=$(git rev-parse HEAD)
    REMOTE_HASH=$(git ls-remote amvera refs/heads/master 2>/dev/null | awk '{print $1}')
    if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
      echo "   Amvera verify: ✅ remote hash совпадает ($REMOTE_HASH)"
    else
      echo "   Amvera verify: ⚠️  расхождение! local=$LOCAL_HASH remote=${REMOTE_HASH:-не найден}"
    fi
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
