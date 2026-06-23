"""
validation/feedback_loop.py — Orchestrate generation + validation + retry.

This is the core quality loop:
  1. Generate plugin from user instruction
  2. Run static checks (fast, no compilation)
  3. Run compilation against Paper API stub
  4. Run plugin.yml validation + cross-checks
  5. If any errors, inject them into a correction prompt and regenerate
  6. Repeat up to MAX_ATTEMPTS times
  7. Return best result

Usage:
    from validation.feedback_loop import PluginGenerator
    gen = PluginGenerator()
    result = gen.generate("Create a plugin that broadcasts a message every 60 seconds")
    if result.success:
        print(result.code)
    else:
        print("Failed after", result.attempts, "attempts:", result.final_errors)
"""

import time
from dataclasses import dataclass, field
from typing import Generator

from inference.router import PluginRouter, SYSTEM_PROMPT, _estimate_complexity
from inference.server import GenerationParams, generate_with_fallback
from inference.smart_assembly import assemble_focused_prompt
from validation.compile_check import CompileResult, DEFAULT_PAPER_PROFILE, compile_response, compile_tests, format_errors_for_prompt
from validation.static_check import check_response as static_check_response
from validation.static_check import get_error_messages as get_static_errors
from validation.yml_check import YmlCheckResult, validate_response as validate_yml


MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Healing helpers
# ---------------------------------------------------------------------------

def _balance_fences(code: str) -> str:
    """
    Ensure all markdown fenced blocks in a response are closed.

    When the AI hits the token limit mid-output, the last ``` block is never
    closed.  Healing models (Gemini/Claude) receive a response that starts
    with ```java but has no matching closing ```, which confuses them and
    causes error counts to grow instead of shrink.  Appending a closing ```
    gives them a valid, parseable structure to work from.
    """
    import re as _re
    stripped = code.rstrip()
    if not stripped:
        return code
    # Count fence-opening lines (``` at the start of a line, with optional lang tag)
    openings = len(_re.findall(r'(?m)^```\S*\s*$', stripped))
    closings = len(_re.findall(r'(?m)^```\s*$', stripped))
    # If odd balance, the last block is open — close it
    if (openings - closings) % 2 == 1:
        return stripped + "\n```"
    return code


def _is_truncation(errors: list[str]) -> bool:
    """Return True when the errors indicate the Java source was cut off mid-file."""
    e_lower = [x.lower() for x in errors]
    for e in e_lower:
        if "reached end of file while parsing" in e:
            return True
        # "no diagnostics" fires when javac exits non-zero but emits nothing
        # parseable (most commonly: truncated output causes an unusual error
        # format, or the raw fallback message itself).  Treat it the same as
        # a clean truncation signal so the loop forces a cloud regeneration.
        if "javac returned no diagnostics" in e or "no parsed diagnostics" in e:
            return True
        # Fired when the class declaration itself was cut off before the opening
        # brace — a more severe truncation than a missing closing brace.
        if "class, interface, enum, or record expected" in e:
            return True
        # Fired when the model stops output mid-string-literal.
        if "unclosed string literal" in e:
            return True
    return False


def _healed_is_better(working_code: str, healed_code: str,
                      working_errors: list, healed_errors: list) -> bool:
    """
    Return True only if healed_code is a genuine improvement over working_code.

    Rules:
    - Must have fewer errors.
    - Must not be a size regression: healed code must be at least 50% the line
      count of working code.  This prevents a healer from "fixing" a 500-line
      plugin by returning a 33-line stub that only has 1 EOF error.
    """
    if len(healed_errors) >= len(working_errors):
        return False
    wlines = working_code.count("\n") + 1
    hlines = healed_code.count("\n") + 1
    # If healed code has zero errors (fully working), accept any size ≥50 lines.
    # A complete 100-line plugin is always better than a 340-line truncated one.
    if len(healed_errors) == 0:
        return hlines >= 50
    # Otherwise require at least 50% of original size to prevent stub regressions.
    return hlines >= wlines * 0.5


def _extract_truncated_filenames(errors: list[str]) -> list[str]:
    """
    Parse Java filenames from truncation-type compiler errors:
      - 'reached end of file while parsing'
      - 'class, interface, enum, or record expected' (class declaration was cut off)
    e.g. 'ru/mycity/service/ResidentService.java:26: error: reached end of file...'
    → ['ru/mycity/service/ResidentService.java']
    """
    import re as _re
    TRUNC_SIGNALS = (
        "reached end of file while parsing",
        "class, interface, enum, or record expected",
    )
    files: list[str] = []
    seen: set[str] = set()
    for err in errors:
        if not any(sig in err.lower() for sig in TRUNC_SIGNALS):
            continue
        m = _re.match(r"([a-zA-Z0-9_./$\-][a-zA-Z0-9_/.$\-]*\.java):\d+:", err)
        if m and m.group(1) not in seen:
            files.append(m.group(1))
            seen.add(m.group(1))
    return files


def _extract_block_for_classname(response: str, filename: str) -> "tuple[str, str, str] | None":
    """
    Find the ```java block in *response* that corresponds to *filename*.
    Matching strategy (in order):
      1. Block body contains 'class ClassName'
      2. Block's first-line comment includes the basename
      3. Block's package declaration starts with the path-derived package prefix
         (prefix match: 'com.example' also matches 'com.example.myplugin')
      4. For GeneratedPlugin_N fallback names (no public class found at extraction
         time): return the first block that has no public class declaration — it
         is the block that was assigned the fallback filename.
    Returns (pre_text, raw_block, post_text) ready for splicing, or None.
    """
    import re as _re
    _PUBLIC_RE = (
        r"public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*"
        r"(?:class|interface|enum|record)\s+\w+"
    )
    basename = filename.split("/")[-1].replace(".java", "")
    parts = filename.replace(".java", "").split("/")
    expected_pkg = ".".join(parts[:-1]) if len(parts) > 1 else None
    pattern = _re.compile(r"(```java[ \t]*\n)(.*?)(```|\Z)", _re.DOTALL)
    for m in pattern.finditer(response):
        content = m.group(2)
        if _re.search(rf"\bclass\s+{_re.escape(basename)}\b", content):
            return (response[:m.start()], m.group(0), response[m.end():])
        first_line = content.lstrip().split("\n")[0].strip()
        if first_line.startswith("//") and basename in first_line:
            return (response[:m.start()], m.group(0), response[m.end():])
        if expected_pkg and _re.search(
            # Prefix match: 'com\.example' matches 'com.example' AND
            # 'com.example.myplugin', 'com.example.foo.bar', etc.
            rf"^\s*package\s+{_re.escape(expected_pkg)}(?:\.\w+)*\s*;",
            content, _re.MULTILINE
        ):
            # Strip line comments before checking for other class declarations
            # (prevents "// ... class declaration" false-positives)
            content_no_comments = _re.sub(r"//[^\n]*", "", content)
            other_classes = [
                c for c in _re.findall(r"\bclass\s+(\w+)", content_no_comments)
                if c != basename
            ]
            if not other_classes:
                return (response[:m.start()], m.group(0), response[m.end():])
    # Strategy 4: GeneratedPlugin_N is the fallback assigned when _path_from_code
    # found no public class declaration.  The corresponding block is the first one
    # that also has no public class declaration.
    if _re.match(r"GeneratedPlugin_\d+$", basename):
        for m in pattern.finditer(response):
            content = m.group(2)
            if not _re.search(_PUBLIC_RE, content):
                return (response[:m.start()], m.group(0), response[m.end():])
    return None


