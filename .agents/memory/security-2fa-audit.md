---
name: Security batch — 2FA, audit-log, per-request CSRF
description: Durable decisions/gotchas from the 2FA + super-admin audit + per-request CSRF work
---

## shop_bot.db-gated tables (gotcha)
`admin_audit_log` and `login_ips` are created in `create_tables()` only when
`'shop_bot' in self.db_file`. Any test or tool that writes to them MUST use a DB
path whose filename contains `shop_bot` (e.g. temp file `shop_bot_*.db`), or the
table silently won't exist and inserts log an error + read returns [].
**Why:** these are centralized (not per-org) tables; the filename guard keeps them
out of org_*.db. **How to apply:** when adding a centralized (non-tenant) table,
guard by `'shop_bot' in self.db_file` and name test DBs accordingly.

## 2FA is self-service, not owner-restricted (decision)
`/settings/2fa/init|enable|disable` are open to ANY authenticated user with a web
credential — each user only ever toggles 2FA on their *own* `cred['id']`. There is
no privilege escalation, so it was intentionally left un-gated despite roadmap item
16 saying "owner 2FA". **Why:** self-service 2FA for every web account is a strict
security improvement; restricting it would only weaken posture. **How to apply:**
don't add a role gate to these routes expecting it to be a fix — it's deliberate.

## TOTP / recovery codes
TOTP via pyotp, `verify_code` uses `valid_window=1`. Recovery codes stored as
sha256-hashed JSON; `consume_recovery_code(hashed_json, code)` returns
`(used: bool, new_json)` and must be persisted back (one-time use). Email-login 2FA
branch lives in email_auth.py and reuses login_nonce + rate-limit.

## Audit coverage
`web/audit.log_admin_action(request, user, action, target, details)` is best-effort
(never raises into the route). Hooked into: billing grant/revoke, org delete,
subscription cancel, backup create/delete. Viewer `/admin/audit` is super_admin-only.
Add a hook for any NEW high-impact super-admin mutation.

## Per-request CSRF
Token = `<nonce>.<hmac(secret, jwt+nonce)[:32]>`, unique per render, HMAC-bound to
the session JWT. `verify_csrf_token` accepts form field OR `X-CSRF-Token` header
(htmx attaches it via `htmx:configRequest`) and still accepts the legacy 32-char
deterministic token for back-compat (open tabs don't break).
