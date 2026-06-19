"""
test_variants.py — Тесты вариантов товара по торговым сетям (product_network_variants).
Покрывает: миграцию, upsert/delete, lookup по сети + fallback, каскад удаления,
полную обратную совместимость для орг без вариантов.
Не требует живого соединения с Telegram.
"""
import os
import sys
import tempfile

PASS = "✅"
FAIL = "❌"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


_tmp_dir = tempfile.mkdtemp()


def make_db(name="variants.db"):
    path = os.path.join(_tmp_dir, name)
    from database import Database
    db = Database(path)
    db.create_tables()
    return db


print("=" * 60)
print("  Тесты вариантов товара по торговым сетям")
print("=" * 60)

db = make_db()

# ── Миграция: таблица существует ────────────────────────────
conn = db.get_connection()
try:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(product_network_variants)").fetchall()]
finally:
    conn.close()
check("миграция: таблица product_network_variants создана", bool(cols))
check("миграция: колонки верные",
      cols == ["id", "product_id", "trade_network", "article", "barcode", "created_at"],
      detail=str(cols))

# ── Подготовка: товар + два магазина в разных сетях ─────────
pid = db.add_product("Наушники Sony", "Электроника", 5000,
                     article="DEFAULT-ART", barcode="0000000000001")
check("add_product: id получен", isinstance(pid, int) and pid > 0)

db.add_user(200001, "Прод", "Авец", trade_network="DNS", shop_name="DNS #1")
db.add_user(200002, "Прод", "Авец2", trade_network="М-Видео", shop_name="М-Видео #7")

check("get_network_for_shop: DNS", db.get_network_for_shop("DNS #1") == "DNS")
check("get_network_for_shop: М-Видео", db.get_network_for_shop("М-Видео #7") == "М-Видео")
check("get_network_for_shop: неизвестный → None", db.get_network_for_shop("Нет такого") is None)

# ── set_product_variant: upsert ─────────────────────────────
r1 = db.set_product_variant(pid, "DNS", "DNS-A100", "1111111111111")
r2 = db.set_product_variant(pid, "М-Видео", "MV-B200", "2222222222222")
check("set_product_variant: DNS ok", r1.get("ok") is True, str(r1))
check("set_product_variant: М-Видео ok", r2.get("ok") is True, str(r2))

vmap = db.get_product_variants_map(pid)
check("variants_map: 2 сети", len(vmap) == 2, str(vmap))
check("variants_map: DNS article upper", vmap.get("DNS", {}).get("article") == "DNS-A100")
check("variants_map: М-Видео barcode", vmap.get("М-Видео", {}).get("barcode") == "2222222222222")

# upsert (обновление существующего)
db.set_product_variant(pid, "DNS", "DNS-A100", "1111111111199")
v = db.get_product_variant(pid, "DNS")
check("upsert: barcode обновлён", v[4] == "1111111111199", str(v))
check("upsert: не создал дубль", len(db.get_product_variants(pid)) == 2)

# ── lookup по штрихкоду: сеть-aware + fallback ──────────────
# DNS-штрихкод с явной сетью DNS → товар
p = db.get_product_by_barcode("1111111111199", trade_network="DNS")
check("by_barcode: DNS-вариант с сетью DNS найден", p is not None and p[0] == pid)
# М-Видео-штрихкод с сетью М-Видео → тот же товар (product_id не дублируется)
p = db.get_product_by_barcode("2222222222222", trade_network="М-Видео")
check("by_barcode: МВ-вариант с сетью МВ → тот же product_id", p is not None and p[0] == pid)
# Вариант-штрихкод БЕЗ сети → всё равно находит (любой вариант)
p = db.get_product_by_barcode("2222222222222")
check("by_barcode: вариант без сети тоже найден", p is not None and p[0] == pid)
# Дефолтный штрихкод products.barcode → fallback работает
p = db.get_product_by_barcode("0000000000001")
check("by_barcode: fallback на products.barcode", p is not None and p[0] == pid)
# Дефолтный штрихкод даже с указанием сети (нет варианта с таким bc) → fallback
p = db.get_product_by_barcode("0000000000001", trade_network="DNS")
check("by_barcode: fallback с сетью (нет варианта) работает", p is not None and p[0] == pid)
# Несуществующий → None
check("by_barcode: несуществующий → None", db.get_product_by_barcode("9999999999999") is None)

