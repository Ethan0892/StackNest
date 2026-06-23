"""
api/db.py — SQLite request logging for StackNest admin panel.

Records every API request with enough detail for admin review, IP management,
and on-the-fly debugging.  Uses only stdlib sqlite3 — no extra dependencies.

Schema (auto-created on first use):
  requests (id, ts, ip, endpoint, tier, instruction, success, attempts,
            elapsed, compile_ok, yml_ok, errors_json, code_snippet, raw_code)
  ip_notes  (ip, note, banned, bypass_limits, updated_at)
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("STACKNEST_DB", "data/stacknest.db"))
_lock = threading.Lock()   # Serialise writes; reads are fine in parallel

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,               -- unix epoch float
    ip           TEXT    NOT NULL,
    endpoint     TEXT    NOT NULL,               -- e.g. /api/generate
    tier         TEXT    NOT NULL DEFAULT 'free',
    instruction  TEXT,                           -- first 300 chars of user instruction
    success      INTEGER,                        -- 1/0/NULL
    attempts     INTEGER,
    elapsed      REAL,
    compile_ok   INTEGER,
    yml_ok       INTEGER,
    errors_json  TEXT,                           -- JSON array of error strings
    code_snippet TEXT,                           -- first 400 chars of generated code
    raw_code     TEXT                            -- full generated code (may be large)
);

CREATE INDEX IF NOT EXISTS idx_requests_ts  ON requests(ts DESC);
CREATE INDEX IF NOT EXISTS idx_requests_ip  ON requests(ip);
CREATE INDEX IF NOT EXISTS idx_requests_ok  ON requests(success);

CREATE TABLE IF NOT EXISTS ip_notes (
    ip            TEXT PRIMARY KEY,
    note          TEXT,
    banned        INTEGER NOT NULL DEFAULT 0,
    bypass_limits INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS gallery (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    instruction TEXT    NOT NULL,
    plugin_name TEXT    NOT NULL DEFAULT 'Unnamed Plugin',
    code        TEXT    NOT NULL,
    tier        TEXT    NOT NULL DEFAULT 'free',
    public      INTEGER NOT NULL DEFAULT 1,
    likes       INTEGER NOT NULL DEFAULT 0,
    ip_hash     TEXT
);

CREATE INDEX IF NOT EXISTS idx_gallery_ts     ON gallery(ts DESC);
CREATE INDEX IF NOT EXISTS idx_gallery_public ON gallery(public, ts DESC);

CREATE TABLE IF NOT EXISTS gallery_likes (
    entry_id   INTEGER NOT NULL,
    voter_key  TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    PRIMARY KEY (entry_id, voter_key)
);
CREATE INDEX IF NOT EXISTS idx_gallery_likes_entry ON gallery_likes(entry_id);

CREATE TABLE IF NOT EXISTS users (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at             REAL    NOT NULL,
    email                  TEXT    NOT NULL UNIQUE,
    password_hash          TEXT    NOT NULL,
    display_name           TEXT    NOT NULL,
    plan                   TEXT    NOT NULL DEFAULT 'free',
    verified               INTEGER NOT NULL DEFAULT 0,
    verification_token     TEXT,
    verification_token_ts  REAL,
    avatar_color           TEXT    NOT NULL DEFAULT '#5c6fff',
    bio                    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_vtoken ON users(verification_token);

CREATE TABLE IF NOT EXISTS user_projects (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    project_name     TEXT    NOT NULL,
    plugin_type      TEXT    NOT NULL DEFAULT 'full_plugin',
    target_api       TEXT    NOT NULL DEFAULT '1.21',
    features_json    TEXT,
    instruction      TEXT,
    full_instruction TEXT,
    include_tests    INTEGER,
    skip_compile     INTEGER,
    success          INTEGER,
    compile_ok       INTEGER,
    generated_code   TEXT,
    warnings_json    TEXT,
    errors_json      TEXT,
    metadata_json    TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_projects_user_updated
    ON user_projects(user_id, updated_at DESC);
"""


# ---------------------------------------------------------------------------
# Live migration — safely adds columns that may not exist in older DBs
# ---------------------------------------------------------------------------
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN verified               INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN verification_token     TEXT",
    "ALTER TABLE users ADD COLUMN verification_token_ts  REAL",
    "ALTER TABLE users ADD COLUMN avatar_color           TEXT NOT NULL DEFAULT '#5c6fff'",
    "ALTER TABLE users ADD COLUMN bio                    TEXT NOT NULL DEFAULT ''",
    # meta KV store for server-side state (admin access log etc.)
    """CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        ts    REAL NOT NULL
    )""",
    # Gallery community upload columns
    "ALTER TABLE gallery ADD COLUMN source            TEXT    NOT NULL DEFAULT 'stacknest'",
    "ALTER TABLE gallery ADD COLUMN author_name       TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN description       TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN github_url        TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN jar_path          TEXT    NOT NULL DEFAULT ''",
    # Gallery v2: platform tags + uploader identity
    "ALTER TABLE gallery ADD COLUMN tags              TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN uploader_user_id  INTEGER",
    # Stripe subscription columns
    "ALTER TABLE users ADD COLUMN stripe_customer_id      TEXT",
    "ALTER TABLE users ADD COLUMN stripe_subscription_id  TEXT",
    # Per-user generation month tracking
    "ALTER TABLE users ADD COLUMN gens_this_month  INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN gens_reset_at    REAL",
    # Google OAuth
    "ALTER TABLE users ADD COLUMN google_id  TEXT",
    "CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id)",
    # Pay-as-you-go non-expiring credits
    "ALTER TABLE users ADD COLUMN bonus_gens INTEGER NOT NULL DEFAULT 0",
    # Profile picture URL
    "ALTER TABLE users ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''",
    # Gallery Skript category
    "ALTER TABLE gallery ADD COLUMN plugin_category TEXT NOT NULL DEFAULT 'plugin'",
    # Gallery v3: resource icon + download counter
    "ALTER TABLE gallery ADD COLUMN icon_url   TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN downloads  INTEGER NOT NULL DEFAULT 0",
    # Discord account linking
    "ALTER TABLE users ADD COLUMN discord_id       TEXT",
    "ALTER TABLE users ADD COLUMN discord_username TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id)",
    # Gallery v4: rich content (Markdown description, changelog, screenshots, versions)
    "ALTER TABLE gallery ADD COLUMN long_description TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN changelog        TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN screenshots      TEXT    NOT NULL DEFAULT '[]'",
    "ALTER TABLE gallery ADD COLUMN versions         TEXT    NOT NULL DEFAULT '[]'",
    "ALTER TABLE gallery ADD COLUMN latest_version   TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE gallery ADD COLUMN updated_at       REAL",
    # Support tickets
    """CREATE TABLE IF NOT EXISTS support_tickets (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL    NOT NULL,
        user_id    INTEGER,
        email      TEXT    NOT NULL,
        subject    TEXT    NOT NULL,
        message    TEXT    NOT NULL,
        status     TEXT    NOT NULL DEFAULT 'open',
        admin_note TEXT    NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON support_tickets(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_user   ON support_tickets(user_id)",
    # Threaded ticket messages (admin replies / user follow-ups)
    """CREATE TABLE IF NOT EXISTS ticket_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id  INTEGER NOT NULL,
        created_at REAL    NOT NULL,
        sender     TEXT    NOT NULL DEFAULT 'admin',
        body       TEXT    NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ticket_msgs_tid ON ticket_messages(ticket_id, created_at)",
    # Runtime plugin testing quota (Starter+)
    "ALTER TABLE users ADD COLUMN runtime_tests_this_month INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN runtime_tests_reset_at   REAL",
    # Security: suspend runtime testing when a malicious JAR upload is detected
    "ALTER TABLE users ADD COLUMN rt_test_suspended   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN rt_test_suspended_until REAL",
    "ALTER TABLE users ADD COLUMN rt_violation_count  INTEGER NOT NULL DEFAULT 0",
    # Smart assembly tracking
    "ALTER TABLE requests ADD COLUMN sa_used     INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE requests ADD COLUMN sa_features TEXT    NOT NULL DEFAULT ''",
    # Studio API key (hashed — raw key never stored)
    "ALTER TABLE users ADD COLUMN api_key_hash   TEXT",
    "ALTER TABLE users ADD COLUMN api_key_prefix TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_key_hash ON users(api_key_hash)",
]

