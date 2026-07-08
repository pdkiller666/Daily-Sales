#!/bin/bash
# Post-merge setup: install/refresh Python dependencies after a task merge.
# The interpreter (nix-provided) is marked externally-managed, but packages
# actually land in the local .pythonlibs venv — pip needs --break-system-packages
# to bypass the interpreter-level guard. Idempotent: pip skips satisfied reqs.
set -e

python3 -m pip install --break-system-packages --quiet -r requirements.txt
