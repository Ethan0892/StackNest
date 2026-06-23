"""
scripts/chunk.py — Split ingested plugins into training-sized chunks.

Strategy:
  Level 1: Full plugin (main class + plugin.yml)   — plugins < 200 lines total
  Level 2: Individual feature class                — files 50–300 lines
  Level 3: Method-level snippet + class header     — files > 300 lines

Output: data/processed/chunks.jsonl
  Each line: { "repo", "chunk_type", "java_code", "plugin_yml", "pom_xml" }

Usage:
    python scripts/chunk.py --input data/raw --output data/processed
"""

import argparse
import json
import pathlib
import re

from rich.console import Console
from rich.progress import track

console = Console()


# --------------------------------------------------------------------------- #
# Version metadata per repo                                                   #
# key  = repo directory name (same as data["repo"])                          #
# min_version / max_version = Paper API version as float                     #
# max_version None → use 9999.0 sentinel ("still valid")                     #
# api_type = broad category used for retrieval metadata                      #
# --------------------------------------------------------------------------- #
REPO_VERSION_META: dict[str, dict] = {
    # Core utilities
    "Essentials":            {"min_version": 1.13, "max_version": None, "api_type": "utility"},
    "Vault":                 {"min_version": 1.13, "max_version": None, "api_type": "economy"},
    "PlaceholderAPI":        {"min_version": 1.13, "max_version": None, "api_type": "placeholder"},
    "LuckPerms":             {"min_version": 1.16, "max_version": None, "api_type": "permissions"},
    # Economy / shops
    "ChestShop3":            {"min_version": 1.13, "max_version": None, "api_type": "economy"},
    # World management
    "WorldGuard":            {"min_version": 1.16, "max_version": None, "api_type": "world"},
    "WorldEdit":             {"min_version": 1.16, "max_version": None, "api_type": "world"},
    "Multiverse-Core":       {"min_version": 1.16, "max_version": None, "api_type": "world"},
    # Chat / messaging
    "TrainCarts":            {"min_version": 1.17, "max_version": None, "api_type": "utility"},
    # Protection
    "GriefPrevention":       {"min_version": 1.16, "max_version": None, "api_type": "protection"},
    # GUI
    "IF":                    {"min_version": 1.16, "max_version": None, "api_type": "gui"},
    # Tab / scoreboard
    "TAB":                   {"min_version": 1.16, "max_version": None, "api_type": "display"},
    # Packet / protocol
    "ProtocolLib":           {"min_version": 1.16, "max_version": None, "api_type": "packet"},
    # Misc modern
    "ViaVersion":            {"min_version": 1.16, "max_version": None, "api_type": "utility"},
    "dynmap":                {"min_version": 1.13, "max_version": None, "api_type": "utility"},
    "DecentHolograms":       {"min_version": 1.18, "max_version": None, "api_type": "hologram"},
    "AxVaults":              {"min_version": 1.18, "max_version": None, "api_type": "gui"},
    "DiscordSRV":            {"min_version": 1.16, "max_version": None, "api_type": "integration"},
    "helper":                {"min_version": 1.16, "max_version": None, "api_type": "utility"},
    # New repos
    "Citizens2":             {"min_version": 1.16, "max_version": None, "api_type": "npc"},
    # HolographicDisplays uses an API removed after 1.19; filter at retrieval time
    "HolographicDisplays":   {"min_version": 1.13, "max_version": 1.19, "api_type": "hologram"},
    "SkinsRestorer":         {"min_version": 1.16, "max_version": None, "api_type": "skin"},
    "CommandAPI":            {"min_version": 1.19, "max_version": None, "api_type": "command"},
    # Handcrafted reference plugins (paper 1.21-specific patterns)
    "_ref_folia_scheduler":  {"min_version": 1.20, "max_version": None, "api_type": "scheduler"},
    "_ref_adventure_api":    {"min_version": 1.18, "max_version": None, "api_type": "messaging"},
    "_ref_pdc_patterns":     {"min_version": 1.14, "max_version": None, "api_type": "data"},
}