def _run_migrations(con: sqlite3.Connection) -> None:
    for sql in _MIGRATIONS:
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column already exists — safe to ignore


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------
@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(_SCHEMA)
        _run_migrations(con)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------
def update_request(
    row_id: int,
    *,
    success: Optional[bool] = None,
    attempts: Optional[int] = None,
    elapsed: Optional[float] = None,
    compile_ok: Optional[bool] = None,
    errors: Optional[list] = None,
    code: Optional[str] = None,
) -> None:
    """Patch an existing request record (admin use — e.g. after a forced regen)."""
    fields, params = [], []
    if success is not None:
        fields.append("success = ?"); params.append(int(success))
    if attempts is not None:
        fields.append("attempts = ?"); params.append(attempts)
    if elapsed is not None:
        fields.append("elapsed = ?"); params.append(elapsed)
    if compile_ok is not None:
        fields.append("compile_ok = ?"); params.append(int(compile_ok))
    if errors is not None:
        fields.append("errors_json = ?"); params.append(json.dumps(errors))
    if code is not None:
        fields.append("code_snippet = ?"); params.append(code[:400])
        fields.append("raw_code = ?"   ); params.append(code)
    if not fields:
        return
    params.append(row_id)
    with _lock, _conn() as con:
        con.execute(
            f"UPDATE requests SET {', '.join(fields)} WHERE id = ?",
            params,
        )


def log_request(
    *,
    ip: str,
    endpoint: str,
    tier: str = "free",
    instruction: Optional[str] = None,
    success: Optional[bool] = None,
    attempts: Optional[int] = None,
    elapsed: Optional[float] = None,
    compile_ok: Optional[bool] = None,
    yml_ok: Optional[bool] = None,
    errors: Optional[list] = None,
    code: Optional[str] = None,
    sa_used: bool = False,
    sa_features: Optional[list] = None,
) -> int:
    """Insert one request record.  Returns the new row id."""
    snippet = (code or "")[:400] if code else None
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO requests
               (ts, ip, endpoint, tier, instruction, success, attempts, elapsed,
                compile_ok, yml_ok, errors_json, code_snippet, raw_code,
                sa_used, sa_features)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                ip,
                endpoint,
                tier,
                (instruction or "")[:300] if instruction else None,
                int(success) if success is not None else None,
                attempts,
                elapsed,
                int(compile_ok) if compile_ok is not None else None,
                int(yml_ok) if yml_ok is not None else None,
                json.dumps(errors) if errors is not None else None,
                snippet,
                code,
                int(sa_used),
                json.dumps(sa_features) if sa_features else "",
            ),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Read helpers (admin queries)
# ---------------------------------------------------------------------------

