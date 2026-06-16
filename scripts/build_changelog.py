#!/usr/bin/env python3
"""Сборка раздела «Что нового» из фрагментов И из истории git.

Два источника новостей объединяются в одну новую запись версии в web/changelog.py:

1. Ручные фрагменты web/changelog.d/*.md — точные формулировки «от владельца»
   (одна значимая строка = один пункт). Имеют приоритет (идут первыми).
2. Авто-вывод из git: заголовки коммитов, смерженных ПОСЛЕ прошлой сборки changelog
   (маркер web/changelog.d/.last_commit). Служебные/шумные коммиты отфильтровываются,
   префиксы вида «Task #NN:», «feat:», «fix:» срезаются. Так каждый деплой сам
   публикует смерженные фичи — без действий со стороны агентов.

После сборки версия поднимается, формулировки (опционально) полируются через
web.ai_utils.ask_llm, фрагменты удаляются, маркер сдвигается на текущий HEAD.

Гарантии:
- нет ни фрагментов, ни новых пользовательских коммитов → no-op (деплой не падает);
- первый запуск без маркера → только фиксируем baseline=HEAD, историю не вываливаем;
- AI-полировка опциональна и НИКОГДА не блокирует сборку (нет ключа / ошибка /
  пустой ответ → берём исходные строки);
- идемпотентность: коммит учитывается один раз (маркер), фрагменты удаляются.

CLI:
    python3 scripts/build_changelog.py            # minor-бамп (по умолчанию)
    python3 scripts/build_changelog.py --major
    python3 scripts/build_changelog.py --patch
    python3 scripts/build_changelog.py --no-ai    # без LLM-полировки
    python3 scripts/build_changelog.py --no-git   # только ручные фрагменты
    python3 scripts/build_changelog.py --dry-run  # показать, не записывать
"""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG_PATH = os.path.join(ROOT, "web", "changelog.py")
FRAGMENTS_DIR = os.path.join(ROOT, "web", "changelog.d")
MARKER_PATH = os.path.join(FRAGMENTS_DIR, ".last_commit")

_RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _ru_date(dt: datetime) -> str:
    return f"{dt.day} {_RU_MONTHS[dt.month]} {dt.year}"


