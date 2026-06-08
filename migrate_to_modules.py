"""
migrate_to_modules.py — one-time migration script.

Converts active legacy subscriptions (Базовый/Стандарт/Премиум/trial)
and org-level plans to billing_module_subs grants.

Run once:  python migrate_to_modules.py
"""
import sqlite3
from datetime import datetime, timedelta

SHOP_BOT_DB = "data/shop_bot.db"
MAIN_DB = "data/main.db"

LEGACY_MAP = {
    "Базовый":  ["analytics", "notifications"],
    "Стандарт": ["analytics", "notifications", "integrations"],
    "Премиум":  ["analytics", "team", "notifications", "plans_motivation",
                 "ai_assistant", "integrations", "chat"],
}
PREMIUM_EXTENSIONS = [
    "abc_analysis", "heatmap", "turnover", "dead_stock", "trend_forecast",
    "ai_forecast", "ai_smart_alerts", "ai_high_limit",
    "gs_realtime", "scheduled_notifs", "smart_alerts",
    "plan_filters", "milestone_alerts", "plan_coefficients",
    "contests", "salary_export", "joint_motivation",
]


def grant_item(conn, tg_id, item_type, item_key, days, note="Мигрирован из legacy-подписки"):
    end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT OR IGNORE INTO billing_module_subs
               (user_telegram_id, item_type, item_key, is_active, price_paid,
                end_date, granted_by, note)
           VALUES (?, ?, ?, 1, 0, ?, 'migration', ?)""",
        (tg_id, item_type, item_key, end_date, note)
    )


def migrate():
    total_grants = 0

    # ── 1. Individual subscriptions in shop_bot.db ────────────────────────────
    print("=== Migrating individual subscriptions (shop_bot.db) ===")
    conn = sqlite3.connect(SHOP_BOT_DB)
    rows = conn.execute(
        """SELECT u.telegram_id, s.plan_type, s.end_date, s.is_trial
           FROM subscriptions s
           JOIN users u ON u.id = s.user_id
           WHERE s.end_date > datetime('now')
             AND (s.plan_type IN ('Базовый','Стандарт','Премиум') OR s.is_trial=1)"""
    ).fetchall()

    if not rows:
        print("  (no active legacy subscriptions found)")

    for tg_id, plan_type, end_date_str, is_trial in rows:
        plan = "Премиум" if is_trial else plan_type
        modules = LEGACY_MAP.get(plan, [])
        if not modules:
            continue
        try:
            end_dt = datetime.fromisoformat(str(end_date_str)[:19])
            days = max(1, (end_dt - datetime.now()).days + 1)
        except Exception:
            days = 30

        note = f"Мигрирован из {'пробного периода' if is_trial else f'тарифа {plan}'}"
        for key in modules:
            grant_item(conn, tg_id, "module", key, days, note)
            total_grants += 1
        if plan == "Премиум":
            for key in PREMIUM_EXTENSIONS:
                grant_item(conn, tg_id, "extension", key, days, note)
                total_grants += 1
        print(f"  ✓ tg_id={tg_id}  plan={plan}  days={days}  modules={len(modules)}")

    conn.commit()
    conn.close()
    print(f"  Individual subscriptions: done\n")

    # ── 2. Org-level plans in main.db → grant to org owners ──────────────────
    print("=== Migrating org-level plans (main.db) ===")
    try:
        main_conn = sqlite3.connect(MAIN_DB)
        shop_conn = sqlite3.connect(SHOP_BOT_DB)

        orgs = main_conn.execute(
            """SELECT o.id, o.name, o.subscription_plan, o.subscription_end,
                      m.telegram_id
               FROM organizations o
               JOIN user_org_mapping m ON m.org_id = o.id
                 AND m.role='owner' AND m.is_active=1
               WHERE o.subscription_plan IN ('Базовый','Стандарт','Премиум')"""
        ).fetchall()

        if not orgs:
            print("  (no org-level paid plans found)")

        for org_id, org_name, plan, sub_end, owner_tg_id in orgs:
            if sub_end:
                try:
                    end_dt = datetime.fromisoformat(str(sub_end)[:19])
                    if end_dt <= datetime.now():
                        print(f"  SKIP org={org_id} ({org_name}) — plan expired {sub_end[:10]}")
                        continue
                    days = max(1, (end_dt - datetime.now()).days + 1)
                except Exception:
                    days = 30
            else:
                days = 365

            modules = LEGACY_MAP.get(plan, [])
            note = f"Мигрирован из тарифа {plan} организации {org_name}"
            for key in modules:
                grant_item(shop_conn, owner_tg_id, "module", key, days, note)
                total_grants += 1
            if plan == "Премиум":
                for key in PREMIUM_EXTENSIONS:
                    grant_item(shop_conn, owner_tg_id, "extension", key, days, note)
                    total_grants += 1
            print(f"  ✓ org={org_id} ({org_name})  tg={owner_tg_id}  plan={plan}  days={days}")

        shop_conn.commit()
        shop_conn.close()
        main_conn.close()
    except Exception as e:
        print(f"  ! Org migration error: {e}")

    print(f"\n=== Done. Total grants created: {total_grants} ===")


if __name__ == "__main__":
    migrate()
