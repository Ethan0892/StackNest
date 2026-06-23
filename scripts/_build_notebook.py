"""One-shot script: rewrites train/StackNest_Retrain.ipynb as standard Jupyter JSON."""
import json, pathlib

OUT = pathlib.Path(__file__).parent.parent / "train" / "StackNest_Retrain.ipynb"

def md(source_lines, idx):
    return {"cell_type": "markdown", "id": f"cell-{idx:02d}", "metadata": {},
            "source": source_lines}

def code(source_lines, idx):
    return {"cell_type": "code", "id": f"cell-{idx:02d}", "metadata": {},
            "execution_count": None, "outputs": [], "source": source_lines}

cells = []
i = 0

# ── 0: title ──────────────────────────────────────────────────────────────────
cells.append(md([
    "# StackNest — Full Retrain Pipeline\n",
    "### From broken scraped data → high-quality Paper 1.21 fine-tune\n",
    "\n",
    "**What this notebook does:**\n",
    "1. Installs every dependency (Unsloth QLoRA, TRL, llama.cpp GGUF exporter)\n",
    "2. Generates 500 synthetic Paper 1.21 training examples via Kimi K2.5\n",
    "3. Fine-tunes `Qwen2.5-Coder-3B-Instruct` with improved LoRA (r=32, MLP modules, 5 epochs)\n",
    "4. Quantises the adapter-merged model to GGUF Q4_K_M\n",
    "5. Downloads the `.gguf` to your machine (or saves to Google Drive)\n",
    "\n",
    "**Runtime needed:** GPU — T4 (free tier works).  \n",
    "Enable: `Runtime → Change runtime type → T4 GPU`\n",
    "\n",
    "**Estimated time:** ~20 min for data generation + ~45 min for training",
], i)); i += 1

# ── 1: Phase 0 heading ────────────────────────────────────────────────────────
cells.append(md(["## Phase 0 — Check GPU & Install Dependencies"], i)); i += 1

# ── 2: GPU check ──────────────────────────────────────────────────────────────
cells.append(code([
    "import subprocess, sys\n",
    "\n",
    "result = subprocess.run(\n",
    "    [\"nvidia-smi\", \"--query-gpu=name,memory.total\", \"--format=csv,noheader\"],\n",
    "    capture_output=True, text=True\n",
    ")\n",
    "if result.returncode == 0:\n",
    "    print(\"GPU:\", result.stdout.strip())\n",
    "else:\n",
    "    raise RuntimeError(\"No GPU found! Enable: Runtime → Change runtime type → T4 GPU\")",
], i)); i += 1

# ── 3: pip install ────────────────────────────────────────────────────────────
cells.append(code([
    "# Unsloth = optimised QLoRA for Qwen2.5 on T4 (2-3x faster than vanilla HF)\n",
    "!pip install -q \"unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git\"\n",
    "!pip install -q --no-deps trl peft accelerate bitsandbytes\n",
    "!pip install -q openai   # Kimi API\n",
    "print(\"All packages installed.\")",
], i)); i += 1

# ── 4: Phase 1 heading ────────────────────────────────────────────────────────
cells.append(md([
    "## Phase 1 — Mount Drive & Copy Project Files\n",
    "\n",
    "Upload your **StackNest** folder to Google Drive (`My Drive/StackNest/`), then run the cell below.\n",
    "If you don't have Drive set up, use the commented-out Option B block to upload individual files.",
], i)); i += 1

# ── 5: mount + copy ───────────────────────────────────────────────────────────
cells.append(code([
    "from google.colab import drive\n",
    "drive.mount('/content/drive')\n",
    "\n",
    "import os, shutil, pathlib\n",
    "\n",
    "DRIVE_PROJECT = \"/content/drive/MyDrive/StackNest\"  # adjust if needed\n",
    "COLAB_PROJECT = \"/content/stacknest\"\n",
    "\n",
    "if os.path.isdir(DRIVE_PROJECT):\n",
    "    if os.path.isdir(COLAB_PROJECT):\n",
    "        shutil.rmtree(COLAB_PROJECT)\n",
    "    shutil.copytree(DRIVE_PROJECT, COLAB_PROJECT)\n",
    "    print(f\"Copied from Drive → {COLAB_PROJECT}\")\n",
    "else:\n",
    "    pathlib.Path(COLAB_PROJECT).mkdir(parents=True, exist_ok=True)\n",
    "    print(\"Drive path not found — created empty directory.\")\n",
    "    print(\"Fix DRIVE_PROJECT path or use Option B below.\")\n",
    "\n",
    "os.chdir(COLAB_PROJECT)\n",
    "print(\"Working directory:\", os.getcwd())",
], i)); i += 1

