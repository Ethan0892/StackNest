"""
scripts/generate_training_data.py
-----------------------------------
Generate high-quality Paper 1.21 plugin training examples via Kimi K2.5.

Each example is a complete, self-contained plugin:
  - One main plugin class (correct Paper API, Adventure messaging)
  - plugin.yml
  - Optional supporting classes
  - JUnit 5 + MockBukkit test class

Usage:
    # Keys are loaded automatically from .env in the project root.
    # Supported env vars (checked in order):
    #   OPENAI_API_KEY    → api.openai.com   + gpt-4o-mini
    #   DEEPSEEK_API_KEY  → api.deepseek.com + deepseek-chat  (recommended — cheap & reliable)
    #   KIMI_API_KEY      → api.moonshot.cn  + moonshot-v1-32k

    # Generate 500 examples (default)
    python scripts/generate_training_data.py

    # Smaller test run
    python scripts/generate_training_data.py --count 20

    # Custom count + auto-merge into train_v2 / val_v2
    python scripts/generate_training_data.py --count 300 --merge

    # Override provider manually
    python scripts/generate_training_data.py --api-key sk-... --base-url https://... --model gpt-4o

Run from the project root.
"""

import argparse
import json
import os
import pathlib
import random
import re
import sys
import time
from typing import Optional


def _load_dotenv(start: pathlib.Path = None) -> None:
    """
    Load key=value pairs from the nearest .env file into os.environ.
    Walks up from `start` (default: cwd) until it finds a .env or hits the
    filesystem root.  Works without python-dotenv installed.
    """
    root = pathlib.Path(start or os.getcwd()).resolve()
    for directory in [root, *root.parents]:
        env_file = directory / ".env"
        if env_file.exists():
            with env_file.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key   = key.strip()
                    value = value.strip().strip('"\'')
                    if key and key not in os.environ:   # don't overwrite real env
                        os.environ[key] = value
            break


