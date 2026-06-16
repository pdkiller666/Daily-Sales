"""Юнит-тесты для scripts/build_changelog.py.

Запуск: python3 test_build_changelog.py
Тесты не требуют сети, LLM-ключей и не трогают реальные файлы проекта.
"""
import importlib
import os
import sys
import tempfile
import textwrap
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

# ── Загрузка модуля по пути (не пакет) ─────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "scripts", "build_changelog.py")

spec = importlib.util.spec_from_file_location("build_changelog", _SCRIPT)
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)


# ── Хелперы ─────────────────────────────────────────────────────────────────

def _make_changelog(tmpdir: str, version: str = "1.0.0") -> str:
    """Записывает минимальный changelog.py в tmpdir и возвращает путь."""
    path = os.path.join(tmpdir, "changelog.py")
    content = textwrap.dedent(f"""\
        CURRENT_VERSION = "{version}"

        ENTRIES = [
            {{
                "version": "{version}",
                "date": "1 января 2026",
                "items": ["Первый релиз"],
            }},
        ]
    """)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _make_fragments_dir(tmpdir: str) -> str:
    """Создаёт web/changelog.d/ в tmpdir и возвращает путь."""
    d = os.path.join(tmpdir, "web", "changelog.d")
    os.makedirs(d, exist_ok=True)
    return d