def _surgical_truncation_heal(
    working_code: str,
    truncated_errors: list[str],
    instruction: str,
    healer_fn,  # kimi_heal — signature: (code, errors, extra_instruction="")
) -> "tuple[str, bool]":
    """
    Heal truncated multi-file output by completing ONLY the cut-off files.

    For each file named in a truncation error, extract its java block, ask the
    healer to complete ONLY that one file, then splice the repaired block back.
    The rest of the response is never touched — no risk of introducing new errors
    in already-correct files (the root cause of the 2→5 errors pattern).

    Returns (result_code, changed) where changed=True if any block was replaced.
    """
    import re as _re
    filenames = _extract_truncated_filenames(truncated_errors)
    if not filenames:
        return working_code, False

    result = working_code
    changed = False
    desc_snippet = instruction[:200].rstrip()

    for filename in filenames:
        extracted = _extract_block_for_classname(result, filename)
        if not extracted:
            continue
        pre, raw_block, post = extracted
        basename = filename.split("/")[-1]

        # Detect imports-only truncation: the code was cut off so early that
        # the class declaration itself was never written (only package + imports).
        # In this case the prompt must explicitly ask for the full class body,
        # not just "add closing braces" (which would mislead the healer into
        # appending a lone '}' to an import list).
        import re as _re2
        _block_src = _re2.search(r"```java[ \t]*\n(.*?)(?:```|\Z)", raw_block, _re2.DOTALL)
        _block_content = _block_src.group(1) if _block_src else raw_block
        _has_class = bool(_re2.search(
            r"public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*"
            r"(?:class|interface|enum|record)\s+\w+",
            _block_content,
        ))

        if not _has_class:
            heal_prompt = (
                f"The Java file below ({basename}) is severely truncated — it "
                f"contains only package/import statements. The entire class "
                f"declaration and method bodies are missing.\n"
                f"Plugin context: {desc_snippet}\n\n"
                f"RULES:\n"
                f"- Keep all existing import statements.\n"
                f"- Add a complete 'public class [ClassName] extends JavaPlugin' declaration.\n"
                f"- Implement onEnable(), onDisable(), and all other required methods "
                f"based on the plugin context above.\n"
                f"- Output ONLY the corrected ```java block — no yaml, no other files.\n"
            )
        else:
            heal_prompt = (
                f"The Java class below ({basename}) was cut off before the closing "
                f"brace — method bodies and the final '}}' are missing.\n"
                f"Plugin context: {desc_snippet}\n\n"
                f"RULES:\n"
                f"- Complete ONLY this file. Add missing method stubs and closing braces.\n"
                f"- Do NOT change any logic that is already written.\n"
                f"- Output ONLY the corrected ```java block — no yaml, no other files.\n"
            )
        try:
            healed = healer_fn(raw_block, [], extra_instruction=heal_prompt)
            if not healed or not healed.strip():
                continue
            m = _re.search(r"```java[ \t]*\n(.*?)```", healed, _re.DOTALL)
            if not m:
                continue
            new_block = f"```java\n{m.group(1).rstrip()}\n```"
            result = pre + new_block + post
            changed = True
        except Exception:
            continue

    return result, changed


def _ends_in_unclosed_string(src: str) -> bool:
    """
    Return True if *src* ends with an unterminated string literal.
    Regular Java strings cannot span lines, so this reliably detects the
    'unclosed string literal' truncation pattern — the model was cut off
    mid-string on the very last line it emitted.
    """
    in_string = False
    in_char = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    while i < len(src):
        c = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ''
        if in_line_comment:
            if c == '\n':
                in_line_comment = False
        elif in_block_comment:
            if c == '*' and nxt == '/':
                in_block_comment = False
                i += 1
        elif in_string:
            if c == '\\':
                i += 1
            elif c == '"':
                in_string = False
        elif in_char:
            if c == '\\':
                i += 1
            elif c == "'":
                in_char = False
        else:
            if c == '/' and nxt == '/':
                in_line_comment = True
                i += 1
            elif c == '/' and nxt == '*':
                in_block_comment = True
                i += 1
            elif c == '"':
                in_string = True
            elif c == "'":
                in_char = True
        i += 1
    return in_string


def _count_missing_braces(code: str) -> int:
    """
    Return how many closing braces '}' are missing from Java source.
    Ignores braces inside string literals and line/block comments (best-effort).
    Returns 0 if the code is balanced or already closed.
    """
    depth = 0
    in_string = False
    in_char = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    while i < len(code):
        c = code[i]
        nxt = code[i + 1] if i + 1 < len(code) else ''
        if in_line_comment:
            if c == '\n':
                in_line_comment = False
        elif in_block_comment:
            if c == '*' and nxt == '/':
                in_block_comment = False
                i += 1
        elif in_string:
            if c == '\\':
                i += 1  # skip escaped char
            elif c == '"':
                in_string = False
        elif in_char:
            if c == '\\':
                i += 1
            elif c == "'":
                in_char = False
        else:
            if c == '/' and nxt == '/':
                in_line_comment = True
                i += 1
            elif c == '/' and nxt == '*':
                in_block_comment = True
                i += 1
            elif c == '"':
                in_string = True
            elif c == "'":
                in_char = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
        i += 1
    return max(0, depth)


def _find_depth1_boundary(java_src: str) -> int:
    """
    Return the line index (0-based) of the last line where a '}' brought
    the brace depth to exactly 1 (end of a method/constructor in the outer class).

    Uses the same string/comment-aware character scan as _count_missing_braces.
    Returns -1 if no such boundary exists.
    """
    depth = 0
    in_string = False
    in_char = False
    in_line_comment = False
    in_block_comment = False
    current_line = 0
    last_depth1_line = -1
    i = 0
    while i < len(java_src):
        c = java_src[i]
        nxt = java_src[i + 1] if i + 1 < len(java_src) else ''
        if c == '\n':
            current_line += 1
            if in_line_comment:
                in_line_comment = False
            i += 1
            continue
        if in_line_comment:
            pass
        elif in_block_comment:
            if c == '*' and nxt == '/':
                in_block_comment = False
                i += 1
        elif in_string:
            if c == '\\':
                i += 1
            elif c == '"':
                in_string = False
        elif in_char:
            if c == '\\':
                i += 1
            elif c == "'":
                in_char = False
        else:
            if c == '/' and nxt == '/':
                in_line_comment = True
                i += 1
            elif c == '/' and nxt == '*':
                in_block_comment = True
                i += 1
            elif c == '"':
                in_string = True
            elif c == "'":
                in_char = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 1:
                    last_depth1_line = current_line
        i += 1
    return last_depth1_line


def _close_open_braces(code: str) -> "str | None":
    """
    Attempt to surgically close a truncated Java block.

    Strategy:
      1. Balance any unclosed markdown fences.
      2. For each ```java block (scanning in reverse — the last block is most
         likely the truncated one), find the last line where brace depth reached
         1 (= end of a complete method/member in the outer class).
      3. Trim the block to that boundary point, count remaining open braces,
         and append that many '}' characters to close all open classes/methods.

    Only applies the fix if the missing brace count is 0-8 (larger gaps mean
    more than a few closing braces are needed and the code is too damaged).

    Returns the patched full response string, or None if no fix was applied.
    """
    import re as _re
    fast_code = _balance_fences(code)
    java_block_re = _re.compile(r"(```java[ \t]*\n)(.*?)(```)", _re.DOTALL)
    all_java = list(java_block_re.finditer(fast_code))

    for m in reversed(all_java):
        src = m.group(2)

        # Pre-process: strip any trailing line that contains an unclosed string
        # literal.  This happens when the model is cut off mid-string (e.g.
        # '    String msg = "partial text' with no closing quote).  A regular
        # Java string cannot span lines, so removing the single offending last
        # line is always safe and lets the boundary + brace logic work normally.
        if _ends_in_unclosed_string(src):
            _tmp = src.splitlines()
            if _tmp:
                _tmp.pop()  # drop the one bad line
            src = "\n".join(_tmp) + "\n" if _tmp else ""
            print("[Healer] _close_open_braces: stripped unclosed-string tail line.")

        last_boundary = _find_depth1_boundary(src)
        lines = src.splitlines()
        if last_boundary < 0:
            # No complete method boundary found — the block was cut off before any
            # method closed.  Fall back to simple append: count all missing braces
            # across the whole block and append them without trimming.  This avoids
            # "reached end of file" at the cost of leaving empty method stubs, which
            # the healer can then fix in a subsequent pass (rather than getting stuck
            # in a truncation loop).
            missing = _count_missing_braces(src)
            if 0 < missing <= 20:
                fixed_src = src.rstrip() + "\n" + "\n".join(["}"] * missing) + "\n"
                patched = (
                    fast_code[: m.start()]
                    + m.group(1)
                    + fixed_src
                    + m.group(3)
                    + fast_code[m.end() :]
                )
                print(
                    f"[Healer] _close_open_braces: no complete boundary, "
                    f"simple-appended {missing}x'}}'"
                )
                return patched
            continue
        if last_boundary >= len(lines) - 1:
            # The depth-1 boundary is at (or after) the final line.
            # This looks like a complete block, BUT the most common truncation
            # pattern is: the last method closes normally at the file boundary,
            # leaving the outer class/interface un-closed.  Double-check the
            # overall brace balance and append any missing braces.
            missing = _count_missing_braces(src)
            if 0 < missing <= 8:
                fixed_src = src.rstrip() + "\n" + "\n".join(["}"] * missing) + "\n"
                patched = (
                    fast_code[: m.start()]
                    + m.group(1)
                    + fixed_src
                    + m.group(3)
                    + fast_code[m.end() :]
                )
                print(
                    f"[Healer] _close_open_braces: boundary=end, "
                    f"appended {missing}x'}}' (outer class was not closed)."
                )
                return patched
            continue
        trimmed_src = "\n".join(lines[: last_boundary + 1]) + "\n"
        missing = _count_missing_braces(trimmed_src)
        if 0 <= missing <= 20:
            close = ("\n".join(["}"] * missing) + "\n") if missing > 0 else ""
            fixed_src = trimmed_src + close
            patched = (
                fast_code[: m.start()]
                + m.group(1)
                + fixed_src
                + m.group(3)
                + fast_code[m.end() :]
            )
            trimmed_n = len(lines) - last_boundary - 1
            print(
                f"[Healer] _close_open_braces: trimmed {trimmed_n} incomplete "
                f"line(s), appended {missing}x'}}'"
            )
            return patched
    return None


