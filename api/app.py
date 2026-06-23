"""
api/app.py — Flask REST API for the StackNest plugin generator.

Endpoints:
  POST /api/generate        — Generate a plugin (blocking, with validation loop)
  POST /api/stream          — Stream tokens (SSE, no validation)
  POST /api/validate        — Validate + Kimi K2.5 deep-check existing code
  POST /api/heal            — Context-Aware Healing: fix errors automatically
    POST /api/jar             — Compile code → download ready-to-deploy .jar
    POST /api/migrate         — Auto-migrate uploaded/GitHub plugin source to newer API
  POST /api/logs/analyze    — Analyse a Minecraft server log (like mclogs)
  GET  /api/gallery         — List public community gallery entries
  POST /api/gallery/submit  — Submit a plugin to the gallery
  GET  /api/gallery/<id>    — Get single gallery entry with full code
  POST /api/gallery/<id>/like — Increment likes
  GET  /api/health          — Health check (API + llama.cpp server + Kimi)
  GET  /api/status          — Queue depth, model info, feature flags

Rate limiting:
  Free tier:  3 plugin generations/month, 20 prompt previews/month per IP
  No auth required for now — add X-API-Key header for paid tiers

Usage:
  python api/app.py
  python api/app.py --port 5000 --debug
"""

import argparse
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import queue
import re
import secrets
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, jsonify, make_response, request, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env from project root so env vars are available everywhere
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from api.db import (
    log_request, get_requests, get_request_by_id, update_request, get_stats,
    get_ip_notes, set_ip_note, delete_ip_note, is_banned, is_bypassed,
    clear_old_logs,
    submit_gallery, get_gallery, get_gallery_entry, like_gallery,
    submit_gallery_community,
    create_user, get_user_by_email, get_user_by_id,
    get_user_by_verification_token, set_verification_token, set_user_verified,
    update_user_profile, update_user_password,
    save_user_project, list_user_projects, get_user_project, delete_user_project,
    set_meta, get_meta,
    set_user_plan, set_user_stripe_ids, get_user_by_stripe_customer_id,
    list_users, count_users,
    get_user_usage, increment_user_generation, check_user_generation_limit,
    get_daily_chart_data,
    create_oauth_user, get_user_by_google_id, set_user_google_id,
    set_user_discord, get_user_by_discord_id, unlink_user_discord,
    _conn,
    create_ticket, get_ticket, get_tickets, update_ticket_status, get_discord_stats,
    add_ticket_message, get_ticket_messages,
    add_user_bonus_gens,
    create_backup, verify_backup, restore_backup, list_backups,
    delete_backup, cleanup_old_backups, get_db_health,
    check_runtime_test_limit, increment_runtime_test, get_user_runtime_test_usage,
    is_rt_test_suspended, suspend_runtime_test,
    get_user_by_api_key, set_user_api_key, clear_user_api_key,
)
from api.mailer import send_verification_email, send_password_reset_email, send_ticket_reply as _send_ticket_reply
from api.migration import (
    MAX_UPLOAD_ZIP_BYTES,
    MigrationError,
    build_zip_bytes,
    extract_source_files,
    fetch_github_archive,
    migrate_sources,
)
from inference.router import PluginRouter

# ---------------------------------------------------------------------------
# Arti — AI away-mode agent
# ---------------------------------------------------------------------------
_ARTI_STATE_FILE = Path(__file__).parent.parent / "data" / "arti_mode.json"
_arti_lock = threading.Lock()

_ARTI_SYSTEM = (
    "You are Arti, the friendly AI support agent for StackNest — an AI-powered "
    "Minecraft plugin generator.\n"
    "Your job is to answer support tickets politely, helpfully, and concisely.\n\n"
    "StackNest facts:\n"
    "- Free tier: 3 plugins/month. Starter: 15. Pro: 100. Studio: 300.\n"
    "- Plugins are generated as Java source + plugin.yml compiled against Paper 26.1.\n"
    "- Supported platforms: Paper 26.1, Folia, Spigot, Purpur, Velocity, BungeeCord.\n"
    "- Users can download a ready-to-deploy .jar from the app (stacknests.com/app).\n"
    "- Billing / plan upgrades: stacknests.com/pricing\n"
    "- Support page: stacknests.com/support\n\n"
    "Rules:\n"
    "- Address the user's question directly and professionally.\n"
    "- Keep replies under 200 words.\n"
    "- Do NOT invent features that do not exist.\n"
    "- Sign every reply with: — Arti, StackNest Support\n"
)


def _load_arti_state() -> dict:
    try:
        if _ARTI_STATE_FILE.exists():
            return json.loads(_ARTI_STATE_FILE.read_text())
    except Exception:
        pass
    return {"enabled": False, "since": None, "message": "", "log": []}


def _save_arti_state(state: dict) -> None:
    _ARTI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ARTI_STATE_FILE.write_text(json.dumps(state, indent=2))


def _arti_log(state: dict, entry: str) -> None:
    log = state.get("log", [])
    log.insert(0, {"ts": time.time(), "msg": entry})
    state["log"] = log[:100]
from inference.server import GenerationParams, health_check, get_model_info
from api.docs_cache import get_doc_context, get_mod_doc_context, get_datapack_doc_context, warm_cache as _warm_doc_cache
from api.paper_versions import startup_refresh as _paper_startup_refresh
from validation.compile_check import build_jar
from validation.feedback_loop import PluginGenerator, run_validation_only

# --------------------------------------------------------------------------- #
# App setup                                                                    #
# --------------------------------------------------------------------------- #
app = Flask(
    __name__,
    static_folder=str(Path(__file__).parent.parent / "frontend"),
    static_url_path="/",
)

# Build/deploy version stamp — used by the frontend to detect redeployments
_APP_VERSION = str(int(time.time()))

# ---------------------------------------------------------------------------
# Security / deployment config
# ---------------------------------------------------------------------------
TRUST_PROXY = os.getenv("TRUST_PROXY", "false").lower() in {"1", "true", "yes", "on"}
if TRUST_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_MIN_REQUEST_BYTES = 12 * 1024 * 1024
MAX_REQUEST_BYTES = max(
    int(os.getenv("MAX_REQUEST_BYTES", str(_MIN_REQUEST_BYTES))),
    _MIN_REQUEST_BYTES,
)  # enforce at least 12 MiB so bot zip uploads are not blocked by stale env values
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

CORS_ALLOWED_ORIGINS = [
    x.strip() for x in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",") if x.strip()
]
if len(CORS_ALLOWED_ORIGINS) == 1 and CORS_ALLOWED_ORIGINS[0] == "*":
    CORS(app, resources={r"/api/*": {"origins": "*"}})
else:
    CORS(app, resources={r"/api/*": {"origins": CORS_ALLOWED_ORIGINS}})

ALLOWED_HOSTS = [
    x.strip().lower() for x in os.getenv("ALLOWED_HOSTS", "").split(",") if x.strip()
]

SECURITY_CSP = os.getenv(
    "SECURITY_CSP",
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://accounts.google.com https://apis.google.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https://*.googleusercontent.com; "
    "connect-src 'self' https://accounts.google.com; "
    "frame-src https://accounts.google.com; "
    "base-uri 'self'; "
    "frame-ancestors 'none'",
)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()

# Discord OAuth config
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_GUILD_ID      = os.getenv("DISCORD_GUILD_ID", "").strip()
DISCORD_BOT_TOKEN_V   = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_INVITE_URL    = os.getenv("DISCORD_INVITE", "https://discord.gg/stacknest").strip()
DISCORD_LINKED_ROLE          = "1479960075488723076"
DISCORD_GALLERY_WEBHOOK_URL  = os.getenv("DISCORD_GALLERY_WEBHOOK_URL", "").strip()
DISCORD_STARTER_ROLE_ID      = os.getenv("DISCORD_STARTER_ROLE_ID",     "").strip()
DISCORD_PRO_ROLE_ID          = os.getenv("DISCORD_PRO_ROLE_ID",         "").strip()
DISCORD_STUDIO_ROLE_ID       = os.getenv("DISCORD_STUDIO_ROLE_ID",      "").strip()
DISCORD_REDIRECT_URI         = "https://stacknests.com/api/auth/discord/callback"

FORCE_HSTS = os.getenv("FORCE_HSTS", "false").lower() in {"1", "true", "yes", "on"}
HSTS_MAX_AGE = int(os.getenv("HSTS_MAX_AGE", "31536000"))

# ── Admin network allowlist ───────────────────────────────────────────────
# Optionally restrict /admin/* to specific IPs/CIDRs (comma-separated).
# Default: empty = no IP restriction; security relies on ADMIN_SECRET password
# + HMAC-signed cookie.  Set e.g. "1.2.3.4,10.0.0.0/8" to lock down further.
ADMIN_ALLOW_NETWORKS_RAW = os.getenv(
    "ADMIN_ALLOW_NETWORKS",
    "",
)
_ADMIN_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
for _cidr in ADMIN_ALLOW_NETWORKS_RAW.split(","):
    _cidr = _cidr.strip()
    if _cidr:
        try:
            _ADMIN_NETS.append(ipaddress.ip_network(_cidr, strict=False))
        except ValueError:
            pass


def _ip_allowed_for_admin(ip: str) -> bool:
    """Return True if ip is within ADMIN_ALLOW_NETWORKS."""
    if not _ADMIN_NETS:
        return True  # no restriction — open
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _ADMIN_NETS)
    except ValueError:
        return False

ADMIN_COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
ADMIN_COOKIE_SAMESITE = os.getenv("ADMIN_COOKIE_SAMESITE", "Strict")


@app.before_request
def _security_preflight():
    # Block invalid Host headers
    if ALLOWED_HOSTS:
        host = (request.host or "").split(":", 1)[0].lower()
        if host not in ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
            return jsonify({"error": "Invalid host header"}), 400

    # Optional IP allowlist for admin.  If ADMIN_ALLOW_NETWORKS is set,
    # block any request to /admin/* that comes from an unrecognised IP AND
    # does not carry a valid admin cookie (already-authenticated sessions
    # are always passed through).
    if _ADMIN_NETS and request.path.startswith("/admin"):
        client_ip = get_remote_address()
        if not _ip_allowed_for_admin(client_ip):
            # Allow through if caller already holds a valid admin cookie
            token = request.cookies.get("sn_admin", "")
            if not _verify_admin_token(token):
                return jsonify({"error": "Admin access is restricted to local network"}), 403


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Content-Security-Policy", SECURITY_CSP)

    if request.is_secure or FORCE_HSTS:
        resp.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={HSTS_MAX_AGE}; includeSubDomains",
        )
    return resp

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],               # No global limit — set per-route
    storage_uri="memory://",
)

# Single shared generator instance (lazy LLM connection)
_router = PluginRouter()
_generator = PluginGenerator(router=_router)

# Pre-warm PaperMC doc cache in the background
_warm_doc_cache()

# Auto-refresh Paper version metadata (downloads new JAR / brigadier if needed).
# Runs in a daemon thread — never blocks startup.
_paper_startup_refresh()

# Watchdog disabled — cloud-only inference, no local llama.cpp server.

# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------
# Set ADMIN_SECRET in .env to a strong password.  If not set, admin panel
# is disabled entirely (returns 503).  All tokens are HMAC-signed cookies.
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
ADMIN_TOKEN_TTL = int(os.getenv("ADMIN_TOKEN_TTL", "3600"))   # 1 hour
USER_AUTH_SECRET = os.getenv("USER_AUTH_SECRET", ADMIN_SECRET or "stacknest-dev-secret")
USER_TOKEN_TTL      = int(os.getenv("USER_TOKEN_TTL",     "7776000"))  # 90 days
USER_TOKEN_REFRESH  = int(os.getenv("USER_TOKEN_REFRESH", "604800"))   # renew if older than 7 days

_FAILED_LOGINS: dict[str, list[float]] = {}  # ip -> list of fail timestamps
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW = 900   # 15 min

# Log analysis limits (tunable via env)
LOG_ANALYSIS_MAX_CHARS = int(os.getenv("LOG_ANALYSIS_MAX_CHARS", "200000"))
LOG_ANALYSIS_MAX_ISSUES = int(os.getenv("LOG_ANALYSIS_MAX_ISSUES", "120"))


