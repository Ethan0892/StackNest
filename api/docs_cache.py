"""
api/docs_cache.py — Fetches and caches PaperMC / Adventure API documentation pages.

Provides stripped plaintext excerpts from docs.papermc.io that get injected into
generation prompts, giving the AI model up-to-date API reference without relying
solely on pre-training knowledge.

Usage:
    from api.docs_cache import get_doc_context
    snippet = get_doc_context("send message to player chat component")
"""

import re
import threading
import time
import urllib.request
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CACHE_TTL = 24 * 3600  # 24 hours — docs don't change minute-to-minute
_FETCH_TIMEOUT = 10      # seconds per page fetch
_MAX_SNIPPET_CHARS = 800 # chars to include per matched page in the prompt

# Curated doc pages: (url, section_tag, context_keywords)
# section_tag: id of the <main> or article to extract (or None for whole body)
# context_keywords: used to decide when to inject this page's content
_DOC_PAGES: list[dict] = [
    {
        "url": "https://docs.papermc.io/paper/dev/getting-started",
        "keywords": ["plugin", "javaPlugin", "onEnable", "onDisable", "getting started", "paper plugin"],
    },
    {
        "url": "https://docs.papermc.io/paper/dev/commands",
        "keywords": ["command", "executor", "CommandSender", "setExecutor", "tab complet", "/"],
    },
    {
        "url": "https://docs.papermc.io/paper/dev/event-api",
        "keywords": ["event", "listener", "EventHandler", "registerEvents", "PlayerJoin", "PlayerDeath"],
    },
    {
        "url": "https://docs.papermc.io/paper/dev/scheduler",
        "keywords": ["scheduler", "runnable", "BukkitRunnable", "repeating", "delay", "timer", "task"],
    },
    {
        "url": "https://docs.papermc.io/paper/dev/misc/pdc",
        "keywords": ["pdc", "PersistentDataContainer", "metadata", "data", "store", "persistent"],
    },
    {
        "url": "https://docs.papermc.io/adventure/text",
        "keywords": ["component", "Component.text", "message", "chat", "adventure", "text", "sendMessage"],
    },
    {
        "url": "https://docs.papermc.io/adventure/minimessage/format",
        "keywords": ["minimessage", "MiniMessage", "color", "gradient", "format", "tag", "<red>"],
    },
    {
        "url": "https://docs.papermc.io/adventure/text-color",
        "keywords": ["NamedTextColor", "TextColor", "color", "RED", "GREEN", "BLUE", "text color"],
    },
    {
        "url": "https://docs.papermc.io/paper/dev/entity-api/entities",
        "keywords": ["entity", "player", "Entity", "Living", "mob", "getNearbyEntities"],
    },
    {
        "url": "https://docs.papermc.io/paper/dev/plugin-yml",
        "keywords": ["plugin.yml", "main:", "api-version:", "commands:", "permissions:", "depend:"],
    },
]

# ---------------------------------------------------------------------------
# HTML → plain-text stripper
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "button"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def text(self) -> str:
        return " ".join(self._parts)