def _complete_truncated(
    code: str,
    errors: list[str],
    instruction: str,
) -> "str | None":
    """
    Complete truncated output by extracting only the cut-off file, asking
    Kimi to finish it in isolation, then stitching it back.

    This is a thin, early-exit wrapper around _surgical_truncation_heal that
    is called WITHIN the generation loop (not just in the post-loop healer)
    so truncation is resolved without burning additional full-regen attempts.

    Returns the stitched code string if the surgical heal changed anything,
    or None if Kimi is unavailable or the heal produced no change.
    """
    try:
        from inference.kimi import heal_available as _kimi_ok, kimi_heal as _kimi_h
    except ImportError:
        return None
    if not _kimi_ok():
        return None
    healed, changed = _surgical_truncation_heal(code, errors, instruction, _kimi_h)
    return healed if changed else None


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _categorize_errors(errors: list[str]) -> dict[str, list[str]]:
    """
    Sort raw compiler/validator errors into named buckets.
    Buckets: imports, methods, deprecated, yml, other.
    """
    cats: dict[str, list[str]] = {
        "imports":    [],
        "methods":    [],
        "deprecated": [],
        "yml":        [],
        "other":      [],
    }
    for err in errors:
        low = err.lower()
        if any(k in low for k in ("cannot find symbol", "package does not exist", "import")):
            cats["imports"].append(err)
        elif any(k in low for k in ("cannot be applied", "method", "no suitable method", "not applicable")):
            cats["methods"].append(err)
        elif "deprecated" in low:
            cats["deprecated"].append(err)
        elif any(k in low for k in ("plugin.yml", "yaml", "yml")):
            cats["yml"].append(err)
        else:
            cats["other"].append(err)
    return cats