def _make_admin_token() -> str:
    """Create a signed token: timestamp.random.hmac"""
    ts = str(int(time.time()))
    rand = secrets.token_hex(16)
    payload = f"{ts}.{rand}"
    sig = hmac.new(ADMIN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_admin_token(token: str) -> bool:
    """Verify a token is valid and not expired."""
    if not ADMIN_SECRET or not token:
        return False
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        ts_str, rand, sig = parts
        payload = f"{ts_str}.{rand}"
        expected = hmac.new(ADMIN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        if time.time() - int(ts_str) > ADMIN_TOKEN_TTL:
            return False
        return True
    except Exception:
        return False


def _admin_required(fn):
    """Decorator: require a valid admin token cookie or 401. Records every access."""
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_SECRET:
            return jsonify({"error": "Admin panel disabled — set ADMIN_SECRET"}), 503
        token = request.cookies.get("sn_admin") or request.headers.get("X-Admin-Token", "")
        if not _verify_admin_token(token):
            return jsonify({"error": "Unauthorized"}), 401
        # Record this authenticated access (ip + timestamp + user-agent)
        try:
            access_ip = get_remote_address()
            set_meta("admin_last_access", json.dumps({
                "ip":    access_ip,
                "ts":    time.time(),
                "ua":    request.headers.get("User-Agent", "")[:120],
                "path":  request.path,
            }))
        except Exception:
            pass
        return fn(*args, **kwargs)
    return wrapper


def _check_login_throttle(ip: str) -> bool:
    """Return True if this IP is allowed to attempt login, False if throttled."""
    now = time.time()
    history = _FAILED_LOGINS.get(ip, [])
    # Prune old entries
    history = [t for t in history if now - t < _LOGIN_WINDOW]
    _FAILED_LOGINS[ip] = history
    return len(history) < _MAX_LOGIN_ATTEMPTS


def _record_failed_login(ip: str):
    _FAILED_LOGINS.setdefault(ip, []).append(time.time())


def _hash_password(password: str, salt_hex: str | None = None) -> str:
    if salt_hex is None:
        salt_hex = secrets.token_hex(16)
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()
    return f"{salt_hex}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest = stored.split("$", 1)
        return hmac.compare_digest(_hash_password(password, salt_hex).split("$", 1)[1], digest)
    except Exception:
        return False


def _make_user_token(user_id: int) -> str:
    ts = str(int(time.time()))
    rand = secrets.token_hex(12)
    payload = f"{ts}.{user_id}.{rand}"
    sig = hmac.new(USER_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_user_token(token: str) -> tuple[int | None, bool]:
    """
    Verify a user token.  Returns (user_id, needs_refresh).
    needs_refresh is True when the token is valid but older than USER_TOKEN_REFRESH,
    so the caller can issue a fresh token.
    """
    try:
        parts = (token or "").split(".")
        if len(parts) != 4:
            return None, False
        ts_str, user_id_str, rand, sig = parts
        payload = f"{ts_str}.{user_id_str}.{rand}"
        expected = hmac.new(USER_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None, False
        age = time.time() - int(ts_str)
        if age > USER_TOKEN_TTL:
            return None, False
        return int(user_id_str), (age > USER_TOKEN_REFRESH)
    except Exception:
        return None, False


def _bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    t = request.headers.get("X-User-Token", "").strip()
    if t:
        return t
    return request.args.get("token", "").strip()


def _current_user() -> dict | None:
    user_id, _ = _verify_user_token(_bearer_token())
    if not user_id:
        return None
    return get_user_by_id(user_id)


def _authenticated_user() -> dict | None:
    """Return the current user from JWT session, Authorization: Bearer, or X-API-Key."""
    user = _current_user()
    if user:
        return user
    # Prefer standard Bearer token; fall back to X-API-Key for backwards compat
    raw_key = ""
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        raw_key = auth_header[7:].strip()
    if not raw_key:
        raw_key = request.headers.get("X-API-Key", "").strip()
    if raw_key and raw_key not in VALID_PRO_KEYS:
        return get_user_by_api_key(raw_key)
    return None


def _user_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        request.stacknest_user = user
        return fn(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# Middleware — parse tier from API key header                                 #
# --------------------------------------------------------------------------- #

FREE_MONTHLY_LIMIT        = "2 per month"    # plugin generations for free users
FREE_STREAM_MONTHLY_LIMIT = "20 per month"   # prompt preview / stream uses for free users
PRO_DAILY_LIMIT           = "500 per day"    # generous for paid tier

# In production, replace with DB lookup
VALID_PRO_KEYS: set[str] = set(
    filter(None, os.getenv("PRO_API_KEYS", "").split(","))
)


def get_tier() -> str:
    """Return the effective tier based on X-API-Key header, user account plan, or bypass flag."""
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        if key in VALID_PRO_KEYS:               # legacy env-var keys still work
            return "pro"
        _key_user = get_user_by_api_key(key)    # hash lookup — raw key never in DB
        if _key_user:
            return _key_user.get("plan", "free")
    # Admin can bypass limits for specific IPs
    if is_bypassed(get_remote_address()):
        return "pro"
    # Logged-in user — return their actual plan
    user = _current_user()
    if user:
        return user.get("plan", "free")
    return "free"


_EDITOR_PLAN_ORDER = ["free", "starter", "pro", "studio"]
# Plans that can opt to keep generated plugins private in the gallery
_PAID_PLANS = {"starter", "pro", "studio"}
# Emails that receive a specific plan on account creation (e.g. recovered paying users)
_RESERVED_EMAIL_PLANS: dict[str, str] = {
    # Load per-user plan grants from the RESERVED_EMAIL_PLANS env var.
    # Format: "email1:plan1,email2:plan2"
    # Example: RESERVED_EMAIL_PLANS=alice@example.com:pro,bob@example.com:studio
    **{
        pair.split(":")[0].strip().lower(): pair.split(":")[1].strip().lower()
        for pair in os.getenv("RESERVED_EMAIL_PLANS", "").split(",")
        if ":" in pair
    },
}
_EDITOR_PLAN_LIMITS: dict[str, dict[str, int]] = {
    "free": {"max_files": 8, "max_chars": 35_000},
    "starter": {"max_files": 20, "max_chars": 120_000},
    "pro": {"max_files": 60, "max_chars": 400_000},
    "studio": {"max_files": 120, "max_chars": 900_000},
}


def _normalize_editor_plan(plan: str | None) -> str:
    raw = (plan or "free").strip().lower()
    if raw in _EDITOR_PLAN_LIMITS:
        return raw
    if raw in {"premium", "paid"}:
        return "pro"
    return "free"


def _effective_editor_plan() -> str:
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        if key in VALID_PRO_KEYS:
            return "pro"
        _key_user = get_user_by_api_key(key)
        if _key_user:
            return _normalize_editor_plan(_key_user.get("plan"))
    if is_bypassed(get_remote_address()):
        return "pro"
    user = _current_user()
    if user:
        return _normalize_editor_plan(user.get("plan"))
    return "free"


def _editor_plan_rank(plan: str | None) -> int:
    normalized = _normalize_editor_plan(plan)
    try:
        return _EDITOR_PLAN_ORDER.index(normalized)
    except ValueError:
        return 0


def _to_inference_tier(plan: str) -> str:
    """Map a Stripe plan name to the inference tier ('premium' or 'free').
    starter / pro / studio all unlock Claude-first generation."""
    return "premium" if plan in ("starter", "pro", "studio") else "free"


_BOT_HOSTING_LIMITS = {"free": 0, "starter": 0, "pro": 1, "studio": 3}


def _generate_api_key() -> str:
    """Generate a new Studio API key: 'sn_' + 40 hex chars (43 chars total)."""
    return "sn_" + secrets.token_hex(20)


def _bot_hosting_meta_key(user_id: int) -> str:
    return f"bot_hosting_override:{int(user_id)}"


def _get_bot_hosting_override(user_id: int) -> dict:
    """Return admin override for bot hosting, or defaults if unset/invalid."""
    row = get_meta(_bot_hosting_meta_key(user_id))
    if not row:
        return {"enabled": False, "limit": 0}
    try:
        data = json.loads(row.get("value", "") or "{}")
    except Exception:
        return {"enabled": False, "limit": 0}
    enabled = bool(data.get("enabled", False))
    try:
        limit = int(data.get("limit", 1))
    except Exception:
        limit = 1
    limit = max(0, min(limit, 10))
    return {"enabled": enabled, "limit": limit}


def _set_bot_hosting_override(user_id: int, enabled: bool, limit: int = 1) -> None:
    payload = {
        "enabled": bool(enabled),
        "limit": max(0, min(int(limit), 10)),
        "updated_at": time.time(),
    }
    set_meta(_bot_hosting_meta_key(user_id), json.dumps(payload))


def _bot_hosting_access(user: dict | None) -> dict:
    """Resolve final bot-hosting allowance from plan + optional admin override."""
    if not user:
        return {
            "allowed": False,
            "limit": 0,
            "source": "plan",
            "plan_limit": 0,
            "override": {"enabled": False, "limit": 0},
        }

    plan = _normalize_editor_plan(user.get("plan"))
    plan_limit = _BOT_HOSTING_LIMITS.get(plan, 0)
    override = _get_bot_hosting_override(int(user["id"]))

    if override["enabled"]:
        final_limit = max(plan_limit, override["limit"] or 1)
        source = "override"
    else:
        final_limit = plan_limit
        source = "plan"

    return {
        "allowed": final_limit > 0,
        "limit": final_limit,
        "source": source,
        "plan_limit": plan_limit,
        "override": override,
    }


_HOSTED_BOTS_ROOT = Path(__file__).parent.parent / "data" / "hosted_bots"
_HOSTED_BOT_MAX_CODE_CHARS = 220_000
_HOSTED_BOT_DEFAULT_RAM_GB = 1
_HOSTED_BOT_UPLOAD_MAX_BYTES = 12 * 1024 * 1024
_HOSTED_BOT_ZIP_MAX_MEMBERS = 1200
_HOSTED_BOT_ZIP_MAX_UNCOMPRESSED_BYTES = 24 * 1024 * 1024
_HOSTED_BOT_PROJECT_MAX_FILES = 400
_HOSTED_BOT_TEXT_EXTS = {
    ".py", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".txt", ".md", ".env", ".csv",
}
_HOSTED_BOT_MAX_FILE_READ_BYTES = 220_000
_HOSTED_BOT_MAX_FILE_WRITE_BYTES = 260_000
_HOSTED_BOT_UPLOAD_STAGING_ROOT = Path(__file__).parent.parent / "data" / "hosted_bot_uploads"
_HOSTED_BOT_PIP_TIMEOUT_SECS = 300
_HOSTED_BOT_MAX_PACKAGE_SPECS = 40
_HOSTED_BOT_PACKAGE_SPEC_RE = re.compile(r"^[A-Za-z0-9_.\-\[\],<>=!~]+$")
_HOSTED_BOT_NPM_SPEC_RE = re.compile(r"^[A-Za-z0-9@._/\-<>=~^]+$")
_HOSTED_BOT_INSTALL_TIMEOUT_SECS = 300
_HOSTED_BOT_METRICS_HISTORY_MAX = 360
_HOSTED_BOT_METRICS_HISTORY_RETURN = 90
_HOSTED_BOT_BASE_PORT_ALLOWANCE = 1
_HOSTED_BOT_MAX_EXTRA_PORTS = 20
_HOSTED_BOT_MAX_PORTS_PER_BOT = 8
_HOSTED_BOT_ALLOWED_PORT_MIN = 1024
_HOSTED_BOT_ALLOWED_PORT_MAX = 65535
_HOSTED_BOT_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".mp3", ".wav", ".ogg", ".ttf", ".woff", ".woff2",
}
_HOSTED_BOT_BLOCKED_FILE_EXTS = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".com", ".msi", ".apk", ".bin",
}
_HOSTED_BOT_BLOCKED_PATH_PARTS = {".git", ".svn", ".hg", ".ssh", ".vscode"}
_HOSTED_BOT_SUSPICIOUS_PATTERNS = [
    re.compile(r"(?is)powershell\s+-e(?:ncodedcommand)?"),
    re.compile(r"(?is)(?:os\.system|subprocess\.(?:run|popen|call)).{0,260}(?:curl|wget|powershell|certutil|bitsadmin)"),
    re.compile(r"(?is)(?:curl|wget).{0,180}\|\s*(?:bash|sh)"),
    re.compile(r"(?is)base64\.b64decode\(.{0,220}(?:exec\(|eval\()"),
    re.compile(r"(?is)from\s+Crypto\.Cipher\s+import\s+AES.{0,260}(?:requests\.|socket\.)"),
    re.compile(r"(?is)socket\.socket\(.{0,260}(?:connect|connect_ex)\("),
]


def _bot_ports_meta_key(bot_id: str) -> str:
    return f"hosted_bot_ports:{bot_id}"


def _user_extra_ports_meta_key(user_id: int) -> str:
    return f"hosted_bot_extra_ports:{int(user_id)}"


def _get_user_extra_ports(user_id: int) -> int:
    row = get_meta(_user_extra_ports_meta_key(user_id))
    if not row:
        return 0
    try:
        value = int((row.get("value") or "0").strip())
    except Exception:
        return 0
    return max(0, min(value, _HOSTED_BOT_MAX_EXTRA_PORTS))


def _set_user_extra_ports(user_id: int, extra_ports: int) -> None:
    value = max(0, min(int(extra_ports), _HOSTED_BOT_MAX_EXTRA_PORTS))
    set_meta(_user_extra_ports_meta_key(user_id), str(value))


def _user_port_quota(user_id: int) -> int:
    return _HOSTED_BOT_BASE_PORT_ALLOWANCE + _get_user_extra_ports(user_id)


def _sanitize_bot_ports(raw_ports: object, max_count: int = _HOSTED_BOT_MAX_PORTS_PER_BOT) -> list[int]:
    if raw_ports is None:
        return []
    if not isinstance(raw_ports, list):
        raise ValueError("ports must be a list of port numbers")

    out: list[int] = []
    seen: set[int] = set()
    for item in raw_ports:
        try:
            port = int(str(item).strip())
        except Exception:
            raise ValueError("Each port must be a valid integer.")
        if port < _HOSTED_BOT_ALLOWED_PORT_MIN or port > _HOSTED_BOT_ALLOWED_PORT_MAX:
            raise ValueError(f"Port {port} is outside allowed range {_HOSTED_BOT_ALLOWED_PORT_MIN}-{_HOSTED_BOT_ALLOWED_PORT_MAX}.")
        if port in seen:
            continue
        seen.add(port)
        out.append(port)
        if len(out) >= max_count:
            break
    return out


def _ports_from_start_flags(flags: str) -> list[int]:
    if not flags:
        return []
    hits = re.findall(r"(?:--port|-p|PORT=)\s*([0-9]{2,5})", str(flags), flags=re.IGNORECASE)
    if not hits:
        return []
    try:
        return _sanitize_bot_ports([int(x) for x in hits], max_count=_HOSTED_BOT_MAX_PORTS_PER_BOT)
    except ValueError:
        return []


def _ensure_ports_within_quota(user_id: int, ports: list[int]) -> None:
    quota = _user_port_quota(user_id)
    if len(ports) <= quota:
        return
    raise ValueError(
        f"Only {quota} port(s) are available on your account. Purchase extra ports to add more."
    )


def _get_bot_ports(bot_id: str) -> list[int]:
    row = get_meta(_bot_ports_meta_key(bot_id))
    if not row:
        return []
    try:
        payload = json.loads(row.get("value", "[]") or "[]")
        return _sanitize_bot_ports(payload)
    except Exception:
        return []


def _set_bot_ports(bot_id: str, ports: list[int]) -> None:
    set_meta(_bot_ports_meta_key(bot_id), json.dumps(ports))


def _scan_hosted_file_security(rel_path: str, content: str) -> None:
    path = str(rel_path or "").replace("\\", "/").strip().lower()
    if not path:
        raise ValueError("Invalid project file path.")
    parts = [p for p in path.split("/") if p]
    if any(p in _HOSTED_BOT_BLOCKED_PATH_PARTS for p in parts):
        raise ValueError(f"Blocked path in upload: {rel_path}")

    ext = Path(path).suffix.lower()
    if ext in _HOSTED_BOT_BLOCKED_FILE_EXTS:
        raise ValueError(f"Blocked executable/script file in upload: {rel_path}")

    text = str(content or "")
    sample = text[:120_000]
    for rule in _HOSTED_BOT_SUSPICIOUS_PATTERNS:
        if rule.search(sample):
            raise ValueError(f"Upload blocked by security scanner: suspicious code in {rel_path}")


def _scan_hosted_project_files_security(project_files: list[dict]) -> None:
    for item in project_files:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path", "") or "")
        content = str(item.get("content", "") or "")
        _scan_hosted_file_security(rel, content)


def _bot_project_root(row: dict) -> Path:
    return Path(row["token_path"]).parent


def _bot_flags_path(row: dict) -> Path:
    return _bot_project_root(row) / "start_flags.txt"


def _bot_dependency_root(row: dict) -> Path:
    return _bot_project_root(row) / ".stacknest" / "python"


def _bot_package_log_path(row: dict) -> Path:
    return _bot_project_root(row) / ".stacknest" / "package-installs.log"


def _bot_requirements_path(row: dict) -> Path:
    return _bot_project_root(row) / "requirements.txt"


def _bot_package_json_path(row: dict) -> Path:
    return _bot_project_root(row) / "package.json"


def _bot_metrics_history_path(row: dict) -> Path:
    return _bot_project_root(row) / ".stacknest" / "metrics-history.jsonl"


def _detect_bot_runtime(row: dict) -> str:
    suffix = Path(row.get("code_path", "bot.py")).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "node"
    if suffix in {".jar", ".java"}:
        return "java"
    return "custom"


def _read_bot_start_flags(row: dict) -> str:
    p = _bot_flags_path(row)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _write_bot_start_flags(row: dict, value: str) -> None:
    p = _bot_flags_path(row)
    p.write_text((value or "").strip() + "\n", encoding="utf-8")


def _read_requirements_lines(path: Path, limit: int = 80) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    out: list[str] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            out.append(line)
            if len(out) >= limit:
                break
    except Exception:
        return []
    return out


def _read_node_manifest_packages(path: Path, limit: int = 120) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []

    out: list[str] = []
    for section in ("dependencies", "devDependencies"):
        deps = payload.get(section)
        if not isinstance(deps, dict):
            continue
        for name, version in deps.items():
            pkg = str(name or "").strip()
            ver = str(version or "").strip()
            if not pkg:
                continue
            out.append(f"{pkg}@{ver}" if ver else pkg)
            if len(out) >= limit:
                return out
    return out


def _sanitize_package_specs(items: list[str], limit: int = _HOSTED_BOT_MAX_PACKAGE_SPECS) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()
    for raw in items:
        spec = str(raw or "").strip()
        if not spec:
            continue
        if len(spec) > 120 or not _HOSTED_BOT_PACKAGE_SPEC_RE.match(spec):
            raise ValueError(f"Invalid package spec: {spec}")
        if spec in seen:
            continue
        seen.add(spec)
        specs.append(spec)
        if len(specs) >= limit:
            break
    return specs


def _sanitize_npm_package_specs(items: list[str], limit: int = _HOSTED_BOT_MAX_PACKAGE_SPECS) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()
    for raw in items:
        spec = str(raw or "").strip()
        if not spec:
            continue
        if len(spec) > 160 or not _HOSTED_BOT_NPM_SPEC_RE.match(spec):
            raise ValueError(f"Invalid npm package spec: {spec}")
        if spec in seen:
            continue
        seen.add(spec)
        specs.append(spec)
        if len(specs) >= limit:
            break
    return specs


def _append_bot_package_log(row: dict, text: str) -> None:
    log_path = _bot_package_log_path(row)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", errors="ignore") as fh:
            fh.write(f"\n[{datetime.now(timezone.utc).isoformat()}] {text}\n")
    except Exception:
        # Never fail request paths because a log file cannot be written.
        return


def _tail_bot_package_log(row: dict, lines: int = 80) -> str:
    return _tail_lines(_bot_package_log_path(row), lines=lines)


def _bot_package_info(row: dict) -> dict:
    runtime = _detect_bot_runtime(row)
    req_path = _bot_requirements_path(row)
    package_json_path = _bot_package_json_path(row)
    dep_root = _bot_dependency_root(row)

    manager = ""
    declared: list[str] = []
    rel_req = ""
    install_supported = False

    if runtime == "python":
        manager = "pip"
        declared = _read_requirements_lines(req_path)
        install_supported = True
        try:
            if req_path.exists():
                rel_req = req_path.relative_to(_bot_project_root(row)).as_posix()
        except Exception:
            rel_req = req_path.name if req_path.exists() else ""
    elif runtime == "node":
        manager = "npm"
        declared = _read_node_manifest_packages(package_json_path)
        install_supported = True
        try:
            if package_json_path.exists():
                rel_req = package_json_path.relative_to(_bot_project_root(row)).as_posix()
        except Exception:
            rel_req = package_json_path.name if package_json_path.exists() else ""

    return {
        "runtime": runtime,
        "manager": manager,
        "requirements_path": rel_req,
        "manifest_path": rel_req,
        "declared_packages": declared,
        "declared_count": len(declared),
        "install_root": dep_root.relative_to(_bot_project_root(row)).as_posix(),
        "custom_install_supported": install_supported,
        "install_log": _tail_bot_package_log(row, lines=40),
    }


def _append_bot_metrics_sample(row: dict, metrics: dict) -> None:
    history_path = _bot_metrics_history_path(row)
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    sample = {
        "ts": int(time.time()),
        "live_status": metrics.get("live_status") or _hosted_bot_live_status(row),
        "cpu_pct": float(metrics.get("cpu_pct") or 0.0),
        "memory_mb": float(metrics.get("memory_mb") or 0.0),
        "threads": int(metrics.get("threads") or 0),
        "uptime_s": int(metrics.get("uptime_s") or 0),
        "available": bool(metrics.get("available")),
    }
    try:
        with history_path.open("a", encoding="utf-8", errors="ignore") as fh:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
    except Exception:
        return

    try:
        lines = history_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > _HOSTED_BOT_METRICS_HISTORY_MAX * 2:
            trimmed = lines[-_HOSTED_BOT_METRICS_HISTORY_MAX:]
            history_path.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
    except Exception:
        pass


def _bot_metrics_history(row: dict, limit: int = _HOSTED_BOT_METRICS_HISTORY_RETURN) -> list[dict]:
    history_path = _bot_metrics_history_path(row)
    if not history_path.exists():
        return []
    out: deque[dict] = deque(maxlen=max(10, min(limit, _HOSTED_BOT_METRICS_HISTORY_MAX)))
    try:
        with history_path.open("r", encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    sample = json.loads(line)
                except Exception:
                    continue
                if not isinstance(sample, dict):
                    continue
                out.append({
                    "ts": int(sample.get("ts") or 0),
                    "cpu_pct": float(sample.get("cpu_pct") or 0.0),
                    "memory_mb": float(sample.get("memory_mb") or 0.0),
                    "threads": int(sample.get("threads") or 0),
                    "uptime_s": int(sample.get("uptime_s") or 0),
                    "available": bool(sample.get("available")),
                    "live_status": sample.get("live_status") or "stopped",
                })
    except Exception:
        return []
    return list(out)


def _build_bot_file_tree(files: list[dict]) -> list[dict]:
    root: dict[str, dict] = {}
    for item in files:
        rel = str(item.get("path", "") or "").strip("/")
        if not rel:
            continue
        parts = [p for p in rel.split("/") if p]
        cursor = root
        current_path = []
        for idx, part in enumerate(parts):
            current_path.append(part)
            node = cursor.get(part)
            is_file = idx == len(parts) - 1
            if not node:
                node = {
                    "name": part,
                    "path": "/".join(current_path),
                    "type": "file" if is_file else "dir",
                }
                if not is_file:
                    node["children"] = {}
                else:
                    node["size"] = int(item.get("size", 0) or 0)
                cursor[part] = node
            if not is_file:
                cursor = node["children"]

    def _serialize(nodes: dict[str, dict]) -> list[dict]:
        ordered = sorted(nodes.values(), key=lambda n: (n["type"] == "file", n["name"].lower()))
        out: list[dict] = []
        for node in ordered:
            if node["type"] == "dir":
                out.append({
                    "name": node["name"],
                    "path": node["path"],
                    "type": "dir",
                    "children": _serialize(node["children"]),
                })
            else:
                out.append({
                    "name": node["name"],
                    "path": node["path"],
                    "type": "file",
                    "size": int(node.get("size", 0) or 0),
                })
        return out

    return _serialize(root)


def _hosted_bot_resource_snapshot(row: dict) -> dict:
    runtime = _detect_bot_runtime(row)
    live_status = _hosted_bot_live_status(row)
    snapshot = {
        "runtime": runtime,
        "live_status": live_status,
        "pid": row.get("pid"),
        "cpu_pct": 0.0,
        "memory_mb": 0.0,
        "memory_pct": 0.0,
        "threads": 0,
        "uptime_s": 0,
        "available": False,
    }
    pid = row.get("pid")
    if not pid or live_status != "running":
        return snapshot
    try:
        import psutil  # type: ignore

        proc = psutil.Process(int(pid))
        with proc.oneshot():
            mem = proc.memory_info()
            snapshot.update({
                "cpu_pct": round(float(proc.cpu_percent(interval=0.0)), 1),
                "memory_mb": round(float(mem.rss) / 1024 / 1024, 1),
                "memory_pct": round(float(proc.memory_percent()), 2),
                "threads": int(proc.num_threads()),
                "uptime_s": max(0, int(time.time() - float(proc.create_time()))),
                "available": True,
            })
    except Exception:
        return snapshot
    return snapshot


def _install_bot_python_packages(row: dict, package_specs: list[str]) -> tuple[bool, str]:
    specs = _sanitize_package_specs(package_specs)
    if not specs:
        return False, "No valid package specs provided."
    dep_root = _bot_dependency_root(row)
    try:
        dep_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False, "Unable to prepare bot dependency directory (permission denied)."
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        "--target",
        str(dep_root),
        *specs,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_bot_project_root(row)),
            capture_output=True,
            text=True,
            timeout=_HOSTED_BOT_PIP_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _append_bot_package_log(row, f"pip install timed out: {' '.join(specs)}")
        return False, "Package install timed out."

    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    _append_bot_package_log(row, f"pip install {' '.join(specs)}\n{output[:6000]}")
    if proc.returncode != 0:
        last_line = output.splitlines()[-1] if output else "pip install failed"
        return False, last_line[:300]
    return True, output or "Packages installed"


def _install_bot_node_packages(row: dict, package_specs: list[str], from_manifest: bool = False) -> tuple[bool, str]:
    try:
        check = subprocess.run(
            ["npm", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if check.returncode != 0:
            return False, "npm is not available on this server."
    except Exception:
        return False, "npm is not available on this server."

    specs = _sanitize_npm_package_specs(package_specs)
    try:
        (_bot_project_root(row) / ".stacknest").mkdir(parents=True, exist_ok=True)
    except Exception:
        return False, "Unable to prepare bot workspace directory (permission denied)."
    cmd = ["npm", "install", "--no-audit", "--no-fund"]
    if specs:
        cmd.extend(specs)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_bot_project_root(row)),
            capture_output=True,
            text=True,
            timeout=_HOSTED_BOT_INSTALL_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _append_bot_package_log(row, f"npm install timed out: {' '.join(specs) if specs else 'manifest install'}")
        return False, "npm install timed out."

    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    what = "manifest" if from_manifest else "custom"
    _append_bot_package_log(row, f"npm install ({what}) {' '.join(specs)}\n{output[:6000]}")
    if proc.returncode != 0:
        last_line = output.splitlines()[-1] if output else "npm install failed"
        return False, last_line[:300]
    return True, output or "Packages installed"


def _safe_bot_rel_path(root: Path, rel_path: str) -> Path:
    rel = str(rel_path or "").replace("\\", "/").strip()
    if not rel:
        raise ValueError("File path is required")
    if rel.startswith("/"):
        raise ValueError("Invalid file path")
    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("Invalid file path")
    out = root.joinpath(*parts)
    out.resolve().relative_to(root.resolve())
    return out


def _list_bot_project_files(row: dict, limit: int = 600) -> list[dict]:
    root = _bot_project_root(row)
    out: list[dict] = []
    for path in sorted(root.rglob("*")):
        if len(out) >= limit:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        lower = rel.lower()
        if lower in {"token.txt", "bot.log", "start_flags.txt"}:
            continue
        if "/__pycache__/" in lower:
            continue
        ext = path.suffix.lower()
        if ext and ext not in _HOSTED_BOT_TEXT_EXTS:
            continue
        try:
            size = path.stat().st_size
        except Exception:
            size = 0
        out.append({"path": rel, "size": int(size)})
    return out


def _hosted_upload_stage_path(user_id: int, upload_id: str) -> Path:
    return _HOSTED_BOT_UPLOAD_STAGING_ROOT / str(int(user_id)) / f"{upload_id}.json"


def _hosted_upload_stage_zip_path(user_id: int, upload_id: str) -> Path:
    return _HOSTED_BOT_UPLOAD_STAGING_ROOT / str(int(user_id)) / f"{upload_id}.zip"


def _cleanup_hosted_upload_staging(user_id: int, keep_upload_id: str | None = None) -> None:
    root = _HOSTED_BOT_UPLOAD_STAGING_ROOT / str(int(user_id))
    if not root.exists():
        return
    try:
        files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return
    kept = 0
    for path in files:
        if keep_upload_id and path.name == f"{keep_upload_id}.json":
            kept += 1
            continue
        if kept < 4:
            kept += 1
            continue
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def _stage_hosted_upload(user_id: int, payload: dict, zip_bytes: bytes | None = None) -> str:
    upload_id = uuid4().hex[:16]
    path = _hosted_upload_stage_path(user_id, upload_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record["upload_id"] = upload_id
    record["created_at"] = time.time()
    path.write_text(json.dumps(record), encoding="utf-8")
    if zip_bytes is not None:
        _hosted_upload_stage_zip_path(user_id, upload_id).write_bytes(zip_bytes)
    _cleanup_hosted_upload_staging(user_id, keep_upload_id=upload_id)
    return upload_id


def _load_staged_hosted_upload(user_id: int, upload_id: str | None) -> dict | None:
    if not upload_id:
        return None
    path = _hosted_upload_stage_path(user_id, upload_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_matching_recent_staged_upload(user_id: int, code: str) -> dict | None:
    root = _HOSTED_BOT_UPLOAD_STAGING_ROOT / str(int(user_id))
    if not root.exists():
        return None
    code_hash = hashlib.sha256((code or "").encode("utf-8")).hexdigest()
    try:
        files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return None
    now = time.time()
    for path in files[:6]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if now - float(payload.get("created_at", 0)) > 3600:
            continue
        if payload.get("entry_code_hash") == code_hash:
            return payload
    return None


def _extract_safe_zip_to_dir(zip_bytes: bytes, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    total_uncompressed = 0
    allowed_exts = _HOSTED_BOT_TEXT_EXTS | _HOSTED_BOT_ASSET_EXTS
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        infos = zf.infolist()
        if len(infos) > _HOSTED_BOT_ZIP_MAX_MEMBERS:
            raise ValueError("ZIP has too many files.")

        top_parts = set()
        cleaned = []
        for info in infos:
            raw_name = info.filename or ""
            if not raw_name:
                continue
            name = raw_name.replace("\\", "/")
            parts = [p for p in name.split("/") if p and p != "."]
            if not parts:
                continue
            if any(p == ".." for p in parts) or name.startswith("/"):
                raise ValueError("ZIP contains unsafe paths.")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("ZIP symlinks are not supported.")
            top_parts.add(parts[0])
            cleaned.append((info, parts))
            total_uncompressed += int(info.file_size or 0)
            if total_uncompressed > _HOSTED_BOT_ZIP_MAX_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP uncompressed size is too large.")

        trim_root = next(iter(top_parts)) if len(top_parts) == 1 else None

        for info, parts in cleaned:
            rel_parts = parts[1:] if trim_root and parts and parts[0] == trim_root else parts
            if not rel_parts:
                continue
            if any(str(p).lower() in _HOSTED_BOT_BLOCKED_PATH_PARTS for p in rel_parts):
                continue
            out = target_dir.joinpath(*rel_parts)
            out.resolve().relative_to(target_dir.resolve())
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue

            ext = out.suffix.lower()
            if ext in _HOSTED_BOT_BLOCKED_FILE_EXTS:
                continue
            if ext and ext not in allowed_exts:
                continue

            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _ensure_hosted_bots_table() -> None:
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS hosted_bots (
                id          TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                bot_name    TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'stopped',
                language    TEXT NOT NULL DEFAULT 'python',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                ram_gb      INTEGER NOT NULL DEFAULT 1,
                pid         INTEGER,
                last_ping   REAL,
                last_error  TEXT,
                code_path   TEXT NOT NULL,
                token_path  TEXT NOT NULL,
                log_path    TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_hosted_bots_user ON hosted_bots(user_id, created_at DESC)")


def _hosted_bot_live_status(row: dict) -> str:
    pid = row.get("pid")
    if not pid:
        return row.get("status") or "stopped"
    try:
        os.kill(int(pid), 0)
        return "running"
    except Exception:
        return "stopped"


def _tail_lines(path: Path, lines: int = 120) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-max(1, min(lines, 500)):])
    except Exception:
        return ""


def _fetch_bot_for_user(user_id: int, bot_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM hosted_bots WHERE id=? AND user_id=?",
            (bot_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def _update_bot_state(bot_id: str, **fields) -> None:
    if not fields:
        return
    allowed = {"status", "pid", "last_ping", "last_error", "updated_at"}
    keys = [k for k in fields if k in allowed]
    if not keys:
        return
    sets = ", ".join([f"{k}=?" for k in keys])
    vals = [fields[k] for k in keys]
    vals.append(bot_id)
    with _conn() as con:
        con.execute(f"UPDATE hosted_bots SET {sets} WHERE id=?", vals)


def _start_hosted_bot(row: dict) -> tuple[bool, str]:
    code_path = Path(row["code_path"])
    token_path = Path(row["token_path"])
    log_path = Path(row["log_path"])
    start_flags = _read_bot_start_flags(row)
    project_root = token_path.parent

    if not code_path.exists():
        return False, "Bot code file is missing."
    if not token_path.exists():
        return False, "Bot token file is missing."

    token = token_path.read_text(encoding="utf-8", errors="ignore").strip()
    if not token:
        return False, "Bot token is empty."

    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DISCORD_TOKEN"] = token
    runtime = _detect_bot_runtime(row)
    ports = _get_bot_ports(str(row.get("id", "")))
    env["STACKNEST_BOT_RUNTIME"] = runtime
    env["STACKNEST_ALLOWED_PORTS"] = ",".join(str(p) for p in ports)
    env["PYTHONUNBUFFERED"] = "1"
    py_paths = []
    dep_root = _bot_dependency_root(row)
    if dep_root.exists():
        py_paths.append(str(dep_root))
    py_paths.append(str(project_root))
    if env.get("PYTHONPATH"):
        py_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_paths)
    try:
        script_args = shlex.split(start_flags) if start_flags else []
    except ValueError as e:
        return False, f"Invalid start flags: {e}"

    if runtime == "python":
        launch_cmd = [sys.executable, str(code_path), *script_args]
    elif runtime == "node":
        launch_cmd = ["node", str(code_path), *script_args]
    else:
        return False, f"Unsupported runtime for hosted execution: {runtime}"

    with log_path.open("a", encoding="utf-8", errors="ignore") as lf:
        lf.write(f"\n[{datetime.now(timezone.utc).isoformat()}] starting bot {row['id']}\n")
        if start_flags:
            lf.write(f"[{datetime.now(timezone.utc).isoformat()}] flags: {start_flags}\n")
        lf.flush()
        proc = subprocess.Popen(
            launch_cmd,
            cwd=str(project_root),
            stdout=lf,
            stderr=lf,
            env=env,
            start_new_session=True,
        )

    # Guard against instant boot failure so UI does not incorrectly show "running".
    time.sleep(0.8)
    rc = proc.poll()
    if rc is not None:
        last_logs = _tail_lines(log_path, lines=40).strip()
        msg = f"Bot crashed on startup (exit {rc})."
        if last_logs:
            msg = f"{msg} {last_logs.splitlines()[-1][:240]}"
        _update_bot_state(
            row["id"],
            status="error",
            pid=None,
            last_error=msg,
            updated_at=time.time(),
        )
        return False, msg

    _update_bot_state(
        row["id"],
        status="running",
        pid=int(proc.pid),
        last_ping=time.time(),
        last_error="",
        updated_at=time.time(),
    )
    return True, "Bot started"


def _stop_hosted_bot(row: dict) -> tuple[bool, str]:
    pid = row.get("pid")
    if not pid:
        _update_bot_state(row["id"], status="stopped", pid=None, updated_at=time.time())
        return True, "Bot already stopped"

    try:
        os.kill(int(pid), signal.SIGTERM)
        deadline = time.time() + 6
        while time.time() < deadline:
            try:
                os.kill(int(pid), 0)
                time.sleep(0.2)
            except Exception:
                break
        else:
            os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass

    _update_bot_state(row["id"], status="stopped", pid=None, updated_at=time.time())
    return True, "Bot stopped"


def _extract_bot_project_from_zip(zip_bytes: bytes) -> dict:
    """Return safe project files and a selected Python entrypoint from ZIP upload."""
    candidates: list[tuple[str, str]] = []
    project_files: list[dict] = []
    total_uncompressed = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        infos = zf.infolist()
        if len(infos) > _HOSTED_BOT_ZIP_MAX_MEMBERS:
            raise ValueError("ZIP has too many files.")

        for info in infos:
            raw_name = info.filename or ""
            if not raw_name or info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted ZIP entries are not supported.")

            name = raw_name.replace("\\", "/")
            parts = [p for p in name.split("/") if p and p != "."]
            if not parts:
                continue
            if any(p == ".." for p in parts):
                raise ValueError("ZIP contains unsafe relative paths.")
            if name.startswith("/"):
                raise ValueError("ZIP contains absolute paths.")

            total_uncompressed += int(info.file_size or 0)
            if total_uncompressed > _HOSTED_BOT_ZIP_MAX_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP uncompressed size is too large.")

            lower = name.lower()
            if "/__pycache__/" in lower or parts[-1].startswith("."):
                continue

            ext = Path(lower).suffix
            if ext not in _HOSTED_BOT_TEXT_EXTS:
                continue
            if ext in _HOSTED_BOT_BLOCKED_FILE_EXTS:
                continue

            data = zf.read(info)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                # Ignore non-text/binary files in ZIP.
                continue

            if not text.strip():
                continue

            _scan_hosted_file_security(name, text)

            project_files.append({"path": name, "content": text})
            if len(project_files) > _HOSTED_BOT_PROJECT_MAX_FILES:
                raise ValueError("ZIP contains too many text files.")

            if ext == ".py":
                candidates.append((name, text))

    if not candidates:
        raise ValueError("No Python .py files found in ZIP.")
    if not project_files:
        raise ValueError("No supported text files found in ZIP.")

    # If archive has a single top-level folder (common on GitHub zips), trim it.
    top_parts = set()
    for item in project_files:
        p = [x for x in item["path"].split("/") if x]
        if p:
            top_parts.add(p[0])
    if len(top_parts) == 1:
        root = next(iter(top_parts)) + "/"
        for item in project_files:
            if item["path"].startswith(root):
                item["path"] = item["path"][len(root):]
        candidates = [
            (name[len(root):] if name.startswith(root) else name, code)
            for name, code in candidates
        ]

    preferred = ("bot.py", "main.py", "app.py")
    for pref in preferred:
        for name, code in candidates:
            if name.lower().endswith("/" + pref) or name.lower() == pref:
                return {
                    "entry_path": name,
                    "entry_code": code,
                    "project_files": project_files,
                }

    candidates.sort(key=lambda item: len(item[1]), reverse=True)
    return {
        "entry_path": candidates[0][0],
        "entry_code": candidates[0][1],
        "project_files": project_files,
    }


def _normalize_target_api(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    if not v:
        return "26.1.x"
    if v in {"paper_26_1", "paper26", "paper-26", "26", "26.1", "26.1.x"}:
        return "26.1.x"
    if v in {"paper_1_21", "1.21", "1.21.1", "1.21.2", "1.21.3", "1.21.4", "1.21.x"}:
        return "1.21.x"
    return raw.strip()


def _paper_profile_for_target_api(target_api: str | None) -> str:
    normalized = _normalize_target_api(target_api)
    return "paper_26_1" if normalized == "26.1.x" else "paper_1_21"


def _editor_metrics(code: str) -> dict[str, int]:
    block_headers = re.findall(r"```[^\n]*\n", code or "")
    file_count = len(block_headers)
    if file_count == 0 and (code or "").strip():
        file_count = 1
    return {
        "file_count": file_count,
        "total_chars": len(code or ""),
    }


def _required_editor_plan(metrics: dict[str, int]) -> str:
    for plan in _EDITOR_PLAN_ORDER:
        limits = _EDITOR_PLAN_LIMITS[plan]
        if (
            metrics["file_count"] <= limits["max_files"]
            and metrics["total_chars"] <= limits["max_chars"]
        ):
            return plan
    return "studio"


def _editor_limit_response(action: str, code: str):
    metrics = _editor_metrics(code)
    if metrics["file_count"] == 0 or metrics["total_chars"] == 0:
        return None

    current_plan = _effective_editor_plan()
    limits = _EDITOR_PLAN_LIMITS[current_plan]
    within_limits = (
        metrics["file_count"] <= limits["max_files"]
        and metrics["total_chars"] <= limits["max_chars"]
    )
    if within_limits:
        return None

    required_plan = _required_editor_plan(metrics)
    current_rank = _EDITOR_PLAN_ORDER.index(current_plan)
    required_rank = _EDITOR_PLAN_ORDER.index(required_plan)
    if required_rank <= current_rank:
        return None

    return jsonify({
        "error": (
            f"This project is too large for the {current_plan.title()} web editor tier when {action}. "
            f"Upgrade to {required_plan.title()} to continue."
        ),
        "code": "editor_tier_upgrade_required",
        "current_plan": current_plan,
        "required_plan": required_plan,
        "metrics": metrics,
        "limits": limits,
        "upgrade_url": "/pricing",
    }), 403


def _editor_feature_gate(required_plan: str, feature_name: str):
    current_plan = _effective_editor_plan()
    required_plan = _normalize_editor_plan(required_plan)
    if _editor_plan_rank(current_plan) >= _editor_plan_rank(required_plan):
        return None

    return jsonify({
        "error": (
            f"{feature_name} is available on the {required_plan.title()} editor tier and above. "
            f"Upgrade from {current_plan.title()} to continue."
        ),
        "code": "editor_feature_upgrade_required",
        "current_plan": current_plan,
        "required_plan": required_plan,
        "upgrade_url": "/pricing",
    }), 403


def _extract_plugin_name(code: str) -> str:
    """Extract the plugin name from generated code using multiple fallback strategies."""
    if not code:
        return ""

    # Strategy 1: inside a ```yaml / ```yml fenced block (tolerate trailing
    # whitespace and CRLF after the language tag, and handle truncated responses
    # that have no closing fence via (?:```|\Z)).
    m = re.search(
        r"```ya?ml[ \t]*\r?\n(.*?)(?:```|\Z)",
        code, re.DOTALL | re.IGNORECASE
    )
    if m:
        n = re.search(
            r"^\s*name\s*:\s*['\"]?([A-Za-z0-9_\- ]+)['\"]?\s*$",
            m.group(1), re.MULTILINE
        )
        if n:
            return n.group(1).strip()

    # Strategy 2: bare `name:` anywhere in the response (handles models that
    # skip the yaml fence entirely and just dump the plugin.yml as plain text).
    n = re.search(
        r"(?:^|\n)\s*name\s*:\s*['\"]?([A-Za-z0-9_\- ]{2,40})['\"]?\s*(?:\n|$)",
        code, re.IGNORECASE
    )
    if n:
        candidate = n.group(1).strip()
        # Exclude obvious false positives like Java field/variable names
        if not re.search(r"\b(class|void|public|private|static|final|return)\b",
                         candidate, re.IGNORECASE):
            return candidate

    # Strategy 3: derive from the main plugin class name in the Java block
    # e.g.  public class TeleportPlugin extends JavaPlugin  →  TeleportPlugin
    j = re.search(
        r"public\s+class\s+(\w+)\s+extends\s+(?:JavaPlugin|Plugin)\b",
        code, re.IGNORECASE
    )
    if j:
        raw = j.group(1)
        # Convert camelCase class name to a readable plugin name
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw).strip()
        return name or raw

    return ""


# Patterns used to extract an explicit plugin name from a user instruction so it
# can be injected at the top of the prompt before generation.  This prevents the
# model from generating wrong class names like "CalledWtpluginPlugin" when the
# instruction contains a lot of meta-commentary around the real plugin name.
_INSTRUCTION_NAME_RE = re.compile(
    r"(?:create|make|build|generate|write|code)\s+a?\s*(?:minecraft\s+)?plugin\s+called?\s+['\"]?([\w][\w\s\-]{1,40}?)['\"]?"
    r"(?=[\s.,\n\r]|$)",
    re.IGNORECASE,
)

# Chatbot meta-instruction phrases that confuse the model and lead to bad class
# naming or conversational responses instead of code. These are stripped from
# the instruction before sending to the generator.
_META_INSTRUCTION_PATTERNS = [
    re.compile(r"(?i)\bdo\s+NOT\s+start\s+coding\s+(?:yet|now)\.?\b"),
    re.compile(r"(?i)\bplease\s+acknowledge\s+(?:this|the\s+(?:roadmap|spec|structure))\b"),
    re.compile(r"(?i)reply\s+with\s+['\"]?Section\s+\d+\s+Received['\"]?[^.]*?\.?"),
    re.compile(r"(?i)For\s+each\s+section[^.]+?reply\s+with[^.]+?\."),
]


def _extract_instruction_plugin_name(instruction: str) -> str | None:
    """
    Try to extract an explicit plugin name from phrases like
    'Create a plugin called MyPlugin' or 'make a plugin named "CoolMod"'.
    Returns the name string or None if not found.
    """
    m = _INSTRUCTION_NAME_RE.search(instruction)
    if m:
        name = m.group(1).strip().strip("'\"").strip()
        # Reject noise — e.g. 'a' or vague words matched by the regex
        if len(name) >= 2 and re.match(r'^[\w][\w\s\-]*$', name):
            return name
    return None


def _strip_meta_instructions(instruction: str) -> str:
    """
    Remove chatbot meta-instruction phrases that confuse the model into
    generating conversational responses instead of code, or into using
    the meta-instruction text as class/plugin names.
    """
    for pat in _META_INSTRUCTION_PATTERNS:
        instruction = pat.sub("", instruction)
    return instruction.strip()


def _brand_free_tier(code: str) -> str:
    """
    For free-tier users, inject 'authors: [StackNest]' into the plugin.yml block.
    This keeps brand visibility on every plugin distributed from the free plan.
    If 'authors:' already exists it is replaced; the main plugin name is kept.
    """
    # Find the yaml block
    yaml_match = re.search(r"(```ya?ml\n)(.*?)(```)", code, re.DOTALL | re.IGNORECASE)
    if not yaml_match:
        return code

    yml_body = yaml_match.group(2)

    # Replace or insert authors field
    if re.search(r"^authors\s*:", yml_body, re.MULTILINE):
        yml_body = re.sub(
            r"^(authors\s*:.*)$",
            "authors: [StackNest]",
            yml_body,
            flags=re.MULTILINE,
        )
    else:
        # Insert after 'version:' line (or prepend if not found)
        if re.search(r"^version\s*:", yml_body, re.MULTILINE):
            yml_body = re.sub(
                r"(^version\s*:.*$)",
                r"\1\nauthors: [StackNest]",
                yml_body,
                flags=re.MULTILINE,
                count=1,
            )
        else:
            yml_body = "authors: [StackNest]\n" + yml_body

    branded = code[: yaml_match.start(2)] + yml_body + code[yaml_match.end(2) :]
    return branded


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/app")
def app_page():
    return send_from_directory(app.static_folder, "app.html")


@app.route("/pricing")
def pricing_page():
    return send_from_directory(app.static_folder, "pricing.html")


@app.route("/docs")
def docs_page():
    return send_from_directory(app.static_folder, "docs.html")


@app.route("/terms")
def terms():
    return send_from_directory(app.static_folder, "terms.html")


@app.route("/privacy")
def privacy():
    return send_from_directory(app.static_folder, "privacy.html")


@app.route("/gallery")
def gallery_page():
    return send_from_directory(app.static_folder, "gallery.html")


@app.route("/profile")
def profile_page():
    return send_from_directory(app.static_folder, "profile.html")


@app.route("/bots")
def bots_page():
    return send_from_directory(app.static_folder, "bots.html")


@app.route("/dashboard")
def bot_dashboard_page():
    return send_from_directory(app.static_folder, "dashboard.html")


@app.route("/ai-code-generators")
def page_ai_code_generators():
    return send_from_directory(app.static_folder, "ai-code-generators.html")


@app.route("/best-ai-tools")
def page_best_ai_tools():
    return send_from_directory(app.static_folder, "best-ai-tools.html")


@app.route("/ai-for-students")
def page_ai_for_students():
    return send_from_directory(app.static_folder, "ai-for-students.html")


@app.route("/ai-for-programmers")
def page_ai_for_programmers():
    return send_from_directory(app.static_folder, "ai-for-programmers.html")


@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory(app.static_folder, "sitemap.xml")


@app.route("/robots.txt")
def robots():
    return send_from_directory(app.static_folder, "robots.txt")


@app.route("/api/health", methods=["GET"])
def health():
    """API health check — always 200.  Inference/Paper status are informational."""
    try:
        from inference.gemini import is_available as gemini_ok, GEMINI_MODEL
        gemini_status = GEMINI_MODEL if gemini_ok() else "no_key"
    except Exception:
        gemini_status = "unknown"
    try:
        from inference.claude import is_available as claude_ok, CLAUDE_MODEL
        claude_status = CLAUDE_MODEL if claude_ok() else "no_key"
    except Exception:
        claude_status = "unknown"
    try:
        llamacpp_ok = health_check(timeout=3.0)
        inference_status = "ok" if llamacpp_ok else "offline"
    except Exception:
        inference_status = "unknown"
    return jsonify({
        "api": "ok",
        "inference_server": inference_status,
        "free_ai": gemini_status,
        "premium_ai": claude_status,
        "timestamp": datetime.utcnow().isoformat(),
    }), 200


@app.route("/api/status", methods=["GET"])
def status():
    """Return model info and server status."""
    model_info = get_model_info()
    return jsonify({
        "model": model_info,
        "rag_enabled": _router.config.use_rag,
        "api_target": _router.config.api_version,
    })


@app.route("/api/health/detailed", methods=["GET"])
def health_detailed():
    """
    Detailed backend health including watchdog state, circuit-breaker info,
    per-backend status and uptime.  Useful for monitoring and debugging.
    """
    # Aggregate basic health
    try:
        from inference.gemini import is_available as gemini_ok, GEMINI_MODEL
        gemini_status = GEMINI_MODEL if gemini_ok() else "no_key"
    except Exception:
        gemini_status = "unknown"
    try:
        from inference.claude import is_available as claude_ok, CLAUDE_MODEL
        claude_status = CLAUDE_MODEL if claude_ok() else "no_key"
    except Exception:
        claude_status = "unknown"
    try:
        from inference.server import get_stats
        _srv_stats    = get_stats()
        inference_status = "cloud-only"
        circuit_state    = "DISABLED"
    except Exception:
        inference_status = "unknown"
        _srv_stats       = {}
        circuit_state    = "unknown"

    # Watchdog snapshot
    try:
        from inference.watchdog import get_status as wd_status
        watchdog = wd_status()
    except Exception:
        watchdog = {"error": "watchdog not started"}

    return jsonify({
        "api": "ok",
        "inference_server": inference_status,
        "circuit_breaker":  circuit_state,
        "free_ai":          gemini_status,
        "premium_ai":       claude_status,
        "server_stats":     _srv_stats,
        "watchdog":         watchdog,
        "timestamp":        datetime.utcnow().isoformat(),
    }), 200


@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("15 per hour", key_func=get_remote_address)
def auth_register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    display_name = str(data.get("display_name", "")).strip()

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "Valid email is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not display_name:
        display_name = email.split("@", 1)[0][:40]
    if len(display_name) > 40:
        return jsonify({"error": "Display name too long"}), 400
    if get_user_by_email(email):
        return jsonify({"error": "Account already exists"}), 409

    user_id = create_user(
        email=email,
        password_hash=_hash_password(password),
        display_name=display_name,
    )
    reserved_plan = _RESERVED_EMAIL_PLANS.get(email)
    if reserved_plan:
        set_user_plan(user_id, reserved_plan)

    # Generate verification token (48-hour expiry enforced at verify time)
    vtoken = secrets.token_urlsafe(32)
    set_verification_token(user_id, vtoken)
    send_verification_email(email, display_name, vtoken, request_base_url=request.host_url)

    token = _make_user_token(user_id)
    return jsonify({
        "ok": True,
        "token": token,
        "verified": False,
        "user": {
            "id": user_id,
            "email": email,
            "display_name": display_name,
            "plan": "free",
            "verified": False,
            "avatar_color": "#5c6fff",
            "bio": "",
        },
    })


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("30 per hour", key_func=get_remote_address)
def auth_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    user = get_user_by_email(email)
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401
    if not user.get("password_hash"):  # Google-only account
        return jsonify({"error": "This account uses Google Sign-In. Please use the \"Sign in with Google\" button."}), 401
    if not _verify_password(password, user.get("password_hash", "")):
        return jsonify({"error": "Invalid email or password"}), 401

    verified = bool(user.get("verified", 0))
    token = _make_user_token(int(user["id"]))
    usage = get_user_usage(int(user["id"]))
    bot_hosting = _bot_hosting_access(user)
    extra_ports = _get_user_extra_ports(int(user["id"]))
    return jsonify({
        "ok": True,
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "plan": user.get("plan", "free"),
            "verified": verified,
            "avatar_color": user.get("avatar_color", "#5c6fff"),
            "bio": user.get("bio", ""),
            "bot_hosting_allowed": bool(bot_hosting["allowed"]),
            "bot_hosting_limit": int(bot_hosting["limit"]),
            "bot_hosting_source": bot_hosting["source"],
            "bot_port_base": _HOSTED_BOT_BASE_PORT_ALLOWANCE,
            "bot_extra_ports": extra_ports,
            "bot_port_quota": _HOSTED_BOT_BASE_PORT_ALLOWANCE + extra_ports,
        },
        "usage": usage,
    })


@app.route("/api/auth/me", methods=["GET"])
@_user_required
def auth_me():
    user = request.stacknest_user
    usage = get_user_usage(int(user["id"]))
    bot_hosting = _bot_hosting_access(user)
    extra_ports = _get_user_extra_ports(int(user["id"]))

    # ── Sliding token renewal ────────────────────────────────────────────────
    # If the token is valid but older than USER_TOKEN_REFRESH, issue a fresh one
    # so the user is never logged out as long as they are active.
    _, needs_refresh = _verify_user_token(_bearer_token())
    new_token = _make_user_token(int(user["id"])) if needs_refresh else None

    payload = {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "plan": user.get("plan", "free"),
            "verified": bool(user.get("verified", 0)),
            "avatar_color": user.get("avatar_color", "#5c6fff"),
            "avatar_url": user.get("avatar_url", ""),
            "bio": user.get("bio", ""),
            "discord_id":       user.get("discord_id") or None,
            "discord_username": user.get("discord_username") or "",
            "bot_hosting_allowed": bool(bot_hosting["allowed"]),
            "bot_hosting_limit": int(bot_hosting["limit"]),
            "bot_hosting_source": bot_hosting["source"],
            "bot_port_base": _HOSTED_BOT_BASE_PORT_ALLOWANCE,
            "bot_extra_ports": extra_ports,
            "bot_port_quota": _HOSTED_BOT_BASE_PORT_ALLOWANCE + extra_ports,
        },
        "usage": usage,
    }
    if new_token:
        payload["new_token"] = new_token
    return jsonify(payload)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# /api/user/me  — identity + quota (auth via session or X-API-Key)
# ---------------------------------------------------------------------------

@app.route("/api/user/me", methods=["GET"])
def api_user_me():
    """
    Returns identity and quota for the authenticated user.
    Accepts both JWT session cookie/header AND X-API-Key header.
    Useful for CLI tools, bots, VS Code extensions, CI pipelines.
    """
    # Prefer Authorization: Bearer; fall back to X-API-Key for backwards compat
    raw_key = ""
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        raw_key = auth_header[7:].strip()
    if not raw_key:
        raw_key = request.headers.get("X-API-Key", "").strip()
    if raw_key:
        if raw_key in VALID_PRO_KEYS:
            return jsonify({"error": "Legacy env-var keys are not supported by /api/user/me"}), 400
        user = get_user_by_api_key(raw_key)
        if not user:
            return jsonify({"error": "Invalid API key"}), 401
    else:
        user = _current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401

    usage = get_user_usage(int(user["id"]))
    return jsonify({
        "username":       user.get("display_name") or user.get("email"),
        "email":          user["email"],
        "plan":           user.get("plan", "free"),
        "gens_used":      usage.get("gens_used", 0),
        "gens_remaining": max(0, (usage.get("gens_limit", 0) + usage.get("bonus_gens", 0)) - usage.get("gens_used", 0)),
        "gens_limit":     usage.get("gens_limit", 0),
        "days_until_reset": usage.get("days_until_reset", 30),
    })


# ---------------------------------------------------------------------------
# /api/v1/  — versioned aliases (forward-compatible; point to same handlers)
# ---------------------------------------------------------------------------
# Register before other /api/user/* routes so Flask resolves them correctly.
app.add_url_rule("/api/v1/user/me",  endpoint="api_v1_user_me",  view_func=api_user_me, methods=["GET"])


# ---------------------------------------------------------------------------
# /api/user/api-key  — Studio API key management (session-only, never key-auth)
# ---------------------------------------------------------------------------

def _require_studio_user():
    """Return (user, error_response) for session-authenticated Pro or Studio users."""
    user = _current_user()
    if not user:
        return None, (jsonify({"error": "Authentication required"}), 401)
    if user.get("plan") not in ("pro", "studio"):
        return None, (jsonify({"error": "API keys require a Pro or Studio plan. Upgrade at /pricing."}), 403)
    return user, None


@app.route("/api/user/api-key", methods=["GET"])
@_user_required
def get_api_key_status():
    """Return whether the current user has an API key and its display prefix."""
    user = request.stacknest_user
    if user.get("plan") not in ("pro", "studio"):
        return jsonify({"error": "API keys require a Pro or Studio plan."}), 403
    has_key = bool(user.get("api_key_hash"))
    return jsonify({
        "exists": has_key,
        "prefix": user.get("api_key_prefix") if has_key else None,
    })


@app.route("/api/user/api-key/generate", methods=["POST"])
@limiter.limit("10 per hour", key_func=get_remote_address)
def generate_api_key():
    """Generate a new API key for the current Studio user (if none exists)."""
    user, err = _require_studio_user()
    if err:
        return err
    if user.get("api_key_hash"):
        return jsonify({"error": "An API key already exists. Use /rotate to replace it."}), 409
    raw_key = _generate_api_key()
    set_user_api_key(int(user["id"]), raw_key)
    return jsonify({
        "key":     raw_key,
        "prefix":  raw_key[:12],
        "warning": "Save this key now. It cannot be retrieved again. Use /rotate to get a new one.",
    })


@app.route("/api/user/api-key/rotate", methods=["POST"])
@limiter.limit("10 per hour", key_func=get_remote_address)
def rotate_api_key():
    """Revoke the current API key and issue a new one. Old key is immediately invalid."""
    user, err = _require_studio_user()
    if err:
        return err
    raw_key = _generate_api_key()
    set_user_api_key(int(user["id"]), raw_key)
    return jsonify({
        "key":     raw_key,
        "prefix":  raw_key[:12],
        "warning": "Your old key has been revoked. Save this key now. It cannot be retrieved again.",
    })


@app.route("/api/user/api-key", methods=["DELETE"])
def revoke_api_key():
    """Revoke the current user's API key entirely."""
    user, err = _require_studio_user()
    if err:
        return err
    clear_user_api_key(int(user["id"]))
    return jsonify({"ok": True, "message": "API key revoked."})


@app.route("/api/version", methods=["GET"])
def api_version():
    """Returns the server startup timestamp; used by the frontend to detect redeployments."""
    return jsonify({"version": _APP_VERSION})


@app.route("/api/auth/verify", methods=["POST"])
@limiter.limit("20 per hour", key_func=get_remote_address)
def auth_verify():
    """Verify an account using the emailed token."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    if not token:
        return jsonify({"error": "Verification token is required"}), 400

    user = get_user_by_verification_token(token)
    if not user:
        return jsonify({"error": "Invalid or already-used verification token"}), 400

    # Tokens expire after 48 hours
    ts = user.get("verification_token_ts") or 0
    if time.time() - float(ts) > 172800:
        return jsonify({"error": "Verification link has expired. Request a new one."}), 400

    set_user_verified(int(user["id"]))
    return jsonify({"ok": True, "message": "Email verified successfully!"})


@app.route("/api/auth/resend-verification", methods=["POST"])
@limiter.limit("5 per hour", key_func=get_remote_address)
@_user_required
def auth_resend_verification():
    """Re-send a verification email for the current user."""
    user = request.stacknest_user
    if user.get("verified"):
        return jsonify({"error": "Account is already verified"}), 400

    vtoken = secrets.token_urlsafe(32)
    set_verification_token(int(user["id"]), vtoken)
    sent = send_verification_email(
        user["email"],
        user["display_name"],
        vtoken,
        request_base_url=request.host_url,
    )
    if sent:
        return jsonify({"ok": True, "message": "Verification email sent!"})
    return jsonify({"error": "Failed to send email. Check server logs."}), 500



@app.route("/api/auth/forgot-password", methods=["POST"])
@limiter.limit("5 per hour", key_func=get_remote_address)
def auth_forgot_password():
    """Send a password-reset email. Always returns 200 to avoid email enumeration."""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    if not email:
        return jsonify({"ok": True}), 200
    user = get_user_by_email(email)
    if user:
        vtoken = secrets.token_urlsafe(32)
        set_verification_token(int(user["id"]), vtoken)
        send_password_reset_email(
            user["email"], user["display_name"], vtoken,
            request_base_url=request.host_url,
        )
    return jsonify({"ok": True}), 200


@app.route("/api/auth/reset-password", methods=["POST"])
@limiter.limit("10 per hour", key_func=get_remote_address)
def auth_reset_password():
    """Consume a reset token and set a new password."""
    data = request.get_json(silent=True) or {}
    token    = str(data.get("token",    "")).strip()
    password = str(data.get("password", "")).strip()
    if not token:
        return jsonify({"error": "Reset token is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    user = get_user_by_verification_token(token)
    if not user:
        return jsonify({"error": "Invalid or already-used reset link"}), 400

    # Tokens expire after 24 hours
    ts = user.get("verification_token_ts") or 0
    if time.time() - float(ts) > 86400:
        return jsonify({"error": "Reset link has expired. Request a new one."}), 400

    uid = int(user["id"])
    update_user_password(uid, _hash_password(password))
    set_user_verified(uid)              # mark verified in case they weren't
    set_verification_token(uid, "")     # invalidate token so it can't be reused

    session_token = _make_user_token(uid)
    fresh = get_user_by_id(uid)
    return jsonify({
        "ok": True,
        "token": session_token,
        "user": {
            "id": fresh["id"],
            "email": fresh["email"],
            "display_name": fresh["display_name"],
            "plan": fresh.get("plan", "free"),
            "verified": True,
            "avatar_color": fresh.get("avatar_color", "#5c6fff"),
            "bio": fresh.get("bio", ""),
        },
    })


@app.route("/api/auth/google", methods=["POST"])
@limiter.limit("20 per hour", key_func=get_remote_address)
def auth_google():
    """Verify a Google ID-token credential and return a StackNest session token."""
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Google sign-in is not configured on this server"}), 503

    data = request.get_json(silent=True) or {}
    credential = str(data.get("credential", "")).strip()
    if not credential:
        return jsonify({"error": "Missing Google credential"}), 400

    try:
        from google.oauth2 import id_token as gid_token
        from google.auth.transport import requests as grequests
        id_info = gid_token.verify_oauth2_token(
            credential,
            grequests.Request(),
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10,
        )
    except Exception:
        return jsonify({"error": "Invalid or expired Google token"}), 401

    google_id = str(id_info.get("sub", "")).strip()
    email     = str(id_info.get("email", "")).strip().lower()
    name      = str(id_info.get("name") or id_info.get("given_name") or email.split("@")[0])[:40]

    if not google_id or not email:
        return jsonify({"error": "Could not read account info from Google"}), 400

    # 1) Look up by Google sub-ID (stable across email changes)
    user = get_user_by_google_id(google_id)

    # 2) Fall back to email — merge with an existing email/password account
    if not user:
        user = get_user_by_email(email)
        if user:
            set_user_google_id(int(user["id"]), google_id)
            user = get_user_by_id(int(user["id"]))  # refresh with new google_id + verified=1

    # 3) Brand-new account — create it (no password, auto-verified)
    if not user:
        new_id = create_oauth_user(email=email, display_name=name, google_id=google_id)
        user = get_user_by_id(new_id)
        reserved_plan = _RESERVED_EMAIL_PLANS.get(email)
        if reserved_plan:
            set_user_plan(new_id, reserved_plan)
            user = get_user_by_id(new_id)

    token = _make_user_token(int(user["id"]))
    return jsonify({
        "ok": True,
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "plan": user.get("plan", "free"),
            "verified": True,
            "avatar_color": user.get("avatar_color", "#5c6fff"),
            "bio": user.get("bio", ""),
        },
    })


# ---------------------------------------------------------------------------
# Discord helper utilities
# ---------------------------------------------------------------------------

def _discord_state_create(user_id: int) -> str:
    """Create a short-lived signed state token for Discord OAuth."""
    ts    = str(int(time.time()))
    nonce = secrets.token_hex(8)
    payload = f"{user_id}.{ts}.{nonce}"
    sig = hmac.new(USER_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{sig}"


def _discord_state_verify(state: str) -> int | None:
    """Verify Discord OAuth state and return user_id, or None."""
    try:
        uid_str, ts_str, nonce, sig = state.split(".")
        payload  = f"{uid_str}.{ts_str}.{nonce}"
        expected = hmac.new(USER_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return None
        if time.time() - int(ts_str) > 600:   # 10-minute window
            return None
        return int(uid_str)
    except Exception:
        return None


def _discord_api(method: str, path: str, data: dict | None = None,
                  access_token: str | None = None) -> dict:
    """Call the Discord REST API."""
    url  = f"https://discord.com/api/v10{path}"
    body = json.dumps(data).encode() if data else None
    auth = f"Bearer {access_token}" if access_token else f"Bot {DISCORD_BOT_TOKEN_V}"
    headers = {
        "Authorization": auth,
        "Content-Type":  "application/json",
        "User-Agent":    "DiscordBot (https://stacknests.com, 1.0)",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"[Discord API] {method} {path} \u2192 HTTP {e.code}: {e.read()[:200]}")
        return {}
    except Exception as e:
        print(f"[Discord API] {method} {path} error: {e}")
        return {}


def _discord_assign_role(discord_user_id: str, role_id: str) -> bool:
    result = _discord_api("PUT",
        f"/guilds/{DISCORD_GUILD_ID}/members/{discord_user_id}/roles/{role_id}")
    return result is not None


def _discord_remove_role(discord_user_id: str, role_id: str) -> bool:
    result = _discord_api("DELETE",
        f"/guilds/{DISCORD_GUILD_ID}/members/{discord_user_id}/roles/{role_id}")
    return result is not None


def _discord_gallery_webhook(plugin_name: str, entry_id: int) -> None:
    """POST a new gallery entry to the Discord #showcase webhook."""
    if not DISCORD_GALLERY_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({
            "username": "StackNest Gallery",
            "embeds": [{
                "title":       f"\U0001f50c New plugin: {plugin_name}",
                "url":         f"https://stacknests.com/gallery/{entry_id}",
                "color":       0x5c6fff,
                "description": f"[View in gallery \u2192](https://stacknests.com/gallery/{entry_id})",
            }],
        }).encode()
        req = urllib.request.Request(
            DISCORD_GALLERY_WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent":   "DiscordBot (https://stacknests.com, 1.0)"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as _e:
        print(f"[Discord webhook] gallery post failed: {_e}")


def _discord_send_dm(discord_user_id: str, content: str) -> None:
    """Open a DM channel with a Discord user and send them a message."""
    if not DISCORD_BOT_TOKEN_V:
        return
    try:
        channel = _discord_api("POST", "/users/@me/channels",
                                data={"recipient_id": discord_user_id})
        channel_id = channel.get("id")
        if channel_id:
            _discord_api("POST", f"/channels/{channel_id}/messages",
                         data={"content": content})
    except Exception as _e:
        print(f"[Discord DM] failed to message {discord_user_id}: {_e}")


def _discord_update_tier_roles(discord_user_id: str, new_plan: str,
                                old_plan: str = "") -> None:
    """Assign the correct tier role and remove the old one (if any)."""
    if not DISCORD_GUILD_ID:
        return
    role_map = {
        "starter": DISCORD_STARTER_ROLE_ID,
        "pro":     DISCORD_PRO_ROLE_ID,
        "studio":  DISCORD_STUDIO_ROLE_ID,
    }
    if old_plan and old_plan in role_map and role_map[old_plan]:
        _discord_remove_role(discord_user_id, role_map[old_plan])
    new_role = role_map.get(new_plan, "")
    if new_role:
        _discord_assign_role(discord_user_id, new_role)


# ---------------------------------------------------------------------------
# Discord account linking
# ---------------------------------------------------------------------------

@app.route("/discord")
def discord_invite():
    """Redirect to Discord server invite."""
    from flask import redirect
    return redirect(DISCORD_INVITE_URL)


@app.route("/api/auth/discord")
@_user_required
def discord_oauth_start():
    """Redirect to Discord OAuth consent page."""
    if not DISCORD_CLIENT_ID:
        return jsonify({"error": "Discord OAuth not configured"}), 503
    user   = request.stacknest_user
    state  = _discord_state_create(int(user["id"]))
    params = urllib.parse.urlencode({
        "client_id":    DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope":        "identify guilds.join",
        "state":        state,
    })
    from flask import redirect
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@app.route("/api/auth/discord/callback")
def discord_oauth_callback():
    """Handle Discord OAuth callback, link account and assign Linked role."""
    from flask import redirect
    code  = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    error = request.args.get("error", "")
    if error or not code:
        return redirect("/profile?discord=cancelled")

    user_id = _discord_state_verify(state)
    if not user_id:
        return redirect("/profile?discord=error&reason=state")

    # Exchange code for access token
    token_data = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  DISCORD_REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        "https://discord.com/api/v10/oauth2/token",
        data=token_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent":   "DiscordBot (https://stacknests.com, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            token_resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[Discord OAuth] token exchange error: HTTP {e.code} {e.reason} | body: {body}")
        return redirect("/profile?discord=error&reason=token")
    except Exception as e:
        print(f"[Discord OAuth] token exchange error: {e}")
        return redirect("/profile?discord=error&reason=token")

    access_token = token_resp.get("access_token", "")
    if not access_token:
        return redirect("/profile?discord=error&reason=token")

    # Fetch Discord user info
    discord_user = _discord_api("GET", "/users/@me", access_token=access_token)
    discord_id   = str(discord_user.get("id",       ""))
    discord_name = str(discord_user.get("username", ""))
    if not discord_id:
        return redirect("/profile?discord=error&reason=user")

    # Prevent one Discord account linking to multiple StackNest accounts
    existing = get_user_by_discord_id(discord_id)
    if existing and int(existing["id"]) != user_id:
        return redirect("/profile?discord=error&reason=already_linked")

    # Store discord_id on user
    set_user_discord(user_id, discord_id, discord_name)

    # Auto-join the guild (uses guilds.join scope + bot token)
    if DISCORD_GUILD_ID:
        _discord_api("PUT",
            f"/guilds/{DISCORD_GUILD_ID}/members/{discord_id}",
            data={"access_token": access_token},
        )
        # Assign Linked role
        _discord_assign_role(discord_id, DISCORD_LINKED_ROLE)
        # If already on a paid plan, assign tier role too
        _user_row = get_user_by_id(user_id)
        if _user_row and _user_row.get("plan", "free") not in ("free", ""):
            _discord_update_tier_roles(discord_id, _user_row["plan"])

    # Welcome DM (best-effort — doesn't block the redirect)
    _discord_send_dm(
        discord_id,
        "\U0001f44b Welcome to **StackNest**! Your Discord account is now linked.\n\n"
        "\U0001f50c Generate plugins: https://stacknests.com/app\n"
        "\U0001f4da Docs & guides: https://stacknests.com/docs\n"
        "\U0001f3a8 Share your creations in **#showcase**!\n\n"
        "_Message here if you need help \u2014 the team will see it._"
    )

    return redirect("/profile?discord=linked")


@app.route("/api/auth/discord/unlink", methods=["POST"])
@_user_required
def discord_unlink():
    """Remove Discord account link and revoke Linked role."""
    user    = request.stacknest_user
    disc_id = user.get("discord_id")
    if disc_id and DISCORD_GUILD_ID:
        _discord_remove_role(str(disc_id), DISCORD_LINKED_ROLE)
    unlink_user_discord(int(user["id"]))
    return jsonify({"ok": True})


@app.route("/api/profile", methods=["GET"])
@_user_required
def profile_get():
    user = request.stacknest_user
    payload = {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "plan": user.get("plan", "free"),
        "verified": bool(user.get("verified", 0)),
        "avatar_color": user.get("avatar_color", "#5c6fff"),
        "avatar_url": user.get("avatar_url", ""),
        "bio": user.get("bio", ""),
        "discord_id":       user.get("discord_id") or None,
        "discord_username": user.get("discord_username") or "",
    }
    return jsonify({"user": payload})


@app.route("/api/profile", methods=["PATCH"])
@_user_required
def profile_update():
    user = request.stacknest_user
    data = request.get_json(silent=True) or {}
    user_id = int(user["id"])

    # Handle password change
    if "new_password" in data:
        current_pw = str(data.get("current_password", ""))
        if not _verify_password(current_pw, user.get("password_hash", "")):
            return jsonify({"error": "Current password is incorrect"}), 400
        new_pw = str(data.get("new_password", ""))
        if len(new_pw) < 8:
            return jsonify({"error": "New password must be at least 8 characters"}), 400
        update_user_password(user_id, _hash_password(new_pw))
        return jsonify({"ok": True, "message": "Password updated"})

    updated = update_user_profile(
        user_id,
        display_name=str(data["display_name"]).strip() if "display_name" in data else None,
        avatar_color=str(data["avatar_color"]) if "avatar_color" in data else None,
        bio=str(data["bio"]) if "bio" in data else None,
    )
    if not updated:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "ok": True,
        "user": {
            "id": updated["id"],
            "email": updated["email"],
            "display_name": updated["display_name"],
            "plan": updated.get("plan", "free"),
            "verified": bool(updated.get("verified", 0)),
            "avatar_color": updated.get("avatar_color", "#5c6fff"),
            "avatar_url": updated.get("avatar_url", ""),
            "bio": updated.get("bio", ""),
        },
        "usage": get_user_usage(user_id),
    })


# --------------------------------------------------------------------------- #
# Profile picture upload                                                       #
# --------------------------------------------------------------------------- #

_AVATARS_DIR = Path(__file__).parent.parent / "data" / "avatars"
_ALLOWED_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


@app.route("/api/avatars/<path:filename>", methods=["GET"])
def serve_avatar(filename: str):
    """Serve a user-uploaded profile picture."""
    safe = secure_filename(filename)
    if not safe or ".." in safe:
        return jsonify({"error": "Invalid filename"}), 400
    if not (_AVATARS_DIR / safe).exists():
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(str(_AVATARS_DIR), safe)


@app.route("/api/user/avatar", methods=["POST"])
@limiter.limit("20 per hour", key_func=get_remote_address)
@_user_required
def upload_avatar():
    """Upload a profile picture. Accepts multipart/form-data with field 'avatar'."""
    user: dict = request.stacknest_user
    user_id = int(user["id"])

    file = request.files.get("avatar")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_IMG_EXTS:
        return jsonify({"error": "Only .jpg, .png, .gif, .webp files are accepted"}), 400

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > _MAX_AVATAR_BYTES:
        return jsonify({"error": "Image too large (max 5 MB)"}), 400

    _AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"avatar_{user_id}_{int(time.time())}{ext}"
    dest = _AVATARS_DIR / filename
    file.save(str(dest))

    avatar_url = f"/api/avatars/{filename}"
    update_user_profile(user_id, avatar_url=avatar_url)

    return jsonify({"ok": True, "avatar_url": avatar_url})


# --------------------------------------------------------------------------- #
# Stripe Subscriptions                                                         #
# --------------------------------------------------------------------------- #

STRIPE_SECRET_KEY             = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET         = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID_STARTER        = os.getenv("STRIPE_PRICE_ID_STARTER", "")
STRIPE_PRICE_ID_STARTER_ANNUAL = os.getenv("STRIPE_PRICE_ID_STARTER_ANNUAL", "")
STRIPE_PRICE_ID_PRO            = os.getenv("STRIPE_PRICE_ID_PRO", "")
STRIPE_PRICE_ID_PRO_ANNUAL     = os.getenv("STRIPE_PRICE_ID_PRO_ANNUAL", "")
STRIPE_PRICE_ID_STUDIO         = os.getenv("STRIPE_PRICE_ID_STUDIO", "")
STRIPE_PRICE_ID_STUDIO_ANNUAL  = os.getenv("STRIPE_PRICE_ID_STUDIO_ANNUAL", "")
STRIPE_SUCCESS_URL            = os.getenv("STRIPE_SUCCESS_URL", "/app?upgraded=1")
STRIPE_CANCEL_URL             = os.getenv("STRIPE_CANCEL_URL", "/pricing")

# Map Stripe price IDs → plan name (used in webhook to determine granted plan)
def _price_to_plan(price_id: str) -> str:
    m = {
        STRIPE_PRICE_ID_STARTER:        "starter",
        STRIPE_PRICE_ID_STARTER_ANNUAL: "starter",
        STRIPE_PRICE_ID_PRO:            "pro",
        STRIPE_PRICE_ID_PRO_ANNUAL:     "pro",
        STRIPE_PRICE_ID_STUDIO:         "studio",
        STRIPE_PRICE_ID_STUDIO_ANNUAL:  "studio",
    }
    return m.get(price_id, "pro")

# Pay-as-you-go one-time Stripe Price IDs and credit amounts
_CREDIT_PACKS = {
    "starter": {"price_id": os.getenv("STRIPE_PRICE_ID_CREDITS_STARTER", ""), "credits": 5},
    "basic":   {"price_id": os.getenv("STRIPE_PRICE_ID_CREDITS_BASIC",   ""), "credits": 15},
    "bundle":  {"price_id": os.getenv("STRIPE_PRICE_ID_CREDITS_BUNDLE",  ""), "credits": 35},
    "topup":   {"price_id": os.getenv("STRIPE_PRICE_ID_CREDITS_TOPUP",   ""), "credits": 50},
}


def _stripe():
    """Lazy-import stripe and configure the API key."""
    import stripe as _s
    _s.api_key = STRIPE_SECRET_KEY
    return _s


@app.route("/api/stripe/checkout", methods=["POST"])
@_user_required
def stripe_checkout():
    """Create a Stripe Checkout Session for upgrading to Pro or Studio."""
    data    = request.get_json(silent=True) or {}
    plan    = str(data.get("plan",    "pro")).lower()
    billing = str(data.get("billing", "monthly")).lower()

    if plan not in ("starter", "pro", "studio"):
        return jsonify({"error": "Invalid plan."}), 400

    # Pick the correct Stripe price ID
    if plan == "studio":
        price_id = STRIPE_PRICE_ID_STUDIO_ANNUAL if billing == "annual" else STRIPE_PRICE_ID_STUDIO
    elif plan == "starter":
        price_id = STRIPE_PRICE_ID_STARTER_ANNUAL if billing == "annual" else STRIPE_PRICE_ID_STARTER
    else:
        price_id = STRIPE_PRICE_ID_PRO_ANNUAL if billing == "annual" else STRIPE_PRICE_ID_PRO

    if not STRIPE_SECRET_KEY or not price_id:
        return jsonify({"error": "This plan is not yet available — check back soon."}), 503

    user = request.stacknest_user
    if user.get("plan") == plan:
        return jsonify({"error": f"You are already on the {plan.capitalize()} plan."}), 400

    stripe = _stripe()
    base = request.host_url.rstrip("/")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=base + STRIPE_SUCCESS_URL,
            cancel_url=base + STRIPE_CANCEL_URL,
            customer_email=user["email"],
            metadata={"stacknest_user_id": str(user["id"]), "plan": plan},
            allow_promotion_codes=True,
        )
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": "Could not create checkout session."}), 503

    return jsonify({"url": session.url})


@app.route("/api/stripe/credits/checkout", methods=["POST"])
@_user_required
def stripe_credits_checkout():
    """Create a one-time Stripe Checkout Session for a pay-as-you-go credit pack."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server."}), 503

    data = request.get_json(silent=True) or {}
    pack = str(data.get("pack", "")).lower()
    if pack not in _CREDIT_PACKS:
        return jsonify({"error": "Invalid pack. Choose starter, basic, or bundle."}), 400

    pack_info = _CREDIT_PACKS[pack]
    if not pack_info["price_id"]:
        return jsonify({"error": "Credit packs are not yet configured — check back soon!"}), 503

    user = request.stacknest_user
    stripe = _stripe()
    base = request.host_url.rstrip("/")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": pack_info["price_id"], "quantity": 1}],
            success_url=base + "/app?credits=1",
            cancel_url=base + "/pricing",
            customer_email=user["email"],
            metadata={
                "stacknest_user_id": str(user["id"]),
                "payment_type": "credits",
                "pack": pack,
            },
            allow_promotion_codes=True,
        )
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Could not create checkout session."}), 503

    return jsonify({"url": session.url})


@app.route("/api/stripe/portal", methods=["POST"])
@_user_required
def stripe_portal():
    """Open Stripe Customer Portal so user can manage/cancel subscription."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server."}), 503

    user = request.stacknest_user
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        return jsonify({"error": "No billing account found. Please upgrade first."}), 400

    stripe = _stripe()
    base = request.host_url.rstrip("/")

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=base + "/app",
        )
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": "Could not open billing portal."}), 503

    return jsonify({"url": session.url})


@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """
    Stripe webhook endpoint — handles subscription lifecycle events.
    Verify the signature then update the user's plan in the DB.
    """
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Webhook not configured"}), 503

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    stripe = _stripe()

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify({"error": "Invalid webhook signature"}), 400

    etype = event["type"]

    # ── checkout.session.completed — user just paid ──────────────────────── #
    if etype == "checkout.session.completed":
        obj = event["data"]["object"]
        customer_id     = obj.get("customer")
        subscription_id = obj.get("subscription")
        meta            = obj.get("metadata") or {}
        uid_str         = meta.get("stacknest_user_id")
        payment_type    = meta.get("payment_type", "subscription")
        if uid_str:
            uid = int(uid_str)
            if payment_type == "credits":
                pack = meta.get("pack", "")
                credits = _CREDIT_PACKS.get(pack, {}).get("credits", 0)
                if credits:
                    add_user_bonus_gens(uid, credits)
                    print(f"[Stripe] Added {credits} bonus gens to user {uid} (pack={pack})")
            else:
                granted_plan = meta.get("plan", "pro")  # 'pro' or 'studio'
                set_user_plan(uid, granted_plan)
                set_user_stripe_ids(uid,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id)
                print(f"[Stripe] Upgraded user {uid} to {granted_plan}")
                # Sync Discord tier role if account is linked
                _upgraded = get_user_by_id(uid)
                if _upgraded and _upgraded.get("discord_id"):
                    _discord_update_tier_roles(str(_upgraded["discord_id"]), granted_plan)

    # ── customer.subscription.deleted — cancelled or expired ────────────── #
    elif etype == "customer.subscription.deleted":
        obj = event["data"]["object"]
        customer_id = obj.get("customer")
        user = get_user_by_stripe_customer_id(customer_id) if customer_id else None
        if user:
            set_user_plan(int(user["id"]), "free")
            # Remove all Discord tier roles
            if user.get("discord_id"):
                for _rid in filter(None, [DISCORD_STARTER_ROLE_ID,
                                          DISCORD_PRO_ROLE_ID,
                                          DISCORD_STUDIO_ROLE_ID]):
                    _discord_remove_role(str(user["discord_id"]), _rid)

    # ── invoice.payment_failed — grace period / retry ───────────────────── #
    elif etype == "invoice.payment_failed":
        obj = event["data"]["object"]
        customer_id = obj.get("customer")
        # Leave plan active during grace period — Stripe will send .deleted if needed
        print(f"[Stripe] Payment failed for customer {customer_id}")

    return jsonify({"received": True})


@app.route("/support")
def support_page():
    return send_from_directory(app.static_folder, "support.html")


@app.route("/api/support/ticket", methods=["POST"])
@limiter.limit("5 per hour", key_func=get_remote_address)
def submit_support_ticket():
    """Submit a support ticket (unauthenticated; email is required)."""
    data    = request.get_json(silent=True, force=True) or {}
    email   = str(data.get("email",   "")).strip()
    subject = str(data.get("subject", "")).strip()
    message = str(data.get("message", "")).strip()
    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required"}), 400
    if not subject:
        return jsonify({"error": "'subject' is required"}), 400
    if len(subject) > 200:
        return jsonify({"error": "Subject too long (max 200 characters)"}), 400
    if len(message.strip()) < 10:
        return jsonify({"error": "Please describe your issue (at least 10 characters)"}), 400
    if len(message) > 4000:
        return jsonify({"error": "Message too long (max 4000 chars)"}), 400
    ticket_id = create_ticket(email, subject, message, user_id=None)

    # If Arti away-mode is active, auto-reply in a background thread
    def _arti_auto_reply():
        try:
            with _arti_lock:
                state = _load_arti_state()
            if not state.get("enabled"):
                return
            user_msg = f"Support ticket from {email}:\nSubject: {subject}\n\n{message}"
            from inference.gemini import is_available as _g_ok, gemini_generate as _g_gen
            from inference.claude import is_available as _c_ok, claude_generate as _c_gen
            if _g_ok():
                reply = _g_gen(user_msg, _ARTI_SYSTEM)
            elif _c_ok():
                reply = _c_gen(user_msg, _ARTI_SYSTEM)
            else:
                return
            if reply:
                update_ticket_status(ticket_id, "in_progress", reply[:2000])
                with _arti_lock:
                    st = _load_arti_state()
                    _arti_log(st, f"Auto-replied to ticket #{ticket_id}: {subject[:60]}")
                    _save_arti_state(st)
        except Exception:
            pass
    threading.Thread(target=_arti_auto_reply, daemon=True).start()

    return jsonify({"ok": True, "ticket_id": ticket_id}), 201


@app.route("/verify")
def verify_page():
    """Redirect email verification link to the app page."""
    return send_from_directory(app.static_folder, "app.html")


@app.route("/api/projects", methods=["GET"])
@_user_required
def user_projects_list():
    user = request.stacknest_user
    limit = min(max(int(request.args.get("limit", 25)), 1), 100)
    rows = list_user_projects(int(user["id"]), limit=limit)
    return jsonify({"projects": rows})


@app.route("/api/projects/<int:project_id>", methods=["GET"])
@_user_required
def user_project_detail(project_id: int):
    user = request.stacknest_user
    row = get_user_project(int(user["id"]), project_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row)


@app.route("/api/projects", methods=["POST"])
@_user_required
def user_projects_create():
    user = request.stacknest_user
    data = request.get_json(silent=True) or {}
    generated_code = str(data.get("generated_code", "") or "")

    gate = _editor_limit_response("saving", generated_code)
    if gate is not None:
        return gate

    project_name = str(data.get("project_name", "")).strip() or "Untitled Plugin"
    target_api = _normalize_target_api(str(data.get("target_api", "26.1.x")))
    project_id = save_user_project(
        user_id=int(user["id"]),
        project_name=project_name,
        plugin_type=str(data.get("plugin_type", "full_plugin")),
        target_api=target_api,
        features=list(data.get("features", [])) if isinstance(data.get("features", []), list) else [],
        instruction=str(data.get("instruction", "")),
        full_instruction=str(data.get("full_instruction", "")),
        include_tests=bool(data.get("include_tests", True)),
        skip_compile=bool(data.get("skip_compile", False)),
        success=bool(data.get("success")) if "success" in data else None,
        compile_ok=bool(data.get("compile_ok")) if "compile_ok" in data else None,
        generated_code=generated_code or None,
        warnings=list(data.get("warnings", [])) if isinstance(data.get("warnings", []), list) else [],
        errors=list(data.get("errors", [])) if isinstance(data.get("errors", []), list) else [],
        metadata=data.get("metadata", {}) if isinstance(data.get("metadata", {}), dict) else {},
    )
    return jsonify({"ok": True, "project_id": project_id}), 201


@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
@_user_required
def user_project_delete(project_id: int):
    user = request.stacknest_user
    deleted = delete_user_project(int(user["id"]), project_id)
    if not deleted:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


# ── Instruction safety ────────────────────────────────────────────────────────
_INJECTION_PHRASES = [
    "ignore previous", "ignore your instructions", "ignore all",
    "forget your", "forget all previous", "forget the above",
    "you are now", "pretend you are", "act as if", "act as a",
    "roleplay as", "jailbreak", "dan mode", "developer mode",
    "override your", "new persona", "disregard", "system prompt",
    "ignore the above", "do not follow", "unlock mode",
    # NOTE: "bypass" and "hack" are NOT here — they are common in legitimate plugin
    # descriptions (e.g. "combatcore.bypass" permission, "anti-hack detection").
    # They are handled by the context-sensitive _HARMFUL_CONTEXT_TERMS check below.
]

_MINECRAFT_TERMS = [
    "plugin", "bukkit", "spigot", "paper", "folia", "velocity", "bungeecord",
    "waterfall", "minecraft", "server", "player", "command", "event",
    "listener", "mob", "entity", "block", "item", "world", "inventory",
    "spawn", "teleport", "chat", "permission", "yml", "java", "pvp",
    "economy", "vault", "kit", "ban", "mute", "home", "warp", "rank",
    "scoreboard", "bossbar", "chunk", "biome", "enchant", "potion",
    "npc", "region", "grief", "craft", "survival", "creative",
    "creeper", "sword", "armor", "health", "damage", "cooldown",
    "gui", "menu", "chest", "click", "bukkit", "spigot",
]

_HARMFUL_TERMS = [
    "malware", "ransomware", "backdoor", "keylogger", "trojan",
    "ddos", "denial of service", "sql inject",
    "remote code execution", "reverse shell", "steal password",
    "steal credentials", "phishing", "rootkit",
]

# Terms that are only harmful when NOT accompanied by a mitigation keyword nearby.
# e.g. "prevent exploits" is fine; "add an exploit" is not.
_HARMFUL_CONTEXT_TERMS = ["exploit", "hack", "bypass"]

_HARMFUL_CONTEXT_MITIGATIONS = [
    "prevent", "no ", "stop", "avoid", "protect", "safe", "block", "detect",
    "without", "anti", "fix", "patch", "secure", "mitigat",
]


def _check_instruction_safety(text: str):
    """Returns (ok, error_message). ok=False means the request should be rejected."""
    lower = " " + text.lower() + " "

    # StackNest compiles with javac — Kotlin plugins require kotlinc which is not available.
    # Redirect Kotlin requests gracefully rather than letting them fail with "No Java code blocks".
    # Note: `lower` has spaces prepended/appended so " kotlin " catches word-boundary cases.
    # Use re.search for .kt to handle ".kt files", ".kt extension", etc. (not just bare " .kt ").
    if " kotlin " in lower or "kotlin plugin" in lower or bool(re.search(r'\.kt\b', lower)):
        if not any(t in lower for t in ("java", "no kotlin", "not kotlin", "without kotlin")):
            return False, (
                "StackNest generates Java Paper plugins. "
                "Kotlin compilation is not supported — please describe your plugin in plain English "
                "and StackNest will generate it in Java."
            )

    for phrase in _INJECTION_PHRASES:
        if phrase in lower:
            return False, "Invalid request. Please describe a Minecraft plugin you'd like to create."

    for term in _HARMFUL_TERMS:
        if term in lower:
            return False, "This request cannot be processed. Please describe a standard Minecraft plugin feature."

    # Context-sensitive terms: only block if no mitigation keyword appears within
    # 60 characters of the term (covers phrases like "prevent exploits/duplication").
    for term in _HARMFUL_CONTEXT_TERMS:
        idx = lower.find(term)
        while idx != -1:
            window = lower[max(0, idx - 60): idx + len(term) + 60]
            if not any(m in window for m in _HARMFUL_CONTEXT_MITIGATIONS):
                return False, "This request cannot be processed. Please describe a standard Minecraft plugin feature."
            idx = lower.find(term, idx + 1)

    if not any(term in lower for term in _MINECRAFT_TERMS):
        return False, (
            "StackNest generates Minecraft server plugins only. "
            "Please describe your plugin — mention commands, events, players, "
            "items, or a server platform like Paper or Spigot."
        )

    return True, None


# ---------------------------------------------------------------------------
# Web search helper — injects up-to-date API context into the prompt
# ---------------------------------------------------------------------------

# Simple in-memory TTL cache for web search results.
# Key: query string.  Value: (expiry_epoch, results_list).
# TTL is 6 hours — long enough to avoid hammering the search API on
# repeated identical requests (e.g. many users asking for economy plugins),
# short enough to pick up newly published API docs within a working day.
_SEARCH_CACHE: dict[str, tuple[float, list]] = {}
_SEARCH_CACHE_TTL = 6 * 3600  # seconds


def _web_search_cached(query: str, max_results: int = 4) -> list:
    """Return cached results for `query` if fresh; otherwise call _web_search and cache."""
    import time as _time
    now = _time.time()
    entry = _SEARCH_CACHE.get(query)
    if entry and entry[0] > now:
        return entry[1]
    results = _web_search(query, max_results=max_results)
    if results:
        # Only cache successful responses
        _SEARCH_CACHE[query] = (now + _SEARCH_CACHE_TTL, results)
        # Evict stale entries if cache grows large (>200 keys)
        if len(_SEARCH_CACHE) > 200:
            cutoff = now
            for k in [k for k, v in list(_SEARCH_CACHE.items()) if v[0] < cutoff]:
                del _SEARCH_CACHE[k]
    return results


def _web_search(query: str, max_results: int = 4) -> list:
    """
    Search the web for recent/relevant information to augment plugin generation.
    Returns a list of {title, snippet, url} dicts (empty list on any failure).

    Tries in priority order:
      1. Brave Search API  (set BRAVE_API_KEY env var)
      2. Serper / Google   (set SERPER_API_KEY env var)
      3. DuckDuckGo Instant Answer API (no key, limited results)
    """
    try:
        brave_key = os.getenv("BRAVE_API_KEY", "").strip()
        if brave_key:
            url = (
                "https://api.search.brave.com/res/v1/web/search"
                f"?q={urllib.parse.quote(query)}&count={max_results}&freshness=pm"
            )
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-Subscription-Token": brave_key,
                },
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            results = []
            for item in data.get("web", {}).get("results", [])[:max_results]:
                results.append({
                    "title":   item.get("title", ""),
                    "snippet": item.get("description", "")[:400],
                    "url":     item.get("url", ""),
                })
            return results

        serper_key = os.getenv("SERPER_API_KEY", "").strip()
        if serper_key:
            payload = json.dumps({"q": query, "num": max_results}).encode()
            req = urllib.request.Request(
                "https://google.serper.dev/search",
                data=payload,
                headers={
                    "X-API-KEY": serper_key,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            results = []
            for item in data.get("organic", [])[:max_results]:
                results.append({
                    "title":   item.get("title", ""),
                    "snippet": item.get("snippet", "")[:400],
                    "url":     item.get("link", ""),
                })
            return results

        # Fallback: DuckDuckGo Instant Answer API (free, but limited depth)
        url = (
            "https://api.duckduckgo.com/"
            f"?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "StackNest/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        results = []
        abstract = data.get("AbstractText", "")
        if abstract:
            results.append({
                "title":   data.get("AbstractSource", "DuckDuckGo"),
                "snippet": abstract[:400],
                "url":     data.get("AbstractURL", ""),
            })
        for item in data.get("RelatedTopics", []):
            if len(results) >= max_results:
                break
            if isinstance(item, dict) and item.get("Text"):
                results.append({
                    "title":   item.get("FirstURL", ""),
                    "snippet": item["Text"][:300],
                    "url":     item.get("FirstURL", ""),
                })
        return results

    except Exception:
        # Web search is best-effort — never block generation on failure
        return []


@app.route("/api/generate", methods=["POST"])
@limiter.limit(FREE_MONTHLY_LIMIT, key_func=get_remote_address,
               exempt_when=lambda: get_tier() == "pro" or _authenticated_user() is not None,
               error_message=json.dumps({
                   "error": "Free tier limit reached (3 plugins/month). Upgrade at stacknests.com/pricing.",
                   "upgrade_url": "/#pricing"
               }))
def generate():
    """
    Generate a plugin from a natural-language instruction.
    Runs the full validation + retry loop before returning.

    Request body:
    {
        "instruction": "Create a plugin that broadcasts a message every 60 seconds",
        "plugin_name": "Announcer",          // optional — hints the class name
        "folia_compatible": false,           // optional — warn if scheduler used
        "skip_compile": false                // optional — skip javac step
    }

    Response:
    {
        "success": true,
        "code": "...",         // markdown with java + yaml blocks
        "attempts": 1,
        "elapsed_seconds": 47.2,
        "warnings": [],
        "errors": []
    }
    """
    ip = get_remote_address()

    # Check if IP is banned
    if is_banned(ip):
        return jsonify({"error": "Access denied."}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    instruction = data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "'instruction' field is required"}), 400
    if len(instruction) > 2000:
        return jsonify({"error": "Instruction too long (max 2000 chars)"}), 400

    skip_compile = bool(data.get("skip_compile", False))
    folia = bool(data.get("folia_compatible", False))
    save_project = bool(data.get("save_project", True))
    target_api = _normalize_target_api(str(data.get("target_api", "26.1.x")))
    paper_profile = _paper_profile_for_target_api(target_api)
    tier = get_tier()
    plan = _effective_editor_plan()   # free / starter / pro / studio

    # Refinement mode: user provides existing code + a change request
    # Gated behind Starter+ — free tier gets single-class generation only.
    previous_code = data.get("previous_code", "").strip()
    if previous_code and plan == "free":
        return jsonify({
            "error": "Code refinement (modifying existing plugins) is a Starter+ feature. Upgrade at stacknests.com/pricing.",
            "upgrade_url": "/pricing",
        }), 403
    if previous_code:
        # For large plugins we need the full context so the model doesn't lose
        # files it can't see ("files getting reduced" symptom). kimi-k2.6 and
        # Gemini support 130k+ token contexts so 25 000 chars is safe.
        code_snippet = previous_code[:50000]
        large_plugin = len(previous_code) > 15000
        large_hint = (
            "\nNOTE: This is a large multi-file plugin. "
            "You MUST output ALL existing files in full — every ```java block, "
            "plugin.yml, and build file — even if only one file changes. "
            "Do NOT drop or omit any file that exists in the === EXISTING CODE ===."
            if large_plugin else ""
        )
        instruction = (
            f"You are modifying an existing Minecraft plugin. "
            f"Keep all existing functionality unless the change request says to remove or replace it. "
            f"Only apply the change described below.{large_hint}\n\n"
            f"=== EXISTING CODE ===\n{code_snippet}\n=== END EXISTING CODE ===\n\n"
            f"Change request: {instruction}"
        )

    ok, safety_err = _check_instruction_safety(instruction)
    if not ok:
        return jsonify({"error": safety_err}), 400

    # Block unverified accounts from free-tier generation (anti-abuse)
    user_pre = _authenticated_user()
    if user_pre and not user_pre.get("verified") and tier == "free" and not is_bypassed(ip):
        return jsonify({
            "error": "Please verify your email address before generating plugins. Check your inbox or resend from Settings.",
            "unverified": True,
        }), 403

    # Per-user monthly generation limit (overrides IP-based limit for logged-in users)
    if user_pre and not is_bypassed(ip):
        allowed, usage = check_user_generation_limit(int(user_pre["id"]))
        if not allowed:
            plan = usage.get("plan", "free")
            limit = usage["gens_limit"]
            days = usage["days_until_reset"]
            upgrade_hint = " Upgrade to Pro for 100/month." if plan == "free" else ""
            return jsonify({
                "error": f"Monthly generation limit reached ({limit}/month).{upgrade_hint} Resets in {days} days.",
                "usage": usage,
                "upgrade_url": "/pricing",
            }), 429

    # Strip chatbot meta-instructions that confuse the model
    instruction = _strip_meta_instructions(instruction)

    # Augment instruction with hints
    if folia:
        instruction = instruction.rstrip(".") + ". This plugin must be Folia-compatible."

    # Inject an explicit plugin name at the top of the instruction so the model
    # picks the right class name even when the instruction is long or has meta-commentary.
    _name_hint = str(data.get("plugin_name") or data.get("project_name") or "").strip()
    if not _name_hint:
        _name_hint = _extract_instruction_plugin_name(instruction) or ""
    if _name_hint and not instruction.startswith("Plugin name:"):
        instruction = f"Plugin name: {_name_hint}\n\n{instruction}"

    # Configure generator — starter/pro/studio unlock Claude, free uses Gemini/Kimi.
    # Smart assembly (feature extraction + focused prompt) is enabled for paid plans:
    # extracts feature blocks from the instruction so the model only writes custom logic.
    gen = PluginGenerator(
        router=_router,
        params=GenerationParams(max_tokens=2048),
        skip_compile=skip_compile,
        tier=_to_inference_tier(tier),
        plan=plan,
        paper_target_profile=paper_profile,
        use_smart_assembly=(plan in _PAID_PLANS),
    )

    try:
        result = gen.generate(instruction)
    except Exception as e:
        traceback.print_exc()
        log_request(ip=ip, endpoint="/api/generate", tier=tier,
                    instruction=instruction, success=False, errors=[str(e)])
        # Give a more descriptive error so users know whether to retry or not
        err_str = str(e).lower()
        if "rate" in err_str or "quota" in err_str or "429" in err_str:
            msg = "AI quota temporarily exhausted. Please try again in a few minutes."
        elif "auth" in err_str or "key" in err_str:
            msg = "AI backend configuration error. Please contact support."
        elif "timeout" in err_str or "connection" in err_str or "unreachable" in err_str:
            msg = "AI backend is unreachable right now. StackNest will retry automatically — please try again in 30 seconds."
        else:
            msg = "Plugin generation is temporarily unavailable. Please try again in a moment."
        return jsonify({"error": msg, "detail": str(e)[:200]}), 503

    final_code = _brand_free_tier(result.code) if tier == "free" else result.code

    log_request(
        ip=ip, endpoint="/api/generate", tier=tier,
        instruction=instruction,
        success=result.success,
        attempts=result.attempts,
        elapsed=result.elapsed_seconds,
        compile_ok=result.compile_result.success if result.compile_result else None,
        yml_ok=result.yml_result.valid if result.yml_result else None,
        errors=result.final_errors or None,
        code=final_code,
        sa_used=gen.use_smart_assembly,
        sa_features=gen.sa_features or None,
    )

    # Auto-submit to gallery on successful compile.
    # Free tier: always public. Paid tiers: respect the `private` flag.
    gallery_id = None
    if result.success:
        _gallery_private = bool(data.get("private", False)) and tier in _PAID_PLANS
        _gallery_public  = not _gallery_private
        _g_name = str(data.get("plugin_name") or data.get("project_name")
                       or _extract_plugin_name(final_code) or "Unnamed Plugin")
        _g_instr = data.get("instruction", "")
        try:
            gallery_id = submit_gallery(
                instruction=_g_instr,
                plugin_name=_g_name,
                code=final_code,
                tier=tier,
                public=_gallery_public,
                ip_hash=hashlib.sha256(ip.encode()).hexdigest()[:16],
            )
            if _gallery_public:
                try:
                    _discord_gallery_webhook(_g_name, gallery_id)
                except Exception:
                    pass
        except Exception:
            pass

    response_payload = {
        "request_id":     "gen_" + secrets.token_hex(6),
        "success":        result.success,
        "code": final_code,
        "attempts": result.attempts,
        "elapsed_seconds": round(result.elapsed_seconds, 1),
        "warnings": result.static_warnings,
        "errors": result.final_errors,
        "compile_ok": result.compile_result.success if result.compile_result else None,
        "yml_ok": result.yml_result.valid if result.yml_result else None,
        "gallery_id": gallery_id,
        "plugin_name": _g_name if result.success else None,
        "target_api": target_api,
    }

    user = _current_user()
    if user and save_project:
        # Charge credits proportionally: 1-3 attempts=1, 4-6=2, 7+=3
        credits_used = max(1, math.ceil(result.attempts / 3))
        try:
            increment_user_generation(int(user["id"]), credits_used)
        except Exception:
            pass
        try:
            save_user_project(
                user_id=int(user["id"]),
                project_name=str(_g_name or data.get("project_name") or data.get("plugin_name") or "Untitled Plugin"),
                plugin_type=str(data.get("plugin_type", "full_plugin")),
                target_api=target_api,
                features=list(data.get("features", [])) if isinstance(data.get("features", []), list) else [],
                instruction=data.get("instruction", ""),
                full_instruction=instruction,
                include_tests=bool(data.get("include_tests", True)),
                skip_compile=skip_compile,
                success=result.success,
                compile_ok=result.compile_result.success if result.compile_result else None,
                generated_code=final_code,
                warnings=result.static_warnings,
                errors=result.final_errors,
                metadata={
                    "attempts": result.attempts,
                    "elapsed_seconds": round(result.elapsed_seconds, 1),
                    "yml_ok": result.yml_result.valid if result.yml_result else None,
                },
            )
            response_payload["saved"] = True
        except Exception:
            response_payload["saved"] = False

    return jsonify(response_payload)


# v1 alias for /api/generate (registered after function definition)
app.add_url_rule("/api/v1/generate", endpoint="api_v1_generate", view_func=generate, methods=["POST"])


# ---------------------------------------------------------------------------
# /api/generate-progress  — SSE generation with web search + thinking stream
# ---------------------------------------------------------------------------

@app.route("/api/generate-progress", methods=["POST"])
@limiter.limit(FREE_MONTHLY_LIMIT, key_func=get_remote_address,
               exempt_when=lambda: get_tier() == "pro" or _authenticated_user() is not None,
               error_message=json.dumps({
                   "error": "Free tier limit reached (3 plugins/month). Upgrade at stacknests.com/pricing.",
                   "upgrade_url": "/#pricing"
               }))
def generate_progress():
    """
    Like /api/generate but streams real-time progress via SSE.

    SSE event types emitted as JSON in `data:` lines:
      {"type":"phase",  "step":"...", "percent":N, "thinking":"..."}
      {"type":"web_results", "results":[{title,snippet,url}, ...]}
      {"type":"done",   ... full generate result payload ...}
      {"type":"error",  "message":"..."}
    """
    ip = get_remote_address()
    if is_banned(ip):
        return jsonify({"error": "Access denied."}), 403

    req_data = request.get_json(silent=True)
    if not req_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    instruction = req_data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "'instruction' field is required"}), 400
    if len(instruction) > 2000:
        return jsonify({"error": "Instruction too long (max 2000 chars)"}), 400

    skip_compile  = bool(req_data.get("skip_compile", False))
    folia         = bool(req_data.get("folia_compatible", False))
    save_project  = bool(req_data.get("save_project", True))
    target_api    = _normalize_target_api(str(req_data.get("target_api", "26.1.x")))
    paper_profile = _paper_profile_for_target_api(target_api)
    use_web       = bool(req_data.get("web_search", True))
    tier          = get_tier()
    plan          = _effective_editor_plan()   # free / starter / pro / studio
    user_pre      = _authenticated_user()

    # Refinement mode — gated behind Starter+ (free tier: single-class only)
    previous_code = req_data.get("previous_code", "").strip()
    if previous_code and plan == "free":
        return jsonify({
            "error": "Code refinement is a Starter+ feature. Upgrade at stacknests.com/pricing.",
            "upgrade_url": "/pricing",
        }), 403
    if previous_code:
        code_snippet = previous_code[:50000]
        large_plugin = len(previous_code) > 15000
        large_hint = (
            "\nNOTE: This is a large multi-file plugin. "
            "You MUST output ALL existing files in full — every ```java block, "
            "plugin.yml, and build file — even if only one file changes. "
            "Do NOT drop or omit any file that exists in the === EXISTING CODE ===."
            if large_plugin else ""
        )
        instruction = (
            f"You are modifying an existing Minecraft plugin. "
            f"Keep all existing functionality unless the change request says to remove or replace it. "
            f"Only apply the change described below.{large_hint}\n\n"
            f"=== EXISTING CODE ===\n{code_snippet}\n=== END EXISTING CODE ===\n\n"
            f"Change request: {instruction}"
        )

    ok, safety_err = _check_instruction_safety(instruction)
    if not ok:
        return jsonify({"error": safety_err}), 400

    if user_pre and not user_pre.get("verified") and tier == "free" and not is_bypassed(ip):
        return jsonify({
            "error": "Please verify your email address before generating plugins.",
            "unverified": True,
        }), 403

    if user_pre and not is_bypassed(ip):
        allowed, usage = check_user_generation_limit(int(user_pre["id"]))
        if not allowed:
            plan  = usage.get("plan", "free")
            limit = usage["gens_limit"]
            days  = usage["days_until_reset"]
            hint  = " Upgrade to Pro for 100/month." if plan == "free" else ""
            return jsonify({
                "error": f"Monthly generation limit reached ({limit}/month).{hint} Resets in {days} days.",
                "usage": usage,
                "upgrade_url": "/pricing",
            }), 429

    # Strip chatbot meta-instructions that confuse the model
    instruction = _strip_meta_instructions(instruction)

    if folia:
        instruction = instruction.rstrip(".") + ". This plugin must be Folia-compatible."

    # Inject an explicit plugin name at the top of the instruction
    _name_hint = str(req_data.get("plugin_name") or req_data.get("project_name") or "").strip()
    if not _name_hint:
        _name_hint = _extract_instruction_plugin_name(instruction) or ""
    if _name_hint and not instruction.startswith("Plugin name:"):
        instruction = f"Plugin name: {_name_hint}\n\n{instruction}"

    # Snapshot mutable request context for the worker thread
    _tier        = tier
    _plan        = plan
    _ip          = ip
    _req_data    = dict(req_data)
    _instruction = instruction
    _skip        = skip_compile
    _user        = user_pre
    _private     = bool(req_data.get("private", False))

    q: queue.Queue = queue.Queue()

    def _worker():
        """Background thread: run generation and push SSE events onto the queue."""
        try:
            # ── Phase 1: analyse instruction ───────────────────────────────
            q.put(json.dumps({
                "type": "phase", "percent": 5,
                "step": "Analysing your plugin requirements\u2026",
                "thinking": f"Instruction: {_instruction[:150]}\u2026",
            }))

            # ── Phase 2: web search ────────────────────────────────────────
            final_instruction = _instruction
            web_results = []
            if use_web:
                sq = f"Paper Minecraft plugin API {_instruction[:70]} {target_api} latest"
                q.put(json.dumps({
                    "type": "phase", "percent": 10,
                    "step": "Searching the web for the latest API info\u2026",
                    "thinking": f"Search query: {sq}",
                }))
                web_results = _web_search_cached(sq, max_results=4)
                if web_results:
                    q.put(json.dumps({"type": "web_results", "results": web_results}))
                    snippets = "\n".join(
                        f"- {r['title']}: {r['snippet']}"
                        for r in web_results[:3]
                        if r.get("snippet")
                    )
                    if snippets:
                        final_instruction = (
                            _instruction
                            + f"\n\n[Web context — latest API info:]\n{snippets}"
                        )

            # ── Phase 2b: inject live PaperMC doc snippets ─────────────────
            doc_context = get_doc_context(_instruction)
            if doc_context:
                final_instruction = final_instruction + doc_context

            # ── Phase 3: retrieve training examples ────────────────────────
            q.put(json.dumps({
                "type": "phase", "percent": 15,
                "step": "Retrieving similar plugin examples\u2026",
                "thinking": "Querying ChromaDB for in-context training examples.",
            }))

            # ── Phase 4: start AI generation ───────────────────────────────
            model_hint = "Claude (Pro)" if _tier == "pro" else "Gemini Flash"
            q.put(json.dumps({
                "type": "phase", "percent": 20,
                "step": "Sending request to AI model\u2026",
                "thinking": f"Model: {model_hint}. Awaiting first token\u2026",
            }))

            gen = PluginGenerator(
                router=_router,
                params=GenerationParams(max_tokens=3000),
                skip_compile=_skip,
                tier=_to_inference_tier(_tier),
                plan=_plan,
                paper_target_profile=paper_profile,
                use_smart_assembly=(_plan in _PAID_PLANS),
            )

            # Run PluginGenerator in a sub-thread so we can emit timed
            # progress updates while it works.
            result_holder: list = [None]
            exc_holder:    list = [None]

            def _gen_thread():
                try:
                    result_holder[0] = gen.generate(final_instruction)
                except Exception as exc:
                    exc_holder[0] = exc

            gen_thread = threading.Thread(target=_gen_thread, daemon=True)
            gen_thread.start()
            _gen_start = time.time()

            # Emit timed phase updates while generation is running.
            _phases = [
                (30, "Generating Java source code\u2026",
                 "Writing main plugin class, commands, and event listeners\u2026"),
                (42, "Writing plugin.yml descriptor\u2026",
                 "Building name, version, commands, permissions, and api-version\u2026"),
                (54, "Generating JUnit test stubs\u2026",
                 "Creating MockBukkit-based unit tests for each command\u2026"),
                (65, f"Compiling against Paper {target_api} API\u2026",
                 "Running javac with Paper, MockBukkit, and JUnit 5 jars\u2026"),
                (74, "Running static analysis\u2026",
                 "Checking for deprecated ChatColor, missing registrations, NMS use\u2026"),
                (82, "Validating plugin.yml\u2026",
                 "Checking main class, api-version, commands section\u2026"),
                (89, "Reviewing output quality\u2026",
                 "Verifying no truncated methods, no empty catch blocks\u2026"),
                (94, "Applying final corrections\u2026",
                 "Running healing pass if any compile errors remain\u2026"),
            ]
            # Rotating messages shown after all phases are emitted (long generations,
            # Claude rate-limit back-off, heal loops, etc.)
            _heartbeat_msgs = [
                "AI is still writing your plugin\u2026",
                "Complex features take a moment\u2014hang tight\u2026",
                "Compiling and checking for errors\u2026",
                "Healing any compile issues found\u2026",
                "Large plugins can take 60\u2013120 s\u2014almost there\u2026",
                "Still working\u2014validating the output\u2026",
                "Running final quality checks\u2026",
                "Applying last-minute fixes\u2026",
            ]
            _hb_idx = 0
            phase_idx = 0

            while gen_thread.is_alive():
                gen_thread.join(timeout=3.5)
                if phase_idx < len(_phases):
                    pct, step, thinking = _phases[phase_idx]
                    q.put(json.dumps({
                        "type": "phase", "percent": pct,
                        "step": step, "thinking": thinking,
                    }))
                    phase_idx += 1
                else:
                    # All scripted phases done but generation still running —
                    # send a heartbeat tick so the UI doesn't appear frozen.
                    elapsed = round(time.time() - _gen_start)
                    msg = _heartbeat_msgs[_hb_idx % len(_heartbeat_msgs)]
                    _hb_idx += 1
                    q.put(json.dumps({
                        "type": "tick",
                        "elapsed": elapsed,
                        "step": msg,
                    }))

            # ── Thread finished ────────────────────────────────────────────
            if exc_holder[0] is not None:
                e = exc_holder[0]
                tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                print(f"[Generation] Exception in generation thread:\n{tb_str}", flush=True)
                err_str = str(e).lower()
                if "rate" in err_str or "quota" in err_str or "429" in err_str:
                    msg = "AI quota temporarily exhausted. Please try again in a few minutes."
                elif "auth" in err_str or "key" in err_str:
                    msg = "AI backend configuration error. Please contact support."
                elif "timeout" in err_str or "connection" in err_str:
                    msg = "AI backend is unreachable. Please try again in 30 seconds."
                else:
                    msg = "Plugin generation is temporarily unavailable. Please try again."
                q.put(json.dumps({"type": "error", "message": msg, "detail": str(e)[:200]}))
                return

            result = result_holder[0]
            final_code = _brand_free_tier(result.code) if _tier == "free" else result.code

            q.put(json.dumps({
                "type": "phase", "percent": 98,
                "step": "Finalising result\u2026",
                "thinking": (
                    f"success={result.success}  attempts={result.attempts}"
                    f"  elapsed={round(result.elapsed_seconds, 1)}s"
                    + ("  compile=OK" if (result.compile_result and result.compile_result.success) else "  compile=failed")
                    + ("  yml=OK" if (result.yml_result and result.yml_result.valid) else "  yml=invalid")
                ),
            }))

            log_request(
                ip=_ip, endpoint="/api/generate-progress", tier=_tier,
                instruction=_instruction,
                success=result.success, attempts=result.attempts,
                elapsed=result.elapsed_seconds,
                compile_ok=result.compile_result.success if result.compile_result else None,
                yml_ok=result.yml_result.valid if result.yml_result else None,
                errors=result.final_errors or None,
                code=final_code,
                sa_used=gen.use_smart_assembly,
                sa_features=gen.sa_features or None,
            )

            _resolved_name = (_req_data.get("plugin_name") or _req_data.get("project_name")
                               or _extract_plugin_name(final_code) or "")

            payload = {
                "type":             "done",
                "success":          result.success,
                "code":             final_code,
                "attempts":         result.attempts,
                "elapsed_seconds":  round(result.elapsed_seconds, 1),
                "warnings":         result.static_warnings,
                "errors":           result.final_errors,
                "compile_ok":       result.compile_result.success if result.compile_result else None,
                "yml_ok":           result.yml_result.valid if result.yml_result else None,
                "web_search_used":  len(web_results) > 0,
                "web_result_count": len(web_results),
                "plugin_name":      _resolved_name or None,
                "target_api":       target_api,
            }

            # Auto-submit to gallery on successful compile.
            # Free tier: always public. Paid tiers: respect the `private` flag.
            if result.success:
                _gallery_private = _private and _tier in _PAID_PLANS
                _gallery_public  = not _gallery_private
                _g_name  = str(_req_data.get("plugin_name") or _req_data.get("project_name")
                                or _extract_plugin_name(final_code) or "Unnamed Plugin")
                _g_instr = _req_data.get("instruction", "")
                try:
                    payload["plugin_name"] = _g_name
                    _gid = submit_gallery(
                        instruction=_g_instr,
                        plugin_name=_g_name,
                        code=final_code,
                        tier=_tier,
                        public=_gallery_public,
                        ip_hash=hashlib.sha256(_ip.encode()).hexdigest()[:16],
                    )
                    payload["gallery_id"] = _gid
                    if _gallery_public:
                        try:
                            _discord_gallery_webhook(_g_name, _gid)
                        except Exception:
                            pass
                except Exception:
                    pass

            if _user and save_project:
                credits_used = max(1, math.ceil(result.attempts / 3))
                try:
                    increment_user_generation(int(_user["id"]), credits_used)
                except Exception:
                    pass
                try:
                    save_user_project(
                        user_id=int(_user["id"]),
                        project_name=str(_resolved_name or "Untitled Plugin"),
                        plugin_type=str(_req_data.get("plugin_type", "full_plugin")),
                        target_api=target_api,
                        features=list(_req_data.get("features", [])),
                        instruction=_req_data.get("instruction", ""),
                        full_instruction=_instruction,
                        include_tests=bool(_req_data.get("include_tests", True)),
                        skip_compile=_skip,
                        success=result.success,
                        compile_ok=result.compile_result.success if result.compile_result else None,
                        generated_code=final_code,
                        warnings=result.static_warnings,
                        errors=result.final_errors,
                        metadata={
                            "attempts":         result.attempts,
                            "elapsed_seconds":  round(result.elapsed_seconds, 1),
                            "yml_ok":           result.yml_result.valid if result.yml_result else None,
                            "web_search_used":  len(web_results) > 0,
                        },
                    )
                    payload["saved"] = True
                except Exception:
                    payload["saved"] = False

            q.put(json.dumps(payload))

        except Exception as e:
            traceback.print_exc()
            q.put(json.dumps({"type": "error", "message": str(e)[:300]}))
        finally:
            q.put(None)  # sentinel → close the stream

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    def _event_stream():
        while True:
            try:
                item = q.get(timeout=25)
            except queue.Empty:
                yield ": heartbeat\n\n"  # keep connection alive through proxies
                continue
            if item is None:
                break
            yield f"data: {item}\n\n"

    return Response(
        _event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Compiled mod JAR store                                                       #
# Keyed by a UUID token; entries expire after 2 hours.  Served by             #
# /api/mod-jar/<token>.                                                        #
# --------------------------------------------------------------------------- #

import uuid as _uuid_mod
_mod_jar_store: dict[str, dict] = {}   # token → {bytes, name, expires_at}
_MOD_JAR_TTL = 7200  # 2 hours in seconds

def _mod_jar_store_put(jar_bytes: bytes, jar_name: str) -> str:
    """Store JAR bytes and return a download token."""
    import time as _t
    # Evict expired entries
    now = _t.time()
    expired = [k for k, v in _mod_jar_store.items() if v["expires_at"] < now]
    for k in expired:
        del _mod_jar_store[k]

    token = _uuid_mod.uuid4().hex
    _mod_jar_store[token] = {
        "bytes":      jar_bytes,
        "name":       jar_name,
        "expires_at": now + _MOD_JAR_TTL,
    }
    return token


@app.route("/api/mod-jar/<token>", methods=["GET"])
def download_mod_jar(token: str):
    """Serve a compiled mod JAR by its short-lived download token."""
    import time as _t
    entry = _mod_jar_store.get(token)
    if not entry or entry["expires_at"] < _t.time():
        return jsonify({"error": "JAR download link has expired or is invalid."}), 404

    resp = make_response(entry["bytes"])
    resp.headers["Content-Type"]        = "application/java-archive"
    resp.headers["Content-Disposition"] = f'attachment; filename="{entry["name"]}"'
    return resp


# --------------------------------------------------------------------------- #
# /api/generate-mod-progress  — Fabric / Forge / NeoForge mod generation     #
# --------------------------------------------------------------------------- #

_MOD_INJECTION_PHRASES = [
    "ignore previous", "ignore your instructions", "ignore all",
    "forget your", "forget all previous", "you are now", "pretend you are",
    "act as", "roleplay as", "jailbreak", "override your", "system prompt",
    "disregard", "developer mode",
    # NOTE: "bypass" is handled by the context-sensitive _HARMFUL_CONTEXT_TERMS check.
]

_MOD_MINECRAFT_TERMS = [
    "mod", "fabric", "forge", "neoforge", "minecraft", "item", "block",
    "entity", "player", "world", "event", "command", "recipe", "crafting",
    "dimension", "biome", "structure", "keybind", "config", "enchantment",
    "mob", "loot", "nbt", "tag", "registry", "datapack", "resource", "java",
]

_MOD_HARMFUL_TERMS = [
    "malware", "ransomware", "backdoor", "keylogger", "ddos",
    "reverse shell", "sql inject", "remote code execution",
]


def _check_mod_safety(text: str):
    lower = " " + text.lower() + " "
    for phrase in _MOD_INJECTION_PHRASES:
        if phrase in lower:
            return False, "Invalid request. Please describe a Minecraft mod to create."
    for term in _MOD_HARMFUL_TERMS:
        if term in lower:
            return False, "This request cannot be processed."
    # Context-sensitive: only block "exploit"/"hack"/"bypass" without a mitigation nearby
    for term in _HARMFUL_CONTEXT_TERMS:
        idx = lower.find(term)
        while idx != -1:
            window = lower[max(0, idx - 60): idx + len(term) + 60]
            if not any(m in window for m in _HARMFUL_CONTEXT_MITIGATIONS):
                return False, "This request cannot be processed."
            idx = lower.find(term, idx + 1)
    if not any(term in lower for term in _MOD_MINECRAFT_TERMS):
        return False, (
            "StackNest generates Minecraft mods only. "
            "Please describe your mod feature — e.g. a new item, block, command, or event."
        )
    return True, None


def _check_mod_output(code: str, loader: str) -> list[str]:
    """
    Return a list of required-but-missing block types.
    Used to detect incomplete mod output so we can retry with a correction prompt.
    """
    missing = []
    if "```java" not in code:
        missing.append("main Java class (```java block)")
    if loader == "fabric":
        if "```json" not in code:
            missing.append("fabric.mod.json (```json block)")
    else:
        if "```toml" not in code:
            missing.append("mods.toml (```toml block)")
    has_gradle = any(f in code for f in ("```gradle", "```groovy", "```kotlin"))
    if not has_gradle:
        missing.append("build.gradle or build.gradle.kts (```gradle/groovy/kotlin block)")

    # Fabric import-wall check: if the main class imports sub-packages of the mod's
    # own namespace (e.g. com.mymod.block.ModBlocks) but no java block declares
    # those sub-packages, the model has the import-wall anti-pattern — those classes
    # are referenced but never generated, so Gradle compile will fail.
    if loader == "fabric" and "```java" in code:
        java_blocks = re.findall(r"```java[ \t]*\n(.*?)```", code, re.DOTALL)
        if java_blocks:
            # Collect all declared packages across all java blocks
            declared_pkgs: set[str] = set()
            for block in java_blocks:
                pm = re.search(r"^\s*package\s+([\w.]+)\s*;", block, re.MULTILINE)
                if pm:
                    declared_pkgs.add(pm.group(1))

            # Find imports in the first block that look like internal mod classes
            first_block = java_blocks[0]
            mod_root = min(declared_pkgs, key=len) if declared_pkgs else ""
            if mod_root and "." in mod_root:  # e.g. com.mymod.mymod
                orphan_imports: list[str] = []
                for imp_m in re.finditer(
                    r"import\s+(" + re.escape(mod_root) + r"\.\w[\w.]*)\s*;",
                    first_block,
                ):
                    imported_fqn = imp_m.group(1)
                    imported_pkg = imported_fqn.rsplit(".", 1)[0]
                    if imported_pkg not in declared_pkgs:
                        orphan_imports.append(imported_fqn.rsplit(".", 1)[-1])
                if orphan_imports:
                    missing.append(
                        f"Import-wall: classes {', '.join(orphan_imports[:4])} are imported "
                        f"but never defined. Add a ```java block for each, or use private "
                        f"static nested classes inside the main class instead."
                    )

    return missing


# Patterns that detect wrong/removed APIs in the generated mod Java source.
# Each entry: (regex, severity, message)   severity = "error" | "warning"
_MOD_STATIC_PATTERNS: dict[str, list[tuple[str, str, str]]] = {
    "fabric": [
        # Removed text API (pre-1.19)
        (r"new\s+LiteralText\s*\(",       "error",   "new LiteralText() was removed in 1.19 — use Text.literal() instead"),
        (r"new\s+TranslatableText\s*\(",  "error",   "new TranslatableText() was removed in 1.19 — use Text.translatable() instead"),
        # Wrong-loader imports
        (r"\bFMLJavaModLoadingContext\b",  "error",   "FMLJavaModLoadingContext is a Forge class — cannot be used in a Fabric mod"),
        (r"import\s+net\.minecraftforge\.", "error",  "net.minecraftforge.* is Forge-only — cannot be used in a Fabric mod"),
        (r"import\s+net\.neoforged\.",     "error",   "net.neoforged.* is NeoForge-only — cannot be used in a Fabric mod"),
        (r"import\s+org\.bukkit\.",        "error",   "Bukkit/Paper API cannot be used in mods — use Fabric API instead"),
        # Wrong message API
        (r"\.sendMessage\s*\(\s*\"",       "warning", "sendMessage(String) — use player.sendMessage(Text.literal(...)) in Fabric 1.21"),
        # Wrong registry API (1.20+)
        (r"\bRegistry\.register\s*\(\s*Registry\.", "error", "Registry.register(Registry.X, ...) is pre-1.20 — use Registry.register(Registries.X, ...) instead"),
        (r"import\s+net\.minecraft\.util\.registry\.Registry\b", "error", "net.minecraft.util.registry.Registry is pre-1.19 — use net.minecraft.registry.Registries"),
        # Wrong event / init pattern
        (r"implements\s+ModInitializer[^;{]*\{[^}]*@Override\s+public\s+void\s+onInitialize\s*\(\s*FabricLoader", "error", "FabricLoader is not a parameter of onInitialize() — use FabricLoader.getInstance() inside the method body"),
        # Deprecated Log4j in Fabric (use SLF4J)
        (r"import\s+org\.apache\.logging\.log4j\.LogManager", "warning", "Use SLF4J (LoggerFactory) in Fabric — not Log4j LogManager"),
        # Client API called from wrong entrypoint marker
        (r"MinecraftClient\.getInstance\(\)", "warning", "MinecraftClient.getInstance() should only be called from a ClientModInitializer or client-tick context"),
    ],
    "forge": [
        # Critical removed API
        (r"\bFMLJavaModLoadingContext\b",  "error",   "FMLJavaModLoadingContext was removed in Forge 51.x — use IEventBus constructor injection"),
        # Wrong-loader imports
        (r"import\s+net\.neoforged\.",     "error",   "net.neoforged.* is NeoForge-only — cannot be used in Forge"),
        (r"import\s+org\.bukkit\.",        "error",   "Bukkit/Paper API cannot be used in mods"),
        # Removed text API
        (r"new\s+LiteralText\s*\(",        "error",   "new LiteralText() was removed — use Component.literal() instead"),
        # Deprecated Forge APIs (pre-1.20)
        (r"\bDistExecutor\b",              "warning", "DistExecutor is deprecated in 1.20+ — use @Mod.EventBusSubscriber(value=Dist.CLIENT) instead"),
        (r"\bObjectHolder\b",              "warning", "@ObjectHolder is deprecated — use RegistryObject or DeferredRegister instead"),
        (r"\bIForgeRegistry\b",            "warning", "IForgeRegistry is replaced — use DeferredRegister and RegistryObject (vanilla registry) in 1.20+"),
        (r"ForgeRegistries\.\w+\.register\s*\(", "warning", "Direct ForgeRegistries.X.register() is unsafe — wrap in DeferredRegister.create() event"),
        # Wrong event bus usage
        (r"MinecraftForge\.EVENT_BUS\.register\s*\(\s*this\s*\)", "warning", "Registering 'this' on MinecraftForge.EVENT_BUS only works for @SubscribeEvent instance methods — mod setup/registry events go on modEventBus"),
        # Capability system change (1.20.1+)
        (r"ICapabilityProvider\b",         "warning", "ICapabilityProvider/getCapability changed in 1.20.1 — verify your capability provider implements the new interface"),
    ],
    "neoforge": [
        # Wrong-loader imports
        (r"import\s+net\.minecraftforge\.", "error",  "net.minecraftforge.* is Forge-only — use net.neoforged.* instead"),
        (r"import\s+org\.bukkit\.",         "error",  "Bukkit/Paper API cannot be used in mods"),
        # Critical removed API
        (r"\bFMLJavaModLoadingContext\b",   "error",  "FMLJavaModLoadingContext was removed — use IEventBus constructor injection"),
        # Removed text API
        (r"new\s+LiteralText\s*\(",         "error",  "new LiteralText() was removed — use Component.literal() instead"),
        # NeoForge-specific wrong patterns
        (r"import\s+net\.minecraftforge\.fml\.common\.Mod\b", "error", "Use net.neoforged.fml.common.Mod — the Forge import doesn't exist in NeoForge"),
        (r"@Mod\.EventBusSubscriber",       "warning", "@Mod.EventBusSubscriber is Forge-only — use @EventBusSubscriber (net.neoforged.fml.common.eventhandler) in NeoForge"),
        (r"NeoForge\.EVENT_BUS\b",          "warning", "Verify NeoForge.EVENT_BUS is imported from net.neoforged.neoforge.common.NeoForge"),
    ],
}

# Patterns applied to all loaders
_MOD_STATIC_PATTERNS_ALL: list[tuple[str, str, str]] = [
    (r"//\s*(?:TODO|FIXME|implement this|your code here)\b", "warning", "Placeholder/TODO comment found — replace with real implementation"),
    (r"(?<!['\"])\.\.\.",                                     "warning", "Ellipsis placeholder (...) found — code may be incomplete"),
    # Wrong server message API (all loaders)
    (r"\.sendMessage\s*\(\s*new\s+\w*Text\s*\(",              "error",   "sendMessage(new XxxText(...)) — use sendMessage(Text.literal(...)) or sendMessage(Component.literal(...))"),
    # Mixed-loader confusion
    (r"import\s+net\.minecraft\.server\.MinecraftServer\b",   "warning", "net.minecraft.server.MinecraftServer is NMS — use the loader-appropriate server accessor instead"),
]


def _extract_java_block(code: str) -> str:
    """Extract the content of the first ```java block from the response."""
    idx = code.find("```java")
    if idx == -1:
        return ""
    start = code.find("\n", idx) + 1
    end   = code.find("```", start)
    return code[start:end] if end != -1 else code[start:]


def _is_java_truncated(java_block: str) -> bool:
    """Return True if the Java block appears cut off.

    Handles two failure modes:
    1. Imports-only: model stopped after import lines — no class declaration written.
    2. Brace imbalance: class was started but method bodies were cut off.
    """
    if not java_block:
        return False
    # Imports-only: the entire class body is missing — no class/interface/enum/record declaration.
    # open_braces == 0 so the old count check (>2) would return False, silently accepting
    # broken output that then fails Gradle compilation with no errors recorded.
    has_class = bool(re.search(
        r"(?:public|private|protected)?\s*(?:abstract\s+)?(?:class|interface|enum|record)\s+\w+",
        java_block,
    ))
    if not has_class:
        return True
    # Brace imbalance: class was opened but methods/class weren't closed.
    return java_block.count("{") - java_block.count("}") > 2


def _check_mod_static(java_code: str, loader: str) -> tuple[list[str], list[str]]:
    """
    Run static analysis on generated mod Java source.
    Returns (errors, warnings) — lists of human-readable strings.
    These are shown in the IDE issues panel and fed into the correction prompt.
    """
    errors: list[str]   = []
    warnings: list[str] = []
    loader_patterns = _MOD_STATIC_PATTERNS.get(loader, [])
    for pattern, severity, message in loader_patterns + _MOD_STATIC_PATTERNS_ALL:
        if re.search(pattern, java_code):
            (errors if severity == "error" else warnings).append(message)
    return errors, warnings


def _parse_mod_files(code: str, loader: str) -> list[dict]:
    """
    Parse code blocks from the AI response into a structured file list.
    Returns [{'name': '...', 'lang': '...', 'content': '...'}, ...].
    Used to populate payload.files for structured download.
    """
    files: list[dict] = []
    java_count = 0
    for m in re.finditer(r'```(\w+)\n([\s\S]*?)```', code):
        lang    = m.group(1).lower()
        content = m.group(2).strip()
        if lang == 'java':
            java_count += 1
            name = 'ExampleMod.java' if java_count == 1 else f'ExtraClass{java_count}.java'
            files.append({'name': name, 'lang': 'java', 'content': content})
        elif lang == 'json' and loader == 'fabric':
            files.append({'name': 'fabric.mod.json', 'lang': 'json', 'content': content})
        elif lang == 'toml':
            meta_name = 'neoforge.mods.toml' if loader == 'neoforge' else 'mods.toml'
            files.append({'name': f'META-INF/{meta_name}', 'lang': 'toml', 'content': content})
        elif lang in ('gradle', 'groovy', 'kotlin'):
            gradle_name = 'build.gradle.kts' if loader in ('fabric', 'neoforge') else 'build.gradle'
            files.append({'name': gradle_name, 'lang': lang, 'content': content})
    return files


@app.route("/api/generate-mod-progress", methods=["POST"])
@limiter.limit(FREE_MONTHLY_LIMIT, key_func=get_remote_address,
               exempt_when=lambda: get_tier() == "pro" or _authenticated_user() is not None,
               error_message=json.dumps({
                   "error": "Free tier limit reached (3/month). Upgrade at stacknests.com/#pricing.",
                   "upgrade_url": "/#pricing",
               }))
def generate_mod_progress():
    """
    Generate a Fabric / Forge / NeoForge mod and stream SSE progress events.
    Same SSE format as /api/generate-progress (phase / done / error).
    No server-side compilation — returns generated source files only.
    """
    ip = get_remote_address()
    if is_banned(ip):
        return jsonify({"error": "Access denied."}), 403

    req_data = request.get_json(silent=True)
    if not req_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    instruction = req_data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "'instruction' field is required"}), 400
    if len(instruction) > 2000:
        return jsonify({"error": "Instruction too long (max 2000 chars)"}), 400

    ok, safety_err = _check_mod_safety(instruction)
    if not ok:
        return jsonify({"error": safety_err}), 400

    loader     = req_data.get("loader", "fabric").lower()
    mc_version = req_data.get("mc_version", "1.21").strip()
    use_web    = bool(req_data.get("web_search", True))
    _tier      = get_tier()
    _user      = _current_user()

    if loader not in ("fabric", "forge", "neoforge"):
        return jsonify({"error": "loader must be 'fabric', 'forge', or 'neoforge'"}), 400

    if _user and not _user.get("verified") and _tier == "free" and not is_bypassed(ip):
        return jsonify({
            "error": "Please verify your email address before generating mods.",
            "unverified": True,
        }), 403

    if _user and not is_bypassed(ip):
        allowed, usage = check_user_generation_limit(int(_user["id"]))
        if not allowed:
            plan  = usage.get("plan", "free")
            limit = usage["gens_limit"]
            days  = usage["days_until_reset"]
            hint  = " Upgrade to Pro for 100/month." if plan == "free" else ""
            return jsonify({
                "error": f"Monthly mod generation limit reached ({limit}/month).{hint}",
                "upgrade_url": "/pricing",
                "usage": usage,
            }), 429

    q: queue.Queue = queue.Queue()

    def _worker():
        try:
            from inference.router import build_mod_prompt, MOD_SYSTEM_PROMPTS

            _instruction = instruction

            # Phase 1: analyse
            q.put(json.dumps({
                "type": "phase", "percent": 5,
                "step": "Analysing your mod requirements\u2026",
                "thinking": f"Loader: {loader}  MC: {mc_version}  Instruction: {_instruction[:120]}\u2026",
            }))

            # Phase 2: web search for latest API changes
            web_results = []
            final_instruction = _instruction
            if use_web:
                loader_label = loader.capitalize()
                sq = f"{loader_label} Minecraft mod API {_instruction[:60]} {mc_version}"
                q.put(json.dumps({
                    "type": "phase", "percent": 10,
                    "step": "Searching for latest mod API documentation\u2026",
                    "thinking": f"Query: {sq}",
                }))
                web_results = _web_search_cached(sq, max_results=4)
                if web_results:
                    q.put(json.dumps({"type": "web_results", "results": web_results}))
                    snippets = "\n".join(
                        f"- {r['title']}: {r['snippet']}"
                        for r in web_results[:3]
                        if r.get("snippet")
                    )
                    if snippets:
                        final_instruction = (
                            _instruction
                            + f"\n\n[Web context \u2014 latest {loader_label} API info:]\n{snippets}"
                        )

            # Phase 3: build prompt
            doc_context = get_mod_doc_context(_instruction, loader)
            q.put(json.dumps({
                "type": "phase", "percent": 18,
                "step": "Building generation prompt\u2026",
                "thinking": f"Injecting {loader} skeleton template + {len(doc_context)} chars of doc context.",
            }))

            prompt = build_mod_prompt(
                final_instruction,
                loader=loader,
                mc_version=mc_version,
                doc_context=doc_context,
            )

            # Phase 4: generate (with 1 retry if required output blocks are missing)
            model_hint = "Claude (Pro)" if _tier == "pro" else "Gemini Flash"
            q.put(json.dumps({
                "type": "phase", "percent": 25,
                "step": f"Generating {loader.capitalize()} mod source\u2026",
                "thinking": f"Model: {model_hint}. Awaiting generation\u2026",
            }))

            import time as _time
            _start = _time.time()

            code = ""
            _gen_attempts = 0
            _current_prompt = prompt
            _current_instruction = final_instruction
            _MAX_MOD_ATTEMPTS = 3

            while _gen_attempts < _MAX_MOD_ATTEMPTS:
                result_holder: list = [None]
                exc_holder: list    = [None]

                def _gen(_p=_current_prompt, _instr=_current_instruction):
                    try:
                        from inference.server import generate_with_fallback, GenerationParams
                        from inference.router import MOD_SYSTEM_PROMPTS
                        system = MOD_SYSTEM_PROMPTS.get(loader, "")
                        out, _source = generate_with_fallback(
                            _p,
                            GenerationParams(max_tokens=5000),
                            system_prompt=system,
                            instruction=_instr,
                            tier=_to_inference_tier(_tier),
                            force_cloud=True,
                        )
                        result_holder[0] = out
                    except Exception as exc:
                        exc_holder[0] = exc

                import threading as _threading
                gen_thread = _threading.Thread(target=_gen, daemon=True)
                gen_thread.start()

                # Emit timed filler phases while waiting
                _filler_base = 30 + _gen_attempts * 30  # 2nd attempt starts at ~60%
                _filler = [
                    (_filler_base + 15, "Writing main mod class and event handlers\u2026",
                     "Generating Java source, mod metadata, and Gradle build file\u2026"),
                    (_filler_base + 25, "Generating build.gradle and metadata\u2026",
                     "Building fabric.mod.json / mods.toml configuration\u2026"),
                ]
                _hb_mod_msgs = [
                    "AI is still writing your mod\u2026",
                    "Hang tight\u2014complex mods take a moment\u2026",
                    "Still generating\u2014almost there\u2026",
                ]
                _fi = 0; _hb_mi = 0
                _mod_start = _time.time()
                while gen_thread.is_alive():
                    gen_thread.join(timeout=3.5)
                    if _fi < len(_filler):
                        pct, step, thinking = _filler[_fi]
                        q.put(json.dumps({
                            "type": "phase",
                            "percent": min(pct, 92),
                            "step": step, "thinking": thinking,
                        }))
                        _fi += 1
                    else:
                        elapsed = round(_time.time() - _mod_start)
                        q.put(json.dumps({"type": "tick", "elapsed": elapsed,
                                          "step": _hb_mod_msgs[_hb_mi % len(_hb_mod_msgs)]}))
                        _hb_mi += 1

                if exc_holder[0]:
                    e = exc_holder[0]
                    err_str = str(e).lower()
                    if "rate" in err_str or "quota" in err_str or "429" in err_str:
                        msg = "AI quota temporarily exhausted. Please try again in a few minutes."
                    elif "timeout" in err_str or "connection" in err_str:
                        msg = "AI backend is unreachable. Please try again in 30 seconds."
                    else:
                        msg = "Mod generation temporarily unavailable. Please try again."
                    q.put(json.dumps({"type": "error", "message": msg, "detail": str(e)[:200]}))
                    return

                code = result_holder[0] or ""
                _gen_attempts += 1

                # ── Validate output quality ──────────────────────────────────
                missing_blocks = _check_mod_output(code, loader)
                java_block     = _extract_java_block(code)
                static_errors, _static_warnings = (
                    _check_mod_static(java_block, loader) if java_block else ([], [])
                )
                truncated  = _is_java_truncated(java_block)
                has_issues = bool(missing_blocks or static_errors or truncated)

                if not has_issues or _gen_attempts >= _MAX_MOD_ATTEMPTS:
                    break

                # ── Build targeted correction prompt ─────────────────────────
                # Import-wall entries in missing_blocks are a SYMPTOM of imports-only
                # truncation, not an independent issue.  Treat them as pure truncation.
                _import_wall_only = (
                    bool(missing_blocks)
                    and all("import-wall" in m.lower() for m in missing_blocks)
                )
                if truncated and (not missing_blocks or _import_wall_only) and not static_errors:
                    # Pure truncation OR imports-only — compact-code retry
                    _no_class = not bool(re.search(
                        r"(?:public|private|protected)?\s*(?:abstract\s+)?"
                        r"(?:class|interface|enum|record)\s+\w+",
                        java_block,
                    ))
                    correction_note = (
                        "\n\nIMPORTANT: Your previous response contained ONLY package/import "
                        "lines — the class body was NEVER written.\n"
                        "RULES:\n"
                        "- Write at most 8 import lines. Use fully-qualified names inline for anything else.\n"
                        "- The VERY NEXT line after your imports MUST be the class declaration.\n"
                        "- Put ALL logic in the main class using private static nested classes.\n"
                        "- Keep the entire mod under 120 lines of Java.\n"
                        "Output must be 100% complete \u2014 ALL required blocks, all braces closed."
                    ) if _no_class else (
                        "\n\nIMPORTANT: Your previous response was truncated before all "
                        "closing braces. Regenerate the ENTIRE mod more compactly:\n"
                        "- Put ALL logic in the main class using private static nested classes\n"
                        "- Use lambda expressions instead of anonymous inner classes\n"
                        "- Omit Javadoc; a single-line comment per method is sufficient\n"
                        "Output must be 100% complete \u2014 ALL required blocks, all braces closed."
                    )
                    step_msg     = "Output was truncated \u2014 retrying with compact format\u2026"
                    thinking_msg = (
                        f"Truncated (open braces > close by >2). "
                        f"Retrying compact. attempts={_gen_attempts}"
                    )
                else:
                    issue_parts = []
                    if missing_blocks:
                        # Special case: only java is missing but config files exist.
                        # Asking to "regenerate the COMPLETE mod" causes the model to
                        # start with gradle.properties again and exhaust its context
                        # before ever reaching the java class — the same failure repeats.
                        # Instead, ask it to output ONLY the missing java file(s).
                        java_only_missing = (
                            len(missing_blocks) == 1
                            and "java" in missing_blocks[0].lower()
                            and any(f in code for f in ("```gradle", "```groovy", "```kotlin", "```json", "```toml"))
                        )
                        if java_only_missing:
                            issue_parts.append(
                                "Missing: the Java source file(s) (```java block).\n"
                                "You have already generated the build.gradle and fabric.mod.json "
                                "correctly — do NOT repeat them.\n"
                                "Output ONLY the missing Java main class as a ```java block. "
                                "Put ALL logic (event listeners, registry calls, etc.) inside "
                                "the main class using private static nested classes and lambdas "
                                "to keep it compact."
                            )
                        else:
                            issue_parts.append(
                                "Missing required output blocks:\n" +
                                "\n".join(f"- {m}" for m in missing_blocks)
                            )
                    if static_errors:
                        issue_parts.append(
                            "Code errors that MUST be fixed:\n" +
                            "\n".join(f"- {e}" for e in static_errors)
                        )
                    if truncated:
                        issue_parts.append(
                            "- Java block is truncated (unbalanced braces) \u2014 "
                            "do NOT cut off the output"
                        )
                    # java_only_missing was set in the missing_blocks branch above
                    _java_only = (
                        len(missing_blocks) == 1
                        and "java" in missing_blocks[0].lower()
                        and any(f in code for f in ("```gradle", "```groovy", "```kotlin", "```json", "```toml"))
                    ) if missing_blocks else False

                    if _java_only:
                        correction_note = (
                            "\n\nIMPORTANT: Your previous response had the following issues:\n\n"
                            + "\n\n".join(issue_parts)
                            + "\n\nDo NOT regenerate the entire project. "
                            "Output ONLY the missing Java ```java block(s) now."
                        )
                    else:
                        correction_note = (
                            "\n\nIMPORTANT: Your previous response had the following issues:\n\n"
                            + "\n\n".join(issue_parts)
                            + "\n\nPlease regenerate the COMPLETE mod fixing ALL issues. "
                            "ALL blocks are required: ```java, ```json/toml, and ```gradle."
                        )
                    step_msg     = "Fixing code errors \u2014 retrying\u2026"
                    thinking_msg = (
                        f"Issues: missing={missing_blocks}, "
                        f"static_errors={static_errors}, truncated={truncated}. "
                        f"Retrying. attempts={_gen_attempts}"
                    )

                _current_instruction = final_instruction + correction_note
                _current_prompt = build_mod_prompt(
                    _current_instruction,
                    loader=loader,
                    mc_version=mc_version,
                    doc_context=doc_context,
                )
                q.put(json.dumps({
                    "type": "phase", "percent": 55,
                    "step": step_msg,
                    "thinking": thinking_msg,
                }))

            elapsed = round(_time.time() - _start, 1)

            q.put(json.dumps({
                "type": "phase", "percent": 98,
                "step": "Finalising result\u2026",
                "thinking": f"elapsed={elapsed}s  attempts={_gen_attempts}  loader={loader}  mc={mc_version}",
            }))

            # Final pass: static analysis on whatever came out
            _final_java = _extract_java_block(code)
            _f_errors, _f_warnings = (
                _check_mod_static(_final_java, loader) if _final_java else ([], [])
            )
            if _is_java_truncated(_final_java):
                _f_warnings.append(
                    "Java output may be truncated (unbalanced braces) — review before building"
                )

            # ── Compile the mod via Gradle and produce a .jar ──────────── #
            jar_download_url: str | None = None
            compile_ok: bool | None      = None
            compile_errors: list[str]    = []

            if code.strip() and not _f_errors:
                from validation.mod_compile import compile_mod as _compile_mod

                _MAX_COMPILE_PASSES = 2
                for _cpass in range(1, _MAX_COMPILE_PASSES + 1):
                    q.put(json.dumps({
                        "type": "phase",
                        "percent": 88 + _cpass * 4,
                        "step": (
                            f"Compiling {loader.capitalize()} mod"
                            + (f" (pass {_cpass}/{_MAX_COMPILE_PASSES})" if _cpass > 1 else "") + "…"
                        ),
                        "thinking": (
                            f"Running Gradle build for {loader} {mc_version}. "
                            "First build per loader downloads ~200-500 MB of mod deps and may take a few minutes."
                        ),
                    }))

                    _cr = _compile_mod(code, loader, mc_version)
                    compile_ok = _cr.success

                    if _cr.success and _cr.jar_bytes:
                        token = _mod_jar_store_put(_cr.jar_bytes, _cr.jar_name)
                        jar_download_url = f"/api/mod-jar/{token}"
                        print(
                            f"[mod-compile] {loader} {mc_version} compiled OK → "
                            f"{_cr.jar_name} ({len(_cr.jar_bytes)//1024} KB)"
                        )
                        break

                    compile_errors = _cr.errors
                    print(f"[mod-compile] pass {_cpass} failed: {compile_errors[:3]}")

                    # If this was the last pass, don't regenerate
                    if _cpass >= _MAX_COMPILE_PASSES:
                        break

                    # One correction-regeneration pass using compile errors
                    _err_lines = "\n".join(f"- {e}" for e in compile_errors[:8])
                    _corr = (
                        f"\n\nIMPORTANT: Your previous response failed Gradle compilation. "
                        f"Fix ALL of these errors and regenerate the COMPLETE mod:\n\n"
                        f"{_err_lines}\n\n"
                        "ALL blocks are required: ```java, ```json/toml, and ```gradle."
                    )
                    _fix_prompt = build_mod_prompt(
                        final_instruction + _corr,
                        loader=loader,
                        mc_version=mc_version,
                        doc_context=doc_context,
                    )
                    q.put(json.dumps({
                        "type": "phase", "percent": 93,
                        "step": f"Fixing {len(compile_errors)} compile error(s)…",
                        "thinking": str(compile_errors[:3]),
                    }))
                    try:
                        from inference.server import generate_with_fallback, GenerationParams
                        from inference.router import MOD_SYSTEM_PROMPTS
                        _fixed, _ = generate_with_fallback(
                            _fix_prompt,
                            GenerationParams(max_tokens=5000),
                            system_prompt=MOD_SYSTEM_PROMPTS.get(loader, ""),
                            instruction=final_instruction + _corr,
                            tier=_to_inference_tier(_tier),
                            force_cloud=True,
                        )
                        if _fixed.strip():
                            code = _fixed
                    except Exception as _gen_exc:
                        print(f"[mod-compile] correction regen failed: {_gen_exc}")
                        break

            payload = {
                "type":             "done",
                "success":          bool(code.strip()),
                "code":             code,
                "files":            _parse_mod_files(code, loader),
                "attempts":         _gen_attempts,
                "elapsed_seconds":  elapsed,
                "warnings":         _f_warnings,
                "errors":           _f_errors,
                "compile_ok":       compile_ok,
                # compile_errors=None when Gradle not installed (compile_ok=None)
                # so the frontend doesn't show a false "compile error" panel.
                "compile_errors":   compile_errors if compile_ok is False else [],
                "jar_download_url": jar_download_url,
                "yml_ok":           None,
                "web_search_used":  len(web_results) > 0,
                "mod_loader":       loader,
                "mc_version":       mc_version,
            }

            if _user and bool(req_data.get("save_project", True)):
                try:
                    credits_used = 1
                    increment_user_generation(int(_user["id"]), credits_used)
                except Exception:
                    pass

            try:
                # compile_ok=None means Gradle not installed — not a real error,
                # the generated code itself was fine. Don't pollute the error log.
                _log_errors = None
                if compile_ok is False:
                    _log_errors = compile_errors or None
                elif _f_errors:
                    _log_errors = _f_errors
                log_request(
                    ip=ip,
                    endpoint="/api/generate-mod-progress",
                    tier=_tier,
                    instruction=f"[{loader.upper()} {mc_version}] {instruction[:260]}",
                    success=bool(code.strip()),
                    attempts=_gen_attempts,
                    elapsed=elapsed,
                    compile_ok=compile_ok,
                    errors=_log_errors,
                    code=code,
                )
            except Exception:
                pass

            q.put(json.dumps(payload))

        except Exception as e:
            traceback.print_exc()
            q.put(json.dumps({"type": "error", "message": str(e)[:300]}))
        finally:
            q.put(None)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    def _event_stream():
        while True:
            try:
                item = q.get(timeout=25)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if item is None:
                break
            yield f"data: {item}\n\n"

    return Response(
        _event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Skript generation endpoint                                                   #
# --------------------------------------------------------------------------- #

@app.route("/api/generate-skript", methods=["POST"])
@limiter.limit(FREE_MONTHLY_LIMIT, key_func=get_remote_address,
               exempt_when=lambda: get_tier() == "pro" or _authenticated_user() is not None,
               error_message=json.dumps({
                   "error": "Free tier limit reached (3/month). Upgrade at stacknests.com/#pricing.",
                   "upgrade_url": "/#pricing",
               }))
def generate_skript():
    """
    Generate a Skript script (.sk file) and stream SSE progress.
    Same SSE format as /api/generate-progress (phase / done / error).
    """
    ip = get_remote_address()
    if is_banned(ip):
        return jsonify({"error": "Access denied."}), 403

    req_data = request.get_json(silent=True)
    if not req_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    instruction = req_data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "'instruction' field is required"}), 400
    if len(instruction) > 2000:
        return jsonify({"error": "Instruction too long (max 2000 chars)"}), 400

    _tier = get_tier()
    _user = _current_user()

    if _user and not _user.get("verified") and _tier == "free" and not is_bypassed(ip):
        return jsonify({
            "error": "Please verify your email address before generating scripts.",
            "unverified": True,
        }), 403

    if _user and not is_bypassed(ip):
        allowed, usage = check_user_generation_limit(int(_user["id"]))
        if not allowed:
            plan  = usage.get("plan", "free")
            limit = usage["gens_limit"]
            hint  = " Upgrade to Pro for 100/month." if plan == "free" else ""
            return jsonify({
                "error": f"Monthly generation limit reached ({limit}/month).{hint}",
                "upgrade_url": "/pricing",
                "usage": usage,
            }), 429

    q: queue.Queue = queue.Queue()

    def _worker():
        try:
            from inference.router import build_skript_prompt, _SKRIPT_SYSTEM

            q.put(json.dumps({
                "type": "phase", "percent": 5,
                "step": "Analysing your Skript requirements\u2026",
                "thinking": f"Instruction: {instruction[:120]}\u2026",
            }))

            # Optional web search for add-on info
            use_web = bool(req_data.get("web_search", False))
            final_instruction = instruction
            web_results = []
            if use_web:
                sq = f"Skript script {instruction[:60]}"
                q.put(json.dumps({
                    "type": "phase", "percent": 10,
                    "step": "Searching Skript documentation\u2026",
                    "thinking": f"Query: {sq}",
                }))
                web_results = _web_search_cached(sq, max_results=3)
                if web_results:
                    q.put(json.dumps({"type": "web_results", "results": web_results}))
                    snippets = "\n".join(
                        f"- {r['title']}: {r['snippet']}"
                        for r in web_results[:3] if r.get("snippet")
                    )
                    if snippets:
                        final_instruction = instruction + f"\n\n[Web context:]\n{snippets}"

            q.put(json.dumps({
                "type": "phase", "percent": 18,
                "step": "Building generation prompt\u2026",
                "thinking": "Injecting Skript 2.x rules and syntax guide.",
            }))

            prompt = build_skript_prompt(final_instruction)

            model_hint = "Claude (Pro)" if _tier == "pro" else "Gemini Flash"
            q.put(json.dumps({
                "type": "phase", "percent": 25,
                "step": "Generating Skript code\u2026",
                "thinking": f"Model: {model_hint}. Awaiting generation\u2026",
            }))

            import time as _time
            _start = _time.time()

            code = ""
            result_holder: list = [None]
            exc_holder: list    = [None]

            def _gen(_p=prompt, _instr=final_instruction):
                try:
                    from inference.server import generate_with_fallback, GenerationParams
                    out, _source = generate_with_fallback(
                        _p,
                        GenerationParams(max_tokens=4000),
                        system_prompt=_SKRIPT_SYSTEM,
                        instruction=_instr,
                        tier=_to_inference_tier(_tier),
                        force_cloud=True,
                    )
                    result_holder[0] = out
                except Exception as exc:
                    exc_holder[0] = exc

            import threading as _threading
            gen_thread = _threading.Thread(target=_gen, daemon=True)
            gen_thread.start()

            _filler = [
                (45, "Writing Skript commands and event handlers\u2026",
                 "Generating .sk file with correct indentation and syntax\u2026"),
                (70, "Finalising script structure\u2026",
                 "Adding options, variables, and permission checks\u2026"),
            ]
            _hb_msgs = [
                "AI is still writing your Skript\u2026",
                "Hang tight\u2014complex scripts take a moment\u2026",
                "Still generating\u2014almost there\u2026",
            ]
            _fi = 0; _hb_i = 0
            _sk_start = _time.time()
            while gen_thread.is_alive():
                gen_thread.join(timeout=3.5)
                if _fi < len(_filler):
                    pct, step, thinking = _filler[_fi]
                    q.put(json.dumps({"type": "phase", "percent": pct, "step": step, "thinking": thinking}))
                    _fi += 1
                else:
                    elapsed = round(_time.time() - _sk_start)
                    q.put(json.dumps({"type": "tick", "elapsed": elapsed,
                                      "step": _hb_msgs[_hb_i % len(_hb_msgs)]}))
                    _hb_i += 1

            if exc_holder[0]:
                e = exc_holder[0]
                err_str = str(e).lower()
                if "rate" in err_str or "quota" in err_str or "429" in err_str:
                    msg = "AI quota temporarily exhausted. Please try again in a few minutes."
                elif "timeout" in err_str or "connection" in err_str:
                    msg = "AI backend is unreachable. Please try again in 30 seconds."
                else:
                    msg = "Skript generation temporarily unavailable. Please try again."
                q.put(json.dumps({"type": "error", "message": msg, "detail": str(e)[:200]}))
                return

            code = result_holder[0] or ""
            elapsed = round(_time.time() - _start, 1)

            # ── Skript static validation + Kimi heal loop ─────────────────
            sk_issues: list[str] = []
            sk_attempts = 1
            if code.strip():
                q.put(json.dumps({
                    "type": "phase", "percent": 82,
                    "step": "Validating Skript syntax\u2026",
                    "thinking": "Running static checks on .sk file\u2026",
                }))
                try:
                    from validation.skript_check import validate_skript
                    val = validate_skript(code)
                    sk_issues = val["issues"]
                    if not val["ok"] and sk_issues:
                        errors_preview = "\n".join(sk_issues[:4])
                        q.put(json.dumps({
                            "type": "phase", "percent": 88,
                            "step": f"Fixing {len(sk_issues)} Skript issue(s) with AI\u2026",
                            "thinking": errors_preview,
                        }))
                        try:
                            from inference.kimi import kimi_heal_skript
                            healed = kimi_heal_skript(code, sk_issues)
                            if healed.strip():
                                code = healed
                                sk_attempts = 2
                                # Re-validate after heal
                                val2 = validate_skript(code)
                                sk_issues = val2["issues"]
                        except Exception as _heal_exc:
                            import logging as _log
                            _log.getLogger(__name__).warning(
                                "[Skript] Kimi heal failed: %s", _heal_exc)
                except Exception as _val_exc:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        "[Skript] Validation error: %s", _val_exc)
            elapsed = round(_time.time() - _start, 1)
            # ─────────────────────────────────────────────────────────────

            q.put(json.dumps({
                "type": "phase", "percent": 98,
                "step": "Finalising result\u2026",
                "thinking": f"elapsed={elapsed}s",
            }))

            payload = {
                "type":            "done",
                "success":         bool(code.strip()),
                "code":            code,
                "attempts":        sk_attempts,
                "elapsed_seconds": elapsed,
                "warnings":        [i for i in sk_issues if i.startswith("[WARNING]")],
                "errors":          [i for i in sk_issues if i.startswith("[ERROR]")],
                "compile_ok":      None,
                "web_search_used": len(web_results) > 0,
                "script_type":     "skript",
            }

            if _user and bool(req_data.get("save_project", True)):
                try:
                    increment_user_generation(int(_user["id"]), 1)
                except Exception:
                    pass

            try:
                log_request(
                    ip=ip,
                    endpoint="/api/generate-skript",
                    tier=_tier,
                    instruction=instruction[:260],
                    success=bool(code.strip()),
                    attempts=sk_attempts,
                    elapsed=elapsed,
                    compile_ok=None,
                    errors=sk_issues,
                    code=code,
                )
            except Exception:
                pass

            q.put(json.dumps(payload))

        except Exception as e:
            traceback.print_exc()
            q.put(json.dumps({"type": "error", "message": str(e)[:300]}))
        finally:
            q.put(None)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    def _event_stream():
        while True:
            try:
                item = q.get(timeout=25)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if item is None:
                break
            yield f"data: {item}\n\n"

    return Response(
        _event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Datapack generation endpoint                                                 #
# --------------------------------------------------------------------------- #

@app.route("/api/generate-datapack", methods=["POST"])
@limiter.limit(FREE_MONTHLY_LIMIT, key_func=get_remote_address,
               exempt_when=lambda: get_tier() == "pro" or _authenticated_user() is not None,
               error_message=json.dumps({
                   "error": "Free tier limit reached (3/month). Upgrade at stacknests.com/#pricing.",
                   "upgrade_url": "/#pricing",
               }))
def generate_datapack():
    """
    Generate a Minecraft datapack and stream SSE progress.
    Same SSE format as /api/generate-progress (phase / done / error).
    """
    ip = get_remote_address()
    if is_banned(ip):
        return jsonify({"error": "Access denied."}), 403

    req_data = request.get_json(silent=True)
    if not req_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    instruction = req_data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "'instruction' field is required"}), 400
    if len(instruction) > 2000:
        return jsonify({"error": "Instruction too long (max 2000 chars)"}), 400

    mc_version = req_data.get("mc_version", "1.21").strip()
    _tier = get_tier()
    _user = _current_user()

    if _user and not _user.get("verified") and _tier == "free" and not is_bypassed(ip):
        return jsonify({
            "error": "Please verify your email address before generating datapacks.",
            "unverified": True,
        }), 403

    if _user and not is_bypassed(ip):
        allowed, usage = check_user_generation_limit(int(_user["id"]))
        if not allowed:
            plan  = usage.get("plan", "free")
            limit = usage["gens_limit"]
            hint  = " Upgrade to Pro for 100/month." if plan == "free" else ""
            return jsonify({
                "error": f"Monthly generation limit reached ({limit}/month).{hint}",
                "upgrade_url": "/pricing",
                "usage": usage,
            }), 429

    q: queue.Queue = queue.Queue()

    def _worker():
        try:
            from inference.router import build_datapack_prompt, _DATAPACK_SYSTEM

            q.put(json.dumps({
                "type": "phase", "percent": 5,
                "step": "Analysing your datapack requirements\u2026",
                "thinking": f"MC version: {mc_version}  Instruction: {instruction[:120]}\u2026",
            }))

            use_web = bool(req_data.get("web_search", True))
            final_instruction = instruction
            web_results = []
            if use_web:
                sq = f"Minecraft datapack {mc_version} {instruction[:60]}"
                q.put(json.dumps({
                    "type": "phase", "percent": 10,
                    "step": "Searching datapack documentation\u2026",
                    "thinking": f"Query: {sq}",
                }))
                web_results = _web_search_cached(sq, max_results=3)
                if web_results:
                    q.put(json.dumps({"type": "web_results", "results": web_results}))
                    snippets = "\n".join(
                        f"- {r['title']}: {r['snippet']}"
                        for r in web_results[:3] if r.get("snippet")
                    )
                    if snippets:
                        final_instruction = instruction + f"\n\n[Web context:]\n{snippets}"

            q.put(json.dumps({
                "type": "phase", "percent": 18,
                "step": "Building generation prompt\u2026",
                "thinking": f"Target MC {mc_version} — injecting datapack structure rules.",
            }))

            doc_ctx = get_datapack_doc_context(final_instruction)
            prompt = build_datapack_prompt(final_instruction, mc_version=mc_version, doc_context=doc_ctx)

            model_hint = "Claude (Pro)" if _tier == "pro" else "Gemini Flash"
            q.put(json.dumps({
                "type": "phase", "percent": 25,
                "step": "Generating datapack files\u2026",
                "thinking": f"Model: {model_hint}. Awaiting generation\u2026",
            }))

            import time as _time
            _start = _time.time()

            code = ""
            result_holder: list = [None]
            exc_holder: list    = [None]

            def _gen(_p=prompt, _instr=final_instruction):
                try:
                    from inference.server import generate_with_fallback, GenerationParams
                    out, _source = generate_with_fallback(
                        _p,
                        GenerationParams(max_tokens=5000),
                        system_prompt=_DATAPACK_SYSTEM,
                        instruction=_instr,
                        tier=_to_inference_tier(_tier),
                        force_cloud=True,
                    )
                    result_holder[0] = out
                except Exception as exc:
                    exc_holder[0] = exc

            import threading as _threading
            gen_thread = _threading.Thread(target=_gen, daemon=True)
            gen_thread.start()

            _filler = [
                (40, "Writing pack.mcmeta and function files\u2026",
                 "Generating datapack directory structure and .mcfunction files\u2026"),
                (65, "Generating JSON data files\u2026",
                 "Building advancements, recipes, loot tables, and tags\u2026"),
            ]
            _hb_msgs = [
                "AI is still building your datapack\u2026",
                "Hang tight\u2014complex datapacks take a moment\u2026",
                "Still generating\u2014almost there\u2026",
            ]
            _fi = 0; _hb_i = 0
            _dp_start = _time.time()
            while gen_thread.is_alive():
                gen_thread.join(timeout=3.5)
                if _fi < len(_filler):
                    pct, step, thinking = _filler[_fi]
                    q.put(json.dumps({"type": "phase", "percent": pct, "step": step, "thinking": thinking}))
                    _fi += 1
                else:
                    elapsed = round(_time.time() - _dp_start)
                    q.put(json.dumps({"type": "tick", "elapsed": elapsed,
                                      "step": _hb_msgs[_hb_i % len(_hb_msgs)]}))
                    _hb_i += 1

            if exc_holder[0]:
                e = exc_holder[0]
                err_str = str(e).lower()
                if "rate" in err_str or "quota" in err_str or "429" in err_str:
                    msg = "AI quota temporarily exhausted. Please try again in a few minutes."
                elif "timeout" in err_str or "connection" in err_str:
                    msg = "AI backend is unreachable. Please try again in 30 seconds."
                else:
                    msg = "Datapack generation temporarily unavailable. Please try again."
                q.put(json.dumps({"type": "error", "message": msg, "detail": str(e)[:200]}))
                return

            code = result_holder[0] or ""
            elapsed = round(_time.time() - _start, 1)

            q.put(json.dumps({
                "type": "phase", "percent": 98,
                "step": "Finalising result\u2026",
                "thinking": f"elapsed={elapsed}s  mc={mc_version}",
            }))

            payload = {
                "type":            "done",
                "success":         bool(code.strip()),
                "code":            code,
                "attempts":        1,
                "elapsed_seconds": elapsed,
                "warnings":        [],
                "errors":          [],
                "compile_ok":      None,
                "web_search_used": len(web_results) > 0,
                "script_type":     "datapack",
                "mc_version":      mc_version,
            }

            if _user and bool(req_data.get("save_project", True)):
                try:
                    increment_user_generation(int(_user["id"]), 1)
                except Exception:
                    pass

            try:
                log_request(
                    ip=ip,
                    endpoint="/api/generate-datapack",
                    tier=_tier,
                    instruction=f"[datapack {mc_version}] {instruction[:260]}",
                    success=bool(code.strip()),
                    attempts=1,
                    elapsed=elapsed,
                    compile_ok=None,
                    code=code,
                )
            except Exception:
                pass

            q.put(json.dumps(payload))

        except Exception as e:
            traceback.print_exc()
            q.put(json.dumps({"type": "error", "message": str(e)[:300]}))
        finally:
            q.put(None)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    def _event_stream():
        while True:
            try:
                item = q.get(timeout=25)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if item is None:
                break
            yield f"data: {item}\n\n"

    return Response(
        _event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Datapack verify + ZIP endpoint                                               #
# --------------------------------------------------------------------------- #

@app.route("/api/build-datapack-zip", methods=["POST"])
def build_datapack_zip():
    """
    POST {code: str}
    Parses, verifies, and packages a generated datapack into a ZIP.
    Returns JSON: {ok, issues, files:[{path, lang}], zip_b64}.
    Always returns 200 — issues are surfaced in the payload, not via HTTP errors.
    """
    import base64
    import io
    import zipfile as _zf
    from validation.datapack_check import parse_datapack_files, verify_datapack

    req_data = request.get_json(silent=True) or {}
    code = (req_data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "No code provided"}), 400

    files = parse_datapack_files(code)
    if not files:
        return jsonify({
            "ok":      False,
            "issues":  [
                "Could not extract any files from the generated output. "
                "Each code block must start with a comment line giving the file path "
                "(e.g. // pack.mcmeta or # data/mynamespace/functions/tick.mcfunction)."
            ],
            "files":   [],
            "zip_b64": "",
        })

    result = verify_datapack(files)

    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f.path, f.content)
    buf.seek(0)
    zip_b64 = base64.b64encode(buf.read()).decode()

    return jsonify({
        "ok":      result["ok"],
        "issues":  result["issues"],
        "files":   [{"path": f.path, "lang": f.lang} for f in files],
        "zip_b64": zip_b64,
    })


