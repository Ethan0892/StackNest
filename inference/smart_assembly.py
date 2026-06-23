"""
inference/smart_assembly.py — Two-phase plugin generation to reduce API cost.

Phase 1 (cheap):   Extract structured attributes from the user instruction via a
                   small Kimi call (~150 output tokens, costs almost nothing).
Phase 2 (focused): Build an augmented system prompt that includes pre-validated
                   feature blocks and a compact instruction that asks the model to
                   implement ONLY the custom logic — not the boilerplate.

Result: ~60% fewer output tokens per generation (output tokens cost 5× more than
        input tokens, so real-world saving is roughly 50–55% per generation).

Typical token budget comparison:
  Standard path:  ~2 500 output tokens (full plugin from scratch)
  Smart path:     ~200 (extraction) + ~800 (custom logic only) = ~1 000 total

The feature block library lives in templates/features/*.java.
Each file is a self-contained, pre-validated Java snippet with
// TODO: implement markers for custom logic sections.

Falls back gracefully at every step:
  - Kimi unavailable → keyword-based attribute extraction
  - Feature block file missing → silently skipped
  - Any exception → caller receives (original_system_prompt, original_instruction)
"""

from __future__ import annotations

import json
import logging
import pathlib
import re

log = logging.getLogger(__name__)

FEATURES_DIR = pathlib.Path(__file__).parent.parent / "templates" / "features"

# ── Known feature names ─────────────────────────────────────────────────────
KNOWN_FEATURES: set[str] = {
    "vault_hook", "coin_balance",
    "inventory_holder", "click_handler",
    "admin_command", "player_command", "tab_completer",
    "sqlite_manager", "config_manager", "pdc_storage",
    "repeating_task", "folia_scheduler",
}

# ── Keyword → feature mapping (pure-Python fallback) ────────────────────────
_KEYWORD_MAP: dict[str, list[str]] = {
    "vault_hook":       ["vault", " economy ", "money balance", "eco plugin",
                         "vault economy", "vault api"],
    "coin_balance":     ["coin", " coins", "custom currenc", "token balance",
                         "point balance", "points system", "token system"],
    "inventory_holder": ["gui", "chest gui", "inventory menu", "inventory ui",
                         "open menu", "shop gui", "custom menu"],
    "click_handler":    ["gui", "menu click", "inventory click", "shop gui",
                         "chest menu", "open a menu", "open menu"],
    "admin_command":    ["/admin", "admin command", "admin-only", "op command",
                         "staff command", "admin sub"],
    "player_command":   ["command", "player command", "use the command", "/cmd"],
    "tab_completer":    ["tab complet", "autocomplete", "tab completion"],
    "sqlite_manager":   ["sqlite", "mysql", "sql database", "store data in",
                         "persistent storage", "save data to", "database"],
    "config_manager":   ["config", "configurable", "config.yml", "setting",
                         "message configur", "configurable message"],
    "pdc_storage":      ["pdc", "persistent data", "nbt tag", "item data",
                         "entity data", "player data"],
    "repeating_task":   ["every ", "timer", "interval", "repeat every",
                         "periodic", "broadcast every", "scheduler", "repeating"],
    "folia_scheduler":  ["folia", "folia-compatible", "folia safe",
                         "threaded region"],
}

# Features that only make sense as a pair
_PAIRED: list[tuple[str, str]] = [
    ("click_handler", "inventory_holder"),  # click_handler requires inventory_holder
]

# Base type keyword mapping
_BASE_MAP: list[tuple[str, list[str]]] = [
    ("gui_plugin",       ["gui", "menu", "shop gui", "inventory menu", "chest gui",
                          "open a chest", "click items"]),
    ("scheduler_plugin", ["every ", "timer", "interval", "repeating task",
                          "broadcast every", "periodic"]),
    ("event_plugin",     ["on join", "on death", "on damage", "on pvp", "on quit",
                          "on login", "event listener", "listen for", "when a player"]),
    ("command_plugin",   ["/", "command"]),
]