# ── 6: option B (commented) ───────────────────────────────────────────────────
cells.append(code([
    "# Option B — manual file upload (uncomment if Drive copy above failed)\n",
    "# from google.colab import files\n",
    "# uploaded = files.upload()   # pick: train/train.py, train/lora_config.py, scripts/generate_training_data.py\n",
    "# import os, pathlib\n",
    "# for name, data in uploaded.items():\n",
    "#     dest = pathlib.Path(f\"/content/stacknest/{name}\")\n",
    "#     dest.parent.mkdir(parents=True, exist_ok=True)\n",
    "#     dest.write_bytes(data)\n",
    "#     print(\"Uploaded:\", dest)\n",
    "\n",
    "# Verify required files exist\n",
    "required = [\n",
    "    \"train/train.py\",\n",
    "    \"train/lora_config.py\",\n",
    "    \"scripts/generate_training_data.py\",\n",
    "]\n",
    "missing = [f for f in required if not os.path.exists(f)]\n",
    "print(\"Missing:\", missing) if missing else print(\"All required files present ✓\")",
], i)); i += 1

# ── 7: Phase 2 heading ────────────────────────────────────────────────────────
cells.append(md([
    "## Phase 2 — Generate Synthetic Training Data\n",
    "\n",
    "Calls **Kimi K2.5** with 140+ diverse plugin prompts to generate 500 complete Paper 1.21 examples.\n",
    "Each example has: main class, `plugin.yml`, supporting classes, and a JUnit 5 test.\n",
    "\n",
    "**Set your Kimi API key** in the Colab Secrets panel (🔒 in the left sidebar):\n",
    "- Key name: `KIMI_API_KEY`\n",
    "- Or paste it directly in the cell below.",
], i)); i += 1

# ── 8: load kimi key ──────────────────────────────────────────────────────────
cells.append(code([
    "import os\n",
    "from google.colab import userdata\n",
    "\n",
    "try:\n",
    "    kimi_key = userdata.get(\"KIMI_API_KEY\")\n",
    "    os.environ[\"KIMI_API_KEY\"] = kimi_key\n",
    "    print(\"Kimi key loaded from Colab Secrets ✓\")\n",
    "except Exception:\n",
    "    kimi_key = \"\"  # ← paste your key here if secrets are not configured\n",
    "    if kimi_key:\n",
    "        os.environ[\"KIMI_API_KEY\"] = kimi_key\n",
    "        print(\"Kimi key loaded from cell ✓\")\n",
    "    else:\n",
    "        raise ValueError(\"KIMI_API_KEY not set. Add to Colab Secrets or paste above.\")",
], i)); i += 1

# ── 9: run generator ──────────────────────────────────────────────────────────
cells.append(code([
    "import pathlib\n",
    "\n",
    "# --count 500  = examples to generate (reduce for quick tests)\n",
    "# --merge      = filter old train.jsonl + combine with synthetic output\n",
    "!python scripts/generate_training_data.py \\\n",
    "    --count 500 \\\n",
    "    --output data/processed/synthetic_train.jsonl \\\n",
    "    --merge \\\n",
    "    --delay 1.2\n",
    "\n",
    "# Report sizes\n",
    "for f in [\"data/processed/train_v2.jsonl\", \"data/processed/val_v2.jsonl\"]:\n",
    "    p = pathlib.Path(f)\n",
    "    if p.exists():\n",
    "        print(f\"{f}: {sum(1 for _ in p.open())} examples\")",
], i)); i += 1

# ── 10: Phase 3 heading ───────────────────────────────────────────────────────
cells.append(md([
    "## Phase 3 — Diagnose Data Quality (Optional)\n",
    "Scan old and new JSONL files for corrupt examples (Minestom imports, missing java/yaml blocks).",
], i)); i += 1

