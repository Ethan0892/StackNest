"""
Datapack verifier — parses a generated Minecraft datapack LLM output into
named files and validates structural requirements.

Usage:
    from validation.datapack_check import parse_datapack_files, verify_datapack
    files  = parse_datapack_files(raw_code)   # list[DpFile]
    result = verify_datapack(files)            # {"ok": bool, "issues": list[str]}
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ── Block parser ──────────────────────────────────────────────────────────── #

_BLOCK_RE = re.compile(r"```(\w+)\n([\s\S]*?)```")
_PATH_COMMENT_RE = re.compile(r"^(?://|#)\s*(.+?)\s*$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9_.\-]+$")


@dataclass
class DpFile:
    path: str
    content: str
    lang: str


def parse_datapack_files(raw_code: str) -> list[DpFile]:
    """
    Parse the LLM-generated multi-block output into a list of DpFile objects.
    Each code block must start with a comment line giving the relative file
    path (e.g. ``// pack.mcmeta`` or ``# data/ns/functions/tick.mcfunction``).
    Blocks without a path comment are skipped.
    """
    files: list[DpFile] = []
    for m in _BLOCK_RE.finditer(raw_code):
        lang = m.group(1).lower()
        body = m.group(2)
        lines = body.split("\n")

        path: str | None = None
        content_start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            pm = _PATH_COMMENT_RE.match(stripped)
            if pm:
                path = pm.group(1).strip()
                content_start = i + 1
            break  # only inspect the very first non-blank line

        if path is None:
            continue

        content = "\n".join(lines[content_start:]).strip()
        if content:
            files.append(DpFile(path=path, content=content, lang=lang))

    return files


# ── Verifier ─────────────────────────────────────────────────────────────── #

def verify_datapack(files: list[DpFile]) -> dict:
    """
    Validate the structural correctness of a parsed datapack.
    Returns {"ok": bool, "issues": list[str]}.
    """
    issues: list[str] = []
    paths = {f.path for f in files}
    by_path = {f.path: f for f in files}

    # 1. pack.mcmeta is mandatory
    if "pack.mcmeta" not in paths:
        issues.append(
            "Missing pack.mcmeta — every datapack needs this file. "
            'It must contain {"pack":{"pack_format":<int>,"description":"..."}}.'
        )
    else:
        try:
            meta = json.loads(by_path["pack.mcmeta"].content)
            pack = meta.get("pack", {})
            if not isinstance(pack.get("pack_format"), int):
                issues.append(
                    "pack.mcmeta: 'pack.pack_format' must be an integer "
                    "(e.g. 61 for MC 1.21.4, 71 for MC 1.21.5)."
                )
            if not pack.get("description"):
                issues.append("pack.mcmeta: 'pack.description' is missing or empty.")
        except json.JSONDecodeError as e:
            issues.append(f"pack.mcmeta: invalid JSON — {e}")

    # 2. All JSON files must be valid JSON
    for f in files:
        if f.lang == "json" and f.path != "pack.mcmeta":
            try:
                json.loads(f.content)
            except json.JSONDecodeError as e:
                issues.append(f"{f.path}: invalid JSON — {e}")

    # 3. Namespace consistency
    namespaces: set[str] = set()
    for f in files:
        m = re.match(r"^data/([^/]+)/", f.path)
        if m and m.group(1) != "minecraft":
            namespaces.add(m.group(1))

    if len(namespaces) > 1:
        issues.append(
            f"Multiple namespaces detected ({', '.join(sorted(namespaces))}). "
            "Use a single consistent namespace across all files."
        )
    for ns in namespaces:
        if not _NAMESPACE_RE.match(ns):
            issues.append(
                f"Namespace '{ns}' contains invalid characters. "
                "Only lowercase letters, digits, underscores, hyphens, and dots are allowed."
            )

    # 4. tick/load tag cross-references — every referenced function must exist
    for tag_path in (
        "data/minecraft/tags/function/tick.json",
        "data/minecraft/tags/function/load.json",
    ):
        if tag_path not in by_path:
            continue
        try:
            tag_data = json.loads(by_path[tag_path].content)
            for ref in tag_data.get("values", []):
                parts = str(ref).split(":", 1)
                if len(parts) != 2:
                    issues.append(
                        f"{tag_path}: invalid function reference '{ref}' "
                        "(expected 'namespace:path/to/function')."
                    )
                    continue
                ns, fn_path = parts
                fn_file = f"data/{ns}/functions/{fn_path}.mcfunction"
                if fn_file not in paths:
                    issues.append(
                        f"{tag_path}: references function '{ref}' "
                        f"but '{fn_file}' was not generated."
                    )
        except (json.JSONDecodeError, AttributeError):
            pass  # JSON parse errors are already caught in check 2

    return {"ok": len(issues) == 0, "issues": issues}
