"""Регресс-тесты выбора формата штрих-кода для ценников.

Покрывает _ean_checksum_ok / _barcode_format / _make_barcode_img:
валидный EAN-13/EAN-8, неверная контрольная цифра → Code-128 fallback,
12/7-значные коды (контрольная цифра досчитывается), нецифровые коды.
"""
from web.routes.products import (
    _ean_checksum_ok, _barcode_format, _make_barcode_img,
)

PASS, FAIL = "✅", "❌"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


# ── checksum ────────────────────────────────────────────────
check("checksum: валидный EAN-13", _ean_checksum_ok("4006381333931") is True)
check("checksum: невалидный EAN-13", _ean_checksum_ok("4600699501234") is False)
check("checksum: валидный EAN-8", _ean_checksum_ok("96385074") is True)
check("checksum: невалидный EAN-8", _ean_checksum_ok("96385070") is False)
check("checksum: неполная длина → False", _ean_checksum_ok("460069950123") is False)
check("checksum: нецифровой → False", _ean_checksum_ok("ABC12345") is False)

# ── format ──────────────────────────────────────────────────
check("format: валидный EAN-13", _barcode_format("4006381333931") == "ean13")
check("format: невалидный EAN-13 → code128", _barcode_format("4600699501234") == "code128")
check("format: 12 цифр → ean13", _barcode_format("460069950123") == "ean13")
check("format: валидный EAN-8", _barcode_format("96385074") == "ean8")
check("format: невалидный EAN-8 → code128", _barcode_format("96385070") == "code128")
check("format: 7 цифр → ean8", _barcode_format("9638507") == "ean8")
check("format: нецифровой → code128", _barcode_format("ART-123") == "code128")
check("format: пустой → code128", _barcode_format("") == "code128")

# ── рендер (PNG bytes генерируются для всех случаев) ─────────
for code in ("4006381333931", "4600699501234", "96385074", "ART-123", "460069950123"):
    img = _make_barcode_img(code)
    check(f"render: {code} → PNG", img is not None and len(img.getvalue()) > 0)
check("render: пустой код → None", _make_barcode_img("") is None)


if __name__ == "__main__":
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'='*50}")
    print(f"Тестов: {len(results)} | Провалов: {len(failed)}")
    print(f"{PASS} ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failed else f"{FAIL} ЕСТЬ ПРОВАЛЫ")
    import sys
    sys.exit(1 if failed else 0)
