"""
StackNest Discord Bot
---------------------
Slash commands for the StackNest Minecraft plugin generator.

Setup:
  1. pip install -r discord_bot/requirements.txt
  2. Set DISCORD_BOT_TOKEN in .env (or as an env var)
  3. python discord_bot/bot.py

Required Bot Permissions (Discord Dev Portal):
  - Send Messages, Embed Links, Use Slash Commands
  - OAuth2 scope: bot + applications.commands
"""

import os, asyncio, aiohttp, time, json, re, random
from pathlib import Path

# Load .env from project root if present
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN    = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
API_BASE = os.environ.get("STACKNEST_API_URL", "https://stacknests.com").rstrip("/")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))

if not TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN is not set. Add it to .env or set the environment variable.")

ACCENT  = 0x5C6FFF
GREEN   = 0x3DDC84
RED     = 0xFF5C6A
YELLOW  = 0xFFD370
SITE    = "https://stacknests.com"

# ── Credit-alert config ──────────────────────────────────────────────────────
# When an AI model runs out of API credits / quota, the backend watchdog writes
# an alert to this file.  The bot polls it and DMs the owner.
OWNER_ID          = int(os.environ.get("DISCORD_OWNER_ID", "0"))
CREDIT_ALERT_FILE = "/tmp/stacknest_credit_alerts.json"

# ── Role-picker config ───────────────────────────────────────────────────────
ROLE_PICKER_CHANNEL = int(os.environ.get("DISCORD_ROLE_PICKER_CHANNEL", "0"))
ROLES = {
    "giveaway":      {"id": int(os.environ.get("DISCORD_ROLE_GIVEAWAY",       "0")),  "emoji": "🎁", "label": "Giveaways",     "desc": "Get pinged for giveaways"},
    "updates":       {"id": int(os.environ.get("DISCORD_ROLE_UPDATES",        "0")),  "emoji": "🔧", "label": "Updates",       "desc": "Plugin generator updates & changelogs"},
    "announcements": {"id": int(os.environ.get("DISCORD_ROLE_ANNOUNCEMENTS", "0")),  "emoji": "📢", "label": "Announcements", "desc": "Important server announcements"},
}

# ── Ticket config ───────────────────────────────────────────────────────────
CH_TICKETS         = int(os.environ.get("DISCORD_TICKETS_CHANNEL",  "0"))  # channel where the ticket panel embed lives
CH_TICKET_CATEGORY = int(os.environ.get("DISCORD_TICKET_CATEGORY",  "0"))  # category where ticket channels are created
ROLE_STAFF         = int(os.environ.get("DISCORD_STAFF_ROLE_ID",    "0"))  # staff role — full ticket access

# ── Ticket state file ───────────────────────────────────────────────────────
STATE_FILE = Path(__file__).resolve().parent / "state.json"

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_state(s: dict):
    # Atomic write: write to a temp file then rename so a crash mid-write
    # cannot leave a corrupted state.json.
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(STATE_FILE)

# ── Giveaway config ─────────────────────────────────────────────────────────
GIVEAWAY_CHANNEL        = int(os.environ.get("DISCORD_GIVEAWAY_CHANNEL", "0"))
GIVEAWAY_ROLE_ID        = ROLES["giveaway"]["id"]
DISCORD_LINKED_ROLE_ID  = int(os.environ.get("DISCORD_LINKED_ROLE_ID", "0"))  # assigned when user links StackNest account
GIVEAWAY_EMOJI          = "🎁"
GIVEAWAY_STATE_FILE     = "/opt/stacknest/data/giveaway_state.json"
GIVEAWAY_DURATION_HOURS = 48  # draw after 2 days
_PRIZES = [
    ("1 Free Generation", "One full plugin generation — no credits needed!"),
    ("3 Free Prompts",    "Three prompt credits for any plugin generation."),
]
_prize_index = 0

# ── Verification / welcome config ────────────────────────────────────────────
CH_RULES      = int(os.environ.get("DISCORD_RULES_CHANNEL",    "0"))
CH_WELCOME    = int(os.environ.get("DISCORD_WELCOME_CHANNEL",  "0"))
ROLE_VERIFIED = int(os.environ.get("DISCORD_VERIFIED_ROLE_ID", "0"))
RULES_EMOJI   = "✅"

# ── Bot setup ────────────────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True   # required for on_member_join (enable in Dev Portal → Bot → Privileged Intents)
bot     = commands.Bot(command_prefix="!", intents=intents)
tree    = bot.tree

