"""
test_package_refunds.py — Регресс-тесты возвратов абонементов (packages_utils.refund_package).

Покрывает баг-класс, из-за которого несколько частичных возвратов подряд могли
в сумме переплатить клиенту сверх price_paid и/или некорректно сторнировать
комиссию продавца (компаундинг ошибки округления/долей).

Проверяет:
  - одиночный частичный возврат;
  - несколько последовательных частичных возвратов + финальный полный —
    сумма всех refund_amount ДОЛЖНА быть равна price_paid ровно (не больше);
  - немедленный полный возврат;
  - повторный возврат уже возвращённого абонемента (already_refunded);
  - сторно комиссии продавца на каждом шаге (абсолютно от original_commission,
    не компаундится);
  - возврат не превышает стоимость неиспользованных занятий, если часть уже
    была использована клиентом;
  - fuzz: много единичных частичных возвратов подряд никогда не переплачивают.

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
_db_counter = [0]


def make_db():
    _db_counter[0] += 1
    path = os.path.join(_tmp_dir, f"pkg_refund_{_db_counter[0]}.db")
    from database import Database
    db = Database(path)
    db.create_tables()
    return db


def make_client(db, name="Клиент"):
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO clients (first_name, last_name, phone) VALUES (?, ?, ?)",
            (name, "", ""),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def make_service(db, name="Услуга"):
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO services (name, price) VALUES (?, ?)", (name, 0),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def make_seller(db, tg_id, name="Продавец"):
    db.add_user(tg_id, name, "Прод", shop_name="Магазин №1")


def set_global_motivation(db, mot_type, value, service_id=None):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO service_motivation_rules (service_id, scope_type, type, value, is_active) "
            "VALUES (?, 'global', ?, ?, 1)",
            (service_id, mot_type, value),
        )
        conn.commit()
    finally:
        conn.close()


def make_service_package(db, name, visits_total, price, service_id=None, validity_days=None):
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO service_packages (name, service_id, visits_total, price, validity_days) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, service_id, visits_total, price, validity_days),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_commission(db, client_package_id):
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT commission_amount, original_commission_amount FROM package_sale_earnings "
            "WHERE client_package_id=?",
            (client_package_id,),
        ).fetchone()
        return row
    finally:
        conn.close()


def get_client_package(db, client_package_id):
    conn = db.get_connection()
    try:
        return conn.execute(
            "SELECT visits_total, visits_used, price_paid, status, original_visits_total "
            "FROM client_packages WHERE id=?",
            (client_package_id,),
        ).fetchone()
    finally:
        conn.close()


def total_refunded(db, client_package_id):
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(refund_amount), 0), COALESCE(SUM(visits_refunded), 0) "
            "FROM client_package_refunds WHERE client_package_id=?",
            (client_package_id,),
        ).fetchone()
        return row
    finally:
        conn.close()


from packages_utils import sell_package, refund_package, consume_package_visit, calc_package_commission

SELLER_TG = 900001

print("=" * 70)
print("  Тесты возвратов абонементов (refund_package)")
print("=" * 70)

# ── Сценарий 1: одиночный частичный возврат ─────────────────────────────
db1 = make_db()
make_seller(db1, SELLER_TG)
service_id = make_service(db1)
set_global_motivation(db1, "percentage", 10, service_id=None)  # global rule
client_id = make_client(db1)
pkg_id = make_service_package(db1, "Абонемент 5 занятий", 5, 500)

conn = db1.get_connection()
cp_id, err = sell_package(conn, pkg_id, client_id, 500, SELLER_TG)
conn.commit()
conn.close()
check("sell_package: без ошибок", err is None, str(err))

comm = get_commission(db1, cp_id)
check("commission: начислена 50 (10% от 500)", comm is not None and abs(comm[0] - 50.0) < 0.001, str(comm))

conn = db1.get_connection()
res1 = refund_package(conn, cp_id, SELLER_TG, visits_to_refund=1)
conn.close()
check("refund #1: ok", res1.get("ok") is True, str(res1))
check("refund #1: сумма 100 (500/5*1)", abs(res1.get("refund_amount", -1) - 100.0) < 0.001, str(res1))
check("refund #1: не полный", res1.get("full_refund") is False, str(res1))

cp = get_client_package(db1, cp_id)
check("после refund #1: visits_total уменьшен на 1 (было 5 → 4)", cp[0] == 4, str(cp))
check("после refund #1: visits_used не тронут", cp[1] == 0, str(cp))
check("после refund #1: original_visits_total сохранён = 5", cp[4] == 5, str(cp))

comm = get_commission(db1, cp_id)
check("commission после refund #1: 40 (50*0.8)", comm is not None and abs(comm[0] - 40.0) < 0.001, str(comm))


# ── Сценарий 2: несколько частичных возвратов + финальный полный ───────
# Итоговая сумма ВСЕХ возвратов ОБЯЗАНА равняться price_paid ровно.
db2 = make_db()
make_seller(db2, SELLER_TG)
set_global_motivation(db2, "percentage", 10, service_id=None)
client_id2 = make_client(db2)
pkg_id2 = make_service_package(db2, "Абонемент 5 занятий", 5, 500)

conn = db2.get_connection()
cp_id2, err = sell_package(conn, pkg_id2, client_id2, 500, SELLER_TG)
conn.commit()
conn.close()

conn = db2.get_connection()
r1 = refund_package(conn, cp_id2, SELLER_TG, visits_to_refund=1)  # 100
conn.close()
conn = db2.get_connection()
r2 = refund_package(conn, cp_id2, SELLER_TG, visits_to_refund=2)  # 200
conn.close()
conn = db2.get_connection()
r3 = refund_package(conn, cp_id2, SELLER_TG)  # остаток (2 занятия) — full
conn.close()

check("шаг1: 100", abs(r1.get("refund_amount", -1) - 100.0) < 0.001, str(r1))
check("шаг2: 200", abs(r2.get("refund_amount", -1) - 200.0) < 0.001, str(r2))
check("шаг3: full=True", r3.get("full_refund") is True, str(r3))
check("шаг3: 200 (точный остаток потолка)", abs(r3.get("refund_amount", -1) - 200.0) < 0.001, str(r3))

total_amt, total_visits = total_refunded(db2, cp_id2)
check("СУММА всех возвратов = price_paid (500) РОВНО, не больше",
      abs(total_amt - 500.0) < 0.001, f"total={total_amt}")
check("не переплачено сверх 500", total_amt <= 500.0 + 1e-9, f"total={total_amt}")
check("всего возвращено занятий = 5", total_visits == 5, f"visits={total_visits}")

cp2 = get_client_package(db2, cp_id2)
check("итоговый статус = refunded", cp2[3] == "refunded", str(cp2))

comm2 = get_commission(db2, cp_id2)
check("итоговая комиссия = 0 (100% возвращено)", abs(comm2[0] - 0.0) < 0.001, str(comm2))


# ── Сценарий 3: полный возврат сразу ────────────────────────────────────
db3 = make_db()
make_seller(db3, SELLER_TG)
set_global_motivation(db3, "fixed", 30, service_id=None)
client_id3 = make_client(db3)
pkg_id3 = make_service_package(db3, "Абонемент 3 занятия", 3, 300)

conn = db3.get_connection()
cp_id3, err = sell_package(conn, pkg_id3, client_id3, 300, SELLER_TG)
conn.commit()
conn.close()

conn = db3.get_connection()
r_full = refund_package(conn, cp_id3, SELLER_TG, reason="клиент передумал")
conn.close()
check("немедленный полный возврат: ok", r_full.get("ok") is True, str(r_full))
check("немедленный полный возврат: сумма = price_paid (300)",
      abs(r_full.get("refund_amount", -1) - 300.0) < 0.001, str(r_full))
check("немедленный полный возврат: full=True", r_full.get("full_refund") is True, str(r_full))

comm3 = get_commission(db3, cp_id3)
check("комиссия после полного возврата = 0", abs(comm3[0] - 0.0) < 0.001, str(comm3))


# ── Сценарий 4: повторный возврат уже возвращённого абонемента ─────────
conn = db3.get_connection()
r_dup = refund_package(conn, cp_id3, SELLER_TG)
conn.close()
check("повторный возврат: error=already_refunded", r_dup.get("error") == "already_refunded", str(r_dup))

# И на абонементе из сценария 2 (полностью возвращён частичными долями)
conn = db2.get_connection()
r_dup2 = refund_package(conn, cp_id2, SELLER_TG)
conn.close()
check("повторный возврат (после серии частичных): already_refunded",
      r_dup2.get("error") == "already_refunded", str(r_dup2))


# ── Сценарий 5: часть занятий уже использована клиентом ─────────────────
# Возврат не должен включать стоимость уже отработанных занятий.
db5 = make_db()
make_seller(db5, SELLER_TG)
client_id5 = make_client(db5)
pkg_id5 = make_service_package(db5, "Абонемент 4 занятия", 4, 400)

conn = db5.get_connection()
cp_id5, err = sell_package(conn, pkg_id5, client_id5, 400, SELLER_TG)
conn.commit()
conn.close()

conn = db5.get_connection()
consume_package_visit(conn, cp_id5, SELLER_TG)  # 1 занятие использовано
conn.commit()
conn.close()

conn = db5.get_connection()
r5 = refund_package(conn, cp_id5, SELLER_TG)  # полный возврат остатка (3 занятия)
conn.close()
check("возврат с использованием: full=True", r5.get("full_refund") is True, str(r5))
check("возврат с использованием: сумма = 300 (100/визит * 3 неисп.)",
      abs(r5.get("refund_amount", -1) - 300.0) < 0.001, str(r5))
check("возврат с использованием: НЕ включает использованное занятие (не 400)",
      r5.get("refund_amount", -1) < 400.0 - 0.001, str(r5))


# ── Сценарий 6: невалидные запросы ──────────────────────────────────────
db6 = make_db()
make_seller(db6, SELLER_TG)
client_id6 = make_client(db6)
pkg_id6 = make_service_package(db6, "Абонемент 2 занятия", 2, 200)
conn = db6.get_connection()
cp_id6, err = sell_package(conn, pkg_id6, client_id6, 200, SELLER_TG)
conn.commit()
conn.close()

conn = db6.get_connection()
r_bad1 = refund_package(conn, cp_id6, SELLER_TG, visits_to_refund=0)
conn.close()
check("visits_to_refund=0 → invalid_amount", r_bad1.get("error") == "invalid_amount", str(r_bad1))

conn = db6.get_connection()
r_bad2 = refund_package(conn, cp_id6, SELLER_TG, visits_to_refund=-1)
conn.close()
check("visits_to_refund=-1 → invalid_amount", r_bad2.get("error") == "invalid_amount", str(r_bad2))

conn = db6.get_connection()
r_bad3 = refund_package(conn, 999999, SELLER_TG)
conn.close()
check("несуществующий client_package_id → not_found", r_bad3.get("error") == "not_found", str(r_bad3))

# Возврат больше остатка молча клэмпится к остатку, не переплачивает
conn = db6.get_connection()
r_over = refund_package(conn, cp_id6, SELLER_TG, visits_to_refund=999)
conn.close()
check("visits_to_refund > остатка → клэмп к остатку (2), сумма 200",
      r_over.get("ok") is True and r_over.get("visits_refunded") == 2
      and abs(r_over.get("refund_amount", -1) - 200.0) < 0.001, str(r_over))


# ── Сценарий 7: fuzz — много единичных частичных возвратов подряд ──────
# Гарантирует, что накопленная ошибка округления НИКОГДА не даёт переплату
# сверх price_paid, независимо от того, сколько занятий в абонементе.
db7 = make_db()
make_seller(db7, SELLER_TG)
set_global_motivation(db7, "percentage", 15, service_id=None)
client_id7 = make_client(db7)
VISITS = 7
PRICE = 999.0  # не делится ровно на VISITS — проверка округления
pkg_id7 = make_service_package(db7, "Абонемент 7 занятий (fuzz)", VISITS, PRICE)

conn = db7.get_connection()
cp_id7, err = sell_package(conn, pkg_id7, client_id7, PRICE, SELLER_TG)
conn.commit()
conn.close()

fuzz_results = []
for i in range(VISITS):
    conn = db7.get_connection()
    r = refund_package(conn, cp_id7, SELLER_TG, visits_to_refund=1)
    conn.close()
    fuzz_results.append(r)
    running_total, _ = total_refunded(db7, cp_id7)
    check(f"fuzz шаг {i+1}: возврат не переплачивает (running_total <= price_paid)",
          running_total <= PRICE + 1e-9, f"running_total={running_total}")

check("fuzz: все 7 шагов успешны", all(r.get("ok") for r in fuzz_results), str(fuzz_results))
check("fuzz: последний шаг — full_refund", fuzz_results[-1].get("full_refund") is True, str(fuzz_results[-1]))
final_total, final_visits = total_refunded(db7, cp_id7)
check("fuzz: итоговая сумма возвратов РОВНО price_paid", abs(final_total - PRICE) < 0.001, f"final={final_total}")
check("fuzz: итоговое число занятий = VISITS", final_visits == VISITS, f"visits={final_visits}")

final_comm = get_commission(db7, cp_id7)
check("fuzz: итоговая комиссия = 0", abs(final_comm[0] - 0.0) < 0.001, str(final_comm))


# ── Итог ─────────────────────────────────────────────────────────────────
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print("=" * 70)
print(f"  Итог: {passed} ОК, {failed} ошибок")
print("=" * 70)

sys.exit(1 if failed else 0)