@app.route("/api/user/usage", methods=["GET"])
@_user_required
def user_usage():
    """Return the current user's generation usage and plan limits."""
    user = request.stacknest_user
    usage = get_user_usage(int(user["id"]))
    return jsonify({"usage": usage, "plan": user.get("plan", "free")})


# ---------------------------------------------------------------------------
# Discord Bot Hosting API
# ---------------------------------------------------------------------------

@app.route("/api/bots/generate", methods=["POST"])
@_user_required
@limiter.limit("20 per hour", key_func=get_remote_address)
def bots_generate():
    user = request.stacknest_user
    access = _bot_hosting_access(user)
    if not access["allowed"]:
        return jsonify({
            "error": "Bot hosting is not enabled for this account.",
            "upgrade_url": "/pricing",
        }), 403

    data = request.get_json(silent=True) or {}
    description = str(data.get("description", "")).strip()
    if not description:
        return jsonify({"error": "'description' is required"}), 400
    if len(description) > 1500:
        return jsonify({"error": "Description too long (max 1500 chars)."}), 400

    # Lightweight deterministic generator for hosted bot testing.
    code = (
        "import os\n"
        "import discord\n"
        "from discord.ext import commands\n\n"
        "TOKEN = os.getenv('DISCORD_TOKEN', '').strip()\n"
        "if not TOKEN:\n"
        "    raise RuntimeError('DISCORD_TOKEN is missing')\n\n"
        "intents = discord.Intents.default()\n"
        "intents.message_content = True\n"
        "bot = commands.Bot(command_prefix='!', intents=intents)\n\n"
        f"BOT_DESCRIPTION = {json.dumps(description)}\n\n"
        "@bot.event\n"
        "async def on_ready():\n"
        "    print(f'Logged in as {bot.user} (id={bot.user.id})')\n"
        "    print('Description:', BOT_DESCRIPTION)\n\n"
        "@bot.command(name='ping')\n"
        "async def ping(ctx):\n"
        "    await ctx.reply('Pong!')\n\n"
        "@bot.command(name='about')\n"
        "async def about(ctx):\n"
        "    await ctx.reply(BOT_DESCRIPTION[:1800])\n\n"
        "bot.run(TOKEN)\n"
    )

    return jsonify({"ok": True, "language": "python", "code": code})


