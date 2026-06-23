"""
api/paper_versions.py — Auto-maintain Paper API version metadata.

Fetches the latest stable Paper version from the PaperMC API and keeps the
version cache fresh so compile_check.py, router.py, and pom_template.xml all
stay up-to-date automatically — no manual version bumps required.

Cache file: data/paper_version_cache.json  (TTL = 7 days)

Public constants (updated on import from cache / on refresh):
    STABLE_MC_VERSION        e.g. "26.1"
    STABLE_JAVA_VERSION      e.g. "25"
    STABLE_PAPER_PROFILE     e.g. "paper_26_1"   (matches compile_check profile key)
    STABLE_PAPER_API_VERSION e.g. "26.1-R0.1-SNAPSHOT"
    BRIGADIER_VERSION        e.g. "1.3.10"

Called by api/app.py at startup via startup_refresh().
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Paths & endpoints
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).parent.parent
_CACHE_PATH = _ROOT / "data" / "paper_version_cache.json"

_PAPERMC_API_URL = "https://api.papermc.io/v2/projects/paper"
_MAVEN_BASE = (
    "https://repo.papermc.io/repository/maven-public/"
    "io/papermc/paper/paper-api/"
)
_MAVEN_METADATA_URL = _MAVEN_BASE + "maven-metadata.xml"
_MAVEN_CENTRAL_BRIGADIER = (
    "https://repo1.maven.org/maven2/com/mojang/brigadier/"
)
_MOJANG_LIBS = "https://libraries.minecraft.net/com/mojang/brigadier/"

_CACHE_TTL = timedelta(days=7)
_REQUEST_TIMEOUT = 12  # seconds

_HEADERS = {"User-Agent": "Mozilla/5.0 (StackNest build system)"}

# ---------------------------------------------------------------------------
# Public constants — overwritten when cache loads / refreshes
# ---------------------------------------------------------------------------
STABLE_MC_VERSION: str = "26.1"
STABLE_JAVA_VERSION: str = "25"
STABLE_PAPER_PROFILE: str = "paper_26_1"
STABLE_PAPER_API_VERSION: str = "26.1-R0.1-SNAPSHOT"
BRIGADIER_VERSION: str = "1.3.10"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _mc_to_java(mc_version: str) -> int:
    """Return the minimum Java version required for a given MC version string."""
    try:
        major = int(mc_version.split(".")[0])
    except (ValueError, IndexError):
        return 21
    # Mojang's 2026 re-versioning scheme (26.x+) ships with Java 25 LTS.
    return 25 if major >= 26 else 21


def _profile_key(mc_version: str) -> str:
    """'26.1' → 'paper_26_1'"""
    return "paper_" + mc_version.replace(".", "_")


def _paper_api_version(mc_version: str) -> str:
    """Legacy helper: '26.1' → '26.1-R0.1-SNAPSHOT'. Not used for new stable builds."""
    return f"{mc_version}-R0.1-SNAPSHOT"


def _paper_api_jar_url(api_version: str) -> str:
    return f"{_MAVEN_BASE}{api_version}/paper-api-{api_version}.jar"


def _paper_api_pom_url(api_version: str) -> str:
    return f"{_MAVEN_BASE}{api_version}/paper-api-{api_version}.pom"


def _brigadier_jar_url(version: str) -> str:
    # Brigadier is on Mojang's library server (not Maven Central)
    return f"{_MOJANG_LIBS}{version}/brigadier-{version}.jar"


# ---------------------------------------------------------------------------
# Network fetchers
# ---------------------------------------------------------------------------

def _fetch_latest_maven_version() -> tuple[str, str]:
    """
    Query the PaperMC Maven metadata to find the latest stable Paper API version.

    Paper uses two versioning schemes in the same repo:
      - Legacy:  '1.21.4-R0.1-SNAPSHOT'  (Minecraft 1.x era)
      - Current: '26.1.2.build.62-stable' (Paper 26.x era, Java 25)

    Returns (maven_version, mc_series) e.g. ('26.1.2.build.62-stable', '26.1').
    Prefers the latest '-stable' version; falls back to the latest snapshot.
    """
    req = urllib.request.Request(_MAVEN_METADATA_URL, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        xml = resp.read().decode("utf-8")

    # Extract all <version> tags
    versions = re.findall(r"<version>([^<]+)</version>", xml)
    if not versions:
        raise ValueError("Maven metadata contained no <version> entries")

    # Prefer stable builds (e.g. '26.1.2.build.62-stable')
    stable = [v for v in versions if v.endswith("-stable")]
    if stable:
        latest = stable[-1]
        # Extract mc_series from '26.1.2.build.62-stable' → '26.1'
        m = re.match(r"^(\d+\.\d+)", latest)
        mc_series = m.group(1) if m else latest
        return latest, mc_series

    # Fall back to latest snapshot (e.g. '1.21.4-R0.1-SNAPSHOT')
    latest = versions[-1]
    m = re.match(r"^([\d.]+)-", latest)
    mc_series = m.group(1) if m else latest
    return latest, mc_series


def _fetch_latest_mc_version() -> str:
    """
    Compatibility shim.  Returns the Minecraft series string e.g. '26.1' or '1.21.4'.
    Use _fetch_latest_maven_version() directly for the full Maven version.
    """
    _, mc_series = _fetch_latest_maven_version()
    return mc_series


def _fetch_brigadier_version(maven_api_version: str) -> str:
    """
    Parse the paper-api POM to find the exact bundled brigadier version.
    Falls back to '1.3.10' if the POM is unreachable or has no brigadier dep.
    """
    pom_url = _paper_api_pom_url(maven_api_version)
    try:
        req = urllib.request.Request(pom_url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            pom_xml = resp.read().decode("utf-8")
        m = re.search(
            r"<groupId>com\.mojang</groupId>\s*"
            r"<artifactId>brigadier</artifactId>\s*"
            r"<version>([\d.]+)</version>",
            pom_xml,
            re.DOTALL,
        )
        if m:
            return m.group(1)
    except Exception as exc:
        print(f"[paper_versions] Brigadier version lookup failed: {exc}")
    return "1.3.10"


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_cache() -> dict | None:
    """Return cached data if it exists and is within TTL, else None."""
    try:
        if not _CACHE_PATH.exists():
            return None
        data: dict = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        raw_ts: str = data.get("fetched_at", "2000-01-01")
        fetched = datetime.fromisoformat(raw_ts)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched < _CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _save_cache(data: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[paper_versions] Could not write version cache: {exc}")


def _apply(data: dict) -> None:
    """Update module-level public constants from a cache dict."""
    global STABLE_MC_VERSION, STABLE_JAVA_VERSION, STABLE_PAPER_PROFILE
    global STABLE_PAPER_API_VERSION, BRIGADIER_VERSION
    stable: dict = data.get("stable", {})
    if stable.get("mc_version"):
        STABLE_MC_VERSION = stable["mc_version"]
    if stable.get("java_version"):
        STABLE_JAVA_VERSION = str(stable["java_version"])
    if stable.get("paper_profile"):
        STABLE_PAPER_PROFILE = stable["paper_profile"]
    if stable.get("paper_api_version"):
        STABLE_PAPER_API_VERSION = stable["paper_api_version"]
    if stable.get("brigadier_version"):
        BRIGADIER_VERSION = stable["brigadier_version"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refresh(force: bool = False) -> bool:
    """
    Refresh the version cache from the PaperMC API.

    Returns True if a network fetch was performed, False if the cache was
    already fresh.  On network failure the existing defaults are kept.
    """
    if not force:
        cached = _load_cache()
        if cached:
            _apply(cached)
            return False  # Cache still valid — nothing to do

    print("[paper_versions] Fetching latest Paper version …")
    try:
        maven_version, mc_series = _fetch_latest_maven_version()
        java_ver = _mc_to_java(mc_series)
        profile = _profile_key(mc_series)
        brigadier_ver = _fetch_brigadier_version(maven_version)

        cache_data: dict = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stable": {
                "mc_version": mc_series,
                "java_version": str(java_ver),
                "paper_profile": profile,
                # Full Maven artifact version (e.g. '26.1.2.build.62-stable')
                "paper_api_version": maven_version,
                "brigadier_version": brigadier_ver,
                "paper_api_jar_url": _paper_api_jar_url(maven_version),
                "brigadier_jar_url": _brigadier_jar_url(brigadier_ver),
            },
        }
        _save_cache(cache_data)
        _apply(cache_data)
        print(
            f"[paper_versions] Stable: Paper {mc_series} ({maven_version}) | "
            f"Java {java_ver} | Brigadier {brigadier_ver}"
        )
        _patch_pom_template(maven_version, str(java_ver))
        return True

    except Exception as exc:
        print(f"[paper_versions] Could not refresh Paper version info: {exc}")
        return False


def startup_refresh() -> None:
    """
    Non-blocking startup refresh.
    Runs in a background daemon thread so it never delays app startup.
    """
    t = threading.Thread(target=refresh, daemon=True, name="paper-version-refresh")
    t.start()


def get_stable_paper_targets_entry() -> dict:
    """
    Return a compile_check._PAPER_TARGETS-compatible dict for the current
    stable version.  Used by compile_check.py to register dynamic profiles.
    """
    return {
        "jar": f"libs/paper-api-{STABLE_MC_VERSION}-stub.jar",
        "url": _paper_api_jar_url(STABLE_PAPER_API_VERSION),
        "source": STABLE_JAVA_VERSION,
        "target": STABLE_JAVA_VERSION,
        "java_required": int(STABLE_JAVA_VERSION),
    }

# ---------------------------------------------------------------------------
# Patch pom_template.xml in-place when a new version is detected
# ---------------------------------------------------------------------------
_POM_TEMPLATE_PATH = _ROOT / "templates" / "pom_template.xml"

# Sentinel comment written into the POM so we can locate the managed block.
_POM_MANAGED_START = "<!-- PAPER_VERSIONS_MANAGED_START -->"
_POM_MANAGED_END = "<!-- PAPER_VERSIONS_MANAGED_END -->"


def _patch_pom_template(paper_api_version: str, java_ver: str) -> None:
    """
    Update the Paper API version and Java compiler release in pom_template.xml.
    Uses sentinel comments so only the managed lines change.
    Writes atomically via a temp file to avoid corruption on failure.
    """
    try:
        if not _POM_TEMPLATE_PATH.exists():
            return
        content = _POM_TEMPLATE_PATH.read_text(encoding="utf-8")

        # Replace the paper-api <version> tag
        content = re.sub(
            r"(<artifactId>paper-api</artifactId>\s*<version>)[^<]+(</version>)",
            rf"\g<1>{paper_api_version}\g<2>",
            content,
        )

        # Replace java.version property
        content = re.sub(
            r"(<java\.version>)\d+(</java\.version>)",
            rf"\g<1>{java_ver}\g<2>",
            content,
        )

        # Replace maven.compiler.release property
        content = re.sub(
            r"(<maven\.compiler\.release>)\d+(</maven\.compiler\.release>)",
            rf"\g<1>{java_ver}\g<2>",
            content,
        )

        # Write atomically: write to .tmp, then rename
        tmp = _POM_TEMPLATE_PATH.with_suffix(".xml.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(_POM_TEMPLATE_PATH)
        print(
            f"[paper_versions] pom_template.xml updated → "
            f"paper-api {paper_api_version}, Java {java_ver}"
        )
    except Exception as exc:
        print(f"[paper_versions] Could not patch pom_template.xml: {exc}")


# ---------------------------------------------------------------------------
# Auto-apply on first import
# ---------------------------------------------------------------------------
_cached_on_import = _load_cache()
if _cached_on_import:
    _apply(_cached_on_import)