# ── lookup по артикулу ──────────────────────────────────────
p = db.get_product_by_article("DNS-A100", trade_network="DNS")
check("by_article: DNS-вариант найден", p is not None and p[0] == pid)
p = db.get_product_by_article("mv-b200")  # регистронезависимо, без сети
check("by_article: МВ-вариант без сети, lower-case", p is not None and p[0] == pid)
p = db.get_product_by_article("DEFAULT-ART")
check("by_article: fallback на products.article", p is not None and p[0] == pid)
check("by_article: несуществующий → None", db.get_product_by_article("NOPE") is None)

# ── get_effective_product_codes ─────────────────────────────
eff = db.get_effective_product_codes(pid, "DNS")
check("effective: DNS → вариант", eff["is_variant"] is True and eff["barcode"] == "1111111111199")
eff = db.get_effective_product_codes(pid, "Лента")  # сеть без варианта
check("effective: сеть без варианта → дефолт", eff["is_variant"] is False and eff["barcode"] == "0000000000001")
eff = db.get_effective_product_codes(pid)  # без сети → дефолт
check("effective: без сети → дефолт", eff["is_variant"] is False and eff["article"] == "DEFAULT-ART")

# ── barcode_conflict: чужой штрихкод занят ──────────────────
pid2 = db.add_product("Колонка JBL", "Электроника", 3000)
rc = db.set_product_variant(pid2, "DNS", "JBL-1", "1111111111199")  # = DNS-вариант pid
check("conflict: занятый штрихкод → barcode_conflict",
      rc.get("ok") is False and rc.get("error") == "barcode_conflict", str(rc))

# ── conflict: вариант vs products.barcode ДРУГОГО товара ────
pid3 = db.add_product("Мышь Logitech", "Электроника", 1200, barcode="8888888888888")
rc2 = db.set_product_variant(pid2, "DNS", "M-1", "8888888888888")  # = products.barcode pid3
check("conflict: вариант = products.barcode другого товара → barcode_conflict",
      rc2.get("ok") is False and rc2.get("error") == "barcode_conflict", str(rc2))
# но свой собственный дефолтный штрихкод как вариант — OK
rc3 = db.set_product_variant(pid3, "DNS", "OWN-1", "8888888888888")
check("own-default: свой products.barcode как вариант → ok", rc3.get("ok") is True, str(rc3))
db.delete_product_variant(pid3, "DNS")
db.delete_product(pid3)

# ── set с пустыми обоими → удаление варианта ────────────────
db.set_product_variant(pid, "М-Видео", "", "")
check("empty-both: вариант М-Видео удалён", db.get_product_variant(pid, "М-Видео") is None)
check("empty-both: остался 1 вариант (DNS)", len(db.get_product_variants(pid)) == 1)

# ── delete_product_variant ──────────────────────────────────
db.delete_product_variant(pid, "DNS")
check("delete_variant: DNS удалён", db.get_product_variant(pid, "DNS") is None)
check("delete_variant: вариантов 0", len(db.get_product_variants(pid)) == 0)

# ── delete_product каскадит варианты ────────────────────────
db.set_product_variant(pid, "DNS", "X1", "1111111111199")
db.set_product_variant(pid, "Лента", "X2", "3333333333333")
check("pre-cascade: 2 варианта", len(db.get_product_variants(pid)) == 2)
db.delete_product(pid)
check("cascade: после delete_product вариантов 0", len(db.get_product_variants(pid)) == 0)
check("cascade: товар удалён", db.get_product_by_barcode("0000000000001") is None)

# ── Обратная совместимость: орг без вариантов ───────────────
db2 = make_db("legacy.db")
lpid = db2.add_product("Чайник", "Быт", 1500, article="LEG-1", barcode="7777777777777")
# старые вызовы без trade_network
check("legacy: by_barcode без сети", db2.get_product_by_barcode("7777777777777")[0] == lpid)
check("legacy: by_article без сети", db2.get_product_by_article("LEG-1")[0] == lpid)
# вызовы С сетью, но без вариантов → fallback
check("legacy: by_barcode с сетью → fallback",
      db2.get_product_by_barcode("7777777777777", trade_network="DNS")[0] == lpid)
check("legacy: variants_map пустой", db2.get_product_variants_map(lpid) == {})

# ── Итог ────────────────────────────────────────────────────
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print("=" * 60)
print(f"  Итог: {passed} ОК, {failed} ошибок")
print("=" * 60)

sys.exit(1 if failed else 0)
