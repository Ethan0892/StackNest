#!/usr/bin/env bash
# =============================================================================
# deploy/deploy_hetzner.sh — StackNest full server setup for Hetzner (x86_64)
#
# Tested on: Ubuntu 24.04 LTS (amd64)
# Run as root on a fresh Hetzner VPS:
#   bash deploy/deploy_hetzner.sh
#
# What this does:
#   1. Hardens the server (ufw, SSH, fail2ban)
#   2. Installs system deps (Python 3.12, OpenJDK 21, Maven, nginx, certbot)
#   3. Creates a dedicated 'stacknest' system user
#   4. Clones the repo to /opt/stacknest
#   5. Creates a venv and installs API-only Python deps
#   6. Installs and enables systemd services (stacknest + llama-server)
#   7. Configures nginx as a reverse proxy
#   8. Optionally gets a Let's Encrypt TLS certificate
#
# Usage:
#   DOMAIN=yourdomain.com REPO_URL=git@github.com:you/stacknest.git bash deploy/deploy_hetzner.sh
#
# Required env vars:
#   DOMAIN      — your domain (e.g. stacknest.dev) — set to 'skip' to skip SSL
#   REPO_URL    — your git repo URL
#
# Optional env vars:
#   APP_USER    — system user to run the app (default: stacknest)
#   APP_DIR     — install directory (default: /opt/stacknest)
#   EMAIL       — certbot email for Let's Encrypt (default: admin@$DOMAIN)
#   BRANCH      — git branch to deploy (default: main)
# =============================================================================
set -euo pipefail

# ── Config defaults ──────────────────────────────────────────────────────────
APP_USER="${APP_USER:-stacknest}"
APP_DIR="${APP_DIR:-/opt/stacknest}"
DOMAIN="${DOMAIN:?'Set DOMAIN=yourdomain.com (or DOMAIN=skip to skip SSL)'}"
REPO_URL="${REPO_URL:?'Set REPO_URL=https://github.com/you/stacknest.git'}"
BRANCH="${BRANCH:-main}"
EMAIL="${EMAIL:-admin@${DOMAIN}}"
VENV="${APP_DIR}/.venv"
PY="${VENV}/bin/python3"
PIP="${VENV}/bin/pip"

# ── Colour helpers ────────────────────────────────────────────────────────────
green()  { echo -e "\033[0;32m==> $*\033[0m"; }
yellow() { echo -e "\033[0;33m  ! $*\033[0m"; }
red()    { echo -e "\033[0;31m  X $*\033[0m"; }

[[ $EUID -ne 0 ]] && { red "Run as root."; exit 1; }

# ── 1. System update & base packages ─────────────────────────────────────────
green "1/8  Updating system packages..."
apt-get update -q
apt-get upgrade -y -q
apt-get install -y -q \
    curl wget git unzip gnupg lsb-release ca-certificates \
    build-essential \
    python3.12 python3.12-venv python3.12-dev python3-pip \
    ufw fail2ban \
    nginx certbot python3-certbot-nginx \
    openjdk-21-jdk-headless maven

# ── 2. Firewall ───────────────────────────────────────────────────────────────
green "2/8  Configuring ufw firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw allow 80/tcp comment "HTTP"
ufw allow 443/tcp comment "HTTPS"
ufw --force enable
ufw status verbose

# ── 3. Fail2ban ──────────────────────────────────────────────────────────────
green "3/8  Configuring fail2ban..."
systemctl enable fail2ban
systemctl start fail2ban

# ── 4. App user ───────────────────────────────────────────────────────────────
green "4/8  Setting up app user '$APP_USER'..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" --create-home "$APP_USER"
    green "  Created user $APP_USER"
else
    yellow "  User $APP_USER already exists — skipping"
fi

# ── 5. Clone / update repo ────────────────────────────────────────────────────
green "5/8  Deploying app to $APP_DIR..."
if [[ -d "$APP_DIR/.git" ]]; then
    yellow "  Repo exists — pulling latest from $BRANCH..."
    git -C "$APP_DIR" fetch origin
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
    green "  Cloning $REPO_URL → $APP_DIR..."
    git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
fi