# Tracks whether first-startup side-effects (posting embeds) have run.
# on_ready fires again on every reconnect; we only want to post once.
_startup_done: bool = False

# ── Health cache (avoid spamming the API on every /status call) ──────────────
_health_cache: dict = {}
_health_ts: float   = 0.0
CACHE_TTL = 30  # seconds

async def _fetch_health() -> dict:
    global _health_cache, _health_ts
    if time.time() - _health_ts < CACHE_TTL:
        return _health_cache
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_BASE}/api/health", timeout=aiohttp.ClientTimeout(total=6)) as r:
                _health_cache = await r.json()
                _health_ts    = time.time()
                return _health_cache
    except Exception as e:
        return {"api": "error", "error": str(e)}


# ── /status ───────────────────────────────────────────────────────────────────
@tree.command(name="status", description="Check if the StackNest API and AI backends are online.")
@app_commands.checks.cooldown(1, 15, key=lambda i: i.user.id)  # 1 use per 15 s per user
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer()
    d = await _fetch_health()

    api_ok  = d.get("api") == "ok"
    inf_ok  = d.get("inference_server") == "ok"
    free_ai = d.get("free_ai", "no_key")
    prem_ai = d.get("premium_ai", "no_key")
    has_ai  = (free_ai not in ("no_key", "unknown", "")) or (prem_ai not in ("no_key", "unknown", ""))

    if not api_ok:
        color = RED
        title = "StackNest — Offline"
        status_line = "❌ API unreachable"
    elif inf_ok:
        color = GREEN
        title = "StackNest — Fully Online"
        status_line = "✅ Local inference + Cloud AI"
    elif has_ai:
        color = YELLOW
        title = "StackNest — Online (Cloud AI)"
        status_line = "🟡 Cloud AI only (local inference offline)"
    else:
        color = RED
        title = "StackNest — Degraded"
        status_line = "⚠️ API up but no AI backend available"

    free_label = free_ai if free_ai not in ("no_key", "unknown", "") else "—"
    prem_label = prem_ai if prem_ai not in ("no_key", "unknown", "") else "—"
    inf_label  = "✅ Online" if inf_ok else "❌ Offline"

    embed = discord.Embed(title=title, description=status_line, color=color)
    embed.add_field(name="Local Inference",  value=inf_label,  inline=True)
    embed.add_field(name="Gemini (Free AI)", value=free_label, inline=True)
    embed.add_field(name="Claude (Pro AI)",  value=prem_label, inline=True)
    embed.set_footer(text="stacknests.com", icon_url=f"{SITE}/icon.svg")
    await interaction.followup.send(embed=embed)


# ── /generate ─────────────────────────────────────────────────────────────────
@tree.command(name="generate", description="Start generating a Minecraft plugin on StackNest.")
@app_commands.describe(description="Brief description of your plugin (optional — opens in browser)")
async def cmd_generate(interaction: discord.Interaction, description: str = ""):
    lines = [
        f"**[Open StackNest Generator]({SITE}/app)**",
        "",
        "Describe your plugin in plain English and get working **Java code**, `plugin.yml`, and a compiled `.jar` in seconds.",
        "",
        "**Supported platforms:** Paper 1.18–1.21 · Folia · Spigot · Purpur · Velocity · BungeeCord",
    ]
    if description:
        lines.insert(0, f"> *\"{description}\"*\n")

    embed = discord.Embed(
        title="StackNest — AI Plugin Generator",
        description="\n".join(lines),
        color=ACCENT,
        url=f"{SITE}/app",
    )
    embed.add_field(name="Free tier",  value="3 plugins/month", inline=True)
    embed.add_field(name="Pro tier",   value="100 plugins/month", inline=True)
    embed.add_field(name="No signup?", value="Sign up free → [stacknests.com](" + SITE + ")", inline=False)
    embed.set_footer(text="stacknests.com · StackNest")
    await interaction.response.send_message(embed=embed)


# ── /pricing ──────────────────────────────────────────────────────────────────
@tree.command(name="pricing", description="View StackNest plans and pricing.")
async def cmd_pricing(interaction: discord.Interaction):
    embed = discord.Embed(
        title="StackNest Pricing",
        description=f"**[View full pricing →]({SITE}/pricing)**",
        color=ACCENT,
    )
    embed.add_field(
        name="Free",
        value="• 3 plugins/month\n• Paper 1.21 + Folia\n• Download .jar\n• No credit card needed",
        inline=True,
    )
    embed.add_field(
        name="Pro — $9.99/mo",
        value="• 100 plugins/month\n• Priority AI (Claude)\n• All platforms\n• Project history",
        inline=True,
    )
    embed.set_footer(text="stacknests.com · StackNest")
    await interaction.response.send_message(embed=embed)