# ── 11: diagnose ──────────────────────────────────────────────────────────────
cells.append(code([
    "import json, pathlib\n",
    "from collections import Counter\n",
    "\n",
    "def analyse(path):\n",
    "    p = pathlib.Path(path)\n",
    "    if not p.exists():\n",
    "        print(path, \"not found\"); return\n",
    "    entries = [json.loads(l) for l in p.open() if l.strip()]\n",
    "    bad = Counter()\n",
    "    no_java = no_yaml = 0\n",
    "    for e in entries:\n",
    "        r = e.get(\"response\", \"\")\n",
    "        if \"net.minestom\" in r: bad[\"minestom\"] += 1\n",
    "        if \"net.ess3\"     in r: bad[\"ess_internals\"] += 1\n",
    "        if \"org.dynmap\"   in r: bad[\"dynmap\"] += 1\n",
    "        if \"```java\" not in r:  no_java += 1\n",
    "        if \"```yaml\" not in r:  no_yaml += 1\n",
    "    print(f\"{path}: {len(entries)} examples | no_java={no_java} no_yaml={no_yaml} bad={dict(bad) or 'none'}\")\n",
    "\n",
    "analyse(\"data/processed/train.jsonl\")     # old (expect problems)\n",
    "analyse(\"data/processed/train_v2.jsonl\")  # new (expect clean)",
], i)); i += 1

# ── 12: Phase 4 heading ───────────────────────────────────────────────────────
cells.append(md([
    "## Phase 4 — Advanced Training Configuration\n",
    "\n",
    "| Setting | Old | **New** | Reason |\n",
    "|---|---|---|---|\n",
    "| `r` | 8 | **32** | More capacity for Paper API patterns |\n",
    "| `lora_alpha` | 16 | **64** | Keeps α/r = 2.0 |\n",
    "| `target_modules` | QKV+O | **+MLP** | MLP stores factual/syntactic knowledge |\n",
    "| `epochs` | 3 | **5** | Larger clean dataset can take more training |\n",
    "| `max_seq_length` | 1800 | **2048** | Full plugin examples need the headroom |\n",
    "| `learning_rate` | 2e-4 | **1e-4** | Lower LR stabilises higher-rank adapters |",
], i)); i += 1

# ── 13: show config ───────────────────────────────────────────────────────────
cells.append(code([
    "import sys\n",
    "sys.path.insert(0, \"/content/stacknest\")\n",
    "from train.lora_config import LoraConfig, TrainingConfig, ModelConfig\n",
    "\n",
    "lora  = LoraConfig()\n",
    "train = TrainingConfig()\n",
    "model = ModelConfig()\n",
    "\n",
    "print(\"=== Model ===\")\n",
    "print(f\"  base:          {model.model_name}\")\n",
    "print(f\"  4-bit:         {model.load_in_4bit}\")\n",
    "print(\"\\n=== LoRA ===\")\n",
    "print(f\"  r:             {lora.r}\")\n",
    "print(f\"  alpha:         {lora.lora_alpha}\")\n",
    "print(f\"  dropout:       {lora.lora_dropout}\")\n",
    "print(f\"  modules:       {lora.target_modules}\")\n",
    "print(\"\\n=== Training ===\")\n",
    "print(f\"  epochs:        {train.num_train_epochs}\")\n",
    "print(f\"  lr:            {train.learning_rate}\")\n",
    "print(f\"  max_seq:       {train.max_seq_length}\")\n",
    "print(f\"  train_file:    {train.train_file}\")\n",
    "print(f\"  val_file:      {train.val_file}\")",
], i)); i += 1

# ── 14: Phase 5 heading ───────────────────────────────────────────────────────
cells.append(md([
    "## Phase 5 — Fine-Tune with Improved Configuration\n",
    "\n",
    "Runs `train/train.py` with the upgraded LoRA config and synthetic dataset.  \n",
    "Expected time: **~40–50 minutes** on a T4 GPU (500 examples × 5 epochs).",
], i)); i += 1

# ── 15: run training ──────────────────────────────────────────────────────────
cells.append(code([
    "import os\n",
    "os.chdir(\"/content/stacknest\")\n",
    "\n",
    "!python train/train.py \\\n",
    "    --train_file data/processed/train_v2.jsonl \\\n",
    "    --val_file   data/processed/val_v2.jsonl \\\n",
    "    --output_dir train/output/\n",
    "\n",
    "import pathlib\n",
    "found = list(pathlib.Path(\"train/output\").rglob(\"adapter_config.json\"))\n",
    "print(\"\\n✓ Adapter:\", found[0].parent) if found else print(\"\\n✗ Adapter not found — check errors above\")",
], i)); i += 1

# ── 16: Phase 6 heading ───────────────────────────────────────────────────────
cells.append(md([
    "## Phase 6 — Evaluate & Benchmark\n",
    "Load the fine-tuned adapter and run 3 test prompts. Check that outputs use Paper API,\n",
    "not Minestom or internal plugin imports.",
], i)); i += 1

