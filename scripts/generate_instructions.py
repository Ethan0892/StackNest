"""
scripts/generate_instructions.py — Generate NL instructions for each code chunk.

Uses a local LLM (via llama.cpp server or Ollama) to produce a natural-language
instruction describing what each code chunk does. Then formats the label +
ground-truth code into the JSONL training format.

Usage:
    # Start Ollama first: ollama serve
    python scripts/generate_instructions.py \
        --input data/processed/chunks.jsonl \
        --output data/processed/train.jsonl \
        --ollama-model qwen2.5-coder:3b

    # Or use llama.cpp server:
    python scripts/generate_instructions.py \
        --input data/processed/chunks.jsonl \
        --output data/processed/train.jsonl \
        --backend llamacpp \
        --llamacpp-url http://localhost:8080
"""

import argparse
import json
import pathlib
import random
import time

import requests
from rich.console import Console
from rich.progress import track

console = Console()

# --------------------------------------------------------------------------- #
# System prompt — baked into every training example                           #
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are a senior Paper plugin developer. Generate correct, compilable "
    "Paper 1.21 plugin code using modern APIs. Always output: "
    "1) The main Java class with full package declaration and imports, "
    "2) plugin.yml with correct api-version '1.21', "
    "3) Any supporting classes needed, "
    "4) A JUnit 5 + MockBukkit test class (package matches plugin, class name "
    "ends in 'Test') placed under src/test/java/. "
    "The test class must import MockBukkit, have @BeforeEach setUp() that calls "
    "MockBukkit.mock() and MockBukkit.load(), and @AfterEach tearDown() that "
    "calls MockBukkit.unmock(). Add one @Test per command or event handler. "
    "Use Adventure API (net.kyori.adventure) for all player messaging. "
    "Never use deprecated ChatColor or sendMessage(String) methods. "
    "Never use NMS or CraftBukkit internals. "
    "Register all commands in onEnable with getCommand(name).setExecutor(this)."
)

# --------------------------------------------------------------------------- #
# Instruction generation prompts                                               #
# --------------------------------------------------------------------------- #
INSTRUCTION_GEN_PROMPT = """You are helping create training data for an AI that generates Minecraft Paper plugins.

Given the following plugin code and plugin.yml, write a single clear natural-language instruction
that a Minecraft server owner (not a developer) might type to request this plugin.

Rules:
- Be specific about the plugin's key features (commands, permissions, storage, events)
- Write from the server owner's perspective: "Create a plugin that..."
- Do NOT include implementation details (no "use BukkitScheduler", no "use HikariCP")
- Maximum 2 sentences
- Output ONLY the instruction, nothing else

plugin.yml:
{plugin_yml}

Main class (first 60 lines):
{java_snippet}

Instruction:"""


# --------------------------------------------------------------------------- #
# Instruction paraphrases — applied to boost diversity                        #
# --------------------------------------------------------------------------- #
PARAPHRASE_PREFIXES = [
    "Create a plugin that",
    "Write a Paper plugin which",
    "I need a Minecraft plugin that",
    "Build a server plugin to",
    "Make a plugin for my Paper server that",
]


def paraphrase_instruction(instruction: str) -> list[str]:
    """Generate 2 additional paraphrased versions of an instruction."""
    # Strip leading "Create a plugin that" style prefix
    import re
    stripped = re.sub(
        r"^(Create|Write|Build|Make|I need)\s+a\s+(plugin|Paper plugin|Minecraft plugin|server plugin)"
        r"\s+(that|which|to|for)\s+",
        "",
        instruction,
        flags=re.IGNORECASE,
    ).strip()

    paraphrases = []
    sample_prefixes = random.sample(PARAPHRASE_PREFIXES, min(2, len(PARAPHRASE_PREFIXES)))
    for prefix in sample_prefixes:
        if not stripped.endswith("."):
            stripped = stripped + "."
        p = f"{prefix} {stripped[0].lower() + stripped[1:]}"
        if p.strip() != instruction.strip():
            paraphrases.append(p)

    return paraphrases


# --------------------------------------------------------------------------- #
# LLM backend wrappers                                                        #
# --------------------------------------------------------------------------- #