def get_requests(
    limit: int = 50,
    offset: int = 0,
    ip_filter: Optional[str] = None,
    endpoint_filter: Optional[str] = None,
    success_filter: Optional[bool] = None,
    ts_from: Optional[float] = None,
    ts_to: Optional[float] = None,
) -> list[dict]:
    """Return recent requests, newest first."""
    clauses, params = [], []
    if ip_filter:
        clauses.append("ip = ?"); params.append(ip_filter)
    if endpoint_filter:
        if "%" in endpoint_filter:
            clauses.append("endpoint LIKE ?"); params.append(endpoint_filter)
        else:
            clauses.append("endpoint = ?"); params.append(endpoint_filter)
    if success_filter is not None:
        clauses.append("success = ?"); params.append(int(success_filter))
    if ts_from is not None:
        clauses.append("ts >= ?"); params.append(ts_from)
    if ts_to is not None:
        clauses.append("ts <= ?"); params.append(ts_to)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params += [limit, offset]

    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM requests {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_request_by_id(row_id: int) -> Optional[dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM requests WHERE id = ?", (row_id,)).fetchone()
    return dict(row) if row else None


def get_stats() -> dict:
    """Return summary statistics for the admin dashboard."""
    with _conn() as con:
        total       = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        success     = con.execute("SELECT COUNT(*) FROM requests WHERE success = 1").fetchone()[0]
        failures    = con.execute("SELECT COUNT(*) FROM requests WHERE success = 0").fetchone()[0]
        today_ts    = time.time() - 86400
        today_total = con.execute("SELECT COUNT(*) FROM requests WHERE ts > ?", (today_ts,)).fetchone()[0]
        today_ok    = con.execute("SELECT COUNT(*) FROM requests WHERE ts > ? AND success = 1", (today_ts,)).fetchone()[0]
        avg_elapsed = con.execute("SELECT AVG(elapsed) FROM requests WHERE elapsed IS NOT NULL").fetchone()[0]
        top_ips_raw = con.execute(
            "SELECT ip, COUNT(*) AS n FROM requests GROUP BY ip ORDER BY n DESC LIMIT 10"
        ).fetchall()
        banned      = con.execute("SELECT COUNT(*) FROM ip_notes WHERE banned = 1").fetchone()[0]
        bypassed    = con.execute("SELECT COUNT(*) FROM ip_notes WHERE bypass_limits = 1").fetchone()[0]
        recent_errors = con.execute(
            """SELECT id, ts, ip, instruction, errors_json FROM requests
               WHERE success = 0 AND errors_json IS NOT NULL
               ORDER BY ts DESC LIMIT 20"""
        ).fetchall()
        mod_total   = con.execute(
            "SELECT COUNT(*) FROM requests WHERE endpoint = '/api/generate-mod-progress'"
        ).fetchone()[0]
        mod_today   = con.execute(
            "SELECT COUNT(*) FROM requests WHERE endpoint = '/api/generate-mod-progress' AND ts > ?",
            (today_ts,)
        ).fetchone()[0]

    return {
        "total":         total,
        "success":       success,
        "failures":      failures,
        "success_rate":  round(success / total * 100, 1) if total else 0,
        "today_total":   today_total,
        "today_ok":      today_ok,
        "avg_elapsed_s": round(avg_elapsed, 1) if avg_elapsed else 0,
        "top_ips":       [{"ip": r[0], "count": r[1]} for r in top_ips_raw],
        "banned_ips":    banned,
        "bypassed_ips":  bypassed,
        "recent_errors": [dict(r) for r in recent_errors],
        "mod_total":     mod_total,
        "mod_today":     mod_today,
    }


def get_ip_notes(ip: Optional[str] = None) -> list[dict]:
    with _conn() as con:
        if ip:
            rows = con.execute("SELECT * FROM ip_notes WHERE ip = ?", (ip,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM ip_notes ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_ip_note(ip: str, note: str = "", banned: bool = False, bypass: bool = False):
    with _lock, _conn() as con:
        con.execute(
            """INSERT INTO ip_notes (ip, note, banned, bypass_limits, updated_at)
               VALUES (?, ?, ?, ?, unixepoch('now'))
               ON CONFLICT(ip) DO UPDATE SET
                 note=excluded.note, banned=excluded.banned,
                 bypass_limits=excluded.bypass_limits, updated_at=excluded.updated_at""",
            (ip, note, int(banned), int(bypass)),
        )


def delete_ip_note(ip: str):
    with _lock, _conn() as con:
        con.execute("DELETE FROM ip_notes WHERE ip = ?", (ip,))


def is_banned(ip: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT banned FROM ip_notes WHERE ip = ?", (ip,)
        ).fetchone()
    return bool(row and row[0])


def is_bypassed(ip: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT bypass_limits FROM ip_notes WHERE ip = ?", (ip,)
        ).fetchone()
    return bool(row and row[0])


def clear_old_logs(days: int = 30):
    """Delete logs older than `days` days.  Call from a maintenance task."""
    cutoff = time.time() - days * 86400
    with _lock, _conn() as con:
        con.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))


# ---------------------------------------------------------------------------
# Gallery helpers
# ---------------------------------------------------------------------------

def submit_gallery(
    *,
    instruction: str,
    plugin_name: str,
    code: str,
    tier: str = "free",
    public: bool = True,
    ip_hash: Optional[str] = None,
) -> int:
    """Insert a gallery entry. Free tier always forces public=True."""
    if tier == "free":
        public = True
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO gallery (ts, instruction, plugin_name, code, tier, public, ip_hash, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'stacknest')""",
            (
                time.time(),
                instruction[:500],
                (plugin_name or "Unnamed Plugin")[:80],
                code,
                tier,
                int(public),
                ip_hash,
            ),
        )
        return cur.lastrowid


def submit_gallery_community(
    *,
    plugin_name: str,
    description: str,
    author_name: str,
    github_url: str = "",
    jar_path: str = "",
    icon_url: str = "",
    ip_hash: Optional[str] = None,
    tags: str = "",
    uploader_user_id: Optional[int] = None,
    plugin_category: str = "plugin",
) -> int:
    """Insert a community-uploaded plugin/script into the gallery."""
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO gallery
               (ts, instruction, plugin_name, code, tier, public, ip_hash,
                source, author_name, description, github_url, jar_path,
                tags, uploader_user_id, plugin_category, icon_url)
               VALUES (?, ?, ?, '', 'community', 1, ?, 'community', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                description[:500],       # instruction field reused as description
                (plugin_name or "Unnamed Plugin")[:80],
                ip_hash,
                (author_name or "Anonymous")[:60],
                description[:500],
                github_url[:200],
                jar_path,
                tags[:200],
                uploader_user_id,
                plugin_category if plugin_category in ("plugin", "skript") else "plugin",
                icon_url,
            ),
        )
        return cur.lastrowid


def get_gallery(
    limit: int = 20,
    offset: int = 0,
    tier_filter: Optional[str] = None,
    tag_filter: Optional[str] = None,
    category_filter: Optional[str] = None,
    sort_by: str = "newest",
    search_query: str = "",
) -> tuple[list[dict], int]:
    """Return public gallery entries. Returns (rows, total_count).
    sort_by: 'newest' (default) | 'likes' | 'downloads'
    """
    clauses = ["public = 1"]
    params: list = []
    if tier_filter:
        clauses.append("tier = ?")
        params.append(tier_filter)
    if tag_filter:
        clauses.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag_filter.lower()},%")
    if category_filter and category_filter in ("plugin", "skript"):
        clauses.append("plugin_category = ?")
        params.append(category_filter)
    if search_query:
        clauses.append("(LOWER(plugin_name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(instruction) LIKE ?)")
        q = f"%{search_query.lower()[:100]}%"
        params.extend([q, q, q])
    where = "WHERE " + " AND ".join(clauses)
    order = {
        "likes":     "likes DESC, ts DESC",
        "downloads": "downloads DESC, ts DESC",
    }.get(sort_by, "ts DESC")
    with _conn() as con:
        total = con.execute(f"SELECT COUNT(*) FROM gallery {where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT id, ts, instruction, plugin_name, tier, likes, downloads,"
            f" source, author_name, description, github_url, jar_path, tags,"
            f" plugin_category, icon_url FROM gallery "
            f"{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def increment_gallery_downloads(entry_id: int) -> None:
    """Increment download counter for a gallery entry."""
    with _lock, _conn() as con:
        con.execute("UPDATE gallery SET downloads = downloads + 1 WHERE id = ?", (entry_id,))


def get_gallery_entry(entry_id: int) -> Optional[dict]:
    """Return a single gallery entry (includes full code)."""
    with _conn() as con:
        row = con.execute("SELECT * FROM gallery WHERE id = ?", (entry_id,)).fetchone()
    return dict(row) if row else None


def like_gallery(entry_id: int, voter_key: str):
    """Like a gallery entry (1 per voter_key/IP). Returns (new_count, did_like)."""
    with _lock, _conn() as con:
        try:
            con.execute(
                "INSERT INTO gallery_likes (entry_id, voter_key, ts) VALUES (?, ?, ?)",
                (entry_id, voter_key, time.time())
            )
        except sqlite3.IntegrityError:
            row = con.execute("SELECT likes FROM gallery WHERE id = ?", (entry_id,)).fetchone()
            return (row[0] if row else 0), False
        con.execute("UPDATE gallery SET likes = likes + 1 WHERE id = ?", (entry_id,))
        row = con.execute("SELECT likes FROM gallery WHERE id = ?", (entry_id,)).fetchone()
        return (row[0] if row else 0), True

def update_gallery_entry(entry_id: int, uploader_user_id: int, **fields) -> Optional[dict]:
    """Owner-only update of a gallery entry. Returns updated row or None."""
    ALLOWED = {
        "plugin_name", "description", "long_description", "changelog",
        "screenshots", "versions", "latest_version", "github_url", "tags",
        "icon_url", "jar_path",
    }
    sets, params = [], []
    for k, v in fields.items():
        if k in ALLOWED:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return None
    sets.append("updated_at = ?")
    params.append(time.time())
    params.extend([entry_id, uploader_user_id])
    with _lock, _conn() as con:
        con.execute(
            f"UPDATE gallery SET {', '.join(sets)} WHERE id = ? AND uploader_user_id = ?",
            params,
        )
        row = con.execute("SELECT * FROM gallery WHERE id = ?", (entry_id,)).fetchone()
    return dict(row) if row else None


def create_user(*, email: str, password_hash: str, display_name: str) -> int:
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO users (created_at, email, password_hash, display_name, plan)
               VALUES (?, ?, ?, ?, 'free')""",
            (time.time(), email.strip().lower(), password_hash, display_name.strip()),
        )
        return cur.lastrowid


def create_oauth_user(*, email: str, display_name: str, google_id: str) -> int:
    """Create a Google-authenticated user (no password, auto-verified)."""
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO users
               (created_at, email, password_hash, display_name, plan, verified, google_id)
               VALUES (?, ?, '', ?, 'free', 1, ?)""",
            (time.time(), email.strip().lower(), display_name.strip()[:40], google_id),
        )
        return cur.lastrowid


def get_user_by_google_id(google_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE google_id = ?", (google_id,)
        ).fetchone()
    return dict(row) if row else None


def set_user_google_id(user_id: int, google_id: str) -> None:
    """Link an existing email/password account to a Google ID."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET google_id = ?, verified = 1 WHERE id = ?",
            (google_id, user_id),
        )