# Known compile-error patterns → exact fix hints injected into heal prompts.
# Each entry: (substring_to_match_in_error, hint_string)
_COMPILE_HINTS: list[tuple[str, str]] = [
    (
        "no suitable method found for text(",
        "Adventure API fix: Component.text(String, TextColor) is the correct overload for coloured text literals. "
        "Do NOT pass a Component as the first argument — only plain String literals. "
        "To recolor an existing component use component.color(NamedTextColor.X). "
        "To concatenate, chain .append(Component.text(...)) calls off Component.empty().",
    ),
    (
        "cannot find symbol\nsymbol: method text(",
        "Import net.kyori.adventure.text.Component and call Component.text(\"message\", NamedTextColor.COLOR).",
    ),
    (
        "method text in interface buildablecomponent",
        "Component.text() builder syntax error. Use Component.text(\"string\"[, TextColor]) — not the builder overload.",
    ),
    (
        "sendmessage(java.lang.string)",
        "sendMessage(String) is removed in Paper 1.21. Use player.sendMessage(Component.text(\"msg\", NamedTextColor.WHITE)).",
    ),
    (
        "cannot find symbol\nsymbol: variable chatcolor",
        "ChatColor is removed. Replace with net.kyori.adventure.text.format.NamedTextColor constants "
        "and send Component objects instead of strings.",
    ),
    (
        "unclosed string literal",
        "The file was truncated mid-string. Remove the incomplete last line (the one with the "
        "unclosed \"\") and then close every open method and class with the correct number of '}' characters.",
    ),
    (
        "reached end of file while parsing",
        "The file was truncated — a class or method body was not closed. "
        "COUNT every '{' in the file and count every '}'. The difference is the number of closing "
        "braces you must append. Add ALL missing '}' characters to fully close every open method and "
        "class before fixing any other errors.",
    ),
    (
        "illegal start of expression",
        "Syntax error — often a missing '}' from a previous method or extra/misplaced brace. "
        "Check the line reported and the method immediately before it.",
    ),
    (
        "cannot find symbol\nsymbol: class namedtextcolor",
        "Add import: import net.kyori.adventure.text.format.NamedTextColor;",
    ),
    (
        "cannot find symbol\nsymbol: class component",
        "Add import: import net.kyori.adventure.text.Component;",
    ),
    (
        "package net.kyori.adventure does not exist",
        "Adventure API is bundled with Paper — do NOT add it as a dependency in pom.xml. "
        "Remove any adventure dependency block; just import net.kyori.adventure.* directly.",
    ),
    (
        "missing @eventhandler",
        "Add @EventHandler (import org.bukkit.event.EventHandler) above every event listener method. "
        "Without it the method is never called — the event silently fires with no effect.",
    ),
    (
        "getcommand() can return null",
        "Null-check getCommand() before chaining: "
        "PluginCommand cmd = getCommand(\"name\"); if (cmd != null) { cmd.setExecutor(this); }",
    ),
    (
        "setexecutor() is never called",
        "In onEnable(), register every command declared in plugin.yml: "
        "PluginCommand cmd = getCommand(\"name\"); if (cmd != null) { cmd.setExecutor(this); } "
        "Do this for EVERY command entry in the plugin.yml commands: section.",
    ),
    (
        "registerevents() is never called",
        "In onEnable(), register every Listener class: "
        "getServer().getPluginManager().registerEvents(this, this); "
        "If there are separate Listener classes, register each one: "
        "getServer().getPluginManager().registerEvents(new MyListener(this), this);",
    ),
    (
        "class plugincommand, location: package org.bukkit.plugin",
        "Wrong import: PluginCommand is in org.bukkit.command, NOT org.bukkit.plugin. "
        "Fix the import to: import org.bukkit.command.PluginCommand;",
    ),
    (
        "no suitable method found for addban",
        "BanList.addBan() signature mismatch. Two valid patterns:\n"
        "PATTERN A — name-based ban (simplest): "
        "Bukkit.<org.bukkit.BanList<String>>getBanList(org.bukkit.BanList.Type.NAME)"
        ".addBan(player.getName(), Component.text(\"reason\"), (Date) null, \"PluginName\"); "
        "PATTERN B — profile-based ban (requires explicit cast): "
        "org.bukkit.ban.ProfileBanList banList = (org.bukkit.ban.ProfileBanList) Bukkit.getBanList(org.bukkit.BanList.Type.PROFILE); "
        "banList.addBan(player.getPlayerProfile(), Component.text(\"reason\"), (Date) null, \"PluginName\"); "
        "NEVER pass a PlayerProfile to BanList<String>.addBan() — they are different types. "
        "NEVER use TextComponent (BungeeCord or Adventure TextComponent) — reason MUST be net.kyori.adventure.text.Component. "
        "NEVER use java.time.Instant for expiry — use java.util.Date ((Date) null for permanent). "
        "ALWAYS cast null explicitly: (Date) null — bare null causes 'no suitable method found' because javac can't resolve the overload. "
        "Required imports for Pattern B: import org.bukkit.ban.ProfileBanList; import org.bukkit.BanList; "
        "import net.kyori.adventure.text.Component; import java.util.Date;",
    ),
    (
        "no suitable method found for ban(",
        "Player.ban() signature in Paper 26.1: player.ban(Component reason, Date expiry, String source, boolean kickIfOnline). "
        "The first argument is Component, NOT boolean. "
        "Correct: player.ban(Component.text(\"reason\"), (Date) null, \"PluginName\", true); "
        "Alternatively: Bukkit.getBanList(BanList.Type.NAME).addBan(player.getName(), Component.text(\"reason\"), (Date) null, \"PluginName\");",
    ),
    (
        "method getenderchest()",
        "OfflinePlayer does not have getEnderChest(). Only an online Player does. "
        "Fix: Player online = Bukkit.getPlayer(offlinePlayer.getUniqueId()); "
        "if (online != null) { Inventory ec = online.getEnderChest(); }",
    ),
    (
        "bossbarviewer cannot be converted to audience",
        "BossBarViewer is not a Paper/Adventure type. bossBar.viewers() returns Set<Audience>. "
        "To remove all viewers: for (Audience v : new java.util.HashSet<>(bossBar.viewers())) bossBar.removeViewer(v); "
        "Required import: import net.kyori.adventure.audience.Audience;",
    ),
    (
        "method transactionsucceeded()",
        "Vault EconomyResponse: the method is transactionSuccess(), NOT transactionSucceeded(). "
        "Fix: if (response.transactionSuccess()) { ... }",
    ),
    (
        "package me.clip.placeholderapi",
        "PlaceholderAPI jar is not on the compile classpath. Two options:\n"
        "Option A (recommended): use soft-depend and check at runtime whether PAPI is loaded, "
        "wrapping all PAPI calls in 'if (Bukkit.getPluginManager().isPluginEnabled(\"PlaceholderAPI\"))'.\n"
        "Option B: the PlaceholderAPI jar must be in the server's libs/ directory for compilation. "
        "Import: import me.clip.placeholderapi.PlaceholderAPI; (NOT me.clip.placeholderapi.expansion). "
        "plugin.yml: softdepend: [PlaceholderAPI]",
    ),
    (
        "bukkitaudiences",
        "Remove BukkitAudiences — it is from adventure-platform-bukkit which is NOT needed for Paper. "
        "Paper bundles Adventure natively. Delete the BukkitAudiences field and import. "
        "To send messages use player.sendMessage(Component.text(\"msg\")) directly.",
    ),
    (
        "method getbans()",
        "BanList.getBans() does not exist. Use getBanEntries(): "
        "Set<BanEntry<?>> entries = Bukkit.getBanList(BanList.Type.NAME).getBanEntries();",
    ),
    (
        "cannot find symbol",
        "Symbol not found. Common causes in Paper 26.1:\n"
        "- getAudience().players() does not exist. Use Bukkit.getServer().sendMessage(component) "
        "or Bukkit.broadcast(component) or for (Player p : Bukkit.getOnlinePlayers()) p.sendMessage(component);\n"
        "- BanList.addBan() with bare null: use (Date) null not null for the expiry arg.\n"
        "- OfflinePlayer.getEnderChest() does not exist — only online Player has it.\n"
        "- BukkitAudiences does not exist in Paper — Adventure is built in, remove the import.",
    ),
    (
        "lifecycleeventmanager<plugin> cannot be converted to lifecycleeventmanager<commands>",
        "Paper Brigadier fix: use this.getLifecycleManager().registerEventHandler("
        "LifecycleEvents.COMMANDS, event -> { Commands cmds = event.registrar(); cmds.register(...); }); "
        "Do NOT cast to LifecycleEventManager<Commands> — the type parameter must be Plugin. "
        "Required imports: import io.papermc.paper.plugin.lifecycle.event.types.LifecycleEvents; "
        "import io.papermc.paper.plugin.lifecycle.event.LifecycleEventManager; "
        "import io.papermc.paper.command.brigadier.Commands;",
    ),
    (
        "type argument commands is not within bounds of type-variable o",
        "Paper Brigadier fix: use this.getLifecycleManager().registerEventHandler("
        "LifecycleEvents.COMMANDS, event -> { Commands cmds = event.registrar(); cmds.register(...); }); "
        "The manager type parameter is Plugin, not Commands. "
        "Required imports: import io.papermc.paper.plugin.lifecycle.event.types.LifecycleEvents; "
        "import io.papermc.paper.command.brigadier.Commands;",
    ),
    (
        "cannot find symbol\nsymbol: class lifecycleevents\nlocation: package io.papermc.paper.plugin.lifecycle.event",
        "Wrong import for LifecycleEvents — it lives in the '.types' sub-package, NOT directly in '.event'. "
        "Fix the import to: import io.papermc.paper.plugin.lifecycle.event.types.LifecycleEvents; "
        "(NOT io.papermc.paper.plugin.lifecycle.event.LifecycleEvents — that class does not exist). "
        "All required Brigadier imports: "
        "import io.papermc.paper.plugin.lifecycle.event.types.LifecycleEvents; "
        "import io.papermc.paper.plugin.lifecycle.event.LifecycleEventManager; "
        "import io.papermc.paper.command.brigadier.Commands; "
        "import io.papermc.paper.command.brigadier.CommandSourceStack;",
    ),
    (
        "cannot find symbol\nsymbol: class lifecycleevents",
        "Wrong or missing import for LifecycleEvents. Correct package is: "
        "import io.papermc.paper.plugin.lifecycle.event.types.LifecycleEvents; "
        "Also add: import io.papermc.paper.command.brigadier.Commands; "
        "import io.papermc.paper.command.brigadier.CommandSourceStack;",
    ),
    (
        "type commands does not take parameters",
        "Paper 26.1 Brigadier fix: Commands (io.papermc.paper.command.brigadier.Commands) is NOT generic — "
        "remove the type parameter entirely. "
        "Correct: Commands cmds = event.registrar(); "
        "NOT: Commands<S> or Commands<BukkitBrigadierCommandSource>.",
    ),
    (
        "brigadier's commands class is not generic",
        "Paper 26.1 Brigadier fix: Commands (io.papermc.paper.command.brigadier.Commands) is NOT generic — "
        "remove the type parameter entirely. "
        "Correct: Commands cmds = event.registrar(); "
        "NOT: Commands<S> or Commands<BukkitBrigadierCommandSource>.",
    ),
    (
        "mockbukkit import detected in runtime plugin source",
        "MockBukkit/JUnit imports MUST ONLY appear in the LAST ```java block (the test class). "
        "Remove every 'import be.seeseemelk.mockbukkit.*' and 'import org.junit.*' from all non-test files. "
        "The test class (src/test/java/...) must be the very last ```java block in your output.",
    ),
    (
        "symbol:   variable raw_fish",
        "Paper 1.21 API fix: Material.RAW_FISH no longer exists. Replace with Material.COD (or Material.SALMON where appropriate).",
    ),
    (
        "symbol:   variable protection_environmental",
        "Paper 1.21 API fix: Enchantment.PROTECTION_ENVIRONMENTAL no longer exists. Use Enchantment.PROTECTION.",
    ),
    (
        "symbol:   variable durability",
        "Paper 1.21 API fix: Enchantment.DURABILITY no longer exists. Use Enchantment.UNBREAKING.",
    ),
    (
        "package com.velocitypowered",
        "Velocity API imports were generated for a Paper plugin. Remove com.velocitypowered.* imports and use Bukkit/Paper APIs only.",
    ),
    (
        "';' expected",
        "Syntax error: ';' expected. Common causes: (1) an expression was left incomplete "
        "(e.g. assignment without a value, or method call without closing parenthesis), "
        "(2) missing semicolon at end of statement, "
        "(3) a for-each loop written as 'for (Type var : collection' missing closing ')'. "
        "Check the reported line and the 2-3 lines before it for the incomplete statement. "
        "Do NOT re-import classes that are not defined in the output — remove all imports "
        "for classes you haven't written and use private inner classes instead.",
    ),    (
        "',', ')', or '[' expected",
        "Syntax error: unexpected token where a comma, closing parenthesis, or opening bracket was expected. "
        "Common causes: "
        "(1) Missing comma between elements in an annotation (e.g. @Plugin dependencies, @Dependency fields, "
        "or @Subscribe order=); "
        "(2) Missing comma between constructor or method parameters; "
        "(3) Malformed generic type (e.g. 'List<Map String, String>' missing '<' around key type); "
        "(4) Multi-catch missing '|': 'catch (TypeA TypeB e)' should be 'catch (TypeA | TypeB e)'; "
        "(5) 'throws' clause missing comma: 'throws ExA ExB' should be 'throws ExA, ExB'. "
        "Check the reported line and the few lines before it for any annotation, parameter list, "
        "generic type, or throws clause that is incomplete.",
    ),
    (
        "incompatible types: invalid method reference",
        "Method reference type mismatch. Replace every 'this::method' or 'ClassName::method' "
        "with an explicit lambda whose parameter types exactly match the target functional interface. "
        "Common Paper interfaces and their required signatures:\n"
        "  CommandExecutor  → (CommandSender sender, Command cmd, String label, String[] args) -> boolean\n"
        "  Runnable         → () -> { ... }\n"
        "  Consumer<T>      → (T t) -> { ... }\n"
        "  Function<T,R>    → (T t) -> { return ...; }\n"
        "Example fix: replace 'getCommand(\"x\").setExecutor(this::handleX)' with "
        "'getCommand(\"x\").setExecutor((sender, cmd, label, args) -> handleX(sender, cmd, label, args))'. "
        "Ensure the delegate method 'handleX' ALSO has the matching signature "
        "'boolean handleX(CommandSender sender, Command cmd, String label, String[] args)'.",
    ),
    (
        "no class declaration found",
        "The file contains only package/import statements — the entire class body is missing. "
        "You MUST write the complete plugin from scratch: keep the existing imports, "
        "then add 'public class [Name] extends JavaPlugin' with onEnable(), onDisable(), "
        "and all required methods. "
        "Output EVERYTHING in a SINGLE ```java block. "
        "Keep the implementation SHORT and focused — do not add features not in the original request.",
    ),
    (
        "no suitable method found for register",
        "Commands.register() argument mismatch. The first arg must be a LiteralCommandNode<CommandSourceStack> "
        "(call .build() on your LiteralArgumentBuilder), NOT Component.text(), TextComponent, or a String. "
        "The description (second arg, if provided) must be a plain String literal, not a Component. "
        "Correct: cmds.register(Commands.literal(\"spawn\").executes(ctx -> { return Command.SINGLE_SUCCESS; }).build()); "
        "With description: cmds.register(node, \"description as plain String\");",
    ),
    (
        "')' or ',' expected",
        "Syntax error: a closing ')' or ',' was expected — most common cause in Brigadier chains: "
        "argument(\"name\", type) is not closed before .executes() or .then(). "
        "The ')' that closes argument() MUST appear before chaining .executes() or .then() onto it. "
        "WRONG: .then(argument(\"x\", StringArgumentType.word()\n    .executes(ctx -> {...})) "
        "RIGHT: .then(argument(\"x\", StringArgumentType.word())\n    .executes(ctx -> {...})) "
        "Count every opening argument() call and ensure each one has its closing ')' before the next '.' chain. "
        "Also check for: missing comma in method parameters, annotation arrays, or multi-catch blocks.",
    ),
    (
        "reached end of file while parsing",
        "The file was truncated — a class or method body was not closed. "
        "COUNT every '{' in the file and count every '}'. The difference is the number of closing "
        "braces you must append. Add ALL missing '}' characters to fully close every open method and "
        "class. Fix this FIRST before addressing any other errors.",
    ),
    (
        "'void' type not allowed here",
        "A void method's return value is used where a value is required. "
        "Common causes: (1) calling a void method inside an expression "
        "(e.g. 'list.add(player.sendMessage(...))') — split it into two statements; "
        "(2) returning a void call from a non-void method — add a dedicated return value. "
        "Fix: extract the void call to its own statement, then use a separate value in the expression.",
    ),
    (
        "string concatenation inside component.text",
        "Replace Component.text(\"literal \" + variable) with chained .append() calls: "
        "Component.text(\"literal \").append(Component.text(variable)). "
        "Component.text() must receive ONLY a plain string literal or a single variable — never the + operator. "
        "Example fix: Component.text(\"Balance: \").append(Component.text(String.valueOf(balance))).append(Component.text(\" gold\")). "
        "Apply this pattern to EVERY occurrence of string concatenation inside Component.text() in the file.",
    ),
    (
        "bungecord textcomponent detected",
        "Remove 'import net.md_5.bungee.api.chat.TextComponent' — BungeeCord TextComponent is NOT the Adventure API. "
        "Replace with: import net.kyori.adventure.text.Component; "
        "For BanList.addBan() use Component.text(\"reason\") as the reason argument. "
        "Full correct call: Bukkit.getBanList(BanList.Type.NAME).addBan(playerName, Component.text(\"reason\"), (Date) null, \"PluginName\"); "
        "(expiry is java.util.Date — use (Date) null for permanent ban, NOT java.time.Instant)",
    ),
    (
        "class banlist, location: package org.bukkit.ban",
        "Wrong import: BanList is in org.bukkit, NOT org.bukkit.ban. "
        "Replace 'import org.bukkit.ban.BanList;' with 'import org.bukkit.BanList;'. "
        "The package org.bukkit.ban does not exist — BanList lives directly in org.bukkit.",
    ),
]