def _fetch_text(url: str) -> str:
    """Fetch a URL and return stripped plaintext (best-effort)."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "StackNest-DocBot/1.0 (stacknests.com)"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Extract <main> or <article> if present for focused content
        main_match = re.search(
            r"<main[^>]*>(.*?)</main>",
            html, re.DOTALL | re.IGNORECASE,
        ) or re.search(
            r"<article[^>]*>(.*?)</article>",
            html, re.DOTALL | re.IGNORECASE,
        )
        body = main_match.group(1) if main_match else html
        parser = _TextExtractor()
        parser.feed(body)
        text = parser.text()
        # Collapse whitespace
        text = re.sub(r"\s{3,}", "  ", text)
        return text[:4000]  # cap at 4000 chars per page
    except Exception as exc:
        return f"[doc fetch failed: {exc}]"


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, str]] = {}    # url -> (expiry, text)
_cache_lock = threading.Lock()
_warmed = False


def _get_cached_text(url: str) -> str:
    with _cache_lock:
        entry = _cache.get(url)
        if entry and time.time() < entry[0]:
            return entry[1]

    text = _fetch_text(url)

    with _cache_lock:
        _cache[url] = (time.time() + _CACHE_TTL, text)

    return text


def warm_cache() -> None:
    """Pre-fetch all doc pages in a background thread. Call once at startup."""
    global _warmed
    if _warmed:
        return
    _warmed = True

    def _warm():
        for page in _DOC_PAGES:
            try:
                _get_cached_text(page["url"])
            except Exception:
                pass

    threading.Thread(target=_warm, daemon=True).start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_doc_context(instruction: str, max_pages: int = 2) -> str:
    """
    Return a concatenated snippet of the most relevant doc sections for the
    given instruction. Returns '' if nothing relevant or cache misses.

    The snippet is formatted for injection into a system prompt.
    """
    lower = instruction.lower()
    scored: list[tuple[int, dict]] = []

    for page in _DOC_PAGES:
        score = sum(1 for kw in page["keywords"] if kw.lower() in lower)
        if score > 0:
            scored.append((score, page))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_pages]

    parts: list[str] = []
    for _score, page in top:
        text = _get_cached_text(page["url"])
        if text and not text.startswith("[doc fetch failed"):
            url_label = page["url"].replace("https://docs.papermc.io/", "papermc.io/")
            excerpt = text[:_MAX_SNIPPET_CHARS].strip()
            parts.append(f"[Doc: {url_label}]\n{excerpt}")

    if not parts:
        return ""

    return (
        "\n\n## Live API Documentation (from docs.papermc.io)\n"
        + "\n\n".join(parts)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Mod doc pages — Fabric / Forge / NeoForge wikis
# ---------------------------------------------------------------------------

# (url, loader_tags, context_keywords)
# loader_tags: set of loaders this page is relevant for
_MOD_DOC_PAGES: list[dict] = [
    # ── Fabric ──
    {
        "url": "https://fabricmc.net/wiki/tutorial:items",
        "loaders": {"fabric"},
        "keywords": ["item", "custom item", "new item", "register item", "sword", "food"],
    },
    {
        "url": "https://fabricmc.net/wiki/tutorial:blocks",
        "loaders": {"fabric"},
        "keywords": ["block", "custom block", "new block", "register block", "block state"],
    },
    {
        "url": "https://fabricmc.net/wiki/tutorial:entities",
        "loaders": {"fabric"},
        "keywords": ["entity", "mob", "custom entity", "custom mob", "creature"],
    },
    {
        "url": "https://fabricmc.net/wiki/tutorial:keybinds",
        "loaders": {"fabric"},
        "keywords": ["keybind", "key binding", "hotkey", "keyboard", "client tick", "key press"],
    },
    {
        "url": "https://fabricmc.net/wiki/tutorial:events",
        "loaders": {"fabric"},
        "keywords": ["event", "callback", "fabric event", "server event", "attack", "interact"],
    },
    {
        "url": "https://fabricmc.net/wiki/tutorial:registry",
        "loaders": {"fabric"},
        "keywords": ["registry", "registries", "register", "DeferredRegister"],
    },
    # ── Forge ──
    {
        "url": "https://forge.gemwire.uk/wiki/Items",
        "loaders": {"forge"},
        "keywords": ["item", "custom item", "register item", "DeferredRegister", "ItemStack"],
    },
    {
        "url": "https://forge.gemwire.uk/wiki/Blocks",
        "loaders": {"forge"},
        "keywords": ["block", "custom block", "register block", "BlockBehaviour", "BlockState"],
    },
    {
        "url": "https://forge.gemwire.uk/wiki/Entities",
        "loaders": {"forge"},
        "keywords": ["entity", "mob", "custom entity", "EntityType", "registerGoals", "PathfinderMob"],
    },
    {
        "url": "https://forge.gemwire.uk/wiki/Events",
        "loaders": {"forge"},
        "keywords": ["event", "SubscribeEvent", "MinecraftForge", "EVENT_BUS", "forge event"],
    },
    {
        "url": "https://forge.gemwire.uk/wiki/Capabilities",
        "loaders": {"forge"},
        "keywords": ["capability", "ICapabilityProvider", "energy", "fluid", "item handler"],
    },
    {
        "url": "https://forge.gemwire.uk/wiki/Networking",
        "loaders": {"forge"},
        "keywords": ["packet", "networking", "SimpleChannel", "network", "send packet"],
    },
    # ── NeoForge ──
    {
        "url": "https://docs.neoforged.net/docs/items/",
        "loaders": {"neoforge"},
        "keywords": ["item", "custom item", "register item", "DeferredItem", "ItemStack"],
    },
    {
        "url": "https://docs.neoforged.net/docs/blocks/",
        "loaders": {"neoforge"},
        "keywords": ["block", "custom block", "register block", "BlockBehaviour", "BlockState"],
    },
    {
        "url": "https://docs.neoforged.net/docs/entities/",
        "loaders": {"neoforge"},
        "keywords": ["entity", "mob", "custom entity", "EntityType", "PathfinderMob"],
    },
    {
        "url": "https://docs.neoforged.net/docs/events/",
        "loaders": {"neoforge"},
        "keywords": ["event", "SubscribeEvent", "NeoForge", "EVENT_BUS", "neoforge event"],
    },
    {
        "url": "https://docs.neoforged.net/docs/networking/",
        "loaders": {"neoforge"},
        "keywords": ["packet", "networking", "network", "send packet", "custom packet"],
    },
    {
        "url": "https://docs.neoforged.net/docs/items/tools/",
        "loaders": {"neoforge"},
        "keywords": ["tool", "pickaxe", "axe", "shovel", "sword", "tool tier"],
    },
]


def get_mod_doc_context(instruction: str, loader: str, max_pages: int = 2) -> str:
    """
    Return doc snippets relevant to this mod instruction and loader.
    Returns '' if nothing relevant or all fetches fail.
    """
    lower  = instruction.lower()
    scored: list[tuple[int, dict]] = []

    for page in _MOD_DOC_PAGES:
        if loader not in page["loaders"]:
            continue
        score = sum(1 for kw in page["keywords"] if kw.lower() in lower)
        if score > 0:
            scored.append((score, page))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_pages]

    parts: list[str] = []
    for _score, page in top:
        text = _get_cached_text(page["url"])
        if text and not text.startswith("[doc fetch failed"):
            url_label = (
                page["url"]
                .replace("https://fabricmc.net/wiki/", "fabricmc.net/wiki/")
                .replace("https://forge.gemwire.uk/wiki/", "forge.gemwire.uk/")
                .replace("https://docs.neoforged.net/docs/", "docs.neoforged.net/")
            )
            excerpt = text[:_MAX_SNIPPET_CHARS].strip()
            parts.append(f"[Doc: {url_label}]\n{excerpt}")

    if not parts:
        return ""

    loader_label = loader.capitalize()
    return (
        f"\n\n## Live {loader_label} API Documentation\n"
        + "\n\n".join(parts)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Datapack doc pages — datapack.wiki
# ---------------------------------------------------------------------------

_DATAPACK_DOC_PAGES: list[dict] = [
    {
        "url": "https://datapack.wiki/wiki/files/functions",
        "keywords": ["function", "mcfunction", "tick", "load", "loop", "execute", "schedule", "return", "macro"],
    },
    {
        "url": "https://datapack.wiki/wiki/files/advancements",
        "keywords": ["advancement", "criteria", "trigger", "reward", "grant", "toast", "goal", "challenge"],
    },
    {
        "url": "https://datapack.wiki/wiki/files/recipes",
        "keywords": ["recipe", "craft", "crafting", "shaped", "shapeless", "smelt", "smeltable", "cook", "blast", "smoke"],
    },
    {
        "url": "https://datapack.wiki/wiki/files/tags",
        "keywords": ["tag", "function tag", "block tag", "item tag", "entity tag", "values", "minecraft:tick", "minecraft:load"],
    },
    {
        "url": "https://datapack.wiki/wiki/files/predicates",
        "keywords": ["predicate", "condition", "loot condition", "random chance", "entity properties", "location", "weather"],
    },
    {
        "url": "https://datapack.wiki/wiki/concepts/target-selectors",
        "keywords": ["@e", "@a", "@s", "@p", "@r", "selector", "target selector", "type=", "nbt=", "tag=", "distance=", "limit="],
    },
    {
        "url": "https://datapack.wiki/wiki/nbt-scoreboards",
        "keywords": ["scoreboard", "nbt", "data storage", "storage", "objectives", "players", "score", "counter", "team"],
    },
    {
        "url": "https://datapack.wiki/guide/getting-started",
        "keywords": ["getting started", "create datapack", "pack.mcmeta", "namespace", "first datapack", "directory structure"],
    },
]


def get_datapack_doc_context(instruction: str, max_pages: int = 2) -> str:
    """
    Return datapack.wiki doc snippets relevant to this datapack instruction.
    Returns '' if nothing relevant or all fetches fail.
    """
    lower = instruction.lower()
    scored: list[tuple[int, dict]] = []

    for page in _DATAPACK_DOC_PAGES:
        score = sum(1 for kw in page["keywords"] if kw.lower() in lower)
        if score > 0:
            scored.append((score, page))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_pages]

    parts: list[str] = []
    for _score, page in top:
        text = _get_cached_text(page["url"])
        if text and not text.startswith("[doc fetch failed"):
            url_label = page["url"].replace("https://datapack.wiki/", "datapack.wiki/")
            excerpt = text[:_MAX_SNIPPET_CHARS].strip()
            parts.append(f"[Doc: {url_label}]\n{excerpt}")

    if not parts:
        return ""

    return (
        "\n\n## Live Datapack Documentation (from datapack.wiki)\n"
        + "\n\n".join(parts)
        + "\n"
    )