def get_user_by_email(email: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_verification_token(token: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE verification_token = ?", (token,)
        ).fetchone()
    return dict(row) if row else None


def set_verification_token(user_id: int, token: str) -> None:
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET verification_token = ?, verification_token_ts = ? WHERE id = ?",
            (token, time.time(), user_id),
        )


def set_user_verified(user_id: int) -> None:
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET verified = 1, verification_token = NULL, verification_token_ts = NULL WHERE id = ?",
            (user_id,),
        )


def update_user_profile(
    user_id: int,
    *,
    display_name: Optional[str] = None,
    avatar_color: Optional[str] = None,
    avatar_url: Optional[str] = None,
    bio: Optional[str] = None,
) -> Optional[dict]:
    sets, params = [], []
    if display_name is not None:
        sets.append("display_name = ?"); params.append(display_name[:80])
    if avatar_color is not None:
        # Simple colour validation — only allow #rrggbb
        import re
        if re.match(r"^#[0-9a-fA-F]{6}$", avatar_color):
            sets.append("avatar_color = ?"); params.append(avatar_color)
    if avatar_url is not None:
        sets.append("avatar_url = ?"); params.append(avatar_url[:300])
    if bio is not None:
        sets.append("bio = ?"); params.append(bio[:280])
    if not sets:
        return get_user_by_id(user_id)
    params.append(user_id)
    with _lock, _conn() as con:
        con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
    return get_user_by_id(user_id)


def update_user_password(user_id: int, new_password_hash: str) -> None:
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_password_hash, user_id),
        )


def set_user_plan(user_id: int, plan: str) -> None:
    """Upgrade or downgrade a user's plan ('free' or 'pro')."""
    with _lock, _conn() as con:
        con.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))
        # Auto-revoke API key whenever plan drops below studio
        if plan != "studio":
            con.execute(
                "UPDATE users SET api_key_hash = NULL, api_key_prefix = NULL WHERE id = ?",
                (user_id,)
            )


def set_user_stripe_ids(
    user_id: int,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
) -> None:
    """Store Stripe customer/subscription IDs against a user."""
    sets, params = [], []
    if stripe_customer_id is not None:
        sets.append("stripe_customer_id = ?")
        params.append(stripe_customer_id)
    if stripe_subscription_id is not None:
        sets.append("stripe_subscription_id = ?")
        params.append(stripe_subscription_id)
    if not sets:
        return
    params.append(user_id)
    with _lock, _conn() as con:
        con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)


