"""
scripts/validate_jsonl.py — Validate training JSONL for token budgets and schema.

Checks:
  1. JSON parseable on every line
  2. Required keys present: system, instruction, response
  3. Total token count <= MAX_TOKENS
  4. response contains at least one java code block
  5. plugin.yml block has required keys if present

Usage:
    python scripts/validate_jsonl.py --input data/processed/train.jsonl
    python scripts/validate_jsonl.py --input data/processed/train.jsonl --fix  # drops bad lines
"""

import argparse
import json
import pathlib
import re

from rich.console import Console
from rich.table import Table

console = Console()

MAX_TOKENS = 1800       # hard ceiling per training example
WARN_TOKENS = 1400      # soft warning threshold
REQUIRED_KEYS = {"system", "instruction", "response"}


# --------------------------------------------------------------------------- #
# Token counter — tiktoken with cl100k fallback                               #
# --------------------------------------------------------------------------- #

def make_token_counter():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(enc.encode(text))
    except ImportError:
        # Rough heuristic if tiktoken not installed: 1 token ≈ 4 chars
        console.print("[yellow]tiktoken not installed — using character-count heuristic[/yellow]")
        return lambda text: len(text) // 4


count_tokens = make_token_counter()


def count_entry_tokens(entry: dict) -> int:
    return sum(
        count_tokens(v) for v in entry.values() if isinstance(v, str)
    )


# --------------------------------------------------------------------------- #
# Validators                                                                   #
# --------------------------------------------------------------------------- #

def has_java_block(response: str) -> bool:
    return bool(re.search(r"```java", response))


def has_plugin_yml_block(response: str) -> bool:
    return bool(re.search(r"```yaml", response))


def validate_plugin_yml_fields(response: str) -> list[str]:
    """Return list of issues with plugin.yml block, empty if OK."""
    import yaml
    issues = []
    m = re.search(r"# plugin\.yml\n```yaml\n(.*?)```", response, re.DOTALL)
    if not m:
        return []  # No plugin.yml block — not always required for class-level chunks

    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return [f"plugin.yml YAML parse error: {e}"]

    if not isinstance(data, dict):
        return ["plugin.yml is not a YAML mapping"]

    REQUIRED = {"name", "version", "main", "api-version"}
    missing = REQUIRED - set(data.keys())
    if missing:
        issues.append(f"plugin.yml missing keys: {missing}")

    api_ver = data.get("api-version", "")
    if api_ver not in ("1.19", "1.20", "1.21", "1.21.4"):
        issues.append(f"Bad api-version: '{api_ver}' (expected 1.21)")

    return issues


def validate_entry(entry: dict, index: int) -> list[str]:
    issues = []

    # Schema check
    missing_keys = REQUIRED_KEYS - set(entry.keys())
    if missing_keys:
        issues.append(f"Missing keys: {missing_keys}")
        return issues  # Can't do further checks

    # Empty values
    for k in REQUIRED_KEYS:
        if not str(entry[k]).strip():
            issues.append(f"Empty value for '{k}'")

    # Token budget
    tokens = count_entry_tokens(entry)
    if tokens > MAX_TOKENS:
        issues.append(f"Token count {tokens} > max {MAX_TOKENS}")
    elif tokens > WARN_TOKENS:
        issues.append(f"WARN: Token count {tokens} > soft limit {WARN_TOKENS}")

    # Response quality
    response = entry["response"]
    if not has_java_block(response):
        issues.append("response has no ```java code block")

    # plugin.yml issues
    yml_issues = validate_plugin_yml_fields(response)
    issues.extend(yml_issues)

    return issues


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate training JSONL")
    parser.add_argument("--input", default="data/processed/train.jsonl")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Rewrite file with invalid lines dropped (WARN lines kept)",
    )
    args = parser.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        console.print(f"[red]File not found: {in_path}[/red]")
        return

    console.rule("[bold blue]StackNest — JSONL Validation")

    raw_lines = in_path.read_text().splitlines()
    entries: list[dict] = []
    parse_errors = 0

    for i, line in enumerate(raw_lines):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            console.print(f"  [red]Line {i+1}: JSON parse error — {e}[/red]")
            parse_errors += 1

    table = Table(title=f"Validation Results — {in_path.name}", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Tokens", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("Issues")

    ok_count = 0
    warn_count = 0
    error_count = 0
    keep_entries: list[dict] = []

    for i, entry in enumerate(entries):
        issues = validate_entry(entry, i)
        tokens = count_entry_tokens(entry)

        errors = [x for x in issues if not x.startswith("WARN:")]
        warnings = [x for x in issues if x.startswith("WARN:")]

        if errors:
            error_count += 1
            status = "[red]ERROR[/red]"
            issue_str = "; ".join(errors[:2])
        elif warnings:
            warn_count += 1
            status = "[yellow]WARN[/yellow]"
            issue_str = "; ".join(warnings[:1])
            keep_entries.append(entry)
        else:
            ok_count += 1
            status = "[green]OK[/green]"
            issue_str = ""
            keep_entries.append(entry)

        if errors or warnings:
            table.add_row(str(i + 1), str(tokens), status, issue_str)

    console.print(table)

    # Token statistics
    all_tokens = [count_entry_tokens(e) for e in entries]
    if all_tokens:
        console.print(f"\nToken stats:")
        console.print(f"  Min:    {min(all_tokens)}")
        console.print(f"  Max:    {max(all_tokens)}")
        console.print(f"  Avg:    {sum(all_tokens) // len(all_tokens)}")
        console.print(f"  Total:  {sum(all_tokens)}")

    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Total lines:  {len(entries)}")
    console.print(f"  [green]OK:     {ok_count}[/green]")
    console.print(f"  [yellow]Warn:   {warn_count}[/yellow]")
    console.print(f"  [red]Error:  {error_count}[/red]")
    if parse_errors:
        console.print(f"  [red]JSON parse errors: {parse_errors}[/red]")

    if args.fix and (error_count > 0 or parse_errors > 0):
        backup = in_path.with_suffix(".bak.jsonl")
        in_path.rename(backup)
        with open(in_path, "w") as f:
            for entry in keep_entries:
                f.write(json.dumps(entry) + "\n")
        console.print(
            f"\n[bold yellow]Fixed:[/bold yellow] dropped {error_count + parse_errors} invalid lines. "
            f"Original backed up to [bold]{backup}[/bold]"
        )
        console.print(f"Kept: {len(keep_entries)} entries → [bold]{in_path}[/bold]")
    elif error_count > 0:
        console.print(f"\n[dim]Run with --fix to drop invalid lines automatically.[/dim]")

    if error_count == 0 and parse_errors == 0:
        console.print(f"\n[bold green]All entries valid.[/bold green]")
        next_cmd = f"python scripts/embed.py --input {in_path} --output data/embeddings/chromadb"
        console.print(f"Next: [dim]{next_cmd}[/dim]")


if __name__ == "__main__":
    main()
