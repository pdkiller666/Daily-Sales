---
name: Standalone test scripts not wired to deploy
description: Where regression tests live and why they don't run automatically before deploy
---

The project's regression tests are standalone top-level `test_*.py` scripts (e.g. `test_variants.py`,
`test_label_presets.py`, `test_package_refunds.py`) that each print a pass/fail summary and
`sys.exit(1)` on failure. They are NOT collected by pytest and there is no central runner.

`deploy.sh` only invokes two of them explicitly (`test_build_changelog.py`, `test_web_smoke.py`
gated behind `WEB_SMOKE_REQUIRE_BROWSER=1`). All other `test_*.py` files must be run manually
(`python3 test_whatever.py`) — they do not block deploys and won't catch a regression unless
someone remembers to run them.

**Why:** discovered while adding `test_package_refunds.py` for subscription-refund regression
coverage — there was no existing hook to wire it into.

**How to apply:** after adding a new `test_*.py`, run it manually to confirm it's green, and don't
assume it will run again automatically. A follow-up task exists to make deploy.sh discover and run
all `test_*.py` files (see project follow-up: "Make sure existing regression tests actually run
before every deploy").
