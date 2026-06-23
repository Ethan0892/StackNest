#!/usr/bin/env bash
set -euo pipefail

# Creates a timestamped backup tarball for StackNest runtime data.
# Usage:
#   bash scripts/backup_stacknest.sh /home/ethan/stacknest /home/ethan/backups

STACKNEST_ROOT="${1:-/home/ethan/stacknest}"
BACKUP_DIR="${2:-$STACKNEST_ROOT/backups}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/stacknest-backup-$TS.tar.gz"

mkdir -p "$BACKUP_DIR"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Copy critical runtime artifacts
mkdir -p "$TMP_DIR/stacknest"
cp -a "$STACKNEST_ROOT/.env" "$TMP_DIR/stacknest/.env" 2>/dev/null || true
cp -a "$STACKNEST_ROOT/.env.example" "$TMP_DIR/stacknest/.env.example" 2>/dev/null || true
cp -a "$STACKNEST_ROOT/stacknest.service" "$TMP_DIR/stacknest/stacknest.service" 2>/dev/null || true
cp -a "$STACKNEST_ROOT/llama-server.service" "$TMP_DIR/stacknest/llama-server.service" 2>/dev/null || true

if [[ -d "$STACKNEST_ROOT/data" ]]; then
  cp -a "$STACKNEST_ROOT/data" "$TMP_DIR/stacknest/data"
fi

if [[ -d "$STACKNEST_ROOT/models" ]]; then
  cp -a "$STACKNEST_ROOT/models" "$TMP_DIR/stacknest/models"
fi

# Optional: sqlite online backup if DB exists and sqlite3 is available
DB_PATH="${STACKNEST_DB:-$STACKNEST_ROOT/data/stacknest.db}"
if [[ -f "$DB_PATH" ]] && command -v sqlite3 >/dev/null 2>&1; then
  mkdir -p "$TMP_DIR/stacknest/sqlite-backup"
  sqlite3 "$DB_PATH" ".backup '$TMP_DIR/stacknest/sqlite-backup/stacknest.db.bak'"
fi

# Create archive
tar -C "$TMP_DIR" -czf "$OUT" stacknest

sha256sum "$OUT" > "$OUT.sha256"

echo "Backup created: $OUT"
echo "Checksum: $OUT.sha256"
