#!/usr/bin/env bash
# deploy_prod.sh — Legacy Pi deployment script
# Deprecated: current production uses deploy/deploy_hetzner.sh and /opt/stacknest.
# To run this script intentionally, set STACKNEST_ALLOW_LEGACY_DEPLOY=1.
set -euo pipefail

if [[ "${STACKNEST_ALLOW_LEGACY_DEPLOY:-}" != "1" ]]; then
  echo "This is a legacy Pi deployment script and is disabled by default."
  echo "Use deploy/deploy_hetzner.sh for current production, or set STACKNEST_ALLOW_LEGACY_DEPLOY=1 to run anyway."
  exit 1
fi

VENV=/home/ethan/stacknest/.venv
APP=/home/ethan/stacknest
SERVICE=stacknest

echo "==> Installing gunicorn..."
"$VENV/bin/pip" install --quiet gunicorn

echo "==> Checking Flask + deps..."
"$VENV/bin/pip" install --quiet flask flask-cors flask-limiter python-dotenv

echo "==> Writing systemd service..."
sudo tee /etc/systemd/system/stacknest.service > /dev/null <<'EOF'
[Unit]
Description=StackNest Plugin Generator API
After=network.target

[Service]
Type=simple
User=ethan
WorkingDirectory=/home/ethan/stacknest
Environment="PATH=/home/ethan/stacknest/.venv/bin:/usr/bin:/bin"
EnvironmentFile=-/home/ethan/stacknest/.env
ExecStart=/home/ethan/stacknest/.venv/bin/gunicorn -c gunicorn.conf.py api.app:app
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=stacknest

[Install]
WantedBy=multi-user.target
EOF

echo "==> Reloading systemd..."
sudo systemctl daemon-reload

echo "==> Stopping any nohup Flask processes..."
pkill -f "python3 api/app.py" 2>/dev/null || true
pkill -f "gunicorn.*api.app" 2>/dev/null || true

echo "==> Starting stacknest service via systemd..."
sudo systemctl enable stacknest
sudo systemctl restart stacknest
sleep 2

if sudo systemctl is-active --quiet stacknest; then
  echo "==> stacknest service is RUNNING ✅"
  sudo systemctl status stacknest --no-pager -l | head -20
else
  echo "==> stacknest service FAILED to start ❌"
  sudo systemctl status stacknest --no-pager -l
  exit 1
fi

echo ""
echo "==> Health check..."
sleep 1
curl -sf http://localhost:5000/api/health | python3 -m json.tool || echo "(curl failed — check logs)"
echo ""
echo "Done! StackNest is running under systemd + gunicorn."
