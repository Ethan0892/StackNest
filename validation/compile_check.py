"""
validation/compile_check.py — Compile generated Java code against Paper 1.21 stub JAR.

Requires:
  - Java 21 installed (java/javac on PATH)
  - Paper API stub JAR — auto-downloaded to libs/ on first use if missing

Usage:
    from validation.compile_check import compile_response
    result = compile_response(model_response_string)
    if result.success:
        print("Compiles!")
    else:
        print(result.errors)
"""

import pathlib
import re
import shutil
import subprocess
import tempfile
import urllib.request
import os
from dataclasses import dataclass, field

import platform

# Paper target profiles used by generation/build/runtime routes.
PAPER_PROFILE_1_21 = "paper_1_21"
PAPER_PROFILE_26_1 = "paper_26_1"
# Paper 26.1 is now stable — use it as the default.
DEFAULT_PAPER_PROFILE = PAPER_PROFILE_26_1

_PAPER_TARGETS: dict[str, dict[str, str | int]] = {
    PAPER_PROFILE_1_21: {
        "jar": "libs/paper-api-1.21-stub.jar",
        "url": (
            "https://repo.papermc.io/repository/maven-public/"
            "io/papermc/paper/paper-api/1.21-R0.1-SNAPSHOT/"
            "paper-api-1.21-R0.1-SNAPSHOT.jar"
        ),
        "source": "21",
        "target": "21",
        "java_required": 21,
    },
    PAPER_PROFILE_26_1: {
        "jar": "libs/paper-api-26.1-stub.jar",
        "url": (
            "https://repo.papermc.io/repository/maven-public/"
            "io/papermc/paper/paper-api/26.1.2.build.62-stable/"
            "paper-api-26.1.2.build.62-stable.jar"
        ),
        "source": "25",
        "target": "25",
        "java_required": 25,
    },
}

# Backward-compat constants for existing call sites.
PAPER_API_JAR = pathlib.Path(str(_PAPER_TARGETS[DEFAULT_PAPER_PROFILE]["jar"]))
PAPER_API_URL = str(_PAPER_TARGETS[DEFAULT_PAPER_PROFILE]["url"])
JAVA_SOURCE_VERSION = str(_PAPER_TARGETS[DEFAULT_PAPER_PROFILE]["source"])
JAVA_TARGET_VERSION = str(_PAPER_TARGETS[DEFAULT_PAPER_PROFILE]["target"])

LIBS_DIR = pathlib.Path("libs")

# ---------------------------------------------------------------------------
# Register dynamic Paper profiles from the auto-update cache
# ---------------------------------------------------------------------------
try:
    from api.paper_versions import (  # noqa: E402
        STABLE_PAPER_PROFILE as _pv_profile,
        STABLE_MC_VERSION    as _pv_mc,
        STABLE_JAVA_VERSION  as _pv_java,
        BRIGADIER_VERSION    as _pv_brigadier,
        get_stable_paper_targets_entry as _pv_targets_entry,
    )
    # Register the stable profile if it isn't already present
    # (handles Paper 26.2, 27.x, etc. without code changes)
    if _pv_profile not in _PAPER_TARGETS:
        _PAPER_TARGETS[_pv_profile] = _pv_targets_entry()
    # Update DEFAULT_PAPER_PROFILE to whatever is currently stable
    DEFAULT_PAPER_PROFILE = _pv_profile
    _BRIGADIER_VERSION = _pv_brigadier
except Exception as _pv_err:
    _BRIGADIER_VERSION = "1.3.10"
    if "ImportError" not in type(_pv_err).__name__:
        print(f"[compile_check] paper_versions load warning: {_pv_err}")