# Create required data dirs
mkdir -p "$APP_DIR/data" "$APP_DIR/libs" "$APP_DIR/models"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 755 "$APP_DIR"                       # nginx (www-data) needs +x to traverse
chmod 755 "$APP_DIR/frontend"              # nginx needs to read static files
find "$APP_DIR/frontend" -type f -exec chmod 644 {} \;

# ── 6. Python venv + deps ─────────────────────────────────────────────────────
green "6/8  Installing Python dependencies..."
if [[ ! -d "$VENV" ]]; then
    sudo -u "$APP_USER" python3.12 -m venv "$VENV"
fi

# Use API-only requirements (no torch/training deps)
REQS="$APP_DIR/deploy/requirements-api.txt"
[[ ! -f "$REQS" ]] && REQS="$APP_DIR/requirements.txt"

sudo -u "$APP_USER" "$PIP" install --upgrade pip wheel --quiet
sudo -u "$APP_USER" "$PIP" install -r "$REQS" --quiet

# ── 7. Systemd services ───────────────────────────────────────────────────────
green "7/8  Installing systemd services..."

# Substitute real paths into the service files
sed "s|/opt/stacknest|$APP_DIR|g; s|User=stacknest|User=$APP_USER|g" \
    "$APP_DIR/deploy/stacknest.service" \
    > /etc/systemd/system/stacknest.service

# Only install llama-server if the binary exists
if [[ -f "$APP_DIR/libs/llama-server" ]]; then
    sed "s|/opt/stacknest|$APP_DIR|g; s|User=stacknest|User=$APP_USER|g" \
        "$APP_DIR/deploy/llama-server.service" \
        > /etc/systemd/system/llama-server.service
    systemctl daemon-reload
    systemctl enable llama-server
    systemctl restart llama-server
    green "  llama-server service installed."
else
    yellow "  libs/llama-server not found — skipping llama service."
    yellow "  Download x86_64 binary: https://github.com/ggml-org/llama.cpp/releases"
    yellow "  Then: place in $APP_DIR/libs/llama-server && chmod +x"
fi

systemctl daemon-reload
systemctl enable stacknest
systemctl restart stacknest
sleep 2

if systemctl is-active --quiet stacknest; then
    green "  stacknest service is RUNNING"
else
    red "  stacknest service FAILED — run: journalctl -u stacknest -n 50"
fi

# ── 8. nginx + TLS ────────────────────────────────────────────────────────────
green "8/8  Configuring nginx..."

# Install vhost config
sed "s|__DOMAIN__|$DOMAIN|g" "$APP_DIR/deploy/nginx.stacknest.conf" \
    > /etc/nginx/sites-available/stacknest

ln -sf /etc/nginx/sites-available/stacknest /etc/nginx/sites-enabled/stacknest
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl reload nginx

if [[ "$DOMAIN" != "skip" ]]; then
    green "  Obtaining Let's Encrypt certificate for $DOMAIN..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" \
        --redirect || yellow "  certbot failed — run manually: certbot --nginx -d $DOMAIN"
    systemctl reload nginx
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
green "====================================================="
green " Deployment complete!"
green "====================================================="
echo ""
echo "  App dir:   $APP_DIR"
echo "  App user:  $APP_USER"
echo "  Venv:      $VENV"
echo "  Domain:    $DOMAIN"
echo ""
echo "  Next steps:"
echo "  1. Copy your production .env to $APP_DIR/.env"
echo "     (see deploy/.env.production.example)"
echo "  2. sudo systemctl restart stacknest"
echo "  3. Check health: curl http://localhost:5000/api/health"
if [[ ! -f "$APP_DIR/libs/llama-server" ]]; then
    echo ""
    echo "  For local inference:"
    echo "  wget https://github.com/ggml-org/llama.cpp/releases/latest/download/llama-b\$(curl -s https://api.github.com/repos/ggml-org/llama.cpp/releases/latest | grep tag_name | cut -d'\"' -f4 | tr -d 'b')-bin-ubuntu-x64.zip"
    echo "  unzip *.zip && cp build/bin/llama-server $APP_DIR/libs/ && chmod +x $APP_DIR/libs/llama-server"
fi
echo ""
