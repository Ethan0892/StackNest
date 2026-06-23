"""
scripts/update_api_notes.py — Weekly API doc scraper for StackNest.

Fetches changelog / migration pages from:
  - Paper  (docs.papermc.io/paper/dev/api + GitHub releases)
  - Velocity (docs.papermc.io/velocity/dev + GitHub releases)
  - Fabric  (fabricmc.net/develop + GitHub releases)
  - Forge   (docs.minecraftforge.net + GitHub releases)
  - Adventure (GitHub releases for net.kyori:adventure-api)

Then asks Gemini to summarise only code-generation-relevant facts
(new API methods, renamed classes, removed classes, new annotations,
behaviour changes that break generated code) and writes the result to
data/api_notes.md, which inference/router.py appends to SYSTEM_PROMPT
at import time.

Run manually:
    python3 -m scripts.update_api_notes

The server runs this weekly via a systemd timer (deploy/stacknest-docs-update.timer).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

_ROOT = pathlib.Path(__file__).parent.parent
_OUT  = _ROOT / "data" / "api_notes.md"

# ── Sources to fetch ──────────────────────────────────────────────────────────
# Each entry: (label, url, selector_hint)
# selector_hint is a substring we look for to validate the page isn't empty.
#
# Paper and Velocity don't use GitHub Releases — they ship through the PaperMC
# Downloads API.  We also pull the Adventure 5 migration guide from GitHub and
# Forge/NeoForge from their Maven repos.
_SOURCES: list[tuple[str, str, str]] = [
    # Paper: latest supported versions from the downloads API
    ("Paper versions (PaperMC downloads API)",
     "https://api.papermc.io/v2/projects/paper",
     "versions"),
    # Velocity: latest supported versions from the downloads API
    ("Velocity versions (PaperMC downloads API)",
     "https://api.papermc.io/v2/projects/velocity",
     "versions"),
    # Adventure API — moved to PaperMC org; uses GitHub releases (well-maintained release notes)
    ("Adventure GitHub releases (latest 10)",
     "https://api.github.com/repos/PaperMC/adventure/releases?per_page=10",
     "tag_name"),
    # Fabric
    ("Fabric loader releases (latest 5)",
     "https://meta.fabricmc.net/v2/versions/loader?limit=5",
     "version"),
    ("Fabric API releases (latest 10)",
     "https://api.github.com/repos/FabricMC/fabric/releases?per_page=10",
     "tag_name"),
    # Forge promotions — maps MC version to recommended/latest Forge version
    ("Forge promotions",
     "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json",
     "latest"),
    # NeoForge Maven metadata — latest release version
    ("NeoForge Maven metadata",
     "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml",
     "release"),
]

# GitHub API requires a User-Agent header; token optional for higher rate limits
_GH_TOKEN = os.getenv("GITHUB_TOKEN", "")


def _fetch(url: str, hint: str) -> str | None:
    """Fetch a URL and return its body as text, or None on failure."""
    headers = {
        "User-Agent": "StackNest-DocUpdater/1.0 (https://stacknests.com)",
        "Accept": "application/json, text/html, */*",
    }
    if _GH_TOKEN and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {_GH_TOKEN}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        if hint not in body:
            print(f"  [warn] hint '{hint}' not found in response from {url}")
        return body
    except urllib.error.HTTPError as e:
        print(f"  [warn] HTTP {e.code} fetching {url}")
        return None
    except Exception as e:
        print(f"  [warn] failed to fetch {url}: {e}")
        return None


def _strip_html(text: str) -> str:
    """Very basic HTML stripping — removes tags and collapses whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"\s{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, chars: int = 4000) -> str:
    return text[:chars] + "…" if len(text) > chars else text


def _extract_release_notes(raw: str, label: str) -> str:
    """Extract release tag names + body snippets from a GitHub releases JSON."""
    try:
        releases = json.loads(raw)
    except Exception:
        return _truncate(raw)
    lines: list[str] = []
    for r in releases[:5]:
        tag  = r.get("tag_name", "?")
        body = (r.get("body") or "").strip()
        body = body[:600] + "…" if len(body) > 600 else body
        lines.append(f"### {tag}\n{body}" if body else f"### {tag}\n(no release notes)")
    return "\n\n".join(lines)


def _extract_papermc_api(raw: str, label: str) -> str:
    """Extract version list from the PaperMC Downloads API response."""
    try:
        data = json.loads(raw)
    except Exception:
        return _truncate(raw)
    versions = data.get("versions", [])
    if not versions:
        return "(no versions found)"
    latest = versions[-10:]   # last 10 (newest first in the list)
    latest.reverse()
    return f"Latest versions: {', '.join(str(v) for v in latest)}"


def _extract_forge_promotions(raw: str) -> str:
    """Extract MC→Forge version mapping from the Forge promotions_slim.json."""
    try:
        data = json.loads(raw)
    except Exception:
        return _truncate(raw)
    promos = data.get("promos", {})
    lines = []
    for key, ver in promos.items():
        lines.append(f"{key}: {ver}")
    return "\n".join(lines[-20:])   # keep the most recent 20 entries