# Load .env at import time so env vars are available even when the module is
# imported rather than run directly (e.g. the Colab notebook imports it).
_load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# Instruction bank — 140+ diverse plugin ideas covering all common types
# ──────────────────────────────────────────────────────────────────────────────
INSTRUCTION_BANK = [
    # ── Commands ──────────────────────────────────────────────────────────────
    "Create a Paper 1.21 plugin with a /fly command that toggles flight for the executing player. Ops bypass the permission check. Show a confirmation message using Adventure API.",
    "Write a Paper plugin with a /heal command that restores the player's health and hunger to full. Add a cooldown of 30 seconds stored in a HashMap. Use Adventure API for all messages.",
    "Make a Paper plugin that adds a /feed command to restore a player's saturation. Require the permission 'feedme.use'. Deny non-players with a descriptive message.",
    "Create a Paper plugin that implements a /speed command allowing players to set walk speed (1-10). Validate input range and show usage on bad args.",
    "Write a Paper 1.21 plugin for a /workbench command that opens a crafting table GUI for the player without needing a physical block.",
    "Build a Paper plugin with a /enderchest command that opens the player's own ender chest. Require permission 'enderchest.open'.",
    "Create a Paper plugin that adds a /back command saving the player's last location on teleport events and restoring it with /back.",
    "Make a Paper 1.21 plugin for a /skull command that gives the player a player head for a specified username. Use the modern skull meta API.",
    "Write a Paper plugin with a /top command that teleports the player to the highest block at their current X/Z position.",
    "Create a Paper plugin with a /time set command that accepts 'day', 'night', 'noon', 'midnight', and maps them to correct tick values.",
    "Build a Paper 1.21 plugin with a /sudo command (op-only) that forces another online player to run a command.",
    "Make a Paper plugin that implements /kick with a custom message. Broadcast the kick reason to ops. Require 'myplugin.kick' permission.",
    "Create a Paper plugin for /mute and /unmute commands. Store muted players in a Set. Block their chat in an AsyncPlayerChatEvent listener.",
    "Write a Paper 1.21 plugin with a /freeze command that prevents a player from moving (cancel PlayerMoveEvent if velocity is not zero).",
    "Build a Paper plugin with a /item rename command using Adventure API to set item display names with colour gradient support.",
    "Create a Paper 1.21 plugin for /tp with tab completion listing online player names using TabCompleter.",
    "Make a Paper plugin that adds /sethome and /home commands. Store homes per-player per-world in a YAML config file. Support multiple homes per player.",
    "Write a Paper plugin with a /warp and /setwarp command. Store warps in config.yml. List warps with /warps command. Require admin permission for /setwarp.",
    "Create a Paper 1.21 plugin that adds a /rtp (random teleport) command. Pick a random XZ within a configurable radius and find a safe surface Y.",
    "Build a Paper plugin with a /repair command that fixes the durability of the item in the player's hand. Add a money-cost concept via a config option (no Vault dependency).",
    "Write a Paper plugin with /powertool — binds a command to the item in hand, runs it on right-click. Store bindings in a HashMap per player UUID.",
    "Create a Paper 1.21 plugin for /broadcast that formats the message in a configurable prefix style using Adventure API MiniMessage.",
    "Make a Paper plugin that adds /seen to show when a player was last online. Store timestamps in a YAML flat-file database.",

    # ── Events ────────────────────────────────────────────────────────────────
    "Create a Paper 1.21 plugin that listens to PlayerDeathEvent and broadcasts a stylised death message using Adventure API with the victim's name highlighted.",
    "Write a Paper plugin that listens to BlockBreakEvent and cancels it in a 'protected zone' defined by two corner locations stored in config.yml.",
    "Make a Paper 1.21 plugin that listens to EntityDamageByEntityEvent. When a player kills another player, drop a skull of the victim.",
    "Build a Paper plugin that gives a new player a starter kit (sword, food, torches) on their first join, tracked via a flag in PersistentDataContainer.",
    "Create a Paper plugin that takes damage on fall (PlayerFallDamageEvent is Paper-specific — fall back to EntityDamageEvent with FALL cause).",
    "Write a Paper 1.21 plugin that cancels all PVP damage in worlds listed in config.yml.",
    "Make a Paper plugin that listens to InventoryClickEvent and prevents players from moving items out of the hotbar while a config flag is set.",
    "Create a Paper plugin that sends a private welcome message to a player when they join for the first time (use PersistentDataContainer to detect first join).",
    "Build a Paper 1.21 plugin that listens to PlayerInteractEvent. When a player right-clicks a sign, execute the command written on the first line.",
    "Write a Paper plugin that listens to BlockPlaceEvent. If the player places TNT without 'tnt.place' permission, cancel the event and send a warning.",
    "Make a Paper plugin that prevents players from breaking blocks placed by other players. Store placer UUID in PersistentDataContainer on BlockPlaceEvent.",
    "Create a Paper 1.21 plugin that listens to PlayerChatEvent (Paper async) and replaces profanity in a config list with asterisks.",
    "Build a Paper plugin for a custom enchantment effect: listen to EntityDamageByEntityEvent, if the attacker's sword has a PDC tag 'lightning_strike', summon a lightning bolt.",
    "Write a Paper plugin that listens to PlayerPickupItemEvent and cancels pickup for items of a Material listed in config.yml.",
    "Create a Paper 1.21 plugin that records the last 10 chat messages per player and logs them to a file when a PlayerQuitEvent fires.",
    "Make a Paper plugin that listens to EntitySpawnEvent and cancels spawning of mobs listed in config.yml in specific worlds.",
    "Build a Paper plugin for explosion protection: cancel BlockDamageByEntityEvent from creepers in worlds listed in config.yml.",
    "Write a Paper 1.21 plugin that listens to PlayerCommandPreprocessEvent and logs all commands to a rotating daily log file.",
    "Create a Paper plugin that listens to AsyncPlayerChatEvent and cancels it for muted players. Add /mute and /unmute commands.",
    "Make a Paper plugin that listens to PlayerBedEnterEvent and cancels it during a configurable 'no-sleep' time window.",

    # ── Schedulers ────────────────────────────────────────────────────────────
    "Build a Paper 1.21 plugin that broadcasts a random tip from a config list every 5 minutes using BukkitRunnable.",
    "Create a Paper plugin that ticks a countdown timer from 60 to 0, broadcast every 10 seconds, then runs /save-all when it hits zero.",
    "Write a Paper plugin that starts a repeating task on enable that restores health to all online players by 1hp every 30 seconds.",
    "Make a Paper 1.21 plugin for an AFK detector: track last move time per player. After 5 minutes of no movement, send them an AFK title.",
    "Build a Paper plugin that runs a daily reward at midnight server time: gives all online players 10 experience levels.",
    "Create a Paper plugin with a /reminder command that sets a personal repeating reminder message every N minutes for the calling player.",
    "Write a Paper 1.21 plugin for a grace-period system: after server start, run a 60-second countdown during which PVP is disabled.",
    "Make a Paper plugin that uses a scheduler to regenerate a specific arena region every 10 minutes by restoring a snapshot stored in a custom format.",
    "Build a Paper plugin for a particle effect aura: every 2 ticks, spawn configurable particles around each player with the 'aura.active' PDC flag set.",
    "Create a Paper 1.21 plugin that auto-saves player inventories to YAML every 5 minutes as a backup, using an async task for file writing.",

    # ── Config-driven ─────────────────────────────────────────────────────────
    "Create a Paper 1.21 plugin where all messages, cooldowns and the list of allowed worlds are fully defined in config.yml. Include a /reload command.",
    "Write a Paper plugin whose config.yml defines a list of 'join sound' effects. On player join, play a random sound from the list.",
    "Build a Paper plugin with a config-driven list of blocked items. Players without 'blockeditem.bypass' cannot hold those items in their hand.",
    "Make a Paper 1.21 plugin that reads a MOTD multi-line list from config.yml and sends it as a chat message to players on join.",
    "Create a Paper plugin where config.yml defines per-world time-lock values. On WorldLoadEvent, set each world's time to its configured value immediately.",
    "Write a Paper 1.21 plugin that reads a 'spawn' location from config.yml (as x, y, z, world, yaw, pitch) and has a /spawn command to teleport there.",

    # ── GUI / Inventories ──────────────────────────────────────────────────────
    "Build a Paper 1.21 plugin for a simple cosmetic selector GUI. Open a 3-row chest with 5 particle effects to choose from. Clicking grants the player that aura (PDC tag). Include InventoryHolder.",
    "Create a Paper plugin for a /kit GUI: show available kits in a chest inventory. Clicking a kit item gives the player that kit. Kit definitions are in config.yml.",
    "Write a Paper 1.21 plugin for a /trash can GUI. Opens a 3-row chest. Items placed inside are permanently deleted on close.",
    "Make a Paper plugin for a simple /shop GUI with 3 items buyable using XP levels. Clicking buys if the player has enough levels. Use Adventure API for item names.",
    "Build a Paper 1.21 plugin for a color picker GUI. 16 colored glass panes in a chest. Clicking one dyes the item in the player's hand that color.",
    "Create a Paper plugin for a confirmation GUI: when a player types /clearinventory, show a 1-row chest with a green confirm and red cancel button.",
    "Write a Paper 1.21 plugin that implements a paginated player-list GUI. Shows online players 45 at a time with next/prev navigation buttons.",
    "Make a Paper plugin for an admin spy inventory: ops can run /spy <player> to view that player's inventory in a read-only GUI.",

    # ── Data persistence ───────────────────────────────────────────────────────
    "Create a Paper 1.21 plugin that stores each player's play-time in a YAML file. On join, start counting. On quit, save to file. Add /playtime command.",
    "Write a Paper plugin that stores player scores as integers using PersistentDataContainer. /score add and /score get commands.",
    "Build a Paper 1.21 plugin that assigns each player a UUID-named YAML file storing their inventory snapshot. /saveinv and /loadinv commands.",
    "Make a Paper plugin that stores a 'nickname' per player in PersistentDataContainer of the Player entity and displays it in tab list.",
    "Create a Paper 1.21 plugin that persists ban records with reason and time in a flat YAML file with no third-party dependency.",
    "Write a Paper plugin using FileConfiguration to maintain a per-player coin balance. /coins give, /coins take, /coins balance commands.",

    # ── Complex multi-class ───────────────────────────────────────────────────
    "Build a complete Paper 1.21 plugin for a capture-the-flag mini-game: two teams, a flag block per team, score tracking, announcements, start/stop commands. Split into GameManager, Team, and main class.",
    "Create a Paper plugin for a border shrink system: configurable map border that shrinks 1 block per minute. Separate BorderManager class for logic.",
    "Write a Paper 1.21 plugin for a bounty system: /bounty set <player> <amount> places a XP-level bounty. Killing the target rewards the killer. BountyManager class for storage.",
    "Make a Paper plugin for auction-house functionality: /ah sell <price> lists item in hand. /ah browse opens GUI. /ah cancel removes listing. AuctionManager class.",
    "Build a Paper 1.21 plugin for a vote-kick system: /votekick <player> starts a 30-second vote. 70% of online players must agree. VoteSession class handles timer.",
    "Create a Paper plugin for a clans system: /clan create, /clan invite, /clan join, /clan leave. ClanManager class. Store in YAML. No friendly fire.",
    "Write a Paper 1.21 plugin for chest shops: right-click a chest to open a shop GUI. ShopData stored in PDC on the chest block entity. Admin command to edit.",
    "Make a Paper plugin for a region-based greeting: define cuboid regions in config. On entry (PlayerMoveEvent), send a greeting message for that region.",
    "Build a Paper 1.21 plugin for a levelling system: players earn XP for killing mobs. Level thresholds in config. Level-up gives a configurable reward. PlayerLevel class.",
    "Create a Paper plugin for a mail system: /mail send <player> <message> stores messages in YAML. /mail read shows unread messages in chat. /mail clear removes all.",

    # ── Specific API use ──────────────────────────────────────────────────────
    "Write a Paper 1.21 plugin that uses BossBar (via Adventure API) to show a 30-second countdown when a player enters a region defined in config.",
    "Make a Paper plugin that uses Title/Subtitle (Adventure API) to display a welcome message populated from config.yml when a player joins.",
    "Create a Paper 1.21 plugin that uses ActionBar (Adventure API) to show current health and hunger permanently above the hotbar, updating every 20 ticks.",
    "Build a Paper plugin that creates a custom named Entity (Villager) at a configurable location. On right-click, opens a pre-set chest inventory shop.",
    "Write a Paper 1.21 plugin that uses WorldBorder API to confine players to a configurable circular area. Warn players at 90% of the border via ActionBar.",
    "Make a Paper plugin that registers a custom recipe (ShapedRecipe) for a 'Super Pickaxe' with Efficiency X. Use NamespacedKey with the plugin instance.",
    "Create a Paper 1.21 plugin that uses Scoreboard API (via Adventure API SidebarComponent) to display top-3 kills on the right-hand sidebar.",
    "Build a Paper plugin that fires lightning at the block a player is looking at when they sneak-right-click with a blaze rod.",
    "Write a Paper 1.21 plugin that monitors chunk loading and logs a warning to console when a single player has caused more than 100 chunk loads in 10 seconds.",
    "Make a Paper plugin that uses persistent=false ArmorStand entities as floating holograms above locations defined in config.yml.",
    "Create a Paper 1.21 plugin that uses PlayerProfile API to fetch the skin texture of a username and apply it to a server skull item.",
    "Build a Paper plugin that hooks into Paper's async chat event to format chat with player prefix (from config), adventure gradients, and world name.",
    "Write a Paper 1.21 plugin for a particle trail: every 4 ticks, spawn configurable Particle type at each online player's feet if they have 'trail.active' PDC tag.",
    "Make a Paper plugin that disables hunger drain by cancelling FoodLevelChangeEvent in worlds listed in config.yml.",
    "Create a Paper 1.21 plugin that listens to PlayerInteractEvent on a specific named sign and runs different actions based on which line of the sign is clicked.",
    "Build a Paper plugin that adds a custom /setspawn command (persist location to config.yml) and teleports players to that spawn instead of the world spawn on respawn.",
    "Write a Paper 1.21 plugin for the /god mode command. While in god mode, all incoming damage is cancelled. Track state in a Set<UUID>.",
    "Make a Paper plugin that limits the number of entities of each type per chunk. On EntitySpawnEvent, count existing entities and cancel if over limit.",
    "Create a Paper 1.21 plugin with a /hat command that places the item in hand on the player's head as a helmet.",
    "Build a Paper plugin that detects if a player has been underground for more than 60 seconds (no sky access) and gives them Night Vision potion effect.",
    "Write a Paper 1.21 plugin that respects per-world flags in config: 'fire-spread', 'leaf-decay', 'mob-spawning'. Toggle each via BlockIgniteEvent, LeavesDecayEvent, EntitySpawnEvent listeners.",
    "Make a Paper plugin implementing the /near command — list all players within a configurable radius with their distance.",
    "Create a Paper 1.21 plugin that adds a /playbrowse command to let admins view any player's inventory without needing spectator mode.",
    "Build a Paper plugin that awards players achievement-style notifications (Title) when they reach configurable kill milestones (5, 10, 25, 50, 100).",
    "Write a Paper 1.21 plugin that implements a currency sign shop: right-click a sign formatted as [shop] / item name / quantity / price to buy with XP levels.",
    "Make a Paper plugin with /challenge: sends a duel request to another player. 30-second timeout. Both players must accept. Arena TP on accept.",
    "Create a Paper 1.21 plugin for a mineable ore streaks counter: on BlockBreakEvent for ores, track consecutive ore breaks in PDC. At 10, spawn bonus XP orbs.",
    "Build a Paper plugin for a random drop table: on EntityDeathEvent for zombies, 15% chance to drop a custom named item with Adventure API lore.",
    "Write a Paper 1.21 plugin that adds fireworks to player deaths for comedic effect: spawn a random-color firework at the death location.",
    "Make a Paper plugin that uses HoverEvent and ClickEvent in Adventure API to make a /helpme command where each help entry is clickable to run the command.",
    "Create a Paper 1.21 plugin for an auto-restart vote: /restart starts a vote. Requires 60% of online players. On success, broadcasts a 30-second countdown then runs restart script.",
]

