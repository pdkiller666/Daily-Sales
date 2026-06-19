"""Регресс-тесты фильтрации товаров для «печати всех по фильтру».

Покрывает _filter_products: фильтр по категории, поиск по
названию/категории/артикулу/штрихкоду, .strip() и регистронезависимость.
Гарантирует, что список товаров (products_page) и набор для печати совпадают.
"""
from web.routes.products import (
    _filter_products, _grid_for_size, _label_size_options,
    _build_qr_payload, _build_label_ctx, _clean_qr_content,
)

PASS, FAIL = "✅", "❌"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


def _p(pid, name, cat, article="", barcode=""):
    # id[0] name[1] category[2] price[3] created_at[4] photo[5] desc[6] article[7] barcode[8]
    return (pid, name, cat, 100, "2026-01-01", "", "", article, barcode)


def main():
    prods = [
        _p(1, "Кофе молотый", "Напитки", article="ART-100", barcode="4600000000017"),
        _p(2, "Чай зелёный", "Напитки", article="ART-200"),
        _p(3, "Печенье овсяное", "Снеки", barcode="4600000000024"),
        _p(4, "Шоколад", "Снеки"),
    ]

    all_ids = lambda r: sorted(p[0] for p in r)

    check("без фильтра → все", all_ids(_filter_products(prods)) == [1, 2, 3, 4])
    check("категория Напитки", all_ids(_filter_products(prods, category="Напитки")) == [1, 2])
    check("категория Снеки", all_ids(_filter_products(prods, category="Снеки")) == [3, 4])
    check("категория с пробелами (.strip)",
          all_ids(_filter_products(prods, category="  Напитки  ")) == [1, 2])
    check("поиск по названию", all_ids(_filter_products(prods, q="кофе")) == [1])
    check("поиск регистронезависим", all_ids(_filter_products(prods, q="ШОКОЛАД")) == [4])
    check("поиск по категории", all_ids(_filter_products(prods, q="снеки")) == [3, 4])
    check("поиск по артикулу", all_ids(_filter_products(prods, q="art-200")) == [2])
    check("поиск по штрихкоду", all_ids(_filter_products(prods, q="4600000000024")) == [3])
    check("поиск с пробелами (.strip)", all_ids(_filter_products(prods, q="  кофе  ")) == [1])
    check("категория + поиск", all_ids(_filter_products(prods, q="чай", category="Напитки")) == [2])
    check("несовпадение → пусто", _filter_products(prods, q="неттакого") == [])
    check("пустой вход → пусто", _filter_products([]) == [])
    check("None вход → пусто", _filter_products(None) == [])

    # ── Раскладка листа: вместимость A4 для каждого размера ──
    expected_per_page = {
        "30x20": (6, 12, 72), "40x30": (4, 8, 32), "58x40": (3, 6, 18),
        "60x40": (3, 6, 18), "a6": (2, 3, 6),
        "a4-24": (3, 8, 24), "a4-65": (5, 13, 65),
    }
    for sz, (ec, er, ep) in expected_per_page.items():
        cols, rows, per = _grid_for_size(sz)
        check(f"раскладка {sz} = {ec}×{er}={ep}", (cols, rows, per) == (ec, er, ep),
              f"получено {cols}×{rows}={per}")
    check("a4-24 строго 24 на лист", _grid_for_size("a4-24")[2] == 24)
    check("a4-65 строго 65 на лист", _grid_for_size("a4-65")[2] == 65)
    check("каждый размер вмещает ≥1", all(_grid_for_size(o[0])[2] >= 1 for o in _label_size_options()))
    check("опций размеров = 7", len(_label_size_options()) == 7)

    # ── QR-содержимое: плейсхолдеры и нормализация ──
    pq = lambda t: _build_qr_payload(t, name="Кофе", price=100,
                                     article="ART-1", barcode="460", product_id=7)
    check("qr пусто → пусто", pq("") == "")
    check("qr {article}", pq("{article}") == "ART-1")
    check("qr {barcode}", pq("{barcode}") == "460")
    check("qr {name}", pq("{name}") == "Кофе")
    check("qr {price}", pq("{price}") == "100")
    check("qr {id}", pq("{id}") == "7")
    check("qr URL c плейсхолдером",
          pq("https://shop.ru/p/{article}") == "https://shop.ru/p/ART-1")
    check("qr несколько плейсхолдеров",
          pq("{name}-{price}") == "Кофе-100")
    check("qr неизвестный плейсхолдер не трогается",
          pq("{unknown}") == "{unknown}")

    check("clean qr trim", _clean_qr_content("  abc  ") == "abc")
    check("clean qr убирает переводы строк", _clean_qr_content("a\nb\rc") == "a b c")
    check("clean qr лимит 300", len(_clean_qr_content("x" * 500)) == 300)
    check("clean qr пусто", _clean_qr_content("") == "")
    check("clean qr None", _clean_qr_content(None) == "")

    # ── _build_label_ctx: QR fallback vs шаблон ──
    prod = _p(5, "Молоко", "Молочка", article="ART-MLK", barcode="999")
    ctx_default = _build_label_ctx(prod)
    check("ctx без шаблона → есть qr_b64", bool(ctx_default["qr_b64"]))
    ctx_tmpl = _build_label_ctx(prod, qr_content="https://s.ru/{article}")
    check("ctx с шаблоном → есть qr_b64", bool(ctx_tmpl["qr_b64"]))
    # товар без кодов: fallback пустой, но шаблон с {name} всё равно даёт QR
    prod_nocode = _p(6, "Хлеб", "Выпечка")
    check("ctx без кодов и без шаблона → qr пуст",
          _build_label_ctx(prod_nocode)["qr_b64"] == "")
    check("ctx без кодов но с шаблоном {name} → qr есть",
          bool(_build_label_ctx(prod_nocode, qr_content="{name}")["qr_b64"]))

    fails = sum(1 for s, _, _ in results if s == FAIL)
    print("=" * 50)
    print(f"Тестов: {len(results)} | Провалов: {fails}")
    print(f"{PASS} ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if fails == 0 else f"{FAIL} ЕСТЬ ПРОВАЛЫ")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