def _error_hints(errors: list[str]) -> str:
    """
    Match compile errors against known patterns and return a block of precise
    fix instructions to prepend to heal prompts.
    """
    hints: list[str] = []
    seen: set[str] = set()
    combined = "\n".join(errors).lower()
    for pattern, hint in _COMPILE_HINTS:
        if pattern.lower() in combined and hint not in seen:
            hints.append(f"• {hint}")
            seen.add(hint)
    if not hints:
        return ""
    return "KNOWN FIX INSTRUCTIONS (apply these exactly):\n" + "\n".join(hints) + "\n\n"


def _build_targeted_heal_prompt(code: str, cats: dict[str, list[str]], focus: str) -> str:
    """
    Build a Kimi healing prompt that focuses on one error category at a time.
    focus: 'imports' | 'methods' | 'other'
    """
    focused_errors = cats.get(focus, []) + cats.get("other" if focus != "other" else "deprecated", [])
    if not focused_errors:
        focused_errors = [e for bucket in cats.values() for e in bucket][:6]

    label_map = {
        "imports":    "missing imports / unresolved symbols",
        "methods":    "wrong method signatures or method-not-found errors",
        "other":      "logic errors and deprecated API usage",
    }
    category_label = label_map.get(focus, focus)

    known_hints = _error_hints(focused_errors)
    error_block = "\n".join(f"  - {e}" for e in focused_errors[:6])
    return (
        f"{known_hints}"
        f"FOCUS: Fix {category_label} only.\n\n"
        f"Errors to fix:\n{error_block}\n\n"
        f"Return the complete corrected plugin code, keeping all features intact."
    )


@dataclass
class GenerationResult:
    success: bool
    code: str                          # Best generated code (may be partially valid)
    attempts: int = 0
    static_warnings: list[str] = field(default_factory=list)
    compile_result: CompileResult | None = None
    test_compile_result: CompileResult | None = None   # MockBukkit test compilation
    yml_result: YmlCheckResult | None = None
    final_errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def has_warnings(self) -> bool:
        return bool(self.static_warnings)

    def summary(self) -> str:
        lines = [f"Success: {self.success}", f"Attempts: {self.attempts}"]
        if self.compile_result:
            lines.append(f"Compile: {'OK' if self.compile_result.success else 'FAIL'}")
            if self.compile_result.errors:
                lines.extend(f"  {e}" for e in self.compile_result.errors[:3])
        if self.yml_result and not self.yml_result.valid:
            lines.append(f"plugin.yml: FAIL")
            lines.extend(f"  {e}" for e in self.yml_result.errors[:3])
        if self.static_warnings:
            lines.append(f"Static warnings: {len(self.static_warnings)}")
        lines.append(f"Elapsed: {self.elapsed_seconds:.1f}s")
        return "\n".join(lines)