def _extract_neoforge_xml(raw: str) -> str:
    """Extract the latest release version from NeoForge Maven metadata XML."""
    latest = re.search(r"<latest>(.*?)</latest>", raw)
    release = re.search(r"<release>(.*?)</release>", raw)
    versions = re.findall(r"<version>(.*?)</version>", raw)
    parts = []
    if release:
        parts.append(f"Latest release: {release.group(1)}")
    elif latest:
        parts.append(f"Latest: {latest.group(1)}")
    if versions:
        parts.append(f"Recent versions: {', '.join(versions[-5:])}")
    return "\n".join(parts) or _truncate(raw)


def _collect_raw() -> dict[str, str]:
    """Fetch all sources and return {label: raw_text}."""
    collected: dict[str, str] = {}
    for label, url, hint in _SOURCES:
        print(f"  Fetching: {label} …")
        raw = _fetch(url, hint)
        if not raw:
            continue
        if "api.papermc.io" in url:
            collected[label] = _extract_papermc_api(raw, label)
        elif "api.github.com" in url or "fabricmc.net" in url:
            collected[label] = _extract_release_notes(raw, label)
        elif "promotions_slim.json" in url:
            collected[label] = _extract_forge_promotions(raw)
        elif "maven-metadata.xml" in url:
            collected[label] = _extract_neoforge_xml(raw)
        else:
            # Plain text / markdown — truncate directly without HTML stripping
            collected[label] = _truncate(raw, 3000)
        time.sleep(0.3)   # be polite
    return collected


def _summarise_with_gemini(collected: dict[str, str]) -> str:
    """
    Ask Gemini Flash to extract only the facts relevant to Java code generation
    for Minecraft plugins (Paper, Velocity, mods).
    Falls back to a raw concatenation if Gemini is unavailable.
    """
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        print("  [warn] GEMINI_API_KEY not set — writing raw notes without summarisation.")
        return _raw_fallback(collected)

    combined = ""
    for label, text in collected.items():
        combined += f"\n\n=== {label} ===\n{text}"
    combined = combined[:24000]    # stay well within Gemini context

    system = (
        "You are a technical writer for Minecraft plugin developers.\n"
        "Your ONLY JOB: read the raw changelog / doc snippets below and extract "
        "information that affects how Java code is WRITTEN or COMPILED for:\n"
        "  - Paper plugins (org.bukkit.*, io.papermc.paper.*, net.kyori.adventure.*)\n"
        "  - Velocity plugins (com.velocitypowered.api.*)\n"
        "  - Fabric mods (net.fabricmc.*)\n"
        "  - Forge/NeoForge mods\n\n"
        "Output ONLY a Markdown document with this structure:\n"
        "## Paper / Bukkit API\n"
        "- bullet: what changed, old API → new API if applicable\n"
        "## Velocity API\n"
        "- bullet …\n"
        "## Adventure (net.kyori)\n"
        "- bullet …\n"
        "## Fabric API\n"
        "- bullet …\n"
        "## Forge / NeoForge\n"
        "- bullet …\n\n"
        "Rules:\n"
        "- Include ONLY facts that affect generated Java code. Skip infra/build/installer changes.\n"
        "- If there is nothing new, write 'No breaking changes detected.' under that section.\n"
        "- Do not add any introduction or closing remarks.\n"
        "- Be concrete: include class names, method signatures, package names where known.\n"
    )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=gemini_key)
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=3000,
                temperature=0.1,
            ),
            contents=combined,
        )
        text = (response.text or "").strip()
        if text:
            return text
        print("  [warn] Gemini returned empty response — using raw fallback.")
    except Exception as e:
        print(f"  [warn] Gemini summarisation failed: {e} — using raw fallback.")

    return _raw_fallback(collected)


def _raw_fallback(collected: dict[str, str]) -> str:
    """Dump raw snippets when Gemini is unavailable."""
    lines = ["## Raw API notes (Gemini summarisation unavailable)\n"]
    for label, text in collected.items():
        lines.append(f"### {label}\n{text}\n")
    return "\n".join(lines)


def run() -> None:
    print(f"[update_api_notes] Starting at {datetime.now(timezone.utc).isoformat()}")

    print("[update_api_notes] Fetching sources …")
    collected = _collect_raw()
    if not collected:
        print("[update_api_notes] ERROR: no sources could be fetched — aborting.")
        sys.exit(1)

    print(f"[update_api_notes] Fetched {len(collected)} sources. Summarising …")
    summary = _summarise_with_gemini(collected)

    header = (
        f"<!-- Generated by scripts/update_api_notes.py on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} -->\n\n"
        f"## Weekly API notes (auto-updated {datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n\n"
    )
    content = header + summary + "\n"

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(content, encoding="utf-8")
    print(f"[update_api_notes] Written → {_OUT} ({len(content)} chars)")

    # Sanity check: make sure router will pick it up
    from inference.router import _load_api_notes
    notes = _load_api_notes()
    print(f"[update_api_notes] Router will inject {len(notes)} chars of api_notes into SYSTEM_PROMPT.")
    print("[update_api_notes] Done.")


if __name__ == "__main__":
    run()