def get_user_by_stripe_customer_id(customer_id: str) -> Optional[dict]:
    """Look up a user by their Stripe customer ID (used in webhooks)."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# API key helpers (Studio plan only — key is hashed before storage)
# ---------------------------------------------------------------------------

def get_user_by_api_key(raw_key: str) -> Optional[dict]:
    """Look up a user by their raw API key (hashes internally — raw key never hits DB)."""
    h = hashlib.sha256(raw_key.encode()).hexdigest()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE api_key_hash = ?", (h,)
        ).fetchone()
    return dict(row) if row else None


def set_user_api_key(user_id: int, raw_key: str) -> None:
    """Store the SHA-256 hash and display prefix of a newly generated API key."""
    h      = hashlib.sha256(raw_key.encode()).hexdigest()
    prefix = raw_key[:12]   # "sn_" + first 9 hex chars — safe to display
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET api_key_hash = ?, api_key_prefix = ? WHERE id = ?",
            (h, prefix, user_id),
        )


def clear_user_api_key(user_id: int) -> None:
    """Revoke a user's API key entirely."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET api_key_hash = NULL, api_key_prefix = NULL WHERE id = ?",
            (user_id,)
        )


def list_users(limit: int = 100, offset: int = 0, search: str = "") -> list[dict]:
    """Return a paginated list of all registered users (admin use)."""
    with _conn() as con:
        if search:
            rows = con.execute(
                """
                SELECT u.id, u.created_at, u.email, u.display_name, u.plan,
                       u.verified, u.avatar_color, u.bio,
                       u.stripe_customer_id, u.stripe_subscription_id,
                       COUNT(p.id) AS project_count
                FROM users u
                LEFT JOIN user_projects p ON p.user_id = u.id
                WHERE u.email LIKE ?
                GROUP BY u.id
                ORDER BY u.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (f"%{search}%", max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT u.id, u.created_at, u.email, u.display_name, u.plan,
                       u.verified, u.avatar_color, u.bio,
                       u.stripe_customer_id, u.stripe_subscription_id,
                       COUNT(p.id) AS project_count
                FROM users u
                LEFT JOIN user_projects p ON p.user_id = u.id
                GROUP BY u.id
                ORDER BY u.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
    return [dict(r) for r in rows]


def count_users(search: str = "") -> int:
    """Total number of registered user accounts."""
    with _conn() as con:
        if search:
            row = con.execute(
                "SELECT COUNT(*) FROM users WHERE email LIKE ?",
                (f"%{search}%",),
            ).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] if row else 0


def save_user_project(
    *,
    user_id: int,
    project_name: str,
    plugin_type: str,
    target_api: str,
    features: Optional[list] = None,
    instruction: Optional[str] = None,
    full_instruction: Optional[str] = None,
    include_tests: Optional[bool] = None,
    skip_compile: Optional[bool] = None,
    success: Optional[bool] = None,
    compile_ok: Optional[bool] = None,
    generated_code: Optional[str] = None,
    warnings: Optional[list] = None,
    errors: Optional[list] = None,
    metadata: Optional[dict] = None,
) -> int:
    now = time.time()
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO user_projects
               (user_id, created_at, updated_at, project_name, plugin_type, target_api,
                features_json, instruction, full_instruction, include_tests, skip_compile,
                success, compile_ok, generated_code, warnings_json, errors_json, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                now,
                now,
                (project_name or "Untitled Plugin")[:120],
                (plugin_type or "full_plugin")[:32],
                (target_api or "1.21")[:32],
                json.dumps(features or []),
                (instruction or "")[:1200],
                (full_instruction or "")[:3000],
                int(include_tests) if include_tests is not None else None,
                int(skip_compile) if skip_compile is not None else None,
                int(success) if success is not None else None,
                int(compile_ok) if compile_ok is not None else None,
                generated_code,
                json.dumps(warnings or []),
                json.dumps(errors or []),
                json.dumps(metadata or {}),
            ),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Meta KV store
# ---------------------------------------------------------------------------

def set_meta(key: str, value: str) -> None:
    """Upsert a key-value pair in the meta table."""
    with _lock, _conn() as con:
        con.execute(
            "INSERT INTO meta (key, value, ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key, value, time.time()),
        )


def get_meta(key: str) -> Optional[dict]:
    """Return {value, ts} for a meta key, or None if not set."""
    with _conn() as con:
        row = con.execute("SELECT value, ts FROM meta WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def list_user_projects(user_id: int, limit: int = 25) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT id, user_id, created_at, updated_at, project_name, plugin_type, target_api,
                      features_json, instruction, include_tests, skip_compile,
                      success, compile_ok, warnings_json, errors_json
               FROM user_projects
               WHERE user_id = ?
               ORDER BY updated_at DESC
               LIMIT ?""",
            (user_id, min(max(limit, 1), 100)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_project(user_id: int, project_id: int) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM user_projects WHERE user_id = ? AND id = ?",
            (user_id, project_id),
        ).fetchone()
    return dict(row) if row else None


def delete_user_project(user_id: int, project_id: int) -> bool:
    with _lock, _conn() as con:
        cur = con.execute(
            "DELETE FROM user_projects WHERE user_id = ? AND id = ?",
            (user_id, project_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Per-user generation tracking
# ---------------------------------------------------------------------------

_PLAN_LIMITS = {"free": 2, "starter": 15, "pro": 100, "studio": 300}
_SECONDS_PER_MONTH = 30 * 86400  # 30-day rolling window


def get_user_usage(user_id: int) -> dict:
    """Return gens_used, gens_limit, bonus_gens, and days_until_reset for a user."""
    with _conn() as con:
        row = con.execute(
            "SELECT plan, gens_this_month, gens_reset_at, bonus_gens FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"gens_used": 0, "gens_limit": 3, "bonus_gens": 0, "days_until_reset": 30}
    plan = row["plan"] or "free"
    limit = _PLAN_LIMITS.get(plan, 3)
    reset_at = row["gens_reset_at"]
    gens = int(row["gens_this_month"] or 0)
    bonus = int(row["bonus_gens"] or 0)
    now = time.time()
    # If we have a reset timestamp, count days remaining
    if reset_at:
        days_left = max(0, round((reset_at - now) / 86400, 1))
    else:
        days_left = 30
    # Auto-reset if the month window has passed
    if reset_at and now >= reset_at:
        gens = 0
    return {"gens_used": gens, "gens_limit": limit, "bonus_gens": bonus, "days_until_reset": days_left, "plan": plan}


def increment_user_generation(user_id: int, amount: int = 1) -> dict:
    """
    Atomically charge `amount` generation credits for the user.
    - 1-3 attempts = 1 credit, 4-6 = 2 credits, 7+ = 3 credits (caller computes amount).
    - Fills from monthly quota first, then from bonus_gens for any remainder.
    Resets counter if the 30-day window has elapsed.
    Returns the new usage dict.
    """
    amount = max(1, int(amount))
    now = time.time()
    with _lock, _conn() as con:
        row = con.execute(
            "SELECT plan, gens_this_month, gens_reset_at, bonus_gens FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return {"gens_used": amount, "gens_limit": 3, "bonus_gens": 0}
        plan = row["plan"] or "free"
        limit = _PLAN_LIMITS.get(plan, 3)
        reset_at = row["gens_reset_at"]
        gens = int(row["gens_this_month"] or 0)
        bonus = int(row["bonus_gens"] or 0)
        # Reset window if expired or never set
        if not reset_at or now >= reset_at:
            gens = 0
            reset_at = now + _SECONDS_PER_MONTH
        # Consume from monthly quota first, then bonus_gens for the remainder
        remaining_quota = max(0, limit - gens)
        from_quota = min(amount, remaining_quota)
        from_bonus = min(bonus, amount - from_quota)
        gens += from_quota
        bonus -= from_bonus
        con.execute(
            "UPDATE users SET gens_this_month=?, gens_reset_at=?, bonus_gens=? WHERE id=?",
            (gens, reset_at, bonus, user_id),
        )
    days_left = max(0, round((reset_at - now) / 86400, 1))
    return {"gens_used": gens, "gens_limit": limit, "bonus_gens": bonus, "days_until_reset": days_left, "plan": plan}


def check_user_generation_limit(user_id: int) -> tuple[bool, dict]:
    """
    Check whether the user has remaining generations this month.
    Allowed if monthly quota not yet reached OR bonus_gens > 0.
    Returns (allowed: bool, usage: dict).
    """
    usage = get_user_usage(user_id)
    allowed = usage["gens_used"] < usage["gens_limit"] or usage.get("bonus_gens", 0) > 0
    return allowed, usage


def add_user_bonus_gens(user_id: int, amount: int) -> None:
    """Add non-expiring pay-as-you-go generation credits to a user."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET bonus_gens = COALESCE(bonus_gens, 0) + ? WHERE id=?",
            (amount, user_id),
        )


# ---------------------------------------------------------------------------
# Per-user runtime plugin test quota  (Starter = 3/mo, Pro = 10, Studio = 30)
# ---------------------------------------------------------------------------

_RT_TEST_LIMITS: dict[str, int] = {
    "free":    0,
    "starter": 3,
    "pro":     10,
    "studio":  30,
}


def get_user_runtime_test_usage(user_id: int) -> dict:
    """Return runtime test usage info for this user (rolling 30-day window)."""
    with _conn() as con:
        row = con.execute(
            "SELECT plan, runtime_tests_this_month, runtime_tests_reset_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"tests_used": 0, "tests_limit": 0, "days_until_reset": 30, "plan": "free"}
    plan     = row["plan"] or "free"
    limit    = _RT_TEST_LIMITS.get(plan, 0)
    reset_at = row["runtime_tests_reset_at"]
    used     = int(row["runtime_tests_this_month"] or 0)
    now      = time.time()
    if reset_at and now >= reset_at:
        used = 0
    days_left = max(0, round((reset_at - now) / 86400, 1)) if reset_at else 30
    return {
        "tests_used":      used,
        "tests_limit":     limit,
        "days_until_reset": days_left,
        "plan":            plan,
    }


def check_runtime_test_limit(user_id: int) -> tuple[bool, dict]:
    """Return (allowed, usage_dict). Allowed if user has remaining tests this month."""
    usage   = get_user_runtime_test_usage(user_id)
    allowed = usage["tests_limit"] > 0 and usage["tests_used"] < usage["tests_limit"]
    return allowed, usage


def increment_runtime_test(user_id: int) -> dict:
    """
    Atomically consume one runtime test credit for this user.
    Resets the counter if the 30-day window has elapsed.
    Returns the updated usage dict.
    """
    now = time.time()
    with _lock, _conn() as con:
        row = con.execute(
            "SELECT plan, runtime_tests_this_month, runtime_tests_reset_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return {"tests_used": 1, "tests_limit": 0, "days_until_reset": 30, "plan": "free"}
        plan     = row["plan"] or "free"
        limit    = _RT_TEST_LIMITS.get(plan, 0)
        reset_at = row["runtime_tests_reset_at"]
        used     = int(row["runtime_tests_this_month"] or 0)
        # Reset window if expired or never initialised
        if not reset_at or now >= reset_at:
            used     = 0
            reset_at = now + _SECONDS_PER_MONTH
        used += 1
        con.execute(
            "UPDATE users SET runtime_tests_this_month=?, runtime_tests_reset_at=? WHERE id=?",
            (used, reset_at, user_id),
        )
    days_left = max(0, round((reset_at - now) / 86400, 1))
    return {
        "tests_used":      used,
        "tests_limit":     limit,
        "days_until_reset": days_left,
        "plan":            plan,
    }


def is_rt_test_suspended(user_id: int) -> bool:
    """Return True while this user's runtime-testing suspension is still active."""
    with _conn() as con:
        row = con.execute(
            "SELECT rt_test_suspended, rt_test_suspended_until FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row or not row["rt_test_suspended"]:
        return False

    suspended_until = row["rt_test_suspended_until"]
    if suspended_until and suspended_until <= time.time():
        unsuspend_runtime_test(user_id)
        return False
    return True


def suspend_runtime_test(user_id: int, *, hours: int = 24) -> None:
    """
    Suspend a user's runtime testing access after a malicious JAR upload.
    Also increments the violation counter for admin review.
    """
    suspended_until = time.time() + max(hours, 1) * 3600
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET rt_test_suspended=1, rt_test_suspended_until=?, "
            "rt_violation_count=COALESCE(rt_violation_count,0)+1 "
            "WHERE id=?",
            (suspended_until, user_id),
        )


def unsuspend_runtime_test(user_id: int) -> None:
    """Reinstate runtime testing access (admin action)."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET rt_test_suspended=0, rt_test_suspended_until=NULL WHERE id=?",
            (user_id,),
        )


# ---------------------------------------------------------------------------
# Discord account linking
# ---------------------------------------------------------------------------

def set_user_discord(user_id: int, discord_id: str, discord_username: str) -> None:
    """Link a Discord account to a StackNest user."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET discord_id=?, discord_username=? WHERE id=?",
            (discord_id, discord_username, user_id),
        )


def get_user_by_discord_id(discord_id: str) -> Optional[dict]:
    """Fetch a user row by their linked Discord ID."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE discord_id=?", (discord_id,)
        ).fetchone()
        return dict(row) if row else None


def unlink_user_discord(user_id: int) -> None:
    """Remove the Discord link from a user."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE users SET discord_id=NULL, discord_username='' WHERE id=?",
            (user_id,),
        )


def get_daily_chart_data(days: int = 14) -> dict:
    """Return daily successful generation counts and new signup counts for the last N days."""
    import datetime as _dt
    cutoff = time.time() - days * 86400
    with _conn() as con:
        gen_rows = con.execute(
            """
            SELECT strftime('%Y-%m-%d', ts, 'unixepoch') AS day, COUNT(*) AS cnt
            FROM requests WHERE ts > ? AND success = 1
            GROUP BY day ORDER BY day""",
            (cutoff,),
        ).fetchall()
        signup_rows = con.execute(
            """
            SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') AS day, COUNT(*) AS cnt
            FROM users WHERE created_at > ?
            GROUP BY day ORDER BY day""",
            (cutoff,),
        ).fetchall()
        fail_rows = con.execute(
            """
            SELECT strftime('%Y-%m-%d', ts, 'unixepoch') AS day, COUNT(*) AS cnt
            FROM requests WHERE ts > ? AND success = 0
            GROUP BY day ORDER BY day""",
            (cutoff,),
        ).fetchall()
    today      = _dt.datetime.utcnow().date()
    date_range = [(today - _dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    gen_map    = {r["day"]: r["cnt"] for r in gen_rows}
    signup_map = {r["day"]: r["cnt"] for r in signup_rows}
    fail_map   = {r["day"]: r["cnt"] for r in fail_rows}
    return {
        "labels":      date_range,
        "generations": [gen_map.get(d, 0)   for d in date_range],
        "signups":     [signup_map.get(d, 0) for d in date_range],
        "failures":    [fail_map.get(d, 0)   for d in date_range],
    }


# ---------------------------------------------------------------------------
# Support tickets
# ---------------------------------------------------------------------------

def create_ticket(email: str, subject: str, message: str,
                  user_id: Optional[int] = None) -> int:
    """Create a support ticket and return its ID."""
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO support_tickets (created_at, user_id, email, subject, message)
               VALUES (?, ?, ?, ?, ?)""",
            (time.time(), user_id, email[:254], subject[:200], message[:4000]),
        )
        return cur.lastrowid


def get_tickets(status: Optional[str] = None, limit: int = 50,
                offset: int = 0) -> list[dict]:
    """List support tickets, optionally filtered by status."""
    with _conn() as con:
        if status:
            rows = con.execute(
                """SELECT * FROM support_tickets WHERE status=?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT * FROM support_tickets
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


def get_ticket(ticket_id: int) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM support_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    return dict(row) if row else None


def update_ticket_status(ticket_id: int, status: str, admin_note: str = "") -> None:
    with _lock, _conn() as con:
        con.execute(
            "UPDATE support_tickets SET status=?, admin_note=? WHERE id=?",
            (status, admin_note, ticket_id),
        )


def add_ticket_message(ticket_id: int, body: str, sender: str = "admin") -> int:
    """Append a message to a ticket thread. Returns the new message id."""
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO ticket_messages (ticket_id, created_at, sender, body)
               VALUES (?, ?, ?, ?)""",
            (ticket_id, time.time(), sender[:20], body[:4000]),
        )
        return cur.lastrowid


def get_ticket_messages(ticket_id: int) -> list[dict]:
    """Return all messages for a ticket ordered chronologically."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC",
            (ticket_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Discord + compile-rate stats for admin dashboard
# ---------------------------------------------------------------------------

def get_discord_stats() -> dict:
    """Discord linking stats and compile success rate for the admin dashboard."""
    with _conn() as con:
        linked = con.execute(
            "SELECT COUNT(*) FROM users WHERE discord_id IS NOT NULL"
        ).fetchone()[0]
        by_plan_rows = con.execute(
            """SELECT plan, COUNT(*) AS cnt FROM users
               WHERE discord_id IS NOT NULL GROUP BY plan"""
        ).fetchall()
        total_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        compile_total = con.execute(
            "SELECT COUNT(*) FROM requests WHERE compile_ok IS NOT NULL"
        ).fetchone()[0]
        compile_ok = con.execute(
            "SELECT COUNT(*) FROM requests WHERE compile_ok = 1"
        ).fetchone()[0]
        open_tickets = con.execute(
            "SELECT COUNT(*) FROM support_tickets WHERE status='open'"
        ).fetchone()[0]
    return {
        "linked_count":  linked,
        "total_users":   total_users,
        "link_pct":      round(linked / total_users * 100, 1) if total_users else 0,
        "by_plan":       {r["plan"]: r["cnt"] for r in by_plan_rows},
        "compile_rate":  round(compile_ok / compile_total * 100, 1) if compile_total else None,
        "compile_total": compile_total,
        "open_tickets":  open_tickets,
    }


# ---------------------------------------------------------------------------
# DB Backup & Restore
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import shutil as _shutil
import datetime as _datetime

_BACKUP_DIR = DB_PATH.parent / "backups"
_MIN_KEEP   = 3   # never delete the most-recent N backups regardless of retention rules


def _backup_dir() -> Path:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _BACKUP_DIR


def _sha256_file(path: Path) -> str:
    h = _hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(label: Optional[str] = None) -> dict:
    """
    Create a hot backup of the live DB using the SQLite online backup API.
    Saves to data/backups/stacknest-YYYYMMDDTHHMMSSZ[-label].db
    Returns metadata dict with path, size, sha256, integrity.
    """
    ts_str = _datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{label}" if label else ""
    filename = f"stacknest-{ts_str}{suffix}.db"
    dest = _backup_dir() / filename

    # Use SQLite online backup API for a consistent hot copy
    src_con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    dst_con = sqlite3.connect(str(dest))
    try:
        with _lock:
            src_con.backup(dst_con)
    finally:
        src_con.close()
        dst_con.close()

    # Compute and store SHA-256 sidecar
    sha = _sha256_file(dest)
    (dest.parent / (dest.name + ".sha256")).write_text(sha + "\n")

    # Quick integrity check on the copy
    chk_con = sqlite3.connect(str(dest))
    try:
        row = chk_con.execute("PRAGMA integrity_check").fetchone()
        integrity = row[0] if row else "unknown"
    finally:
        chk_con.close()

    size = dest.stat().st_size
    return {
        "filename":  filename,
        "path":      str(dest),
        "size":      size,
        "sha256":    sha,
        "integrity": integrity,
        "ts":        time.time(),
        "label":     label or "",
    }


def verify_backup(filename: str) -> dict:
    """
    Verify a backup file: check SHA-256 against sidecar and run integrity_check.
    Returns {"ok": bool, "integrity": str, "sha256_match": bool, "error": str|None}
    """
    dest = _backup_dir() / filename
    if not dest.exists():
        return {"ok": False, "error": "File not found", "integrity": "n/a", "sha256_match": False}

    # SHA-256 check
    sidecar = dest.parent / (dest.name + ".sha256")
    sha_match = False
    if sidecar.exists():
        expected = sidecar.read_text().strip()
        actual   = _sha256_file(dest)
        sha_match = (actual == expected)
    else:
        sha_match = None  # no sidecar — can't verify

    # Integrity check
    try:
        chk_con = sqlite3.connect(str(dest))
        row = chk_con.execute("PRAGMA integrity_check").fetchone()
        integrity = row[0] if row else "unknown"
        chk_con.close()
    except Exception as e:
        return {"ok": False, "error": str(e), "integrity": "error", "sha256_match": sha_match}

    ok = (integrity == "ok") and (sha_match is not False)
    return {"ok": ok, "integrity": integrity, "sha256_match": sha_match, "error": None}


def restore_backup(filename: str) -> dict:
    """
    Restore the live DB from a verified backup.
    First creates a pre-restore snapshot, then atomically replaces the live DB.
    Returns {"ok": bool, "pre_restore_backup": str, "error": str|None}
    """
    src = _backup_dir() / filename
    if not src.exists():
        return {"ok": False, "error": "Backup file not found", "pre_restore_backup": ""}

    # Always take a pre-restore snapshot first
    try:
        pre = create_backup(label="pre-restore")
        pre_name = pre["filename"]
    except Exception as e:
        return {"ok": False, "error": f"Failed to create pre-restore snapshot: {e}", "pre_restore_backup": ""}

    # Atomic replace: write to .tmp then rename
    tmp = DB_PATH.with_suffix(".tmp_restore")
    try:
        _shutil.copy2(str(src), str(tmp))
        with _lock:
            tmp.replace(DB_PATH)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": f"Replace failed: {e}", "pre_restore_backup": pre_name}

    return {"ok": True, "pre_restore_backup": pre_name, "error": None}


def list_backups() -> list[dict]:
    """
    Return all backup files in data/backups/, newest first.
    Each entry: {filename, size, ts_str, age_days, sha256_ok, integrity_cached}
    """
    bd = _backup_dir()
    result = []
    for f in sorted(bd.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat    = f.stat()
        sidecar = bd / (f.name + ".sha256")
        sha_ok  = sidecar.exists()  # presence only — full verify is expensive
        age_s   = time.time() - stat.st_mtime
        result.append({
            "filename":  f.name,
            "size":      stat.st_size,
            "mtime":     stat.st_mtime,
            "age_days":  round(age_s / 86400, 1),
            "sha256_ok": sha_ok,
        })
    return result


def delete_backup(filename: str) -> dict:
    """
    Delete a backup file (and its sidecar). Refuses if it would drop below _MIN_KEEP.
    Returns {"ok": bool, "error": str|None}
    """
    all_backups = list_backups()
    if len(all_backups) <= _MIN_KEEP:
        return {"ok": False, "error": f"Cannot delete: must keep at least {_MIN_KEEP} backups"}
    # Ensure we don't delete one of the most recent _MIN_KEEP
    recent_names = {b["filename"] for b in all_backups[:_MIN_KEEP]}
    if filename in recent_names:
        return {"ok": False, "error": f"Cannot delete one of the {_MIN_KEEP} most recent backups"}

    target = _backup_dir() / filename
    if not target.exists():
        return {"ok": False, "error": "File not found"}
    target.unlink()
    sidecar = target.parent / (target.name + ".sha256")
    sidecar.unlink(missing_ok=True)
    return {"ok": True, "error": None}


def cleanup_old_backups(keep: int = 10, max_age_days: float = 30.0) -> dict:
    """
    Delete backups beyond retention policy:
    - Keep the `keep` most recent regardless of age.
    - Delete any older than max_age_days beyond the first `keep`.
    Never drops below _MIN_KEEP total.
    Returns {"deleted": [filenames], "kept": N}
    """
    keep  = max(keep, _MIN_KEEP)
    all_b = list_backups()   # newest first
    deleted = []
    for i, b in enumerate(all_b):
        if i < keep:
            continue   # always keep the N most recent
        if b["age_days"] > max_age_days:
            result = delete_backup(b["filename"])
            if result["ok"]:
                deleted.append(b["filename"])
    return {"deleted": deleted, "kept": len(all_b) - len(deleted)}


def get_db_health() -> dict:
    """
    Quick DB health snapshot: integrity check, table row counts, DB file size.
    """
    try:
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except OSError:
        size = 0
    try:
        with _conn() as con:
            integrity_row = con.execute("PRAGMA integrity_check").fetchone()
            integrity     = integrity_row[0] if integrity_row else "unknown"
            users         = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            paid          = con.execute("SELECT COUNT(*) FROM users WHERE plan != 'free'").fetchone()[0]
            projects      = con.execute("SELECT COUNT(*) FROM user_projects").fetchone()[0]
            requests_ct   = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            gallery_ct    = con.execute("SELECT COUNT(*) FROM gallery").fetchone()[0]
    except Exception as e:
        return {"ok": False, "error": str(e), "integrity": "error"}
    return {
        "ok":         integrity == "ok",
        "integrity":  integrity,
        "size_bytes": size,
        "size_mb":    round(size / 1024 / 1024, 2),
        "users":      users,
        "paid_users": paid,
        "projects":   projects,
        "requests":   requests_ct,
        "gallery":    gallery_ct,
    }