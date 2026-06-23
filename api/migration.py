"""Helpers for automated plugin source migration to Paper 1.21-style APIs."""

from __future__ import annotations

import difflib
import io
import posixpath
import re
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass


class MigrationError(ValueError):
    pass


PARTICLE_RENAMES: dict[str, str] = {
    "EXPLOSION_HUGE": "EXPLOSION_EMITTER",
    "EXPLOSION_LARGE": "EXPLOSION_EMITTER",
    "EXPLOSION_NORMAL": "EXPLOSION",
    "FIREWORKS_SPARK": "FIREWORK",
    "SMOKE_NORMAL": "SMOKE",
    "SMOKE_LARGE": "LARGE_SMOKE",
    "CRIT_MAGIC": "ENCHANTED_HIT",
    "SPELL_MOB": "ENTITY_EFFECT",
    "ENCHANTMENT_TABLE": "ENCHANT",
}

COLOR_MAP: dict[str, str] = {
    "BLACK": "BLACK",
    "DARK_BLUE": "DARK_BLUE",
    "DARK_GREEN": "DARK_GREEN",
    "DARK_AQUA": "DARK_AQUA",
    "DARK_RED": "DARK_RED",
    "DARK_PURPLE": "DARK_PURPLE",
    "GOLD": "GOLD",
    "GRAY": "GRAY",
    "DARK_GRAY": "DARK_GRAY",
    "BLUE": "BLUE",
    "GREEN": "GREEN",
    "AQUA": "AQUA",
    "RED": "RED",
    "LIGHT_PURPLE": "LIGHT_PURPLE",
    "YELLOW": "YELLOW",
    "WHITE": "WHITE",
}

MAX_GITHUB_ZIP_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_ZIP_BYTES = 12 * 1024 * 1024
MAX_FILES = 800
MAX_TEXT_FILE_BYTES = 300 * 1024


@dataclass
class MigrationOutcome:
    source: str
    source_version: str | None
    target_version: str
    files_total: int
    files_changed: int
    changed_files: list[str]
    fixes_applied: dict[str, int]
    unified_diff: str
    migrated_files: dict[str, str]


