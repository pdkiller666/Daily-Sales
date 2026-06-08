"""
migrate_to_modules.py — one-time startup migration.

Converts active legacy subscriptions and org-level plans in main.db
to billing_module_subs grants in shop_bot.db.

Runs automatically at startup via run_startup_migration() called from main.py.
Flag file data/billing_v1_migrated prevents re-running.

Manual run:  python migrate_to_modules.py
"""
import os
import sqlite3
from datetime import datetime, timedelta

SHOP_BOT_DB = "data/shop_bot.db"
MAIN_DB     = "data/main.db"
FLAG_FILE   = "data/billing_v1_migrated"

LEGACY_MAP = {
    "Базовый":  ["analytics", "notifications"],
    "Стандарт": ["analytics", "notifications", "integrations"],
    "Премиум":  ["analytics", "team", "notifications", "plans_motivation",
                 "ai_assistant", "integrations", "chat"],
}
STANDARD_EXTENSIONS = [
    "scheduled_notifs", "smart_alerts",
    "plan_filters", "milestone_alerts",
]
PREMIUM_EXTENSIONS = [
    "abc_analysis", "heatmap", "turnover", "dead_stock", "trend_forecast",
    "ai_forecast", "ai_smart_alerts", "ai_high_limit",
    "gs_realtime", "scheduled_notifs", "smart_alerts",
    "plan_filters", "milestone_alerts", "plan_coefficients",
    "contests", "salary_export", "joint_motivation",
]

PLAN_EXTENSIONS = {
    "Стандарт": STANDARD_EXTENSIONS,
    "Премиум":  PREMIUM_EXTENSIONS,
}


def _grant(conn, tg_id: int, item_type: str, item_key: str, days: int, note: str):
    end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT OR IGNORE INTO billing_module_subs
               (user_telegram_id, item_type, item_key, is_active, price_paid,
                end_date, granted_by, note)
           VALUES (?, ?, ?, 1, 0, ?, 'migration', ?)""",
        (tg_id, item_type, item_key, end_date, note),
    )


def _grant_plan(conn, tg_id: int, plan: str, days: int, note: str):
    for key in LEGACY_MAP.get(plan, []):
        _grant(conn, tg_id, "module", key, days, note)
    for key in PLAN_EXTENSIONS.get(plan, []):
        _grant(conn, tg_id, "extension", key, days, note)


def _days_until(date_str: str, default: int = 365) -> int:
    if not date_str:
        return default
    try:
        end_dt = datetime.fromisoformat(str(date_str)[:19])
        return max(1, (end_dt - datetime.now()).days + 1)
    except Exception:
        return default


def migrate() -> int:
    """Run the full migration. Returns total number of grants created."""
    total = 0

    # ── 1. Individual subscriptions in shop_bot.db ────────────────────────────
    print("[migration] Step 1: individual subscriptions (shop_bot.db)")
    conn = sqlite3.connect(SHOP_BOT_DB)
    rows = conn.execute(
        """SELECT u.telegram_id, s.plan_type, s.end_date, s.is_trial
           FROM subscriptions s
           JOIN users u ON u.id = s.user_id
           WHERE s.end_date > datetime('now')
             AND (s.plan_type IN ('Базовый','Стандарт','Премиум') OR s.is_trial = 1)"""
    ).fetchall()

    if not rows:
        print("[migration]   (no active individual legacy subscriptions)")

    for tg_id, plan_type, end_date_str, is_trial in rows:
        plan = "Премиум" if is_trial else plan_type
        if plan not in LEGACY_MAP:
            continue
        days = _days_until(end_date_str, default=30)
        note = f"Мигрирован из {'пробного периода' if is_trial else f'тарифа {plan}'}"
        before = conn.execute(
            "SELECT COUNT(*) FROM billing_module_subs WHERE user_telegram_id=?", (tg_id,)
        ).fetchone()[0]
        _grant_plan(conn, tg_id, plan, days, note)
        after = conn.execute(
            "SELECT COUNT(*) FROM billing_module_subs WHERE user_telegram_id=?", (tg_id,)
        ).fetchone()[0]
        added = after - before
        total += added
        print(f"[migration]   tg_id={tg_id}  plan={plan}  days={days}  +{added} grants")

    conn.commit()
    conn.close()

    # ── 2. Org-level plans: grant to ALL active org members ───────────────────
    print("[migration] Step 2: org-level plans (main.db → all active members)")
    try:
        main_conn = sqlite3.connect(MAIN_DB)
        shop_conn = sqlite3.connect(SHOP_BOT_DB)

        orgs = main_conn.execute(
            """SELECT o.id, o.name, o.subscription_plan, o.subscription_end
               FROM organizations o
               WHERE o.subscription_plan IN ('Базовый','Стандарт','Премиум')
                 AND o.is_active = 1"""
        ).fetchall()

        if not orgs:
            print("[migration]   (no org-level paid plans found)")

        for org_id, org_name, plan, sub_end in orgs:
            # Skip truly expired org subscriptions (None sub_end = unlimited/lifetime)
            if sub_end:
                try:
                    if datetime.fromisoformat(str(sub_end)[:19]) <= datetime.now():
                        print(f"[migration]   SKIP org={org_name!r} — expired {sub_end[:10]}")
                        continue
                except Exception:
                    pass

            days = _days_until(sub_end, default=365)

            # Get ALL active members of this org
            members = main_conn.execute(
                """SELECT telegram_id, role FROM user_org_mapping
                   WHERE org_id = ? AND is_active = 1""",
                (org_id,),
            ).fetchall()

            if not members:
                print(f"[migration]   SKIP org={org_name!r} — no active members")
                continue

            print(f"[migration]   org={org_name!r}  plan={plan}  days={days}  members={len(members)}")
            for tg_id, role in members:
                note = f"Мигрирован из тарифа {plan} организации «{org_name}» (роль: {role})"
                before = shop_conn.execute(
                    "SELECT COUNT(*) FROM billing_module_subs WHERE user_telegram_id=?", (tg_id,)
                ).fetchone()[0]
                _grant_plan(shop_conn, tg_id, plan, days, note)
                after = shop_conn.execute(
                    "SELECT COUNT(*) FROM billing_module_subs WHERE user_telegram_id=?", (tg_id,)
                ).fetchone()[0]
                added = after - before
                total += added
                print(f"[migration]     tg_id={tg_id}  role={role}  +{added} grants")

        shop_conn.commit()
        shop_conn.close()
        main_conn.close()

    except Exception as e:
        print(f"[migration]   ! Org migration error: {e}")
        import traceback
        traceback.print_exc()

    print(f"[migration] Done. Total grants created: {total}")
    return total


def run_startup_migration():
    """
    Called automatically from main.py at startup.
    Runs the migration exactly once, then writes a flag file.
    Safe to call every restart — skips if flag already exists.
    """
    os.makedirs("data", exist_ok=True)

    if os.path.exists(FLAG_FILE):
        return  # already done

    print("[migration] First-run billing migration starting…")
    try:
        total = migrate()
        # Write flag ONLY after successful completion
        with open(FLAG_FILE, "w") as f:
            f.write(f"{datetime.now().isoformat()}  grants={total}\n")
        print(f"[migration] Migration complete ({total} grants). Flag written.")
    except Exception as e:
        print(f"[migration] WARNING: migration failed ({e}). Will retry on next restart.")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # Manual run: always execute regardless of flag file
    migrate()