def _load_changelog():
    """Импортирует web/changelog.py как данные (файл — чистые константы)."""
    spec = importlib.util.spec_from_file_location("_changelog_data", CHANGELOG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CURRENT_VERSION, list(mod.ENTRIES)


def _bump_version(version: str, level: str) -> str:
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        major, minor, patch = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        # Нестандартная версия — безопасно начинаем с 1.0.0
        major, minor, patch = 1, 0, 0
    if level == "major":
        major, minor, patch = major + 1, 0, 0
    elif level == "patch":
        patch += 1
    else:  # minor
        minor, patch = minor + 1, 0
    return f"{major}.{minor}.{patch}"


def _read_fragments():
    """Возвращает (bullets, used_files). Каждая значимая строка фрагмента = пункт."""
    bullets: list[str] = []
    used_files: list[str] = []
    if not os.path.isdir(FRAGMENTS_DIR):
        return bullets, used_files
    for name in sorted(os.listdir(FRAGMENTS_DIR)):
        if name in ("README.md", ".gitkeep") or name.startswith("."):
            continue
        if not name.endswith(".md"):
            continue
        path = os.path.join(FRAGMENTS_DIR, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            file_has_bullet = False
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                bullets.append(line)
                file_has_bullet = True
        if file_has_bullet:
            used_files.append(path)
        else:
            # Пустой/только-комментарии фрагмент — всё равно убираем, чтобы не копился
            used_files.append(path)
    return bullets, used_files


# ─── Авто-вывод новостей из git-истории ─────────────────────────────────────

# Коммиты-«шум»: служебные, инфраструктурные, сам changelog/деплой, чекпоинты.
_NOISE_RE = re.compile(
    r"("
    r"^auto:|"
    r"^merge\b|"
    r"^(?:chore|docs|doc|test|tests|refactor|style|ci|build)\b|"
    r"agent_handoff|"
    r"changelog|"
    r"что нового|what'?s new|whats new|"
    r"^transition|"            # чекпоинты Plan↔Build «Transitioned ...»
    r"^обновление \d{4}-|"     # дефолтный коммит deploy.sh «Обновление 2026-..»
    r"update deployment information|"
    r"\bdeploy\b|деплой"
    r")",
    re.IGNORECASE,
)

# Срезаемые ведущие префиксы: «Task #49:», «#49 -», conventional commits.
_TASK_PREFIX_RE = re.compile(r"^\s*(?:task\s*)?#\d+\s*[:\-–—.)]*\s*", re.IGNORECASE)
_CC_PREFIX_RE = re.compile(
    r"^\s*(?:feat|fix|perf|refactor|chore|docs|style|test|build|ci)(?:\([^)]*\))?!?:\s*",
    re.IGNORECASE,
)


def _run_git(args):
    """(returncode, stdout). Любая ошибка/таймаут → (1, "")."""
    try:
        r = subprocess.run(
            ["git", "--no-optional-locks", "-C", ROOT] + args,
            capture_output=True, text=True, timeout=20,
        )
        return r.returncode, r.stdout
    except Exception:  # noqa: BLE001
        return 1, ""


def _git_head():
    code, out = _run_git(["rev-parse", "HEAD"])
    return out.strip() if code == 0 and out.strip() else None


def _commit_exists(sha: str) -> bool:
    code, _ = _run_git(["cat-file", "-e", f"{sha}^{{commit}}"])
    return code == 0


def _read_marker():
    try:
        with open(MARKER_PATH, encoding="utf-8") as fh:
            v = fh.read().strip()
            return v or None
    except OSError:
        return None


def _write_marker(sha: str) -> None:
    try:
        os.makedirs(FRAGMENTS_DIR, exist_ok=True)
        with open(MARKER_PATH, "w", encoding="utf-8") as fh:
            fh.write(sha + "\n")
    except OSError:
        pass


def _clean_subject(subj: str) -> str:
    s = subj.strip()
    s = _TASK_PREFIX_RE.sub("", s)
    s = _CC_PREFIX_RE.sub("", s)
    return s.strip()


def _read_commit_bullets():
    """(bullets, new_head). Выводит пункты из коммитов после маркера.

    Возвращает new_head=None, если git недоступен (тогда маркер не двигаем).
    На первом запуске (нет валидного маркера) пунктов не вываливаем — только
    фиксируем baseline=HEAD, чтобы вся история не попала в одну запись.
    """
    head = _git_head()
    if not head:
        return [], None

    last = _read_marker()
    if last and not _commit_exists(last):
        print(f"   git: маркер {last[:9]} недостижим — сбрасываю baseline на HEAD")
        last = None
    if not last:
        # baseline: историю не вываливаем, только зафиксируем точку отсчёта
        return [], head
    if last == head:
        return [], head

    code, out = _run_git(["log", "--no-merges", "--format=%s", f"{last}..HEAD"])
    if code != 0:
        # git log упал (timeout/ошибка) — НЕ двигаем маркер (new_head=None),
        # иначе диапазон коммитов будет навсегда пропущен. Повторим на след. деплое.
        print("   ⚠️  git log не удался — маркер не двигаю, повтор на следующем деплое")
        return [], None

    bullets: list[str] = []
    for line in out.splitlines():
        subj = line.strip()
        if not subj or _NOISE_RE.search(subj):
            continue
        cleaned = _clean_subject(subj)
        if cleaned:
            bullets.append(cleaned)
    # git log идёт новые→старые; в новостях логичнее старые→новые
    bullets.reverse()
    return bullets, head


def _dedupe(bullets):
    seen = set()
    out = []
    for b in bullets:
        key = re.sub(r"\s+", " ", b).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def _polish_with_ai(bullets: list[str]) -> list[str]:
    """Полировка формулировок через ask_llm. При любой проблеме → исходные строки."""
    try:
        sys.path.insert(0, ROOT)
        from web.ai_utils import ask_llm, is_configured  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"   AI: модуль ai_utils недоступен ({exc}) — без полировки")
        return bullets
    try:
        if not is_configured():
            print("   AI: ключи LLM не заданы — без полировки")
            return bullets
    except Exception:  # noqa: BLE001
        return bullets

    system = (
        "Ты — редактор новостей продукта для владельцев розничных магазинов. "
        "Тебе дают черновые заметки об изменениях. Перепиши каждую как короткий "
        "понятный пункт новости на русском языке. Правила: один пункт на строку, "
        "ровно столько же строк, сколько на входе, порядок сохраняй, в начале каждой "
        "строки подходящий эмодзи, без markdown, без нумерации, без вводных фраз. "
        "Пиши с точки зрения пользователя (что он теперь может), а не разработчика."
    )
    numbered = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(bullets))
    prompt = f"Заметки об изменениях ({len(bullets)} шт.):\n{numbered}"

    try:
        result = asyncio.run(ask_llm(prompt, system=system, max_tokens=800, temperature=0.3))
    except Exception as exc:  # noqa: BLE001
        print(f"   AI: ошибка вызова ({exc}) — без полировки")
        return bullets

    if not result:
        print("   AI: пустой ответ — без полировки")
        return bullets

    polished = []
    for line in result.splitlines():
        s = line.strip()
        if not s:
            continue
        # срезаем ведущую нумерацию/маркеры: "1. ", "12) ", "- ", "* "
        s = re.sub(r"^\s*(?:\d+[.)]|[-*])\s+", "", s)
        if s:
            polished.append(s)

    if not polished:
        print("   AI: не удалось распарсить ответ — без полировки")
        return bullets
    # Строгий контракт: LLM обязан вернуть ровно столько же пунктов, сколько на входе.
    # Иначе есть риск молча потерять/склеить новости → откатываемся к исходным строкам.
    if len(polished) != len(bullets):
        print(f"   AI: число пунктов изменилось ({len(bullets)} → {len(polished)}) "
              "— игнорирую полировку, беру исходные строки")
        return bullets
    print(f"   AI: формулировки отполированы ({len(polished)} пунктов)")
    return polished