# ── Extraction prompt ────────────────────────────────────────────────────────
_EXTRACT_SYSTEM = (
    "You are a JSON extractor. "
    "Respond with ONLY valid JSON — no markdown, no explanation, no ```."
)

def _extract_prompt(instruction: str) -> str:
    feature_list = " | ".join(sorted(KNOWN_FEATURES))
    return (
        "Extract plugin attributes from this Minecraft Paper plugin request as JSON.\n"
        "Rules:\n"
        "- name: PascalCase, end with Plugin (e.g. ShopPlugin, CooldownPlugin)\n"
        "- base: exactly one of: command_plugin | event_plugin | gui_plugin | "
        "scheduler_plugin | full_plugin\n"
        f"- features: zero to five of: {feature_list}\n"
        "- custom_logic: one sentence describing ONLY the unique business logic "
        "that is NOT already covered by the selected features above\n\n"
        f"Request: {instruction}\n\n"
        "JSON:"
    )


# ── Pure-Python keyword fallback ─────────────────────────────────────────────
def _keyword_extract(instruction: str) -> dict:
    """Attribute extraction without any AI call."""
    low = instruction.lower()

    # Base type
    base = "full_plugin"
    for base_type, keywords in _BASE_MAP:
        if any(kw in low for kw in keywords):
            base = base_type
            break

    # Features (cap at 5)
    features: list[str] = []
    for feature, keywords in _KEYWORD_MAP.items():
        if any(kw in low for kw in keywords):
            features.append(feature)
    features = features[:5]

    # Enforce pairs — if click_handler selected but not inventory_holder, add it
    for dependent, required in _PAIRED:
        if dependent in features and required not in features:
            features.append(required)
    features = features[:5]

    # Name: look for "XxxPlugin" / "XxxSystem" pattern, else derive from content words
    m = re.search(
        r'\b([A-Z][a-zA-Z]{2,}(?:Plugin|System|Manager|Guard|Kit|Bot|Hook))\b',
        instruction,
    )
    if m:
        name = m.group(1)
    else:
        stop = {"create", "make", "build", "plugin", "that", "which", "with",
                "and", "for", "the", "paper", "minecraft", "server", "player",
                "players", "when", "will", "can", "add", "using", "use", "have",
                "where", "who", "they", "save", "show", "display", "allow",
                "allows", "give", "get", "set", "run", "runs", "lets", "also",
                "simple", "basic", "custom", "feature", "features", "items",
                "item", "every", "each", "into", "from", "their", "them",
                "repeating", "then", "does", "them", "just", "only", "want",
                "wants", "need", "needs", "should", "would"}
        words = re.findall(r'\b[a-zA-Z]{3,}\b', instruction)
        content = [w.capitalize() for w in words if w.lower() not in stop][:2]
        name = ("".join(content) + "Plugin") if content else "CustomPlugin"

    return {
        "name": name,
        "base": base,
        "features": features,
        "custom_logic": instruction[:300].rstrip(),
    }


# ── AI extraction ─────────────────────────────────────────────────────────────
def extract_attributes(instruction: str) -> dict:
    """
    Use a cheap Kimi call (~150 output tokens) to extract structured attributes.
    Falls back to keyword-based extraction if Kimi is unavailable or returns
    unparseable output.
    """
    try:
        from inference.kimi import is_available as kimi_ok, _post, KIMI_GEN_MODEL  # noqa
        if not kimi_ok():
            raise RuntimeError("Kimi not configured")

        raw = _post(
            KIMI_GEN_MODEL,
            _EXTRACT_SYSTEM,
            _extract_prompt(instruction),
            max_tokens=200,
            temperature=0.0,
        )

        # Strip markdown fences if the model wraps the JSON anyway
        raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r'\s*```$', '', raw.strip())

        attrs = json.loads(raw)

        # Validate / fill defaults
        if not isinstance(attrs.get("name"), str) or not attrs["name"]:
            attrs["name"] = "CustomPlugin"
        if attrs.get("base") not in {
            "command_plugin", "event_plugin", "gui_plugin",
            "scheduler_plugin", "full_plugin"
        }:
            attrs["base"] = "full_plugin"
        if not isinstance(attrs.get("features"), list):
            attrs["features"] = []
        # Filter out any hallucinated feature names
        attrs["features"] = [
            f for f in attrs["features"] if f in KNOWN_FEATURES
        ][:5]
        if not isinstance(attrs.get("custom_logic"), str) or not attrs["custom_logic"]:
            attrs["custom_logic"] = instruction[:300].rstrip()

        # Enforce pairs
        for dependent, required in _PAIRED:
            if dependent in attrs["features"] and required not in attrs["features"]:
                attrs["features"].append(required)
        attrs["features"] = attrs["features"][:5]

        log.info(
            "[SmartAssembly] Extracted: name=%s base=%s features=%s",
            attrs["name"], attrs["base"], attrs["features"],
        )
        return attrs

    except Exception as exc:
        log.warning(
            "[SmartAssembly] AI extraction failed (%s) — using keyword fallback", exc
        )
        return _keyword_extract(instruction)