# ── 17: evaluate ──────────────────────────────────────────────────────────────
cells.append(code([
    "import torch, sys\n",
    "from transformers import AutoTokenizer, AutoModelForCausalLM\n",
    "from peft import PeftModel\n",
    "sys.path.insert(0, \"/content/stacknest\")\n",
    "from train.lora_config import get_bnb_config, ModelConfig\n",
    "\n",
    "mc  = ModelConfig()\n",
    "bnb = get_bnb_config()\n",
    "\n",
    "tokenizer = AutoTokenizer.from_pretrained(mc.model_name, trust_remote_code=True)\n",
    "base  = AutoModelForCausalLM.from_pretrained(mc.model_name, quantization_config=bnb,\n",
    "                                              device_map=\"auto\", trust_remote_code=True)\n",
    "model = PeftModel.from_pretrained(base, \"/content/stacknest/train/output/\")\n",
    "model.eval()\n",
    "print(\"Loaded ✓\")\n",
    "\n",
    "SYSTEM = (\n",
    "    \"You are StackNest, an expert Paper 1.21 Minecraft plugin developer.\\n\"\n",
    "    \"Output a ```java block (complete plugin) and a ```yaml block (plugin.yml).\\n\"\n",
    "    \"Never use ChatColor, NMS, Minestom, or third-party APIs.\"\n",
    ")\n",
    "\n",
    "def generate(prompt, max_new=800):\n",
    "    msgs = [{\"role\":\"system\",\"content\":SYSTEM},{\"role\":\"user\",\"content\":prompt}]\n",
    "    text = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)\n",
    "    inp  = tokenizer(text, return_tensors=\"pt\").to(model.device)\n",
    "    with torch.no_grad():\n",
    "        out = model.generate(**inp, max_new_tokens=max_new, temperature=0.2,\n",
    "                             do_sample=True, pad_token_id=tokenizer.eos_token_id)\n",
    "    return tokenizer.decode(out[0][inp.input_ids.shape[1]:], skip_special_tokens=True)\n",
    "\n",
    "prompts = [\n",
    "    \"Create a Paper 1.21 plugin with a /fly command that toggles flight. Use Adventure API.\",\n",
    "    \"Write a Paper plugin that broadcasts a random tip from config.yml every 5 minutes.\",\n",
    "    \"Make a Paper 1.21 plugin that opens a crafting GUI with /workbench.\",\n",
    "]\n",
    "\n",
    "for idx, p in enumerate(prompts, 1):\n",
    "    resp = generate(p)\n",
    "    has_j = \"```java\" in resp\n",
    "    has_y = \"```yaml\" in resp\n",
    "    paper = \"org.bukkit\" in resp or \"io.papermc\" in resp\n",
    "    bad   = \"net.minestom\" in resp or \"net.ess3\" in resp\n",
    "    print(f\"[{idx}] java={has_j} yaml={has_y} paper={paper} bad_framework={bad}\")\n",
    "    print(resp[:800])\n",
    "    print(\"-\" * 70)",
], i)); i += 1

# ── 18: Phase 7 heading ───────────────────────────────────────────────────────
cells.append(md([
    "## Phase 7 — Merge Adapter & Export to GGUF Q4_K_M\n",
    "\n",
    "Merges LoRA weights into the base model, then quantises to Q4_K_M —\n",
    "the same format used by `llama-server` on the Pi (≈2 GB).",
], i)); i += 1