def _serialize(version: str, entries: list) -> str:
    out = ["CURRENT_VERSION = " + json.dumps(version, ensure_ascii=False), "", "ENTRIES = ["]
    for entry in entries:
        out.append("    {")
        out.append('        "version": ' + json.dumps(entry["version"], ensure_ascii=False) + ",")
        out.append('        "date": ' + json.dumps(entry["date"], ensure_ascii=False) + ",")
        out.append('        "items": [')
        for item in entry["items"]:
            out.append("            " + json.dumps(item, ensure_ascii=False) + ",")
        out.append("        ],")
        out.append("    },")
    out.append("]")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать changelog из фрагментов и git")
    level = parser.add_mutually_exclusive_group()
    level.add_argument("--major", action="store_true", help="major-бамп версии")
    level.add_argument("--minor", action="store_true", help="minor-бамп версии (по умолчанию)")
    level.add_argument("--patch", action="store_true", help="patch-бамп версии")
    parser.add_argument("--no-ai", action="store_true", help="не полировать через LLM")
    parser.add_argument("--no-git", action="store_true", help="не выводить новости из git")
    parser.add_argument("--dry-run", action="store_true", help="показать результат без записи")
    args = parser.parse_args()

    frag_bullets, used_files = _read_fragments()

    if args.no_git:
        commit_bullets, new_head = [], _git_head()
    else:
        commit_bullets, new_head = _read_commit_bullets()
        if commit_bullets:
            print(f"   git: найдено пользовательских коммитов: {len(commit_bullets)}")

    # Ручные фрагменты приоритетны (идут первыми), затем авто из коммитов; дедуп.
    bullets = _dedupe(list(frag_bullets) + list(commit_bullets))

    def _cleanup_fragments():
        if args.dry_run:
            return
        for path in used_files:
            try:
                os.remove(path)
            except OSError:
                pass

    def _advance_marker():
        if not args.dry_run and new_head:
            _write_marker(new_head)

    if not bullets:
        print("changelog: ни фрагментов, ни новых коммитов — нечего собирать (no-op).")
        _cleanup_fragments()
        _advance_marker()  # фиксируем baseline/сдвигаем точку отсчёта
        return 0

    current_version, entries = _load_changelog()

    bump_level = "major" if args.major else "patch" if args.patch else "minor"
    new_version = _bump_version(current_version, bump_level)

    if not args.no_ai:
        bullets = _polish_with_ai(bullets)

    # Защита: после AI-стадии пунктов не должно стать ноль (иначе пустая запись).
    if not bullets:
        print("changelog: после обработки не осталось пунктов — пропускаю (no-op).")
        return 0

    new_entry = {
        "version": new_version,
        "date": _ru_date(datetime.now()),
        "items": bullets,
    }
    entries = [new_entry] + entries
    content = _serialize(new_version, entries)

    print(f"changelog: {current_version} → {new_version} · {new_entry['date']} · {len(bullets)} пунктов")
    for b in bullets:
        print(f"   • {b}")

    if args.dry_run:
        print("(--dry-run: файл не изменён, фрагменты и маркер не тронуты)")
        return 0

    with open(CHANGELOG_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)
    _cleanup_fragments()
    _advance_marker()
    print(f"changelog: записано в {os.path.relpath(CHANGELOG_PATH, ROOT)}, "
          f"удалено фрагментов: {len(used_files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