# ──────────────────────────────────────────────────────────────────────────────
# System prompt sent to Kimi for EVERY generation
# Matches the inference SYSTEM_PROMPT closely to reduce distribution shift
# ──────────────────────────────────────────────────────────────────────────────
DATA_GEN_SYSTEM = """\
You are an expert Paper 1.21 Minecraft plugin developer generating training data.

You MUST output EXACTLY this structure, no more, no less:

1. A ```java code block containing the complete main plugin class.
   - Package: com.example.<pluginname> (all lowercase)
   - Full imports (no wildcards)
   - Extends JavaPlugin
   - All messages via Adventure API: player.sendMessage(Component.text(...))
   - NEVER use org.bukkit.ChatColor — removed in 1.21
   - NEVER use NMS or CraftBukkit internals
   - Register listeners in onEnable(): getServer().getPluginManager().registerEvents(new Listener(this), this)
   - Register commands in onEnable(): Objects.requireNonNull(getCommand("cmd")).setExecutor(new Handler(this))
   - If config is used: saveDefaultConfig() in onEnable() before any getConfig() calls

2. A ```yaml code block — complete plugin.yml:
   - name, version: '1.0', main (exact FQCN), api-version: '1.21'
   - Every command declared under 'commands:' with usage and description
   - Every permission under 'permissions:' with default: op

3. (If needed) Additional ```java blocks for supporting classes (listeners, managers, etc.)
   CRITICAL: Every class referenced in any import statement that you wrote yourself MUST
   appear as its own ```java block. Never import a class without generating it.
   Prefer private inner/nested classes over separate files to avoid missing-class errors.

4. A ```java code block for the JUnit 5 + MockBukkit test class:
   - Package matches main class
   - Class name ends in 'Test'
   - @BeforeEach setUp(): MockBukkit.mock(); MockBukkit.load(MainClass.class)
   - @AfterEach tearDown(): MockBukkit.unmock()
   - One @Test per command or event handler

Rules:
- Output COMPLETE code. Never truncate. Never write "// ...rest of implementation..."
- No TODO comments — write real logic
- Validate all command arguments, show usage on wrong input
- Use Objects.requireNonNull() for getCommand() results
- Async tasks that touch the Bukkit API must sync back with runTask()
- PersistentDataContainer for data that must survive restarts
- Name keys with new NamespacedKey(plugin, "key")
"""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def has_required_blocks(text: str) -> bool:
    """Check the response contains at least one java block and one yaml block."""
    return bool(re.search(r"```java", text)) and bool(re.search(r"```yaml", text))