# ── /help ─────────────────────────────────────────────────────────────────────
@tree.command(name="help", description="What can StackNest do?")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="StackNest — AI Minecraft Plugin Generator",
        description=(
            "StackNest turns plain English into **working, compiled Minecraft plugins**.\n"
            "No Java knowledge required — describe what you want and get a `.jar` + source code."
        ),
        color=ACCENT,
        url=SITE,
    )
    embed.add_field(
        name="Bot Commands",
        value=(
            "`/status` — API & AI backend health\n"
            "`/generate` — Open the plugin generator\n"
            "`/pricing` — View plans\n"
            "`/help` — This message\n"
            "`/about` — About StackNest"
        ),
        inline=False,
    )
    embed.add_field(
        name="Website",
        value=f"[stacknests.com]({SITE})",
        inline=True,
    )
    embed.add_field(
        name="Generator",
        value=f"[Open App]({SITE}/app)",
        inline=True,
    )
    embed.set_footer(text="stacknests.com · StackNest")
    await interaction.response.send_message(embed=embed)


# ── /about ────────────────────────────────────────────────────────────────────
@tree.command(name="about", description="About StackNest.")
async def cmd_about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="About StackNest",
        description=(
            "StackNest is an **AI-powered Minecraft plugin generator** built by Arti & Ethan.\n\n"
            "Describe your plugin idea → get Paper/Spigot/Folia-compatible Java code, "
            "`plugin.yml`, `pom.xml`, and a compiled `.jar` — all in seconds.\n\n"
            f"**[Get started free at stacknests.com]({SITE})**"
        ),
        color=ACCENT,
        url=SITE,
    )
    embed.set_footer(text="stacknests.com · StackNest")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
#  TICKET SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

def _ticket_channel_name(user: discord.Member) -> str:
    safe = re.sub(r"[^a-z0-9]", "-", user.name.lower())[:20].strip("-")
    return f"ticket-{safe}-{user.id % 10000}"


async def _create_ticket_channel(
    guild: discord.Guild,
    user: discord.Member,
) -> discord.TextChannel:
    """Create a private text channel visible only to the opener and staff."""
    category   = guild.get_channel(CH_TICKET_CATEGORY)
    staff_role = guild.get_role(ROLE_STAFF)
    everyone   = guild.default_role
    bot_member = guild.get_member(bot.user.id)

    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True, embed_links=True,
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True,
            manage_messages=True, read_message_history=True,
        ),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True, manage_channels=True, manage_messages=True,
        )

    return await guild.create_text_channel(
        name=_ticket_channel_name(user),
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=overwrites,
        reason=f"Support ticket for {user}",
    )


class TicketCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🔒  Close Ticket",
            style=discord.ButtonStyle.danger,
            custom_id="ticket:close",
        )

    async def callback(self, interaction: discord.Interaction):
        channel      = interaction.channel
        state        = _load_state()
        open_tickets = state.get("open_tickets", {})
        owner_id     = next(
            (int(uid) for uid, cid in open_tickets.items() if cid == channel.id),
            None,
        )
        is_staff = any(r.id == ROLE_STAFF for r in getattr(interaction.user, "roles", []))
        is_owner = interaction.user.id == owner_id
        if not is_staff and not is_owner:
            await interaction.response.send_message(
                "Only the ticket owner or staff can close this.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                description="✅ Ticket closed. This channel will be deleted in 5 seconds.",
                color=GREEN,
            )
        )
        state["open_tickets"] = {k: v for k, v in open_tickets.items() if v != channel.id}
        _save_state(state)
        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Ticket closed")
        except Exception:
            pass


class TicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="📩  Create Ticket",
            style=discord.ButtonStyle.primary,
            custom_id="ticket:open",
        )

    async def callback(self, interaction: discord.Interaction):
        guild        = interaction.guild
        user         = interaction.user
        state        = _load_state()
        open_tickets = state.get("open_tickets", {})

        if str(user.id) in open_tickets:
            cid = open_tickets[str(user.id)]
            existing = guild.get_channel(cid)
            if existing:
                await interaction.response.send_message(
                    f"You already have an open ticket: {existing.mention}\n"
                    "Please use that channel or close it first.",
                    ephemeral=True,
                )
                return
            del open_tickets[str(user.id)]

        await interaction.response.defer(ephemeral=True)

        try:
            ticket_ch = await _create_ticket_channel(guild, user)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Bot is missing **Manage Channels** permission.", ephemeral=True
            )
            return

        close_view = discord.ui.View(timeout=None)
        close_view.add_item(TicketCloseButton())
        embed = discord.Embed(
            title="🎫  Support Ticket",
            description=(
                f"Hello {user.mention}! A staff member will be with you shortly.\n\n"
                "**Please describe your issue:**\n"
                "• What were you trying to do?\n"
                "• What happened instead?\n"
                "• Any error messages or screenshots?\n\n"
                "Click **Close Ticket** when your issue is resolved."
            ),
            color=ACCENT,
        )
        embed.set_footer(text="StackNest Support · Do not share passwords or payment info")
        await ticket_ch.send(content=user.mention, embed=embed, view=close_view)

        open_tickets[str(user.id)] = ticket_ch.id
        state["open_tickets"] = open_tickets
        _save_state(state)
        await interaction.followup.send(f"Ticket created: {ticket_ch.mention}", ephemeral=True)
        print(f"[Bot] Ticket {ticket_ch.id} opened for {user}")


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketButton())


def _build_ticket_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎫  StackNest Support",
        description=(
            "Need help? Click the button below to open a private support ticket.\n\n"
            "**We can help with:**\n"
            "• Plugin generation issues or errors\n"
            "• Account & billing questions\n"
            "• Bug reports\n"
            "• Feature requests\n"
            "• Anything else StackNest-related\n\n"
            "A staff member will respond as soon as possible.\n\n"
            f"📖 Check the **[docs]({SITE}/docs)** first — many answers are there!"
        ),
        color=ACCENT,
    )
    embed.set_footer(text="StackNest Support · One ticket per issue please")
    return embed


async def _ensure_ticket_panel(channel: discord.TextChannel):
    state  = _load_state()
    msg_id = state.get("ticket_panel_msg_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=_build_ticket_embed(), view=TicketPanelView())
            print("[Bot] Ticket panel refreshed.")
            return
        except discord.NotFound:
            pass
    msg = await channel.send(embed=_build_ticket_embed(), view=TicketPanelView())
    state["ticket_panel_msg_id"] = msg.id
    _save_state(state)
    print(f"[Bot] Ticket panel posted (id={msg.id}).")


@tree.command(name="ticket_add", description="Add a member to your open ticket (owner or staff only).")
@app_commands.describe(member="The member to invite into this ticket channel.")
async def cmd_ticket_add(interaction: discord.Interaction, member: discord.Member):
    channel      = interaction.channel
    state        = _load_state()
    open_tickets = state.get("open_tickets", {})
    if channel.id not in set(open_tickets.values()):
        await interaction.response.send_message(
            "This command can only be used inside a ticket channel.", ephemeral=True
        )
        return
    owner_id = next(
        (int(uid) for uid, cid in open_tickets.items() if cid == channel.id), None
    )
    is_staff = any(r.id == ROLE_STAFF for r in getattr(interaction.user, "roles", []))
    is_owner = interaction.user.id == owner_id
    if not is_staff and not is_owner:
        await interaction.response.send_message(
            "Only the ticket owner or staff can add members.", ephemeral=True
        )
        return
    try:
        await channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True,
        )
        await interaction.response.send_message(
            f"✅ {member.mention} has been added to this ticket."
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Bot lacks **Manage Channels** permission.", ephemeral=True
        )


# ── Role-picker persistent view ──────────────────────────────────────────────
class RoleButton(discord.ui.Button):
    def __init__(self, key: str):
        role_cfg = ROLES[key]
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=role_cfg["label"],
            emoji=role_cfg["emoji"],
            custom_id=f"role_toggle_{key}",
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        role_id  = ROLES[self.key]["id"]
        label    = ROLES[self.key]["label"]
        member   = interaction.user
        role     = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message(
                f"⚠️ Role not found. Please contact a moderator.", ephemeral=True
            )
            return
        if role in member.roles:
            await member.remove_roles(role, reason="Role picker — self-removed")
            await interaction.response.send_message(
                f"✅ Removed **{label}** — you won't receive these notifications.", ephemeral=True
            )
        else:
            await member.add_roles(role, reason="Role picker — self-assigned")
            await interaction.response.send_message(
                f"✅ Added **{label}** — you'll now receive these notifications!", ephemeral=True
            )


class RolePickerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent — survives bot restarts
        for key in ROLES:
            self.add_item(RoleButton(key))


def _build_role_picker_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔔 Notification Roles",
        description=(
            "Click a button below to **add or remove** a notification role.\n"
            "Click again to toggle it off.\n\u200b"
        ),
        color=ACCENT,
    )
    for cfg in ROLES.values():
        embed.add_field(
            name=f"{cfg['emoji']} {cfg['label']}",
            value=cfg["desc"],
            inline=True,
        )
    embed.set_footer(text="stacknests.com · Roles update instantly")
    return embed


@tree.command(name="setup_roles", description="[Admin] Post the notification role picker embed.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_setup_roles(interaction: discord.Interaction):
    channel = interaction.guild.get_channel(ROLE_PICKER_CHANNEL)
    if not channel:
        await interaction.response.send_message("❌ Role picker channel not found.", ephemeral=True)
        return
    await channel.send(embed=_build_role_picker_embed(), view=RolePickerView())
    await interaction.response.send_message(f"✅ Role picker posted in {channel.mention}!", ephemeral=True)


@tree.command(name="setup_tickets", description="[Admin] Post or refresh the support ticket panel.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_setup_tickets(interaction: discord.Interaction):
    channel = interaction.guild.get_channel(CH_TICKETS)
    if not channel:
        await interaction.response.send_message("❌ Ticket channel not found.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await _ensure_ticket_panel(channel)
    await interaction.followup.send(f"✅ Ticket panel refreshed in {channel.mention}!", ephemeral=True)


# ── Credit alert polling ───────────────────────────────────────────────────────────
@tasks.loop(minutes=2)
async def check_credit_alerts():
    """
    Poll /tmp/stacknest_credit_alerts.json for unnotified credit-exhaustion events.
    DMs OWNER_ID when a model needs topping up.
    """
    try:
        from pathlib import Path as _Path
        alert_path = _Path(CREDIT_ALERT_FILE)
        if not alert_path.exists():
            return

        alerts: dict = json.loads(alert_path.read_text(encoding="utf-8"))
        updated = False

        for model, info in alerts.items():
            if info.get("notified", True):
                continue  # already notified

            label = info.get("label", model)
            try:
                owner = await bot.fetch_user(OWNER_ID)
                await owner.send(
                    f"⚠️ **StackNest credit alert** — "
                    f"**{label}** has run out of API credits / quota.\n"
                    f"Please top up the key so generation can continue."
                )
                print(f"[Bot] Sent credit-alert DM to owner for {model}.")
            except Exception as dm_err:
                print(f"[Bot] Failed to DM owner for {model}: {dm_err}")
                continue  # don't mark notified if DM failed

            alerts[model]["notified"] = True
            updated = True

        if updated:
            alert_path.write_text(json.dumps(alerts), encoding="utf-8")

    except Exception as e:
        print(f"[Bot] check_credit_alerts error: {e}")

# ── Giveaway helpers & tasks ────────────────────────────────────────────────
from pathlib import Path as _GPath
import random as _random

def _load_giveaway():
    try:
        p = _GPath(GIVEAWAY_STATE_FILE)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return None

def _save_giveaway(data):
    try:
        p = _GPath(GIVEAWAY_STATE_FILE)
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(json.dumps(data))
    except Exception:
        pass

async def _post_giveaway():
    global _prize_index
    # Don't post if there's already an active giveaway
    existing = _load_giveaway()
    if existing and time.time() < existing["end_ts"]:
        print(f"[Bot] Giveaway already active, skipping post.", flush=True)
        return
    if not GIVEAWAY_CHANNEL:
        print("[Bot] DISCORD_GIVEAWAY_CHANNEL not set — skipping giveaway", flush=True)
        return
    prize_name, prize_desc = _PRIZES[_prize_index % len(_PRIZES)]
    _prize_index += 1
    end_ts = int(time.time()) + GIVEAWAY_DURATION_HOURS * 3600
    embed = discord.Embed(
        title=f"🎁 Weekly Giveaway — {prize_name}",
        description=(
            f"**Prize:** {prize_desc}\n\n"
            f"React with {GIVEAWAY_EMOJI} to enter!\n"
            f"Winner drawn <t:{end_ts}:R> • <t:{end_ts}:F>\n\n"
            f"⚠️ **You must have linked your StackNest account to be eligible.**\n"
            f"Link at {SITE}/profile → Discord tab."
        ),
        color=0xFFD370,
    )
    embed.set_footer(text="StackNest Giveaway • 1 winner drawn automatically • Good luck!")
    try:
        channel = await bot.fetch_channel(GIVEAWAY_CHANNEL)
        role_mention = f"<@&{GIVEAWAY_ROLE_ID}>" if GIVEAWAY_ROLE_ID else ""
        msg = await channel.send(content=role_mention, embed=embed)
        await msg.add_reaction(GIVEAWAY_EMOJI)
        _save_giveaway({
            "message_id": msg.id,
            "channel_id": GIVEAWAY_CHANNEL,
            "prize_name": prize_name,
            "end_ts": end_ts,
        })
        print(f"[Bot] Giveaway posted: {prize_name}, ends {end_ts}", flush=True)
    except Exception as e:
        print(f"[Bot] Failed to post giveaway: {e}", flush=True)

@tasks.loop(hours=168)  # every 7 days
async def weekly_giveaway():
    await _post_giveaway()

async def _execute_giveaway_draw(state: dict):
    """Draw the winner for a finished giveaway. Safe to call from any context.
    Marks state['drawn'] = True before touching Discord so a crash/restart
    cannot double-draw. Clears state file only after a successful announcement.
    Returns True on success, False on error."""
    # Mark drawn immediately so retries / restarts don't double-draw
    state["drawn"] = True
    _save_giveaway(state)
    try:
        channel = await bot.fetch_channel(state["channel_id"])
        msg     = await channel.fetch_message(state["message_id"])

        guild        = bot.get_guild(GUILD_ID)
        raw_entrants = []
        for reaction in msg.reactions:
            if str(reaction.emoji) == GIVEAWAY_EMOJI:
                async for user in reaction.users():
                    if not user.bot:
                        raw_entrants.append(user)
                break

        entrants = []
        for user in raw_entrants:
            try:
                member = guild.get_member(user.id) or await guild.fetch_member(user.id)
                if any(r.id == DISCORD_LINKED_ROLE_ID for r in member.roles):
                    entrants.append(user)
            except Exception:
                pass

        if not entrants:
            await channel.send(
                "🎁 Giveaway ended — no eligible entries this week.\n"
                "To enter future giveaways, link your StackNest account at "
                f"{SITE}/profile (Discord tab) to get the **Linked** role."
            )
        else:
            winner = _random.choice(entrants)
            win_embed = discord.Embed(
                title="🎉 Giveaway Winner!",
                description=(
                    f"Congratulations {winner.mention}! 🎊\n"
                    f"You won **{state['prize_name']}** on StackNest!\n\n"
                    f"DM <@{OWNER_ID}> or open a support ticket at {SITE}/support to claim your prize."
                ),
                color=GREEN,
            )
            win_embed.set_footer(text="StackNest Giveaway")
            await channel.send(embed=win_embed)
            print(f"[Bot] Giveaway winner: {winner} won {state['prize_name']}", flush=True)

        _save_giveaway(None)  # clean up only after successful announcement
        return True
    except Exception as e:
        print(f"[Bot] _execute_giveaway_draw error: {e}", flush=True)
        # Leave state file with drawn=True so the task doesn't retry automatically.
        # Owner can use /giveaway-draw to force a re-attempt.
        return False


@tasks.loop(minutes=10)
async def check_giveaway_end():
    state = _load_giveaway()
    if not state or time.time() < state["end_ts"]:
        return
    if state.get("drawn"):
        return  # already drawn (or draw in progress), skip
    await _execute_giveaway_draw(state)


# ── /giveaway-draw  (owner-only manual trigger) ───────────────────────────
@tree.command(name="giveaway-draw", description="[Owner] Manually draw the current giveaway winner.")
async def cmd_giveaway_draw(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    state = _load_giveaway()
    if not state:
        await interaction.followup.send("No active giveaway state found.", ephemeral=True)
        return
    # Allow re-draw even if drawn=True (owner override)
    state.pop("drawn", None)
    ok = await _execute_giveaway_draw(state)
    if ok:
        await interaction.followup.send("✅ Giveaway drawn successfully.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Draw failed — check bot logs.", ephemeral=True)

# ── Periodic status update in bot presence ────────────────────────────────────
@tasks.loop(minutes=5)
async def update_presence():
    try:
        d = await _fetch_health()
        inf_ok  = d.get("inference_server") == "ok"
        free_ai = d.get("free_ai", "no_key")
        has_ai  = free_ai not in ("no_key", "unknown", "")
        if inf_ok:
            activity = discord.Activity(type=discord.ActivityType.playing, name="plugins | /help")
        elif has_ai:
            activity = discord.Activity(type=discord.ActivityType.watching, name="Cloud AI | /status")
        else:
            activity = discord.Activity(type=discord.ActivityType.watching, name="stacknests.com")
        await bot.change_presence(activity=activity)
    except Exception:
        pass


# ── Server rules embed ───────────────────────────────────────────────────────

_RULES_TEXT = """
**Welcome to the StackNest Discord!**
Please read and follow all rules to keep this a great community.

React with ✅ below to gain access to the server.

──────────────────────────────────────────────

**1 · Be respectful**
Treat everyone with kindness. No harassment, hate speech, discrimination, or personal attacks of any kind.

**2 · No spam or self-promotion**
Don't flood channels with messages, repeated content, or unsolicited advertisements. Sharing your plugins in #showcase is fine.

**3 · Keep topics on-point**
Post in the correct channels. Minecraft plugin talk, StackNest feedback, and support questions each have their own space.

**4 · No NSFW or harmful content**
Absolutely no adult content, graphic violence, or illegal material.

**5 · No cheating or abuse**
Don't attempt to exploit the StackNest platform, bots, or bypass rate limits. Accounts found abusing the service will be banned.

**6 · English in main channels**
Use English in general channels so staff can moderate effectively. Other languages are welcome in DMs or dedicated threads.

**7 · Use the ticket system for support**
If you need help with your account, a generation, or have a billing issue — open a ticket. Don't DM staff directly.

**8 · Respect staff decisions**
Moderator decisions are final. If you disagree, open a ticket — don't argue publicly.
""".strip()


def _build_rules_embed() -> discord.Embed:
    import datetime
    month_year = datetime.datetime.utcnow().strftime("%B %Y")
    embed = discord.Embed(title="📋  Server Rules", description=_RULES_TEXT, color=GREEN)
    embed.set_footer(text=f"StackNest · React ✅ to get verified · Last updated {month_year}")
    return embed


async def _ensure_rules_embed(channel: discord.TextChannel):
    """Post (or refresh) the rules embed and store msg_id in state."""
    state  = _load_state()
    msg_id = state.get("rules_msg_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=_build_rules_embed())
            # Re-add reaction if it was cleared
            me_reacted = any(str(r.emoji) == RULES_EMOJI and r.me for r in msg.reactions)
            if not me_reacted:
                await msg.add_reaction(RULES_EMOJI)
            print("[Bot] Rules embed refreshed.")
            return
        except discord.NotFound:
            pass
    msg = await channel.send(embed=_build_rules_embed())
    await msg.add_reaction(RULES_EMOJI)
    state["rules_msg_id"] = msg.id
    _save_state(state)
    print(f"[Bot] Rules embed posted (id={msg.id}).")


@tree.command(name="setup_rules", description="[Admin] Post or refresh the rules embed.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_setup_rules(interaction: discord.Interaction):
    channel = interaction.guild.get_channel(CH_RULES)
    if not channel:
        await interaction.response.send_message("❌ Rules channel not found (check CH_RULES config).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await _ensure_rules_embed(channel)
    await interaction.followup.send(f"✅ Rules embed refreshed in {channel.mention}!", ephemeral=True)


# ── Reaction-based verification ───────────────────────────────────────────────

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if payload.channel_id != CH_RULES:
        return
    state = _load_state()
    if payload.message_id != state.get("rules_msg_id"):
        return
    if str(payload.emoji) != RULES_EMOJI:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    role = guild.get_role(ROLE_VERIFIED)
    if not role:
        print(f"[Bot] ROLE_VERIFIED {ROLE_VERIFIED} not found!", flush=True)
        return
    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        if role not in member.roles:
            await member.add_roles(role, reason="Accepted server rules")
            print(f"[Bot] Verified {member} — gave role '{role.name}'")
    except Exception as e:
        print(f"[Bot] Failed to verify {payload.user_id}: {e}", flush=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.channel_id != CH_RULES:
        return
    state = _load_state()
    if payload.message_id != state.get("rules_msg_id"):
        return
    if str(payload.emoji) != RULES_EMOJI:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    role = guild.get_role(ROLE_VERIFIED)
    if not role:
        return
    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        if role in member.roles:
            await member.remove_roles(role, reason="Removed rules reaction")
            print(f"[Bot] Unverified {member} — removed role '{role.name}'")
    except Exception as e:
        print(f"[Bot] Failed to unverify {payload.user_id}: {e}", flush=True)


# ── Welcome message on member join ────────────────────────────────────────────

@bot.event
async def on_member_join(member: discord.Member):
    channel = member.guild.get_channel(CH_WELCOME)
    if not channel:
        return
    member_count = member.guild.member_count
    embed = discord.Embed(
        title=f"👋  Welcome to StackNest, {member.display_name}!",
        description=(
            f"Hey {member.mention}, we're glad you're here! 🎉\n\n"
            "**Getting started:**\n"
            f"📋 Head to <#{CH_RULES}> and react with ✅ to unlock the server\n"
            f"🦖 Generate Minecraft plugins instantly at {SITE}\n"
            "🎁 Pick notification roles to stay in the loop on giveaways & updates\n"
            "🎫 Need help? Open a support ticket anytime\n\n"
            "Enjoy your stay!"
        ),
        color=ACCENT,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Member #{member_count} · stacknests.com")
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[Bot] Failed to send welcome for {member}: {e}", flush=True)


# ── Error handlers ────────────────────────────────────────────────────────────
@bot.event
async def on_error(event: str, *args, **kwargs):
    import traceback
    print(f"[Bot] Unhandled exception in event '{event}':", flush=True)
    traceback.print_exc()


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Slow down — try again in {error.retry_after:.0f}s.",
            ephemeral=True,
        )
        return
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You don't have permission to use that command.",
            ephemeral=True,
        )
        return
    # Generic fallback — log and reply
    import traceback
    print(f"[Bot] Command error in '{interaction.command.name}':", flush=True)
    traceback.print_exc()
    msg = f"❌ Something went wrong. Please try again later."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global _startup_done
    print(f"[StackNest Bot] {'Reconnected' if _startup_done else 'Logged in'} as {bot.user} ({bot.user.id})")

    # Always re-register persistent views — Discord.py requires this on every
    # process start so button callbacks survive restarts.
    bot.add_view(RolePickerView())
    bot.add_view(TicketPanelView())
    _close_view = discord.ui.View(timeout=None)
    _close_view.add_item(TicketCloseButton())
    bot.add_view(_close_view)

    # ── One-time startup work (do NOT repeat on reconnect) ─────────────────
    if not _startup_done:
        _startup_done = True

        try:
            guild = discord.Object(id=GUILD_ID) if GUILD_ID else None
            if guild:
                tree.copy_global_to(guild=guild)
                synced = await tree.sync(guild=guild)
                print(f"[StackNest Bot] Synced {len(synced)} command(s) to guild {GUILD_ID}")
            else:
                synced = await tree.sync()
                print(f"[StackNest Bot] Synced {len(synced)} command(s) globally")
        except Exception as e:
            print(f"[StackNest Bot] Sync error: {e}")

        # Post role picker embed only on first start
        try:
            roles_channel = await bot.fetch_channel(ROLE_PICKER_CHANNEL)
            await roles_channel.send(embed=_build_role_picker_embed(), view=RolePickerView())
        except Exception as e:
            print(f"[StackNest Bot] Could not post role picker embed: {e}", flush=True)

        # Refresh ticket panel (edits existing if tracked, otherwise posts new)
        try:
            ticket_channel = await bot.fetch_channel(CH_TICKETS)
            await _ensure_ticket_panel(ticket_channel)
        except Exception as e:
            print(f"[StackNest Bot] Could not refresh ticket panel: {e}", flush=True)

        # Start background tasks (guard against double-start on reconnect)
        if not update_presence.is_running():    update_presence.start()
        if not check_credit_alerts.is_running(): check_credit_alerts.start()
        if not weekly_giveaway.is_running():     weekly_giveaway.start()
        if not check_giveaway_end.is_running():  check_giveaway_end.start()

        # If the bot restarted after a giveaway's end time, draw immediately.
        _g = _load_giveaway()
        if _g and time.time() >= _g["end_ts"] and not _g.get("drawn"):
            print("[Bot] Giveaway expired while bot was offline — drawing now", flush=True)
            asyncio.ensure_future(_execute_giveaway_draw(_g))


if __name__ == "__main__":
    bot.run(TOKEN)