_VERSION_DEFAULT = {"min_version": 1.16, "max_version": None, "api_type": "utility"}


def get_version_meta(repo: str) -> dict:
    """Return version/type metadata for a repo, with safe defaults."""
    m = REPO_VERSION_META.get(repo, _VERSION_DEFAULT)
    return {
        "min_version": m["min_version"],
        "max_version": m["max_version"] if m["max_version"] is not None else 9999.0,
        "api_type": m["api_type"],
    }


def infer_api_type(java_code: str) -> str | None:
    """
    Lightweight keyword scan to infer api_type from actual code content.
    Returns None if no strong signal — caller keeps the repo-level default.

    Types are intentionally matched to the keys used by classify_intent()
    in inference/router.py so the retrieval boost can align them directly:
      command, event_handler, scheduler, gui, economy, npc,
      hologram, skin, data (PDC), world, messaging, packet, utility
    """
    code = java_code.lower()

    # Folia / RegionScheduler — highest specificity, check first
    if any(k in code for k in (
        "regionscheduler", "asyncscheduler", "globalregionscheduler"
    )):
        return "scheduler"

    # PDC / PersistentDataContainer
    if any(k in code for k in (
        "persistentdatacontainer", "persistentdatatype", "namespacedkey"
    )):
        return "data"

    # Economy (Vault hook)
    if any(k in code for k in (
        "economy", "vault", "net.milkbowl", "economyprovider",
        "registereconomy", "depositplayer", "withdrawplayer",
        "getbalance", "hasmoney",
    )):
        return "economy"

    # NPC (Citizens)
    if any(k in code for k in (
        "npcregistry", "trait", "npc.create", "citizensapi",
        "npctype", "abstracttrait",
    )):
        return "npc"

    # Hologram
    if any(k in code for k in (
        "holographicdisplays", "holoapi", "decentholograms",
        "createhologram", "hologramline", "holographiclines",
    )):
        return "hologram"

    # Skin management
    if any(k in code for k in (
        "skinsrestorer", "skinmanager", "skingetter",
        "setskin", "getplayerskin",
    )):
        return "skin"

    # CommandAPI (brigadier-backed)
    if any(k in code for k in (
        "commandapi", "commandapicommand", "new commandapicommand",
        "brigadierutil", "commandpermission",
    )):
        return "command"

    # GUI / inventory menus
    if any(k in code for k in (
        "inventoryholder", "inventoryclickevent", "createinventory",
        "chest_gui", "openmenu", "clickevent",
    )):
        return "gui"

    # World management
    if any(k in code for k in (
        "worldmanager", "multiversecore", "mvworld",
        "regioncontainer", "protectedregion",
    )):
        return "world"

    # Adventure API / messaging
    if any(k in code for k in (
        "component.text", "minimessage", "net.kyori.adventure",
        "textcomponent", "showTitle", "sendactionbar",
    )):
        return "messaging"

    # Packet manipulation
    if any(k in code for k in (
        "protocollib", "packetlistener", "packettype",
        "packetcontainer", "protocolmanager",
    )):
        return "packet"

    # Generic command executor
    if any(k in code for k in (
        "commandexecutor", "tabcompleter", "oncommand",
    )):
        return "command"

    # Generic event listener
    if any(k in code for k in (
        "eventhandler", "implements listener", "@eventhandler",
    )):
        return "event_handler"

    return None


def extract_class_name(java_code: str) -> str | None:
    m = re.search(r"public\s+(?:class|interface|enum|record)\s+(\w+)", java_code)
    return m.group(1) if m else None


def is_main_plugin_class(java_code: str) -> bool:
    """True if the file extends JavaPlugin / extends Plugin."""
    return bool(re.search(r"extends\s+(JavaPlugin|Plugin)\b", java_code))