@app.route("/api/bots", methods=["GET"])
@_user_required
def bots_list():
    user = request.stacknest_user
    _ensure_hosted_bots_table()
    with _conn() as con:
        rows = con.execute(
            "SELECT id, bot_name, status, language, created_at, updated_at, ram_gb, pid, last_ping, last_error FROM hosted_bots WHERE user_id=? ORDER BY created_at DESC",
            (int(user["id"]),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["live_status"] = _hosted_bot_live_status(d)
        if d["live_status"] != "running" and d.get("status") == "running":
            _update_bot_state(d["id"], status="stopped", pid=None, updated_at=time.time())
            d["status"] = "stopped"
        out.append(d)
    return jsonify({"bots": out})


@app.route("/api/bots/upload", methods=["POST"])
@_user_required
@limiter.limit("30 per hour", key_func=get_remote_address)
def bots_upload():
    user = request.stacknest_user
    access = _bot_hosting_access(user)
    if not access["allowed"]:
        return jsonify({"error": "Bot hosting is not enabled for this account.", "upgrade_url": "/pricing"}), 403

    up = request.files.get("bot_file")
    if not up or not getattr(up, "filename", ""):
        return jsonify({"error": "Missing file field 'bot_file'."}), 400

    safe_name = secure_filename(up.filename)[:120]
    if not safe_name:
        return jsonify({"error": "Invalid filename."}), 400

    ext = Path(safe_name).suffix.lower()
    if ext not in {".py", ".zip"}:
        return jsonify({"error": "Only .py or .zip files are supported."}), 400

    raw = up.read(_HOSTED_BOT_UPLOAD_MAX_BYTES + 1)
    if len(raw) > _HOSTED_BOT_UPLOAD_MAX_BYTES:
        return jsonify({"error": f"File too large (max {_HOSTED_BOT_UPLOAD_MAX_BYTES // 1000}KB)."}), 400
    if not raw:
        return jsonify({"error": "Uploaded file is empty."}), 400

    try:
        if ext == ".py":
            try:
                code = raw.decode("utf-8")
            except UnicodeDecodeError:
                return jsonify({"error": "Python files must be UTF-8 encoded."}), 400
            source_name = safe_name
            _scan_hosted_file_security(source_name, code)
            entrypoint_path = source_name
            project_files = [{"path": source_name, "content": code}]
        else:
            project = _extract_bot_project_from_zip(raw)
            source_name = project["entry_path"]
            code = project["entry_code"]
            entrypoint_path = project["entry_path"]
            project_files = project["project_files"]
            _scan_hosted_project_files_security(project_files)
    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid ZIP file."}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    code = code.strip()
    if not code:
        return jsonify({"error": "Uploaded code is empty."}), 400
    if len(code) > _HOSTED_BOT_MAX_CODE_CHARS:
        return jsonify({"error": f"Code is too large (max {_HOSTED_BOT_MAX_CODE_CHARS} chars)."}), 400

    suggestion = re.sub(r"[^\w\- ]", "", Path(source_name).stem).strip()[:60] or "Uploaded Bot"
    entry_code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    upload_id = _stage_hosted_upload(int(user["id"]), {
        "filename": source_name,
        "entrypoint_path": entrypoint_path,
        "project_files": project_files,
        "entry_code_hash": entry_code_hash,
    }, zip_bytes=raw if ext == ".zip" else None)
    return jsonify({
        "ok": True,
        "upload_id": upload_id,
        "filename": source_name,
        "bot_name_suggestion": suggestion,
        "entrypoint_path": entrypoint_path,
        "project_files": project_files,
        "entry_code_hash": entry_code_hash,
        "code": code,
    })


@app.route("/api/bots/deploy", methods=["POST"])
@_user_required
@limiter.limit("20 per hour", key_func=get_remote_address)
def bots_deploy():
    user = request.stacknest_user
    access = _bot_hosting_access(user)
    if not access["allowed"]:
        return jsonify({"error": "Bot hosting is not enabled for this account.", "upgrade_url": "/pricing"}), 403

    data = request.get_json(silent=True) or {}
    bot_name = str(data.get("bot_name", "")).strip()
    token = str(data.get("token", "")).strip()
    code = str(data.get("code", ""))
    project_files = data.get("project_files")
    entrypoint_path = str(data.get("entrypoint_path", "") or "").strip()
    upload_id = str(data.get("upload_id", "") or "").strip()
    raw_ports = data.get("ports", [])
    user_id = int(user["id"])

    if not bot_name:
        return jsonify({"error": "'bot_name' is required"}), 400
    if not token:
        return jsonify({"error": "'token' is required"}), 400
    has_project_files = isinstance(project_files, list) and len(project_files) > 0
    if not has_project_files:
        staged = _load_staged_hosted_upload(user_id, upload_id) or _load_matching_recent_staged_upload(user_id, code)
        if staged:
            upload_id = upload_id or str(staged.get("upload_id", "") or "")
            project_files = staged.get("project_files")
            entrypoint_path = entrypoint_path or str(staged.get("entrypoint_path", "") or "")
            has_project_files = isinstance(project_files, list) and len(project_files) > 0

    if not has_project_files:
        if not code.strip():
            return jsonify({"error": "'code' is required"}), 400
        if len(code) > _HOSTED_BOT_MAX_CODE_CHARS:
            return jsonify({"error": f"Code is too large (max {_HOSTED_BOT_MAX_CODE_CHARS} chars)."}), 400
        _scan_hosted_file_security("bot.py", code)

    try:
        ports = _sanitize_bot_ports(raw_ports)
        _ensure_ports_within_quota(user_id, ports)
    except ValueError as e:
        return jsonify({
            "error": str(e),
            "code": "bot_ports_upgrade_required",
            "upgrade_url": "/pricing",
            "port_quota": _user_port_quota(user_id),
        }), 403

    _ensure_hosted_bots_table()
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (user_id,)).fetchone()
        current_count = int(row[0] if row else 0)
    if current_count >= int(access["limit"]):
        return jsonify({"error": f"Bot limit reached ({access['limit']}). Delete one before deploying another."}), 409

    bot_id = uuid4().hex[:12]
    safe_name = re.sub(r"[^\w\- ]", "", bot_name).strip()[:60] or "HostedBot"
    bot_dir = _HOSTED_BOTS_ROOT / str(user_id) / bot_id
    bot_dir.mkdir(parents=True, exist_ok=True)
    token_path = bot_dir / "token.txt"
    log_path = bot_dir / "bot.log"

    if has_project_files:
        if len(project_files) > _HOSTED_BOT_PROJECT_MAX_FILES:
            return jsonify({"error": "Project has too many files."}), 400

        safe_written_paths = set()
        staged = _load_staged_hosted_upload(user_id, upload_id) if upload_id else None
        staged_zip = _hosted_upload_stage_zip_path(user_id, upload_id) if upload_id else None
        if staged and staged_zip and staged_zip.exists():
            try:
                _extract_safe_zip_to_dir(staged_zip.read_bytes(), bot_dir)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            for path in bot_dir.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(bot_dir).as_posix()
                    ext = path.suffix.lower()
                    if ext in _HOSTED_BOT_TEXT_EXTS:
                        try:
                            text = path.read_text(encoding="utf-8", errors="ignore")
                            _scan_hosted_file_security(rel, text)
                        except ValueError as e:
                            return jsonify({"error": str(e)}), 400
                    safe_written_paths.add(rel)
        else:
            for item in project_files:
                if not isinstance(item, dict):
                    return jsonify({"error": "Invalid project file payload."}), 400
                rel = str(item.get("path", "") or "").replace("\\", "/").strip()
                content = item.get("content", "")
                if not rel or rel.startswith("/"):
                    return jsonify({"error": "Invalid project file path."}), 400
                parts = [p for p in rel.split("/") if p and p != "."]
                if not parts or any(p == ".." for p in parts):
                    return jsonify({"error": "Invalid project file path."}), 400
                if not isinstance(content, str):
                    return jsonify({"error": "Project file content must be text."}), 400

                rel = "/".join(parts)
                ext = Path(rel).suffix.lower()
                if ext not in _HOSTED_BOT_TEXT_EXTS:
                    continue

                try:
                    _scan_hosted_file_security(rel, str(content))
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400

                target = bot_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                safe_written_paths.add(rel)

        if not safe_written_paths:
            return jsonify({"error": "No valid project files to deploy."}), 400

        entry_rel = (entrypoint_path or "").replace("\\", "/").strip()
        if not entry_rel:
            py_candidates = sorted([p for p in safe_written_paths if p.lower().endswith(".py")])
            entry_rel = py_candidates[0] if py_candidates else ""
        if not entry_rel or entry_rel not in safe_written_paths:
            return jsonify({"error": "Entrypoint file is missing from project files."}), 400
        if not entry_rel.lower().endswith((".py", ".js", ".mjs", ".cjs")):
            return jsonify({"error": "Entrypoint must be a Python or Node.js file."}), 400

        code_path = bot_dir / entry_rel
    else:
        code_path = bot_dir / "bot.py"
        code_path.write_text(code, encoding="utf-8")

    runtime = _detect_bot_runtime({"code_path": str(code_path)})
    if runtime not in {"python", "node"}:
        return jsonify({"error": "Only Python and Node.js bots are supported."}), 400

    token_path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except Exception:
        pass

    now = time.time()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO hosted_bots
                (id, user_id, bot_name, status, language, created_at, updated_at, ram_gb, pid, last_ping, last_error, code_path, token_path, log_path)
            VALUES
                (?,  ?,      ?,        'stopped', ?,       ?,         ?,         ?,     NULL, NULL,      '',         ?,         ?,         ?)
            """,
            (
                bot_id,
                user_id,
                safe_name,
                runtime,
                now,
                now,
                _HOSTED_BOT_DEFAULT_RAM_GB,
                str(code_path),
                str(token_path),
                str(log_path),
            ),
        )

    row = _fetch_bot_for_user(user_id, bot_id)
    try:
        _set_bot_ports(bot_id, ports)
    except Exception:
        pass
    ok, msg = _start_hosted_bot(row)
    if not ok:
        _update_bot_state(bot_id, status="error", last_error=msg, updated_at=time.time())
        return jsonify({"error": f"Deploy failed: {msg}"}), 500

    return jsonify({"ok": True, "id": bot_id, "status": "running", "message": "Bot deployed"})


@app.route("/api/bots/<string:bot_id>/start", methods=["POST"])
@_user_required
def bots_start(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404
    if _hosted_bot_live_status(row) == "running":
        return jsonify({"ok": True, "message": "Bot already running"})
    ok, msg = _start_hosted_bot(row)
    if not ok:
        _update_bot_state(bot_id, status="error", last_error=msg, updated_at=time.time())
        return jsonify({"error": msg}), 500
    return jsonify({"ok": True, "message": msg})


@app.route("/api/bots/<string:bot_id>/stop", methods=["POST"])
@_user_required
def bots_stop(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404
    ok, msg = _stop_hosted_bot(row)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/bots/<string:bot_id>/restart", methods=["POST"])
@_user_required
def bots_restart(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404
    _stop_hosted_bot(row)
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    ok, msg = _start_hosted_bot(row)
    if not ok:
        _update_bot_state(bot_id, status="error", last_error=msg, updated_at=time.time())
        return jsonify({"error": msg}), 500
    return jsonify({"ok": True, "message": "Bot restarted"})


@app.route("/api/bots/<string:bot_id>", methods=["DELETE"])
@_user_required
def bots_delete(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404
    _stop_hosted_bot(row)
    with _conn() as con:
        con.execute("DELETE FROM hosted_bots WHERE id=? AND user_id=?", (bot_id, int(user["id"])))
    try:
        set_meta(_bot_ports_meta_key(bot_id), "")
    except Exception:
        pass
    try:
        shutil.rmtree(Path(row["code_path"]).parent, ignore_errors=True)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/bots/<string:bot_id>/logs", methods=["GET"])
@_user_required
def bots_logs(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404
    lines = min(max(int(request.args.get("lines", 150)), 10), 500)
    logs = _tail_lines(Path(row["log_path"]), lines=lines)
    return jsonify({"logs": logs})


@app.route("/api/bots/<string:bot_id>/panel", methods=["GET"])
@_user_required
def bots_panel(bot_id: str):
    user = request.stacknest_user
    user_id = int(user["id"])
    row = _fetch_bot_for_user(user_id, bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404

    files = _list_bot_project_files(row)
    live = _hosted_bot_live_status(row)
    if live != "running" and row.get("status") == "running":
        _update_bot_state(bot_id, status="stopped", pid=None, updated_at=time.time())

    try:
        entrypoint = Path(row.get("code_path", "bot.py")).relative_to(_bot_project_root(row)).as_posix()
    except Exception:
        entrypoint = Path(row.get("code_path", "bot.py")).name or "bot.py"

    metrics = _hosted_bot_resource_snapshot(row)
    _append_bot_metrics_sample(row, metrics)
    metrics_history = _bot_metrics_history(row)
    package_info = _bot_package_info(row)
    file_tree = _build_bot_file_tree(files)
    ports = _get_bot_ports(bot_id)
    port_quota = _user_port_quota(user_id)

    return jsonify({
        "bot": {
            "id": row["id"],
            "name": row.get("bot_name", "Bot"),
            "status": row.get("status", "stopped"),
            "live_status": live,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "ram_gb": row.get("ram_gb", 1),
            "pid": row.get("pid"),
            "entrypoint": entrypoint,
            "start_flags": _read_bot_start_flags(row),
            "last_error": row.get("last_error") or "",
            "runtime": _detect_bot_runtime(row),
            "ports": ports,
            "port_quota": port_quota,
        },
        "files": files,
        "file_tree": file_tree,
        "metrics": metrics,
        "metrics_history": metrics_history,
        "packages": package_info,
        "security": {
            "port_quota": port_quota,
            "base_port_allowance": _HOSTED_BOT_BASE_PORT_ALLOWANCE,
            "extra_ports": _get_user_extra_ports(user_id),
        },
    })


@app.route("/api/bots/<string:bot_id>/metrics", methods=["GET"])
@_user_required
def bots_metrics(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404
    metrics = _hosted_bot_resource_snapshot(row)
    _append_bot_metrics_sample(row, metrics)
    return jsonify({
        "bot_id": bot_id,
        "live_status": _hosted_bot_live_status(row),
        "metrics": metrics,
        "metrics_history": _bot_metrics_history(row),
    })


@app.route("/api/bots/<string:bot_id>/packages/install", methods=["POST"])
@_user_required
def bots_install_packages(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404

    runtime = _detect_bot_runtime(row)
    if runtime not in {"python", "node"}:
        return jsonify({"error": "Package installs are supported for Python and Node.js bots."}), 400

    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "custom") or "custom").strip().lower()
    package_specs: list[str] = []
    if mode in {"requirements", "manifest"}:
        if runtime == "python":
            package_specs = _read_requirements_lines(_bot_requirements_path(row), limit=_HOSTED_BOT_MAX_PACKAGE_SPECS)
            if not package_specs:
                return jsonify({"error": "No requirements.txt packages found."}), 400
        else:
            package_json = _bot_package_json_path(row)
            if not package_json.exists():
                return jsonify({"error": "package.json not found in bot project root."}), 400
            package_specs = []
    else:
        raw_packages = data.get("packages", [])
        if isinstance(raw_packages, str):
            raw_packages = [line.strip() for line in raw_packages.splitlines() if line.strip()]
        if not isinstance(raw_packages, list):
            return jsonify({"error": "packages must be a list of package specs."}), 400
        package_specs = [str(item or "").strip() for item in raw_packages if str(item or "").strip()]
        if not package_specs:
            return jsonify({"error": "Provide at least one package spec."}), 400

    try:
        if runtime == "python":
            ok, message = _install_bot_python_packages(row, package_specs)
        else:
            ok, message = _install_bot_node_packages(row, package_specs, from_manifest=(mode in {"requirements", "manifest"}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    package_info = _bot_package_info(row)
    if not ok:
        return jsonify({
            "error": message,
            "packages": package_info,
        }), 500
    _update_bot_state(bot_id, updated_at=time.time())
    return jsonify({
        "ok": True,
        "message": message,
        "packages": package_info,
    })


@app.route("/api/bots/<string:bot_id>/start-flags", methods=["POST"])
@_user_required
def bots_set_start_flags(bot_id: str):
    user = request.stacknest_user
    user_id = int(user["id"])
    row = _fetch_bot_for_user(user_id, bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404

    data = request.get_json(silent=True) or {}
    flags = str(data.get("flags", "")).strip()
    if len(flags) > 300:
        return jsonify({"error": "Start flags too long (max 300 chars)."}), 400

    requested_ports = _ports_from_start_flags(flags)
    fixed_ports = _get_bot_ports(bot_id)
    merged_ports = sorted(set(fixed_ports) | set(requested_ports))
    try:
        _ensure_ports_within_quota(user_id, merged_ports)
    except ValueError as e:
        return jsonify({
            "error": str(e),
            "code": "bot_ports_upgrade_required",
            "upgrade_url": "/pricing",
            "port_quota": _user_port_quota(user_id),
        }), 403

    _write_bot_start_flags(row, flags)
    return jsonify({"ok": True, "flags": flags})


@app.route("/api/bots/<string:bot_id>/ports", methods=["POST"])
@_user_required
def bots_set_ports(bot_id: str):
    user = request.stacknest_user
    user_id = int(user["id"])
    row = _fetch_bot_for_user(user_id, bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404

    data = request.get_json(silent=True) or {}
    try:
        ports = _sanitize_bot_ports(data.get("ports", []))
        _ensure_ports_within_quota(user_id, ports)
    except ValueError as e:
        return jsonify({
            "error": str(e),
            "code": "bot_ports_upgrade_required",
            "upgrade_url": "/pricing",
            "port_quota": _user_port_quota(user_id),
        }), 403

    _set_bot_ports(bot_id, ports)
    return jsonify({
        "ok": True,
        "ports": ports,
        "port_quota": _user_port_quota(user_id),
    })


@app.route("/api/bots/<string:bot_id>/file", methods=["GET"])
@_user_required
def bots_get_file(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404

    path_q = request.args.get("path", "")
    try:
        target = _safe_bot_rel_path(_bot_project_root(row), path_q)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not target.exists() or not target.is_file():
        return jsonify({"error": "File not found"}), 404
    ext = target.suffix.lower()
    if ext and ext not in _HOSTED_BOT_TEXT_EXTS:
        return jsonify({"error": "Only text files are editable in panel."}), 400
    if target.stat().st_size > _HOSTED_BOT_MAX_FILE_READ_BYTES:
        return jsonify({"error": "File is too large to view in browser."}), 400

    content = target.read_text(encoding="utf-8", errors="ignore")
    return jsonify({"path": target.relative_to(_bot_project_root(row)).as_posix(), "content": content})


@app.route("/api/bots/<string:bot_id>/file", methods=["POST"])
@_user_required
def bots_write_file(bot_id: str):
    user = request.stacknest_user
    row = _fetch_bot_for_user(int(user["id"]), bot_id)
    if not row:
        return jsonify({"error": "Bot not found"}), 404

    data = request.get_json(silent=True) or {}
    path_q = str(data.get("path", ""))
    content = str(data.get("content", ""))
    if len(content.encode("utf-8")) > _HOSTED_BOT_MAX_FILE_WRITE_BYTES:
        return jsonify({"error": "File content too large to save."}), 400

    try:
        target = _safe_bot_rel_path(_bot_project_root(row), path_q)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    ext = target.suffix.lower()
    if ext and ext not in _HOSTED_BOT_TEXT_EXTS:
        return jsonify({"error": "Only text files are editable in panel."}), 400

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _update_bot_state(bot_id, updated_at=time.time())
    return jsonify({"ok": True, "path": target.relative_to(_bot_project_root(row)).as_posix()})


@app.route("/admin/api/bot-hosting/health", methods=["GET"])
@_admin_required
def admin_bot_hosting_health():
    _ensure_hosted_bots_table()
    with _conn() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM hosted_bots ORDER BY created_at DESC").fetchall()]

    running = 0
    stopped = 0
    zombie = 0
    missing_files = 0
    for r in rows:
        live = _hosted_bot_live_status(r)
        if live == "running":
            running += 1
        else:
            stopped += 1
            if r.get("status") == "running":
                zombie += 1
        if not Path(r.get("code_path", "")).exists() or not Path(r.get("token_path", "")).exists():
            missing_files += 1

    return jsonify({
        "total": len(rows),
        "running": running,
        "stopped": stopped,
        "zombie": zombie,
        "missing_files": missing_files,
        "worker_python": sys.executable,
        "hosted_root": str(_HOSTED_BOTS_ROOT),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/admin/api/bot-hosting/users", methods=["GET"])
@_admin_required
def admin_bot_hosting_users():
    _ensure_hosted_bots_table()
    limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    search = request.args.get("search", "").strip()
    users = list_users(limit=limit, offset=offset, search=search)

    user_ids = [int(u["id"]) for u in users]
    counts: dict[int, int] = {}
    if user_ids:
        placeholders = ",".join(["?"] * len(user_ids))
        with _conn() as con:
            for r in con.execute(
                f"SELECT user_id, COUNT(*) AS cnt FROM hosted_bots WHERE user_id IN ({placeholders}) GROUP BY user_id",
                user_ids,
            ).fetchall():
                counts[int(r["user_id"])] = int(r["cnt"])

    out = []
    for u in users:
        access = _bot_hosting_access(u)
        extra_ports = _get_user_extra_ports(int(u["id"]))
        out.append({
            "id": u["id"],
            "email": u.get("email", ""),
            "plan": u.get("plan", "free"),
            "hosting_allowed": bool(access["allowed"]),
            "hosting_limit": int(access["limit"]),
            "hosting_source": access["source"],
            "bot_port_base": _HOSTED_BOT_BASE_PORT_ALLOWANCE,
            "bot_extra_ports": extra_ports,
            "bot_port_quota": _HOSTED_BOT_BASE_PORT_ALLOWANCE + extra_ports,
            "hosted_count": counts.get(int(u["id"]), 0),
            "created_at": u.get("created_at"),
        })

    total = count_users(search=search)
    return jsonify({"users": out, "total": total, "limit": limit, "offset": offset})


@app.route("/api/stream", methods=["POST"])
@limiter.limit(FREE_STREAM_MONTHLY_LIMIT, key_func=get_remote_address,
               exempt_when=lambda: get_tier() == "pro",
               error_message=json.dumps({
                   "error": "Free tier prompt limit reached (20/month). Upgrade at stacknests.com/pricing.",
                   "upgrade_url": "/#pricing"
               }))
def stream_generate():
    """
    Stream plugin generation tokens via Server-Sent Events (SSE).
    No validation loop — client receives raw tokens and should call /api/validate after.

    Request body: same as /api/generate
    Response: text/event-stream
    """
    data = request.get_json(silent=True) or {}
    instruction = data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "'instruction' is required"}), 400

    stream_tier = _to_inference_tier(get_tier())
    gen = PluginGenerator(router=_router, params=GenerationParams(max_tokens=3000), tier=stream_tier)

    def event_stream():
        try:
            for token in gen.generate_stream(instruction):
                # SSE format: data: <token>\n\n
                escaped = token.replace("\n", "\\n")
                yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            yield f"data: {json.dumps({'done': True})}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/api/validate", methods=["POST"])
def validate():
    """
    Run validation on provided code without generating anything.
    Useful for validating user-modified code.

    Request body:
    {
        "code": "```java\n...\n```\n```yaml\n...\n```",
        "skip_compile": false
    }
    """
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"error": "'code' field is required"}), 400

    gate = _editor_limit_response("validating", code)
    if gate is not None:
        return gate

    skip_compile = bool(data.get("skip_compile", False))

    try:
        result = run_validation_only(code, skip_compile=skip_compile)
    except Exception as e:
        return jsonify({"error": f"Validation error: {e}"}), 500

    return jsonify({
        "valid": result.success,
        "errors": result.final_errors,
        "warnings": result.static_warnings,
        "compile_ok": result.compile_result.success if result.compile_result else None,
        "yml_ok": result.yml_result.valid if result.yml_result else None,
    })


@app.route("/editor")
def editor_page():
    return send_from_directory(app.static_folder, "editor.html")


@app.route("/setup")
def setup_page():
    return send_from_directory(app.static_folder, "setup.html")


@app.route("/logs")
def logs_page():
    return send_from_directory(app.static_folder, "logs.html")


@app.route("/admin/api/users")
@_admin_required
def admin_list_users():
    """Paginated list of all registered user accounts."""
    limit  = min(int(request.args.get("limit",  50)), 200)
    offset = int(request.args.get("offset", 0))
    search = request.args.get("search", "").strip()
    users  = list_users(limit=limit, offset=offset, search=search)
    total  = count_users(search=search)
    # Redact password hashes
    for u in users:
        u.pop("password_hash", None)
        u.pop("verification_token", None)
        _bha = _bot_hosting_access(u)
        extra_ports = _get_user_extra_ports(int(u["id"]))
        u["bot_hosting_allowed"] = bool(_bha["allowed"])
        u["bot_hosting_limit"] = int(_bha["limit"])
        u["bot_hosting_source"] = _bha["source"]
        u["bot_hosting_override_enabled"] = bool(_bha["override"].get("enabled"))
        u["bot_hosting_override_limit"] = int(_bha["override"].get("limit", 0))
        u["bot_port_base"] = _HOSTED_BOT_BASE_PORT_ALLOWANCE
        u["bot_extra_ports"] = extra_ports
        u["bot_port_quota"] = _HOSTED_BOT_BASE_PORT_ALLOWANCE + extra_ports
    return jsonify({"users": users, "total": total, "limit": limit, "offset": offset})


@app.route("/admin/api/users/<int:uid>", methods=["PATCH"])
@_admin_required
def admin_update_user(uid: int):
    """Manually upgrade/downgrade plan and bot-hosting test access."""
    data = request.get_json(silent=True) or {}
    user = get_user_by_id(uid)
    if not user:
        return jsonify({"error": "User not found"}), 404

    plan = str(data.get("plan", "")).strip().lower()
    if plan:
        if plan not in {"free", "starter", "pro", "studio"}:
            return jsonify({"error": "plan must be 'free', 'starter', 'pro', or 'studio'"}), 400
        set_user_plan(uid, plan)

    if "bot_hosting_test_access" in data:
        enabled = bool(data.get("bot_hosting_test_access", False))
        try:
            limit = int(data.get("bot_hosting_limit", 1))
        except Exception:
            limit = 1
        _set_bot_hosting_override(uid, enabled=enabled, limit=limit)

    if "bot_extra_ports" in data:
        try:
            extra_ports = int(data.get("bot_extra_ports", 0))
        except Exception:
            return jsonify({"error": "bot_extra_ports must be an integer"}), 400
        _set_user_extra_ports(uid, extra_ports)

    updated = get_user_by_id(uid) or user
    hosting = _bot_hosting_access(updated)
    return jsonify({
        "ok": True,
        "id": uid,
        "plan": updated.get("plan", "free"),
        "bot_hosting_allowed": bool(hosting["allowed"]),
        "bot_hosting_limit": int(hosting["limit"]),
        "bot_hosting_source": hosting["source"],
        "bot_hosting_override_enabled": bool(hosting["override"].get("enabled")),
        "bot_hosting_override_limit": int(hosting["override"].get("limit", 0)),
        "bot_port_base": _HOSTED_BOT_BASE_PORT_ALLOWANCE,
        "bot_extra_ports": _get_user_extra_ports(uid),
        "bot_port_quota": _user_port_quota(uid),
    })


@app.route("/admin/api/godview")
@_admin_required
def admin_godview():
    """God-view: live feed of recent generations + user stats for super-admin overview."""
    import psutil, platform

    # Recent generations — last 40
    try:
        recent_logs = get_requests(limit=40, offset=0)
    except Exception:
        recent_logs = []

    # Recent users — last 20 signups
    try:
        recent_users = list_users(limit=20, offset=0)
    except Exception:
        recent_users = []

    # System resource snapshot
    try:
        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        uptime_s = int(time.time() - psutil.boot_time())
        sys_res = {
            "cpu_pct": cpu,
            "mem_used_mb": round(mem.used / 1024 / 1024),
            "mem_total_mb": round(mem.total / 1024 / 1024),
            "mem_pct": mem.percent,
            "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
            "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
            "disk_pct": disk.percent,
            "uptime_s": uptime_s,
        }
    except Exception:
        sys_res = {}

    # Active users in last 1 h (IPs that made requests)
    try:
        hour_ago = time.time() - 3600
        all_recent = get_requests(limit=500, offset=0)
        gens_hr       = [r for r in all_recent if r.get("ts", 0) > hour_ago]
        active_ips    = len({r["ip"] for r in gens_hr})
        gen_last_hour = len(gens_hr)
        ok_last_hour  = sum(1 for r in gens_hr if r.get("success") == 1)
        fail_last_hour = sum(1 for r in gens_hr if r.get("success") == 0)
    except Exception:
        active_ips = gen_last_hour = ok_last_hour = fail_last_hour = 0

    return jsonify({
        "recent_logs": [dict(r) for r in recent_logs],
        "recent_users": [dict(u) for u in recent_users],
        "system": sys_res,
        "live": {
            "active_ips_1h": active_ips,
            "gen_last_hour": gen_last_hour,
            "ok_last_hour":  ok_last_hour,
            "fail_last_hour": fail_last_hour,
        },
    })


@app.route("/admin/api/server-log")
@_admin_required
def admin_server_log():
    """Tail the stacknest systemd service log (last N lines)."""
    import subprocess as _sp
    n = min(int(request.args.get("lines", 200)), 1000)
    try:
        result = _sp.run(
            ["journalctl", "-u", "stacknest", f"-n{n}", "--no-pager", "--output=cat"],
            capture_output=True, text=True, timeout=8,
        )
        lines = result.stdout.splitlines()
        return jsonify({"ok": True, "lines": lines, "count": len(lines)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "lines": [], "count": 0})


@app.route("/admin")
def admin_page():
    return send_from_directory(app.static_folder, "admin.html")


# ---------------------------------------------------------------------------
# Admin DB Backup / Restore
# ---------------------------------------------------------------------------

@app.route("/admin/api/backup/health")
@_admin_required
def admin_backup_health():
    """DB integrity snapshot + row counts."""
    return jsonify(get_db_health())


@app.route("/admin/api/backup/list")
@_admin_required
def admin_backup_list():
    """List all backup files."""
    return jsonify({"backups": list_backups()})


@app.route("/admin/api/backup/create", methods=["POST"])
@_admin_required
def admin_backup_create():
    """Trigger an on-demand backup. Optional body: {label: "my-label"}"""
    data  = request.get_json(silent=True) or {}
    label = str(data.get("label", "")).strip()[:40] or None
    try:
        result = create_backup(label=label)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/api/backup/verify/<path:filename>", methods=["POST"])
@_admin_required
def admin_backup_verify(filename: str):
    """Run SHA-256 + integrity_check on a backup file."""
    import re as _re
    if not _re.match(r'^stacknest-[\w\-\.]+\.db$', filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    return jsonify(verify_backup(filename))


@app.route("/admin/api/backup/restore/<path:filename>", methods=["POST"])
@_admin_required
def admin_backup_restore(filename: str):
    """
    Restore live DB from a backup.
    Body must contain: {"confirm": true}
    """
    import re as _re
    if not _re.match(r'^stacknest-[\w\-\.]+\.db$', filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"ok": False, "error": "Pass {'confirm': true} to confirm restore"}), 400
    try:
        result = restore_backup(filename)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/api/backup/<path:filename>", methods=["DELETE"])
@_admin_required
def admin_backup_delete(filename: str):
    """Delete a single backup file (and its SHA-256 sidecar)."""
    import re as _re
    if not _re.match(r'^stacknest-[\w\-\.]+\.db$', filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    result = delete_backup(filename)
    if not result["ok"]:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/admin/api/backup/cleanup", methods=["POST"])
@_admin_required
def admin_backup_cleanup():
    """
    Delete old backups according to retention policy.
    Body (optional): {"keep": 10, "max_age_days": 30}
    """
    data        = request.get_json(silent=True) or {}
    keep        = max(3, int(data.get("keep", 10)))
    max_age_days = max(1.0, float(data.get("max_age_days", 30)))
    result = cleanup_old_backups(keep=keep, max_age_days=max_age_days)
    return jsonify({"ok": True, **result})


@app.route("/admin/api/paper/refresh", methods=["POST"])
@_admin_required
def admin_paper_refresh():
    """
    Force-refresh the Paper version cache from the PaperMC API.
    Downloads the latest Paper API JAR and brigadier JAR if the version changed.
    """
    from api.paper_versions import refresh as _pv_refresh, STABLE_MC_VERSION, STABLE_JAVA_VERSION, BRIGADIER_VERSION
    fetched = _pv_refresh(force=True)
    return jsonify({
        "ok": True,
        "fetched_from_network": fetched,
        "stable_mc_version": STABLE_MC_VERSION,
        "stable_java_version": STABLE_JAVA_VERSION,
        "brigadier_version": BRIGADIER_VERSION,
    })


# ---------------------------------------------------------------------------
# Admin auth endpoints
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["POST"])
@limiter.limit("10 per hour", key_func=get_remote_address)
def admin_login():
    """Validate admin password and set a signed session cookie."""
    if not ADMIN_SECRET:
        return jsonify({"error": "Admin panel disabled — set ADMIN_SECRET"}), 503

    ip = get_remote_address()
    if not _check_login_throttle(ip):
        return jsonify({"error": "Too many failed attempts. Try again in 15 minutes."}), 429

    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()

    # Constant-time compare
    if not hmac.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(),
        hashlib.sha256(ADMIN_SECRET.encode()).hexdigest(),
    ):
        _record_failed_login(ip)
        return jsonify({"error": "Invalid password"}), 401

    token = _make_admin_token()
    # Record successful login: IP + user-agent
    try:
        set_meta("admin_last_login", json.dumps({
            "ip": ip,
            "ts": time.time(),
            "ua": request.headers.get("User-Agent", "")[:120],
        }))
    except Exception:
        pass
    resp = make_response(jsonify({"ok": True, "token": token}))
    resp.set_cookie(
        "sn_admin", token,
        httponly=True,
        secure=(ADMIN_COOKIE_SECURE or request.is_secure),
        samesite=ADMIN_COOKIE_SAMESITE,
        path="/",
        max_age=ADMIN_TOKEN_TTL,
    )
    return resp


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("sn_admin", path="/")
    return resp


@app.route("/admin/api/session")
@_admin_required
def admin_session():
    """Quick session validity check — returns 200 if cookie is valid."""
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin API — all protected by _admin_required
# ---------------------------------------------------------------------------

@app.route("/admin/api/stats")
@_admin_required
def admin_stats():
    """High-level dashboard stats."""
    from inference.kimi import is_available as kimi_ok
    from inference.gemini import is_available as gemini_ok
    from inference.claude import is_available as claude_ok
    llamacpp_ok = health_check(timeout=3.0)
    model_info = get_model_info()
    db_stats = get_stats()

    # ── Security: last login + last access records ────────────────────── #
    def _load_meta_json(key: str) -> dict:
        row = get_meta(key)
        if not row:
            return {}
        try:
            return json.loads(row["value"])
        except Exception:
            return {}

    last_login  = _load_meta_json("admin_last_login")
    last_access = _load_meta_json("admin_last_access")
    try:
        discord_stats = get_discord_stats()
    except Exception:
        discord_stats = None

    return jsonify({
        "system": {
            "api":              "ok",
            "inference_server": "ok" if llamacpp_ok else "unreachable",
            "free_ai":          "available" if gemini_ok() else "no_key",
            "premium_ai":       "available" if claude_ok() else "no_key",
            "kimi_validate":    "available" if kimi_ok() else "no_key",
            "model":            model_info,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
        },
        "db": db_stats,
        "discord": discord_stats,
        "security": {
            "last_login": {
                "ip": last_login.get("ip", "—"),
                "ts": last_login.get("ts"),
                "ua": last_login.get("ua", ""),
            },
            "last_access": {
                "ip":   last_access.get("ip", "—"),
                "ts":   last_access.get("ts"),
                "ua":   last_access.get("ua", ""),
                "path": last_access.get("path", ""),
            },
        },
    })




@app.route("/admin/api/bot-stats")
@_admin_required
def admin_bot_stats():
    """Return aggregate bot hosting metrics for the admin dashboard."""
    from api.db import _conn
    try:
        with _conn() as con:
            # Check table exists before querying
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "hosted_bots" not in tables:
                return jsonify({"total": 0, "running": 0, "stopped": 0, "recent": [], "note": "bot hosting not yet enabled"})
            total   = con.execute("SELECT COUNT(*) FROM hosted_bots").fetchone()[0]
            running = con.execute(
                "SELECT COUNT(*) FROM hosted_bots WHERE status='running'").fetchone()[0]
            stopped = con.execute(
                "SELECT COUNT(*) FROM hosted_bots WHERE status IN ('stopped','idle')").fetchone()[0]
            recent  = con.execute(
                """SELECT id, bot_name, status, language, user_id, last_ping
                   FROM hosted_bots ORDER BY created_at DESC LIMIT 20""").fetchall()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "total":   total,
        "running": running,
        "stopped": stopped,
        "recent":  [dict(r) for r in recent],
    })
@app.route("/admin/api/chart-data")
@_admin_required
def admin_chart_data():
    """14-day daily activity data for dashboard charts."""
    return jsonify(get_daily_chart_data(days=14))


@app.route("/admin/api/logs")
@_admin_required
def admin_logs():
    """Paginated request log with optional filters."""
    limit   = min(int(request.args.get("limit",  100)), 500)
    offset  = int(request.args.get("offset", 0))
    ip_f    = request.args.get("ip")       or None
    ep_f    = request.args.get("endpoint") or None
    ok_raw  = request.args.get("success")
    ok_f    = None if ok_raw is None else (ok_raw.lower() == "true")
    ts_from_r = request.args.get("from")
    ts_to_r   = request.args.get("to")
    ts_from = float(ts_from_r) if ts_from_r else None
    ts_to   = float(ts_to_r)   if ts_to_r   else None
    rows    = get_requests(limit=limit, offset=offset,
                           ip_filter=ip_f, endpoint_filter=ep_f,
                           success_filter=ok_f, ts_from=ts_from, ts_to=ts_to)
    return jsonify({"logs": rows, "limit": limit, "offset": offset})


@app.route("/admin/api/logs/<int:row_id>")
@_admin_required
def admin_log_detail(row_id: int):
    """Get full detail (including raw_code) for a single request."""
    row = get_request_by_id(row_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row)


@app.route("/admin/api/logs/clear", methods=["POST"])
@_admin_required
def admin_logs_clear():
    """Delete logs older than N days (default 30)."""
    days = int((request.get_json(silent=True) or {}).get("days", 30))
    clear_old_logs(days)
    return jsonify({"ok": True, "deleted_before_days": days})


@app.route("/admin/api/ips")
@_admin_required
def admin_ips():
    """List all IP notes (banned / bypassed)."""
    return jsonify({"ips": get_ip_notes()})


@app.route("/admin/api/ips", methods=["POST"])
@_admin_required
def admin_set_ip():
    """Set a note, ban, or bypass for an IP."""
    data = request.get_json(silent=True) or {}
    ip   = data.get("ip", "").strip()
    if not ip:
        return jsonify({"error": "'ip' is required"}), 400
    set_ip_note(
        ip=ip,
        note=data.get("note", ""),
        banned=bool(data.get("banned", False)),
        bypass=bool(data.get("bypass_limits", False)),
    )
    return jsonify({"ok": True})


@app.route("/admin/api/ips/<path:ip>", methods=["DELETE"])
@_admin_required
def admin_delete_ip(ip: str):
    """Remove ban/bypass entry for an IP."""
    delete_ip_note(ip)
    return jsonify({"ok": True})


@app.route("/admin/api/tickets")
@_admin_required
def admin_list_tickets():
    """List support tickets with optional status filter."""
    status = request.args.get("status", "")
    limit  = min(int(request.args.get("limit",  50)), 200)
    offset = int(request.args.get("offset", 0))
    rows   = get_tickets(status=status or None, limit=limit, offset=offset)
    return jsonify({"tickets": rows})


@app.route("/admin/api/tickets/<int:ticket_id>", methods=["PATCH"])
@_admin_required
def admin_update_ticket(ticket_id: int):
    """Update a ticket's status and/or admin note."""
    data   = request.get_json(silent=True) or {}
    status = str(data.get("status", "open")).lower()
    note   = str(data.get("admin_note", ""))
    if status not in ("open", "in_progress", "closed"):
        return jsonify({"error": "status must be open / in_progress / closed"}), 400
    update_ticket_status(ticket_id, status, note)
    return jsonify({"ok": True})


@app.route("/admin/api/tickets/<int:ticket_id>", methods=["GET"])
@_admin_required
def admin_get_ticket(ticket_id: int):
    """Return a single ticket with its message thread."""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    ticket["messages"] = get_ticket_messages(ticket_id)
    return jsonify(ticket)


@app.route("/admin/api/tickets/<int:ticket_id>/reply", methods=["POST"])
@_admin_required
def admin_reply_ticket(ticket_id: int):
    """Send an admin reply: saves message thread entry + emails the user."""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    data = request.get_json(silent=True) or {}
    body = str(data.get("body", "")).strip()
    if len(body) < 2:
        return jsonify({"error": "Reply body is required"}), 400
    if len(body) > 4000:
        return jsonify({"error": "Reply too long (max 4000 chars)"}), 400
    add_ticket_message(ticket_id, body, sender="admin")
    # Auto-advance status to in_progress if still open
    if ticket["status"] == "open":
        update_ticket_status(ticket_id, "in_progress", ticket.get("admin_note", ""))
    # Email the user
    _send_ticket_reply(
        to=ticket["email"],
        subject=ticket["subject"],
        reply_body=body,
        ticket_id=ticket_id,
    )
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Arti admin endpoints
# ---------------------------------------------------------------------------

@app.route("/admin/api/arti", methods=["GET"])
@_admin_required
def admin_arti_status():
    """Return current Arti away-mode state."""
    with _arti_lock:
        state = _load_arti_state()
    return jsonify(state)


@app.route("/admin/api/arti", methods=["POST"])
@_admin_required
def admin_arti_toggle():
    """Enable or disable Arti away mode."""
    data    = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))
    message = str(data.get("message", "")).strip()[:500]
    with _arti_lock:
        state = _load_arti_state()
        state["enabled"] = enabled
        state["message"] = message
        if enabled and not state.get("since"):
            state["since"] = time.time()
        elif not enabled:
            state["since"] = None
        _arti_log(state, ("Arti mode ENABLED" if enabled else "Arti mode DISABLED")
                  + (f" — {message}" if message else ""))
        _save_arti_state(state)
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/admin/api/arti/reply/<int:ticket_id>", methods=["POST"])
@_admin_required
def admin_arti_reply(ticket_id: int):
    """Ask Arti to generate and save a reply for a support ticket."""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404

    subject   = ticket.get("subject", "(no subject)")
    message   = ticket.get("message", "")
    email     = ticket.get("email", "user")
    user_msg  = f"Support ticket from {email}:\nSubject: {subject}\n\n{message}"

    try:
        from inference.gemini import is_available as _g_ok, gemini_generate as _g_gen
        from inference.claude import is_available as _c_ok, claude_generate as _c_gen
        if _g_ok():
            reply = _g_gen(user_msg, _ARTI_SYSTEM)
        elif _c_ok():
            reply = _c_gen(user_msg, _ARTI_SYSTEM)
        else:
            return jsonify({"error": "No AI backend available for Arti"}), 503

        update_ticket_status(ticket_id, "in_progress", reply[:2000])
        with _arti_lock:
            state = _load_arti_state()
            _arti_log(state, f"Replied to ticket #{ticket_id}: {subject[:60]}")
            _save_arti_state(state)
        return jsonify({"ok": True, "reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/admin/api/heal", methods=["POST"])
@_admin_required
def admin_heal():
    """
    Admin: trigger Context-Aware Healing on a specific logged request.
    Body: { "log_id": 42 }  OR  { "code": "...", "errors": [...] }
    """
    from inference.kimi import is_available as kimi_ok, kimi_heal
    data = request.get_json(silent=True) or {}

    code, errors = "", []
    if "log_id" in data:
        row = get_request_by_id(int(data["log_id"]))
        if not row:
            return jsonify({"error": "Log entry not found"}), 404
        code   = row.get("raw_code", "") or ""
        errors = json.loads(row.get("errors_json") or "[]") 
    else:
        code   = data.get("code", "")
        errors = data.get("errors", [])

    if not code:
        return jsonify({"error": "No code to heal"}), 400

    if not kimi_ok():
        return jsonify({"error": "Kimi API key not configured"}), 503

    try:
        healed = kimi_heal(code, errors)
        result = run_validation_only(healed)
        return jsonify({
            "healed":  result.success,
            "code":    healed,
            "errors":  result.final_errors,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/regenerate", methods=["POST"])
@_admin_required
def admin_regenerate():
    """
    Admin: regenerate plugin from a logged instruction.
    Body: { "log_id": 42 }  OR  { "instruction": "..." }
    When log_id is provided and generation succeeds, the original request
    record is updated in-place so the user sees the correct result.
    """
    data = request.get_json(silent=True) or {}
    log_id = int(data["log_id"]) if "log_id" in data else None

    if log_id is not None:
        row = get_request_by_id(log_id)
        if not row:
            return jsonify({"error": "Log entry not found"}), 404
        instruction = row.get("instruction", "")
    else:
        instruction = data.get("instruction", "")

    if not instruction:
        return jsonify({"error": "No instruction found"}), 400

    repair_tier = _to_inference_tier(get_tier())
    gen = PluginGenerator(router=_router, params=GenerationParams(max_tokens=2048), tier=repair_tier)
    try:
        result = gen.generate(instruction)
        # Patch the original request record so the user's history reflects the fix
        if log_id is not None:
            update_request(
                log_id,
                success=result.success,
                attempts=result.attempts,
                elapsed=result.elapsed_seconds,
                compile_ok=result.success,
                errors=result.final_errors if result.final_errors else [],
                code=result.code,
            )
        return jsonify({
            "success":  result.success,
            "code":     result.code,
            "attempts": result.attempts,
            "elapsed":  round(result.elapsed_seconds, 1),
            "errors":   result.final_errors,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------------- #
# Context-Aware Healing                                                        #
# --------------------------------------------------------------------------- #

@app.route("/api/heal", methods=["POST"])
def heal():
    """
    Context-Aware Healing — automatically fix build / compile errors in code.

    Request body:
    {
        "code":   "```java ...``` ```yaml ...```",  // current broken code
        "errors": ["error line 1", "error line 2"]  // errors from /api/validate
    }

    Response:
    {
        "healed":  true,
        "code":    "...corrected code...",
        "errors":  [],          // remaining errors after heal attempt
        "source":  "kimi"       // 'kimi' (cloud) or 'local' (retry)
    }
    """
    data = request.get_json(silent=True) or {}
    code   = data.get("code", "").strip()
    errors = data.get("errors", [])

    if not code:
        return jsonify({"error": "'code' field is required"}), 400

    gate = _editor_limit_response("healing", code)
    if gate is not None:
        return gate

    feature_gate = _editor_feature_gate("pro", "Auto-Heal")
    if feature_gate is not None:
        return feature_gate

    from inference.kimi import is_available as kimi_ok, kimi_heal

    if not kimi_ok():
        return jsonify({
            "error": "Context-Aware Healing is temporarily unavailable.",
            "kimi_available": False,
        }), 503

    try:
        healed_code = kimi_heal(code, errors)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Healing is temporarily unavailable. Please try again later."}), 503

    # Re-validate the healed code
    try:
        result = run_validation_only(healed_code)
        remaining_errors = result.final_errors
    except Exception:
        remaining_errors = []

    if get_tier() == "free":
        healed_code = _brand_free_tier(healed_code)

    return jsonify({
        "healed":  len(remaining_errors) == 0,
        "code":    healed_code,
        "errors":  remaining_errors,
        "source":  "kimi",
    })


# --------------------------------------------------------------------------- #
# Kimi deep-validation                                                         #
# --------------------------------------------------------------------------- #

@app.route("/api/kimi/validate", methods=["POST"])
def kimi_validate_endpoint():
    """
    Deep code validation using Kimi K2.5.
    Returns issues found and an auto-corrected version.

    Request body: { "code": "..." }
    Response:
    {
        "valid":       false,
        "issues":      ["[ERROR] ...", "[WARNING] ..."],
        "fixed_code":  "...",   // corrected code or null
        "kimi_available": true
    }
    """
    from inference.kimi import is_available as kimi_ok, kimi_validate

    if not kimi_ok():
        return jsonify({
            "error": "Deep validation is temporarily unavailable.",
            "kimi_available": False,
        }), 503

    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"error": "'code' is required"}), 400

    gate = _editor_limit_response("running deep validation", code)
    if gate is not None:
        return gate

    feature_gate = _editor_feature_gate("starter", "Deep Check")
    if feature_gate is not None:
        return feature_gate

    try:
        result = kimi_validate(code)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Deep validation is temporarily unavailable. Please try again later."}), 503

    return jsonify({**result, "kimi_available": True})


# --------------------------------------------------------------------------- #
# Server Setup Assistant                                                       #
# --------------------------------------------------------------------------- #

# Per-tier limits (requests per day)
_SETUP_LIMITS = {
    "free":    "1 per day",
    "starter": "5 per day",
    "pro":     "20 per day",
    "studio":  "50 per day",
}

@app.route("/api/setup-assistant", methods=["POST"])
@limiter.limit("10 per hour", key_func=get_remote_address)
def setup_assistant():
    """
    Generate a Minecraft server setup plan from a plain-English description.

    Request body:
    {
        "description": "survival server with economy, land claiming, and a shop"
    }

    Tier behaviour:
        free    → basic plugin list only (Gemini, 1/day)
        starter → full guide, no per-server breakdown (Kimi, 5/day)
        pro     → full guide with configs, load order, conflicts (Kimi, 20/day)
        studio  → multi-server network design (Kimi, 50/day)

    Response:
    {
        "plan":  "## Plugin Stack\n...",
        "mode":  "basic|full|multi",
        "tier":  "free|starter|pro|studio"
    }
    """
    from inference.kimi     import is_available as kimi_ok, kimi_setup_assistant
    from inference.gemini   import is_available as gemini_ok, gemini_simple
    from inference.deepseek import is_available as deepseek_ok, deepseek_generate
    from inference.claude   import is_available as claude_ok, claude_generate
    from inference.kimi     import _SETUP_SYSTEM_BASIC, _SETUP_SYSTEM_FULL, _SETUP_SYSTEM_MULTI

    data        = request.get_json(silent=True, force=True) or {}
    description = str(data.get("description", "")).strip()

    if not description:
        return jsonify({"error": "'description' field is required"}), 400
    if len(description) < 10:
        return jsonify({"error": "Please describe your server in at least 10 characters"}), 400
    if len(description) > 2000:
        return jsonify({"error": "Description too long (max 2000 characters)"}), 400

    user   = _current_user()
    plan   = (user.get("plan") or "free") if user else "free"
    plan   = plan.strip().lower()
    if plan not in ("free", "starter", "pro", "studio"):
        plan = "free"

    # Determine generation mode from plan
    if plan == "studio":
        mode = "multi"
    elif plan in ("pro", "starter"):
        mode = "full"
    else:
        mode = "basic"

    # Rate-limit per user account (in addition to IP limiter above)
    if user:
        uid      = int(user["id"])
        day_key  = f"setup:{uid}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        day_max  = {"free": 1, "starter": 5, "pro": 20, "studio": 50}.get(plan, 1)
        # Use the existing generation counter table as a simple flag store
        # We piggyback on log_request to record usage; check by counting today's rows.
        today_count = 0
        try:
            with _conn() as con:
                today_count = con.execute(
                    "SELECT COUNT(*) FROM requests WHERE user_id=? AND endpoint='/api/setup-assistant' "
                    "AND created_at >= date('now')",
                    (uid,),
                ).fetchone()[0]
        except Exception:
            pass
        if today_count >= day_max:
            return jsonify({
                "error": f"Daily limit reached ({day_max}/day on {plan} plan). Upgrade for more.",
                "upgrade_url": "/pricing",
            }), 429

    ip = get_remote_address()
    log_request(ip=ip, endpoint="/api/setup-assistant", tier=plan,
                instruction=description[:200], success=False)

    try:
        if mode == "basic":
            # Free tier — try Gemini → DeepSeek → Claude → Kimi in order
            result_text = None
            last_err = None
            for _name, _avail, _fn in [
                ("gemini",   gemini_ok,   lambda: gemini_simple(_SETUP_SYSTEM_BASIC, description, max_tokens=2500)),
                ("deepseek", deepseek_ok, lambda: deepseek_generate(description, _SETUP_SYSTEM_BASIC, max_tokens=2500)),
                ("claude",   claude_ok,   lambda: claude_generate(description, _SETUP_SYSTEM_BASIC, max_tokens=2500)),
                ("kimi",     kimi_ok,     lambda: kimi_setup_assistant(description, mode="basic")),
            ]:
                if not _avail():
                    continue
                try:
                    result_text = _fn()
                    if result_text:
                        break
                except Exception as _e:
                    last_err = _e
                    traceback.print_exc()
                    continue
            if not result_text:
                app.logger.error("[setup-assistant] all free backends failed. last=%s", last_err)
                return jsonify({"error": "AI service temporarily unavailable. Please try again later."}), 503
        else:
            # Paid tiers — try Kimi → DeepSeek → Claude → Gemini in order.
            # Use the appropriate full/multi system prompt so fallback backends
            # produce the same quality output as Kimi (not the basic free-tier prompt).
            _paid_prompt = _SETUP_SYSTEM_MULTI if mode == "multi" else _SETUP_SYSTEM_FULL
            result_text = None
            last_err = None
            for _name, _avail, _fn in [
                ("kimi",     kimi_ok,     lambda: kimi_setup_assistant(description, mode=mode)),
                ("deepseek", deepseek_ok, lambda: deepseek_generate(description, _paid_prompt, max_tokens=3500)),
                ("claude",   claude_ok,   lambda: claude_generate(description, _paid_prompt, max_tokens=3500)),
                ("gemini",   gemini_ok,   lambda: gemini_simple(_paid_prompt, description, max_tokens=3500)),
            ]:
                if not _avail():
                    continue
                try:
                    result_text = _fn()
                    if result_text:
                        break
                except Exception as _e:
                    last_err = _e
                    traceback.print_exc()
                    continue
            if not result_text:
                app.logger.error("[setup-assistant] all paid backends failed. last=%s", last_err)
                return jsonify({"error": "AI service temporarily unavailable. Please try again later."}), 503
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Setup assistant is temporarily unavailable. Please try again later."}), 503

    # Update the last log_request row to mark success
    try:
        with _conn() as con:
            con.execute(
                "UPDATE requests SET success=1 WHERE ip=? AND endpoint='/api/setup-assistant' "
                "ORDER BY id DESC LIMIT 1",
                (ip,),
            )
    except Exception:
        pass

    # Strip any AI-generated upsell footer (limited match to prevent ReDoS)
    result_text = re.sub(
        r'\n*---\n\*Upgrade to Pro for[^*]{0,300}\*\s*$',
        '',
        result_text,
        flags=re.IGNORECASE,
    ).rstrip()

    return jsonify({
        "plan":  result_text,
        "mode":  mode,
        "tier":  plan,
    })


# --------------------------------------------------------------------------- #
# Server log analysis                                                          #
# --------------------------------------------------------------------------- #

@app.route("/api/logs/analyze", methods=["POST"])
def analyze_log():
    """
    Analyse a Minecraft server log and return a structured AI diagnosis.
    Works like mclogs but powered by Kimi AI.

    Request body:
    {
        "log": "<full server log text>"
    }
    OR multipart form with field 'log'.

    Response:
    {
        "analysis": "## Issue 1: ...\n...",
        "error_count": 3,
        "kimi_available": true
    }
    """
    from inference.kimi import is_available as kimi_ok, kimi_analyze_log

    log_text = ""
    if request.is_json:
        log_text = (request.get_json(silent=True) or {}).get("log", "")
    else:
        log_text = request.form.get("log", "") or ""

    if not log_text.strip():
        return jsonify({"error": "'log' field is required"}), 400

    if len(log_text) > LOG_ANALYSIS_MAX_CHARS:
        return jsonify({"error": f"Log too large (max {LOG_ANALYSIS_MAX_CHARS} chars)"}), 400

    lines = log_text.splitlines()

    def _line_level(line: str) -> str:
        low = line.lower()
        if "[severe]" in low:
            return "error"
        if "[error]" in low:
            return "error"
        if "[warn]" in low or "[warning]" in low:
            return "warn"
        if "exception" in low and not line.lstrip().startswith("at "):
            return "error"
        return "info"

    def _is_stack_line(line: str) -> bool:
        stripped = line.lstrip()
        return (
            stripped.startswith("at ")
            or stripped.startswith("... ")
            or stripped.lower().startswith("caused by:")
        )

    def _extract_plugin(line: str) -> str | None:
        m = re.search(r"\[(?!\d{2}:\d{2}:\d{2})([^\]]+)\]", line)
        if not m:
            return None
        name = m.group(1).strip()
        if name.lower() in {"error", "warn", "warning", "info", "severe"}:
            return None
        # Filter out Minecraft thread identifiers like "Server thread/WARN"
        if "/" in name:
            return None
        if len(name) > 48:
            return None
        return name

    issues: list[dict] = []
    plugin_counter: Counter[str] = Counter()
    exception_counter: Counter[str] = Counter()

    i = 0
    while i < len(lines):
        line = lines[i]
        level = _line_level(line)
        if level in {"error", "warn"}:
            trace: list[str] = []
            j = i + 1
            while j < len(lines) and _is_stack_line(lines[j]):
                trace.append(lines[j].rstrip())
                j += 1

            plugin = _extract_plugin(line)
            if plugin:
                plugin_counter[plugin] += 1

            for em in re.findall(r"\b[\w$.]*(?:Exception|Error)\b", line):
                if len(em) > 8:
                    exception_counter[em] += 1

            issues.append(
                {
                    "line": i + 1,
                    "severity": level,
                    "message": line.strip(),
                    "trace": trace[:10],
                    "plugin": plugin,
                }
            )
            i = j
            continue
        i += 1

    error_count = sum(1 for x in issues if x["severity"] == "error")
    warning_count = sum(1 for x in issues if x["severity"] == "warn")
    top_exceptions = [
        {"name": name, "count": cnt}
        for name, cnt in exception_counter.most_common(8)
    ]
    probable_plugins = [
        {"name": name, "count": cnt}
        for name, cnt in plugin_counter.most_common(8)
    ]

    if not kimi_ok():
        if not issues:
            return jsonify(
                {
                    "analysis": "No obvious errors found in your log.",
                    "error_count": 0,
                    "warning_count": 0,
                    "issue_count": 0,
                    "issues": [],
                    "top_exceptions": [],
                    "probable_plugins": [],
                    "analysis_version": "v2",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "kimi_available": False,
                }
            )

        fallback = [
            "## Quick triage (basic mode)",
            "",
            f"- Errors: **{error_count}**",
            f"- Warnings: **{warning_count}**",
            "",
            "### First issues",
        ]
        for item in issues[:15]:
            fallback.append(
                f"- [L{item['line']}] **{item['severity'].upper()}** `{item['message'][:180]}`"
            )

        return jsonify(
            {
                "analysis": "\n".join(fallback),
                "error_count": error_count,
                "warning_count": warning_count,
                "issue_count": len(issues),
                "issues": issues[:LOG_ANALYSIS_MAX_ISSUES],
                "top_exceptions": top_exceptions,
                "probable_plugins": probable_plugins,
                "analysis_version": "v2",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "kimi_available": False,
            }
        )

    try:
        analysis = kimi_analyze_log(log_text)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Log analysis is temporarily unavailable. Please try again later."}), 503

    return jsonify(
        {
            "analysis": analysis,
            "error_count": error_count,
            "warning_count": warning_count,
            "issue_count": len(issues),
            "issues": issues[:LOG_ANALYSIS_MAX_ISSUES],
            "top_exceptions": top_exceptions,
            "probable_plugins": probable_plugins,
            "analysis_version": "v2",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "kimi_available": True,
        }
    )


# --------------------------------------------------------------------------- #
# Plugin Presets  (zero AI cost — static compiled templates)                  #
# --------------------------------------------------------------------------- #

@app.route("/api/presets", methods=["GET"])
def list_presets():
    """Return the preset catalog (no auth required)."""
    from api.presets import preset_catalog
    return jsonify(preset_catalog())


@app.route("/api/preset/build", methods=["POST"])
@limiter.limit("30 per hour")
def build_preset_jar():
    """
    Compile a static preset and return a ready-to-deploy JAR.
    No AI is used — compiles a fully-written Java template.

    Request body:
    {
        "preset_id":   "heal",
        "plugin_name": "MyHeal"   // optional, defaults to preset name
    }

    Response: application/java-archive  (binary download)
    """
    user = _get_current_user()
    if not user:
        return jsonify({"error": "Authentication required."}), 401

    data = request.get_json(silent=True) or {}
    preset_id = str(data.get("preset_id", "")).strip().lower()
    if not preset_id:
        return jsonify({"error": "'preset_id' is required."}), 400

    from api.presets import get_preset, build_preset
    preset = get_preset(preset_id)
    if preset is None:
        return jsonify({"error": f"Unknown preset: {preset_id!r}"}), 404

    plugin_name = str(data.get("plugin_name") or preset["name"]).strip() or preset["name"]
    target_api  = _normalize_target_api(str(data.get("target_api", "26.1.x")))
    paper_profile = _paper_profile_for_target_api(target_api)

    try:
        jar_bytes = build_preset(preset_id, plugin_name, paper_profile=paper_profile)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 422
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Preset build failed. Please try again."}), 500

    from api.presets import _to_pascal  # noqa: PLC0415
    safe_name = re.sub(r"[^\w\-]", "", _to_pascal(plugin_name))[:64] or "Plugin"
    resp = make_response(jar_bytes)
    resp.headers["Content-Type"] = "application/java-archive"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.jar"'
    resp.headers["Content-Length"] = str(len(jar_bytes))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# --------------------------------------------------------------------------- #
# Ready-to-Deploy JAR                                                          #
# --------------------------------------------------------------------------- #

@app.route("/api/jar", methods=["POST"])
def download_jar():
    """
    Compile generated plugin code and return a ready-to-deploy .jar file.

    Request body:
    {
        "code":        "```java ...``` ```yaml ...```",
        "plugin_name": "MyPlugin"   // optional, defaults to 'StackNestPlugin'
    }

    Response: application/java-archive  (binary download)
    """
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()
    plugin_name = data.get("plugin_name", "StackNestPlugin").strip() or "StackNestPlugin"
    target_api = _normalize_target_api(str(data.get("target_api", "26.1.x")))
    paper_profile = _paper_profile_for_target_api(target_api)

    if not code:
        return jsonify({"error": "'code' field is required"}), 400

    gate = _editor_limit_response("building a JAR", code)
    if gate is not None:
        return gate

    tier = get_tier()
    if tier == "free":
        code = _brand_free_tier(code)

    try:
        jar_bytes = build_jar(code, plugin_name, paper_profile=paper_profile)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "JAR build failed due to an internal error. Please try again."}), 500

    safe_name = re.sub(r"[^\w\-]", "", plugin_name)[:64] or "StackNestPlugin"
    response_obj = make_response(jar_bytes)
    response_obj.headers["Content-Type"] = "application/java-archive"
    response_obj.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.jar"'
    response_obj.headers["Content-Length"] = str(len(jar_bytes))
    response_obj.headers["Cache-Control"] = "no-store"
    response_obj.headers["X-Content-Type-Options"] = "nosniff"
    return response_obj


# --------------------------------------------------------------------------- #
# Runtime Plugin Testing  (Starter+)                                          #
# --------------------------------------------------------------------------- #

@app.route("/api/test-plugin", methods=["POST"])
@limiter.limit("30 per hour")
def test_plugin_endpoint():
    """
    Test a plugin JAR on a real Paper server instance and stream results via SSE.

    Accepts either:
      1. multipart/form-data  — jar=<file>, plugin_name=<str> (optional)
      2. application/json     — {"code": "...", "plugin_name": "..."}
                                (builds the JAR first then runs it)

    Returns: text/event-stream (SSE)
    Requires: Starter plan or above.
    """
    from flask import stream_with_context

    # ── Auth ────────────────────────────────────────────────────────────────
    user = _current_user()
    if not user:
        return jsonify({"error": "Login required to use runtime plugin testing."}), 401
    if user.get("is_banned"):
        return jsonify({"error": "Access denied."}), 403

    plan = _normalize_editor_plan(user.get("plan"))
    if plan not in _PAID_PLANS:
        return jsonify({
            "error": (
                "Runtime plugin testing is available on Starter and above. "
                "Upgrade at stacknest.com/pricing."
            ),
            "upgrade":     True,
            "upgrade_url": "/pricing",
        }), 403

    user_id = int(user["id"])

    # ── Security suspension check ────────────────────────────────────────────
    if is_rt_test_suspended(user_id):
        return jsonify({
            "error": (
                "Your runtime testing access has been temporarily suspended for 24 hours "
                "due to a security policy violation. Contact support if you believe this is a mistake."
            ),
            "suspended": True,
        }), 403

    # ── Runtime test quota ──────────────────────────────────────────────────
    allowed, rt_usage = check_runtime_test_limit(user_id)
    if not allowed:
        return jsonify({
            "error": (
                f"Monthly runtime test limit reached "
                f"({rt_usage['tests_limit']}/month for {plan}). "
                f"Resets in {rt_usage['days_until_reset']} days."
            ),
            "usage": rt_usage,
        }), 429

    # ── Get JAR bytes ────────────────────────────────────────────────────────
    jar_bytes:   bytes = b""
    plugin_name: str   = "TestPlugin"
    target_api: str    = "26.1.x"
    is_external: bool  = False    # True when the user uploaded their own JAR

    ct = request.content_type or ""
    if "multipart" in ct:
        jar_file = request.files.get("jar")
        if not jar_file:
            return jsonify({"error": "No 'jar' file field found in the upload."}), 400
        # Validate filename extension to reject obvious non-JARs early
        filename = jar_file.filename or ""
        if not filename.lower().endswith(".jar"):
            return jsonify({"error": "Only .jar files are accepted."}), 400
        plugin_name = (request.form.get("plugin_name") or "TestPlugin").strip() or "TestPlugin"
        target_api  = _normalize_target_api(request.form.get("target_api", "26.1.x"))
        jar_bytes   = jar_file.read()
        is_external = True
    elif "json" in ct:
        data = request.get_json(silent=True) or {}
        code = data.get("code", "").strip()
        if not code:
            return jsonify({"error": "'code' is required when sending JSON."}), 400
        plugin_name = (data.get("plugin_name") or "TestPlugin").strip() or "TestPlugin"
        target_api  = _normalize_target_api(str(data.get("target_api", "26.1.x")))
        paper_profile = _paper_profile_for_target_api(target_api)
        try:
            jar_bytes = build_jar(code, plugin_name, paper_profile=paper_profile)
        except RuntimeError as exc:
            return jsonify({"error": f"JAR build failed before testing: {exc}"}), 422
        except Exception:
            traceback.print_exc()
            return jsonify({"error": "JAR build failed (internal error)."}), 500
    else:
        return jsonify({
            "error": "Send multipart/form-data with a 'jar' field, or JSON with 'code'."
        }), 400

    if not jar_bytes:
        return jsonify({"error": "JAR file is empty."}), 400

    # ── Consume one test credit ─────────────────────────────────────────────
    increment_runtime_test(user_id)

    # ── Stream SSE ───────────────────────────────────────────────────────────
    def _generate():
        try:
            from inference.server_test import run_plugin_test
            from validation.jar_scan import scan_jar, ScanResult

            # Phase 0: security scan (always, but especially important for external JARs)
            yield f"data: {json.dumps({'type':'phase','percent':4,'step':'Scanning JAR for security issues\u2026','thinking':'Inspecting class bytecode for dangerous patterns.'})}\n\n"

            try:
                scan: ScanResult = scan_jar(jar_bytes)
            except ValueError as ve:
                yield f"data: {json.dumps({'type':'error','message':str(ve)})}\n\n"
                return

            if scan.blocked:
                # Suspend user's testing and reject
                suspend_runtime_test(user_id)
                detail = "; ".join(scan.findings[:5])
                yield f"data: {json.dumps({'type':'error','message':f'JAR blocked by security scan: {detail}. Your runtime testing access has been suspended for 24 hours. Contact support if you believe this is a mistake.','security_violation':True})}\n\n"
                return

            if scan.risk_level == "suspicious":
                # Log and warn but allow execution
                app.logger.warning(
                    "RT test suspicious JAR: user=%s findings=%r", user_id, scan.findings
                )
                warn_msg = "Suspicious patterns detected in JAR (allowed but logged): " + "; ".join(scan.findings[:3])
                yield f"data: {json.dumps({'type':'phase','percent':8,'step':'Security scan complete \u2014 suspicious patterns noted','thinking':warn_msg})}\n\n"
            else:
                yield f"data: {json.dumps({'type':'phase','percent':8,'step':'Security scan passed','thinking':f'{scan.class_count} classes scanned, no dangerous patterns found.'})}\n\n"

            for event in run_plugin_test(
                jar_bytes,
                plugin_name,
                runtime_profile=_paper_profile_for_target_api(target_api),
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            traceback.print_exc()
            err = {"type": "error", "message": f"Unexpected error: {exc}"}
            yield f"data: {json.dumps(err)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@app.route("/api/test-plugin/usage", methods=["GET"])
def test_plugin_usage():
    """Return the current user's runtime test quota."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401
    usage = get_user_runtime_test_usage(int(user["id"]))
    return jsonify(usage)


# --------------------------------------------------------------------------- #
# Public Stats                                                                 #
# --------------------------------------------------------------------------- #

@app.route("/api/public/stats")
def public_stats():
    """Non-sensitive public stats for the landing page."""
    try:
        with _conn() as con:
            total_gens  = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            success     = con.execute("SELECT COUNT(*) FROM requests WHERE success = 1").fetchone()[0]
            gallery_ct  = con.execute("SELECT COUNT(*) FROM gallery WHERE public = 1").fetchone()[0]
            avg_row     = con.execute("SELECT AVG(elapsed) FROM requests WHERE elapsed IS NOT NULL AND elapsed > 0").fetchone()
            avg_el      = avg_row[0] if avg_row else None
        rate = round(success / total_gens * 100, 1) if total_gens else 0
        return jsonify({
            "total_generations": total_gens,
            "success_rate":      rate,
            "avg_elapsed_s":     round(avg_el, 1) if avg_el else 0,
            "gallery_plugins":   gallery_ct,
        })
    except Exception:
        return jsonify({"total_generations": 0, "success_rate": 0, "avg_elapsed_s": 0, "gallery_plugins": 0})


# --------------------------------------------------------------------------- #
# Community Gallery                                                            #
# --------------------------------------------------------------------------- #

@app.route("/api/gallery", methods=["GET"])
def gallery_list():
    """
    Return paginated list of public gallery entries.

    Query params: limit (default 20), offset (default 0), tier (optional filter)
    Response: { "entries": [...], "total": N, "limit": N, "offset": N }
    """
    limit      = min(int(request.args.get("limit", 20)), 100)
    offset     = max(int(request.args.get("offset", 0)), 0)
    tier_f     = request.args.get("tier") or None
    tag_f      = request.args.get("tag") or None
    category_f = request.args.get("category") or None

    entries, total = get_gallery(
        limit=limit, offset=offset,
        tier_filter=tier_f, tag_filter=tag_f,
        category_filter=category_f,
    )
    return jsonify({"entries": entries, "total": total, "limit": limit, "offset": offset})


@app.route("/api/gallery/submit", methods=["POST"])
def gallery_submit():
    """
    Submit a generated plugin to the community gallery.

    Request body:
    {
        "code":        "...full plugin markdown...",
        "instruction": "Create a plugin that...",
        "plugin_name": "MyPlugin",
        "public":      true   // ignored for free tier (always true)
    }

    Response: { "id": N, "public": true/false }
    """
    data = request.get_json(silent=True) or {}
    code        = data.get("code", "").strip()
    instruction = data.get("instruction", "").strip()
    plugin_name = data.get("plugin_name", "Unnamed Plugin").strip()
    public_req  = bool(data.get("public", True))

    if not code:
        return jsonify({"error": "'code' field is required"}), 400
    if not instruction:
        return jsonify({"error": "'instruction' field is required"}), 400

    tier = get_tier()
    ip   = request.remote_addr or "0.0.0.0"
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]

    try:
        entry_id = submit_gallery(
            instruction=instruction,
            plugin_name=plugin_name,
            code=code,
            tier=tier,
            public=public_req,
            ip_hash=ip_hash,
        )
    except Exception as e:
        return jsonify({"error": f"Gallery submission failed: {e}"}), 500

    # Free tier is always forced public
    is_public = True if tier == "free" else public_req
    if is_public:
        try:
            _discord_gallery_webhook(plugin_name, entry_id)
        except Exception:
            pass
    return jsonify({"id": entry_id, "public": is_public}), 201


_GALLERY_UPLOADS = Path(__file__).parent.parent / "data" / "gallery_uploads"
_MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"
_ALLOWED_UPLOAD_EXTS = {".jar", ".zip", ".sk"}
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
_VALID_TAGS = {"paper", "purpur", "bukkit", "folia", "spigot", "velocity", "bungeecord", "waterfall"}


def _has_plugin_yml(path: str) -> bool:
    """Return True if the ZIP/JAR contains plugin.yml or paper-plugin.yml at any depth."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = {n.split("/")[-1].lower() for n in zf.namelist()}
            return bool(names & {"plugin.yml", "paper-plugin.yml", "bungee.yml"})
    except Exception:
        return False


@app.route("/api/gallery/upload", methods=["POST"])
@limiter.limit("10 per hour", key_func=get_remote_address)
@_user_required
def gallery_community_upload():
    """
    Upload a plugin (JAR/ZIP) or Skript script (.sk) to the community gallery.
    Requires a valid user auth token (Bearer or X-User-Token).
    Accepts multipart/form-data:
      - name             : display name (required)
      - description      : short description (required)
      - plugin_category  : 'plugin' (default) or 'skript'
      - tags             : comma-separated platform tags, e.g. 'paper,folia' (optional)
      - github_url       : GitHub link (optional)
      - jar_file         : .jar/.zip for plugins, .sk for Skript (required)
    """
    user: dict = request.stacknest_user  # set by @_user_required

    name             = request.form.get("name", "").strip()
    description      = request.form.get("description", "").strip()
    github_url       = request.form.get("github_url", "").strip()
    plugin_category  = request.form.get("plugin_category", "plugin").strip().lower()
    if plugin_category not in ("plugin", "skript"):
        plugin_category = "plugin"

    # Parse + sanitise tags
    raw_tags   = request.form.get("tags", "").lower()
    parsed_tags = ",".join(
        t for t in (t.strip() for t in raw_tags.split(",")) if t in _VALID_TAGS
    )

    if not name:
        return jsonify({"error": "'name' is required"}), 400
    if not description:
        return jsonify({"error": "'description' is required"}), 400
    if len(name) > 80:
        return jsonify({"error": "Name too long (max 80 chars)"}), 400

    ip      = request.remote_addr or "0.0.0.0"
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]

    # File is REQUIRED
    file = request.files.get("jar_file")
    if not file or not file.filename:
        return jsonify({"error": "A file is required (.jar/.zip for plugins, .sk for Skript)"}), 400

    ext = Path(file.filename).suffix.lower()
    if plugin_category == "skript":
        if ext != ".sk":
            return jsonify({"error": "Skript scripts must be .sk files"}), 400
    else:
        if ext not in {".jar", ".zip"}:
            return jsonify({"error": "Only .jar or .zip files are accepted for plugins"}), 400

    # Check size before saving
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > _MAX_UPLOAD_BYTES:
        return jsonify({"error": f"File too large (max {_MAX_UPLOAD_BYTES // 1024 // 1024} MB)"}), 400

    _GALLERY_UPLOADS.mkdir(parents=True, exist_ok=True)
    safe_stem = secure_filename(Path(file.filename).stem)[:40] or "script"
    tmp_name  = f"{safe_stem}_{int(time.time())}{ext}"
    dest = _GALLERY_UPLOADS / tmp_name
    file.save(str(dest))

    # Validate: plugins must contain plugin.yml; Skript files are plain text
    if plugin_category == "plugin" and not _has_plugin_yml(str(dest)):
        dest.unlink(missing_ok=True)
        return jsonify({
            "error": "Invalid plugin: archive must contain plugin.yml (or paper-plugin.yml / bungee.yml). "
                     "Please upload a real compiled Minecraft plugin JAR."
        }), 400

    # Author name: use uploader's display_name, fall back to email prefix
    author_name = user.get("display_name") or user.get("email", "Anonymous").split("@")[0]

    try:
        entry_id = submit_gallery_community(
            plugin_name=name,
            description=description,
            author_name=author_name,
            github_url=github_url,
            jar_path=str(dest),
            ip_hash=ip_hash,
            tags=parsed_tags,
            uploader_user_id=user.get("id"),
            plugin_category=plugin_category,
        )
    except Exception as e:
        dest.unlink(missing_ok=True)
        return jsonify({"error": f"Upload failed: {e}"}), 500

    return jsonify({"id": entry_id, "message": "Submitted to gallery!"}), 201


@app.route("/api/gallery/uploads/<path:filename>", methods=["GET"])
def gallery_download(filename: str):
    """Serve an uploaded gallery JAR/ZIP for download."""
    safe = secure_filename(filename)
    if not safe or ".." in safe:
        return jsonify({"error": "Invalid filename"}), 400
    if not (_GALLERY_UPLOADS / safe).exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(str(_GALLERY_UPLOADS), safe, as_attachment=True)


@app.route("/api/migrations/<path:filename>", methods=["GET"])
def migration_download(filename: str):
    """Serve generated migration ZIP files."""
    safe = secure_filename(filename)
    if not safe or ".." in safe:
        return jsonify({"error": "Invalid filename"}), 400
    if not (_MIGRATIONS_DIR / safe).exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(str(_MIGRATIONS_DIR), safe, as_attachment=True)


@app.route("/api/migrate", methods=["POST"])
@limiter.limit("20 per hour", key_func=get_remote_address)
def migrate_plugin_sources():
    """
    Migrate plugin source files to a newer Paper API target.

    Input (either):
      - JSON: {"github_url": "https://github.com/owner/repo", "target_version": "1.21"}
      - multipart/form-data: file field "source_zip" or "zip_file"
    """
    target_version = "1.21"
    source_label = "upload"

    try:
        zip_bytes: bytes
        if request.content_type and "multipart/form-data" in request.content_type.lower():
            up = request.files.get("source_zip") or request.files.get("zip_file")
            if not up or not up.filename:
                return jsonify({"error": "Missing ZIP file in field 'source_zip'"}), 400
            if Path(up.filename).suffix.lower() != ".zip":
                return jsonify({"error": "Only .zip source uploads are supported"}), 400

            up.seek(0, 2)
            size = up.tell()
            up.seek(0)
            if size > MAX_UPLOAD_ZIP_BYTES:
                return jsonify({"error": "ZIP too large"}), 400

            zip_bytes = up.read()
            source_label = "upload"
            form_target = (request.form.get("target_version", "") or "").strip()
            if form_target:
                target_version = form_target
        else:
            data = request.get_json(silent=True) or {}
            github_url = (data.get("github_url", "") or "").strip()
            if not github_url:
                return jsonify({"error": "Provide 'github_url' JSON field or upload a ZIP"}), 400
            target_version = (data.get("target_version", "") or "").strip() or "1.21"
            zip_bytes = fetch_github_archive(github_url)
            source_label = github_url

        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", target_version):
            return jsonify({"error": "Invalid target_version format. Example: '1.21'"}), 400

        files = extract_source_files(zip_bytes)
        outcome = migrate_sources(files, source=source_label, target_version=target_version)

        migrated_zip = build_zip_bytes(outcome.migrated_files)
        _MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        artifact_name = f"migrated_{int(time.time())}_{secrets.token_hex(4)}.zip"
        artifact_path = _MIGRATIONS_DIR / artifact_name
        artifact_path.write_bytes(migrated_zip)

        return jsonify({
            "ok": True,
            "source": outcome.source,
            "source_version": outcome.source_version,
            "target_version": outcome.target_version,
            "files_total": outcome.files_total,
            "files_changed": outcome.files_changed,
            "changed_files": outcome.changed_files,
            "fixes_applied": outcome.fixes_applied,
            "diff": outcome.unified_diff,
            "download_url": f"/api/migrations/{artifact_name}",
        })
    except MigrationError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Migration failed: {e}"}), 500


@app.route("/api/gallery/<int:entry_id>", methods=["GET"])
def gallery_entry(entry_id: int):
    """Return a single gallery entry (includes full code)."""
    entry = get_gallery_entry(entry_id)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    if not entry.get("public"):
        return jsonify({"error": "This entry is private"}), 403
    return jsonify(entry)


@app.route("/api/gallery/<int:entry_id>/like", methods=["POST"])
def gallery_like(entry_id: int):
    """Increment likes for a gallery entry. Returns { 'likes': N }."""
    entry = get_gallery_entry(entry_id)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    new_count, _ = like_gallery(entry_id, get_remote_address())
    return jsonify({"likes": new_count})


# --------------------------------------------------------------------------- #
# Error handlers                                                               #
# --------------------------------------------------------------------------- #

@app.errorhandler(429)
def rate_limit_handler(e):
    try:
        msg = json.loads(str(e.description))
    except Exception:
        msg = {"error": "Rate limit exceeded"}
    return jsonify(msg), 429


@app.errorhandler(413)
def payload_too_large(e):
    return jsonify({"error": f"Payload too large (max {MAX_REQUEST_BYTES} bytes)"}), 413


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error"}), 500


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="StackNest API server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 5000)))
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--debug", action="store_true", default=False)
    args = parser.parse_args()

    print(f"StackNest API  : http://{args.host}:{args.port}")
    print(f"Inference      : {os.getenv('LLAMACPP_URL', 'http://localhost:8080')}")
    print(f"ChromaDB       : {os.getenv('CHROMADB_PATH', 'data/embeddings/chromadb')}")
    print(f"Kimi fallback  : {'enabled' if os.getenv('KIMI_API_KEY') else 'disabled (set KIMI_API_KEY)'}")
    print(f"Routes         : / /app /editor /logs /terms /privacy /admin")
    print(f"API routes     : /api/generate /api/stream /api/validate /api/heal /api/jar")
    print(f"               : /api/gallery /api/gallery/submit /api/gallery/<id>/like")
    print(f"               : /api/kimi/validate /api/logs/analyze")
    print(f"Admin routes   : /admin/login /admin/api/stats /admin/api/logs /admin/api/ips")
    print(f"Admin enabled  : {'YES' if ADMIN_SECRET else 'NO — set ADMIN_SECRET in .env'}")

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
