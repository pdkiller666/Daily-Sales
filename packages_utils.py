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
            "(client_package_id, user_id, service_id, commission_amount, motivation_type, motivation_value, motivation_source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'global')",
            (client_package_id, user_id, service_id, commission, mot_type, mot_value),
        )
    except Exception as e:
        logger.warning("calc_package_commission client_package_id=%s: %s", client_package_id, e)


def sell_package(conn, package_id: int, client_id: int, price_paid, sold_by_tg_id: int, notes: str = ""):
    """Продать абонемент клиенту. Возвращает (client_package_id, error_code).
    error_code — один из None/"pkg_not_found"/"client_not_found"/"invalid_price".
    Коммит НЕ выполняется — вызывающий код должен сам сделать conn.commit().
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

    cur = conn.execute(
        "INSERT INTO client_packages "
        "(package_id, client_id, visits_total, visits_used, price_paid, expires_at, sold_by, notes) "
        "VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
        (package_id, client_id, visits_total, paid, expires_at, sold_by_tg_id, (notes or "").strip()),
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