def has_paper_imports(text: str) -> bool:
    """Check that Paper/Bukkit imports appear (not Minestom/Sponge/etc.)."""
    has_bukkit = bool(re.search(r"org\.bukkit\.|io\.papermc\.|net\.kyori\.adventure", text))
    has_bad = bool(re.search(r"net\.minestom\.|org\.spongepowered\.|org\.fabricmc\.", text))
    return has_bukkit and not has_bad


def call_api(client, model: str, instruction: str, max_retries: int = 3) -> Optional[str]:
    """Call the configured OpenAI-compatible API, return the response text or None."""
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": DATA_GEN_SYSTEM},
                    {"role": "user",   "content": instruction},
                ],
                temperature=0.4,
                max_tokens=6000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            wait = 2 ** attempt
            print(f"    [retry {attempt+1}/{max_retries}] Error: {e} — waiting {wait}s")
            time.sleep(wait)
    return None


def build_entry(instruction: str, response: str) -> dict:
    return {
        "system": DATA_GEN_SYSTEM.strip(),
        "instruction": instruction,
        "response": response,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

# ── Provider auto-detection ──────────────────────────────────────────────────
# Priority: explicit --api-key flag > env vars (checked in order below)
_PROVIDERS = [
    # (env_var,           default_base_url,                    default_model)
    ("OPENAI_API_KEY",    "https://api.openai.com/v1",         "gpt-4o-mini"),
    ("DEEPSEEK_API_KEY",  "https://api.deepseek.com",          "deepseek-chat"),
    ("KIMI_API_KEY",      "https://api.moonshot.cn/v1",        "moonshot-v1-32k"),
]

def _detect_provider():
    """Return (api_key, base_url, model) for the first available env var."""
    for env_var, base_url, model in _PROVIDERS:
        key = os.getenv(env_var)
        if key:
            return key, base_url, model
    return None, None, None


def main():
    parser = argparse.ArgumentParser(
        description="Generate Paper 1.21 plugin training data via any OpenAI-compatible API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Provider examples:
  DeepSeek (recommended): set DEEPSEEK_API_KEY  — auto-detected, ~$0.14/M tokens
  OpenAI (gpt-4o-mini):   set OPENAI_API_KEY    — auto-detected
  Moonshot/Kimi:          set KIMI_API_KEY       — auto-detected
  Custom endpoint:        --api-key sk-... --base-url https://... --model <name>
"""
    )
    parser.add_argument("--count",    type=int, default=500,
                        help="Number of examples to generate (default 500)")
    parser.add_argument("--output",   default="data/processed/synthetic_train.jsonl",
                        help="Output JSONL path")
    parser.add_argument("--merge",    action="store_true",
                        help="Merge with existing data/processed/train.jsonl into output")
    parser.add_argument("--delay",    type=float, default=1.2,
                        help="Seconds between API calls to avoid rate limits")
    parser.add_argument("--api-key",  default=None,
                        help="API key (overrides env vars)")
    parser.add_argument("--base-url", default=None,
                        help="Base URL for OpenAI-compatible API (overrides auto-detect)")
    parser.add_argument("--model",    default=None,
                        help="Model name (overrides auto-detect)")
    args = parser.parse_args()

    # Resolve credentials
    if args.api_key:
        api_key  = args.api_key
        base_url = args.base_url or "https://api.openai.com/v1"
        model    = args.model    or "gpt-4o-mini"
    else:
        api_key, base_url, model = _detect_provider()
        if args.base_url: base_url = args.base_url
        if args.model:    model    = args.model

    if not api_key:
        print("ERROR: No API key found.")
        print("Set one of: OPENAI_API_KEY, KIMI_API_KEY")
        print("Or pass:    --api-key sk-...  [--base-url URL]  [--model NAME]")
        sys.exit(1)

    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: openai package not installed. Run: pip install openai")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=base_url)

    out_path = pathlib.Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build instruction list — cycle through bank with shuffle, repeat if needed
    instructions = INSTRUCTION_BANK.copy()
    random.shuffle(instructions)
    while len(instructions) < args.count:
        extra = INSTRUCTION_BANK.copy()
        random.shuffle(extra)
        instructions.extend(extra)
    instructions = instructions[:args.count]

    ok = 0
    skip = 0
    results = []

    print(f"Generating {args.count} examples → {out_path}")
    print(f"Model:    {model}")
    print(f"Endpoint: {base_url}\n")

    for i, instr in enumerate(instructions, 1):
        print(f"[{i:>4}/{args.count}] {instr[:80]}...")
        response = call_api(client, model, instr)

        if response is None:
            print("         ✗ API failure — skipping")
            skip += 1
            continue

        if not has_required_blocks(response):
            print("         ✗ Missing java/yaml blocks — skipping")
            skip += 1
            continue

        if not has_paper_imports(response):
            print("         ✗ No Paper imports / bad framework detected — skipping")
            skip += 1
            continue

        results.append(build_entry(instr, response))
        ok += 1
        print(f"         ✓ OK  ({ok} good so far, {skip} skipped)")

        time.sleep(args.delay)

    # Write output
    with out_path.open("w", encoding="utf-8") as f:
        for entry in results:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\nWrote {ok} examples to {out_path}  ({skip} skipped)")

    # Merge if requested
    if args.merge:
        existing_path = pathlib.Path("data/processed/train.jsonl")
        if existing_path.exists():
            with existing_path.open(encoding="utf-8") as f:
                existing = [json.loads(l) for l in f if l.strip()]

            # Filter bad existing entries (no Paper imports in response)
            clean_existing = [e for e in existing if has_paper_imports(e.get("response",""))]
            print(f"Existing: {len(existing)} entries, {len(clean_existing)} passed quality filter")

            merged = clean_existing + results
            random.shuffle(merged)

            # 90/10 train/val split
            split = int(len(merged) * 0.9)
            train_out = pathlib.Path("data/processed/train_v2.jsonl")
            val_out   = pathlib.Path("data/processed/val_v2.jsonl")

            with train_out.open("w", encoding="utf-8") as f:
                for e in merged[:split]:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            with val_out.open("w", encoding="utf-8") as f:
                for e in merged[split:]:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")

            print(f"Merged → {train_out} ({split} examples), {val_out} ({len(merged)-split} examples)")
        else:
            print(f"--merge: {existing_path} not found, skipping merge")


if __name__ == "__main__":
    main()
