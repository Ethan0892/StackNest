import json
from pathlib import Path

out = Path('train/StackNest_Retrain_Colab_Fixed.ipynb')


def md(*lines):
    return {
        'cell_type': 'markdown',
        'metadata': {},
        'source': [line + '\n' for line in lines],
    }


def code(*lines):
    return {
        'cell_type': 'code',
        'metadata': {},
        'execution_count': None,
        'outputs': [],
        'source': [line + '\n' for line in lines],
    }

cells = []

cells.append(md(
    '# StackNest Retrain (Colab, Fixed)',
    '',
    'This notebook is path-safe and includes:',
    '- flat `/content` and project `/content/stacknest` support',
    '- corrected training args (`--train-file`, `--val-file`, `--output-dir`)',
    '- adapter auto-discovery (`output/lora_adapter` or latest `checkpoint-*`)',
    '- save-to-Drive backup cells',
))

cells.append(code(
    'import subprocess',
    'res = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True)',
    'if res.returncode != 0:',
    '    raise RuntimeError("No GPU detected. Runtime -> Change runtime type -> T4 GPU")',
    'print("GPU:", res.stdout.strip())',
))

cells.append(code(
    '!pip install -q "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"',
    '!pip install -q trl peft accelerate bitsandbytes datasets openai',
    'print("Dependencies installed")',
))

cells.append(code(
    'from google.colab import drive',
    'import shutil, pathlib',
    "drive.mount('/content/drive')",
    "DRIVE_PROJECT = pathlib.Path('/content/drive/MyDrive/StackNest')",
    "COLAB_PROJECT = pathlib.Path('/content/stacknest')",
    'if DRIVE_PROJECT.exists():',
    '    if COLAB_PROJECT.exists():',
    '        shutil.rmtree(COLAB_PROJECT)',
    '    shutil.copytree(DRIVE_PROJECT, COLAB_PROJECT)',
    '    print("Copied from Drive ->", COLAB_PROJECT)',
    'else:',
    '    print("Drive project not found; continuing with current /content files")',
))

cells.append(code(
    'import os, sys, pathlib',
    'candidates = [pathlib.Path("/content/stacknest"), pathlib.Path("/content")]',
    'PROJECT = None',
    'for c in candidates:',
    '    has_train = (c / "train.py").exists() or (c / "train" / "train.py").exists()',
    '    has_lora = (c / "lora_config.py").exists() or (c / "train" / "lora_config.py").exists()',
    '    if has_train and has_lora:',
    '        PROJECT = c',
    '        break',
    'if PROJECT is None:',
    '    raise FileNotFoundError("Could not find train.py + lora_config.py in /content or /content/stacknest")',
    'os.chdir(PROJECT)',
    'print("Using PROJECT:", PROJECT)',
    'if (PROJECT / "lora_config.py").exists():',
    '    sys.path.insert(0, str(PROJECT))',
    '    from lora_config import LoraConfig, TrainingConfig, ModelConfig, get_bnb_config',
    'else:',
    '    sys.path.insert(0, str(PROJECT))',
    '    from train.lora_config import LoraConfig, TrainingConfig, ModelConfig, get_bnb_config',
    'lora = LoraConfig(); tr = TrainingConfig(); mc = ModelConfig()',
    'print("base:", mc.model_name, "| 4-bit:", mc.load_in_4bit)',
    'print("r:", lora.r, "alpha:", lora.lora_alpha, "epochs:", tr.num_train_epochs)',
))

cells.append(code(
    'import json, random, pathlib',
    'base = pathlib.Path("data/processed")',
    'src = base / "synthetic_train.jsonl"',
    'train_out = base / "train_v2.jsonl"',
    'val_out = base / "val_v2.jsonl"',
    'if src.exists() and (not train_out.exists() or not val_out.exists()):',
    '    rows = [json.loads(line) for line in src.open(encoding="utf-8") if line.strip()]',
    '    random.shuffle(rows)',
    '    split = int(len(rows) * 0.9)',
    '    train_rows, val_rows = rows[:split], rows[split:]',
    '    with train_out.open("w", encoding="utf-8") as f:',
    '        for r in train_rows: f.write(json.dumps(r, ensure_ascii=False) + "\\n")',
    '    with val_out.open("w", encoding="utf-8") as f:',
    '        for r in val_rows: f.write(json.dumps(r, ensure_ascii=False) + "\\n")',
    '    print("train_v2:", len(train_rows), "val_v2:", len(val_rows))',
    'else:',
    '    print("Split exists already or synthetic_train.jsonl missing")',
))

