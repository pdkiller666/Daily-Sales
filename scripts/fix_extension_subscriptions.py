"""
Скрипт восстановления данных после бага с расширениями.

Проблема: при подтверждении оплаты расширения (extension_<key>) через
confirm_payment_request(), эта функция не имела ветки для extension_ и
падала в fallthrough create_subscription(). Та перезаписывала план пользователя
на 'extension_<key>' с end_date='9999-12-31'.

Что делает этот скрипт:
1. Находит все subscription-строки с plan_type LIKE 'extension_%'
2. Для каждого пользователя ищет его последний одобренный Премиум/Стандарт/Базовый платёж
3. Восстанавливает plan_type и end_date из этого платежа
4. Добавляет расширение в billing_module_subs (если ещё нет)
5. Выводит полный отчёт о том что сделано

Запуск:
    python scripts/fix_extension_subscriptions.py
    python scripts/fix_extension_subscriptions.py --dry-run   (только просмотр, без изменений)
"""

import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv

SHOP_BOT_DB = "data/shop_bot.db"
BASE_DIR = Path(__file__).parent.parent
SHOP_BOT_PATH = str(BASE_DIR / SHOP_BOT_DB)

KNOWN_BASE_PLANS = ("Премиум", "Стандарт", "Базовый", "Бесплатный", "free")

LEGACY_GRANT_MAP = {
    "Базовый":  ["analytics", "notifications"],
    "Стандарт": ["analytics", "notifications", "integrations"],
    "Премиум":  ["analytics", "team", "notifications",
                 "plans_motivation", "ai_assistant", "integrations", "chat"],
}

print(f"{'[DRY RUN] ' if DRY_RUN else ''}Скрипт восстановления расширений")
print("=" * 60)

conn = sqlite3.connect(SHOP_BOT_PATH)
conn.row_factory = sqlite3.Row

# 1. Найти все испорченные строки
corrupted = conn.execute("""
    SELECT s.id as sub_id, s.user_id, s.plan_type as bad_plan,
           s.end_date, s.start_date,
           u.telegram_id, u.first_name, u.username
    FROM subscriptions s
    JOIN users u ON s.user_id = u.id
    WHERE s.plan_type LIKE 'extension_%'
    ORDER BY s.start_date DESC
""").fetchall()

if not corrupted:
    print("Испорченных строк не найдено. Всё чисто.")
    conn.close()
    sys.exit(0)

print(f"Найдено испорченных строк: {len(corrupted)}\n")

fixed = 0
errors = 0

for row in corrupted:
    sub_id = row["sub_id"]
    user_id = row["user_id"]
    tg_id = row["telegram_id"]
    bad_plan = row["bad_plan"]
    name = f"{row['first_name'] or ''} (@{row['username'] or tg_id})"
    ext_key = bad_plan[len("extension_"):]

    print(f"Пользователь: {name} | tg_id={tg_id}")
    print(f"  Испорченная строка: sub_id={sub_id} plan='{bad_plan}' end={row['end_date']}")

    # 2. Найти последний одобренный базовый платёж
    prem_req = conn.execute("""
        SELECT id, plan_type, amount, processed_at
        FROM payment_requests
        WHERE user_id = ? AND plan_type IN ('Премиум','Стандарт','Базовый')
          AND status = 'approved'
        ORDER BY processed_at DESC LIMIT 1
    """, (user_id,)).fetchone()

    if prem_req:
        restore_plan = prem_req["plan_type"]
        try:
            base_dt = datetime.fromisoformat(prem_req["processed_at"])
        except Exception:
            base_dt = datetime.now()
        # 30 дней от момента подтверждения базового плана
        restore_end = (base_dt + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  Базовый план: '{restore_plan}', платёж подтверждён {prem_req['processed_at']}")
        print(f"  Восстанавливаем end_date: {restore_end}")
    else:
        # Нет платёжной истории — ставим Бесплатный (бессрочно)
        restore_plan = "Бесплатный"
        restore_end = "9999-12-31 23:59:59"
        print(f"  Платёж за базовый план не найден — восстанавливаем 'Бесплатный'")

    # 3. Проверить есть ли уже расширение в billing_module_subs
    ext_grant = conn.execute("""
        SELECT id, end_date FROM billing_module_subs
        WHERE user_telegram_id = ? AND item_key = ?
        ORDER BY end_date DESC LIMIT 1
    """, (tg_id, ext_key)).fetchone()

    if ext_grant:
        print(f"  Расширение '{ext_key}': уже есть в billing_module_subs, end={ext_grant['end_date']}")
    else:
        print(f"  Расширение '{ext_key}': отсутствует в billing_module_subs — добавим на 30 дней")

    if not DRY_RUN:
        try:
            # Восстанавливаем подписку
            conn.execute("""
                UPDATE subscriptions
                SET plan_type = ?, end_date = ?, start_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (restore_plan, restore_end, sub_id))

            # Если расширения нет в billing_module_subs — добавляем
            if not ext_grant:
                ext_end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                # Находим request_id для этого расширения (если есть)
                ext_req = conn.execute("""
                    SELECT id FROM payment_requests
                    WHERE user_id = ? AND plan_type = ? AND status = 'approved'
                    ORDER BY processed_at DESC LIMIT 1
                """, (user_id, bad_plan)).fetchone()
                ext_req_id = ext_req["id"] if ext_req else None

                conn.execute("""
                    INSERT INTO billing_module_subs
                        (user_telegram_id, item_type, item_key, duration_days,
                         price_paid, granted_by, note, end_date, is_active,
                         payment_request_id, created_at)
                    VALUES (?, 'extension', ?, 30, 0.0, 'fix_script',
                            'Восстановлено fix_extension_subscriptions.py',
                            ?, 1, ?, datetime('now'))
                """, (tg_id, ext_key, ext_end, ext_req_id))

            conn.commit()
            fixed += 1
            print(f"  ✅ Исправлено успешно")
        except Exception as e:
            conn.rollback()
            errors += 1
            print(f"  ❌ Ошибка: {e}")
    else:
        print(f"  [DRY RUN] Изменения не применены")

    print()

conn.close()

print("=" * 60)
if DRY_RUN:
    print(f"DRY RUN завершён. Строк к исправлению: {len(corrupted)}")
else:
    print(f"Готово. Исправлено: {fixed}, ошибок: {errors}")
    if fixed > 0:
        print()
        print("ВАЖНО: после запуска скрипта попросите пользователей")
        print("перезайти в веб-кабинет (сессия кэширует лимиты на 2 мин).")
