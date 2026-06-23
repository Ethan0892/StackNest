# StackNest Server — Operations Guide

## SSH Access

| Field    | Value                        |
|----------|------------------------------|
| Host     | `your-server-ip`             |
| Port     | `2222` (not the default 22)  |
| User     | `root`                       |
| Auth     | SSH key (`~/.ssh/id_ed25519`) |

```bash
ssh -p 2222 root@your-server-ip
```

Add your public key to `/root/.ssh/authorized_keys` on the server.

---

## App Location

```
/opt/stacknest/
```

Key paths:
| Path                          | Purpose                          |
|-------------------------------|----------------------------------|
| `/opt/stacknest/api/`         | Flask/Python backend             |
| `/opt/stacknest/frontend/`    | Static HTML/JS frontend          |
| `/opt/stacknest/data/`        | SQLite DB, avatars, backups      |
| `/opt/stacknest/.env`         | Live environment config (secret) |
| `/opt/stacknest/.venv/`       | Python virtualenv                |
| `/opt/stacknest/hosted_bots/` | Hosted Discord bot instances     |

---

## Service Management

The app runs under `systemd` as the `stacknest` service.

```bash
# Status
systemctl status stacknest

# Restart (e.g. after a deploy)
systemctl restart stacknest

# Stop / Start
systemctl stop stacknest
systemctl start stacknest

# Live logs (follow)
journalctl -u stacknest -f

# Last 100 log lines
journalctl -u stacknest -n 100 --no-pager
```

---

## Deploying Latest Code

```bash
ssh -p 2222 root@your-server-ip
cd /opt/stacknest
git pull --ff-only origin main
systemctl restart stacknest
```

If `git pull` fails due to local changes on the server:
```bash
git stash --include-untracked   # saves server-side changes safely
git pull --ff-only origin main
systemctl restart stacknest
# To review what was stashed:
git stash show -p stash@{0}
```

---

## GitHub Repository

**URL:** https://github.com/your-username/StackNest

The server pulls via SSH key (`/root/.ssh/id_ed25519`).  
Your local machine pushes via HTTPS with GitHub credentials.

Typical workflow:
1. Edit locally in `/path/to/local/StackNest`
2. `git add` / `git commit` / `git push`
3. SSH to server → `git pull` → `systemctl restart stacknest`

---

## Admin Panel

Accessible at `https://stacknest.app/admin`

Access is restricted by IP — only IPs listed in `ADMIN_ALLOW_NETWORKS`
in `/opt/stacknest/.env` can reach it. Add your IP to that variable to
gain access.

The admin secret is set via `ADMIN_SECRET` in `.env`.

---

## Health Check

```bash
curl -s http://localhost:5000/api/health | python3 -m json.tool
```

(Run on the server itself — gunicorn binds to localhost, nginx faces the internet.)

---

## Nginx

Nginx proxies public traffic to gunicorn on `localhost:5000`.

```bash
systemctl status nginx
systemctl reload nginx        # apply config changes without downtime
nginx -t                      # test config syntax before reloading
```

Config location: `/etc/nginx/sites-enabled/` or `/etc/nginx/conf.d/`

---

## Environment Config

Live config file: `/opt/stacknest/.env`

**Never commit this file to git** — it contains secrets.
It is listed in `.gitignore`.

To edit on server:
```bash
nano /opt/stacknest/.env
systemctl restart stacknest
```

Key variables to know:
| Variable               | Purpose                                      |
|------------------------|----------------------------------------------|
| `ADMIN_ALLOW_NETWORKS` | IPs/CIDRs allowed to reach `/admin`          |
| `APP_BASE_URL`         | Public URL used in verification emails       |
| `ADMIN_SECRET`         | Password for the admin panel                 |
| `USER_AUTH_SECRET`     | JWT signing secret for user sessions         |
| `SMTP_*`               | Transactional email (Brevo SMTP relay)       |
| `GEMINI_API_KEY`       | Free-tier AI generation backend              |
| `CLAUDE_API_KEY`       | Premium-tier AI generation backend           |
| `STRIPE_SECRET_KEY`    | Payments                                     |
| `DISCORD_BOT_TOKEN`    | Discord integration                          |

---

## Hetzner Cloud Console

Emergency web terminal (no SSH needed):
https://console.hetzner.cloud → Project → Server → Console tab

Useful when:
- SSH key isn't installed yet
- Port 2222 is unreachable
- Need to reboot or fix sshd config
