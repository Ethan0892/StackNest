#!/bin/bash
# Safe deploy script for StackNest production
# Blocks destructive git commands and ensures DB backup before deploy
set -euo pipefail

# Only allow running as root or stacknest user
if [[ $EUID -ne 0 && $(whoami) != "stacknest" ]]; then
  echo "Must run as root or stacknest user" >&2
  exit 1
fi

cd /opt/stacknest

# Backup DB before pulling
if [[ -f data/stacknest.db ]]; then
  ts=$(date +%Y%m%d_%H%M%S)
  cp -a data/stacknest.db data/stacknest.db.predeploy_$ts
  echo "Database backed up to data/stacknest.db.predeploy_$ts"
fi

echo "Pulling latest code from main..."
git pull --ff-only origin main

echo "Restarting stacknest service..."
systemctl restart stacknest
sleep 5
systemctl is-active stacknest

# Health check
curl -sS http://localhost:5000/api/health || {
  echo "Health check failed after deploy!" >&2
  exit 3
}
echo "Deploy complete and healthy."
