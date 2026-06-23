"""
train/train.py — Fine-tune Qwen2.5-Coder-3B-Instruct with LoRA on Minecraft plugin data.

Run on Google Colab (T4 GPU) or any CUDA machine >= 16GB VRAM.
Do NOT run on the Raspberry Pi 5.

Usage:
    # On Colab:
    !pip install transformers peft trl bitsandbytes accelerate datasets
    !python train/train.py

    # With custom paths:
    python train/train.py \
        --train-file data/processed/train.jsonl \
        --val-file data/processed/val.jsonl \
        --output-dir train/output
"""

import argparse
import json
import pathlib
import sys

from rich.console import Console

console = Console()


# --------------------------------------------------------------------------- #
# Dataset formatting                                                           #
# --------------------------------------------------------------------------- #

def format_prompt(entry: dict) -> str:
    """
    Format a training entry into the Qwen2.5-Instruct chat template.
    <|im_start|>system\n...<|im_end|>\n<|im_start|>user\n...<|im_end|>\n<|im_start|>assistant\n...
    """
    system = entry.get("system", "")
    instruction = entry.get("instruction", "")
    response = entry.get("response", "")

    return (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{instruction}<|im_end|>\n"
        f"<|im_start|>assistant\n{response}<|im_end|>"
    )


def load_jsonl(path: str) -> list[str]:
    """Load JSONL and return list of formatted prompt strings."""
    prompts = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            prompts.append(format_prompt(entry))
    return prompts


# --------------------------------------------------------------------------- #
# Main training routine                                                        #
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Fine-tune Qwen2.5-Coder-3B with LoRA")
    parser.add_argument("--train-file", default="data/processed/train.jsonl")
    parser.add_argument("--val-file", default="data/processed/val.jsonl")
    parser.add_argument("--output-dir", default="train/output")
    args = parser.parse_args()

    # Guard: check for CUDA before spending time on imports
    try:
        import torch
        if not torch.cuda.is_available():
            console.print(
                "[bold yellow]WARNING:[/bold yellow] CUDA not available. "
                "Training on CPU will take 6–10 hours. "
                "Run on Google Colab (T4) for ~20 minutes instead."
            )
            if input("Continue anyway? [y/N] ").lower() != "y":
                sys.exit(0)
    except ImportError:
        console.print("[red]PyTorch not installed. Run: pip install torch[/red]")
        sys.exit(1)

    from datasets import Dataset
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTTrainer, SFTConfig

    # Import our config — works both from project root and when run directly in Colab
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))  # project root
    sys.path.insert(0, str(pathlib.Path(__file__).parent))         # same dir (Colab)
    try:
        from train.lora_config import ModelConfig, TrainingConfig, get_bnb_config, get_peft_config
    except ModuleNotFoundError:
        from lora_config import ModelConfig, TrainingConfig, get_bnb_config, get_peft_config

    model_cfg = ModelConfig()
    train_cfg = TrainingConfig(
        train_file=args.train_file,
        val_file=args.val_file,
        output_dir=args.output_dir,
    )

    # ---------------------------------------------------------------------- #
    # Load dataset                                                             #
    # ---------------------------------------------------------------------- #
    console.rule("[bold blue]StackNest — LoRA Training")

    train_texts = load_jsonl(train_cfg.train_file)
    val_texts = load_jsonl(train_cfg.val_file)

    console.print(f"Train: {len(train_texts)} examples")
    console.print(f"Val:   {len(val_texts)} examples\n")

    train_dataset = Dataset.from_dict({"text": train_texts})
    val_dataset = Dataset.from_dict({"text": val_texts})

    # ---------------------------------------------------------------------- #
    # Load tokenizer                                                           #
    # ---------------------------------------------------------------------- #
    console.print(f"Loading tokenizer: [bold]{model_cfg.model_name}[/bold]")
    tokenizer = AutoTokenizer.from_pretrained(
        model_cfg.model_name,
        trust_remote_code=True,
        padding_side="right",
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # ---------------------------------------------------------------------- #
    # Load model with 4-bit quantization                                      #
    # ---------------------------------------------------------------------- #
    console.print(f"Loading model in 4-bit NF4...")
    bnb_config = get_bnb_config()

    model = AutoModelForCausalLM.from_pretrained(
        model_cfg.model_name,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
        attn_implementation="eager",  # Flash attention not guaranteed on all T4 configs
    )
    model.config.use_cache = False  # Required for gradient checkpointing
    model.enable_input_require_grads()

    # ---------------------------------------------------------------------- #
    # LoRA configuration                                                       #
    # ---------------------------------------------------------------------- #
    peft_config = get_peft_config()
    console.print(f"LoRA: r={peft_config.r}, alpha={peft_config.lora_alpha}, "
                  f"modules={peft_config.target_modules}")

    # ---------------------------------------------------------------------- #
    # SFTConfig (combines TrainingArguments + SFT-specific args in modern TRL) #
    # ---------------------------------------------------------------------- #
    training_args = SFTConfig(
        output_dir=train_cfg.output_dir,
        num_train_epochs=train_cfg.num_train_epochs,
        per_device_train_batch_size=train_cfg.per_device_train_batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=train_cfg.gradient_accumulation_steps,
        learning_rate=train_cfg.learning_rate,
        weight_decay=train_cfg.weight_decay,
        warmup_steps=10,
        lr_scheduler_type=train_cfg.lr_scheduler_type,
        bf16=train_cfg.bf16,
        fp16=train_cfg.fp16,
        eval_strategy=train_cfg.evaluation_strategy,
        eval_steps=train_cfg.eval_steps,
        save_strategy=train_cfg.save_strategy,
        load_best_model_at_end=train_cfg.load_best_model_at_end,
        metric_for_best_model=train_cfg.metric_for_best_model,
        greater_is_better=train_cfg.greater_is_better,
        logging_steps=train_cfg.logging_steps,
        report_to=train_cfg.report_to,
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
        dataloader_num_workers=0,
        remove_unused_columns=False,
        # SFT-specific
        dataset_text_field="text",
        max_length=train_cfg.max_seq_length,
        packing=False,
    )

    # ---------------------------------------------------------------------- #
    # SFTTrainer                                                               #
    # ---------------------------------------------------------------------- #
    trainer = SFTTrainer(
        model=model,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        peft_config=peft_config,
        processing_class=tokenizer,
        args=training_args,
    )

    console.print(
        f"\n[bold green]Starting training...[/bold green] "
        f"{train_cfg.num_train_epochs} epochs, "
        f"effective batch size {train_cfg.per_device_train_batch_size * train_cfg.gradient_accumulation_steps}"
    )

    trainer.train()

    # ---------------------------------------------------------------------- #
    # Save the LoRA adapter                                                    #
    # ---------------------------------------------------------------------- #
    adapter_path = pathlib.Path(train_cfg.output_dir) / "lora_adapter"
    trainer.model.save_pretrained(str(adapter_path))
    tokenizer.save_pretrained(str(adapter_path))

    console.print(f"\n[bold green]Training complete.[/bold green]")
    console.print(f"LoRA adapter saved to: [bold]{adapter_path}[/bold]")
    console.print(
        "\nNext steps:\n"
        "  1. Download the 'lora_adapter/' folder from Colab\n"
        "  2. On your Pi 5, merge into GGUF:\n"
        "     python train/merge_lora.py --base models/base.gguf "
        "--adapter train/lora_adapter --output models/minecraft-coder-q4km.gguf"
    )


if __name__ == "__main__":
    main()