def call_ollama(model: str, prompt: str, base_url: str = "http://localhost:11434") -> str:
    resp = requests.post(
        f"{base_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 150},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


def call_llamacpp(prompt: str, base_url: str = "http://localhost:8080") -> str:
    resp = requests.post(
        f"{base_url}/completion",
        json={
            "prompt": prompt,
            "max_tokens": 150,
            "temperature": 0.3,
            "stop": ["\n\n", "###"],
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["content"].strip()


# --------------------------------------------------------------------------- #
# Response formatting                                                          #
# --------------------------------------------------------------------------- #

def format_response(chunk: dict) -> str:
    """Format the ground-truth code response for a training entry."""
    parts = []

    if chunk.get("java_code"):
        path = chunk.get("source_path", "src/main/java/com/example/Plugin.java")
        # Normalise path to src/main/java/... format
        if "src/main/java/" not in path:
            path = "src/main/java/com/example/" + pathlib.Path(path).name
        parts.append(f"```java\n// {path}\n{chunk['java_code'].strip()}\n```")

    if chunk.get("plugin_yml"):
        parts.append(f"```yaml\n# plugin.yml\n{chunk['plugin_yml'].strip()}\n```")

    return "\n".join(parts)


def build_training_entry(instruction: str, chunk: dict) -> dict:
    entry = {
        "system": SYSTEM_PROMPT,
        "instruction": instruction,
        "response": format_response(chunk),
    }
    # Preserve version metadata so embed.py can filter stale chunks at index time
    if "min_version" in chunk:
        entry["min_version"] = chunk["min_version"]
        entry["max_version"] = chunk["max_version"]
        entry["api_type"]    = chunk.get("api_type", "utility")
    return entry


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate instructions for plugin chunks")
    parser.add_argument("--input", default="data/processed/chunks.jsonl")
    parser.add_argument("--output", default="data/processed/train.jsonl")
    parser.add_argument("--backend", choices=["ollama", "llamacpp"], default="ollama")
    parser.add_argument("--ollama-model", default="qwen2.5-coder:3b")
    parser.add_argument("--llamacpp-url", default="http://localhost:8080")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument(
        "--paraphrase",
        action="store_true",
        default=True,
        help="Generate 2 paraphrase variants per instruction (3× data volume)",
    )
    parser.add_argument(
        "--max-examples",
        type=int,
        default=600,
        help="Hard cap on total training entries (before train/val/test split)",
    )
    args = parser.parse_args()

    in_path = pathlib.Path(args.input)
    out_path = pathlib.Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not in_path.exists():
        console.print(f"[red]Input not found: {in_path}[/red]")
        console.print(f"Run: [dim]python scripts/chunk.py[/dim]")
        return

    chunks = []
    with open(in_path) as f:
        for line in f:
            if line.strip():
                chunks.append(json.loads(line))

    console.rule("[bold blue]StackNest — Instruction Generation")
    console.print(f"Processing {len(chunks)} chunks...\n")

    entries: list[dict] = []
    errors = 0

    for chunk in track(chunks, description="Generating instructions"):
        plugin_yml = chunk.get("plugin_yml", "(no plugin.yml)")
        java_snippet = "\n".join(chunk["java_code"].splitlines()[:60])

        prompt = INSTRUCTION_GEN_PROMPT.format(
            plugin_yml=plugin_yml[:800],
            java_snippet=java_snippet,
        )

        try:
            if args.backend == "ollama":
                instruction = call_ollama(args.ollama_model, prompt, args.ollama_url)
            else:
                instruction = call_llamacpp(prompt, args.llamacpp_url)
        except Exception as e:
            console.print(f"  [red]LLM error for {chunk['repo']}: {e}[/red]")
            errors += 1
            # Fall back to plugin.yml description field if available
            import re
            m = re.search(r"description:\s*(.+)", chunk.get("plugin_yml", ""))
            if m:
                instruction = f"Create a plugin that {m.group(1).strip().lower()}"
            else:
                instruction = f"Create a plugin similar to {chunk['repo']} with the shown functionality."

        # Primary entry
        entries.append(build_training_entry(instruction, chunk))

        # Paraphrase variants (same code, different instruction wording)
        if args.paraphrase:
            for para in paraphrase_instruction(instruction):
                if len(entries) >= args.max_examples:
                    break
                entries.append(build_training_entry(para, chunk))

        if len(entries) >= args.max_examples:
            console.print(f"[yellow]Reached max-examples cap ({args.max_examples})[/yellow]")
            break

        # Polite delay to avoid hammering local LLM
        time.sleep(0.1)

    # Shuffle before train/val/test split
    random.seed(42)
    random.shuffle(entries)

    n_test = max(5, int(len(entries) * 0.05))
    n_val = max(15, int(len(entries) * 0.15))
    test_entries = entries[:n_test]
    val_entries = entries[n_test : n_test + n_val]
    train_entries = entries[n_test + n_val :]

    out_dir = out_path.parent

    def write_jsonl(path: pathlib.Path, data: list[dict]) -> None:
        with open(path, "w") as f:
            for entry in data:
                f.write(json.dumps(entry) + "\n")

    write_jsonl(out_path, train_entries)  # train.jsonl
    write_jsonl(out_dir / "val.jsonl", val_entries)
    write_jsonl(out_dir / "test.jsonl", test_entries)

    console.print(f"\n[bold green]Done.[/bold green]")
    console.print(f"  Train:      {len(train_entries)} examples → [bold]{out_path}[/bold]")
    console.print(f"  Validation: {len(val_entries)} examples → [bold]{out_dir/'val.jsonl'}[/bold]")
    console.print(f"  Test:       {len(test_entries)} examples → [bold]{out_dir/'test.jsonl'}[/bold]")
    if errors:
        console.print(f"  [yellow]LLM errors (fell back to heuristic): {errors}[/yellow]")

    console.print(
        f"\nNext: [dim]python scripts/validate_jsonl.py --input {out_path}[/dim]"
    )


if __name__ == "__main__":
    main()
