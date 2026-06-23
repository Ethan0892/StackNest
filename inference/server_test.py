"""
StackNest — Runtime plugin testing on a real Paper 26.1 server.

Provides run_plugin_test() which:
  1. Creates an isolated tmpdir by copying the pre-built Paper base directory
  2. Writes the submitted plugin JAR into plugins/
  3. Launches Paper 26.1 inside a new network namespace (no outbound network)
     with conservative JVM flags and OS resource limits
  4. Streams console output, watching for "Done (" (server-ready signal)
  5. Waits a few extra seconds so all async plugin tasks fire, then stops
  6. Parses the full console log for errors, warnings, and stack traces
  7. Returns a structured result dict with StackNest-curated hints

Prerequisites (set up once on the server):
    /opt/stacknest/paper-test-base-26/
        paper.jar      — Paper 26.1 (latest build, Java 25)
        eula.txt       — eula=true
        server.properties  — online-mode=false, level-type=flat
        plugins/
            Vault.jar  — optional common dependency
"""

import os
import re
import time
import shutil
import tempfile
import threading
import subprocess
import resource
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TEST_TIMEOUT = 50        # hard kill after N seconds waiting for ready signal
SETTLE_SECS  = 5         # wait this long after "Done (" before sending stop
MAX_JAR_MB   = 20        # reject JARs larger than this

RUNTIME_PROFILE_1_21 = "paper_1_21"
RUNTIME_PROFILE_26_1 = "paper_26_1"
DEFAULT_RUNTIME_PROFILE = RUNTIME_PROFILE_26_1  # Paper 26.1 (Java 25) is now the default

_RUNTIME_TARGETS = {
    RUNTIME_PROFILE_1_21: {
        "label": "1.21.x",
        "base": Path(os.getenv("PAPER_TEST_BASE", "/opt/stacknest/paper-test-base")),
        "java": os.getenv("PAPER_TEST_JAVA_21", "").strip() or os.getenv("PAPER_TEST_JAVA", "").strip() or "java",
        "java_required": 21,
    },
    RUNTIME_PROFILE_26_1: {
        "label": "26.1.x",
        "base": Path(os.getenv("PAPER_TEST_BASE_26", os.getenv("PAPER_TEST_BASE", "/opt/stacknest/paper-test-base"))),
        "java": os.getenv("PAPER_TEST_JAVA_25", "").strip() or os.getenv("PAPER_TEST_JAVA", "").strip() or "java",
        "java_required": 25,
    },
}

# Limit concurrent test instances to avoid OOM on the VPS
_CONCURRENCY = threading.Semaphore(int(os.getenv("PAPER_TEST_CONCURRENCY", "2")))

# ---------------------------------------------------------------------------
# Hint rules  (pattern → human-readable advice)
# ---------------------------------------------------------------------------