cells.append(code(
    'import pathlib',
    'if pathlib.Path("train.py").exists():',
    '    train_entry = "train.py"',
    'elif pathlib.Path("train/train.py").exists():',
    '    train_entry = "train/train.py"',
    'else:',
    '    raise FileNotFoundError("Could not find train entrypoint")',
    '!python {train_entry} --train-file data/processed/train_v2.jsonl --val-file data/processed/val_v2.jsonl --output-dir output',
))

cells.append(code(
    'import pathlib',
    'out = pathlib.Path("output")',
    'if (out / "lora_adapter" / "adapter_config.json").exists():',
    '    adapter_dir = out / "lora_adapter"',
    'else:',
    '    ckpts = sorted(out.glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[1]) if p.name.split("-")[1].isdigit() else -1)',
    '    adapter_dir = next((p for p in reversed(ckpts) if (p / "adapter_config.json").exists()), None)',
    'if adapter_dir is None:',
    '    raise FileNotFoundError("No adapter_config.json found in output")',
    'print("Adapter:", adapter_dir)',
))

cells.append(code(
    'import re, torch',
    'from transformers import AutoTokenizer, AutoModelForCausalLM',
    'from peft import PeftModel',
    'bnb = get_bnb_config()',
    'tokenizer = AutoTokenizer.from_pretrained(mc.model_name, trust_remote_code=True)',
    'base_model = AutoModelForCausalLM.from_pretrained(mc.model_name, quantization_config=bnb, device_map="auto", trust_remote_code=True)',
    'ft_model = PeftModel.from_pretrained(base_model, str(adapter_dir))',
    'ft_model.eval()',
    'SYSTEM = "You are StackNest, an expert Paper 1.21 Minecraft plugin developer. Output a ```java block and a ```yaml block. Never use ChatColor, NMS, Minestom, or third-party APIs."',
    'def generate(prompt, max_new=800):',
    '    msgs = [{"role":"system","content":SYSTEM},{"role":"user","content":prompt}]',
    '    txt = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)',
    '    inp = tokenizer(txt, return_tensors="pt").to(next(ft_model.parameters()).device)',
    '    with torch.no_grad():',
    '        out = ft_model.generate(**inp, max_new_tokens=max_new, temperature=0.2, do_sample=True, pad_token_id=tokenizer.eos_token_id)',
    '    return tokenizer.decode(out[0][inp.input_ids.shape[1]:], skip_special_tokens=True)',
    'prompts = [',
    '    "Create a Paper 1.21 plugin with a /fly command that toggles flight. Use Adventure API.",',
    '    "Write a Paper plugin that broadcasts a random tip from config.yml every 5 minutes.",',
    '    "Make a Paper 1.21 plugin that opens a crafting GUI with /workbench.",',
    ']',
    'bad_patterns = [r"net\\.minestom", r"net\\.ess3", r"ChatColor", r"org\\.bukkit\\.craftbukkit", r"net\\.minecraft\\.server"]',
    'for i, p in enumerate(prompts, 1):',
    '    resp = generate(p)',
    '    has_java = "```java" in resp',
    '    has_yaml = "```yaml" in resp',
    '    has_paper = ("org.bukkit" in resp) or ("io.papermc" in resp) or ("net.kyori.adventure" in resp)',
    '    bad_hits = [pat for pat in bad_patterns if re.search(pat, resp)]',
    '    print(f"[{i}] java={has_java} yaml={has_yaml} paper={has_paper} bad_hits={len(bad_hits)}")',
    '    if bad_hits: print("  bad:", bad_hits)',
    '    print(resp[:800])',
    '    print("-" * 70)',
))

cells.append(md('## Save StackNest to Drive'))

cells.append(code(
    'from google.colab import drive',
    'import shutil, pathlib, datetime',
    'drive.mount("/content/drive")',
    'src = PROJECT',
    'stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")',
    'dst = pathlib.Path(f"/content/drive/MyDrive/StackNest_Backups/StackNest_{stamp}")',
    'dst.parent.mkdir(parents=True, exist_ok=True)',
    'if dst.exists():',
    '    shutil.rmtree(dst)',
    'shutil.copytree(src, dst)',
    'print("Saved backup:", dst)',
))

cells.append(code(
    'from google.colab import drive',
    'import shutil, pathlib',
    'drive.mount("/content/drive")',
    'canonical = pathlib.Path("/content/drive/MyDrive/StackNest")',
    'if canonical.exists():',
    '    shutil.rmtree(canonical)',
    'shutil.copytree(PROJECT, canonical)',
    'print("Refreshed canonical project at:", canonical)',
))

nb = {
    'nbformat': 4,
    'nbformat_minor': 5,
    'metadata': {
        'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
        'language_info': {'name': 'python'},
        'colab': {'provenance': []},
    },
    'cells': cells,
}

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding='utf-8')
print('wrote', out, 'size', out.stat().st_size)
