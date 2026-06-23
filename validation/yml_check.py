"""
validation/yml_check.py — Validate plugin.yml and cross-check it against Java code.

Checks:
  1. YAML is parseable
  2. Required keys present (name, version, main, api-version)
  3. api-version is current (1.21)
  4. main class path format is correct (com.example.PluginName)
  5. Commands declared in plugin.yml are registered in Java onEnable
  6. Main class name in plugin.yml matches actual Java class declaration
"""

import re
from dataclasses import dataclass, field

import yaml


@dataclass
class YmlCheckResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def format_errors(self) -> str:
        if not self.errors:
            return ""
        return "plugin.yml errors:\n" + "\n".join(f"  - {e}" for e in self.errors)


REQUIRED_KEYS = {"name", "version", "main", "api-version"}
VALID_API_VERSIONS = {"1.19", "1.20", "1.21", "1.21.1", "1.21.4"}


def extract_yml_block(response: str) -> str | None:
    """Extract plugin.yml content from a model response."""
    # Pattern 1: labelled as # plugin.yml
    m = re.search(r"#\s*plugin\.yml\s*\n```yaml\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1)
    # Pattern 2: first yaml block in response
    m = re.search(r"```yaml\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1)
    return None


def extract_java_blocks(response: str) -> list[str]:
    """Return raw Java code strings from any recognisable code fence."""
    # Canonical ```java blocks first
    results = re.findall(r"```java\n(.*?)```", response, re.DOTALL)
    if results:
        return results
    # Fallback: any fenced block whose content looks like Java
    for block in re.findall(r"```(?:text|plaintext|code|java)?\s*\n(.*?)```", response, re.DOTALL | re.IGNORECASE):
        if re.search(r'(?:^|\n)\s*(?:package\s+[\w.]+\s*;|public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*(?:class|interface|enum|record)\s+\w+)', block):
            results.append(block)
    return results


def check_command_registration(yml_data: dict, java_blocks: list[str]) -> list[str]:
    """
    Verify that commands declared in plugin.yml are registered via
    getCommand("name").setExecutor(...) in the Java code.
    """
    warnings = []
    commands = yml_data.get("commands", {})
    if not commands:
        return warnings

    all_java = "\n".join(java_blocks)

    for cmd_name in commands.keys():
        # Check for getCommand("cmdName") or getCommand('cmdName')
        pattern = rf'getCommand\s*\(\s*["\']({re.escape(cmd_name)})["\']'
        if not re.search(pattern, all_java, re.IGNORECASE):
            warnings.append(
                f"Command '{cmd_name}' declared in plugin.yml but no "
                f"getCommand(\"{cmd_name}\") call found in Java. "
                f"Add: getCommand(\"{cmd_name}\").setExecutor(this); to onEnable()."
            )

    return warnings


def check_main_class_exists(yml_data: dict, java_blocks: list[str]) -> list[str]:
    """
    Verify the 'main:' class path matches an actual class declaration in the Java code.
    """
    errors = []
    main_path = yml_data.get("main", "")
    if not main_path:
        return errors

    # Extract class name from path (last segment after the last dot)
    class_name = main_path.split(".")[-1]
    all_java = "\n".join(java_blocks)

    # Handle modifiers between 'public' and 'class' (abstract, final, sealed, etc.)
    class_pattern = (
        rf"public\s+(?:(?:abstract|final|sealed|non-sealed|strictfp)\s+)*"
        rf"class\s+{re.escape(class_name)}\b"
    )
    if not re.search(class_pattern, all_java):
        errors.append(
            f"main: '{main_path}' references class '{class_name}' "
            f"but no 'public class {class_name}' found in generated Java code."
        )

    return errors


def check_listener_registration(java_blocks: list[str]) -> list[str]:
    """Check if Listener classes exist but are never registered."""
    warnings = []
    all_java = "\n".join(java_blocks)

    implements_listener = bool(re.search(r"implements\s+Listener\b", all_java))
    registers_events = bool(re.search(r"registerEvents\s*\(", all_java))

    if implements_listener and not registers_events:
        warnings.append(
            "Class implements Listener but registerEvents() not found. "
            "Add: Bukkit.getPluginManager().registerEvents(this, this); to onEnable()."
        )

    return warnings


def validate_response(response: str) -> YmlCheckResult:
    """Run all plugin.yml and cross-checks on a model response."""
    yml_text = extract_yml_block(response)
    java_blocks = extract_java_blocks(response)

    # Velocity plugins use @Plugin annotations + velocity-plugin.json, not plugin.yml.
    # If any java block imports com.velocitypowered the yml rules don't apply.
    is_velocity = any(
        re.search(r"import\s+com\.velocitypowered\.", blk)
        for blk in java_blocks
    )
    if is_velocity:
        return YmlCheckResult(valid=True, warnings=[])

    # No plugin.yml block — may be a class-only chunk, don't hard-fail
    if yml_text is None:
        return YmlCheckResult(
            valid=True,
            warnings=["No plugin.yml block found — OK for class-level snippets."],
        )

    errors: list[str] = []
    warnings: list[str] = []
    data: dict = {}

    # --- Parse YAML ---
    try:
        data = yaml.safe_load(yml_text) or {}
    except yaml.YAMLError as e:
        return YmlCheckResult(valid=False, errors=[f"YAML parse error: {e}"])

    if not isinstance(data, dict):
        return YmlCheckResult(valid=False, errors=["plugin.yml root is not a YAML mapping"])

    # --- Required keys ---
    missing = REQUIRED_KEYS - set(data.keys())
    for k in sorted(missing):
        errors.append(f"Missing required key: '{k}'")

    # --- api-version ---
    api_ver = str(data.get("api-version", "")).strip("'\"")
    if api_ver and api_ver not in VALID_API_VERSIONS:
        errors.append(
            f"Invalid api-version: '{api_ver}'. "
            f"Expected one of: {sorted(VALID_API_VERSIONS)}"
        )

    # --- main class format ---
    main_val = data.get("main", "")
    if main_val and not re.match(r"^[\w]+(\.[\w]+)+$", str(main_val)):
        errors.append(
            f"Invalid main class path: '{main_val}'. "
            "Expected format: com.example.PluginName"
        )

    # --- Cross-checks with Java ---
    if java_blocks:
        errors.extend(check_main_class_exists(data, java_blocks))
        warnings.extend(check_command_registration(data, java_blocks))
        warnings.extend(check_listener_registration(java_blocks))

    return YmlCheckResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        data=data,
    )