_HINT_RULES: list[tuple[str, str]] = [
    # plugin.yml issues
    (
        r"Does not contain a valid plugin\.yml",
        "No valid plugin.yml was found inside the JAR. Make sure plugin.yml is in "
        "src/main/resources/ and that your Maven/Gradle build includes resources.",
    ),
    (
        r"Cannot load .+?: Invalid `?plugin\.yml`?",
        "Your plugin.yml is malformed. Every plugin.yml must have at minimum: "
        "name, version, main, and api-version.",
    ),
    (
        r"main class .+ of plugin .+ does not exist",
        "The `main:` field in plugin.yml points to a class that doesn't exist in your JAR. "
        "Double-check the fully-qualified class name (e.g. com.example.myplugin.MyPlugin).",
    ),
    (
        r"api-version .+? is not supported|api-version is not defined",
        "Set `api-version: '1.21'` (or your target Paper version) in plugin.yml to "
        "avoid legacy-mode loading and version warnings.",
    ),
    # Missing dependency classes
    (
        r"(ClassNotFoundException|NoClassDefFoundError):.*(com\.sk89q|worldguard|worldedit)",
        "WorldGuard/WorldEdit class not found at runtime. Add it as a Maven/Gradle "
        "dependency with `provided` scope and list it under `depend:` in plugin.yml.",
    ),
    (
        r"(ClassNotFoundException|NoClassDefFoundError):.*(net\.milkbowl|VaultAPI|Vault)",
        "Vault API class not found. Add `depend: [Vault]` to plugin.yml. "
        "Vault is pre-installed in the StackNest test environment.",
    ),
    (
        r"(ClassNotFoundException|NoClassDefFoundError):.*(me\.lucko|luckperms|LuckPerms)",
        "LuckPerms class not found. Use `softdepend: [LuckPerms]` in plugin.yml and "
        "access it via the services API rather than a direct import.",
    ),
    (
        r"(ClassNotFoundException|NoClassDefFoundError):\s*(\S+)",
        "A class could not be located at runtime. If it comes from a third-party "
        "library, either shade it into your JAR (Maven Shade / Gradle Shadow) or "
        "declare it as a `depend:` entry in plugin.yml.",
    ),
    # NullPointerException
    (
        r"NullPointerException",
        "A NullPointerException was thrown. Make sure all plugin instances, config "
        "objects, player references, and Bukkit service lookups are non-null before "
        "calling methods on them.",
    ),
    # Method / field errors
    (
        r"NoSuchMethodError|NoSuchMethodException",
        "A method does not exist in this version of the Paper API. "
        "Check the Paper 1.21 Javadocs for the correct method signature.",
    ),
    (
        r"NoSuchFieldError|NoSuchFieldException",
        "A field does not exist. Avoid accessing internal NMS/CraftBukkit fields "
        "directly — use the public Paper API instead.",
    ),
    # Cast
    (
        r"ClassCastException",
        "A ClassCastException occurred. Verify that you are casting to the correct "
        "type — for example, getEntity() returns Entity, not always Player.",
    ),
    # Stack overflow
    (
        r"StackOverflowError",
        "Infinite recursion detected. Look for event handlers that trigger themselves "
        "or methods that call each other in a loop.",
    ),
    # Java version mismatch
    (
        r"UnsupportedClassVersionError",
        "The JAR was compiled for a newer Java version than the server (Java 21). "
        "In your pom.xml set `<release>21</release>` (Maven Compiler Plugin) or "
        "`sourceCompatibility = JavaVersion.VERSION_21` in Gradle.",
    ),
    # Listener registration
    (
        r"Error registering listener .+ for plugin",
        "An event listener failed to register. The handler method must be "
        "`public void onEvent(SomeEvent e)` and annotated with `@EventHandler`.",
    ),
    # Scheduler
    (
        r"Cannot register task because plugin .+ is disabled",
        "A scheduler task was registered after the plugin was disabled. "
        "Only register tasks inside `onEnable()`, not in constructors or static blocks.",
    ),
    # YAML config
    (
        r"Cannot use section as value",
        "A YAML section was read as a scalar. Use "
        "`getConfigurationSection(\"key\")` to read a sub-section, not `getString()` or `getInt()`.",
    ),
    # Generic enable failure
    (
        r"Error occurred while enabling .+ \(Is it up to date\?\)",
        "Your plugin threw an unhandled exception inside `onEnable()`. "
        "Read the stack trace below this line to find the root cause.",
    ),
    (
        r"Could not load 'plugins/(.+\.jar)'",
        "Paper could not load your JAR. Ensure plugin.yml is included, "
        "the `main:` class exists, and all required `depend:` plugins are present.",
    ),
]

# Pre-compile
_HINT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), hint) for pat, hint in _HINT_RULES
]


def _get_hints(text_lines: list[str]) -> list[str]:
    """Return deduplicated, ordered hints matching any pattern in the log."""
    full_text = "\n".join(text_lines)
    seen: set[str] = set()
    hints: list[str] = []
    for pat, hint in _HINT_PATTERNS:
        if pat.search(full_text) and hint not in seen:
            seen.add(hint)
            hints.append(hint)
    return hints


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------

