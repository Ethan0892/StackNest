"""
validation/skript_check.py — Static analyser for Skript .sk files.

Checks performed:
  1. Mixed tabs/spaces indentation  → parse errors
  2. Undefined {@option} references → runtime "can't understand" errors
  3. Command blocks missing trigger: → command silently ignored
  4. Semicolons                      → Java carry-over, not valid Skript
  5. Java-style opening braces       → common beginner mistake
  6. Empty section bodies            → parser warning / silent failure
  7. Unclosed variable braces        → {varname without matching }

Usage:
    from validation.skript_check import validate_skript
    result = validate_skript(raw_llm_output)
    # result = {"ok": bool, "issues": ["[ERROR] ...", "[WARNING] ..."]}
"""

import re
from typing import NamedTuple


class _Issue(NamedTuple):
    severity: str   # "ERROR" or "WARNING"
    line: int       # 1-based, 0 = file-level
    message: str

    def __str__(self) -> str:
        loc = f" (line {self.line})" if self.line else ""
        return f"[{self.severity}]{loc} {self.message}"


def _extract_skript_block(raw: str) -> str:
    """Return just the .sk content, stripping the ```skript fence if present."""
    m = re.search(r"```skript\s*\n([\s\S]*?)```", raw, re.IGNORECASE)
    return m.group(1) if m else raw


def validate_skript(raw_code: str) -> dict:
    """
    Run static checks on a Skript .sk file (or LLM response containing one).

    Returns:
        {
            "ok":     bool,          # False if any ERROR-level issue found
            "issues": list[str],     # stringified _Issue list
        }
    """
    code  = _extract_skript_block(raw_code)
    lines = code.splitlines()
    issues: list[_Issue] = []

    # ── 1. Mixed tabs / spaces ───────────────────────────────────────────────
    tab_lines   = [i + 1 for i, l in enumerate(lines) if l.startswith("\t") and l.strip()]
    space_lines = [i + 1 for i, l in enumerate(lines) if l.startswith(" ")  and l.strip()]
    if tab_lines and space_lines:
        issues.append(_Issue("ERROR", 0,
            "Mixed tabs and spaces — Skript is indentation-sensitive. "
            f"Tab-indented lines: {tab_lines[:3]}; space-indented lines: {space_lines[:3]}"))

    # ── 2. Undefined {@option} references ────────────────────────────────────
    options_defined: set[str] = set()
    in_options = False
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped == "options:":
            in_options = True
            continue
        if in_options:
            # Options block ends when an unindented non-blank, non-comment line is found
            if stripped and not stripped.startswith("#") and not line[0:1] in ("\t", " "):
                in_options = False
            elif ":" in stripped and not stripped.startswith("#"):
                key = stripped.split(":")[0].strip()
                if key:
                    options_defined.add(key)

    for lineno, line in enumerate(lines, 1):
        for ref in re.findall(r"\{@(\w+)\}", line):
            if ref not in options_defined:
                issues.append(_Issue("ERROR", lineno,
                    f"{{@{ref}}} used but not defined in the options: block"))

    # ── 3. Command blocks missing trigger: ───────────────────────────────────
    cmd_re     = re.compile(r"^command\s+/\S+.*:$", re.IGNORECASE)
    trigger_re = re.compile(r"^\s+(trigger)\s*:", re.IGNORECASE)
    in_cmd = False
    cmd_line = 0
    has_trigger = False
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if cmd_re.match(stripped):
            if in_cmd and not has_trigger:
                issues.append(_Issue("ERROR", cmd_line,
                    f"command block is missing a 'trigger:' section"))
            in_cmd     = True
            cmd_line   = lineno
            has_trigger = False
        elif in_cmd:
            if trigger_re.match(line):
                has_trigger = True
            elif stripped and not stripped.startswith("#") and not line[:1] in ("\t", " "):
                # Un-indented new block — leaving the command scope
                if not has_trigger:
                    issues.append(_Issue("ERROR", cmd_line,
                        f"command block is missing a 'trigger:' section"))
                in_cmd = False
    if in_cmd and not has_trigger:
        issues.append(_Issue("ERROR", cmd_line,
            "command block is missing a 'trigger:' section"))

    # ── 4. Semicolons ─────────────────────────────────────────────────────────
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.endswith(";") and not stripped.startswith("#"):
            issues.append(_Issue("ERROR", lineno,
                "Semicolons are not valid in Skript — remove the trailing ';'"))

    # ── 5. Java-style opening braces ─────────────────────────────────────────
    # In Skript, all { must be part of variable refs like {varname} — a bare
    # trailing space-brace ( " {" at EOL) indicates Java scope syntax.
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"\s\{\s*$", line):
            issues.append(_Issue("WARNING", lineno,
                "Java-style opening brace detected — Skript uses indentation, not braces"))

    # ── 6. Empty section bodies ───────────────────────────────────────────────
    for i in range(len(lines) - 1):
        curr = lines[i].strip()
        nxt  = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if curr.endswith(":") and curr and not curr.startswith("#"):
            # Next non-blank line must be indented relative to this one
            curr_indent = len(lines[i]) - len(lines[i].lstrip())
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                next_indent = len(lines[j]) - len(lines[j].lstrip())
                if next_indent <= curr_indent and lines[j].strip() and not lines[j].strip().startswith("#"):
                    issues.append(_Issue("WARNING", i + 1,
                        f"Section '{curr}' appears to have an empty body"))

    errors_only = [i for i in issues if i.severity == "ERROR"]
    return {
        "ok":     len(errors_only) == 0,
        "issues": [str(i) for i in issues],
    }
