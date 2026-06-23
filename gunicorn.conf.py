# gunicorn.conf.py — Production WSGI server config for StackNest
# Usage: gunicorn -c gunicorn.conf.py api.app:app

import multiprocessing

# ── Binding ────────────────────────────────────────────────────────────────
# Behind nginx: listen on localhost only — never expose gunicorn directly
bind    = "127.0.0.1:5000"
workers = multiprocessing.cpu_count() * 2 + 1  # auto-scales to server CPU count

# ── Worker class ──────────────────────────────────────────────────────────
worker_class = "sync"              # sync is safe for SQLite + blocking inference
timeout      = 720                 # 12 min — complex plugins need Gemini×3 + Kimi heal + compile passes (can exceed 7 min)
keepalive    = 5

# ── Forwarded IPs (nginx proxy) ──────────────────────────────────────────
# Trust X-Forwarded-For from any local proxy so real IPs appear in logs
forwarded_allow_ips = "127.0.0.1"

# ── Logging ───────────────────────────────────────────────────────────────
accesslog  = "-"                   # stdout → systemd journal
errorlog   = "-"
loglevel   = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Pid / reload ──────────────────────────────────────────────────────────
pidfile    = "/tmp/stacknest.pid"
reload     = False                 # set True in dev to auto-reload on code change

# ── Process title ─────────────────────────────────────────────────────────
proc_name  = "stacknest"