_STACK_LINE_RE  = re.compile(r"^\s+at [\w.$<>]+\(")
_CAUSED_BY_RE   = re.compile(r"^\s*Caused by:", re.IGNORECASE)
_ERROR_LINE_RE  = re.compile(r"\[ERROR\]|\[SEVERE\]", re.IGNORECASE)
_WARN_LINE_RE   = re.compile(r"\[WARN\]|\[WARNING\]", re.IGNORECASE)
_ENABLE_OK_RE   = re.compile(r"Enabling (.+?) v([\w.\-]+)", re.IGNORECASE)
_VERSION_RE     = re.compile(r"Enabling .+? v([\w.\-]+)", re.IGNORECASE)

_FATAL_RE = re.compile(
    r"Could not load|Error occurred while enabling|Cannot load|"
    r"failed to enable|plugin already initialised|"
    r"Unable to load .+ into .+ (disable)",
    re.IGNORECASE,
)


def _parse_output(lines: list[str]) -> dict:
    """Parse Paper console output into a structured result dict."""
    errors:       list[str] = []
    warnings:     list[str] = []
    stack_traces: list[str] = []
    loaded        = False
    enabled       = False
    started_ok    = False

    _in_trace      = False
    _trace_buf: list[str] = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if _in_trace and _trace_buf:
                stack_traces.append("\n".join(_trace_buf))
                _trace_buf = []
                _in_trace  = False
            continue

        # Accumulate stack trace lines
        if _STACK_LINE_RE.match(stripped) or _CAUSED_BY_RE.match(stripped):
            _in_trace = True
            _trace_buf.append(stripped)
            continue
        else:
            if _in_trace:
                stack_traces.append("\n".join(_trace_buf))
                _trace_buf = []
                _in_trace  = False

        if "Done (" in line:
            started_ok = True
        if re.search(r"Loading .+? v[\w.\-]+", line, re.IGNORECASE):
            loaded = True
        if _ENABLE_OK_RE.search(line):
            enabled = True

        if _ERROR_LINE_RE.search(line):
            errors.append(stripped)
        elif _WARN_LINE_RE.search(line):
            warnings.append(stripped)

    # Flush trailing trace
    if _in_trace and _trace_buf:
        stack_traces.append("\n".join(_trace_buf))

    has_fatal = bool(_FATAL_RE.search("\n".join(errors)))
    success   = started_ok and enabled and not has_fatal

    return {
        "started_ok":   started_ok,
        "loaded":       loaded,
        "enabled":      enabled,
        "success":      success,
        "errors":       errors[:50],
        "warnings":     warnings[:50],
        "stack_traces": stack_traces[:15],
    }


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------

