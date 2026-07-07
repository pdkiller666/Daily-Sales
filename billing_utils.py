"""
billing_utils.py — Feature-gate checks for the modular billing system.

Usage:
    from billing_utils import has_module, has_extension, get_active_billing_items

All functions are synchronous and safe to call from web routes and bot handlers.
Super-admin and active-trial users always get full access.
Access is granted exclusively via billing_module_subs (direct grants or bundles).
Legacy plan tiers (Базовый/Стандарт/Премиум) are no longer used for access control.
"""

import json
import logging
import sqlite3
from typing import Set, Dict

from env_manager import env_manager as _env_mgr

SHOP_BOT_DB = "data/shop_bot.db"

# ── Soft-expiry grace window ────────────────────────────────────────────────
# Платные модули/пакеты продолжают работать ещё GRACE_DAYS дней после end_date,
# чтобы задержка оплаты не отключала доступ мгновенно. В это время в кабинете
# показывается баннер «оплатите». По истечении окна доступ возвращается к free.
# Триал и super_admin в grace НЕ попадают (у них доступ и так не платный).
GRACE_DAYS = 3
_GRACE_CUTOFF = f"datetime('now', '-{int(GRACE_DAYS)} days')"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(SHOP_BOT_DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=3000")
    return c


def _is_trial(tg_id: int) -> bool:
    """Active trial in subscriptions table (is_trial=1). Trial = full access."""
    try:
        db = _conn()
        row = db.execute(
            """SELECT 1 FROM subscriptions s
               JOIN users u ON u.id = s.user_id
               WHERE u.telegram_id=? AND s.is_trial=1
                 AND s.end_date > datetime('now') LIMIT 1""",
            (tg_id,)
        ).fetchone()
        db.close()
        return row is not None
    except Exception:
        return False


def _is_free_module(module_key: str) -> bool:
    """True if the module has price_monthly=0 (always active for everyone)."""
    try:
        db = _conn()
        row = db.execute(
            "SELECT 1 FROM billing_modules WHERE key=? AND price_monthly=0 AND is_active=1 LIMIT 1",
            (module_key,)
        ).fetchone()
        db.close()
        return row is not None
    except Exception:
        return False


def _free_module_keys() -> Set[str]:
    """Return set of all free module keys (price_monthly=0)."""
    try:
        db = _conn()
        rows = db.execute(
            "SELECT key FROM billing_modules WHERE price_monthly=0 AND is_active=1"
        ).fetchall()
        db.close()
        return {r[0] for r in rows}
    except Exception:
        return {'pos_retail'}


def _has_direct_item(tg_id: int, item_key: str) -> bool:
    """Direct row in billing_module_subs for item_key."""
    try:
        db = _conn()
        row = db.execute(
            f"""SELECT 1 FROM billing_module_subs
               WHERE user_telegram_id=? AND item_key=? AND is_active=1
                 AND (end_date IS NULL OR end_date > {_GRACE_CUTOFF}) LIMIT 1""",
            (tg_id, item_key)
        ).fetchone()
        db.close()
        return row is not None
    except Exception:
        return False


def _active_bundle_keys(tg_id: int) -> list:
    """Return list of active bundle item_keys for the user."""
    try:
        db = _conn()
        rows = db.execute(
            f"""SELECT item_key FROM billing_module_subs
               WHERE user_telegram_id=? AND item_type='bundle' AND is_active=1
                 AND (end_date IS NULL OR end_date > {_GRACE_CUTOFF})""",
            (tg_id,)
        ).fetchall()
        db.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _bundle_modules(bundle_key: str) -> Set[str]:
    """Expand a bundle key → set of all keys it contains (modules + extensions)."""
    try:
        db = _conn()
        row = db.execute(
            "SELECT includes_json FROM billing_bundles WHERE key=? AND is_active=1 LIMIT 1",
            (bundle_key,)
        ).fetchone()
        db.close()
        if not row:
            return set()
        data = json.loads(row[0] or "{}")
        keys: Set[str] = set()
        keys.update(data.get("modules", []))
        keys.update(data.get("extensions", []))
        return keys
    except Exception:
        return set()


def _bundle_includes(bundle_key: str) -> Dict[str, Set[str]]:
    """Expand a bundle → {'modules': set, 'extensions': set}."""
    try:
        db = _conn()
        row = db.execute(
            "SELECT includes_json FROM billing_bundles WHERE key=? AND is_active=1 LIMIT 1",
            (bundle_key,)
        ).fetchone()
        db.close()
        if not row:
            return {"modules": set(), "extensions": set()}
        data = json.loads(row[0] or "{}")
        return {
            "modules":    set(data.get("modules", [])),
            "extensions": set(data.get("extensions", [])),
        }
    except Exception:
        return {"modules": set(), "extensions": set()}


def _module_from_bundles(tg_id: int, key: str) -> bool:
    """Check if user has an active bundle that includes *key*."""
    for bk in _active_bundle_keys(tg_id):
        if key in _bundle_modules(bk):
            return True
    return False


def _ext_module_key(ext_key: str) -> str | None:
    """Return parent module_key for an extension, or None if not in DB."""
    try:
        db = _conn()
        row = db.execute(
            'SELECT module_key FROM billing_extensions WHERE key=? LIMIT 1', (ext_key,)
        ).fetchone()
        db.close()
        return row[0] if row else None
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def in_grace_period(tg_id: int) -> bool:
    """True если пользователь сейчас в окне soft-expiry: есть платный пункт,
    у которого end_date уже прошёл, но не более чем GRACE_DAYS назад.
    Триал и super_admin никогда не в grace."""
    try:
        if _env_mgr.is_super_admin(tg_id) or _is_trial(tg_id):
            return False
        db = _conn()
        row = db.execute(
            f"""SELECT 1 FROM billing_module_subs
                WHERE user_telegram_id=? AND is_active=1
                  AND end_date IS NOT NULL
                  AND end_date <= datetime('now')
                  AND end_date > {_GRACE_CUTOFF} LIMIT 1""",
            (tg_id,)
        ).fetchone()
        db.close()
        return row is not None
    except Exception as e:
        logging.error(f"in_grace_period({tg_id}): {e}")
        return False


def grace_days_left(tg_id: int) -> "int | None":
    """Сколько дней осталось в grace-окне (по самому позднему истёкшему пункту),
    или None если пользователь не в grace."""
    try:
        if _env_mgr.is_super_admin(tg_id) or _is_trial(tg_id):
            return None
        db = _conn()
        row = db.execute(
            f"""SELECT MAX(end_date) FROM billing_module_subs
                WHERE user_telegram_id=? AND is_active=1
                  AND end_date IS NOT NULL
                  AND end_date <= datetime('now')
                  AND end_date > {_GRACE_CUTOFF}""",
            (tg_id,)
        ).fetchone()
        db.close()
        if not row or not row[0]:
            return None
        from datetime import datetime as _dt, timedelta as _td
        end_dt = _dt.fromisoformat(row[0])
        grace_end = end_dt + _td(days=GRACE_DAYS)
        return max(0, (grace_end - _dt.utcnow()).days)
    except Exception as e:
        logging.error(f"grace_days_left({tg_id}): {e}")
        return None


def has_module(tg_id: int, module_key: str) -> bool:
    """True if user has access to the module.

    Priority: super_admin → free modules → trial → direct grant → bundle
    """
    try:
        # pos_retail (basic POS) is permanently free for everyone
        if module_key == 'pos_retail':
            return True
        if _env_mgr.is_super_admin(tg_id):
            return True
        # Free modules (price_monthly=0) are always active for everyone
        if _is_free_module(module_key):
            return True
        if _is_trial(tg_id):
            return True
        if _has_direct_item(tg_id, module_key):
            return True
        if _module_from_bundles(tg_id, module_key):
            return True
        return False
    except Exception as e:
        logging.error(f"has_module({tg_id},{module_key}): {e}")
        return False


def get_modules_access(tg_id: int, keys) -> Dict[str, bool]:
    """Bulk equivalent of ``{k: has_module(tg_id, k) for k in keys}``.

    Computes the shared super_admin / trial / direct-grant / bundle data ONCE
    (≈3 queries total) instead of re-querying inside has_module for every key
    (≈4 queries × len(keys)). Semantically identical to per-key has_module:
      access = super_admin OR trial OR (key is a direct active item_key)
                OR (key is included in an active bundle); pos_retail always True.
    """
    keys = list(keys)
    try:
        if _env_mgr.is_super_admin(tg_id) or _is_trial(tg_id):
            return {k: True for k in keys}
        db = _conn()
        rows = db.execute(
            f"""SELECT item_key, item_type FROM billing_module_subs
               WHERE user_telegram_id=? AND is_active=1
                 AND (end_date IS NULL OR end_date > {_GRACE_CUTOFF})""",
            (tg_id,)
        ).fetchall()
        db.close()
        direct: Set[str] = set()
        bundle_keys = []
        for r in rows:
            direct.add(r[0])  # _has_direct_item matches any item_type
            if r[1] == 'bundle':
                bundle_keys.append(r[0])
        bundle_mods: Set[str] = set()
        for bk in bundle_keys:
            bundle_mods |= _bundle_modules(bk)
        # Free modules (price_monthly=0) always active — fetch once for the bulk path
        free_mods = _free_module_keys()
        result: Dict[str, bool] = {}
        for k in keys:
            if k == 'pos_retail' or k in free_mods:
                result[k] = True
            else:
                result[k] = (k in direct) or (k in bundle_mods)
        return result
    except Exception as e:
        logging.error(f"get_modules_access({tg_id}): {e}")
        # Fail safe: fall back to the per-key path (preserves exact semantics).
        return {k: has_module(tg_id, k) for k in keys}


def is_extension_denied(tg_id: int, ext_key: str) -> bool:
    """Fail-open gate: returns True ONLY when there is an explicit is_active=0 record.

    Absence of a subscription record = not denied (allow).
    Use this for extensions that should work for existing users until explicitly revoked.
    """
    try:
        if _env_mgr.is_super_admin(tg_id):
            return False
        if _is_trial(tg_id):
            return False
        db = _conn()
        row = db.execute(
            """SELECT is_active FROM billing_module_subs
               WHERE user_telegram_id=? AND item_key=?
               ORDER BY id DESC LIMIT 1""",
            (tg_id, ext_key)
        ).fetchone()
        db.close()
        if row is None:
            return False  # no record at all → fail-open, allow
        return row[0] == 0  # explicitly revoked
    except Exception as e:
        logging.error(f"is_extension_denied({tg_id},{ext_key}): {e}")
        return False  # fail-open on exception


def has_extension(tg_id: int, ext_key: str) -> bool:
    """True if user has access to the extension AND its parent module is active.

    Priority: super_admin → trial → (direct grant OR bundle) + parent module check.
    Расширение недоступно, если родительский модуль отозван — даже при активной
    записи в billing_module_subs для самого расширения.
    """
    try:
        if _env_mgr.is_super_admin(tg_id):
            return True
        if _is_trial(tg_id):
            return True
        ext_ok = _has_direct_item(tg_id, ext_key) or _module_from_bundles(tg_id, ext_key)
        if not ext_ok:
            return False
        mod_key = _ext_module_key(ext_key)
        if mod_key and not has_module(tg_id, mod_key):
            return False
        return True
    except Exception as e:
        logging.error(f"has_extension({tg_id},{ext_key}): {e}")
        return False


def get_active_billing_items(tg_id: int) -> Dict[str, Set[str]]:
    """Return {modules, extensions, bundles} sets of active keys for UI."""
    try:
        if _env_mgr.is_super_admin(tg_id):
            return {"modules": {"*"}, "extensions": {"*"}, "bundles": {"*"}}

        # Trial → full access
        if _is_trial(tg_id):
            return {"modules": {"*"}, "extensions": {"*"}, "bundles": set()}

        db = _conn()
        rows = db.execute(
            f"""SELECT item_type, item_key FROM billing_module_subs
               WHERE user_telegram_id=? AND is_active=1
                 AND (end_date IS NULL OR end_date > {_GRACE_CUTOFF})""",
            (tg_id,)
        ).fetchall()
        db.close()

        modules: Set[str] = set()
        extensions: Set[str] = set()
        bundles: Set[str] = set()

        for r in rows:
            t, k = r[0], r[1]
            if t == "module":
                modules.add(k)
            elif t == "extension":
                extensions.add(k)
            elif t == "bundle":
                bundles.add(k)
                inc = _bundle_includes(k)
                modules.update(inc["modules"])
                extensions.update(inc["extensions"])

        # Free modules (price_monthly=0) are always active — add them so the
        # UI can display parent_active=True for their extension groups without
        # requiring an explicit billing_module_subs record.
        try:
            _fdb = _conn()
            _free_rows = _fdb.execute(
                "SELECT key FROM billing_modules WHERE price_monthly=0 AND is_active=1"
            ).fetchall()
            _fdb.close()
            for (fk,) in _free_rows:
                modules.add(fk)
        except Exception:
            modules.add('pos_retail')  # hardcoded fallback

        return {"modules": modules, "extensions": extensions, "bundles": bundles}
    except Exception as e:
        logging.error(f"get_active_billing_items({tg_id}): {e}")
        return {"modules": set(), "extensions": set(), "bundles": set()}