# Extra JARs to download for compile-time resolution of common third-party APIs
_EXTRA_JARS: list[tuple[str, str]] = [
    # Brigadier — Paper's command framework (com.mojang.brigadier.*)
    # MUST be on the classpath separately; the thin paper-api stub does not
    # bundle it. Without this, any Brigadier import causes 'package does not exist'.
    # Hosted on Mojang's library server (not Maven Central).
    (f"brigadier-{_BRIGADIER_VERSION}.jar",
     f"https://libraries.minecraft.net/com/mojang/brigadier/{_BRIGADIER_VERSION}/brigadier-{_BRIGADIER_VERSION}.jar"),
    # net.kyori adventure (bundled with Paper but not in the thin API stub)
    ("adventure-api-4.17.0.jar",
     "https://repo1.maven.org/maven2/net/kyori/adventure-api/4.17.0/adventure-api-4.17.0.jar"),
    ("adventure-key-4.17.0.jar",
     "https://repo1.maven.org/maven2/net/kyori/adventure-key/4.17.0/adventure-key-4.17.0.jar"),
    ("adventure-text-minimessage-4.17.0.jar",
     "https://repo1.maven.org/maven2/net/kyori/adventure-text-minimessage/4.17.0/adventure-text-minimessage-4.17.0.jar"),
    ("adventure-text-serializer-legacy-4.17.0.jar",
     "https://repo1.maven.org/maven2/net/kyori/adventure-text-serializer-legacy/4.17.0/adventure-text-serializer-legacy-4.17.0.jar"),
    ("adventure-text-serializer-gson-4.17.0.jar",
     "https://repo1.maven.org/maven2/net/kyori/adventure-text-serializer-gson/4.17.0/adventure-text-serializer-gson-4.17.0.jar"),
    ("adventure-text-serializer-plain-4.17.0.jar",
     "https://repo1.maven.org/maven2/net/kyori/adventure-text-serializer-plain/4.17.0/adventure-text-serializer-plain-4.17.0.jar"),
    ("examination-api-1.3.0.jar",
     "https://repo1.maven.org/maven2/net/kyori/examination-api/1.3.0/examination-api-1.3.0.jar"),
    # JetBrains annotations (@NotNull, @Nullable, etc.)
    ("jetbrains-annotations-24.1.0.jar",
     "https://repo1.maven.org/maven2/org/jetbrains/annotations/24.1.0/annotations-24.1.0.jar"),
    # gson (used by BungeeCord chat and Paper legacy API)
    ("gson-2.10.1.jar",
     "https://repo1.maven.org/maven2/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar"),
    # guava (referenced by Paper API)
    ("guava-33.2.1-jre.jar",
     "https://repo1.maven.org/maven2/com/google/guava/guava/33.2.1-jre/guava-33.2.1-jre.jar"),
    # commons-lang3 (referenced by some Paper utilities)
    ("commons-lang3-3.14.0.jar",
     "https://repo1.maven.org/maven2/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar"),
    # Note: bungeecord-chat JAR (net.md_5.bungee) is not on Maven Central.
    # "cannot access BaseComponent" errors are filtered in _is_transitive_error().
    # WorldGuard / WorldEdit compile stubs
    ("worldguard-core-7.0.12.jar",
     "https://maven.enginehub.org/repo/com/sk89q/worldguard/worldguard-core/7.0.12/worldguard-core-7.0.12.jar"),
    ("worldedit-core-7.3.6.jar",
     "https://maven.enginehub.org/repo/com/sk89q/worldedit/worldedit-core/7.3.6/worldedit-core-7.3.6.jar"),
    # Vault API (economy / permissions integrations)
    ("VaultAPI-1.7.1.jar",
     "https://jitpack.io/com/github/MilkBowl/VaultAPI/1.7.1/VaultAPI-1.7.1.jar"),
    # Velocity proxy API dependencies (Guice DI + SLF4J logging)
    ("guice-7.0.0.jar",
     "https://repo1.maven.org/maven2/com/google/inject/guice/7.0.0/guice-7.0.0.jar"),
    ("slf4j-api-2.0.16.jar",
     "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.16/slf4j-api-2.0.16.jar"),
    # JUnit Jupiter (used when AI generates test classes; must match JUNIT_JAR path below)
    ("junit-jupiter-api-5.11.3.jar",
     "https://repo1.maven.org/maven2/org/junit/jupiter/junit-jupiter-api/5.11.3/junit-jupiter-api-5.11.3.jar"),
    ("junit-platform-commons-1.11.3.jar",
     "https://repo1.maven.org/maven2/org/junit/platform/junit-platform-commons/1.11.3/junit-platform-commons-1.11.3.jar"),
    ("opentest4j-1.3.0.jar",
     "https://repo1.maven.org/maven2/org/opentest4j/opentest4j/1.3.0/opentest4j-1.3.0.jar"),
]


