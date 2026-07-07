---
name: Super-admin role overwritten by stale-session refresh
description: Session refresh logic in /dashboard read role from org DB — downgrading super_admin to user
---

# Super-admin role overwritten by stale-session refresh

## The rule
`get_user_role_from_db()` in `web/deps.py` MUST check `env_manager.is_super_admin()` FIRST before reading from `user_org_mapping`. The super_admin role is set at login via `env_manager`, not stored in org tables — an org may list the super admin as `role='user'`.

The stale-session refresh in `/dashboard` (`web/routes/dashboard.py`) must skip role refresh entirely when `role == 'super_admin'` (guard: `if telegram_id > 0 and role != 'super_admin':`).

**Why:** The session auto-refresh feature (Task #17) fetches the live org role to detect promotions. For super admins this returns `'user'` from `user_org_mapping`, silently downgrading their JWT. Super admins are NOT identified by their org row — they are identified by `env_manager.is_super_admin(telegram_id)`.

**How to apply:** Any code path that calls `get_user_role_from_db()` or reads `user_org_mapping.role` for role-based decisions must first guard with `env_manager.is_super_admin()`. Super-admin JWT role must never be derived from org DB.