def extract_methods(java_code: str) -> list[dict]:
    """
    Very lightweight method splitter — does NOT use a full Java parser.
    Splits on public/protected/private method declarations.
    Returns list of { name, code } dicts.
    """
    lines = java_code.splitlines()
    methods: list[dict] = []
    method_start: int | None = None
    method_name: str = ""
    brace_depth = 0
    in_method = False

    method_sig = re.compile(
        r"^\s*(?:public|protected|private|static|final|synchronized|@Override)"
        r"[\w\s<>\[\],@]*\s+(\w+)\s*\("
    )

    for i, line in enumerate(lines):
        if not in_method:
            m = method_sig.match(line)
            if m and "{" in line:
                in_method = True
                method_start = i
                method_name = m.group(1)
                brace_depth = line.count("{") - line.count("}")
        else:
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                snippet = "\n".join(lines[method_start : i + 1])
                methods.append({"name": method_name, "code": snippet, "start": method_start})
                in_method = False
                brace_depth = 0

    return methods


def get_class_header(java_code: str) -> str:
    """Return package declaration + imports + class signature (up to first {)."""
    lines = java_code.splitlines()
    header_lines = []
    for line in lines:
        header_lines.append(line)
        if re.match(r"\s*(public|abstract|final)\s+class\s+", line):
            header_lines.append("    // ... class body ...")
            break
    return "\n".join(header_lines)


# --------------------------------------------------------------------------- #
# Chunking levels                                                              #
# --------------------------------------------------------------------------- #

def chunk_level1_full_plugin(data: dict) -> list[dict]:
    """For small plugins: emit one chunk with main class + plugin.yml."""
    main_classes = [s for s in data["sources"] if is_main_plugin_class(s["content"])]
    if not main_classes:
        return []
    main = main_classes[0]
    vmeta = get_version_meta(data["repo"])
    api_type = infer_api_type(main["content"]) or vmeta["api_type"]
    return [
        {
            "repo": data["repo"],
            "chunk_type": "full_plugin",
            "java_code": main["content"],
            "plugin_yml": data.get("plugin_yml", ""),
            "pom_xml": data.get("pom_xml", ""),
            "source_path": main["path"],
            "min_version": vmeta["min_version"],
            "max_version": vmeta["max_version"],
            "api_type": api_type,
        }
    ]


def chunk_level2_class(data: dict) -> list[dict]:
    """Emit one chunk per Java class file (50–300 lines)."""
    chunks = []
    vmeta = get_version_meta(data["repo"])
    for src in data["sources"]:
        lines = src["lines"]
        if 50 <= lines <= 300:
            api_type = infer_api_type(src["content"]) or vmeta["api_type"]
            chunks.append(
                {
                    "repo": data["repo"],
                    "chunk_type": "class_level",
                    "java_code": src["content"],
                    "plugin_yml": data.get("plugin_yml", "") if is_main_plugin_class(src["content"]) else "",
                    "pom_xml": "",
                    "source_path": src["path"],
                    "min_version": vmeta["min_version"],
                    "max_version": vmeta["max_version"],
                    "api_type": api_type,
                }
            )
    return chunks


