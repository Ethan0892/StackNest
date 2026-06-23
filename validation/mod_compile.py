"""
validation/mod_compile.py — Compile Fabric / Forge / NeoForge mods via Gradle.

Strategy
--------
1. Ensure a Gradle 8.8 binary is available (downloads once to libs/).
2. Write the AI-generated source + metadata + build.gradle.kts to a temp dir.
3. Inject a static settings.gradle.kts with the correct plugin repos.
4. Run `gradle build --no-daemon -x test`.
5. Parse javac diagnostics from Gradle's output.
6. Return ModCompileResult(success, errors, jar_bytes, jar_name).

Gradle user home is pinned to libs/gradle-home/ so dependency JARs are cached
across builds.  First build per loader downloads mod deps (~200-500 MB).
Subsequent builds use the cache and typically finish in 15-45 s.
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field

# Resolve paths relative to this file's location (project root / validation /)
_REPO_ROOT      = pathlib.Path(__file__).parent.parent
LIBS_DIR        = _REPO_ROOT / "libs"
_GRADLE_VERSION = "8.8"
_GRADLE_DIR     = LIBS_DIR / f"gradle-{_GRADLE_VERSION}"
_GRADLE_BIN     = _GRADLE_DIR / "bin" / "gradle"
_GRADLE_HOME    = LIBS_DIR / "gradle-home"   # persistent dep cache
_GRADLE_ZIP_URL = (
    f"https://services.gradle.org/distributions/"
    f"gradle-{_GRADLE_VERSION}-bin.zip"
)


# --------------------------------------------------------------------------- #
# Result type                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class ModCompileResult:
    success:        bool | None   # None = skipped/unavailable, True = OK, False = failed
    errors:         list[str] = field(default_factory=list)
    warnings:       list[str] = field(default_factory=list)
    jar_bytes:      bytes | None = None
    jar_name:       str = ""
    files_compiled: int = 0


# --------------------------------------------------------------------------- #
# Gradle bootstrap                                                             #
# --------------------------------------------------------------------------- #

def _ensure_gradle() -> "str | None":
    """
    Return a path to a Gradle 8+ binary.
    Checks PATH first, then the previously downloaded copy, then downloads.
    """
    # 1. System PATH
    sys_g = shutil.which("gradle")
    if sys_g:
        try:
            out = subprocess.run(
                [sys_g, "--version"], capture_output=True, text=True, timeout=15
            ).stdout
            m = re.search(r"Gradle\s+(\d+)", out)
            if m and int(m.group(1)) >= 8:
                return sys_g
        except Exception:
            pass

    # 2. Previously downloaded
    if _GRADLE_BIN.exists():
        return str(_GRADLE_BIN)

    # 3. Download
    LIBS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = LIBS_DIR / f"gradle-{_GRADLE_VERSION}-bin.zip"

    if not zip_path.exists():
        print(f"[mod_compile] Downloading Gradle {_GRADLE_VERSION} (~130 MB)…")
        try:
            req = urllib.request.Request(
                _GRADLE_ZIP_URL,
                headers={"User-Agent": "StackNest-mod-compiler"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp, \
                    open(zip_path, "wb") as fh:
                fh.write(resp.read())
            print(f"[mod_compile] Gradle downloaded ({zip_path.stat().st_size // 1024} KB)")
        except Exception as exc:
            print(f"[mod_compile] Gradle download failed: {exc}")
            return None

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(LIBS_DIR)
    except Exception as exc:
        print(f"[mod_compile] Gradle extraction failed: {exc}")
        return None

    if _GRADLE_BIN.exists():
        try:
            _GRADLE_BIN.chmod(0o755)
        except Exception:
            pass
        return str(_GRADLE_BIN)

    return None


# --------------------------------------------------------------------------- #
# settings.gradle.kts templates (injected per loader; not AI-generated)       #
# --------------------------------------------------------------------------- #

_SETTINGS_KTS: dict[str, str] = {
    "fabric": (
        "pluginManagement {\n"
        "    repositories {\n"
        '        maven { url = uri("https://maven.fabricmc.net/") }\n'
        "        gradlePluginPortal()\n"
        "    }\n"
        "}\n"
        'rootProject.name = "generated-mod"\n'
    ),
    "forge": (
        "pluginManagement {\n"
        "    repositories {\n"
        '        maven { url = uri("https://maven.minecraftforge.net/") }\n'
        "        gradlePluginPortal()\n"
        "    }\n"
        "}\n"
        'rootProject.name = "generated-mod"\n'
    ),
    "neoforge": (
        "pluginManagement {\n"
        "    repositories {\n"
        '        maven { url = uri("https://maven.neoforged.net/releases/") }\n'
        "        gradlePluginPortal()\n"
        "    }\n"
        "}\n"
        "plugins {\n"
        '    id("org.gradle.toolchains.foojay-resolver-convention") version "0.8.0"\n'
        "}\n"
        'rootProject.name = "generated-mod"\n'
    ),
}


# --------------------------------------------------------------------------- #
# Error parsing                                                                #
# --------------------------------------------------------------------------- #

def _parse_gradle_errors(output: str) -> tuple[list[str], list[str]]:
    """
    Extract javac-style diagnostics from Gradle build output.
    Returns (errors, warnings).
    """
    errors:   list[str] = []
    warnings: list[str] = []

    _path_strip = re.compile(r"^.*/src/(?:main|test)/java/", re.IGNORECASE)
    _diag_re    = re.compile(
        r"^.+\.java:\d+:\s+(?:error|warning):\s+.+",
        re.MULTILINE | re.IGNORECASE,
    )
    _ctx_re = re.compile(r"^\s+(?:symbol|location):\s+.+")

    lines = output.splitlines()
    i = 0
    while i < len(lines):
        if _diag_re.match(lines[i]):
            kind  = "error" if "error:" in lines[i].lower() else "warning"
            clean = _path_strip.sub("", lines[i]).strip()
            ctx:  list[str] = []
            j = i + 1
            while j < len(lines) and _ctx_re.match(lines[j]):
                ctx.append(lines[j].strip())
                j += 1
            if ctx:
                clean += " — " + ", ".join(ctx)
            (errors if kind == "error" else warnings).append(clean)
            i = j
        else:
            i += 1

    # If no javac lines but build still failed, surface the summary line
    if not errors and "BUILD FAILED" in output:
        for line in output.splitlines():
            s = line.strip()
            if (s.startswith("> ") or "error:" in s.lower()) and 10 < len(s) < 300:
                errors.append(s.lstrip("> "))
        if not errors:
            errors.append(
                "Gradle build failed — check that build.gradle.kts and "
                "metadata file are syntactically correct."
            )

    return errors, warnings


# --------------------------------------------------------------------------- #
# Main entry point                                                             #
# --------------------------------------------------------------------------- #

def compile_mod(
    response:   str,
    loader:     str,
    mc_version: str = "1.21",
    *,
    timeout:    int = 600,
) -> ModCompileResult:
    """
    Compile a generated mod response via Gradle.

    Args:
        response:   Full model response (```java + ```json/toml + ```gradle blocks).
        loader:     'fabric', 'forge', or 'neoforge'.
        mc_version: Minecraft version string, e.g. '1.21'.
        timeout:    Gradle build timeout in seconds.
                    First build per loader downloads deps — allow ≥600 s.

    Returns:
        ModCompileResult.  On success jar_bytes and jar_name are populated.
    """
    from validation.compile_check import extract_java_blocks  # reuse parser

    gradle = _ensure_gradle()
    if not gradle:
        return ModCompileResult(
            success=None,   # None = skipped (not a failure), Gradle simply not installed
            errors=[
                "Gradle binary not available — mod compilation skipped. "
                "Ensure the server has internet access so Gradle can be downloaded."
            ],
        )

    loader = loader.lower()

    # ── Extract blocks from response ──────────────────────────────────────── #

    java_blocks = extract_java_blocks(response)     # [(rel_path, code), ...]
    if not java_blocks:
        return ModCompileResult(
            success=False,
            errors=["No Java source blocks found in the response."],
        )

    # build.gradle.kts  (gradle / groovy / kotlin fence)
    gradle_src = ""
    m = re.search(r"```(?:gradle|groovy|kotlin)[ \t]*\n(.*?)```", response, re.DOTALL)
    if m:
        gradle_src = m.group(1)

    # metadata file
    if loader == "fabric":
        meta_re  = re.compile(r"```json[ \t]*\n(.*?)```", re.DOTALL)
        meta_rel = "src/main/resources/fabric.mod.json"
    else:
        meta_re  = re.compile(r"```toml[ \t]*\n(.*?)```", re.DOTALL)
        suffix   = "neoforge.mods.toml" if loader == "neoforge" else "mods.toml"
        meta_rel = f"src/main/resources/META-INF/{suffix}"

    meta_src = ""
    m = meta_re.search(response)
    if m:
        meta_src = m.group(1)

    # ── Build temp Gradle project ──────────────────────────────────────────── #

    _GRADLE_HOME.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="stacknest_mod_") as tmpdir:
        root = pathlib.Path(tmpdir)

        # settings.gradle.kts (static, loader-specific plugin repos)
        (root / "settings.gradle.kts").write_text(
            _SETTINGS_KTS.get(loader, _SETTINGS_KTS["fabric"]),
            encoding="utf-8",
        )

        # build.gradle.kts (AI-generated or template fallback)
        if gradle_src:
            (root / "build.gradle.kts").write_text(gradle_src, encoding="utf-8")
        else:
            try:
                from inference.router import _load_mod_gradle_template
                tpl = _load_mod_gradle_template(loader)
            except Exception:
                tpl = ""
            (root / "build.gradle.kts").write_text(tpl or "", encoding="utf-8")

        # Java source files
        for rel_path, code in java_blocks:
            dest = root / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")

        # Metadata (fabric.mod.json / mods.toml)
        if meta_src:
            meta_dest = root / meta_rel
            meta_dest.parent.mkdir(parents=True, exist_ok=True)
            meta_dest.write_text(meta_src, encoding="utf-8")

        # ── Run Gradle ────────────────────────────────────────────────────── #

        env = {
            **os.environ,
            "GRADLE_USER_HOME": str(_GRADLE_HOME.resolve()),
            "GRADLE_OPTS":      "-Dorg.gradle.welcome=never -Xmx1g",
        }

        try:
            proc = subprocess.run(
                [
                    gradle,
                    "build",
                    "--no-daemon",
                    "-x", "test",
                    "--configuration-cache=off",
                    "--warning-mode=none",
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ModCompileResult(
                success=False,
                errors=[
                    f"Gradle build timed out after {timeout}s. "
                    "The first build downloads mod dependencies — allow more time "
                    "or ensure the server has a fast internet connection."
                ],
            )

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        errors, warnings = _parse_gradle_errors(output)

        if proc.returncode != 0:
            return ModCompileResult(
                success=False,
                errors=errors or ["Gradle build failed."],
                warnings=warnings,
            )

        # ── Find output JAR ───────────────────────────────────────────────── #

        skip_suffixes = ("-sources", "-javadoc", "-dev", "-shadow", "-slim")
        jars = [
            p for p in (root / "build" / "libs").glob("*.jar")
            if not any(p.name.endswith(s + ".jar") or s in p.name for s in skip_suffixes)
        ]
        if not jars:
            return ModCompileResult(
                success=False,
                errors=["Build succeeded but no output JAR found in build/libs/."],
                warnings=warnings,
            )

        jar_path  = jars[0]
        jar_bytes = jar_path.read_bytes()

        return ModCompileResult(
            success        = True,
            errors         = [],
            warnings       = warnings,
            jar_bytes      = jar_bytes,
            jar_name       = jar_path.name,
            files_compiled = len(java_blocks),
        )