def _add_fragment(frags_dir: str, name: str, content: str) -> str:
    path = os.path.join(frags_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _read_marker(frags_dir: str):
    p = os.path.join(frags_dir, ".last_commit")
    try:
        with open(p, encoding="utf-8") as fh:
            v = fh.read().strip()
            return v or None
    except OSError:
        return None


def _write_marker(frags_dir: str, sha: str):
    p = os.path.join(frags_dir, ".last_commit")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(sha + "\n")


class _BaseTest(unittest.TestCase):
    """Базовый класс: создаёт tmpdir и патчит пути модуля."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._frags_dir = _make_fragments_dir(self._tmpdir)
        self._changelog_path = _make_changelog(
            os.path.join(self._tmpdir, "web"), version="1.0.0"
        )
        os.makedirs(os.path.join(self._tmpdir, "web"), exist_ok=True)
        # Перезаписываем changelog.py
        self._changelog_path = _make_changelog(
            self._tmpdir, version="1.0.0"
        )
        # Перемещаем в web/
        web_changelog = os.path.join(self._tmpdir, "web", "changelog.py")
        import shutil
        shutil.copy(self._changelog_path, web_changelog)
        self._changelog_path = web_changelog

        # Патчим пути модуля
        self._patches = [
            patch.object(bc, "CHANGELOG_PATH", self._changelog_path),
            patch.object(bc, "FRAGMENTS_DIR", self._frags_dir),
            patch.object(bc, "MARKER_PATH",
                         os.path.join(self._frags_dir, ".last_commit")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _read_changelog(self):
        """Загружает changelog.py из tmpdir."""
        spec2 = importlib.util.spec_from_file_location(
            "_tc_data", self._changelog_path
        )
        mod = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(mod)
        return mod.CURRENT_VERSION, list(mod.ENTRIES)

    def _run_main(self, argv=None):
        """Вызывает main() с подменённым sys.argv. Возвращает код выхода."""
        old_argv = sys.argv
        sys.argv = ["build_changelog.py"] + (argv or [])
        try:
            return bc.main()
        finally:
            sys.argv = old_argv


# ════════════════════════════════════════════════════════════════════════════
# 1. Первый запуск без маркера → только baseline=HEAD, новой записи нет
# ════════════════════════════════════════════════════════════════════════════

class TestFirstRunNoMarker(_BaseTest):
    def test_no_new_entry_and_marker_written(self):
        HEAD = "aabbccdd" * 5

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            return 1, ""

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        # Версия не должна смениться, новой записи нет
        version, entries = self._read_changelog()
        self.assertEqual(version, "1.0.0")
        self.assertEqual(len(entries), 1)
        # Маркер должен быть зафиксирован на HEAD
        marker = _read_marker(self._frags_dir)
        self.assertEqual(marker, HEAD)


# ════════════════════════════════════════════════════════════════════════════
# 2. Маркер == HEAD → полный no-op
# ════════════════════════════════════════════════════════════════════════════

class TestMarkerEqualsHead(_BaseTest):
    def test_noop_when_marker_is_head(self):
        HEAD = "deadbeef" * 5
        _write_marker(self._frags_dir, HEAD)

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            if args[0] == "cat-file":
                return 0, ""
            return 1, ""

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        version, entries = self._read_changelog()
        self.assertEqual(version, "1.0.0")
        self.assertEqual(len(entries), 1)
        # Маркер остался тем же
        self.assertEqual(_read_marker(self._frags_dir), HEAD)


# ════════════════════════════════════════════════════════════════════════════
# 3. Маркер указывает на несуществующий коммит → сброс baseline без падения
# ════════════════════════════════════════════════════════════════════════════

class TestMarkerMissingCommit(_BaseTest):
    def test_reset_baseline_on_missing_commit(self):
        OLD_SHA = "00000000" * 5
        HEAD = "11111111" * 5
        _write_marker(self._frags_dir, OLD_SHA)

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            if args[0] == "cat-file":
                # несуществующий коммит → non-zero
                return 1, ""
            return 1, ""

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        # Версия не изменилась (нет пунктов — no-op)
        version, entries = self._read_changelog()
        self.assertEqual(version, "1.0.0")
        self.assertEqual(len(entries), 1)
        # Маркер должен быть обновлён до HEAD
        self.assertEqual(_read_marker(self._frags_dir), HEAD)


# ════════════════════════════════════════════════════════════════════════════
# 4. git недоступен (_git_head() → None) → no-op, маркер не двигается
# ════════════════════════════════════════════════════════════════════════════

class TestGitUnavailable(_BaseTest):
    def test_noop_and_marker_not_moved_when_git_fails(self):
        OLD_SHA = "cafebabe" * 5
        _write_marker(self._frags_dir, OLD_SHA)

        def fake_git(args):
            return 1, ""  # git полностью недоступен

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        version, _ = self._read_changelog()
        self.assertEqual(version, "1.0.0")
        # Маркер не должен двигаться
        self.assertEqual(_read_marker(self._frags_dir), OLD_SHA)


# ════════════════════════════════════════════════════════════════════════════
# 4b. git log падает (timeout/ошибка) при валидном marker..HEAD
#     → НЕ двигаем маркер, иначе диапазон коммитов потеряется навсегда
# ════════════════════════════════════════════════════════════════════════════

class TestGitLogFails(_BaseTest):
    def test_marker_not_moved_when_git_log_fails(self):
        OLD_SHA = "abcdef01" * 5
        HEAD = "12345678" * 5
        _write_marker(self._frags_dir, OLD_SHA)

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            if args[0] == "cat-file":
                return 0, ""          # маркерный коммит существует
            if args[0] == "log":
                return 1, ""          # git log упал
            return 1, ""

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        # Версия не изменилась — пунктов нет
        version, entries = self._read_changelog()
        self.assertEqual(version, "1.0.0")
        self.assertEqual(len(entries), 1)
        # КРИТИЧНО: маркер НЕ должен сдвинуться (повтор на следующем деплое)
        self.assertEqual(_read_marker(self._frags_dir), OLD_SHA)


# ════════════════════════════════════════════════════════════════════════════
# 5. Нормальный диапазон marker..HEAD
#    - шум отфильтрован
#    - префиксы срезаны
#    - порядок старые→новые
#    - дедуп работает
# ════════════════════════════════════════════════════════════════════════════

class TestNormalCommitRange(_BaseTest):
    def test_noise_filtered_prefixes_stripped_order_deduped(self):
        MARKER = "aaaaaaaa" * 5
        HEAD = "bbbbbbbb" * 5
        _write_marker(self._frags_dir, MARKER)

        # git log выдаёт от нового к старому; шумные и дублирующие включены
        GIT_LOG = "\n".join([
            "Task #12: Добавить фильтр по дате",      # срезать Task-префикс
            "feat: новый экспорт в Excel",             # срезать feat-префикс
            "auto: checkpoint",                        # шум → отфильтровать
            "Merge branch 'main'",                     # шум → отфильтровать
            "deploy: push to Amvera",                  # шум → отфильтровать
            "новый экспорт в Excel",                   # дубль (после среза) → убрать
            "chore: bump deps",                        # шум → отфильтровать
            "Улучшить страницу продаж",                # чистый заголовок
        ])

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            if args[0] == "cat-file":
                return 0, ""
            if args[0] == "log":
                return 0, GIT_LOG + "\n"
            return 1, ""

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        version, entries = self._read_changelog()
        # Версия должна подняться
        self.assertEqual(version, "1.1.0")
        new_entry = entries[0]
        items = new_entry["items"]

        # Только 3 уникальных нешумных пункта
        self.assertEqual(len(items), 3, f"Ожидали 3 пункта, получили: {items}")

        # Проверяем порядок: старые→новые (git log reverseд)
        self.assertEqual(items[0], "Улучшить страницу продаж")
        self.assertEqual(items[1], "новый экспорт в Excel")
        self.assertEqual(items[2], "Добавить фильтр по дате")

        # Маркер обновлён
        self.assertEqual(_read_marker(self._frags_dir), HEAD)


# ════════════════════════════════════════════════════════════════════════════
# 6. --dry-run не трогает файлы и маркер
# ════════════════════════════════════════════════════════════════════════════

class TestDryRun(_BaseTest):
    def test_dryrun_does_not_modify_files_or_marker(self):
        MARKER = "cccccccc" * 5
        HEAD = "dddddddd" * 5
        _write_marker(self._frags_dir, MARKER)
        _add_fragment(self._frags_dir, "myfeature.md", "Новая крутая фича\n")

        GIT_LOG = "Фикс авторизации"

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            if args[0] == "cat-file":
                return 0, ""
            if args[0] == "log":
                return 0, GIT_LOG + "\n"
            return 1, ""

        changelog_mtime_before = os.path.getmtime(self._changelog_path)

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai", "--dry-run"])

        self.assertEqual(rc, 0)

        # changelog.py не изменён
        changelog_mtime_after = os.path.getmtime(self._changelog_path)
        self.assertEqual(changelog_mtime_before, changelog_mtime_after)

        # Маркер остался прежним
        self.assertEqual(_read_marker(self._frags_dir), MARKER)

        # Фрагмент не удалён
        frag_path = os.path.join(self._frags_dir, "myfeature.md")
        self.assertTrue(os.path.exists(frag_path))

        # Версия в файле не изменилась
        version, _ = self._read_changelog()
        self.assertEqual(version, "1.0.0")


# ════════════════════════════════════════════════════════════════════════════
# 7. Ручные фрагменты приоритетны (идут первыми) над авто-коммитами
# ════════════════════════════════════════════════════════════════════════════

class TestFragmentsPriority(_BaseTest):
    def test_fragments_come_before_commit_bullets(self):
        MARKER = "11223344" * 5
        HEAD = "55667788" * 5
        _write_marker(self._frags_dir, MARKER)
        _add_fragment(self._frags_dir, "001_manual.md",
                      "Ручной пункт из фрагмента\n")

        GIT_LOG = "Авто-пункт из git"

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            if args[0] == "cat-file":
                return 0, ""
            if args[0] == "log":
                return 0, GIT_LOG + "\n"
            return 1, ""

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        _, entries = self._read_changelog()
        items = entries[0]["items"]
        self.assertGreaterEqual(len(items), 2)
        # Ручной фрагмент должен быть первым
        self.assertEqual(items[0], "Ручной пункт из фрагмента")
        # Авто-пункт из git должен быть вторым
        self.assertEqual(items[1], "Авто-пункт из git")
        # Фрагмент должен быть удалён после сборки
        frag_path = os.path.join(self._frags_dir, "001_manual.md")
        self.assertFalse(os.path.exists(frag_path))


# ════════════════════════════════════════════════════════════════════════════
# 8. Шум-фильтр: все паттерны _NOISE_RE корректно срабатывают
# ════════════════════════════════════════════════════════════════════════════

class TestNoiseFilter(unittest.TestCase):
    def _is_noise(self, subj: str) -> bool:
        return bool(bc._NOISE_RE.search(subj))

    def test_auto_prefix_is_noise(self):
        self.assertTrue(self._is_noise("auto: checkpoint"))

    def test_merge_is_noise(self):
        self.assertTrue(self._is_noise("Merge branch 'main'"))

    def test_chore_is_noise(self):
        self.assertTrue(self._is_noise("chore: bump deps"))

    def test_deploy_word_is_noise(self):
        self.assertTrue(self._is_noise("deploy: push to amvera"))

    def test_changelog_is_noise(self):
        self.assertTrue(self._is_noise("Update changelog for v1.2"))

    def test_deployment_update_is_noise(self):
        self.assertTrue(self._is_noise("Update deployment information"))

    def test_обновление_date_is_noise(self):
        self.assertTrue(self._is_noise("Обновление 2026-06-15"))

    def test_agent_handoff_is_noise(self):
        self.assertTrue(self._is_noise("agent_handoff updated"))

    def test_normal_commit_is_not_noise(self):
        self.assertFalse(self._is_noise("Добавить фильтр по дате"))

    def test_feature_commit_not_noise(self):
        self.assertFalse(self._is_noise("Новый раздел аналитики"))


# ════════════════════════════════════════════════════════════════════════════
# 9. _clean_subject: срезание Task- и CC-префиксов
# ════════════════════════════════════════════════════════════════════════════

class TestCleanSubject(unittest.TestCase):
    def test_task_prefix_stripped(self):
        self.assertEqual(bc._clean_subject("Task #42: Фикс авторизации"), "Фикс авторизации")

    def test_hash_only_prefix_stripped(self):
        self.assertEqual(bc._clean_subject("#12 - Улучшение экспорта"), "Улучшение экспорта")

    def test_feat_prefix_stripped(self):
        self.assertEqual(bc._clean_subject("feat: новый экспорт"), "новый экспорт")

    def test_fix_scope_prefix_stripped(self):
        self.assertEqual(bc._clean_subject("fix(auth): исправить токен"), "исправить токен")

    def test_clean_subject_unchanged(self):
        self.assertEqual(bc._clean_subject("Добавить штрихкоды"), "Добавить штрихкоды")


# ════════════════════════════════════════════════════════════════════════════
# 10. _bump_version корректен для всех уровней
# ════════════════════════════════════════════════════════════════════════════

class TestBumpVersion(unittest.TestCase):
    def test_minor_bump(self):
        self.assertEqual(bc._bump_version("1.5.3", "minor"), "1.6.0")

    def test_major_bump(self):
        self.assertEqual(bc._bump_version("1.5.3", "major"), "2.0.0")

    def test_patch_bump(self):
        self.assertEqual(bc._bump_version("1.5.3", "patch"), "1.5.4")

    def test_non_standard_version_reset(self):
        result = bc._bump_version("broken", "minor")
        self.assertEqual(result, "1.1.0")


# ════════════════════════════════════════════════════════════════════════════
# 11. _dedupe работает правильно
# ════════════════════════════════════════════════════════════════════════════

class TestDedupe(unittest.TestCase):
    def test_exact_duplicates_removed(self):
        inp = ["Фича А", "Фича Б", "Фича А"]
        self.assertEqual(bc._dedupe(inp), ["Фича А", "Фича Б"])

    def test_case_insensitive_dedup(self):
        inp = ["Экспорт в Excel", "экспорт в excel"]
        self.assertEqual(bc._dedupe(inp), ["Экспорт в Excel"])

    def test_whitespace_normalized_for_dedup(self):
        inp = ["Фича  А", "Фича А"]
        self.assertEqual(bc._dedupe(inp), ["Фича  А"])

    def test_empty_strings_removed(self):
        inp = ["", "Фича А", "  "]
        result = bc._dedupe(inp)
        self.assertEqual(result, ["Фича А"])

    def test_order_preserved(self):
        inp = ["Б", "А", "В"]
        self.assertEqual(bc._dedupe(inp), ["Б", "А", "В"])


# ════════════════════════════════════════════════════════════════════════════
# 12. Нет ни фрагментов, ни коммитов → no-op, версия не меняется
# ════════════════════════════════════════════════════════════════════════════

class TestFullNoOp(_BaseTest):
    def test_no_fragments_no_commits_noop(self):
        MARKER = "eeeeeeee" * 5
        HEAD = MARKER
        _write_marker(self._frags_dir, MARKER)

        def fake_git(args):
            if args[0] == "rev-parse":
                return 0, HEAD + "\n"
            if args[0] == "cat-file":
                return 0, ""
            return 1, ""

        with patch.object(bc, "_run_git", side_effect=fake_git):
            rc = self._run_main(["--no-ai"])

        self.assertEqual(rc, 0)
        version, entries = self._read_changelog()
        self.assertEqual(version, "1.0.0")
        self.assertEqual(len(entries), 1)


# ════════════════════════════════════════════════════════════════════════════
# 13. _polish_with_ai: ветки fallback и успешной полировки (mock ask_llm)
# ════════════════════════════════════════════════════════════════════════════

@contextmanager
def _mock_ai(ask_llm_impl, is_configured_result=True):
    """Подменяет web.ai_utils в sys.modules фейковым модулем.

    ask_llm_impl — async-функция (корутина), которую вызовет _polish_with_ai.
    is_configured_result — что вернёт is_configured().
    """
    fake = types.ModuleType("web.ai_utils")
    fake.ask_llm = ask_llm_impl

    def _is_configured():
        return is_configured_result

    fake.is_configured = _is_configured

    saved = sys.modules.get("web.ai_utils")
    sys.modules["web.ai_utils"] = fake
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["web.ai_utils"] = saved
        else:
            sys.modules.pop("web.ai_utils", None)


class TestPolishWithAI(unittest.TestCase):
    def test_ask_llm_raises_returns_original(self):
        bullets = ["Добавить штрихкоды", "Новый экспорт"]

        async def boom(*args, **kwargs):
            raise RuntimeError("LLM упал")

        with _mock_ai(boom):
            result = bc._polish_with_ai(list(bullets))

        self.assertEqual(result, bullets)

    def test_empty_answer_returns_original(self):
        bullets = ["Добавить штрихкоды", "Новый экспорт"]

        async def empty(*args, **kwargs):
            return ""

        with _mock_ai(empty):
            result = bc._polish_with_ai(list(bullets))

        self.assertEqual(result, bullets)

    def test_wrong_count_returns_original(self):
        bullets = ["Пункт А", "Пункт Б", "Пункт В"]

        async def too_few(*args, **kwargs):
            # Вернули N-1 пунктов → откат к исходным (не теряем новости)
            return "🚀 Пункт А\n📦 Пункт Б"

        with _mock_ai(too_few):
            result = bc._polish_with_ai(list(bullets))

        self.assertEqual(result, bullets)

    def test_extra_count_returns_original(self):
        bullets = ["Пункт А", "Пункт Б"]

        async def too_many(*args, **kwargs):
            # Вернули N+1 пунктов → откат к исходным
            return "🚀 Пункт А\n📦 Пункт Б\n✨ Лишний пункт"

        with _mock_ai(too_many):
            result = bc._polish_with_ai(list(bullets))

        self.assertEqual(result, bullets)

    def test_exact_count_polished(self):
        bullets = ["добавить штрихкоды", "новый экспорт в excel"]

        async def ok(*args, **kwargs):
            # Ровно N пунктов, с эмодзи, с нумерацией — нумерацию срезаем
            return "1. 🏷️ Теперь можно сканировать штрихкоды\n2. 📊 Доступен экспорт в Excel"

        with _mock_ai(ok):
            result = bc._polish_with_ai(list(bullets))

        self.assertEqual(result, [
            "🏷️ Теперь можно сканировать штрихкоды",
            "📊 Доступен экспорт в Excel",
        ])

    def test_not_configured_returns_original(self):
        bullets = ["Пункт А", "Пункт Б"]

        async def never_called(*args, **kwargs):
            raise AssertionError("ask_llm не должен вызываться без ключей")

        with _mock_ai(never_called, is_configured_result=False):
            result = bc._polish_with_ai(list(bullets))

        self.assertEqual(result, bullets)


if __name__ == "__main__":
    unittest.main(verbosity=2)