def ensure_extra_jars() -> None:
    """Download any missing compile-time dependency JARs into libs/."""
    LIBS_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (StackNest build system)"}
    for filename, url in _EXTRA_JARS:
        dest = LIBS_DIR / filename
        if dest.exists():
            continue
        try:
            print(f"[compile_check] Downloading {filename} ...")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
                out.write(resp.read())
            print(f"[compile_check] Downloaded {filename} ({dest.stat().st_size // 1024} KB)")
        except Exception as exc:
            print(f"[compile_check] Warning: could not download {filename}: {exc}")


def _build_classpath() -> str:
    """Return a platform-correct classpath string from all JARs in libs/."""
    sep = ";" if platform.system() == "Windows" else ":"
    jars = sorted(LIBS_DIR.glob("*.jar"))
    return sep.join(str(j.resolve()) for j in jars)


def _target_cfg(paper_profile: str) -> dict[str, str | int]:
    return _PAPER_TARGETS.get(paper_profile, _PAPER_TARGETS[DEFAULT_PAPER_PROFILE])


def _parse_java_major(version_output: str) -> int | None:
    m = re.search(r"(\d+)(?:\.\d+)?", version_output)
    return int(m.group(1)) if m else None


def _find_javac(required_major: int = 21) -> str | None:
    """Return a javac path meeting minimum major version, or None."""
    env_override = os.getenv("STACKNEST_JAVAC", "").strip()
    candidates: list[str] = []
    if required_major >= 25:
        env_25 = os.getenv("STACKNEST_JAVAC_25", "").strip()
        if env_25:
            candidates.append(env_25)
    if env_override:
        candidates.append(env_override)
    path_javac = shutil.which("javac")
    if path_javac:
        candidates.append(path_javac)

    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            proc = subprocess.run(
                [cand, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
            major = _parse_java_major(raw)
            if major is not None and major >= required_major:
                return cand
        except Exception:
            continue
    return None


def ensure_paper_jar(paper_profile: str = DEFAULT_PAPER_PROFILE) -> bool:
    """Download the Paper API JAR if it doesn't exist. Returns True if available."""
    cfg = _target_cfg(paper_profile)
    paper_api_jar = pathlib.Path(str(cfg["jar"]))
    paper_api_url = str(cfg["url"])

    if paper_api_jar.exists():
        ensure_extra_jars()  # opportunistically grab extras too
        return True
    paper_api_jar.parent.mkdir(parents=True, exist_ok=True)
    try:
        print(f"[compile_check] Downloading Paper API JAR → {paper_api_jar} ...")
        headers = {"User-Agent": "Mozilla/5.0 (StackNest build system)"}
        req = urllib.request.Request(paper_api_url, headers=headers)
        with urllib.request.urlopen(req) as resp, open(paper_api_jar, "wb") as out:
            out.write(resp.read())
        print(f"[compile_check] Paper API JAR downloaded ({paper_api_jar.stat().st_size // 1024} KB)")
        ensure_extra_jars()
        return True
    except Exception as e:
        print(f"[compile_check] Failed to download Paper API JAR: {e}")
        return False


@dataclass
class CompileResult:
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_compiled: int = 0
    temp_dir: str = ""


def _parse_javac_errors(stderr: str) -> tuple[list[str], list[str]]:
    """
    Parse javac stderr and return (errors, warnings) where each entry is the
    full diagnostic block —  error line  +  ``symbol:``  +  ``location:``
    context lines joined together.

    javac emits multi-line diagnostics:
        path/File.java:15: error: cannot find symbol
                import com.example.Foo;
                                   ^
          symbol:   class Foo
          location: package com.example

    The old single-line filter captured only the first line, so the AI received
    "error: cannot find symbol" with no idea *what* symbol to fix.  This parser
    groups each diagnostic with its context lines so the AI gets, e.g.:
        "File.java:15: error: cannot find symbol — symbol: class Foo, location: package com.example"
    """
    # Strip the long temp-dir prefix from every path so messages are concise.
    _path_strip_re = re.compile(
        r"^.*?/src/(?:main|test)/java/",
        re.IGNORECASE,
    )
    # Marks the START of a javac diagnostic: anything like "path.java:N: error: ..."
    _diag_re = re.compile(r"^.+\.java:\d+:\s+(?:error|warning):\s+", re.IGNORECASE)

    lines = stderr.splitlines()
    errors: list[str] = []
    warnings: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _diag_re.match(line)
        if not m:
            i += 1
            continue

        kind = "error" if "error:" in line.lower() else "warning"
        clean_line = _path_strip_re.sub("", line).strip()

        # Gather following context lines (symbol:, location:) until blank line
        # or next diagnostic.
        context_parts: list[str] = []
        j = i + 1
        while j < len(lines):
            ctx = lines[j].strip()
            if not ctx:
                break
            if _diag_re.match(lines[j]):
                break
            if ctx.startswith("symbol:") or ctx.startswith("location:"):
                context_parts.append(ctx)
            j += 1

        combined = clean_line
        if context_parts:
            combined += " — " + ", ".join(context_parts)

        if kind == "error":
            errors.append(combined)
        else:
            warnings.append(combined)

        i = j  # jump past the context lines we already consumed

    return errors, warnings


# Classes whose "cannot access" errors are purely transitive-classpath issues
# (missing 3rd-party JARs we don't ship), not bugs in the generated code.
_TRANSITIVE_CLASSES = {
    "BaseComponent",       # net.md_5.bungee.api.chat — Paper legacy wraps this
    "TextComponent",       # net.md_5.bungee.api.chat
    "BaseComponentSerializer",
    "ComponentSerializer",
    "HoverEvent",          # bungee hover
    "ClickEvent",          # bungee click
}

def _is_transitive_error(line: str) -> bool:
    """
    Return True if this javac error line is a transitive classpath issue
    (missing JAR from a Paper dependency) rather than an error in the
    generated code itself.

    javac emits "error: cannot access Foo" when it resolved Foo's parent
    class chain but couldn't load a class Foo transitively depends on.
    This is NEVER the user's fault — it means our compile-time classpath
    is incomplete.  We filter these so the AI doesn't try to 'fix' them.
    """
    if "error: cannot access" not in line.lower():
        return False
    # Check if it specifically mentions a known transitive class
    for cls in _TRANSITIVE_CLASSES:
        if cls in line:
            return True
    # Any "cannot access" whose class is in a known 3rd-party namespace
    return bool(re.search(
        r"cannot access\s+(?:net\.md_5|com\.google|org\.apache|io\.netty|"
        r"com\.mojang|net\.minecraft|org\.slf4j|com\.destroystokyo)\.",
        line,
        re.IGNORECASE,
    ))


# Matches any top-level public type declaration, including modifiers like abstract/final.
# Used for filename derivation and multi-class splitting.
_PUBLIC_CLASS_RE = re.compile(
    r"public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|enum|record)\s+(\w+)"
)


def _looks_like_java(code: str) -> bool:
    """Return True if the string appears to be Java source code."""
    return bool(re.search(
        r'(?:^|\n)\s*(?:package\s+[\w.]+\s*;'
        r'|import\s+[\w.*]+\s*;'
        r'|public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*'
        r'(?:class|interface|enum|record)\s+\w+)',
        code,
    ))


def _path_from_code(code: str, index: int) -> str:
    """Derive src/main/java/... path from package + class declarations.

    Handles modifiers (abstract, final, sealed, non-sealed, strictfp) that the
    old regex silently ignored, causing a fallback to GeneratedPlugin_N.java even
    when the class name was clearly present in the source.
    """
    pkg_match = re.search(r"^package\s+([\w.]+)\s*;", code, re.MULTILINE)
    cls_match = _PUBLIC_CLASS_RE.search(code)
    if pkg_match and cls_match:
        return f"src/main/java/{pkg_match.group(1).replace('.', '/')}/{cls_match.group(1)}.java"
    if cls_match:
        # Class name found but package declaration is absent — use com/example/ default.
        # Still better than GeneratedPlugin_N.java because at least the filename matches.
        return f"src/main/java/com/example/{cls_match.group(1)}.java"
    return f"src/main/java/com/example/GeneratedPlugin_{index}.java"


def _split_multi_class_block(code: str, base_index: int) -> list[tuple[str, str]]:
    """If a single code block contains multiple top-level public class declarations,
    split it into separate (path, code) pairs — one per public class — so javac
    doesn't raise 'class X is public, should be declared in a file named X.java'.

    Non-public helper classes are kept with the nearest preceding public class chunk.
    Returns a single-element list when no split is needed.
    """
    matches = list(_PUBLIC_CLASS_RE.finditer(code))
    if len(matches) <= 1:
        return [(_path_from_code(code, base_index), code)]

    # Everything before the first public class = preamble (package + imports).
    preamble = code[:matches[0].start()]

    result: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        if i == 0:
            # First class keeps the original preamble intact.
            full = code[:next_start].strip()
        else:
            chunk = code[m.start():next_start].strip()
            full = (preamble.rstrip() + "\n\n" + chunk).strip()
        result.append((_path_from_code(full, base_index + i), full))

    return result


def extract_java_blocks(response: str) -> list[tuple[str, str]]:
    """
    Extract (filepath, code) tuples from a model response.

    Priority order:
      1. ```java fenced blocks (canonical format)
      2. Unlabelled ``` or ```text/plaintext/code fenced blocks whose content looks
         like Java (handles models that forget the language tag)
      3. Entire response body if it looks like raw unfenced Java
    """
    results: list[tuple[str, str]] = []

    # ── 1. Canonical ```java blocks ─────────────────────────────────────── #
    # (?:```|\Z) lets the pattern match truncated responses (token-limit cutoff
    # means no closing fence) so we never fall back to writing ```java into a
    # .java file, which causes "illegal character: '`'" compile errors.
    pattern = re.compile(r"```java[ \t]*\n(?://[ \t]*([\w./\-]+\.java)[ \t]*\n)?(.*?)(?:```|\Z)", re.DOTALL)

    for match in pattern.finditer(response):
        raw_path = match.group(1)
        code = match.group(2).strip()

        if not code:
            continue

        if raw_path:
            path = raw_path.strip()
            # Normalise to src/main/java/... if it's just a bare filename.
            # IMPORTANT: use the package declared in the code itself rather than
            # blindly placing the file in com/example/ — otherwise a plugin with
            # package com.example.myplugin and hint comment '// MyPlugin.java'
            # ends up at com/example/MyPlugin.java and javac errors on the
            # package/path mismatch.
            if "/" not in path and "\\" not in path:
                derived = _path_from_code(code, len(results))
                if "GeneratedPlugin_" not in derived:
                    path = derived
                else:
                    path = f"src/main/java/com/example/{path}"
        else:
            path = _path_from_code(code, len(results))

        # If the block contains multiple top-level public classes, split it into
        # separate files (re-deriving paths per class).  For single-class blocks
        # honour the explicit path computed above.
        splits = _split_multi_class_block(code, len(results))
        if len(splits) > 1:
            results.extend(splits)
        else:
            results.append((path, code))

    if results:
        return results

    # ── 2. Any fenced block whose content looks like Java ───────────────── #
    fallback_pat = re.compile(
        r"```(?:text|plaintext|code|java)?[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL | re.IGNORECASE
    )
    for match in fallback_pat.finditer(response):
        code = match.group(1).strip()
        if code and _looks_like_java(code):
            results.extend(_split_multi_class_block(code, len(results)))

    if results:
        return results

    # ── 3. Entire response is raw unfenced Java ──────────────────────────── #
    stripped = response.strip()
    # If the AI started a markdown fence but hit the token limit before the
    # closing ```, the response looks like "```java\npackage ...".  Strip the
    # opening fence line so we don't write backticks into the .java file.
    if stripped.startswith("```"):
        first_nl = stripped.find('\n')
        if first_nl != -1:
            stripped = stripped[first_nl + 1:]
    if _looks_like_java(stripped):
        results.extend(_split_multi_class_block(stripped, 0))

    return results


def _is_test_like_block(path: str, code: str) -> bool:
    """
    Return True when a Java block looks like test-only code.

    Some model responses emit test stubs in plain ```java blocks with generic
    class names (e.g. GeneratedPlugin_1) instead of *Test suffixes or src/test
    paths. Those should be compiled in compile_tests(), not as runtime plugin
    sources in compile_response().
    """
    low = code.lower()
    if "src/test/" in path:
        return True

    cls_match = re.search(r"public\s+class\s+(\w+)", code)
    if cls_match and cls_match.group(1).endswith("Test"):
        return True

    test_markers = (
        "import org.junit",
        "@test",
        # NOTE: bare "assert" removed — it matches Java assert statements and method
        # names like assertNotNull() in legitimate plugins, causing false positives.
        # JUnit tests are already caught by "import org.junit" above.
        "import be.seeseemelk.mockbukkit",
        "servermock",
        "mockbukkit",
        "import org.mockito",
        "@mock",
    )
    return any(marker in low for marker in test_markers)


def compile_response(response: str, paper_profile: str = DEFAULT_PAPER_PROFILE) -> CompileResult:
    """
    Extract Java code from a model response and compile it against Paper API.
    Returns a CompileResult with success flag and any error messages.
    """
    cfg = _target_cfg(paper_profile)
    source_version = str(cfg["source"])
    target_version = str(cfg["target"])
    java_required = int(cfg["java_required"])

    javac = _find_javac(required_major=java_required)
    if not javac:
        return CompileResult(
            success=False,
            errors=[f"javac {java_required}+ not found on PATH. Install Java {java_required}."],
        )

    # Always call ensure_paper_jar — it also runs ensure_extra_jars() so any
    # newly added _EXTRA_JARS entries get downloaded even when the Paper JAR
    # already exists on disk.
    if not ensure_paper_jar(paper_profile=paper_profile):
        return CompileResult(
            success=False,
            errors=["Paper API JAR unavailable. Check network connectivity."],
        )

    java_files = extract_java_blocks(response)
    if not java_files:
        return CompileResult(
            success=False,
            errors=["No Java code blocks found in model response."],
        )

    runtime_files = [(path, code) for path, code in java_files if not _is_test_like_block(path, code)]
    if not runtime_files:
        return CompileResult(
            success=False,
            errors=["No runtime Java source files found (only test-like blocks were detected)."],
        )

    with tempfile.TemporaryDirectory(prefix="stacknest_") as tmpdir:
        src_root = pathlib.Path(tmpdir) / "src"
        written: list[pathlib.Path] = []

        for rel_path, code in runtime_files:
            full_path = src_root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(code, encoding="utf-8")
            written.append(full_path)

        classpath = _build_classpath()
        cmd = [
            javac,
            "-cp", classpath,
            "-source", source_version,
            "-target", target_version,
            "-encoding", "UTF-8",
            "-Xlint:all",          # Enable all warnings
            "-Xlint:-options",     # Suppress cross-compilation warnings
        ] + [str(f) for f in written]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmpdir,
        )

        stderr = result.stderr or ""
        raw_errors, warnings = _parse_javac_errors(stderr)

        # Filter out transitive-classpath errors ("cannot access X").
        # These mean a dependency JAR is missing from *our* compile-time classpath,
        # not that the user's code is wrong.  They are never actionable by the AI.
        errors = [e for e in raw_errors if not _is_transitive_error(e)]

        # If we filtered errors and javac exited non-zero due solely to those
        # transitive issues, treat the compile as successful.
        success = (result.returncode == 0) or (len(raw_errors) > 0 and len(errors) == 0)

        # When javac exits non-zero but _parse_javac_errors found nothing (output
        # format didn't match — e.g. JVM ICE, annotation-processor crash, or
        # javac writing to stdout instead of stderr) capture the raw output so
        # the feedback loop has something to act on rather than a silent failure.
        if not success and not errors:
            raw_out = (result.stdout or "").strip()
            raw_err = (result.stderr or "").strip()
            fallback = raw_err or raw_out
            if fallback:
                # Trim to a reasonable length and prepend context.
                trimmed = fallback[:600]
                errors = [f"javac exited {result.returncode} with no parsed diagnostics. Raw output: {trimmed}"]

        return CompileResult(
            success=success,
            errors=errors,
            warnings=warnings,
            files_compiled=len(written),
            temp_dir=tmpdir if not success else "",
        )


def extract_test_blocks(response: str) -> list[tuple[str, str]]:
    """
    Extract (filepath, code) tuples for test classes.
    Looks for ```java blocks whose class name ends in 'Test' or
    whose path sits under src/test/.
    """
    all_blocks = extract_java_blocks(response)
    test_blocks: list[tuple[str, str]] = []
    for path, code in all_blocks:
        if _is_test_like_block(path, code):
            # Ensure path is under src/test/java/
            if "src/test/" not in path:
                path = path.replace("src/main/", "src/test/")
            test_blocks.append((path, code))
    return test_blocks


# Optional MockBukkit JAR — only needed to compile test classes
MOCKBUKKIT_JAR = pathlib.Path("libs/mockbukkit-v1.21.jar")
JUNIT_JAR = pathlib.Path("libs/junit-jupiter-api-5.11.3.jar")


def compile_tests(response: str, paper_profile: str = DEFAULT_PAPER_PROFILE) -> CompileResult:
    """
    Compile test classes extracted from the model response against Paper API +
    MockBukkit + JUnit 5.  Returns CompileResult(success=True, files_compiled=0)
    gracefully if MockBukkit / JUnit JARs are absent (non-fatal).
    """
    cfg = _target_cfg(paper_profile)
    source_version = str(cfg["source"])
    target_version = str(cfg["target"])
    java_required = int(cfg["java_required"])

    # MockBukkit target is 1.21-focused; skip strict test compile for 26.x profile.
    if paper_profile != PAPER_PROFILE_1_21:
        return CompileResult(success=True, files_compiled=0)

    javac = _find_javac(required_major=java_required)
    if not javac:
        return CompileResult(success=False, errors=[f"javac {java_required}+ not found on PATH."])

    test_files = extract_test_blocks(response)
    if not test_files:
        return CompileResult(success=True, files_compiled=0)  # Nothing to compile

    extra = []
    if MOCKBUKKIT_JAR.exists():
        extra.append(str(MOCKBUKKIT_JAR.resolve()))
    if JUNIT_JAR.exists():
        extra.append(str(JUNIT_JAR.resolve()))

    paper_api_jar = pathlib.Path(str(cfg["jar"]))
    if not paper_api_jar.exists():
        if not ensure_paper_jar(paper_profile=paper_profile):
            return CompileResult(
                success=False,
                errors=[f"Paper API JAR unavailable."],
            )

    sep = ";" if platform.system() == "Windows" else ":"
    classpath = _build_classpath()
    if extra:
        classpath = classpath + sep + sep.join(extra)

    with tempfile.TemporaryDirectory(prefix="stacknest_test_") as tmpdir:
        src_root = pathlib.Path(tmpdir) / "src"
        written: list[pathlib.Path] = []

        for rel_path, code in test_files:
            full_path = src_root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(code, encoding="utf-8")
            written.append(full_path)

        cmd = [
            javac,
            "-cp", classpath,
            "-source", source_version,
            "-target", target_version,
            "-encoding", "UTF-8",
        ] + [str(f) for f in written]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=tmpdir
        )

        stderr = result.stderr or ""
        raw_errors, warnings = _parse_javac_errors(stderr)
        errors     = [e for e in raw_errors if not _is_transitive_error(e)]
        success    = (result.returncode == 0) or (len(raw_errors) > 0 and len(errors) == 0)

        return CompileResult(
            success=success,
            errors=errors,
            warnings=warnings,
            files_compiled=len(written),
        )