def _normalize_repo_url(url: str) -> tuple[str, str, str | None]:
    """Return (owner, repo, branch_or_none) for github.com URLs."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"https", "http"}:
        raise MigrationError("GitHub URL must start with http:// or https://")
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise MigrationError("Only github.com repository URLs are supported")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise MigrationError("GitHub URL must include owner and repository")

    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    branch = None

    # /owner/repo/tree/<branch>
    if len(parts) >= 4 and parts[2] == "tree":
        branch = "/".join(parts[3:])

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner):
        raise MigrationError("Invalid GitHub owner")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise MigrationError("Invalid GitHub repository")

    return owner, repo, branch


def _download_bytes(url: str, max_bytes: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "StackNestMigration/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        chunks = []
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise MigrationError("Archive too large")
            chunks.append(chunk)
    return b"".join(chunks)


def fetch_github_archive(repo_url: str) -> bytes:
    owner, repo, branch = _normalize_repo_url(repo_url)

    candidates = []
    if branch:
        candidates.append(f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}")
    candidates.append(f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/main")
    candidates.append(f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/master")

    last_err = None
    for url in candidates:
        try:
            return _download_bytes(url, max_bytes=MAX_GITHUB_ZIP_BYTES)
        except Exception as e:  # pragma: no cover - network dependent
            last_err = e
            continue

    raise MigrationError(f"Could not download repository archive: {last_err}")


def _is_source_path(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(".java") or lower.endswith(".yml") or lower.endswith(".yaml")


def extract_source_files(zip_bytes: bytes) -> dict[str, str]:
    files: dict[str, str] = {}

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except Exception as e:
        raise MigrationError(f"Invalid ZIP archive: {e}") from e

    names = zf.namelist()
    if len(names) > MAX_FILES:
        raise MigrationError("Too many files in archive")

    for name in names:
        # Normalize to POSIX style and strip top-level folder from GitHub zips.
        norm = posixpath.normpath(name).lstrip("/")
        if not norm or norm.endswith("/"):
            continue
        parts = norm.split("/")
        rel = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
        if not rel or rel.startswith("../") or rel == "..":
            continue
        if not _is_source_path(rel):
            continue

        info = zf.getinfo(name)
        if info.file_size > MAX_TEXT_FILE_BYTES:
            continue

        raw = zf.read(name)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except Exception:
                continue
        files[rel] = text

    if not files:
        raise MigrationError("No .java/.yml/.yaml source files were found")

    return files


def _fix_java(content: str, fixes: dict[str, int]) -> str:
    updated = content

    if "import org.bukkit.plugin.PluginCommand;" in updated:
        updated = updated.replace(
            "import org.bukkit.plugin.PluginCommand;",
            "import org.bukkit.command.PluginCommand;",
        )
        fixes["plugin_command_import"] = fixes.get("plugin_command_import", 0) + 1

    has_chatcolor_import = "import org.bukkit.ChatColor;" in updated
    if has_chatcolor_import:
        updated = updated.replace("import org.bukkit.ChatColor;\n", "")
        if "import net.kyori.adventure.text.Component;" not in updated:
            updated = updated.replace(
                "package ",
                "package ",
                1,
            )
            # Insert imports after package declaration when present, otherwise at top.
            pkg_match = re.search(r"^\s*package\s+[^;]+;\s*\n", updated, flags=re.MULTILINE)
            import_block = (
                "import net.kyori.adventure.text.Component;\n"
                "import net.kyori.adventure.text.format.NamedTextColor;\n"
            )
            if pkg_match:
                idx = pkg_match.end()
                updated = updated[:idx] + import_block + updated[idx:]
            else:
                updated = import_block + updated
        elif "import net.kyori.adventure.text.format.NamedTextColor;" not in updated:
            updated = updated.replace(
                "import net.kyori.adventure.text.Component;\n",
                "import net.kyori.adventure.text.Component;\n"
                "import net.kyori.adventure.text.format.NamedTextColor;\n",
                1,
            )
        fixes["chatcolor_import"] = fixes.get("chatcolor_import", 0) + 1

    for old, new in PARTICLE_RENAMES.items():
        n = len(re.findall(rf"\bParticle\.{re.escape(old)}\b", updated))
        if n:
            updated = re.sub(rf"\bParticle\.{re.escape(old)}\b", f"Particle.{new}", updated)
            fixes["particle_rename"] = fixes.get("particle_rename", 0) + n

    pattern = re.compile(
        r"(\w+\.sendMessage\s*\(\s*)ChatColor\.([A-Z_]+)\s*\+\s*\"([^\"\\]*(?:\\.[^\"\\]*)*)\"\s*\)",
        flags=re.MULTILINE,
    )

    def _chatcolor_repl(m: re.Match[str]) -> str:
        receiver = m.group(1)
        color = m.group(2)
        text = m.group(3)
        named = COLOR_MAP.get(color)
        if not named:
            return m.group(0)
        fixes["sendmessage_chatcolor"] = fixes.get("sendmessage_chatcolor", 0) + 1
        return f'{receiver}Component.text("{text}", NamedTextColor.{named}))'

    updated = pattern.sub(_chatcolor_repl, updated)

    literal_pattern = re.compile(
        r"(\w+\.sendMessage\s*\(\s*)\"([^\"\\]*(?:\\.[^\"\\]*)*)\"\s*\)",
        flags=re.MULTILINE,
    )

    def _literal_repl(m: re.Match[str]) -> str:
        fixes["sendmessage_literal"] = fixes.get("sendmessage_literal", 0) + 1
        return f'{m.group(1)}Component.text("{m.group(2)}"))'

    updated = literal_pattern.sub(_literal_repl, updated)
    return updated


def _fix_yml(content: str, target_version: str, fixes: dict[str, int]) -> tuple[str, str | None]:
    source_version = None
    m = re.search(r"(?im)^\s*api-version\s*:\s*['\"]?([^'\"\n]+)['\"]?\s*$", content)
    if m:
        source_version = m.group(1).strip()
        content2 = re.sub(
            r"(?im)^\s*api-version\s*:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
            f"api-version: '{target_version}'",
            content,
            count=1,
        )
        if content2 != content:
            fixes["api_version"] = fixes.get("api_version", 0) + 1
        return content2, source_version

    # plugin.yml without api-version: add one to top-level.
    if "name:" in content and "main:" in content:
        lines = content.splitlines()
        lines.append(f"api-version: '{target_version}'")
        fixes["api_version_added"] = fixes.get("api_version_added", 0) + 1
        return "\n".join(lines) + ("\n" if content.endswith("\n") else ""), source_version

    return content, source_version


def migrate_sources(files: dict[str, str], source: str, target_version: str = "1.21") -> MigrationOutcome:
    migrated: dict[str, str] = {}
    changed: list[str] = []
    diffs: list[str] = []
    fixes: dict[str, int] = {}
    source_version: str | None = None

    for path, content in files.items():
        updated = content

        lower = path.lower()
        if lower.endswith(".java"):
            updated = _fix_java(updated, fixes)
        elif lower.endswith(("plugin.yml", "paper-plugin.yml", "bungee.yml", ".yaml", ".yml")):
            updated, detected = _fix_yml(updated, target_version=target_version, fixes=fixes)
            if detected and not source_version:
                source_version = detected

        migrated[path] = updated
        if updated != content:
            changed.append(path)
            diff = "\n".join(
                difflib.unified_diff(
                    content.splitlines(),
                    updated.splitlines(),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="",
                )
            )
            if diff:
                diffs.append(diff)

    return MigrationOutcome(
        source=source,
        source_version=source_version,
        target_version=target_version,
        files_total=len(files),
        files_changed=len(changed),
        changed_files=sorted(changed),
        fixes_applied=fixes,
        unified_diff="\n\n".join(diffs),
        migrated_files=migrated,
    )


def build_zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()
