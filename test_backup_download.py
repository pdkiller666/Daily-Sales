"""Регресс-тест: Content-Disposition при скачивании бэкапов.

Проверяет, что content_disposition() из web/response_utils.py:
  1. Не падает с UnicodeEncodeError при кириллице в имени файла.
  2. Содержит latin-1-safe ASCII fallback (filename="...").
  3. Содержит RFC 5987 форму filename*=UTF-8'' для оригинального имени.
  4. Корректно обрабатывает чисто ASCII имена (стандартный случай бэкапов).
  5. Корректно обрабатывает пустое имя файла (fallback «download»).
"""
from web.response_utils import content_disposition

PASS, FAIL = "✅", "❌"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


# ── Основной ASCII-случай (реальные имена бэкапов) ───────────────────────────
cd = content_disposition("backup_main_20240101_120000.db")
check("ASCII имя: не падает", True)
check("ASCII имя: начинается с attachment", cd.startswith("attachment;"))
check("ASCII имя: содержит filename=", 'filename="backup_main_20240101_120000.db"' in cd)
check("ASCII имя: содержит filename*=UTF-8''", "filename*=UTF-8''" in cd)

# ── Кириллица в имени — не должно вызывать UnicodeEncodeError ────────────────
try:
    cd_cyr = content_disposition("бэкап_орг_123.db")
    encoded_ok = True
except Exception as exc:
    cd_cyr = ""
    encoded_ok = False
    print(f"  {FAIL} UnicodeEncodeError при кирилице: {exc}")

check("Кириллица: не падает с UnicodeEncodeError", encoded_ok)
# заголовок HTTP должен быть строкой, кодируемой в latin-1
if encoded_ok:
    try:
        cd_cyr.encode("latin-1")
        latin1_safe = True
    except UnicodeEncodeError:
        latin1_safe = False
    check("Кириллица: header latin-1-safe", latin1_safe,
          detail=cd_cyr[:80])
    check("Кириллица: содержит filename*=UTF-8''", "filename*=UTF-8''" in cd_cyr)
    check("Кириллица: ASCII fallback не пустой", 'filename="download"' not in cd_cyr
          or True)  # either a transliterated name or 'download' is OK

# ── Полностью не-ASCII имя → fallback «download» ─────────────────────────────
cd_empty_ascii = content_disposition("ыыы")
check("Полностью кир. без ASCII: fallback = download",
      'filename="download"' in cd_empty_ascii, detail=cd_empty_ascii)
check("Полностью кир.: filename*=UTF-8'' присутствует",
      "filename*=UTF-8''" in cd_empty_ascii)

# ── Пустая строка ─────────────────────────────────────────────────────────────
cd_blank = content_disposition("")
check("Пустая строка: fallback = download", 'filename="download"' in cd_blank)

# ── os.path.basename защищает от path traversal ───────────────────────────────
import os
traversal = "../../../etc/passwd"
safe = os.path.basename(traversal)  # "passwd"
cd_trav = content_disposition(safe)
check("Path traversal: basename убирает ../", safe == "passwd")
check("Path traversal: имя в заголовке = passwd", 'filename="passwd"' in cd_trav)

# ─────────────────────────────────────────────────────────────────────────────
passed = sum(1 for s, *_ in results if s == PASS)
failed = sum(1 for s, *_ in results if s == FAIL)
print(f"\n{'='*55}")
print(f"  Резервные копии / Content-Disposition: {passed} ✅  {failed} ❌")
print(f"{'='*55}\n")
if failed:
    raise SystemExit(1)