def chunk_level3_methods(data: dict) -> list[dict]:
    """For large files: emit chunks of 2–4 methods + class header."""
    chunks = []
    vmeta = get_version_meta(data["repo"])
    for src in data["sources"]:
        if src["lines"] <= 300:
            continue
        methods = extract_methods(src["content"])
        if not methods:
            continue
        header = get_class_header(src["content"])
        # Group methods in windows of 3
        for i in range(0, len(methods), 3):
            group = methods[i : i + 3]
            combined = header + "\n\n" + "\n\n".join(m["code"] for m in group) + "\n}"
            api_type = infer_api_type(combined) or vmeta["api_type"]
            chunks.append(
                {
                    "repo": data["repo"],
                    "chunk_type": "method_group",
                    "java_code": combined,
                    "plugin_yml": "",
                    "pom_xml": "",
                    "source_path": src["path"],
                    "methods": [m["name"] for m in group],
                    "min_version": vmeta["min_version"],
                    "max_version": vmeta["max_version"],
                    "api_type": api_type,
                }
            )
    return chunks


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def process_repo(data: dict) -> list[dict]:
    total_lines = sum(s["lines"] for s in data["sources"])

    if total_lines < 200:
        return chunk_level1_full_plugin(data)

    chunks = []
    # Always try to include the main plugin class as a level-1 chunk
    chunks.extend(chunk_level1_full_plugin(data))
    # Add class-level chunks for supporting classes
    chunks.extend(chunk_level2_class(data))
    # Add method-group chunks for large files
    chunks.extend(chunk_level3_methods(data))

    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="Chunk ingested plugin data into training units")
    parser.add_argument("--input", default="data/raw", help="Directory of repo JSON files")
    parser.add_argument("--output", default="data/processed", help="Output directory")
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=500,
        help="Hard cap on total chunks (prevents dataset explosion)",
    )
    parser.add_argument(
        "--max-per-repo",
        type=int,
        default=30,
        help="Max chunks per repo — keeps the dataset diverse across repos",
    )
    args = parser.parse_args()

    in_dir = pathlib.Path(args.input)
    out_dir = pathlib.Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    repo_files = sorted(in_dir.glob("*.json"))
    # Exclude metadata.json
    repo_files = [f for f in repo_files if f.stem != "metadata"]

    if not repo_files:
        console.print(f"[red]No repo JSON files found in {in_dir}[/red]")
        console.print(f"Run: [dim]python scripts/ingest.py --output {in_dir}[/dim]")
        return

    console.rule("[bold blue]StackNest — Chunking")
    console.print(f"Processing {len(repo_files)} repos...\n")

    all_chunks: list[dict] = []
    for repo_file in track(repo_files, description="Chunking repos"):
        with open(repo_file) as f:
            data = json.load(f)
        chunks = process_repo(data)

        # Per-repo cap — prevents one large repo (e.g. Essentials) from
        # filling the entire dataset and starving smaller repos.
        if len(chunks) > args.max_per_repo:
            # Keep a balanced sample: prefer full_plugin > class_level > method_group
            by_type: dict[str, list] = {}
            for c in chunks:
                by_type.setdefault(c["chunk_type"], []).append(c)
            selected: list[dict] = []
            priority = ["full_plugin", "class_level", "method_group"]
            per_type = max(1, args.max_per_repo // len(priority))
            for ct in priority:
                selected.extend(by_type.get(ct, [])[:per_type])
            # Top up with whatever type has the most remaining
            remaining = args.max_per_repo - len(selected)
            for ct in priority:
                leftover = [c for c in by_type.get(ct, []) if c not in selected]
                selected.extend(leftover[:remaining])
                remaining = args.max_per_repo - len(selected)
                if remaining <= 0:
                    break
            chunks = selected[:args.max_per_repo]

        all_chunks.extend(chunks)

        if len(all_chunks) >= args.max_chunks:
            console.print(
                f"[yellow]Hit max-chunks cap ({args.max_chunks}). Stopping early.[/yellow]"
            )
            all_chunks = all_chunks[: args.max_chunks]
            break

    out_path = out_dir / "chunks.jsonl"
    with open(out_path, "w") as f:
        for chunk in all_chunks:
            f.write(json.dumps(chunk) + "\n")

    # Summary
    chunk_types: dict[str, int] = {}
    for c in all_chunks:
        ct = c["chunk_type"]
        chunk_types[ct] = chunk_types.get(ct, 0) + 1

    console.print(f"\n[bold green]Done.[/bold green] {len(all_chunks)} chunks → [bold]{out_path}[/bold]")
    for ct, count in chunk_types.items():
        console.print(f"  {ct}: {count}")

    next_cmd = f"python scripts/generate_instructions.py --input {out_path}"
    console.print(f"Next: [dim]{next_cmd}[/dim]")


if __name__ == "__main__":
    main()