# ── Feature block loader ─────────────────────────────────────────────────────
def load_feature_blocks(features: list[str]) -> dict[str, str]:
    """Return {feature_name: java_code} for each available feature block file."""
    blocks: dict[str, str] = {}
    for feature in features:
        path = FEATURES_DIR / f"{feature}.java"
        if path.exists():
            try:
                blocks[feature] = path.read_text(encoding="utf-8")
            except OSError as exc:
                log.debug("[SmartAssembly] Could not read feature block %s: %s", feature, exc)
        else:
            log.debug("[SmartAssembly] Feature block not found: %s.java", feature)
    return blocks


# ── Main entry point ─────────────────────────────────────────────────────────
def assemble_focused_prompt(
    instruction: str,
    base_system_prompt: str,
) -> tuple[str, str, list[str]]:
    """
    Extract attributes, load feature blocks, and return an augmented
    (system_prompt, instruction, features_used) triple for the generation call.

    The augmented system prompt injects pre-validated feature blocks so the
    generation model only needs to produce custom logic, not boilerplate.

    Returns:
        (augmented_system_prompt, focused_instruction, features_used)
        features_used is the list of feature block names that were injected.

    On any failure, returns (base_system_prompt, instruction, []) unchanged so
    the caller can proceed with standard generation.
    """
    try:
        attrs = extract_attributes(instruction)
        blocks = load_feature_blocks(attrs.get("features", []))

        augmented_system = base_system_prompt

        if blocks:
            block_section = (
                "\n\n## PRE-VALIDATED FEATURE IMPLEMENTATIONS\n"
                "The following Java code blocks are pre-validated against Paper 26.1 and compile "
                "correctly. Copy them VERBATIM into your output — do NOT rewrite them. "
                "Only modify sections marked with `// TODO: implement`.\n"
            )
            for feature_name, code in blocks.items():
                block_section += f"\n### {feature_name}\n```java\n{code.strip()}\n```\n"
            augmented_system += block_section

        name         = attrs.get("name", "CustomPlugin")
        custom_logic = attrs.get("custom_logic", instruction)
        feature_list = ", ".join(attrs.get("features", [])) or "none"

        focused_instruction = (
            f"Build a Paper 26.1 plugin named {name}.\n\n"
            f"Pre-validated components are provided in your context "
            f"(features included: {feature_list}). "
            f"Copy those blocks VERBATIM — do NOT rewrite any pre-validated code. "
            f"Only write:\n"
            f"  1. The plugin class structure (onEnable, onDisable, plugin.yml)\n"
            f"  2. The following custom logic (this is the ONLY part you write from scratch):\n\n"
            f"     {custom_logic}\n\n"
            f"Original full request for context: {instruction}"
        )

        return augmented_system, focused_instruction, list(blocks.keys())

    except Exception as exc:
        log.warning("[SmartAssembly] assemble_focused_prompt failed (%s) — using standard prompt", exc)
        return base_system_prompt, instruction, []
