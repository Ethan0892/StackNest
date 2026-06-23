# StackNest: Minecraft Plugin & Mod Generator — Complete Architecture

> **Infrastructure**: Hetzner VPS (cloud-hosted, `stacknests.com`)  
> **Status**: Live — production deployment  
> **Target**: Paper / Folia / Velocity plugins + Fabric / Forge / NeoForge mods  
> **Date**: April 2026 (updated from February 2026 draft)

---

## Table of Contents

1. [Overall Architecture](#1-overall-architecture)
2. [Data Ingestion Pipeline](#2-data-ingestion-pipeline)
3. [Dataset Formatting Strategy](#3-dataset-formatting-strategy)
4. [Model Strategy](#4-model-strategy)
5. [Training Plan](#5-training-plan)
6. [Inference Setup](#6-inference-setup)
7. [Validation Layer](#7-validation-layer)
8. [Monetization Path](#8-monetization-path)
9. [Realistic Limitations](#9-realistic-limitations)

---

## 1. Overall Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        USER REQUEST                                 │
│   stacknests.com/app — plugin or mod description + options         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  HTTPS
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│               nginx (reverse proxy, TLS termination)                │
│  Static assets served directly; /api/* proxied to Gunicorn         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  localhost:5000
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│             Gunicorn WSGI server  (api/app.py — Flask)              │
│  Endpoints:                                                         │
│    POST /api/stream          SSE streamed generation (plugin / mod) │
│    POST /api/generate        Blocking generation + validation loop  │
│    POST /api/validate        Deep-check with Kimi K2.5              │
│    POST /api/heal            Context-aware error repair             │
│    POST /api/jar             Compile source → downloadable .jar     │
│    POST /api/logs/analyze    Server log diagnosis                   │
│    GET  /api/gallery         Community plugin gallery               │
│    POST /api/auth/*          Register, login, Google OAuth          │
│    POST /api/stripe/*        Stripe billing webhooks               │
│    GET  /api/admin/*         Admin panel (IP mgmt, stats, logs)     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│  PROMPT BUILDER  │  │  CLOUD INFERENCE │  │  VALIDATION PIPELINE │
│  inference/      │  │  inference/      │  │  validation/         │
│  router.py       │  │  server.py       │  │                      │
│                  │  │                  │  │  1. Static patterns  │
│  · Classify      │  │  Free tier:      │  │     (deprecated APIs,│
│    intent type   │  │   Kimi k2-turbo  │  │     NMS imports,     │
│  · RAG retrieval │  │   → Gemini Flash │  │     common mistakes) │
│    (ChromaDB)    │  │   → Claude Haiku │  │  2. javac + Paper    │
│  · Build system  │  │                  │  │     stub JAR compile │
│    prompt with   │  │  Premium tier:   │  │  3. plugin.yml YAML  │
│    doc context   │  │   Claude         │  │     schema check     │
│  · Inject doc    │  │   → Kimi         │  │  4. Cross-checks     │
│    context       │  │   → Gemini       │  │     (class↔yml,      │
│    (docs_cache)  │  │                  │  │      cmd reg, etc.)  │
│  · Mod intent    │  │  All with retry  │  │  5. Heal loop ×3     │
│    classifier    │  │  + failover      │  │     (inject errors   │
│  · Template      │  │                  │  │      → regenerate)   │
│    skeleton      │  └──────────────────┘  └──────────────────────┘
│    injection     │
└──────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PACKAGED OUTPUT                                  │
│  · Plugin: Main.java + plugin.yml + supporting classes + pom.xml   │
│  · Mod (Fabric): Main.java + fabric.mod.json + build.gradle.kts    │
│  · Mod (Forge/NeoForge): Main.java + mods.toml + build.gradle      │
│  · JUnit 5 + MockBukkit test class (plugins only)                  │
│  · Optional: compiled .jar ready for server deployment             │
└─────────────────────────────────────────────────────────────────────┘
```

### Architecture decisions since February 2026 draft

| Decision | February plan | April reality |
|---|---|---|
| Inference | Local llama.cpp on Pi 5 | Cloud APIs (Kimi / Gemini / Claude) |
| Hardware | Raspberry Pi 5 8GB | Hetzner VPS (Debian, multi-core) |
| Fine-tuning | LoRA on Qwen2.5-Coder-3B | Abandoned — cloud APIs far superior |
| Targets | Paper plugins only | Paper + Folia + Velocity + Fabric + Forge + NeoForge |
| Auth | None | Email + Google OAuth + Discord OAuth |
| Billing | None | Stripe (monthly + annual plans) |
| Domain | Planned | Live at `stacknests.com` |
| Latency | 45–120 s (Pi) | 5–20 s (cloud streaming) |

---

## 2. Data Ingestion Pipeline

### 2.1 Source Selection

Target GitHub repositories using the following query:
```
topic:minecraft-plugin language:Java stars:>50 license:MIT OR license:Apache-2.0
```

**Priority plugins to include (representative API surface coverage):**

| Plugin | API Patterns Covered |
|--------|----------------------|
| EssentialsX | Commands, config, economy API |
| Vault | Service provider pattern |
| WorldGuard | Region flags, protection API |
| LuckPerms | Permission nodes |
| CMI (lite fork) | Multi-feature plugin structure |
| PlaceholderAPI | Placeholder expansion |
| Citizens | NPC API, traits |
| Jobs Reborn | Economy events |
| ChestShop | Inventory events, economy |
| ProtocolLib | Packet listeners |

Supplement with 90 smaller plugins from [Hangar](https://hangar.papermc.io) (open-source filter).

### 2.2 Ingestion Script

```python
# scripts/ingest.py
import subprocess, os, json, pathlib

REPOS = [
    "EssentialsX/Essentials",
    "PaperMC/Paper",       # source reference only, not training
    # ... 
]

def clone_and_extract(repo: str, out_dir: str):
    name = repo.split("/")[1]
    subprocess.run(["git", "clone", "--depth=1",
                    f"https://github.com/{repo}", f"{out_dir}/{name}"])
    
    java_files = list(pathlib.Path(f"{out_dir}/{name}").rglob("*.java"))
    yml_files  = list(pathlib.Path(f"{out_dir}/{name}").rglob("plugin.yml"))
    pom_files  = list(pathlib.Path(f"{out_dir}/{name}").rglob("pom.xml"))
    
    return {
        "name": name,
        "java": [f.read_text(errors="ignore") for f in java_files],
        "plugin_yml": yml_files[0].read_text() if yml_files else "",
        "pom": pom_files[0].read_text() if pom_files else "",
    }
```

### 2.3 Filtering Rules (DROP if any match)

- File > 800 lines (too large for context window)
- Uses Spigot 1.8 deprecated APIs (`org.bukkit.craftbukkit` internal imports)
- Obfuscated identifiers (variable names < 2 chars average)
- Zero Javadoc coverage
- Requires external non-Paper dependencies not in your stub jar

### 2.4 Chunking Strategy

Do **not** train on entire plugins at once. Chunk by semantic unit:

```
Level 1: Full plugin (main class + plugin.yml)  — for small plugins < 200 lines
Level 2: Individual feature class               — for medium plugins
Level 3: Method-level snippet + context header  — for large plugins
```

---

## 3. Dataset Formatting Strategy

### 3.1 Format: Instruction-Response JSONL

Each line is one training example:

```json
{
  "system": "You are a senior Paper plugin developer. Generate correct, compilable Paper 1.21 plugin code. Always include plugin.yml. Never use deprecated Spigot APIs. Use Adventure API for chat components.",
  "instruction": "Create a Paper plugin that broadcasts a message every 60 seconds using the BukkitScheduler. The message should be configurable in config.yml.",
  "response": "```java\n// src/main/java/com/example/announcer/AnnouncerPlugin.java\npackage com.example.announcer;\n\nimport org.bukkit.Bukkit;\nimport org.bukkit.plugin.java.JavaPlugin;\nimport net.kyori.adventure.text.Component;\nimport net.kyori.adventure.text.minimessage.MiniMessage;\n\npublic class AnnouncerPlugin extends JavaPlugin {\n    private final MiniMessage mm = MiniMessage.miniMessage();\n\n    @Override\n    public void onEnable() {\n        saveDefaultConfig();\n        String raw = getConfig().getString(\"message\", \"<yellow>Server announcement!\");\n        long intervalTicks = getConfig().getLong(\"interval-seconds\", 60) * 20L;\n\n        Bukkit.getScheduler().runTaskTimer(this, () ->\n            Bukkit.broadcast(mm.deserialize(raw)),\n            intervalTicks, intervalTicks\n        );\n        getLogger().info(\"AnnouncerPlugin enabled.\");\n    }\n}\n```\n```yaml\n# plugin.yml\nname: Announcer\nversion: 1.0.0\nmain: com.example.announcer.AnnouncerPlugin\napi-version: '1.21'\ndescription: Broadcasts configurable messages on a timer.\n```\n```yaml\n# config.yml\nmessage: '<yellow>Welcome to the server!'\ninterval-seconds: 60\n```"
}
```

### 3.2 Instruction Generation Strategy

**Do NOT write instructions by hand for all 100 plugins.**

Use this semi-automated approach:
1. Feed each plugin's `plugin.yml` description + main class to a local model (`ollama run qwen2.5:3b`) to generate a natural-language instruction.
2. Human review pass: 15 minutes of spot-checking per batch of 20.
3. Store both the generated instruction and the ground-truth code.

```python
# scripts/generate_instructions.py
PROMPT = """
Given this Paper plugin description and main class, write a single clear
natural-language instruction that a server owner might type to request this plugin.
Be specific about features, not vague.

plugin.yml description: {desc}
Main class snippet (first 50 lines): {snippet}

Output ONLY the instruction, no explanation.
"""
```

### 3.3 Example Categories to Cover

| Category | Min Examples | API Surface |
|----------|-------------|-------------|
| Scheduler tasks | 8 | `BukkitScheduler`, `runTaskTimer` |
| Event listeners | 15 | `@EventHandler`, `Listener`, `PlayerJoinEvent` etc. |
| Commands | 12 | `CommandExecutor`, `TabCompleter` |
| Config YAML | 10 | `FileConfiguration`, `saveDefaultConfig` |
| Permissions | 6 | `player.hasPermission()` |
| Economy (Vault) | 5 | `Economy` service |
| Database (SQLite) | 4 | `HikariCP`, `PreparedStatement` |
| GUIs (inventory) | 8 | `Inventory`, `InventoryClickEvent` |
| Chat (Adventure) | 6 | `MiniMessage`, `Component` |
| Multi-file plugins | 10 | package structure, manager classes |

**Target: 84+ training examples, 15 validation examples, 5 held-out test examples.**

---

## 4. Model Strategy

### 4.1 Current Production Inference Stack

Local llama.cpp was removed. The server has no GPU and insufficient RAM for an inference model alongside the Flask app, database, and other services. Cloud APIs deliver dramatically higher quality at lower operational complexity.

```
inference/server.py — Cloud-only router

Free tier priority:     Kimi k2-turbo  →  Gemini 2.0 Flash  →  Claude 3.5 Haiku
Premium tier priority:  Claude 3.5 Sonnet  →  Kimi k2-turbo  →  Gemini 2.0 Flash

Each provider implements:
  · generate(instruction, system_prompt, params) → str
  · generate_stream(...)                         → Generator[str]
  · heal(code, errors)                           → str  (error correction)
  · is_available()                               → bool (key configured?)

Failover: if provider N raises an exception (rate limit, quota, API error),
the router automatically tries provider N+1. All three must fail for the
request to return an error to the user.
```

### 4.2 Provider Roles

| Provider | Model | Role | Cost |
|---|---|---|---|
| Kimi (Moonshot AI) | kimi-k2-turbo-preview | Primary free-tier gen | Pay-per-token |
| Kimi (Moonshot AI) | kimi-k2.5 | Deep validation (`/api/validate`) | Pay-per-token |
| Google Gemini | gemini-2.0-flash | Free-tier fallback | Free (1500 req/day quota) |
| Anthropic Claude | claude-3-5-haiku-20241022 | Free-tier last resort / heal | Pay-per-token |
| Anthropic Claude | claude-3-5-sonnet | Premium primary | Pay-per-token |

### 4.3 Why Fine-Tuning Was Abandoned

The original plan was to LoRA-fine-tune Qwen2.5-Coder-3B on ~84 Paper plugin examples. This was deprioritised because:

1. **Quality gap was too large** — even a fine-tuned 3B model hallucinated API methods and produced incomplete code. Claude 3.5 Haiku produces compilable code on the first attempt >75% of the time.
2. **Infrastructure mismatch** — the Hetzner VPS is a CPU-only machine; loading a 2.2 GB GGUF alongside Gunicorn would exhaust available RAM.
3. **Training dataset is ready** — `data/processed/train.jsonl` (84 examples) and the LoRA config in `train/lora_config.py` still exist. Fine-tuning remains a future option for a GPU-equipped tier to reduce API costs at scale.

### 4.4 Prompt Engineering (replaces fine-tuning quality gap)

The system prompt in `inference/router.py` (`SYSTEM_PROMPT`) is the primary quality driver. It encodes every Paper 1.21 idiom that a fine-tuned model would have learned from training data:

- Adventure API for all text (`Component.text(...)`, never `ChatColor`)
- `getCommand()` null-safety guard before `setExecutor()`
- `registerEvents()` call required for every `Listener` class
- `api-version: '1.21'` (string, not float) in plugin.yml
- `org.bukkit.command.PluginCommand` (not `plugin.PluginCommand`)
- MockBukkit JUnit 5 test class structure

This 150-line system prompt was iteratively refined from real compile errors observed in the validation pipeline logs.

### 4.5 RAG Context Injection

Two retrieval paths inject live documentation into every prompt:

```python
# api/docs_cache.py

# Plugin requests → PaperMC + Adventure docs
get_doc_context(instruction)           → up to 2 matched pages, 800 chars each

# Mod requests → loader-filtered docs
get_mod_doc_context(instruction, loader)
  # loader='fabric'   → fabricmc.net / fabric.mod.json docs
  # loader='forge'    → forge.gemwire.uk / DeferredRegister docs
  # loader='neoforge' → docs.neoforged.net / IEventBus docs
```

Doc pages are fetched once and cached for 24 hours. Matching is by keyword scoring against the user's instruction — no embedding required, zero cold-start cost.

---

## 5. Training Plan

> **Status**: Training dataset assembled and validated. Fine-tuning deferred pending GPU infrastructure.

The training pipeline code (`train/`, `scripts/`) is preserved and functional. The dataset at `data/processed/train.jsonl` contains 84 curated Paper 1.21 plugin examples in instruction-response JSONL format.

### 5.1 Dataset Status

```
data/processed/
  train.jsonl     84 examples — Paper 1.21, full plugin + plugin.yml + test class
  val.jsonl       15 examples — held-out validation
  test.jsonl       5 examples — held-out test
```

### 5.2 If/When Fine-Tuning Resumes

The LoRA config in `train/lora_config.py`:

```python
LoraConfig(
    r=8,
    lora_alpha=16,
    target_modules=["q_proj", "v_proj", "k_proj", "o_proj"],
    lora_dropout=0.1,
    bias="none",
    task_type="CAUSAL_LM",
)
```

Recommended path: Google Colab A100 (Colab Pro+) → export adapter → merge with `llama.cpp/convert_lora_to_gguf.py` → deploy as a sidecar service on dedicated GPU hardware for Studio-tier users.

### 5.3 Token Budget

```
Max context (Qwen2.5-Coder-3B-Instruct): 8192 tokens
System prompt:  ~120 tokens
Instruction:     ~80 tokens
Response:        ~900 tokens (target ceiling)
Per-example:    ~1100 tokens

84 examples × 1100 × 3 epochs = ~277,000 training tokens
Estimated training time on A100 (Colab): ~8 minutes
```

---


## 6. Inference Setup

### 6.1 Production Stack

```
stacknests.com
│
├── nginx (TLS, static files, rate limiting)
│     ├── /          → /opt/stacknest/frontend/  (served directly)
│     └── /api/*     → localhost:5000 (Gunicorn)
│
├── Gunicorn  (api/app.py)
│     bind:         127.0.0.1:5000
│     workers:      cpu_count * 2 + 1
│     worker_class: sync
│     timeout:      420s  (accommodates multi-model heal chain)
│
├── Flask API (api/app.py)
│     · Rate limiting via Flask-Limiter
│     · SQLite database (api/db.py) — requests, users, gallery
│     · Stripe webhooks for billing
│     · Google + Discord OAuth
│
├── Inference router (inference/server.py)
│     · Kimi k2-turbo  (KIMI_API_KEY)
│     · Gemini 2.0 Flash  (GEMINI_API_KEY)
│     · Claude 3.5 Haiku / Sonnet  (CLAUDE_API_KEY)
│
└── Discord bot (discord_bot/bot.py)  — separate systemd service
```

### 6.2 Generation Modes

**Plugin mode** (Paper 1.21 / Folia / Velocity / Spigot / Purpur / BungeeCord):
- Intent classified into: `command | event | scheduler | gui | full_plugin | config`
- Template skeleton injected per type (`templates/plugin_templates/*.java`)
- RAG: top-3 similar training examples from ChromaDB (`all-MiniLM-L6-v2` embeddings)
- Doc context: up to 2 PaperMC/Adventure doc pages matched to instruction keywords
- Output: Main.java + plugin.yml + supporting classes + JUnit 5 MockBukkit test
- `max_tokens`: 3000 (streaming) / 3000 (progress)

**Mod mode** (Fabric / Forge / NeoForge):
- Intent classified into: `custom_item | custom_block | custom_entity | world_gen | network_packet | full_mod`
- Loader-specific system prompt injected (`MOD_SYSTEM_PROMPTS` in `router.py`)
- `_MOD_TYPE_EXTRA` dict injects targeted API guidance per type (DeferredRegister, registerGoals, BiomeModifications, SimpleChannel etc.)
- Doc context: loader-filtered pages (fabricmc.net / forge.gemwire.uk / docs.neoforged.net)
- Static patterns checker: 12 Fabric / 10 Forge / 7 NeoForge / 4 universal patterns

### 6.3 Streaming Architecture

`/api/stream` uses Server-Sent Events (SSE):

```
Client ──── POST /api/stream ─────► Flask (SSE response)
                                        │
                              ┌─────────┴──────────┐
                              │  Cloud API stream   │
                              │  (Kimi/Gemini/      │
                              │   Claude)           │
                              └─────────┬──────────┘
                                        │ token chunks
                              Flask writes data: {...}\n\n
                                        │
Client receives tokens live ◄───────────┘
```

The client-side JS in `frontend/app.html` renders the SSE stream token-by-token into a syntax-highlighted code editor.

### 6.4 Context-Aware Healing (`/api/heal`)

When the user receives code with errors (or the validation loop fails), `/api/heal` runs a dedicated repair chain:

```
1. User submits: code + error messages
2. Claude heal prompt: "fix EVERY error without removing any functionality"
   (if Claude unavailable → Kimi heal → Gemini heal)
3. Response is a complete corrected plugin — not a diff
4. Fence balancer stitches truncated markdown if the model hit token limit
5. Optional: re-run javac compile check, return updated compile status
```

The heal system prompt (`claude.py:_HEAL_SYSTEM`) enforces: first character must be a backtick (no preamble), fix every error, preserve all features, no explanations after closing fence.

### 6.5 JAR Compilation (`/api/jar`)

```python
# validation/compile_check.py
build_jar(java_files, plugin_yml) → bytes  # ready-to-deploy .jar

Steps:
  1. Write all Java source files to a temp directory
  2. javac -cp paper-api-1.21-R0.1-SNAPSHOT-shaded.jar -source 21 -target 21 *.java
  3. Write plugin.yml to temp dir
  4. jar cf output.jar -C classes . + plugin.yml
  5. Return jar bytes for download
```

Users can click "Download JAR" in the app to get a ready-to-drop-in server plugin.

### 6.6 Latency

```
Streaming (SSE, first token):   2–4 s   (cloud API response start)
Full plugin generation:         8–20 s  (350-800 token response)
Heal pass (if needed):         10–25 s  (complete plugin correction)
JAR compilation:                1–3 s   (javac on Hetzner VPS)

Total round-trip (good path):   10–25 s
Total round-trip (with 1 heal): 20–50 s

Comparison to February plan (Pi 5 llama.cpp): 45–120 s
Improvement: 3–6×
```

---

## 7. Validation Layer

### 7.1 Static Analysis (Fast, Pre-Compile)

`validation/static_check.py` runs before `javac` — catches common model mistakes in milliseconds.

**Plugin patterns** (always checked):
- `sendMessage(String)` — use Adventure `Component.text()`
- `org.bukkit.craftbukkit` — NMS/CraftBukkit internal import
- `ChatColor.` — removed in 1.21
- `setMetadata|getMetadata` — prefer PersistentDataContainer
- Missing `getCommand().setExecutor()` registration
- `org.bukkit.plugin.PluginCommand` — wrong package (correct: `org.bukkit.command`)

**Fabric patterns** (12 rules): correct event subscription, `ServerLifecycleEvents`, `ServerPlayNetworkHandler`, `fabric.mod.json` boilerplate errors

**Forge patterns** (10 rules): `FMLJavaModLoadingContext` (banned), `@Mod.EventBusSubscriber`, wrong bus (`FORGE_BUS` vs `MOD_BUS`), unchecked capability casts

**NeoForge patterns** (7 rules): `net.minecraftforge.*` imports (wrong — must be `net.neoforged.*`), old `@SubscribeEvent` patterns, missing `IEventBus` constructor param

### 7.2 Compilation Check

```python
# validation/compile_check.py
PAPER_STUB_JAR = "libs/paper-api-1.21-R0.1-SNAPSHOT-shaded.jar"

def compile_plugin(response: str) -> CompileResult:
    # Extract all ```java blocks + paths from response
    # Write to temp directory, preserving package structure
    # javac -cp paper-stub.jar -source 21 -target 21 *.java
    # Return CompileResult(success, errors, files_compiled)
```

This catches: missing imports, wrong method signatures, nonexistent API methods, type errors.

### 7.3 plugin.yml Validation + Cross-Checks

```python
# validation/yml_check.py
REQUIRED_KEYS = {"name", "version", "main", "api-version"}

Checks performed:
  · All required keys present
  · api-version is '1.21' (string)
  · main: matches the Java class package + name declared in the source
  · Every command declared in plugin.yml has a handler in Java
  · Every permission used in hasPermission() is declared in plugin.yml
  · No command declared in plugin.yml is missing setExecutor() in onEnable()
```

### 7.4 Error Feedback Loop

```python
# validation/feedback_loop.py
MAX_ATTEMPTS = 3

for attempt in range(MAX_ATTEMPTS):
    code = generate(instruction, history)
    static_warns = static_check(code)
    compile_result = compile_plugin(code)
    yml_result = validate_yml(code)

    if compile_result.success and yml_result.valid:
        return GenerationResult(success=True, code=code, attempts=attempt+1)

    # Inject errors into correction prompt
    history.append({"attempt": attempt+1, "code": code, "errors": ...})

# After 3 failures: return best attempt + all errors
```

The correction prompt format appends the error list to the previous code and instructs the model to fix every listed error specifically — not regenerate from scratch.

### 7.5 fence-balancing (`_balance_fences`)

When a model hits its token limit mid-generation, the last markdown ``` block is unclosed. This confuses the heal-model and causes error counts to increase instead of decrease. `feedback_loop._balance_fences()` detects odd open/close fence counts and appends a closing ``` before passing to the next attempt.

---

## 8. Product & Monetization

### 8.1 Current Tier Structure

```
FREE  — 3 generations / month
  · Plugin and mod generation (Kimi → Gemini → Claude)
  · Streaming output
  · Validation + compile check
  · JAR download
  · Community gallery

STARTER — £3/month (or £27/year)
  · 15 generations / month
  · All free features

PRO — £8/month (or £72/year)
  · 100 generations / month
  · Premium inference priority (Claude first)
  · All starter features

STUDIO — £18/month (or £162/year)
  · 300 generations / month
  · Priority queue
  · All pro features
```

Billing: Stripe (monthly + annual price IDs). Webhooks update the SQLite `users` table on `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.

### 8.2 Auth System

```
Email + password  → verification email via api/mailer.py (SMTP)
Google OAuth      → one-tap sign-in via Google Identity Services
Discord OAuth     → link/unlink Discord account; bot role sync on plan change
```

JWT-style session tokens stored as signed cookies. User data (projects, usage, plan) in SQLite via `api/db.py`.

### 8.3 User Project Storage

Each authenticated user can save generated plugins as named projects:

```
save_user_project(user_id, name, instruction, code)  → project_id
list_user_projects(user_id)                          → [{id, name, ts}, ...]
get_user_project(user_id, project_id)                → {instruction, code}
delete_user_project(user_id, project_id)
```

Stored in SQLite `projects` table. No file system per user — avoids directory traversal risk.

### 8.4 Community Gallery

Users can submit generated plugins publicly:

```
POST /api/gallery/submit  → {instruction, code, plugin_name}
GET  /api/gallery         → paginated public entries (50/page)
GET  /api/gallery/<id>    → single entry + full code
POST /api/gallery/<id>/like → increment likes (dedup by IP hash)
```

Gallery entries drive organic SEO — each public plugin is a unique content page.

### 8.5 Infrastructure (Current)

```
stacknests.com (Hetzner VPS, Debian)
│
├── nginx + Let's Encrypt TLS
├── Gunicorn (Flask API, systemd service: stacknest.service)
├── SQLite (data/stacknest.db, daily backups to data/backups/)
├── Discord bot (systemd service: stacknest-bot.service)
├── Automated DB backups + restore scripts (scripts/backup_stacknest.sh)
└── Deployment: git pull + systemctl reload (deploy/update.sh)
```

### 8.6 Scaling Path

```
Stage 1 (Now):
  Hetzner VPS → Gunicorn → Flask → Cloud APIs (Kimi/Gemini/Claude)
  Zero on-device inference load. Variable cost scales with usage.

Stage 2 (£200–500 MRR):
  Add Redis for rate limiting (replace in-memory Flask-Limiter)
  Add PostgreSQL if SQLite write contention becomes an issue
  Separate worker process for heavy JAR compilation queue

Stage 3 (£1000+ MRR):
  GPU inference node (Lambda Labs A10 spot: ~$0.75/hr)
  Run fine-tuned 7B model for Studio tier → eliminate API cost for power users
  Retain cloud APIs as fallback only
```

---

## 9. Limitations & Known Failure Modes

### 9.1 What the System Does Well (April 2026)

- ✅ Paper 1.21 plugins: boilerplate, commands, event handlers, schedulers, GUIs, config YAML, economy stubs
- ✅ Multi-file plugins with inner/static nested classes (single-file strategy)
- ✅ JUnit 5 + MockBukkit test scaffold generated automatically
- ✅ Adventure API text throughout (no ChatColor regressions)
- ✅ Fabric mods: item/block/entity/command registration, event subscribers, keybinds
- ✅ Forge mods: DeferredRegister pattern, IEventBus constructor injection
- ✅ NeoForge mods: net.neoforged.* imports, correct mods.toml structure
- ✅ JAR compilation + download: compile check passes for ~75% of first-attempt outputs
- ✅ Healing: `/api/heal` resolves most compile errors in one correction pass

### 9.2 Current Limitations

| Limitation | Root cause | Mitigation / Workaround |
|---|---|---|
| Complex multi-class plugins (>600 lines) occasionally truncate | Cloud model token limit hit | Fence balancer + heal pass; system prompt pushes inner-class strategy |
| Forge/NeoForge mods less reliable than plugins | Fewer training examples; smaller API surface in RAG | Expanded static patterns (10/7 rules) + loader-specific doc injection |
| Folia async-safe code not always correct | Folia threading model is complex | Document limitation; separate Folia system prompt planned |
| Multi-turn dialogue ("refine this plugin") | Not built — single-shot generation only | Project save/reload + re-generate workaround (clunky) — **#2 priority gap** |
| PlaceholderAPI, LuckPerms, MythicMobs integrations | Not in RAG index or training data | User must describe integration; model uses general API knowledge |
| Generated pom.xml dependency versions | Resolution is external knowledge | `pom_template.xml` filled programmatically with known-good versions |

### 9.3 API Hallucination Patterns (Monitored)

1. `player.setFly()` (doesn't exist — correct: `player.setAllowFlight(true)`)
2. `getCommand("name")` without null check → NPE at startup
3. `org.bukkit.plugin.PluginCommand` (wrong package — correct: `org.bukkit.command`)
4. Unregistered `Listener` classes (events silently never fire)
5. `sendMessage(String)` — removed in 1.21, must use `sendMessage(Component)`
6. `ChatColor.` — removed in 1.21
7. Forge: `FMLJavaModLoadingContext.get().getModEventBus()` — patterns like this are caught by static check

All 7 patterns are in `_STATIC_PATTERNS` / `_MOD_STATIC_PATTERNS` and trigger a penalty/correction prompt when detected.

### 9.4 Quality Roadmap

In rough priority order:

1. **Compile-and-retry for mods** — mod generation currently lacks the full feedback loop that plugin generation has. Add `javac` + mod-loader stub JARs to `compile_check.py`.
2. **Multi-turn refinement** — "Refine this plugin" is the most natural follow-up action after generation and it isn't supported yet. The project save/reload workaround is clunky. This is the most impactful UX gap: implement session context so successive prompts amend the previous output rather than regenerating from scratch.
3. **Forge/NeoForge ChromaDB 3-shot RAG** — `docs_cache` covers the loader APIs but not with the precision of embedding-similarity retrieval. A ChromaDB index over Forge/NeoForge Javadoc + sample mods would close the reliability gap for NeoForge 1.21+.
4. **Folia-safe generation** — dedicated Folia system prompt enforcing `RegionScheduler`/`AsyncScheduler` instead of `BukkitRunnable`.
5. **Fine-tuned 7B model** — when MRR justifies GPU cost, a domain-fine-tuned 7B model will outperform general 3B models on Paper-specific patterns at lower token cost than Claude Sonnet.

---

## Quick Reference — File Map

```
api/
  app.py          Flask API — all HTTP endpoints, auth, Stripe, admin
  db.py           SQLite ORM — users, projects, gallery, requests, tickets
  docs_cache.py   RAG doc fetcher — PaperMC / Fabric / Forge / NeoForge pages
  mailer.py       SMTP email (verification, password reset)

inference/
  router.py       Prompt builder — intent classification, RAG retrieval, system prompt
  server.py       Cloud inference router — Kimi → Gemini → Claude failover
  claude.py       Anthropic Claude client (gen + heal + stream)
  gemini.py       Google Gemini client (gen + heal + stream)
  kimi.py         Moonshot Kimi client (gen + validate + heal)
  watchdog.py     Credit exhaustion detection → Discord alert

validation/
  feedback_loop.py  Generate → validate → heal loop (max 3 attempts)
  compile_check.py  javac + Paper stub jar compilation + jar builder
  static_check.py   Regex pattern checks (deprecated APIs, wrong imports)
  yml_check.py      plugin.yml schema validator + cross-checks

frontend/
  app.html        Single-page app — plugin + mod generator, streaming, editor
  gallery.html    Community plugin gallery
  profile.html    User dashboard, project list, usage stats
  pricing.html    Tier comparison + Stripe checkout
  admin.html      Admin panel (requests, users, IP management)
  auth.js         Shared auth helpers (token refresh, Google OAuth)

discord_bot/
  bot.py          Slash commands (/generate, /validate), credit alerts, role picker

templates/
  plugin_templates/  Java skeleton files injected per plugin type
  pom_template.xml   Maven POM with correct Paper + MockBukkit dependency versions

deploy/
  nginx.stacknest.conf  nginx config (TLS, static files, rate limits, CSP)
  stacknest.service     systemd unit for Gunicorn
  update.sh             git pull + systemctl reload deploy script
```

---

*Architecture version 2.0 — StackNest*  
*Updated April 2026 — live at stacknests.com*  
*Cloud inference (Kimi / Gemini / Claude) + Hetzner VPS + Paper 1.21 / Fabric / Forge / NeoForge*
