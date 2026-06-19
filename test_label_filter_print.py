"""Регресс-тесты фильтрации товаров для «печати всех по фильтру».

Покрывает _filter_products: фильтр по категории, поиск по
названию/категории/артикулу/штрихкоду, .strip() и регистронезависимость.
Гарантирует, что список товаров (products_page) и набор для печати совпадают.
"""
from web.routes.products import _filter_products

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

    fails = sum(1 for s, _, _ in results if s == FAIL)
    print("=" * 50)
    print(f"Тестов: {len(results)} | Провалов: {fails}")
    print(f"{PASS} ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if fails == 0 else f"{FAIL} ЕСТЬ ПРОВАЛЫ")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
