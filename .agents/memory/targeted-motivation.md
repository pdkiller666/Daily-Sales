---
name: Targeted motivation by org structure
description: How product motivation resolves by seller scope, and the backward-compat contract
---

Product motivation supports scope targeting (global / trade_network / city / shop / user)
via a dedicated rules table that superseded the old single-rate motivation table
(auto-migrated existing rates → global so nothing changes for existing orgs).

**Resolution priority (most specific wins):** user > shop > city > trade_network > global.
The resolver takes seller attributes (the seller's network/shop/city + user id) and
returns the winning rate plus *which scope* it came from.

**Why this matters:** orgs that never set a targeted rule must behave exactly as before —
a global-only setup resolves identically to the legacy single-rate path. Any commission
caller that does not pass seller attributes therefore falls back to the global rate by design.

**How to apply:**
- The earnings row now carries the winning scope as an extra trailing field. Treat the
  earnings tuple as append-only: consumers must use index access or star-unpack
  (`*rest = row`), never fixed-length unpacking — adding a field broke bot earnings
  screens once (ValueError on 8-value unpack).
- GS motivation import writes per-network (trade_network) rules; rows with no matching
  product are skipped, and recalculation is deferred to the end of the batch.
- New sale / recalculation consumer sites should pass seller attributes so targeted
  rates apply instead of silently falling back to global.
