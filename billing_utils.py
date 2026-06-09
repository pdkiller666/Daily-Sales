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


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(SHOP_BOT_DB)
    c.row_factory = sqlite3.Row
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


def _has_direct_item(tg_id: int, item_key: str) -> bool:
    """Direct row in billing_module_subs for item_key."""
    try:
        db = _conn()
        row = db.execute(
            """SELECT 1 FROM billing_module_subs
               WHERE user_telegram_id=? AND item_key=? AND is_active=1
                 AND (end_date IS NULL OR end_date > datetime('now')) LIMIT 1""",
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
            """SELECT item_key FROM billing_module_subs
               WHERE user_telegram_id=? AND item_type='bundle' AND is_active=1
                 AND (end_date IS NULL OR end_date > datetime('now'))""",
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

def has_module(tg_id: int, module_key: str) -> bool:
    """True if user has access to the module.

    Priority: super_admin → trial → direct grant → bundle
    """
    try:
        if _env_mgr.is_super_admin(tg_id):
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
            """SELECT item_type, item_key FROM billing_module_subs
               WHERE user_telegram_id=? AND is_active=1
                 AND (end_date IS NULL OR end_date > datetime('now'))""",
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

        return {"modules": modules, "extensions": extensions, "bundles": bundles}
    except Exception as e:
        logging.error(f"get_active_billing_items({tg_id}): {e}")
        return {"modules": set(), "extensions": set(), "bundles": set()}