def run_plugin_test(
    jar_bytes: bytes,
    plugin_name: str,
    runtime_profile: str = DEFAULT_RUNTIME_PROFILE,
) -> Iterator[dict]:
    """
    Generator — yields SSE-style event dicts then a final result dict.

    Phase:  {"type": "phase",  "step": str, "percent": int, "thinking": str}
    Result: {"type": "result", "success": bool, "plugin_name": str,
             "version": str, "load_time_ms": int,
             "errors": [...], "warnings": [...], "stack_traces": [...],
             "hints": [...], "raw_lines": [...],
             "duration_s": float, "timed_out": bool}
    Error:  {"type": "error",  "message": str}
    """
    cfg = _RUNTIME_TARGETS.get(runtime_profile, _RUNTIME_TARGETS[DEFAULT_RUNTIME_PROFILE])
    paper_base: Path = cfg["base"]
    paper_jar = paper_base / "paper.jar"
    java_cmd: str = cfg["java"]
    profile_label: str = cfg["label"]
    java_required: int = int(cfg["java_required"])

    if len(jar_bytes) > MAX_JAR_MB * 1024 * 1024:
        yield {"type": "error", "message": f"JAR is too large (max {MAX_JAR_MB} MB)."}
        return

    if not paper_jar.exists():
        yield {
            "type":    "error",
            "message": (
                f"The Paper {profile_label} test environment is not configured on this server. "
                "Please contact StackNest support."
            ),
        }
        return

    acquired = _CONCURRENCY.acquire(blocking=True, timeout=20)
    if not acquired:
        yield {
            "type":    "error",
            "message": "All test slots are currently busy — please try again in a few seconds.",
        }
        return

    tmpdir: Path | None = None
    proc:   subprocess.Popen | None = None
    t_start = time.time()

    try:
        # ── 1. Isolated environment ──────────────────────────────────────────
        yield {
            "type":     "phase",
            "percent":  8,
            "step":     "Preparing isolated server environment…",
            "thinking": f"Cloning Paper {profile_label} base directory into a fresh tmpdir.",
        }

        tmpdir = Path(tempfile.mkdtemp(prefix="sn_rt_"))
        shutil.copytree(str(paper_base), str(tmpdir), dirs_exist_ok=True)

        # Keep Vault/LuckPerms but remove any other JARs from the base
        plugins_dir = tmpdir / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        _keep = {"vault", "luckperms"}
        for f in list(plugins_dir.glob("*.jar")):
            if f.stem.lower() not in _keep:
                f.unlink(missing_ok=True)

        # Write the plugin under test
        safe_name = re.sub(r"[^\w\-]", "_", plugin_name)[:64] or "TestPlugin"
        jar_path  = plugins_dir / f"{safe_name}.jar"
        jar_path.write_bytes(jar_bytes)

        yield {
            "type":     "phase",
            "percent":  18,
            "step":     f"Launching Paper {profile_label}…",
            "thinking": (
                f"JVM command: {java_cmd} (requires Java {java_required}+), "
                "flags: -Xms128m -Xmx512m, no GUI, offline mode."
            ),
        }

        # ── 2. Harden server.properties ─────────────────────────────────────
        (tmpdir / "eula.txt").write_text("eula=true\n")

        props_path = tmpdir / "server.properties"
        existing   = props_path.read_text() if props_path.exists() else ""
        if "online-mode" in existing:
            existing = re.sub(r"online-mode\s*=\s*\w+", "online-mode=false", existing)
        else:
            existing += "\nonline-mode=false\n"
        # Use flat world for fastest generation
        if "level-type" in existing:
            existing = re.sub(r"level-type\s*=\s*\S+", "level-type=flat", existing)
        else:
            existing += "level-type=flat\n"
        props_path.write_text(existing)

        # ── 3. Start Paper (sandboxed) ───────────────────────────────────────
        # Security layers:
        #   a) Network namespace (unshare -n) — no outbound or inbound network
        #   b) OS resource limits via preexec_fn:
        #        RLIMIT_FSIZE  — 256 MB max file write size (stops file-bomb payloads)
        #        RLIMIT_NPROC  — 256 sub-processes max
        #        RLIMIT_AS     — 1.5 GB virtual address space
        #   c) Tmpdir isolation — all writes are confined to the tmpdir
        java_flags = [
            "-Xms128m", "-Xmx512m",
            "-XX:+UseG1GC", "-XX:MaxGCPauseMillis=50",
            "-Dterminal.jline=false",
            "-Dterminal.ansi=false",
            "-Dcom.mojang.eula.agree=true",
            # Redirect any stray file writes to the tmpdir
            f"-Djava.io.tmpdir={tmpdir}",
        ]
        paper_args = ["-jar", str(tmpdir / "paper.jar"), "--nogui"]

        # Wrap with unshare --net if available (blocks all network I/O in the child)
        import shutil as _shutil
        _unshare = _shutil.which("unshare")
        if _unshare:
            cmd = [_unshare, "--net", "--", java_cmd] + java_flags + paper_args
        else:
            cmd = [java_cmd] + java_flags + paper_args

        _FSIZE_LIMIT = 256 * 1024 * 1024    # 256 MB
        _NPROC_LIMIT = 256
        _AS_LIMIT    = 1536 * 1024 * 1024   # 1.5 GB virtual memory

        def _set_resource_limits():
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, (_FSIZE_LIMIT, _FSIZE_LIMIT))
            except Exception:
                pass
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (_NPROC_LIMIT, _NPROC_LIMIT))
            except Exception:
                pass
            try:
                resource.setrlimit(resource.RLIMIT_AS, (_AS_LIMIT, _AS_LIMIT))
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd,
            cwd=str(tmpdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=_set_resource_limits,
        )

        yield {
            "type":     "phase",
            "percent":  30,
            "step":     "Server booting — waiting for plugin to load…",
            "thinking": "Streaming Paper stdout, watching for startup-complete signal.",
        }

        # ── 4. Read output in background thread ─────────────────────────────
        all_lines: list[str] = []
        ready     = False
        t_ready   = None

        def _reader():
            nonlocal ready, t_ready
            try:
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    all_lines.append(line)
                    if not ready and "Done (" in line:
                        ready   = True
                        t_ready = time.time()
            except Exception:
                pass

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        # Main thread: yield progress ticks while waiting
        t_kill    = t_start + TEST_TIMEOUT
        _last_tick = t_start

        while not ready and time.time() < t_kill:
            time.sleep(0.4)
            elapsed = time.time() - t_start
            if time.time() - _last_tick >= 4:
                _last_tick = time.time()
                pct = min(75, 30 + int(elapsed / TEST_TIMEOUT * 50))
                yield {
                    "type":     "phase",
                    "percent":  pct,
                    "step":     f"Loading plugins… ({int(elapsed)}s elapsed)",
                    "thinking": f"Log lines captured: {len(all_lines)}",
                }

        if not ready:
            # Hard timeout — kill and parse whatever we got
            try:
                proc.kill()
            except Exception:
                pass
            reader.join(timeout=4)
            yield {
                "type":     "phase",
                "percent":  85,
                "step":     "Parsing output (server timed out)…",
                "thinking": f"No ready signal within {TEST_TIMEOUT}s. Analysing log anyway.",
            }
        else:
            yield {
                "type":     "phase",
                "percent":  82,
                "step":     "Plugin loaded — capturing final output…",
                "thinking": (
                    f"Server ready in {round(t_ready - t_start, 1)}s. "
                    f"Waiting {SETTLE_SECS}s for async tasks to fire."
                ),
            }
            time.sleep(SETTLE_SECS)

            # Graceful shutdown
            try:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
            reader.join(timeout=6)

        # ── 5. Parse + hint ─────────────────────────────────────────────────
        duration = round(time.time() - t_start, 2)

        yield {
            "type":     "phase",
            "percent":  94,
            "step":     "Analysing server log…",
            "thinking": f"Scanning {len(all_lines)} log lines for issues.",
        }

        parsed = _parse_output(all_lines)
        hints  = _get_hints(
            all_lines + parsed["errors"] + parsed["stack_traces"]
        )

        # Extract plugin version from enable line
        version = ""
        for line in all_lines:
            m = _VERSION_RE.search(line)
            if m:
                version = m.group(1)
                break

        load_ms = int((t_ready - t_start) * 1000) if t_ready else int(TEST_TIMEOUT * 1000)

        # Summary counts for the UI badge
        n_errors   = len(parsed["errors"])
        n_warnings = len(parsed["warnings"])
        n_traces   = len(parsed["stack_traces"])

        yield {
            "type":           "result",
            "success":        parsed["success"],
            "plugin_name":    plugin_name,
            "version":        version,
            "load_time_ms":   load_ms,
            "errors":         parsed["errors"],
            "warnings":       parsed["warnings"],
            "stack_traces":   parsed["stack_traces"],
            "hints":          hints,
            "raw_lines":      all_lines[-300:],
            "duration_s":     duration,
            "timed_out":      not ready,
            "started_ok":     parsed["started_ok"],
            "enabled":        parsed["enabled"],
            "n_errors":       n_errors,
            "n_warnings":     n_warnings,
            "n_traces":       n_traces,
            "runtime_profile": runtime_profile,
            "target_api":      profile_label,
        }

    except Exception as exc:
        import traceback
        traceback.print_exc()
        yield {"type": "error", "message": f"Test runner internal error: {exc}"}

    finally:
        # Always clean up
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if tmpdir is not None and tmpdir.exists():
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
        _CONCURRENCY.release()