# ── 19: merge + quantise ──────────────────────────────────────────────────────
cells.append(code([
    "import os, pathlib, torch, sys\n",
    "from transformers import AutoTokenizer, AutoModelForCausalLM\n",
    "from peft import PeftModel\n",
    "sys.path.insert(0, \"/content/stacknest\")\n",
    "from train.lora_config import ModelConfig\n",
    "\n",
    "mc          = ModelConfig()\n",
    "ADAPTER_DIR = \"/content/stacknest/train/output\"\n",
    "MERGED_DIR  = \"/content/merged_model\"\n",
    "GGUF_DIR    = \"/content/gguf_output\"\n",
    "GGUF_NAME   = \"minecraft-coder-q4km.gguf\"\n",
    "\n",
    "# Step 1 — merge (fp16, CPU to save VRAM)\n",
    "print(\"Loading base model for merge (fp16, CPU)...\")\n",
    "tok    = AutoTokenizer.from_pretrained(mc.model_name, trust_remote_code=True)\n",
    "base   = AutoModelForCausalLM.from_pretrained(mc.model_name, torch_dtype=torch.float16,\n",
    "                                               device_map=\"cpu\", trust_remote_code=True)\n",
    "merged = PeftModel.from_pretrained(base, ADAPTER_DIR).merge_and_unload()\n",
    "print(\"Merged ✓  saving to\", MERGED_DIR)\n",
    "merged.save_pretrained(MERGED_DIR)\n",
    "tok.save_pretrained(MERGED_DIR)\n",
    "del merged, base\n",
    "torch.cuda.empty_cache()\n",
    "\n",
    "# Step 2 — clone llama.cpp + install deps\n",
    "!git clone --depth=1 https://github.com/ggerganov/llama.cpp /content/llama.cpp 2>&1 | tail -3\n",
    "!pip install -q gguf sentencepiece\n",
    "\n",
    "# Step 3 — convert HF → F16 GGUF\n",
    "pathlib.Path(GGUF_DIR).mkdir(exist_ok=True)\n",
    "!python /content/llama.cpp/convert_hf_to_gguf.py \\\n",
    "    {MERGED_DIR} --outfile {GGUF_DIR}/model-f16.gguf --outtype f16\n",
    "print(\"F16 GGUF created ✓\")\n",
    "\n",
    "# Step 4 — build llama-quantize and quantise to Q4_K_M\n",
    "!cmake /content/llama.cpp -B /content/llama_build -DLLAMA_BUILD_TESTS=OFF 2>&1 | tail -3\n",
    "!cmake --build /content/llama_build --config Release -j4 -- llama-quantize 2>&1 | tail -5\n",
    "!  /content/llama_build/bin/llama-quantize \\\n",
    "    {GGUF_DIR}/model-f16.gguf {GGUF_DIR}/{GGUF_NAME} Q4_K_M\n",
    "sz = pathlib.Path(f\"{GGUF_DIR}/{GGUF_NAME}\").stat().st_size / 1e9\n",
    "print(f\"\\n✓  {GGUF_NAME}  ({sz:.2f} GB)\")",
], i)); i += 1

# ── 20: Phase 8 heading ───────────────────────────────────────────────────────
cells.append(md([
    "## Phase 8 — Download & Deploy to Pi\n",
    "Save the GGUF to Drive and trigger a browser download, then copy it to the Pi.",
], i)); i += 1

# ── 21: download + deploy ─────────────────────────────────────────────────────
cells.append(code([
    "import shutil, pathlib\n",
    "from google.colab import files\n",
    "\n",
    "GGUF_DIR  = \"/content/gguf_output\"\n",
    "GGUF_NAME = \"minecraft-coder-q4km.gguf\"\n",
    "\n",
    "# Save to Drive (backup)\n",
    "drive_dest = pathlib.Path(f\"/content/drive/MyDrive/StackNest/models/{GGUF_NAME}\")\n",
    "drive_dest.parent.mkdir(parents=True, exist_ok=True)\n",
    "shutil.copy(f\"{GGUF_DIR}/{GGUF_NAME}\", drive_dest)\n",
    "print(f\"Saved to Drive: {drive_dest}\")\n",
    "\n",
    "# Download to local machine\n",
    "files.download(f\"{GGUF_DIR}/{GGUF_NAME}\")\n",
    "\n",
    "print(\"\"\"\n",
    "──────────────────────────────────────────────────────\n",
    "Deploy to Pi (run these from your LOCAL machine):\n",
    "\n",
    "  scp minecraft-coder-q4km.gguf \\\\\n",
    "      ethan@stacknest:/home/ethan/stacknest/models/minecraft-coder-q4km.gguf\n",
    "\n",
    "  ssh ethan@stacknest \"sudo systemctl restart llama-server\"\n",
    "  ssh ethan@stacknest \"journalctl -u llama-server -f\"\n",
    "──────────────────────────────────────────────────────\n",
    "\"\"\")",
], i)); i += 1

# ── Write notebook JSON ────────────────────────────────────────────────────────
nb = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.10.0"},
        "colab": {"provenance": []}
    },
    "cells": cells,
}

with OUT.open("w", encoding="utf-8") as f:
    json.dump(nb, f, indent=1, ensure_ascii=False)

print(f"Written {len(cells)} cells → {OUT}")
