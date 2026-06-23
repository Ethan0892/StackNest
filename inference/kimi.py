"""
inference/kimi.py — Kimi (Moonshot AI) API client.

Used for:
  1. Code generation fallback when local llama.cpp is unavailable
  2. Code validation using Kimi K2.5 (kimi-k2-0711-preview)
  3. Context-Aware Healing — fix compile/build errors automatically
  4. Server log analysis — diagnose Minecraft server errors

Requirements:
  Set KIMI_API_KEY in your .env / environment.
  Optional: KIMI_BASE_URL (default: https://api.moonshot.ai/v1)
             KIMI_GEN_MODEL (default: moonshot-v1-128k)
             KIMI_VALIDATE_MODEL (default: kimi-k2.5)
"""

import os
import logging
import time

import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
KIMI_BASE_URL       = os.getenv("KIMI_BASE_URL",       "https://api.moonshot.ai/v1")
KIMI_API_KEY        = os.getenv("KIMI_API_KEY",        "")
KIMI_GEN_MODEL      = os.getenv("KIMI_GEN_MODEL",      "moonshot-v1-128k")
KIMI_VALIDATE_MODEL = os.getenv("KIMI_VALIDATE_MODEL", "kimi-k2.5")

# --------------------------------------------------------------------------- #
# System prompts (specialised per role)                                       #
# --------------------------------------------------------------------------- #
_VALIDATE_SYSTEM = (
    "You are a senior Java code reviewer specialising in Paper 1.21 Minecraft plugins.\n"
    "Given plugin code (Java + plugin.yml), you must:\n"
    "1. Identify ALL compilation errors, deprecated API usages, missing imports, "
    "logic bugs, and plugin.yml specification mismatches.\n"
    "2. Prefix each issue with [ERROR] or [WARNING] followed by a short description.\n"
    "3. After the issue list, output a section starting with '## FIXES' containing "
    "the COMPLETE corrected code in the same markdown format "
    "(```java ... ``` and ```yaml ... ``` blocks).\n"
    "If the code is entirely correct, output only the word: VALID\n"
    "Do NOT add any other commentary."
)

_HEAL_SYSTEM = (
    "You are a senior Java developer specialising in Minecraft plugin development "
    "(Paper and Velocity).\n"
    "You will receive plugin code and a list of build errors.\n"
    "Fix EVERY error listed. Output the COMPLETE corrected plugin code in the same "
    "markdown format as the input (```java ... ``` and ```yaml ... ``` blocks).\n"
    "Do NOT add explanations, preamble, or summaries — only the corrected code."
)

_LOG_SYSTEM = (
    "You are an expert Minecraft server administrator and plugin developer.\n"
    "Analyse the server log provided.\n"
    "Important: prefer root-cause triage over counting exceptions.\n"
    "Group repeated stack traces into one issue.\n"
    "For each distinct issue include:\n"
    "  - probable cause\n"
    "  - likely plugin/component\n"
    "  - confidence (high/medium/low)\n"
    "  - actionable fix steps\n"
    "  - what to verify after applying the fix\n"
    "Use this markdown structure exactly:\n"
    "## Summary\n"
    "## Issues\n"
    "### Issue N — <short title>\n"
    "- Cause: ...\n"
    "- Suspect: ...\n"
    "- Confidence: ...\n"
    "- Fix: ...\n"
    "- Verify: ...\n"
    "## Priority Order\n"
    "If no errors are found, say: No errors or warnings detected."
)

_SKRIPT_HEAL_SYSTEM = (
    "You are an expert Skript developer for Minecraft (Paper/Spigot servers).\n"
    "Skript 2.x uses indentation-based scope (no braces), colons to open blocks, "
    "and %expr% for string interpolation.\n"
    "You will receive a Skript .sk file and a list of issues to fix.\n"
    "Fix EVERY issue listed. Output the COMPLETE corrected .sk file in exactly ONE "
    "```skript code block. Do NOT add explanations, preamble, or summaries — "
    "only the corrected code inside the fence."
)


