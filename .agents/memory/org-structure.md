---
name: Flexible org structure (departments, custom roles, module access)
description: How the paid org_structure feature decomposes into legacy fields and how per-user module gating must respect billing
---

# Flexible org structure (Variant A+B)

Paid billing module `org_structure` (349₽). Free tier gets "minimal": ≤3 flat departments, no custom roles, no per-user module access. Full tier: hierarchy/regions, custom roles, multi-scope, per-user module access.

## Decomposition principle (why legacy code keeps working)
`tenant_manager.assign_org_role(tg, org_role_id)` does NOT add a new authz path. It writes the custom role back into the EXISTING `user_org_mapping` fields: `role=base_role` (owner/admin/user), `scope_type`/`scope_value`, `custom_title="icon name"`, plus `org_role_id` as a back-reference. So `is_any_admin()`, `get_user_org_scope()`, role display all keep working unchanged.
**How to apply:** never branch authorization on `org_role_id`; the base_role + scope IS the enforcement. The `can_*` permission flags on `org_roles` are stored metadata only (UI/future), not wired into routes.

## Reset semantics
`assign_org_role(tg, None)` must restore a standard employee: `role='user', scope_type/value=NULL, custom_title=NULL, org_role_id=NULL`, but ONLY `WHERE org_role_id IS NOT NULL AND role != 'owner'` — otherwise it would wipe a manually-set admin scope or demote an owner.

## Gating
`db_utils.org_structure_level(tg)` → 'full' if `has_module(get_org_owner_tg(tg) or tg, 'org_structure')` else 'minimal'. Billing is attached to the OWNER, not the employee.

## Per-user module gating — billing must NOT be bypassed
`has_module(tg)` is per-user; an employee's own billing is empty. In `web/app.py _nav_modules`, the `user_module_access` override means:
- `deny` → False (hide even if paid)
- `allow` → `has_module(OWNER_tg, k)` — grant only within what the org actually paid. **Never return unconditional True** (that was a billing bypass the architect caught).
- None → `has_module(tg, k)` (legacy per-user behavior, unchanged)

## Scope vocabulary
Use `'network'` for trade-network scope everywhere (matches existing `staff_set_scope`/`get_user_org_scope`). Do NOT use `'trade_network'` — it won't map into existing scope logic/display.

## Schema
- main.db `user_org_mapping` += `org_role_id, department_id, scope_shops, scope_cities` (all NULL = old behavior; backward-compatible migration in `tenant_manager._init_main_db`).
- org_*.db += `departments` (hierarchy via parent_id), `org_roles` (custom roles + can_* perms + modules list), `user_module_access` (allow/deny per module). Created in `database.create_tables()`.