def format_errors_for_prompt(result: CompileResult) -> str:
    """Format compilation errors into a concise string for the correction prompt."""
    if result.success:
        return ""
    lines = ["Compilation errors:"]
    for e in result.errors[:10]:  # Limit to 10 errors to stay within token budget
        # Strip the full path prefix — model doesn't need the temp dir path
        cleaned = re.sub(r"/tmp/stacknest_\w+/src/", "", e)
        lines.append(f"  {cleaned}")
    if len(result.errors) > 10:
        lines.append(f"  ... and {len(result.errors) - 10} more errors")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JAR builder
# ---------------------------------------------------------------------------

def build_jar(
    response: str,
    plugin_name: str = "StackNestPlugin",
    paper_profile: str = DEFAULT_PAPER_PROFILE,
) -> bytes:
    """
    Compile Java code from a model response and package it into a deployable .jar.

    The resulting .jar contains compiled .class files + plugin.yml and can be
    dropped directly into a Minecraft server's plugins/ folder.

    Returns the .jar file as bytes, or raises RuntimeError on failure.
    """
    cfg = _target_cfg(paper_profile)
    source_version = str(cfg["source"])
    target_version = str(cfg["target"])
    java_required = int(cfg["java_required"])

    javac = _find_javac(required_major=java_required)
    if not javac:
        raise RuntimeError(
            f"javac {java_required}+ not found on PATH. Install Java {java_required}."
        )

    jar_cmd = shutil.which("jar")
    if not jar_cmd:
        raise RuntimeError("'jar' tool not found on PATH — required for packaging.")

    paper_api_jar = pathlib.Path(str(cfg["jar"]))
    if not paper_api_jar.exists():
        if not ensure_paper_jar(paper_profile=paper_profile):
            raise RuntimeError("Paper API JAR unavailable — cannot compile.")

    java_files = extract_java_blocks(response)
    if not java_files:
        raise RuntimeError("No Java code blocks found in response.")

    # Strip test classes — they depend on MockBukkit/JUnit which aren't on the
    # compile classpath and must never end up in a production jar anyway.
    _TEST_SIGNALS = (
        "import org.junit",
        "import be.seeseemelk.mockbukkit",
        "@Test",
        "extends MockBukkit",
        "MockBukkitExtension",
    )
    def _is_test_file(path: str, code: str) -> bool:
        p = path.replace("\\", "/").lower()
        if "/test/" in p or p.endswith("test.java") or "test" in p.split("/")[-1].lower():
            return True
        return any(sig.lower() in code.lower() for sig in _TEST_SIGNALS)

    production_files = [(p, c) for p, c in java_files if not _is_test_file(p, c)]
    if not production_files:
        raise RuntimeError("No Java code blocks found in response.")
    java_files = production_files

    # Detect truncated responses — if any file looks cut off (no closing brace,
    # fewer than 5 lines) the jar would silently compile to a broken/empty plugin.
    _TRUNCATION_THRESHOLD = 5  # lines
    truncated = []
    for path, code in java_files:
        lines = [l for l in code.splitlines() if l.strip()]
        if len(lines) < _TRUNCATION_THRESHOLD:
            truncated.append(path)
        # Check for unbalanced braces — strong sign of a cut-off block
        elif code.count('{') > code.count('}') + 2:
            truncated.append(path)
    if truncated:
        names = ", ".join(p.split("/")[-1] for p in truncated)
        raise RuntimeError(
            f"The generated code appears to be truncated ({names}). "
            "This usually means the plugin is too large for one response. "
            "Try asking for a simpler version, or break it into smaller features and use Refine to add them."
        )

    # Extract plugin.yml
    yml_match = re.search(r"```ya?ml\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if not yml_match:
        raise RuntimeError("No plugin.yml block found in response. Regenerate and try again.")
    plugin_yml = yml_match.group(1)

    safe_name = re.sub(r"[^\w\-]", "", plugin_name.strip()) or "StackNestPlugin"

    with tempfile.TemporaryDirectory(prefix="stacknest_jar_") as tmpdir:
        tmppath = pathlib.Path(tmpdir)
        src_root = tmppath / "src"
        cls_root = tmppath / "classes"
        cls_root.mkdir()

        # Write source files
        written: list[pathlib.Path] = []
        for rel_path, code in java_files:
            full_path = src_root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(code, encoding="utf-8")
            written.append(full_path)

        # Compile to classes/
        classpath = _build_classpath()
        compile_proc = subprocess.run(
            [
                javac,
                "-cp", classpath,
                "-source", source_version,
                "-target", target_version,
                "-encoding", "UTF-8",
                "-d", str(cls_root),
            ] + [str(f) for f in written],
            capture_output=True, text=True, timeout=60, cwd=tmpdir,
        )

        if compile_proc.returncode != 0:
            raw_errors = [
                re.sub(r"/tmp/stacknest_jar_\w+/src/", "", l)
                for l in compile_proc.stderr.splitlines()
                if "error:" in l.lower()
            ]
            # Filter out transitive-classpath false-positives (e.g. "cannot access BaseComponent")
            errors = [e for e in raw_errors if not _is_transitive_error(e)]
            if errors:
                raise RuntimeError("Compilation failed:\n" + "\n".join(errors[:8]))
            # Only transitive issues filtered — but if no .class files were produced
            # javac still failed; surface the raw stderr rather than making an empty jar
            class_files = list(cls_root.rglob("*.class"))
            if not class_files:
                raise RuntimeError(
                    "Compilation failed (no class files produced):\n" +
                    "\n".join(compile_proc.stderr.splitlines()[:8])
                )

        # Write plugin.yml into classes/ root (Minecraft expects it at jar root)
        (cls_root / "plugin.yml").write_text(plugin_yml, encoding="utf-8")

        # Safety net: never package a jar with no compiled classes
        class_files = list(cls_root.rglob("*.class"))
        if not class_files:
            raise RuntimeError(
                "No compiled class files found after build. "
                "The plugin code may be invalid or the compiler produced no output. "
                "Try regenerating."
            )

        # Package into .jar
        jar_path = tmppath / f"{safe_name}.jar"
        jar_proc = subprocess.run(
            [jar_cmd, "--create", "--file", str(jar_path), "-C", str(cls_root), "."],
            capture_output=True, text=True, timeout=30, cwd=tmpdir,
        )

        if jar_proc.returncode != 0:
            raise RuntimeError(f"JAR packaging failed: {jar_proc.stderr.strip()}")

        return jar_path.read_bytes()
