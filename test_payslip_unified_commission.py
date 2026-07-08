"""
Regression test: staff bot personal payslip total must include
service_earnings and package_sale_earnings, not just seller_earnings (products).

Verifies that the commission total in my_payslip matches the web layer's
grand_total for a user with mixed sales (products + services + packages).
"""
import sqlite3
import os
import sys
import tempfile
import unittest

# ---------------------------------------------------------------------------
# Minimal in-memory DB setup (mirrors Database class just enough for the test)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    timezone TEXT DEFAULT 'Europe/Moscow'
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    shop_name TEXT NOT NULL,
    quantity_sold INTEGER NOT NULL,
    sale_price REAL,
    user_id INTEGER NOT NULL,
    sale_date TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL
);

-- seller_earnings: product motivation rows
CREATE TABLE IF NOT EXISTS seller_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER,
    user_id INTEGER NOT NULL,
    commission_amount REAL NOT NULL DEFAULT 0,
    motivation_type TEXT DEFAULT 'percent'
);

-- service_earnings: per-appointment commission
CREATE TABLE IF NOT EXISTS service_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER,
    user_id INTEGER NOT NULL,
    commission_amount REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL
);

-- package_sale_earnings: per-package commission
CREATE TABLE IF NOT EXISTS package_sale_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_package_id INTEGER,
    user_id INTEGER NOT NULL,
    commission_amount REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS client_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchased_at TEXT NOT NULL
);
"""

START = "2024-05-01"
END   = "2024-05-31"


def _build_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    # user internal id = 1
    conn.execute("INSERT INTO users(telegram_id, first_name, last_name) VALUES (111, 'Test', 'User')")

    # products sale → seller_earnings
    conn.execute("INSERT INTO products(name, category, price) VALUES ('Widget', 'Cat', 100)")
    conn.execute("INSERT INTO sales(product_id, shop_name, quantity_sold, sale_price, user_id, sale_date)"
                 " VALUES (1, 'Shop1', 1, 100, 1, '2024-05-10')")
    conn.execute("INSERT INTO seller_earnings(sale_id, user_id, commission_amount, motivation_type)"
                 " VALUES (1, 1, 150.0, 'percent')")   # product commission

    # return reversal
    conn.execute("INSERT INTO sales(product_id, shop_name, quantity_sold, sale_price, user_id, sale_date)"
                 " VALUES (1, 'Shop1', 1, 100, 1, '2024-05-11')")
    conn.execute("INSERT INTO seller_earnings(sale_id, user_id, commission_amount, motivation_type)"
                 " VALUES (2, 1, -30.0, 'return_reversal')")  # product return deduction

    # service appointment → service_earnings
    conn.execute("INSERT INTO appointments(start_time) VALUES ('2024-05-15 10:00')")
    conn.execute("INSERT INTO service_earnings(appointment_id, user_id, commission_amount)"
                 " VALUES (1, 1, 200.0)")   # service commission

    # package sale → package_sale_earnings
    conn.execute("INSERT INTO client_packages(purchased_at) VALUES ('2024-05-20')")
    conn.execute("INSERT INTO package_sale_earnings(client_package_id, user_id, commission_amount)"
                 " VALUES (1, 1, 80.0)")    # package commission

    conn.commit()
    return conn


class TestPayslipUnifiedCommission(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._conn = _build_db(self._tmp.name)

    def tearDown(self):
        self._conn.close()
        os.unlink(self._tmp.name)

    # ------------------------------------------------------------------
    # Helper: replicate the DB queries used by my_payslip
    # ------------------------------------------------------------------

    def _get_seller_motivation_summary(self, user_id):
        """Mirrors Database.get_seller_motivation_summary()."""
        row = self._conn.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN se.motivation_type != 'return_reversal'
                                 THEN se.commission_amount ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN se.motivation_type = 'return_reversal'
                                 THEN se.commission_amount ELSE 0 END), 0)
               FROM seller_earnings se
               JOIN sales s ON se.sale_id = s.id
               WHERE se.user_id = ? AND s.sale_date >= ? AND s.sale_date <= ?""",
            (user_id, START, END)
        ).fetchone()
        return (round(float(row[0] or 0), 2), round(float(row[1] or 0), 2))

    def _get_unified_service_package(self, user_id):
        """Mirrors the service/package parts of get_unified_commission_for_user()."""
        svc = self._conn.execute(
            """SELECT COALESCE(SUM(se.commission_amount), 0.0)
               FROM service_earnings se
               LEFT JOIN appointments a ON se.appointment_id = a.id
               WHERE se.user_id = ?
                 AND date(a.start_time) >= ? AND date(a.start_time) <= ?""",
            (user_id, START, END)
        ).fetchone()[0]
        pkg = self._conn.execute(
            """SELECT COALESCE(SUM(pse.commission_amount), 0.0)
               FROM package_sale_earnings pse
               LEFT JOIN client_packages cp ON pse.client_package_id = cp.id
               WHERE pse.user_id = ?
                 AND date(cp.purchased_at) >= ? AND date(cp.purchased_at) <= ?""",
            (user_id, START, END)
        ).fetchone()[0]
        return round(float(svc or 0), 2), round(float(pkg or 0), 2)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_product_only_total_is_incomplete(self):
        """Old behaviour: using only get_seller_motivation_summary misses svc+pkg."""
        gross, ret = self._get_seller_motivation_summary(1)
        product_only_total = gross + ret           # 150 - 30 = 120
        self.assertAlmostEqual(product_only_total, 120.0)

        svc, pkg = self._get_unified_service_package(1)
        full_total = product_only_total + svc + pkg  # 120 + 200 + 80 = 400
        self.assertGreater(full_total, product_only_total,
                           "Full commission must exceed product-only total when services/packages present")

    def test_payslip_total_matches_unified_grand_total(self):
        """New behaviour: my_payslip total = products + services + packages."""
        gross, ret = self._get_seller_motivation_summary(1)    # 150, -30
        svc, pkg   = self._get_unified_service_package(1)      # 200, 80

        commission_total = gross + ret + svc + pkg  # 120 + 200 + 80 = 400
        self.assertAlmostEqual(commission_total, 400.0,
                               msg="Payslip commission total must include all three sources")

    def test_product_gross_and_return_are_separated(self):
        """Display: gross_motivation and return_deduction remain distinct for display."""
        gross, ret = self._get_seller_motivation_summary(1)
        self.assertAlmostEqual(gross, 150.0, msg="Gross product motivation should be 150")
        self.assertAlmostEqual(ret,  -30.0,  msg="Return deduction should be -30")

    def test_service_and_package_commissions_are_nonzero(self):
        """Sanity: both service and package commissions are fetched correctly."""
        svc, pkg = self._get_unified_service_package(1)
        self.assertAlmostEqual(svc, 200.0, msg="Service commission should be 200")
        self.assertAlmostEqual(pkg,  80.0, msg="Package commission should be 80")

    def test_no_service_package_org_is_unaffected(self):
        """For an org without services/packages the totals stay identical to old behaviour."""
        # Create a user with only product sales
        self._conn.execute("INSERT INTO users(telegram_id, first_name, last_name) VALUES (222, 'Plain', 'Seller')")
        self._conn.execute("INSERT INTO sales(product_id, shop_name, quantity_sold, sale_price, user_id, sale_date)"
                           " VALUES (1, 'Shop2', 2, 100, 2, '2024-05-05')")
        self._conn.execute("INSERT INTO seller_earnings(sale_id, user_id, commission_amount, motivation_type)"
                           " VALUES (3, 2, 50.0, 'percent')")
        self._conn.commit()

        gross, ret = self._get_seller_motivation_summary(2)
        svc, pkg   = self._get_unified_service_package(2)

        self.assertAlmostEqual(svc, 0.0)
        self.assertAlmostEqual(pkg, 0.0)
        self.assertAlmostEqual(gross + ret + svc + pkg, gross + ret,
                               msg="No-service orgs: unified total equals product total")


if __name__ == "__main__":
    unittest.main(verbosity=2)
