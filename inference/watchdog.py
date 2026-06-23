"""
inference/watchdog.py — Backend health watchdog for StackNest.

Runs a single daemon thread that:
  • Probes the llama.cpp server every PROBE_INTERVAL seconds.
  • Tracks up/down transitions with timestamps.
  • Automatically resets the circuit breaker the moment llama.cpp recovers.
  • Tracks Gemini / Claude availability (key present + last known call result).
  • Exposes get_status() for the /api/health/detailed endpoint.
  • Exposes is_healthy(backend) for fast path-selection decisions.

Thread safety: all shared state is guarded by _lock.

Usage:
    from inference.watchdog import start, get_status, is_healthy
    start()   # call once at app startup — idempotent
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# ── Tunables ─────────────────────────────────────────────────────────────────
PROBE_INTERVAL    = float(os.getenv("WATCHDOG_PROBE_INTERVAL",  "30"))   # seconds
RECOVERY_TIMEOUT  = float(os.getenv("WATCHDOG_RECOVERY_TIMEOUT", "5"))   # llama health probe timeout
ALERT_AFTER_FAILS = int(os.getenv("WATCHDOG_ALERT_AFTER_FAILS",  "3"))   # consecutive fails before logging warning

# ── Cross-process cloud-health file ──────────────────────────────────────────
# When a worker detects a quota/billing failure it writes to this file so ALL
# gunicorn workers (which are separate processes with separate in-process state)
# immediately stop trying the affected backend instead of each making their own
# 3 wasted retry attempts before independently marking the backend unhealthy.
_CLOUD_HEALTH_FILE = "/tmp/stacknest_cloud_health.json"
_file_health_lock  = threading.Lock()
# Per-process cache: backend -> (healthy: bool, expiry_ts: float)
_file_health_cache: dict[str, tuple[bool, float]] = {}


def _write_cross_proc_down(backend: str, error: str) -> None:
    """Persist a quota-exhaustion event to the shared health file."""
    try:
        with _file_health_lock:
            try:
                with open(_CLOUD_HEALTH_FILE) as f:
                    data: dict = json.load(f)
            except Exception:
                data = {}
            data[backend] = {"healthy": False, "ts": time.time(), "error": error[:120]}
            with open(_CLOUD_HEALTH_FILE, "w") as f:
                json.dump(data, f)
            # Invalidate local cache entry so next is_healthy call reads fresh data
            _file_health_cache.pop(backend, None)
    except Exception:
        pass


def _write_cross_proc_up(backend: str) -> None:
    """Remove a backend from the shared health file (quota recovered)."""
    try:
        with _file_health_lock:
            try:
                with open(_CLOUD_HEALTH_FILE) as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data.pop(backend, None)
            with open(_CLOUD_HEALTH_FILE, "w") as f:
                json.dump(data, f)
            _file_health_cache.pop(backend, None)
    except Exception:
        pass


def _cross_proc_is_healthy(backend: str) -> bool:
    """
    Return False if the shared health file says this backend is quota-down.
    Caches the file read per-process for 60s (down) / 30s (up) to avoid
    hitting disk on every inference call.

    Auto-recovery: a "down" entry older than 4 hours is treated as stale and
    removed from the file so the backend is retried.  This handles the case
    where billing is topped up without the server knowing.
    """
    _DOWN_TTL = 4 * 3600  # 4 hours — re-probe after this even if never cleared
    with _file_health_lock:
        cached = _file_health_cache.get(backend)
        if cached is not None and time.time() < cached[1]:
            return cached[0]
    try:
        with open(_CLOUD_HEALTH_FILE) as f:
            data = json.load(f)
        entry = data.get(backend)
        if entry and not entry.get("healthy", True):
            # Auto-expire stale down entries so billing top-ups are noticed
            age = time.time() - entry.get("ts", 0)
            if age > _DOWN_TTL:
                # Remove the stale entry and let the next live call decide
                data.pop(backend, None)
                try:
                    with open(_CLOUD_HEALTH_FILE, "w") as f2:
                        json.dump(data, f2)
                except Exception:
                    pass
                healthy = True
                expiry  = time.time() + 30
            else:
                healthy = False
                expiry  = time.time() + 60   # re-check after 60s (quota may have recovered)
        else:
            healthy = True
            expiry  = time.time() + 30
    except FileNotFoundError:
        healthy = True
        expiry  = time.time() + 30
    except Exception:
        healthy = True
        expiry  = time.time() + 10
    with _file_health_lock:
        _file_health_cache[backend] = (healthy, expiry)
    return healthy

# ── Shared state ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state: dict[str, Any] = {
    # llama.cpp / local inference
    "local": {
        "healthy":        False,
        "consecutive_ok": 0,
        "consecutive_fail": 0,
        "last_ok_ts":     0.0,
        "last_fail_ts":   0.0,
        "last_probe_ts":  0.0,
        "latency_ms":     None,
    },
    # Cloud AI backends — no active probing (would cost quota); tracked passively
    "gemini": {
        "configured":     False,
        "healthy":        True,      # assumed healthy until proven otherwise
        "consecutive_fail": 0,
        "last_fail_ts":   0.0,
        "last_ok_ts":     0.0,
        "error":          None,
    },
    "claude": {
        "configured":     False,
        "healthy":        True,
        "consecutive_fail": 0,
        "last_fail_ts":   0.0,
        "last_ok_ts":     0.0,
        "error":          None,
    },
    "kimi": {
        "configured":     False,
        "healthy":        True,
        "consecutive_fail": 0,
        "last_fail_ts":   0.0,
        "last_ok_ts":     0.0,
        "error":          None,
    },
    "deepseek": {
        "configured":     False,
        "healthy":        True,
        "consecutive_fail": 0,
        "last_fail_ts":   0.0,
        "last_ok_ts":     0.0,
        "error":          None,
    },
    "groq": {
        "configured":     False,
        "healthy":        True,
        "consecutive_fail": 0,
        "last_fail_ts":   0.0,
        "last_ok_ts":     0.0,
        "error":          None,
    },
    "api_start_ts": time.time(),
    "watchdog_running": False,
}

_started = False
_thread: threading.Thread | None = None
_watchdog_lock_fd = None  # holds the fcntl file-lock fd so it is not GC-ed


# ── Cloud backend availability refresh ───────────────────────────────────────

def _refresh_cloud_availability() -> None:
    """Check whether each cloud key is configured (no network call)."""
    try:
        from inference.gemini import is_available as gem_ok
        with _lock:
            _state["gemini"]["configured"] = gem_ok()
    except Exception:
        pass
    try:
        from inference.claude import is_available as cla_ok
        with _lock:
            _state["claude"]["configured"] = cla_ok()
    except Exception:
        pass
    try:
        from inference.kimi import is_available as kim_ok
        with _lock:
            _state["kimi"]["configured"] = kim_ok()
    except Exception:
        pass
    try:
        from inference.deepseek import is_available as ds_ok
        with _lock:
            _state["deepseek"]["configured"] = ds_ok()
    except Exception:
        pass
    try:
        from inference.groq import is_available as groq_ok
        with _lock:
            _state["groq"]["configured"] = groq_ok()
    except Exception:
        pass


# Initialise cloud configured flags at import time (cheap key-presence check,
# no network call) so that every gunicorn worker — not just the one that wins
# the watchdog lock — knows which backends are available.
try:
    _refresh_cloud_availability()
except Exception:
    pass


# ── Local probe ──────────────────────────────────────────────────────────────

def _probe_local() -> bool:
    """Test llama.cpp health. Returns True if up."""
    try:
        from inference.server import health_check
        t0 = time.monotonic()
        up = health_check(timeout=RECOVERY_TIMEOUT)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        with _lock:
            _state["local"]["last_probe_ts"] = time.time()
            _state["local"]["latency_ms"] = latency_ms
        return up
    except Exception:
        return False


def _on_local_up() -> None:
    """Called when llama.cpp responds healthy — resets circuit breaker if needed."""
    with _lock:
        prev_healthy = _state["local"]["healthy"]
        _state["local"]["healthy"]         = True
        _state["local"]["consecutive_ok"] += 1
        _state["local"]["consecutive_fail"] = 0
        _state["local"]["last_ok_ts"]       = time.time()
        was_down = not prev_healthy

    if was_down:
        print("[Watchdog] llama.cpp RECOVERED — resetting circuit breaker.")
        try:
            # Import here to avoid circular dependency
            import inference.server as _srv  # noqa: PLC0415
            _srv._cb_success()
        except Exception as e:
            print(f"[Watchdog] Failed to reset circuit breaker: {e}")


def _on_local_down() -> None:
    """Called when llama.cpp probe fails."""
    with _lock:
        prev_healthy = _state["local"]["healthy"]
        _state["local"]["healthy"]           = False
        _state["local"]["consecutive_fail"] += 1
        _state["local"]["consecutive_ok"]    = 0
        _state["local"]["last_fail_ts"]      = time.time()
        consec = _state["local"]["consecutive_fail"]

    if prev_healthy:
        print("[Watchdog] llama.cpp went DOWN.")
    elif consec % ALERT_AFTER_FAILS == 0:
        print(f"[Watchdog] llama.cpp still unreachable (fail #{consec}).")


# ── Watchdog loop ─────────────────────────────────────────────────────────────

def _loop() -> None:
    with _lock:
        _state["watchdog_running"] = True

    print(f"[Watchdog] Started (probe every {PROBE_INTERVAL}s).")
    _refresh_cloud_availability()

    while True:
        try:
            if _probe_local():
                _on_local_up()
            else:
                _on_local_down()

            # Refresh cloud key config every 5 probes (keys can be added at runtime)
            with _lock:
                _consec = _state["local"]["consecutive_ok"] + _state["local"]["consecutive_fail"]
            if _consec % 5 == 0:
                _refresh_cloud_availability()

        except Exception as e:
            print(f"[Watchdog] Unexpected error in probe loop: {e}")

        time.sleep(PROBE_INTERVAL)


# ── Public API ────────────────────────────────────────────────────────────────

def start() -> None:
    """
    Start the watchdog daemon thread.

    Uses an exclusive fcntl file-lock on /tmp/stacknest_watchdog.lock so that
    exactly ONE gunicorn worker (whichever wins the race) runs the thread.
    All other workers fail to acquire the lock and skip silently.
    In standalone Flask dev mode the lock is still acquired (no contention).
    Idempotent — safe to call multiple times.
    """
    global _started, _thread, _watchdog_lock_fd
    # After gunicorn forks a worker, _started may be True (inherited from master)
    # but the watchdog thread does NOT survive fork — detect and reset.
    if _started and (_thread is None or not _thread.is_alive()):
        _started = False
        _watchdog_lock_fd = None
    if _started:
        return
    try:
        import fcntl
        lf = open("/tmp/stacknest_watchdog.lock", "w")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _watchdog_lock_fd = lf          # keep reference so fd stays open
    except (OSError, IOError):
        return  # another worker already holds the lock — skip silently
    _started = True
    _thread = threading.Thread(target=_loop, name="sn-watchdog", daemon=True)
    _thread.start()


def report_cloud_success(backend: str) -> None:
    """
    Call this after a successful cloud API call to track backend health.
    backend: 'gemini' | 'claude' | 'kimi'
    """
    with _lock:
        if backend in _state:
            _state[backend]["healthy"]           = True
            _state[backend]["consecutive_fail"]  = 0
            _state[backend]["last_ok_ts"]        = time.time()
            _state[backend]["error"]             = None
    # Clear the cross-process down marker so other workers recover too
    _write_cross_proc_up(backend)


def report_cloud_failure(backend: str, error: str, is_quota: bool = False) -> None:
    """
    Call this after a cloud API call fails.
    backend: 'gemini' | 'claude' | 'kimi'
    is_quota: True if this was a rate-limit / quota-exhausted error.
    """
    with _lock:
        if backend in _state:
            _state[backend]["consecutive_fail"] += 1
            _state[backend]["last_fail_ts"]      = time.time()
            _state[backend]["error"]             = error[:200]
            # Only mark unhealthy on quota exhaustion or repeated failures
            if is_quota or _state[backend]["consecutive_fail"] >= 3:
                _state[backend]["healthy"] = False    # Quota/billing failures: write to shared file so ALL workers skip this
    # backend immediately without each needing to accumulate 3 failures
    if is_quota:
        _write_cross_proc_down(backend, error)

def report_cloud_quota_reset(backend: str) -> None:
    """Mark a cloud backend as healthy again (e.g. after a 24h quota reset)."""
    with _lock:
        if backend in _state:
            _state[backend]["healthy"]          = True
            _state[backend]["consecutive_fail"] = 0
            _state[backend]["error"]            = None


def is_healthy(backend: str) -> bool:
    """Return True if the backend is currently considered healthy."""
    # Fast cross-process check first: quota events are written to a shared file
    # so all gunicorn workers immediately skip a downed backend
    if not _cross_proc_is_healthy(backend):
        return False
    with _lock:
        b = _state.get(backend, {})
        if backend == "local":
            return b.get("healthy", False)
        # If this worker never ran the watchdog thread, configured may still be
        # False even though the API key IS set.  Do a cheap key-check on the fly.
        if not b.get("configured", False):
            try:
                if backend == "gemini":
                    from inference.gemini import is_available as _ok
                elif backend == "claude":
                    from inference.claude import is_available as _ok
                elif backend == "kimi":
                    from inference.kimi import is_available as _ok
                else:
                    return False
                configured = _ok()
                b["configured"] = configured
                if not configured:
                    return False
            except Exception:
                return False
        # Cloud: healthy if not marked down by previous failures
        return b.get("healthy", True)


def any_backend_available() -> bool:
    """Return True if at least one generation backend is usable."""
    return (
        is_healthy("local")
        or is_healthy("gemini")
        or is_healthy("claude")
        or is_healthy("kimi")
    )


def get_status() -> dict:
    """
    Return a full health snapshot for the /api/health/detailed endpoint.
    This is a read-only copy — no side effects.
    """
    with _lock:
        import copy
        snap = copy.deepcopy(_state)

    now = time.time()
    uptime = round(now - snap["api_start_ts"])

    # Human-readable age fields
    for key in ("local", "gemini", "claude", "kimi", "deepseek", "groq"):
        b = snap[key]
        for ts_field in ("last_ok_ts", "last_fail_ts", "last_probe_ts"):
            ts = b.get(ts_field, 0.0)
            if ts:
                b[f"{ts_field}_ago_s"] = round(now - ts)

    return {
        "api_uptime_s":      uptime,
        "watchdog_running":  snap["watchdog_running"],
        "backends": {
            "local":    snap["local"],
            "gemini":   snap["gemini"],
            "claude":   snap["claude"],
            "kimi":     snap["kimi"],
            "deepseek": snap["deepseek"],
            "groq":     snap["groq"],
        },
    }
