#!/bin/bash
set -e

# Post-merge setup for DailySales bot
# Installs/syncs Python dependencies (no-op if already installed)
pip install -q -r requirements.txt --no-input 2>/dev/null || true

echo "Post-merge setup complete."