class PluginGenerator:
    def __init__(
        self,
        router: PluginRouter | None = None,
        params: GenerationParams | None = None,
        skip_compile: bool = False,
        tier: str = "free",
        plan: str = "free",
        paper_target_profile: str = DEFAULT_PAPER_PROFILE,
        use_smart_assembly: bool = False,
    ) -> None:
        self.router = router or PluginRouter()
        self.params = params or GenerationParams()
        self.skip_compile = skip_compile
        self.tier = tier
        self.plan = plan
        self.paper_target_profile = paper_target_profile
        self.use_smart_assembly = use_smart_assembly
        self.sa_features: list[str] = []

    def _collect_errors(
        self,
        response: str,
        compile_result: CompileResult,
        yml_result: YmlCheckResult,
    ) -> list[str]:
        """Aggregate all errors from all validators into a single list."""
        errors: list[str] = []

        # Static errors (deprecated APIs etc.)
        errors.extend(get_static_errors(response))

        # Compilation errors
        if not self.skip_compile and not compile_result.success:
            errors.extend(compile_result.errors[:5])  # Cap at 5 — focus the heal model on the most impactful errors
            if not compile_result.errors:
                errors.append("Compilation failed but javac returned no diagnostics. Regenerate code and avoid incomplete/truncated output.")

        # plugin.yml errors
        if not yml_result.valid:
            errors.extend(yml_result.errors)

        return errors

    def generate(self, instruction: str) -> GenerationResult:
        """
        Run the full generation + validation loop.
        Returns when code is valid or MAX_ATTEMPTS is exhausted.
        """
        start = time.time()
        last_code = ""
        last_errors: list[str] = []

        # Estimate complexity once — used for backend routing and prompt injection.
        complexity = _estimate_complexity(instruction)
        if complexity != "simple":
            print(f"[Loop] Request complexity: {complexity}")

        # Smart assembly — paid plans only: extract feature blocks and focus the prompt
        if self.use_smart_assembly:
            try:
                _sys, instruction, self.sa_features = assemble_focused_prompt(
                    instruction, SYSTEM_PROMPT
                )
                if self.sa_features:
                    print(f"[SmartAssembly] Features injected: {self.sa_features}")
            except Exception as _sa_err:
                print(f"[SmartAssembly] Skipped ({_sa_err})")
        # Safe defaults in case every attempt errors out before reaching validation
        compile_result      = CompileResult(success=False, errors=["Never compiled"])
        test_compile_result = CompileResult(success=False)
        yml_result          = YmlCheckResult(valid=False, errors=["Never validated"])

        # Track the best result across all attempts (fewest errors)
        best_error_count   = float("inf")
        best_code          = ""
        best_compile        = compile_result
        best_yml            = yml_result
        best_errors: list[str] = []

        # Track which backends have produced truncated output so retries skip them
        truncated_backends: set[str] = set()
        last_gen_source: str = ""
        # Set to True when _close_open_braces applied a structural repair so that
        # the NEXT correction prompt tells the healer to reconstruct missing method
        # bodies rather than delete unresolved references.
        _brace_repair_applied: bool = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Build prompt — first attempt uses standard prompt,
            # subsequent attempts use correction prompt
            truncation_retry = False
            if attempt == 1:
                prompt = self.router.build_prompt(instruction, plan=self.plan)
            else:
                # If the previous attempt was truncated (missing closing braces),
                # use a compact-regeneration prompt AND skip the local model.
                # The local model has a fixed max_tokens budget (~1400 tokens);
                # retrying it with a compact prompt still risks the same cutoff
                # on genuinely large plugins.  Going straight to Kimi/Gemini
                # (which have no output-size issue) on the second attempt is
                # far more likely to succeed.
                if _is_truncation(last_errors):
                    if last_gen_source:
                        truncated_backends.add(last_gen_source)
                    # Detect imports-only: code was cut off before the class body.
                    # Needs a stronger prompt than regular truncation recovery.
                    import re as _re_io
                    _last_imports_only = not bool(_re_io.search(
                        r"public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*"
                        r"(?:class|interface|enum|record)\s+\w+",
                        last_code,
                    ))
                    prompt = self.router.build_completion_prompt(
                        instruction, plan=self.plan, imports_only=_last_imports_only
                    )
                    truncation_retry = True
                    _brace_repair_applied = False
                    excl_msg = f" (excluding: {', '.join(sorted(truncated_backends))})" if truncated_backends else ""
                    print(f"[Loop] Attempt {attempt}: {'imports-only' if _last_imports_only else 'truncation'} detected — "
                          f"skipping local model, forcing cloud for this retry{excl_msg}.")
                else:
                    _repair_preamble = (
                        "CONTEXT: The previous code was structurally repaired after truncation — "
                        "method bodies are likely incomplete. Prioritize reconstructing the "
                        "missing implementations from the original instruction rather than "
                        "deleting unresolved symbols or removing imports."
                    ) if _brace_repair_applied else ""
                    prompt = self.router.build_correction_prompt(
                        instruction, last_code, last_errors, preamble=_repair_preamble
                    )
                _brace_repair_applied = False  # reset each attempt

            # Generate — local model first, then tier-appropriate cloud fallback.
            # force_cloud=True when we already know the local model truncated output.
            # exclude_backends prevents retrying the backend that already truncated.
            _excl = frozenset(truncated_backends) if (truncation_retry and truncated_backends) else None
            try:
                response, gen_source = generate_with_fallback(
                    prompt, self.params,
                    system_prompt=SYSTEM_PROMPT,
                    instruction=instruction,
                    tier=self.tier,
                    force_cloud=truncation_retry,
                    exclude_backends=_excl,
                    complexity=complexity,
                )
            except Exception as e:
                return GenerationResult(
                    success=False,
                    code=last_code,
                    attempts=attempt,
                    final_errors=[f"Inference server error: {e}"],
                    elapsed_seconds=time.time() - start,
                )

            last_code = response
            last_gen_source = gen_source.lower()

            # --- Validation pass ---
            # 1. Static checks (fast)
            try:
                static_issues = static_check_response(response)
                static_warnings = [str(i) for i in static_issues if i.severity != "error"]
            except Exception as _e:
                print(f"[Loop] Static check failed (attempt {attempt}): {_e}")
                static_issues = []
                static_warnings = []

            # 2. Compile (slower — skip for class-only chunks)
            if not self.skip_compile:
                try:
                    compile_result = compile_response(response, paper_profile=self.paper_target_profile)
                except Exception as e:
                    compile_result = CompileResult(
                        success=False, errors=[f"Compile exception: {e}"]
                    )
                # Test compilation — non-blocking (warnings only)
                try:
                    test_compile_result = compile_tests(response, paper_profile=self.paper_target_profile)
                except Exception:
                    test_compile_result = CompileResult(success=True, files_compiled=0)
            else:
                compile_result = CompileResult(success=True, files_compiled=0)
                test_compile_result = CompileResult(success=True, files_compiled=0)

            # 3. plugin.yml
            try:
                yml_result = validate_yml(response)
            except Exception as _e:
                print(f"[Loop] YML validation failed (attempt {attempt}): {_e}")
                yml_result = YmlCheckResult(valid=False, errors=[f"Validation exception: {_e}"])

            # Collect all errors
            try:
                errors = self._collect_errors(response, compile_result, yml_result)
            except Exception as _e:
                print(f"[Loop] Error collection failed (attempt {attempt}): {_e}")
                errors = ["Internal validation error"]

            # Track best result (fewest errors)
            if len(errors) < best_error_count:
                best_error_count = len(errors)
                best_code        = response
                best_compile     = compile_result
                best_yml         = yml_result
                best_errors      = errors

            if not errors:
                return GenerationResult(
                    success=True,
                    code=response,
                    attempts=attempt,
                    static_warnings=static_warnings,
                    compile_result=compile_result,
                    test_compile_result=test_compile_result,
                    yml_result=yml_result,
                    elapsed_seconds=time.time() - start,
                )

            last_errors = errors
            last_code   = response

            # ── Early truncation fast-fixes (within-loop) ─────────────────── #
            # When truncation is detected on any attempt, try lightweight fixes
            # BEFORE burning the next generation attempt on a full regen.
            # This saves at most 2 cloud API calls per truncated request.
            if _is_truncation(errors) and not self.skip_compile:
                print(f"[Loop] Attempt {attempt}: truncation detected — trying fast in-loop fixes.")

                # Fix 1 (zero-AI): trim to last complete method + close braces
                _bf = _close_open_braces(response)
                if _bf is not None:
                    try:
                        _bf_compile = compile_response(_bf, paper_profile=self.paper_target_profile)
                        _bf_yml     = validate_yml(_bf)
                        _bf_errors  = self._collect_errors(_bf, _bf_compile, _bf_yml)
                        print(f"[Loop] In-loop brace-close: {len(errors)} → {len(_bf_errors)} errors.")
                        if not _bf_errors:
                            return GenerationResult(
                                success=True, code=_bf, attempts=attempt,
                                static_warnings=static_warnings,
                                compile_result=_bf_compile,
                                test_compile_result=test_compile_result,
                                yml_result=_bf_yml,
                                elapsed_seconds=time.time() - start,
                            )
                        if _healed_is_better(response, _bf, errors, _bf_errors):
                            last_code   = _bf
                            last_errors = _bf_errors
                            # Signal to the next correction prompt that this code
                            # was structurally repaired — the healer should
                            # reconstruct missing bodies, not delete broken refs.
                            _brace_repair_applied = True
                            if len(_bf_errors) < best_error_count:
                                best_error_count = len(_bf_errors)
                                best_code        = _bf
                                best_compile     = _bf_compile
                                best_yml         = _bf_yml
                                best_errors      = _bf_errors
                    except Exception as _e:
                        print(f"[Loop] In-loop brace-close failed: {_e}")

                # Fix 2 (one Kimi call): surgical per-file completion + stitch
                if _is_truncation(last_errors):
                    _ct = _complete_truncated(last_code, last_errors, instruction)
                    if _ct is not None:
                        try:
                            _ct_compile = compile_response(_ct, paper_profile=self.paper_target_profile)
                            _ct_yml     = validate_yml(_ct)
                            _ct_errors  = self._collect_errors(_ct, _ct_compile, _ct_yml)
                            print(f"[Loop] In-loop surgical complete: {len(last_errors)} → {len(_ct_errors)} errors.")
                            if not _ct_errors:
                                return GenerationResult(
                                    success=True, code=_ct, attempts=attempt,
                                    static_warnings=static_warnings,
                                    compile_result=_ct_compile,
                                    test_compile_result=test_compile_result,
                                    yml_result=_ct_yml,
                                    elapsed_seconds=time.time() - start,
                                )
                            if _healed_is_better(last_code, _ct, last_errors, _ct_errors):
                                last_code   = _ct
                                last_errors = _ct_errors
                                if len(_ct_errors) < best_error_count:
                                    best_error_count = len(_ct_errors)
                                    best_code        = _ct
                                    best_compile     = _ct_compile
                                    best_yml         = _ct_yml
                                    best_errors      = _ct_errors
                        except Exception as _e:
                            print(f"[Loop] In-loop surgical complete failed: {_e}")

        # ── All local attempts exhausted ───────────────────────────────── #
        # Use the best result (fewest errors) as the starting point for healing.
        working_code   = best_code   or last_code
        working_errors = best_errors or last_errors

        # ── Zero-AI brace-append fast-pass ────────────────────────────────── #
        # "reached end of file while parsing" = model hit its token limit.
        # Strategy: trim the truncated java block to the last COMPLETE method/
        # member boundary (depth-1 in the class body), then append the missing
        # class/interface closing braces. This avoids dangling method bodies that
        # cause "missing return statement" errors after naive brace-appending.
        # Note: _close_open_braces() applies the same logic as the in-loop fast-fix
        # above.  Running it again here handles cases where the best result across
        # all attempts was NOT the last-generated response (e.g. attempt 1 was
        # better but still truncated, while attempt 2 regressed).
        if _is_truncation(working_errors) and not self.skip_compile:
            _patched_code = _close_open_braces(working_code)
            if _patched_code:
                try:
                    fast_compile = compile_response(_patched_code, paper_profile=self.paper_target_profile)
                    fast_yml     = validate_yml(_patched_code)
                    fast_errors  = self._collect_errors(_patched_code, fast_compile, fast_yml)
                    print(f"[Healer] Brace-trim+append: {len(working_errors)} → {len(fast_errors)} errors.")
                    if not fast_errors:
                        return GenerationResult(
                            success=True, code=_patched_code,
                            attempts=MAX_ATTEMPTS + 1,
                            compile_result=fast_compile, yml_result=fast_yml,
                            elapsed_seconds=time.time() - start,
                        )
                    if _healed_is_better(working_code, _patched_code, working_errors, fast_errors):
                        working_code   = _patched_code
                        working_errors = fast_errors
                except Exception as e:
                    print(f"[Healer] Brace-trim+append failed: {e}")

        # ── Surgical truncation heal (multi-file: fix ONLY the cut-off files) ──
        # When output spans multiple java blocks and only the last file(s) are
        # truncated, sending the entire codebase to a healer causes it to rewrite
        # already-correct files and introduce new errors (the 2→5 pattern).
        # Extract each truncated file by class name, heal it in isolation, splice
        # back — leaving all other blocks untouched.
        if _is_truncation(working_errors):
            from inference.kimi import heal_available as _kimi_avail_s, kimi_heal as _kimi_heal_s
            if _kimi_avail_s():
                try:
                    surgically_healed, was_changed = _surgical_truncation_heal(
                        working_code, working_errors, instruction, _kimi_heal_s
                    )
                    if was_changed:
                        sg_compile = compile_response(surgically_healed, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                        sg_yml     = validate_yml(surgically_healed)
                        sg_errors  = self._collect_errors(surgically_healed, sg_compile, sg_yml)
                        print(f"[Healer] Surgical truncation: {len(working_errors)} → {len(sg_errors)} errors.")
                        if not sg_errors:
                            return GenerationResult(
                                success=True, code=surgically_healed,
                                attempts=MAX_ATTEMPTS + 1,
                                compile_result=sg_compile, yml_result=sg_yml,
                                elapsed_seconds=time.time() - start,
                            )
                        if _healed_is_better(working_code, surgically_healed, working_errors, sg_errors):
                            working_code   = surgically_healed
                            working_errors = sg_errors
                except Exception as e:
                    print(f"[Healer] Surgical truncation failed: {e}")

        # ── Gemini heal (fast — try first) ─────────────────────────────── #
        from inference.gemini import is_available as gemini_available, gemini_heal
        if working_errors and gemini_available():
            truncated = _is_truncation(working_errors)
            # Detect imports-only: the model never emitted a class declaration.
            # This is a more severe failure than regular truncation — Gemini must
            # generate from scratch rather than complete a partial body.
            import re as _re
            _has_any_class = bool(_re.search(
                r"public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*"
                r"(?:class|interface|enum|record)\s+\w+",
                working_code,
            ))
            imports_only = truncated and not _has_any_class
            # Skip Gemini for regular mid-body truncation: passing a cut-off stub
            # causes it to invent code and regularly produces more errors (1→5
            # pattern).  BUT for imports-only (no class at all), Gemini must do a
            # full fresh regeneration — the kimi surgical healer already failed.
            if not truncated or imports_only:
                print(f"[Healer] {'Gemini imports-only regen' if imports_only else 'Gemini heal'} ({len(working_errors)} errors).")
                try:
                    # For imports-only, pass just the import stub so Gemini keeps
                    # the correct imports and builds the complete class on top.
                    # Use a compact-class instruction so Gemini doesn't repeat the
                    # same imports-and-stop failure pattern.
                    heal_input = _balance_fences(working_code) if not imports_only else working_code
                    heal_errors = working_errors[:8]
                    _imports_only_hint = (
                        "CRITICAL: The code above has ONLY import/package lines — NO class body. "
                        "You must write the COMPLETE plugin class now. "
                        "Keep at most 8 of the existing imports (the most essential ones). "
                        "Use fully-qualified names inline for the rest. "
                        "Start with: public class [PluginName] extends JavaPlugin { "
                        "and implement all required methods. Keep the total under 120 lines. "
                        "Output in ONE ```java block only.\n\n"
                    ) if imports_only else ""
                    healed_g = gemini_heal(heal_input, heal_errors,
                                           _imports_only_hint + _error_hints(heal_errors) + instruction)
                    if healed_g.strip():
                        hg_compile = compile_response(healed_g, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                        hg_yml     = validate_yml(healed_g)
                        hg_errors  = self._collect_errors(healed_g, hg_compile, hg_yml)
                        print(f"[Healer] Gemini {'imports-only regen' if imports_only else 'heal'}: {len(working_errors)} → {len(hg_errors)} errors.")
                        if not hg_errors:
                            return GenerationResult(
                                success=True, code=healed_g,
                                attempts=MAX_ATTEMPTS + 1,
                                compile_result=hg_compile, yml_result=hg_yml,
                                elapsed_seconds=time.time() - start,
                            )
                        if _healed_is_better(working_code, healed_g, working_errors, hg_errors):
                            working_code   = healed_g
                            working_errors = hg_errors
                except Exception as e:
                    print(f"[Healer] Gemini heal failed: {e}")

        # ── Kimi multi-pass heal (deeper analysis, slower) ─────────────── #
        from inference.kimi import heal_available as kimi_available, kimi_heal
        if working_errors and kimi_available():
            cats = _categorize_errors(working_errors)
            n_cats = sum(1 for v in cats.values() if v)
            truncated = _is_truncation(working_errors)
            print(
                f"[Healer] Kimi multi-pass: "
                f"{len(working_errors)} error(s) across {n_cats} category(\"ies\")."
                + (" Code appears truncated." if truncated else "")
            )

            # Truncation pass
            if truncated:
                from validation.compile_check import extract_java_blocks as _ejb
                blocks = _ejb(_balance_fences(working_code))
                missing = sum(_count_missing_braces(src) for _, src in blocks)
                code_lines = working_code.count("\n")

                # Large plugins (>350 lines): asking Kimi to complete a huge partial
                # file reliably times out at 120s.  Do a fresh scope-reduced regen
                # via cloud instead, excluding every backend that already truncated.
                if code_lines > 350:
                    regen_excl = frozenset(truncated_backends) if truncated_backends else None
                    excl_note  = f" (excl: {', '.join(sorted(regen_excl))})" if regen_excl else ""
                    print(
                        f"[Healer] Code is {code_lines} lines — too large for Kimi "
                        f"truncation completion. Scope-reduced regen{excl_note}."
                    )
                    try:
                        from inference.router import SYSTEM_PROMPT as _SP
                        regen_code, regen_src = generate_with_fallback(
                            self.router.build_completion_prompt(instruction, plan=self.plan),
                            self.params,
                            system_prompt=_SP,
                            instruction=instruction,
                            tier=self.tier,
                            exclude_backends=regen_excl,
                            complexity=complexity,
                        )
                        if regen_code.strip():
                            rr_compile = compile_response(regen_code, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                            rr_yml     = validate_yml(regen_code)
                            rr_errors  = self._collect_errors(regen_code, rr_compile, rr_yml)
                            print(f"[Healer] Scope-reduced regen via {regen_src}: {len(working_errors)} → {len(rr_errors)} errors.")
                            if not rr_errors:
                                return GenerationResult(
                                    success=True, code=regen_code,
                                    attempts=MAX_ATTEMPTS + 2,
                                    compile_result=rr_compile, yml_result=rr_yml,
                                    elapsed_seconds=time.time() - start,
                                )
                            if len(rr_errors) < len(working_errors):
                                working_code   = regen_code
                                working_errors = rr_errors
                                cats           = _categorize_errors(working_errors)
                    except Exception as e:
                        print(f"[Healer] Scope-reduced regen failed: {e}")
                else:
                    # Detect imports-only truncation: if missing == 0, the code
                    # was cut off before any class braces were opened.
                    if missing == 0:
                        truncation_hint = (
                            f"Original plugin description: {instruction[:200]}\n"
                            f"The code is severely truncated — it contains only "
                            f"package/import statements; the class declaration and "
                            f"all method bodies are absent.\n"
                            f"Write the complete plugin from scratch: keep the existing "
                            f"imports, add the class declaration and all required methods. "
                            f"Output EVERYTHING in a SINGLE ```java block.\n"
                            f"IMPORTANT: do NOT split across multiple files."
                        )
                    else:
                        truncation_hint = (
                            f"Original plugin description: {instruction[:200]}\n"
                            f"The code was cut off before completion — it is missing "
                            f"approximately {missing} closing brace(s). "
                            f"First, complete the truncated class/method bodies by adding "
                            f"the missing closing braces, then fix any remaining compile errors. "
                            f"Output the COMPLETE plugin from the package declaration to the final '}}'.\n"
                            f"IMPORTANT: consolidate everything into a SINGLE ```java block using "
                            f"private static nested classes — do NOT split across multiple files."
                        )
                    try:
                        healed_t = kimi_heal(
                            _balance_fences(working_code),
                            working_errors,
                            extra_instruction=truncation_hint,
                        )
                        if healed_t.strip():
                            ht_compile = compile_response(healed_t, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                            ht_yml     = validate_yml(healed_t)
                            ht_errors  = self._collect_errors(healed_t, ht_compile, ht_yml)
                            print(f"[Healer] Truncation pass: {len(working_errors)} → {len(ht_errors)} errors.")
                            if not ht_errors:
                                return GenerationResult(
                                    success=True, code=healed_t,
                                    attempts=MAX_ATTEMPTS + 2,
                                    compile_result=ht_compile, yml_result=ht_yml,
                                    elapsed_seconds=time.time() - start,
                                )
                            if _healed_is_better(working_code, healed_t, working_errors, ht_errors):
                                working_code   = healed_t
                                working_errors = ht_errors
                                cats           = _categorize_errors(working_errors)
                    except Exception as e:
                        print(f"[Healer] Truncation pass failed: {e}")

            # Pass 1 — fix imports / unresolved symbols (most common blocker)
            if cats["imports"]:
                try:
                    prompt1 = _build_targeted_heal_prompt(working_code, cats, "imports")
                    healed1 = kimi_heal(working_code, cats["imports"], extra_instruction=prompt1)
                    if healed1.strip():
                        h1_compile = compile_response(healed1, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                        h1_yml     = validate_yml(healed1)
                        h1_errors  = self._collect_errors(healed1, h1_compile, h1_yml)
                        print(f"[Healer] Pass 1 (imports): {len(cats['imports'])} → {len(h1_errors)} errors.")
                        if not h1_errors:
                            return GenerationResult(
                                success=True, code=healed1,
                                attempts=MAX_ATTEMPTS + 2,
                                compile_result=h1_compile, yml_result=h1_yml,
                                elapsed_seconds=time.time() - start,
                            )
                        if _healed_is_better(working_code, healed1, working_errors, h1_errors):
                            working_code   = healed1
                            working_errors = h1_errors
                            cats           = _categorize_errors(working_errors)
                except Exception as e:
                    print(f"[Healer] Pass 1 failed: {e}")

            # Pass 2 — fix remaining logic / deprecated API errors
            remaining = cats["methods"] + cats["deprecated"] + cats["other"]
            if remaining or working_errors:
                try:
                    prompt2 = _build_targeted_heal_prompt(working_code, cats, "other")
                    healed2 = kimi_heal(working_code, working_errors, extra_instruction=prompt2)
                    if healed2.strip():
                        h2_compile = compile_response(healed2, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                        h2_yml     = validate_yml(healed2)
                        h2_errors  = self._collect_errors(healed2, h2_compile, h2_yml)
                        print(f"[Healer] Pass 2 (logic/deprecated): {len(working_errors)} → {len(h2_errors)} errors.")
                        if not h2_errors:
                            return GenerationResult(
                                success=True, code=healed2,
                                attempts=MAX_ATTEMPTS + 3,
                                compile_result=h2_compile, yml_result=h2_yml,
                                elapsed_seconds=time.time() - start,
                            )
                        if _healed_is_better(working_code, healed2, working_errors, h2_errors):
                            working_code   = healed2
                            working_errors = h2_errors
                except Exception as e:
                    print(f"[Healer] Pass 2 failed: {e}")

        # ── Claude heal fallback (premium tier or when all else fails) ─────── #
        from inference.claude import is_available as claude_available, claude_heal
        if working_errors and claude_available():
            truncated = _is_truncation(working_errors)
            print(f"[Healer] Trying Claude heal ({len(working_errors)} errors"
                  + (", truncated" if truncated else "") + ").")
            try:
                heal_input = _balance_fences(working_code)
                heal_errors = working_errors[:8]
                if truncated:
                    from validation.compile_check import extract_java_blocks as _ejb3
                    blocks3 = _ejb3(heal_input)
                    missing3 = sum(_count_missing_braces(s) for _, s in blocks3)
                    heal_errors = [
                        f"[TRUNCATED] Code was cut off — ~{missing3} closing brace(s) missing. "
                        "Complete the class/method bodies first, then fix remaining errors."
                    ] + [e for e in working_errors[:6] if "reached end of file" not in e.lower()]
                healed_c = claude_heal(heal_input, heal_errors, instruction)
                if healed_c.strip():
                    hc_compile = compile_response(healed_c, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                    hc_yml     = validate_yml(healed_c)
                    hc_errors  = self._collect_errors(healed_c, hc_compile, hc_yml)
                    print(f"[Healer] Claude heal: {len(working_errors)} → {len(hc_errors)} errors.")
                    if not hc_errors:
                        return GenerationResult(
                            success=True, code=healed_c,
                            attempts=MAX_ATTEMPTS + 4,
                            compile_result=hc_compile, yml_result=hc_yml,
                            elapsed_seconds=time.time() - start,
                        )
                    if _healed_is_better(working_code, healed_c, working_errors, hc_errors):
                        working_code   = healed_c
                        working_errors = hc_errors
            except Exception as e:
                print(f"[Healer] Claude heal failed: {e}")

        # Return best-effort result (may be from healed code or original best attempt)
        # Last-ditch: if working code is STILL truncated (reached end of file) after
        # all healing attempts, do one compact-format full regeneration via cloud.
        # This handles plugins that are too large for a single response — ask the AI
        # to rewrite compactly (inner classes + lambdas) so it fits in one output.
        if _is_truncation(working_errors) and self.cloud_client:
            try:
                print("[Healer] Final compact-regen attempt for truncated output.")
                compact_instr = (
                    instruction.rstrip() +
                    "\n\nIMPORTANT: Your previous response was cut off before completing the code. "
                    "Rewrite the ENTIRE plugin more compactly so it fits in one response:\n"
                    "- Use private static nested classes instead of separate top-level classes\n"
                    "- Use lambdas instead of anonymous inner classes\n"
                    "- Omit Javadoc; one-line comments per method are fine\n"
                    "- Keep the total under 350 lines\n"
                    "Output must be 100% complete with ALL braces closed."
                )
                regen_result = self.cloud_client.complete(compact_instr)
                if regen_result and regen_result.strip():
                    regen_compile = compile_response(regen_result, paper_profile=self.paper_target_profile) if not self.skip_compile else CompileResult(success=True)
                    regen_yml     = validate_yml(regen_result)
                    regen_errors  = self._collect_errors(regen_result, regen_compile, regen_yml)
                    print(f"[Healer] Compact regen: {len(working_errors)} → {len(regen_errors)} errors.")
                    if len(regen_errors) < len(working_errors):
                        working_code   = regen_result
                        working_errors = regen_errors
                        best_compile   = regen_compile
                        best_yml       = regen_yml
                        if not regen_errors:
                            return GenerationResult(
                                success=True, code=working_code,
                                attempts=MAX_ATTEMPTS + 5,
                                compile_result=best_compile, yml_result=best_yml,
                                elapsed_seconds=time.time() - start,
                            )
            except Exception as e:
                print(f"[Healer] Compact regen failed: {e}")
        return GenerationResult(
            success=False,
            code=working_code,
            attempts=MAX_ATTEMPTS,
            compile_result=best_compile,
            test_compile_result=test_compile_result,
            yml_result=best_yml,
            final_errors=working_errors,
            elapsed_seconds=time.time() - start,
        )

    def generate_stream(self, instruction: str) -> Generator[str, None, None]:
        """
        Stream generation tokens (simulated — cloud APIs return full text at once).
        Yields the result in word-chunks for SSE display.
        """
        prompt = self.router.build_prompt(instruction)
        try:
            text, _ = generate_with_fallback(
                prompt, self.params,
                system_prompt=SYSTEM_PROMPT,
                instruction=instruction,
                tier=self.tier,
            )
        except Exception as exc:
            raise RuntimeError(f"Cloud generation failed: {exc}") from exc

        words = text.split(" ")
        chunk: list[str] = []
        for word in words:
            chunk.append(word)
            if len(chunk) >= 8:
                yield " ".join(chunk) + " "
                chunk = []
        if chunk:
            yield " ".join(chunk)


def run_validation_only(
    response: str,
    skip_compile: bool = False,
    paper_target_profile: str = DEFAULT_PAPER_PROFILE,
) -> GenerationResult:
    """
    Run the full validation suite on an already-generated response.
    Useful for re-validating cached or user-provided code.
    """
    static_issues = static_check_response(response)
    static_warnings = [str(i) for i in static_issues if i.severity != "error"]

    compile_result: CompileResult
    if skip_compile:
        compile_result = CompileResult(success=True)
        test_compile_result: CompileResult = CompileResult(success=True)
    else:
        try:
            compile_result = compile_response(response, paper_profile=paper_target_profile)
        except Exception as e:
            compile_result = CompileResult(success=False, errors=[str(e)])
        try:
            test_compile_result = compile_tests(response, paper_profile=paper_target_profile)
        except Exception:
            test_compile_result = CompileResult(success=True, files_compiled=0)

    yml_result = validate_yml(response)

    errors = get_static_errors(response)
    if not compile_result.success:
        errors.extend(compile_result.errors)
    if not yml_result.valid:
        errors.extend(yml_result.errors)

    return GenerationResult(
        success=len(errors) == 0,
        code=response,
        attempts=1,
        static_warnings=static_warnings,
        compile_result=compile_result,
        test_compile_result=test_compile_result,
        yml_result=yml_result,
        final_errors=errors,
    )
