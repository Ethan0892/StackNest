"""
scripts/embed.py — Build ChromaDB retrieval index from training JSONL.

The index is used at inference time to find the most similar training example(s)
to a user's instruction, which are then prepended as in-context examples (RAG).

Usage:
    python scripts/embed.py \
        --input data/processed/train.jsonl \
        --output data/embeddings/chromadb
"""

import argparse
import json
import pathlib

from rich.console import Console
from rich.progress import track

console = Console()

COLLECTION_NAME = "plugins"
EMBED_MODEL = "all-MiniLM-L6-v2"  # 22 MB — fast on Pi 5


def main() -> None:
    parser = argparse.ArgumentParser(description="Build ChromaDB index from training JSONL")
    parser.add_argument("--input", default="data/processed/train.jsonl")
    parser.add_argument("--output", default="data/embeddings/chromadb")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete and rebuild from scratch",
    )
    args = parser.parse_args()

    try:
        import chromadb
        from chromadb.utils import embedding_functions
    except ImportError:
        console.print("[red]chromadb not installed. Run: pip install chromadb sentence-transformers[/red]")
        return

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        console.print(f"[red]Input not found: {in_path}[/red]")
        return

    out_dir = pathlib.Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load training data
    entries: list[dict] = []
    with open(in_path) as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))

    console.rule("[bold blue]StackNest — Embedding Index Build")
    console.print(f"Embedding {len(entries)} training examples...\n")
    console.print(f"Embedding model: [cyan]{EMBED_MODEL}[/cyan] (downloads ~22 MB on first run)\n")

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBED_MODEL
    )
    client = chromadb.PersistentClient(path=str(out_dir))

    # Rebuild collection if requested
    if args.rebuild:
        try:
            client.delete_collection(COLLECTION_NAME)
            console.print(f"[yellow]Deleted existing collection '{COLLECTION_NAME}'[/yellow]")
        except Exception:
            pass

    try:
        collection = client.get_collection(COLLECTION_NAME, embedding_function=ef)
        console.print(
            f"[yellow]Collection '{COLLECTION_NAME}' already exists "
            f"({collection.count()} items). Use --rebuild to overwrite.[/yellow]"
        )
        return
    except Exception:
        collection = client.create_collection(COLLECTION_NAME, embedding_function=ef)

    # Batch upsert — ChromaDB handles batching internally
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for i, entry in enumerate(track(entries, description="Indexing")):
        # Index on instruction — that's what we query with at runtime
        doc = entry.get("instruction", "")
        if not doc.strip():
            continue

        ids.append(f"ex_{i:05d}")
        documents.append(doc)
        metadatas.append(
            {
                "response":    entry.get("response", "")[:2000],  # ChromaDB metadata limit
                "system":      entry.get("system", ""),
                "min_version": float(entry.get("min_version", 1.16)),
                "max_version": float(entry.get("max_version", 9999.0)),
                "api_type":    str(entry.get("api_type", "utility")),
            }
        )

    # Upsert in batches of 50
    BATCH = 50
    for start in range(0, len(ids), BATCH):
        collection.upsert(
            ids=ids[start : start + BATCH],
            documents=documents[start : start + BATCH],
            metadatas=metadatas[start : start + BATCH],
        )

    console.print(f"\n[bold green]Done.[/bold green] {collection.count()} entries in '{COLLECTION_NAME}' collection")
    console.print(f"Index stored at: [bold]{out_dir}[/bold]")
    console.print("Next: [dim]python train/train.py[/dim] (run on Colab/GPU)")


if __name__ == "__main__":
    main()