# --------------------------------------------------------------------------- #
# Core HTTP helper                                                             #
# --------------------------------------------------------------------------- #
# kimi-k2.x reasoning models only accept temperature=1 — auto-enforce it
_TEMP1_MODELS = {"kimi-k2.5", "kimi-k2.5-turbo", "kimi-k2.6", "kimi-k2.6-turbo"}


def _post(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    raise_on_truncation: bool = False,
) -> str:
    """Send a single chat completion request to the Kimi API."""
    if model in _TEMP1_MODELS:
        temperature = 1.0
    if not KIMI_API_KEY:
        raise RuntimeError(
            "KIMI_API_KEY is not set. Add it to your .env file to enable Kimi features."
        )

    headers = {
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        resp = requests.post(
            f"{KIMI_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        # Kimi enforces a hard concurrency cap of 3 parallel requests.
        # When exceeded it returns 429 with "concurrency" in the message.
        # Also handles "engine_overloaded_error" — global Kimi overload.
        # Sleep and retry once before failing over for either case.
        # Billing suspensions ("insufficient balance") are NOT retried —
        # mark the backend unhealthy immediately so all workers skip it.
        if resp.status_code == 429:
            err_text = resp.text.lower()
            if "insufficient balance" in err_text or "suspended" in err_text:
                try:
                    from inference.watchdog import _write_cross_proc_down
                    _write_cross_proc_down("kimi", "billing suspended")
                except Exception:
                    pass
                resp.raise_for_status()  # raises HTTPError → caught below
            elif "concurrency" in err_text or "overloaded" in err_text:
                time.sleep(5)
                resp = requests.post(
                    f"{KIMI_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=120,
                )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError("Kimi API request timed out (120s)")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Kimi API HTTP error: {e.response.status_code} — {e.response.text[:200]}")

    data = resp.json()
    choice  = data["choices"][0]
    content = choice["message"]["content"].strip()
    # Detect token-limit truncation — raise so generate_with_fallback falls
    # through to the next backend (Claude / Gemini) instead of returning
    # broken truncated Java.  Only applied when the caller opts in via
    # raise_on_truncation (generation calls only — not heal/validate).
    if raise_on_truncation:
        finish_reason = choice.get("finish_reason", "")
        if finish_reason == "length":
            raise RuntimeError(
                f"Kimi output truncated (finish_reason=length) at {len(content)} chars "
                "— falling back to next backend"
            )
    return content


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def is_available() -> bool:
    """Return True if a Kimi API key is configured and the account is not billing-suspended."""
    if not KIMI_API_KEY:
        return False
    try:
        from inference.watchdog import _cross_proc_is_healthy
        return _cross_proc_is_healthy("kimi")
    except Exception:
        return True  # watchdog unavailable — assume healthy


def heal_available() -> bool:
    """Return True if at least one healing backend is usable.

    kimi_heal() falls back to Gemini internally when Kimi is billing-suspended,
    so healing paths should gate on this rather than is_available() alone.
    """
    if is_available():
        return True
    try:
        from inference.gemini import is_available as _gemini_ok
        return _gemini_ok()
    except Exception:
        return False


def kimi_generate(instruction: str, system_prompt: str) -> str:
    """
    Generate a plugin using Kimi as a fallback to the local model.
    Uses the same system_prompt as the local model for consistency.
    Raises RuntimeError (causing fallback to next backend) when Kimi hits
    its token ceiling and returns truncated output.
    """
    log.info("[Kimi] Fallback generation for: %s", instruction[:80])
    return _post(KIMI_GEN_MODEL, system_prompt, instruction, max_tokens=32768, temperature=0.15,
                 raise_on_truncation=True)


def kimi_validate(code: str) -> dict:
    """
    Validate plugin code using Kimi K2.5.

    Returns:
        {
            "valid":      bool,
            "issues":     list[str],   # "[ERROR] ..." / "[WARNING] ..." lines
            "fixed_code": str | None,  # corrected markdown code, or None if valid/no fix
        }
    """
    log.info("[Kimi] Validating %d chars of code", len(code))
    result = _post(KIMI_VALIDATE_MODEL, _VALIDATE_SYSTEM, code, max_tokens=4096)

    if result.strip().upper() == "VALID":
        return {"valid": True, "issues": [], "fixed_code": None}

    issues: list[str] = []
    fixed_lines: list[str] = []
    in_fixes = False

    for line in result.split("\n"):
        if line.strip().startswith("## FIXES"):
            in_fixes = True
            continue
        if in_fixes:
            fixed_lines.append(line)
        elif line.startswith("[ERROR]") or line.startswith("[WARNING]"):
            issues.append(line.strip())

    fixed_code = "\n".join(fixed_lines).strip() or None
    has_errors = any("[ERROR]" in i for i in issues)

    return {
        "valid": not has_errors,
        "issues": issues,
        "fixed_code": fixed_code,
    }


def kimi_heal(code: str, errors: list[str], extra_instruction: str = "") -> str:
    """
    Use Kimi K2.5 to fix build/compile errors in plugin code.

    Args:
        code:              The full plugin markdown (java + yaml blocks).
        errors:            List of error strings from javac / static checker / yml checker.
        extra_instruction: Optional targeted instruction prepended to the prompt
                           (e.g. "Fix imports only").

    Returns:
        Corrected plugin code in the same markdown format.
    """
    if not errors:
        return code

    error_block = "\n".join(f"- {e}" for e in errors)
    focus = f"{extra_instruction.strip()}\n\n" if extra_instruction.strip() else ""
    user_msg = (
        f"{focus}"
        f"Plugin code:\n\n{code}\n\n"
        f"Build errors to fix:\n{error_block}\n\n"
        "Output the complete corrected code."
    )
    log.info("[Kimi] Healing %d errors%s", len(errors), " (targeted)" if extra_instruction else "")
    try:
        return _post(KIMI_VALIDATE_MODEL, _HEAL_SYSTEM, user_msg, max_tokens=16384)
    except RuntimeError as exc:
        log.warning("[Kimi] kimi_heal failed (%s) — falling back to Gemini heal", exc)
        from inference.gemini import is_available as _gemini_ok, gemini_heal as _gemini_heal
        if _gemini_ok():
            return _gemini_heal(code, errors, extra_instruction or "Fix all listed errors")
        raise


def kimi_heal_skript(code: str, errors: list[str]) -> str:
    """
    Use Kimi to fix syntax/logic errors in a Skript .sk file.

    Args:
        code:   Raw LLM output containing the ```skript block.
        errors: List of issue strings from validate_skript().

    Returns:
        Corrected LLM response containing the fixed ```skript block.
    """
    if not errors:
        return code

    error_block = "\n".join(f"- {e}" for e in errors)
    user_msg = (
        f"Skript code:\n\n{code}\n\n"
        f"Issues to fix:\n{error_block}\n\n"
        "Output the complete corrected Skript code."
    )
    log.info("[Kimi] Healing Skript: %d issues", len(errors))
    return _post(KIMI_VALIDATE_MODEL, _SKRIPT_HEAL_SYSTEM, user_msg, max_tokens=8192)


def kimi_analyze_log(log_text: str) -> str:
    """
    Analyse a Minecraft server log and return a structured markdown diagnosis.
    Trims input to last 8000 chars to stay within context limits.
    """
    trimmed = log_text[-8000:] if len(log_text) > 8000 else log_text
    log.info("[Kimi] Analysing server log (%d chars)", len(trimmed))
    return _post(KIMI_GEN_MODEL, _LOG_SYSTEM, trimmed, max_tokens=2048, temperature=0.2)


# ── Server Setup Assistant ─────────────────────────────────────────────────

_SETUP_SYSTEM_FULL = """\
You are a senior Minecraft server administrator with deep expertise in plugin ecosystems.
Given a server description, produce a COMPLETE server setup guide.

Use this EXACT structure:

## Plugin Stack
For every recommended plugin use this sub-format:
### [Plugin Name](download-url)
- **Purpose:** one sentence
- **Download:** Modrinth URL preferred, SpigotMC as fallback — use real, verified URLs
- **Conflicts:** list any known incompatibilities, or "None known"

## Load Order
Numbered list of plugins in the recommended enable order with a one-line reason for each position.

## Sample Configs
For every plugin that needs non-default configuration, output a commented YAML block:
### PluginName — config.yml
```yaml
# … fully commented settings …
```

## Known Conflicts & Gotchas
Bullet list of common issues with this specific combination.

## Quick-Start Checklist
Short ordered checklist to get the server running correctly.

Rules:
- Use REAL, currently maintained plugins only.
- Prefer Paper-native plugins over Spigot-only ones.
- All download links must be real (modrinth.com or spigotmc.org).
- Do NOT truncate — output the complete guide.
"""

_SETUP_SYSTEM_MULTI = """\
You are a senior Minecraft infrastructure architect specialising in multi-server networks.
Given a network description, design the complete server infrastructure.

Use this EXACT structure:

## Network Architecture
ASCII or text diagram of the node layout (proxy → servers).

## Software Recommendations
Recommended proxy software (Velocity/BungeeCord) and game-server software (Paper/Purpur) with versions.

## Per-Node Plugin Stacks
For each node type (proxy, lobby, survival, etc.) follow this format:

### Node: [Name] — [Software]
#### Plugin Stack
### [Plugin Name](download-url)
- **Purpose:** one sentence
- **Download:** real URL
- **Conflicts:** …

## Cross-Server Configuration
How plugins communicate across nodes (LuckPerms sync, economy sync, messaging channels, etc.) with sample config snippets.

## Load Order per Node Type

## Known Conflicts & Gotchas

## Scalability Notes
How to scale each node type under player load.

Rules:
- Use real, currently maintained plugins only.
- All download links must be real (modrinth.com or spigotmc.org).
- Do NOT truncate.
"""

_SETUP_SYSTEM_BASIC = """\
You are an expert Minecraft server administrator.
Your task: read the server description carefully, identify the exact server type, and produce a plugin list that precisely matches it.

STRICT RULES:
- Identify the server type from the description (survival, economy, creative, PvP, factions, prison, skyblock, minigames, SMP, etc.).
- Recommend ONLY plugins that directly serve the described server type.
  * Survival/economy server → economy, jobs, shops, land claim, permissions. NOT creative/plot plugins.
  * Creative server → plot management, build tools, world editor. NOT economy/jobs plugins.
  * PvP/factions server → faction tools, combat, raiding. NOT economy-first plugins.
  * Prison server → mine ranks, cell management, economy. NOT survival/creative tools.
- 6–9 plugins maximum. Fewer quality picks beat a padded list.
- Every download URL must be a real, currently maintained plugin.
  Modrinth: https://modrinth.com/plugin/<slug>   SpigotMC: https://www.spigotmc.org/resources/<id>
- One sentence per plugin — no padding.
- Do NOT write an intro paragraph. Start directly with the first plugin heading.
- Do NOT truncate. Output every plugin before finishing.

OUTPUT FORMAT (follow exactly):

### [PluginName](https://exact-download-url)
**Purpose:** One sentence explaining what this adds to THIS specific server type.

[repeat for each plugin]

## Setup Notes
- [load order note, known conflict, or important first-run gotcha]
- [second note if relevant]

"""


def kimi_setup_assistant(description: str, mode: str = "full") -> str:
    """
    Generate a server setup guide.

    mode:
        "basic"  — short plugin list (free tier via Gemini, but Kimi fallback here)
        "full"   — complete guide with configs (pro)
        "multi"  — multi-server network design (studio)
    """
    systems = {
        "basic": _SETUP_SYSTEM_BASIC,
        "full":  _SETUP_SYSTEM_FULL,
        "multi": _SETUP_SYSTEM_MULTI,
    }
    system = systems.get(mode, _SETUP_SYSTEM_FULL)
    max_tokens = 2500 if mode == "basic" else (6000 if mode == "multi" else 5000)
    log.info("[Kimi] setup-assistant mode=%s, desc=%d chars", mode, len(description))
    return _post(KIMI_GEN_MODEL, system, description.strip(), max_tokens=max_tokens, temperature=0.3)
