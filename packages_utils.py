"""
Общая бизнес-логика модуля «Абонементы» (service_packages / client_packages).
Используется веб-роутами (web/routes/packages.py, web/routes/appointments.py)
и бот-хендлерами (packages_handlers.py), чтобы продажа/списание/комиссия
считались одинаково независимо от канала (веб или Telegram-бот).
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def resolve_internal_user_id(conn, telegram_id: int):
    """users.telegram_id -> users.id, либо None."""
    row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    return row[0] if row else None


def calc_package_commission(conn, client_package_id: int, service_id, sold_by_tg_id: int, price: float):
    """Начисляет комиссию продавцу за продажу абонемента, если для привязанной
    услуги (или глобально) настроено правило мотивации (service_motivation_rules).
    Не бросает исключений наружу — при любой проблеме просто не начисляет.
    """
    try:
        user_id = resolve_internal_user_id(conn, sold_by_tg_id)
        if not user_id:
            return
        rule = None
        if service_id:
            rule = conn.execute(
                "SELECT type, value FROM service_motivation_rules "
                "WHERE service_id=? AND is_active=1 ORDER BY "
                "CASE scope_type WHEN 'user' THEN 1 WHEN 'shop' THEN 2 WHEN 'global' THEN 3 ELSE 4 END LIMIT 1",
                (service_id,),
            ).fetchone()
        if not rule:
            rule = conn.execute(
                "SELECT type, value FROM service_motivation_rules "
                "WHERE (service_id IS NULL OR service_id=0) AND scope_type='global' AND is_active=1 LIMIT 1",
            ).fetchone()
        if not rule:
            return

        mot_type, mot_value = rule
        if mot_type == "percentage":
            commission = (price * mot_value) / 100.0
        else:
            commission = float(mot_value)

        conn.execute(
            "INSERT OR REPLACE INTO package_sale_earnings "
            "(client_package_id, user_id, service_id, commission_amount, original_commission_amount, "
            "motivation_type, motivation_value, motivation_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'global')",
            (client_package_id, user_id, service_id, commission, commission, mot_type, mot_value),
        )
    except Exception as e:
        logger.warning("calc_package_commission client_package_id=%s: %s", client_package_id, e)


def _resolve_shop_name(conn, sold_by_tg_id: int):
    """Магазин продавца (users.shop_name по telegram_id), либо None."""
    try:
        row = conn.execute(
            "SELECT shop_name FROM users WHERE telegram_id=?", (sold_by_tg_id,)
        ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def sell_package(conn, package_id: int, client_id: int, price_paid, sold_by_tg_id: int, notes: str = "", receipt_id=None):
    """Продать абонемент клиенту. Возвращает (client_package_id, error_code).
    error_code — один из None/"pkg_not_found"/"client_not_found"/"invalid_price".
    Коммит НЕ выполняется — вызывающий код должен сам сделать conn.commit().
    shop_name продажи берётся из карточки продавца (users.shop_name по telegram_id),
    чтобы выручка от абонементов корректно попадала в кассу конкретного магазина.
    """
    pkg = conn.execute(
        "SELECT visits_total, price, validity_days, service_id FROM service_packages WHERE id=? AND is_active=1",
        (package_id,),
    ).fetchone()
    if not pkg:
        return None, "pkg_not_found"

    client = conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return None, "client_not_found"

    visits_total, pkg_price, validity_days, service_id = pkg
    try:
        paid = float(price_paid) if price_paid not in (None, "") else pkg_price
    except (ValueError, TypeError):
        return None, "invalid_price"
    if paid < 0:
        return None, "invalid_price"

    expires_at = None
    if validity_days:
        expires_at = (datetime.now() + timedelta(days=int(validity_days))).strftime("%Y-%m-%d %H:%M:%S")

    shop_name = _resolve_shop_name(conn, sold_by_tg_id)

    cur = conn.execute(
        "INSERT INTO client_packages "
        "(package_id, client_id, visits_total, original_visits_total, visits_used, price_paid, "
        "expires_at, sold_by, notes, shop_name, receipt_id) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
        (package_id, client_id, visits_total, visits_total, paid, expires_at, sold_by_tg_id,
         (notes or "").strip(), shop_name, receipt_id),
    )
    cp_id = cur.lastrowid
    calc_package_commission(conn, cp_id, service_id, sold_by_tg_id, paid)
    return cp_id, None


def find_consumable_package(conn, client_id: int, service_id):
    """Находит активный абонемент клиента, подходящий для списания визита по
    услуге service_id (либо универсальный, если у шаблона service_id NULL).
    Приоритет: сначала абонементы под конкретную услугу, затем универсальные;
    среди подходящих — тот, что истекает раньше (FIFO по expires_at).
    Возвращает client_package_id либо None.
    """
    if not client_id:
        return None
    row = conn.execute(
        "SELECT cp.id FROM client_packages cp "
        "JOIN service_packages sp ON sp.id=cp.package_id "
        "WHERE cp.client_id=? AND cp.status='active' AND cp.visits_used < cp.visits_total "
        "AND (cp.expires_at IS NULL OR cp.expires_at >= datetime('now')) "
        "AND (sp.service_id=? OR sp.service_id IS NULL) "
        "ORDER BY CASE WHEN sp.service_id IS NULL THEN 1 ELSE 0 END, "
        "cp.expires_at IS NULL, cp.expires_at ASC LIMIT 1",
        (client_id, service_id),
    ).fetchone()
    return row[0] if row else None


def consume_package_visit(conn, client_package_id: int, used_by_tg_id: int, appointment_id=None, note: str = ""):
    """Списывает одно занятие с абонемента. Возвращает dict с результатом либо
    dict с ключом 'error'. Коммит НЕ выполняется — обязанность вызывающего кода.
    """
    cp = conn.execute(
        "SELECT client_id, visits_total, visits_used, status, expires_at FROM client_packages WHERE id=?",
        (client_package_id,),
    ).fetchone()
    if not cp:
        return {"error": "not_found"}
    client_id, visits_total, visits_used, status, expires_at = cp
    if expires_at and str(expires_at) < datetime.now().strftime("%Y-%m-%d %H:%M:%S") and status == "active":
        conn.execute("UPDATE client_packages SET status='expired' WHERE id=?", (client_package_id,))
        status = "expired"
    if status != "active":
        return {"error": "not_active", "status": status}
    if visits_used >= visits_total:
        return {"error": "exhausted"}

    new_used = visits_used + 1
    new_status = "exhausted" if new_used >= visits_total else "active"
    conn.execute(
        "UPDATE client_packages SET visits_used=?, status=? WHERE id=?",
        (new_used, new_status, client_package_id),
    )
    conn.execute(
        "INSERT INTO client_package_uses (client_package_id, appointment_id, used_by, note) VALUES (?, ?, ?, ?)",
        (client_package_id, appointment_id, used_by_tg_id, (note or "").strip()),
    )
    return {
        "ok": True, "visits_used": new_used, "visits_left": visits_total - new_used,
        "status": new_status,
    }


def already_consumed_for_appointment(conn, appointment_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM client_package_uses WHERE appointment_id=? LIMIT 1", (appointment_id,)
    ).fetchone()
    return row is not None


def refund_package(conn, client_package_id: int, refunded_by_tg_id: int,
                    visits_to_refund=None, reason: str = ""):
    """Оформляет возврат абонемента (частичный — по числу неиспользованных
    занятий, или полный, если visits_to_refund не задан либо >= остатка).

    Возврат считается пропорционально цене за занятие от НЕИЗМЕННОГО базиса
    продажи (price_paid / original_visits_total), а не от текущего
    (уже урезанного предыдущими частичными возвратами) visits_total —
    иначе повторные частичные возвраты завышают сумму и могут в сумме
    превысить оплаченное. Итоговая сумма всех возвратов по абонементу
    жёстко ограничена price_paid.

    Комиссия продавца (package_sale_earnings, одна строка на абонемент)
    пересчитывается АБСОЛЮТНО от original_commission_amount и итоговой
    (кумулятивной) доли возврата — не компаундится от текущего значения,
    иначе несколько частичных возвратов дают накопленную ошибку.

    Коммит выполняется этой функцией самостоятельно (в отличие от старой
    версии) — вся операция от чтения остатка до записи возврата идёт внутри
    одной BEGIN IMMEDIATE транзакции, которая берёт write-lock ДО первого
    SELECT. Это исключает гонку: два параллельных запроса на возврат одного
    и того же абонемента больше не могут оба прочитать одно и то же "старое"
    состояние и оба применить свой возврат поверх него (двойное списание
    занятий/переплата/некорректное сторно комиссии). Второй конкурентный
    вызов дожидается снятия lock'а первым и работает уже с его результатом.
    Повторный conn.commit() у вызывающего кода после успешного возврата
    безвреден (нет активной транзакции).

    Возвращает dict с результатом либо dict с ключом 'error'.
    """
    raw = object.__getattribute__(conn, '_c') if hasattr(conn, '_c') else conn
    prev_isolation = raw.isolation_level
    raw.isolation_level = None  # autocommit → разрешает явный BEGIN
    cur = raw.cursor()
    began = False
    try:
        cur.execute("BEGIN IMMEDIATE")
        began = True

        cp = cur.execute(
            "SELECT visits_total, visits_used, price_paid, status, original_visits_total "
            "FROM client_packages WHERE id=?",
            (client_package_id,),
        ).fetchone()
        if not cp:
            cur.execute("ROLLBACK")
            return {"error": "not_found"}
        visits_total, visits_used, price_paid, status, original_visits_total = cp
        if status == "refunded":
            cur.execute("ROLLBACK")
            return {"error": "already_refunded"}

        visits_left = max(0, visits_total - visits_used)
        if visits_left <= 0:
            cur.execute("ROLLBACK")
            return {"error": "nothing_to_refund"}

        try:
            visits_to_refund = int(visits_to_refund) if visits_to_refund not in (None, "") else visits_left
        except (ValueError, TypeError):
            cur.execute("ROLLBACK")
            return {"error": "invalid_amount"}
        if visits_to_refund <= 0:
            cur.execute("ROLLBACK")
            return {"error": "invalid_amount"}
        visits_to_refund = min(visits_to_refund, visits_left)
        is_full = visits_to_refund >= visits_left

        # Неизменный базис: для старых записей (созданных до появления колонки)
        # original_visits_total может быть NULL — восстанавливаем его как
        # текущий visits_total + сумму уже возвращённых занятий.
        prev_refunds = cur.execute(
            "SELECT COALESCE(SUM(visits_refunded), 0), COALESCE(SUM(refund_amount), 0) "
            "FROM client_package_refunds WHERE client_package_id=?",
            (client_package_id,),
        ).fetchone()
        prev_visits_refunded, prev_refund_amount = prev_refunds
        basis_visits_total = original_visits_total or (visits_total + prev_visits_refunded) or 1

        price_per_visit = (price_paid / basis_visits_total) if basis_visits_total else 0.0
        raw_refund_amount = round(price_per_visit * visits_to_refund, 2)
        # Потолок возврата — НЕ price_paid целиком, а стоимость только тех занятий,
        # что ещё не были использованы клиентом (visits_used никогда не
        # возвращается). Иначе "полный" возврат остатка ошибочно вернул бы деньги
        # и за уже отработанные занятия.
        refundable_ceiling = round(price_per_visit * max(0, basis_visits_total - visits_used), 2)
        remaining_ceiling = max(0.0, refundable_ceiling - prev_refund_amount)
        if is_full:
            # Последний возврат остатка НЕИСПОЛЬЗОВАННЫХ занятий — отдаём точный
            # остаток потолка (защита от накопленных ошибок округления на
            # предыдущих частичных возвратах), а не price_paid целиком.
            refund_amount = round(remaining_ceiling, 2)
        else:
            refund_amount = round(min(raw_refund_amount, remaining_ceiling), 2)

        if is_full:
            cur.execute(
                "UPDATE client_packages SET status='refunded' WHERE id=? AND status != 'refunded'",
                (client_package_id,),
            )
            if cur.rowcount == 0:
                # Конкурентный запрос уже оформил возврат этого абонемента.
                cur.execute("ROLLBACK")
                return {"error": "already_refunded"}
        else:
            # Частичный возврат: возвращённые занятия "сгорают" — уменьшаем общее
            # число занятий по абонементу, использованные остаются как есть.
            # WHERE переподтверждает visits_total, прочитанный в НАЧАЛЕ этой же
            # транзакции — под write-lock'ом он не может измениться конкурентно,
            # но условие оставлено как defence-in-depth на случай будущих
            # изменений в обвязке транзакций.
            cur.execute(
                "UPDATE client_packages SET visits_total = visits_total - ? "
                "WHERE id=? AND visits_total=?",
                (visits_to_refund, client_package_id, visits_total),
            )
            if cur.rowcount == 0:
                cur.execute("ROLLBACK")
                return {"error": "concurrent_update"}

        cur.execute(
            "INSERT INTO client_package_refunds "
            "(client_package_id, visits_refunded, refund_amount, refunded_by, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (client_package_id, visits_to_refund, refund_amount, refunded_by_tg_id, (reason or "").strip()),
        )

        # Сторнируем комиссию продавца пропорционально КУМУЛЯТИВНОЙ доле возврата
        # в оплаченной сумме, считая от неизменного original_commission_amount —
        # не компаундим повторные частичные возвраты.
        try:
            row = cur.execute(
                "SELECT original_commission_amount, commission_amount FROM package_sale_earnings "
                "WHERE client_package_id=?",
                (client_package_id,),
            ).fetchone()
            if row and price_paid > 0:
                original_commission = row[0] if row[0] is not None else row[1]
                if original_commission:
                    total_refunded_after = prev_refund_amount + refund_amount
                    cumulative_ratio = min(1.0, total_refunded_after / price_paid)
                    new_commission = round(float(original_commission) * (1 - cumulative_ratio), 2)
                    cur.execute(
                        "UPDATE package_sale_earnings SET commission_amount=? WHERE client_package_id=?",
                        (new_commission, client_package_id),
                    )
        except Exception as e:
            logger.warning("refund_package commission reversal cp_id=%s: %s", client_package_id, e)

        cur.execute("COMMIT")
        began = False
        return {
            "ok": True,
            "refund_amount": refund_amount,
            "visits_refunded": visits_to_refund,
            "full_refund": is_full,
            "status": "refunded" if is_full else status,
        }
    except Exception:
        if began:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
        raise
    finally:
        try:
            raw.isolation_level = prev_isolation
        except Exception:
            pass


def auto_expire_packages(conn) -> None:
    """Автоматически переводит истёкшие/исчерпанные абонементы в соответствующий статус."""
    try:
        conn.execute(
            "UPDATE client_packages SET status='expired' "
            "WHERE status='active' AND expires_at IS NOT NULL AND expires_at < datetime('now')"
        )
        conn.execute(
            "UPDATE client_packages SET status='exhausted' "
            "WHERE status IN ('active','frozen') AND visits_used >= visits_total"
        )
    except Exception as e:
        logger.warning("auto_expire_packages: %s", e)
