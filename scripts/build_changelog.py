#!/usr/bin/env python3
"""Сборка раздела «Что нового» из фрагментов.

Читает фрагменты из web/changelog.d/*.md, формирует новую запись версии в
web/changelog.py, поднимает CURRENT_VERSION, (опционально) полирует формулировки
через web.ai_utils.ask_llm и удаляет использованные фрагменты.

Гарантии:
- пустая папка фрагментов → no-op (деплой не падает);
- AI-полировка опциональна и НИКОГДА не блокирует сборку (нет ключа / ошибка /
  пустой ответ → берём исходные строки фрагментов);
- идемпотентность: после сборки фрагменты удалены, повторный запуск = no-op.

CLI:
    python3 scripts/build_changelog.py            # minor-бамп (по умолчанию)
    python3 scripts/build_changelog.py --major
    python3 scripts/build_changelog.py --patch
    python3 scripts/build_changelog.py --no-ai    # без LLM-полировки
    python3 scripts/build_changelog.py --dry-run  # показать, не записывать
"""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG_PATH = os.path.join(ROOT, "web", "changelog.py")
FRAGMENTS_DIR = os.path.join(ROOT, "web", "changelog.d")

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
    parser = argparse.ArgumentParser(description="Собрать changelog из фрагментов")
    level = parser.add_mutually_exclusive_group()
    level.add_argument("--major", action="store_true", help="major-бамп версии")
    level.add_argument("--minor", action="store_true", help="minor-бамп версии (по умолчанию)")
    level.add_argument("--patch", action="store_true", help="patch-бамп версии")
    parser.add_argument("--no-ai", action="store_true", help="не полировать через LLM")
    parser.add_argument("--dry-run", action="store_true", help="показать результат без записи")
    args = parser.parse_args()

    bullets, used_files = _read_fragments()
    if not bullets:
        print("changelog: фрагментов нет — нечего собирать (no-op).")
        # Подчистим возможные пустые фрагменты, если не dry-run
        if not args.dry_run:
            for path in used_files:
                try:
                    os.remove(path)
                except OSError:
                    pass
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
        print("(--dry-run: файл не изменён, фрагменты не удалены)")
        return 0

    with open(CHANGELOG_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)
    for path in used_files:
        try:
            os.remove(path)
        except OSError:
            pass
    print(f"changelog: записано в {os.path.relpath(CHANGELOG_PATH, ROOT)}, "
          f"удалено фрагментов: {len(used_files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
