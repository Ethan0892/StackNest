#!/usr/bin/env bash
# =============================================================================
# deploy/update.sh — Zero-downtime code update on Hetzner
#
# Run as root (or with sudo) on the Hetzner server:
#   bash /opt/stacknest/deploy/update.sh
#
# What it does:
#   1. git pull latest code
#   2. pip install any new deps (API-only)
#   3. Reload gunicorn in-place (no downtime — SIGHUP)
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/stacknest}"
APP_USER="${APP_USER:-stacknest}"
BRANCH="${BRANCH:-main}"
VENV="${APP_DIR}/.venv"
PIP="${VENV}/bin/pip"

green() { echo -e "\033[0;32m==> $*\033[0m"; }

[[ $EUID -ne 0 ]] && { echo "Run as root."; exit 1; }

green "Pulling latest code (branch: $BRANCH)..."
git -C "$APP_DIR" fetch origin

# ── Safety: backup the DB before any code update ──────────────────────────────
DB="$APP_DIR/data/stacknest.db"
if [[ -f "$DB" ]]; then
  BACKUP="$APP_DIR/data/backups/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ).db"
  mkdir -p "$APP_DIR/data/backups"
  sqlite3 "$DB" ".backup '$BACKUP'" && green "DB backed up to $BACKUP" || echo "WARNING: DB backup failed"
fi
# ──────────────────────────────────────────────────────────────────────────────

git -C "$APP_DIR" reset --hard "origin/$BRANCH"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

green "Installing any new Python dependencies..."
REQS="$APP_DIR/deploy/requirements-api.txt"
[[ ! -f "$REQS" ]] && REQS="$APP_DIR/requirements.txt"
sudo -u "$APP_USER" "$PIP" install -r "$REQS" --quiet

green "Reloading gunicorn (graceful — no dropped requests)..."
systemctl reload stacknest || systemctl restart stacknest

sleep 1
if systemctl is-active --quiet stacknest; then
    green "stacknest is running. Done!"
else
    echo "stacknest failed to restart — check: journalctl -u stacknest -n 40"
    exit 1
fi
