"""
billing_utils.py — Feature-gate checks for the modular billing system.

Usage:
    from billing_utils import has_module, has_extension, get_active_billing_items

All functions are synchronous and safe to call from web routes and bot handlers.
Super-admin and active-trial users always get full access.
Legacy plan subscribers are mapped to corresponding modules for backward compat.
"""

import json
import logging
import sqlite3
from typing import Set, Dict

import env_manager

SHOP_BOT_DB = "data/shop_bot.db"

# ── Legacy plan → module keys ─────────────────────────────────────────────────
_LEGACY_MODULE_MAP: Dict[str, Set[str]] = {
    "Бесплатный":  set(),
    "Базовый":     {"analytics", "notifications"},
    "Стандарт":    {"analytics", "notifications", "integrations"},
    "Премиум":     {"analytics", "team", "notifications", "plans_motivation",
                    "ai_assistant", "integrations", "chat"},
}
_LEGACY_FULL_ACCESS = {"Премиум"}   # these plans include all extensions

# ── Internal helpers ──────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(SHOP_BOT_DB)
    c.row_factory = sqlite3.Row
    return c


def _is_trial(tg_id: int) -> bool:
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


def _legacy_plan(tg_id: int) -> str:
    try:
        db = _conn()
        row = db.execute(
            """SELECT s.plan_type FROM subscriptions s
               JOIN users u ON u.id = s.user_id
               WHERE u.telegram_id=? AND s.end_date > datetime('now')
               ORDER BY s.end_date DESC LIMIT 1""",
            (tg_id,)
        ).fetchone()
        db.close()
        return row[0] if row else "Бесплатный"
    except Exception:
        return "Бесплатный"


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


# ── Public API ────────────────────────────────────────────────────────────────

def has_module(tg_id: int, module_key: str) -> bool:
    """True if user has access to the module.

    Priority: super_admin → trial → direct grant → bundle → legacy plan
    """
    try:
        if env_manager.is_super_admin(tg_id):
            return True
        if _is_trial(tg_id):
            return True
        if _has_direct_item(tg_id, module_key):
            return True
        if _module_from_bundles(tg_id, module_key):
            return True
        plan = _legacy_plan(tg_id)
        return module_key in _LEGACY_MODULE_MAP.get(plan, set())
    except Exception as e:
        logging.error(f"has_module({tg_id},{module_key}): {e}")
        return False


def has_extension(tg_id: int, ext_key: str) -> bool:
    """True if user has access to the extension within a module.

    Priority: super_admin → trial → direct grant → bundle → legacy premium
    """
    try:
        if env_manager.is_super_admin(tg_id):
            return True
        if _is_trial(tg_id):
            return True
        if _has_direct_item(tg_id, ext_key):
            return True
        if _module_from_bundles(tg_id, ext_key):
            return True
        return _legacy_plan(tg_id) in _LEGACY_FULL_ACCESS
    except Exception as e:
        logging.error(f"has_extension({tg_id},{ext_key}): {e}")
        return False


def get_active_billing_items(tg_id: int) -> Dict[str, Set[str]]:
    """Return {modules, extensions, bundles} sets of active keys for UI."""
    try:
        if env_manager.is_super_admin(tg_id):
            return {"modules": {"*"}, "extensions": {"*"}, "bundles": {"*"}}

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
                # Expand bundle — modules → modules, extensions → extensions
                inc = _bundle_includes(k)
                modules.update(inc["modules"])
                extensions.update(inc["extensions"])

        # Legacy plan fallback
        is_trial = _is_trial(tg_id)
        plan = _legacy_plan(tg_id)
        if is_trial:
            plan = "Премиум"
        modules.update(_LEGACY_MODULE_MAP.get(plan, set()))
        if plan in _LEGACY_FULL_ACCESS:
            extensions.add("*")

        return {"modules": modules, "extensions": extensions, "bundles": bundles}
    except Exception as e:
        logging.error(f"get_active_billing_items({tg_id}): {e}")
        return {"modules": set(), "extensions": set(), "bundles": set()}
