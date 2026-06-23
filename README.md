# StackNest

> **AI-powered Minecraft plugin generation.** Describe what you want. Get compiled, validated Java + plugin.yml + unit tests in seconds.

StackNest generates production-ready [Paper](https://papermc.io/software/paper) / Folia / Velocity / Spigot plugins from plain-English descriptions, powered by Claude AI with Gemini fallback.

- **javac validated** — every output is compiled against the real Paper 1.21 API before it reaches you  
- **MockBukkit unit tests** — JUnit 5 test classes generated alongside plugin code  
- **Plugin type selector** — Full Plugin, Command, Event Listener, Scheduler, GUI  
- **Feature chips** — Vault economy, PlaceholderAPI, SQLite, config, permissions, cooldowns  
- **Download-ready** — complete Maven project ZIP included  
- **Discord bot hosting** — generate with AI or upload your own Python bot files (`.py`/`.zip`) and deploy from `/bots`  
- **MC 1.21.x + Minecraft 26.x** — stable target is Paper 1.21/Java 21; opt-in to 26.x (Java 25, alpha)

---

## Pricing

| Tier | Price | Generations / mo | Notes |
|------|-------|-----------------|-------|
| **Free** | £0 | 3 | Paper & Spigot only |
| **Starter** | £4 | 15 | + Folia & Velocity |
| **Pro** | £9 | 100 | + Claude AI, all platforms |
| **Studio** | £29 | 300 | + 5 team seats, API access |

Pay-as-you-go credits (Gemini quality, never expire) also available.  
Contact **hello@stacknests.com** for billing help.

---

## Deploying

The app runs on a Hetzner VPS behind nginx + gunicorn. See [SERVER.md](SERVER.md) for SSH access, systemd management, and the full deploy workflow.

Quick deploy after a local commit:

```bash
git push
ssh -p 2222 root@65.109.137.196 "cd /opt/stacknest && git pull && systemctl restart stacknest"
```

---

## Local Development Setup

### Prerequisites

```bash
# Python 3.11+
pip install -r deploy/requirements-api.txt   # API-only, no ML deps

# Optional: full ML deps (training, ChromaDB, embeddings)
pip install -r requirements.txt
```

### Run locally

```bash
cp .env.example .env   # fill in GEMINI_API_KEY / CLAUDE_API_KEY at minimum
python api/app.py
```

Access at **http://localhost:5000**

---

## Data Pipeline (optional — re-training only)

```bash
# Ingest plugin source code from GitHub
python scripts/ingest.py --output data/raw --limit 30

# Chunk into training units
python scripts/chunk.py --input data/raw --output data/processed

# Generate instruction pairs
python scripts/generate_instructions.py --input data/processed/chunks.jsonl \
  --output data/processed/train.jsonl

# Validate token budgets
python scripts/validate_jsonl.py --input data/processed/train.jsonl --fix

# Build ChromaDB retrieval index
python scripts/embed.py --input data/processed/train.jsonl \
  --output data/embeddings/chromadb
```

Fine-tuning (Colab / GPU) lives in `train/` — see `train/StackNest_Retrain_Colab_Fixed.ipynb`.

---

## Project Structure

```
StackNest/
├── api/                    # Flask REST API + gunicorn
│   ├── app.py              # All routes (/api/*, /gallery, /auth, ...)
│   ├── db.py               # SQLite helpers
│   └── mailer.py           # Transactional email (Brevo SMTP)
├── deploy/                 # Server config & deploy scripts
│   ├── nginx.stacknest.conf
│   ├── stacknest.service
│   └── requirements-api.txt
├── discord_bot/            # Standalone Discord bot
├── frontend/               # Static HTML/JS UI
│   ├── index.html          # Marketing landing page
│   ├── app.html            # Plugin generator
│   ├── gallery.html        # Public plugin gallery
│   ├── pricing.html        # Pricing page
│   └── ...
├── inference/              # AI backends + prompt routing
│   ├── router.py           # SYSTEM_PROMPT, RAG, version constants
│   ├── server.py           # llama.cpp client + generate_with_fallback
│   ├── watchdog.py         # Backend health monitor
│   ├── claude.py           # Anthropic Claude client
│   ├── gemini.py           # Google Gemini client
│   └── kimi.py             # Moonshot Kimi client
├── scripts/                # Data pipeline
│   ├── ingest.py
│   ├── chunk.py
│   ├── embed.py
│   ├── validate_jsonl.py
│   └── patches/            # One-off migration scripts (applied, archived)
├── templates/              # pom_template.xml, Java plugin scaffolds
├── train/                  # LoRA fine-tuning (Colab)
└── validation/             # Post-generation checks
    ├── compile_check.py    # javac + Paper JAR
    ├── static_check.py     # Deprecated API detection
    ├── yml_check.py        # plugin.yml schema
    └── feedback_loop.py    # Retry + truncation recovery
```

---

## API Reference

All endpoints at `https://stacknests.com` (or `http://localhost:5000` locally).

### `POST /api/generate`

```json
{
  "instruction": "Create a /heal command that restores the player's health",
  "plugin_name": "HealCommand",
  "folia_compatible": false,
  "skip_compile": false
}
```

Response includes `success`, `code`, `attempts`, `elapsed_seconds`, `warnings`, `errors`.

### `POST /api/generate-progress`

Same as `/api/generate` but streams real-time progress via SSE.

### `GET /api/health`

Returns `{ "api": "ok", "inference_server": "ok"|"offline", "backends": {...} }`.

---

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Google Gemini (primary free-tier backend) |
| `CLAUDE_API_KEY` | — | Anthropic Claude (Pro/Studio backend) |
| `KIMI_API_KEY` | — | Moonshot Kimi (deep-check / truncation recovery) |
| `LLAMACPP_URL` | `http://localhost:8080` | Optional local llama-server |
| `CHROMADB_PATH` | `data/embeddings/chromadb` | RAG index path |
| `STRIPE_SECRET_KEY` | — | Payments |
| `DISCORD_BOT_TOKEN` | — | Discord OAuth + bot |
| `SMTP_*` | — | Transactional email (Brevo) |
| `ADMIN_SECRET` | — | Admin panel password |
| `PORT` | `5000` | Flask listen port |

See `.env.example` for the full list.

---

## Legal

- [Terms of Service](https://stacknests.com/terms)  
- [Privacy Policy](https://stacknests.com/privacy)  
- Contact: **hello@stacknests.com**

---

*Built with Claude AI, Gemini, Flask, gunicorn, nginx, ChromaDB, MockBukkit, and Stripe.*
