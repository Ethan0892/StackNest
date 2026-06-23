"""
scripts/ingest.py — Clone open-source Paper plugin repos and extract source files.

Usage:
    python scripts/ingest.py --output data/raw --limit 30
    python scripts/ingest.py --repos-file scripts/repo_list.txt --output data/raw
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()

# --------------------------------------------------------------------------- #
# Default repo list — MIT/Apache open-source plugins with good API coverage   #
# --------------------------------------------------------------------------- #
DEFAULT_REPOS = [
    # Core utility plugins — large, broad API coverage
    "EssentialsX/Essentials",
    "MilkBowl/Vault",
    "PlaceholderAPI/PlaceholderAPI",
    "LuckPerms/LuckPerms",
    # Economy / shops
    "Acrobot/ChestShop3",
    # World management
    "EngineHub/WorldGuard",
    "EngineHub/WorldEdit",
    # Chat / messaging (Adventure API patterns)
    "bergerhealer/TrainCarts",
    # Teleport / homes
    "TechFortress/GriefPrevention",
    "Multiverse/Multiverse-Core",
    # GUI / inventory menus
    "InventoryFramework/IF",
    # Tab list / scoreboard
    "NEZNAMY/TAB",
    # Packet / protocol
    "dmulloy2/ProtocolLib",
    # Small single-purpose reference plugins
    "kennytv/ViaVersion",
    "webbukkit/dynmap",
    "DecentSoftware-EU/DecentHolograms",
    "Artillex-Studios/AxVaults",
    "DiscordSRV/DiscordSRV",
    "lucko/helper",
    # Heavily-requested plugin types / modern frameworks
    "CitizensDev/Citizens2",           # NPC creation patterns
    "filoghost/HolographicDisplays",   # legacy hologram API (max_version=1.19)
    "SkinsRestorer/SkinsRestorer",     # skin management patterns
    "CommandAPI/CommandAPI",           # modern brigadier command framework
]


def clone_repo(repo: str, out_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Shallow-clone a GitHub repo. Return the path, or None on failure."""
    name = repo.split("/")[1]
    dest = out_dir / name
    if dest.exists():
        console.print(f"  [dim]Skip (exists): {name}[/dim]")
        return dest
    result = subprocess.run(
        [
            "git", "clone",
            "--depth=1",
            "--single-branch",
            f"https://github.com/{repo}",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        console.print(f"  [red]FAIL[/red] {repo}: {result.stderr.strip()[:120]}")
        return None
    return dest


def extract_plugin(repo_path: pathlib.Path) -> dict:
    """Extract Java sources, plugin.yml, and pom.xml from a cloned repo."""
    java_files = sorted(repo_path.rglob("*.java"))
    yml_files = sorted(repo_path.rglob("plugin.yml"))
    pom_files = sorted(repo_path.rglob("pom.xml"))

    sources = []
    for jf in java_files:
        try:
            text = jf.read_text(errors="ignore")
        except OSError:
            continue
        rel = str(jf.relative_to(repo_path))
        lines = text.splitlines()
        sources.append({"path": rel, "content": text, "lines": len(lines)})

    plugin_yml = ""
    if yml_files:
        try:
            plugin_yml = yml_files[0].read_text(errors="ignore")
        except OSError:
            pass

    pom_xml = ""
    if pom_files:
        try:
            pom_xml = pom_files[0].read_text(errors="ignore")
        except OSError:
            pass

    return {
        "repo": repo_path.name,
        "plugin_yml": plugin_yml,
        "pom_xml": pom_xml,
        "sources": sources,
        "total_java_files": len(sources),
        "total_lines": sum(s["lines"] for s in sources),
    }


def filter_sources(data: dict) -> dict:
    """
    Apply quality filters:
      - Drop files > 800 lines
      - Drop files with NMS / CraftBukkit internals
      - Drop files with very short average identifier length (obfuscated)
    """
    import re

    NMS_PATTERN = re.compile(r"org\.bukkit\.craftbukkit|net\.minecraft\.server")
    OBFUSCATED_RE = re.compile(r"\b[a-z]\b")  # single-char identifiers

    clean_sources = []
    dropped = 0

    for src in data["sources"]:
        lines = src["lines"]
        content = src["content"]

        # Rule 1: file too large for context window
        if lines > 800:
            dropped += 1
            continue

        # Rule 2: NMS / internal craft imports
        if NMS_PATTERN.search(content):
            dropped += 1
            continue

        # Rule 3: obfuscation heuristic — >10% of tokens are single chars
        tokens = re.findall(r"\b[a-zA-Z_]\w*\b", content)
        if tokens:
            single_char = sum(1 for t in tokens if len(t) == 1)
            if single_char / len(tokens) > 0.10:
                dropped += 1
                continue

        clean_sources.append(src)

    filtered = dict(data)
    filtered["sources"] = clean_sources
    filtered["dropped_files"] = dropped
    return filtered


def save_metadata(all_data: list[dict], out_dir: pathlib.Path) -> None:
    meta_path = out_dir / "metadata.json"
    with open(meta_path, "w") as f:
        # Write summary only — not full source text (that lives in individual files)
        summary = [
            {
                "repo": d["repo"],
                "total_java_files": d["total_java_files"],
                "clean_files": len(d["sources"]),
                "dropped_files": d.get("dropped_files", 0),
                "total_lines": d["total_lines"],
                "has_plugin_yml": bool(d["plugin_yml"]),
                "has_pom": bool(d["pom_xml"]),
            }
            for d in all_data
        ]
        json.dump(summary, f, indent=2)

    # Also write full data per repo
    for d in all_data:
        repo_file = out_dir / f"{d['repo']}.json"
        with open(repo_file, "w") as f:
            json.dump(d, f, indent=2)


def print_summary(all_data: list[dict]) -> None:
    table = Table(title="Ingestion Summary", show_lines=True)
    table.add_column("Repo", style="cyan")
    table.add_column("Java Files", justify="right")
    table.add_column("Kept", justify="right")
    table.add_column("Dropped", justify="right")
    table.add_column("plugin.yml", justify="center")

    for d in all_data:
        kept = len(d["sources"])
        dropped = d.get("dropped_files", 0)
        table.add_row(
            d["repo"],
            str(d["total_java_files"]),
            f"[green]{kept}[/green]",
            f"[red]{dropped}[/red]" if dropped else "0",
            "✓" if d["plugin_yml"] else "[red]✗[/red]",
        )
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Minecraft plugin repos")
    parser.add_argument("--output", default="data/raw", help="Output directory")
    parser.add_argument(
        "--limit", type=int, default=len(DEFAULT_REPOS), help="Max repos to clone"
    )
    parser.add_argument(
        "--repos-file",
        default=None,
        help="Path to text file with owner/repo per line (overrides default list)",
    )
    args = parser.parse_args()

    out_dir = pathlib.Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.repos_file:
        repos = pathlib.Path(args.repos_file).read_text().splitlines()
        repos = [r.strip() for r in repos if r.strip() and not r.startswith("#")]
    else:
        repos = DEFAULT_REPOS[: args.limit]

    console.rule("[bold blue]StackNest — Plugin Ingestion")
    console.print(f"Cloning [bold]{len(repos)}[/bold] repos → [bold]{out_dir}[/bold]\n")

    all_data: list[dict] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task("Cloning...", total=len(repos))

        for repo in repos:
            progress.update(task, description=f"[cyan]{repo}[/cyan]")
            repo_path = clone_repo(repo, out_dir)
            if repo_path is None:
                progress.advance(task)
                continue

            data = extract_plugin(repo_path)
            data = filter_sources(data)
            all_data.append(data)
            progress.advance(task)

    save_metadata(all_data, out_dir)
    print_summary(all_data)

    total_kept = sum(len(d["sources"]) for d in all_data)
    console.print(
        f"\n[bold green]Done.[/bold green] "
        f"{len(all_data)} repos, {total_kept} Java files kept. "
        f"Metadata → [bold]{out_dir}/metadata.json[/bold]"
    )
    console.print(f"Next: [dim]python scripts/chunk.py --input {out_dir} --output data/processed[/dim]")


if __name__ == "__main__":
    main()
