"""
train/merge_lora.py — Merge LoRA adapter into GGUF for llama.cpp deployment.

This script is run AFTER training on Colab. It:
  1. Loads the base HuggingFace model
  2. Merges the LoRA adapter weights into it
  3. Saves as a merged HuggingFace model
  4. Calls llama.cpp convert script to produce a GGUF

Run this on a machine with enough RAM to hold the full FP16 model (~7 GB for 3B).
Can run on Pi 5 if you have patience — takes ~30 min.

Usage:
    python train/merge_lora.py \
        --adapter train/lora_adapter \
        --output-dir train/merged_model \
        --gguf-output models/minecraft-coder-q4km.gguf \
        --llamacpp-dir /path/to/llama.cpp

    # Or skip GGUF conversion if you'll use HuggingFace-format inference:
    python train/merge_lora.py --adapter train/lora_adapter --output-dir train/merged_model
"""

import argparse
import pathlib
import subprocess
import sys

from rich.console import Console

console = Console()


def merge(adapter_path: str, output_dir: str) -> None:
    console.print(f"Loading base model + LoRA adapter from [bold]{adapter_path}[/bold]")

    try:
        import torch
        from peft import AutoPeftModelForCausalLM
        from transformers import AutoTokenizer
    except ImportError:
        console.print("[red]Missing deps: pip install peft transformers torch[/red]")
        sys.exit(1)

    model = AutoPeftModelForCausalLM.from_pretrained(
        adapter_path,
        torch_dtype=torch.float16,
        device_map="cpu",   # CPU merge — safe on Pi, just slow
        trust_remote_code=True,
    )
    console.print("Merging LoRA weights into base model...")
    merged = model.merge_and_unload()

    tokenizer = AutoTokenizer.from_pretrained(adapter_path, trust_remote_code=True)

    out = pathlib.Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    merged.save_pretrained(str(out))
    tokenizer.save_pretrained(str(out))
    console.print(f"[green]Merged model saved to {out}[/green]")


def convert_to_gguf(merged_dir: str, gguf_output: str, llamacpp_dir: str) -> None:
    convert_script = pathlib.Path(llamacpp_dir) / "convert_hf_to_gguf.py"
    if not convert_script.exists():
        console.print(f"[red]llama.cpp convert script not found: {convert_script}[/red]")
        console.print("Clone llama.cpp and build it first.")
        return

    gguf_path = pathlib.Path(gguf_output)
    gguf_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(f"Converting to GGUF (Q4_K_M)...")
    result = subprocess.run(
        [
            sys.executable,
            str(convert_script),
            merged_dir,
            "--outfile", str(gguf_path.with_suffix(".f16.gguf")),
            "--outtype", "f16",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]Conversion failed:\n{result.stderr}[/red]")
        return

    console.print("Quantizing to Q4_K_M...")
    quantize_bin = pathlib.Path(llamacpp_dir) / "build" / "bin" / "llama-quantize"
    if not quantize_bin.exists():
        # Fallback: some builds put it directly in the root
        quantize_bin = pathlib.Path(llamacpp_dir) / "llama-quantize"
    result2 = subprocess.run(
        [
            str(quantize_bin),
            str(gguf_path.with_suffix(".f16.gguf")),
            str(gguf_path),
            "Q4_K_M",
        ],
        capture_output=True,
        text=True,
    )
    if result2.returncode != 0:
        console.print(f"[red]Quantization failed:\n{result2.stderr}[/red]")
        return

    size_mb = gguf_path.stat().st_size / (1024 * 1024)
    console.print(f"[bold green]GGUF ready:[/bold green] {gguf_path} ({size_mb:.0f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge LoRA adapter into GGUF")
    parser.add_argument("--adapter", default="train/lora_adapter")
    parser.add_argument("--output-dir", default="train/merged_model")
    parser.add_argument("--gguf-output", default="models/minecraft-coder-q4km.gguf")
    parser.add_argument(
        "--llamacpp-dir",
        default=None,
        help="Path to compiled llama.cpp directory (skip if not converting to GGUF)",
    )
    args = parser.parse_args()

    console.rule("[bold blue]StackNest — LoRA Merge")
    merge(args.adapter, args.output_dir)

    if args.llamacpp_dir:
        convert_to_gguf(args.output_dir, args.gguf_output, args.llamacpp_dir)
        console.print(
            f"\nNext: start the inference server:\n"
            f"  ./llama-server --model {args.gguf_output} "
            f"--ctx-size 8192 --port 8080 --mlock --threads 4"
        )
    else:
        console.print(
            "\nSkipping GGUF conversion (no --llamacpp-dir provided).\n"
            "To convert later, re-run with: --llamacpp-dir /path/to/llama.cpp"
        )


if __name__ == "__main__":
    main()
