"""
validation/static_check.py — Detect deprecated/incorrect Paper API usage without compiling.

These patterns are checked BEFORE compilation so the model can be given quick
feedback without waiting for javac. They catch the most common 3B model mistakes.
"""

import re
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning", "info"]


@dataclass
class StaticIssue:
    severity: Severity
    pattern: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        loc = f" (line {self.line})" if self.line else ""
        return f"[{self.severity.upper()}]{loc} {self.message}"


# --------------------------------------------------------------------------- #
# Pattern definitions                                                          #
# --------------------------------------------------------------------------- #

# (regex, severity, message)
PATTERNS: list[tuple[str, Severity, str]] = [
    # ---- Deprecated API --------------------------------------------------------
    (
        r"\bChatColor\.",
        "error",
        "ChatColor is deprecated. Use net.kyori.adventure.text.format.NamedTextColor or MiniMessage.",
    ),
    (
        r"\.sendMessage\s*\(\s*\"",
        "error",
        "sendMessage(String) is deprecated. Use player.sendMessage(Component) with Adventure API.",
    ),
    (
        r"org\.bukkit\.craftbukkit",
        "error",
        "CraftBukkit internal import detected. Never use NMS/CraftBukkit internals.",
    ),
    (
        r"net\.minecraft\.server",
        "error",
        "NMS import detected. Never use net.minecraft.server internals.",
    ),
    (
        r"\bsetMetadata\s*\(",
        "warning",
        "setMetadata() is deprecated for persistent data. Use PersistentDataContainer instead.",
    ),
    (
        r"\bgetMetadata\s*\(",
        "warning",
        "getMetadata() is deprecated for persistent data. Use PersistentDataContainer instead.",
    ),
    (
        r"\bPlayerChatEvent\b",
        "error",
        "PlayerChatEvent is cancelled on Paper. Use AsyncChatEvent (io.papermc.paper.event.player).",
    ),
    (
        r"\bBukkit\.broadcastMessage\s*\(",
        "warning",
        "Bukkit.broadcastMessage(String) is deprecated. Use Bukkit.broadcast(Component).",
    ),

    # ---- Common model mistakes -------------------------------------------------
    (
        r"new\s+FileConfiguration\s*\(",
        "error",
        "FileConfiguration cannot be instantiated directly. Use YamlConfiguration.loadConfiguration(file).",
    ),
    (
        r"api-version:\s*['\"]?1\.(8|9|10|11|12|13|14|15|16|17|18)['\"]?",
        "error",
        "api-version is too old. Use '1.21' for Paper 1.21.",
    ),
    (
        r"import\s+org\.bukkit\.plugin\.PluginCommand",
        "error",
        "Wrong import: PluginCommand is in org.bukkit.command, NOT org.bukkit.plugin. "
        "Change to: import org.bukkit.command.PluginCommand;",
    ),
    (
        r"import\s+net\.md_5\.bungee\.api\.chat\.TextComponent",
        "error",
        "BungeeCord TextComponent detected. BanList.addBan() and Adventure API require "
        "net.kyori.adventure.text.Component, not BungeeCord TextComponent.",
    ),
    (
        r"import\s+be\.seeseemelk\.mockbukkit",
        "error",
        "MockBukkit import detected in runtime plugin source. Move all MockBukkit/JUnit code to src/test/java and keep runtime files free of test framework imports.",
    ),
    (
        r"\bMaterial\.RAW_FISH\b",
        "error",
        "Material.RAW_FISH was removed. Use Material.COD (or SALMON) on modern Paper versions.",
    ),
    (
        r"\bEnchantment\.PROTECTION_ENVIRONMENTAL\b",
        "error",
        "Enchantment.PROTECTION_ENVIRONMENTAL was removed. Use Enchantment.PROTECTION.",
    ),
    (
        r"\bEnchantment\.DURABILITY\b",
        "error",
        "Enchantment.DURABILITY was removed. Use Enchantment.UNBREAKING.",
    ),

    # ---- Folia safety ----------------------------------------------------------
    (
        r"Bukkit\.getScheduler\(\)",
        "info",
        "BukkitScheduler is not Folia-compatible. For Folia, use entity.getScheduler() or RegionScheduler.",
    ),

    # ---- Adventure API wrong overloads -----------------------------------------
    (
        r"Component\.text\s*\([^)]*Component[^)]*,\s*(?:Named)?TextColor",
        "error",
        "Wrong Adventure API call: Component.text() does not accept (Component, TextColor). "
        "Use Component.text(\"string\", NamedTextColor.X) for literals, or "
        "component.color(NamedTextColor.X) to recolor an existing component.",
    ),
    (
        r"Component\.text\s*\(\s*\)\s*\.append\s*",
        "warning",
        "Prefer Component.empty().append(...) instead of Component.text().append(...) to start a component chain.",
    ),
    (
        r"\.sendMessage\s*\(\s*(?:ChatColor|\")",
        "error",
        "sendMessage() with a String/ChatColor is deprecated. Use sendMessage(Component) with Adventure API.",
    ),
    (
        # Fire only on single-arg Component.text() calls that contain string concat.
        # Component.text("x" + var)           → BAD  (no comma → single arg)
        # Component.text("x" + var, Color)    → OK   (comma after the + expression)
        # Match: Component.text( <no-comma content> + <no-comma content> )
        r"Component\.text\s*\(\s*[^,)]*\+[^,)]*\)",
        "error",
        "String concatenation inside Component.text(). "
        "Replace Component.text(\"Hello \" + var) with "
        "Component.text(\"Hello \").append(Component.text(var)). "
        "Every Component.text() call must contain ONLY a literal string or a single variable — no + operator.",
    ),

    # ---- Particle enum renamed in 1.20.5+ (old names were removed) ----------------
    (
        r"\bParticle\.EXPLOSION_HUGE\b",
        "error",
        "Particle.EXPLOSION_HUGE was removed in 1.20.5. Use Particle.EXPLOSION_EMITTER instead.",
    ),
    (
        r"\bParticle\.EXPLOSION_LARGE\b",
        "error",
        "Particle.EXPLOSION_LARGE was removed in 1.20.5. Use Particle.EXPLOSION_EMITTER instead.",
    ),
    (
        r"\bParticle\.EXPLOSION_NORMAL\b",
        "error",
        "Particle.EXPLOSION_NORMAL was removed in 1.20.5. Use Particle.EXPLOSION for small explosions instead.",
    ),
    (
        r"\bParticle\.FIREWORKS_SPARK\b",
        "error",
        "Particle.FIREWORKS_SPARK was renamed to Particle.FIREWORK in 1.20.5.",
    ),
    (
        r"\bParticle\.SMOKE_NORMAL\b",
        "error",
        "Particle.SMOKE_NORMAL was renamed to Particle.SMOKE in 1.20.5.",
    ),
    (
        r"\bParticle\.SMOKE_LARGE\b",
        "error",
        "Particle.SMOKE_LARGE was renamed to Particle.LARGE_SMOKE in 1.20.5.",
    ),
    (
        r"\bParticle\.CRIT_MAGIC\b",
        "error",
        "Particle.CRIT_MAGIC was renamed to Particle.ENCHANTED_HIT in 1.20.5.",
    ),
    (
        r"\bParticle\.SPELL_MOB\b",
        "error",
        "Particle.SPELL_MOB was renamed to Particle.ENTITY_EFFECT in 1.20.5.",
    ),
    (
        r"\bParticle\.ENCHANTMENT_TABLE\b",
        "error",
        "Particle.ENCHANTMENT_TABLE was renamed to Particle.ENCHANT in 1.20.5.",
    ),

    # ---- BanList API misuse -------------------------------------------------------
    (
        # Detect: getBanList(BanList.Type.PROFILE) NOT assigned to ProfileBanList.
        # The return type needs an explicit cast or typed variable; without it javac
        # resolves the generic as BanList<?> and rejects .addBan(PlayerProfile,...).
        r"getBanList\s*\(\s*(?:BanList\.)?Type\.PROFILE\s*\)\s*\.addBan",
        "error",
        "getBanList(BanList.Type.PROFILE).addBan() fails because getBanList() returns BanList<?> "
        "which doesn't expose addBan(PlayerProfile,...) directly. "
        "Cast first: ProfileBanList banList = (ProfileBanList) Bukkit.getBanList(BanList.Type.PROFILE); "
        "then call banList.addBan(player.getPlayerProfile(), Component.text(\"reason\"), (Date) null, \"PluginName\"); "
        "Required imports: import org.bukkit.ban.ProfileBanList; import org.bukkit.BanList; "
        "import net.kyori.adventure.text.Component; import java.util.Date;",
    ),
    (
        r"\.addBan\s*\([^)]*Instant[^)]*\)",
        "error",
        "BanList.addBan() expiry parameter is java.util.Date, NOT java.time.Instant. "
        "Use (Date) null for a permanent ban: "
        "banList.addBan(target, Component.text(\"reason\"), (Date) null, \"source\");",
    ),
    (
        r"\.addBan\s*\([^)]*TextComponent[^)]*\)",
        "error",
        "BanList.addBan() reason must be net.kyori.adventure.text.Component, NOT TextComponent. "
        "Use Component.text(\"reason\") — never new TextComponent(...) or any TextComponent variant. "
        "Correct call: Bukkit.getBanList(BanList.Type.NAME).addBan(playerName, Component.text(\"reason\"), (Date) null, \"PluginName\"); "
        "Required imports: import net.kyori.adventure.text.Component; import java.util.Date;",
    ),
    (
        # addBan with bare null (not cast to Date) causes 'no suitable method found' because
        # javac can't resolve the overload when null has no explicit type annotation.
        r"\.addBan\s*\([^)]*,\s*null\s*[,)]",
        "error",
        "BanList.addBan() — pass (Date) null for the expiry parameter, NOT bare null. "
        "Java cannot resolve the overload when null has no type: "
        "addBan(target, Component.text(\"reason\"), (Date) null, \"source\") — note the cast (Date) null.",
    ),
    (
        # Audience.players() doesn't exist in Paper/Adventure API.
        r"getAudience\(\)\.players\(\)",
        "error",
        "Audience.players() does not exist in Paper/Adventure API. "
        "To message all online players use Bukkit.getServer().sendMessage(component) "
        "or Bukkit.broadcast(component) or iterate Bukkit.getOnlinePlayers().",
    ),
    (
        # Paper 1.13+ bundles Adventure API natively. The standalone adventure-platform-bukkit
        # library is only needed for vanilla Spigot (no Adventure). BukkitAudiences is wrong for Paper.
        r"import\s+net\.kyori\.adventure\.platform\.bukkit\.BukkitAudiences",
        "error",
        "BukkitAudiences (adventure-platform-bukkit) is NOT needed for Paper plugins. "
        "Paper 1.13+ bundles the Adventure API natively. "
        "Remove the BukkitAudiences import and all references. "
        "To send a message: player.sendMessage(Component.text(\"message\")); "
        "To broadcast: Bukkit.broadcast(component); "
        "Required import only: import net.kyori.adventure.text.Component;",
    ),
    (
        # OfflinePlayer does not have getEnderChest(). Only online Player does.
        r"(?:OfflinePlayer|offlinePlayer|offline)\s*\.\s*getEnderChest\s*\(",
        "error",
        "OfflinePlayer.getEnderChest() does not exist. "
        "Only an online Player has getEnderChest(). "
        "Check if the player is online first: Player online = Bukkit.getPlayer(offlinePlayer.getUniqueId()); "
        "if (online != null) { Inventory ec = online.getEnderChest(); ... }",
    ),
    (
        # Player.ban() in Paper takes (Component reason, Date expiry, String source, boolean kick).
        # Passing a boolean as the first argument is wrong.
        r"\.ban\s*\(\s*(?:true|false)\s*,",
        "error",
        "Player.ban() first argument is NOT a boolean. "
        "Paper 26.1 signature: player.ban(Component reason, Date expiry, String source, boolean kickIfOnline). "
        "Correct usage: player.ban(Component.text(\"reason\"), (Date) null, \"PluginName\", true); "
        "Alternatively use BanList: Bukkit.getBanList(BanList.Type.NAME).addBan(player.getName(), Component.text(\"reason\"), (Date) null, \"PluginName\");",
    ),
    (
        # Paper 26.2: beds are no longer block entities — they have no PDC.
        # Detect TileState cast of a block whose type includes a bed material.
        # Also detect getPersistentDataContainer() called on block.getState() near bed material checks.
        r"(?:Material\.(?:[A-Z_]*_BED|BED)\b.*\bTileState|TileState.*Material\.(?:[A-Z_]*_BED|BED)\b)",
        "error",
        "Beds are no longer block entities in Paper 26.2 (Minecraft 26.2). "
        "They have no PersistentDataContainer and cannot be cast to TileState. "
        "Store your data elsewhere (e.g. in a config file or SQLite keyed on the block location).",
    ),
    (
        # BossBarViewer is not a type in Paper/Adventure API. bossBar.viewers() returns Set<Audience>.
        r"BossBarViewer",
        "error",
        "BossBarViewer is not a type in Paper/Adventure API. "
        "bossBar.viewers() returns Set<Audience> — each element is already an Audience. "
        "To remove all viewers: for (Audience viewer : new HashSet<>(bossBar.viewers())) bossBar.removeViewer(viewer); "
        "Or: bossBar.viewers().forEach(bossBar::removeViewer);",
    ),
    (
        # BanList.getBans() doesn't exist. The correct method is getBanEntries().
        r"\.getBans\s*\(",
        "error",
        "BanList.getBans() does not exist. "
        "Use getBanEntries() to get all ban entries: Set<BanEntry<?>> entries = Bukkit.getBanList(BanList.Type.NAME).getBanEntries();",
    ),

    # ---- Paper Brigadier API misuse ------------------------------------------
    (
        r"\bCommands\s*<",
        "error",
        "Paper 26.1 Brigadier: Commands (io.papermc.paper.command.brigadier.Commands) is NOT a generic type. "
        "Remove the type parameter — use 'Commands cmds = event.registrar();' not 'Commands<S>' or "
        "'Commands<BukkitBrigadierCommandSource>'.",
    ),
    (
        r"\.register\s*\(\s*(?:Component\.text|new\s+TextComponent)",
        "error",
        "Commands.register() first argument must be a LiteralCommandNode<CommandSourceStack> "
        "(built with Commands.literal(\"name\").executes(...).build()), NOT Component.text() or TextComponent. "
        "The command name is embedded in Commands.literal(\"name\") — never passed to register() as a Component. "
        "Correct: cmds.register(Commands.literal(\"spawn\").executes(ctx -> { return Command.SINGLE_SUCCESS; }).build()); "
        "With description: cmds.register(node, \"plain String description\");",
    ),

    # ---- Runtime failures that still compile -----------------------------------
    (
        r"getCommand\s*\(\s*[\"'][\w-]+[\"']\s*\)\s*\.",
        "warning",
        "getCommand() can return null — direct chaining will throw a NullPointerException on startup. "
        "Use: PluginCommand cmd = getCommand(\"name\"); if (cmd != null) { cmd.setExecutor(this); }",
    ),
]


# Velocity API imports — if any of these appear the file is a Velocity plugin,
# and Paper-specific rules (ChatColor, @EventHandler, PlayerChatEvent, etc.) must not apply.
_VELOCITY_IMPORT_RE = re.compile(r"import\s+com\.velocitypowered\.", re.MULTILINE)

# Fabric / NeoForge imports — mods using these legitimately reference net.minecraft.* via
# deobfuscated Yarn/Parchment mappings.  The NMS/CraftBukkit rules must not fire for them.
_FABRIC_IMPORT_RE = re.compile(r"import\s+net\.fabricmc\.", re.MULTILINE)
_NEOFORGE_IMPORT_RE = re.compile(r"import\s+net\.neoforged\.", re.MULTILINE)

# Patterns that only apply to Paper/Bukkit plugins — Fabric/NeoForge mods legitimately
# use net.minecraft.* (via Yarn/Parchment) so those checks must be suppressed for them.
_PAPER_ONLY_NMS_PATTERNS: frozenset[str] = frozenset({
    r"net\.minecraft\.server",
    r"org\.bukkit\.craftbukkit",
})

_PAPER_ONLY_PATTERNS: frozenset[str] = frozenset({
    r"\bPlayerChatEvent\b",
    r"\bChatColor\.",
    r"\.sendMessage\s*\(\s*\"",
    r"\bgetMetadata\s*\(",
    r"\bsetMetadata\s*\(",
    r"\bBukkit\.broadcastMessage\s*\(",
    r"api-version:\s*['\"]?1\.(8|9|10|11|12|13|14|15|16|17|18)['\"]?",
})

# Patterns that are legitimate in JUnit/MockBukkit test classes and must not fire there.
_TEST_SKIP_PATTERNS: frozenset[str] = frozenset({
    r"import\s+be\.seeseemelk\.mockbukkit",
    r"import\s+org\.junit",
})


def _is_test_class(java_code: str) -> bool:
    """Return True when the block looks like a JUnit test class (not a runtime plugin)."""
    return bool(
        re.search(r'@(?:BeforeEach|AfterEach|Test|ExtendWith)\b', java_code)
        or re.search(r'\bclass\s+\w+Test\b', java_code)
    )


def check_java(java_code: str) -> list[StaticIssue]:
    """Run all patterns against Java source code. Returns list of issues."""
    issues: list[StaticIssue] = []
    lines = java_code.splitlines()

    is_velocity = bool(_VELOCITY_IMPORT_RE.search(java_code))
    is_fabric_or_neo = bool(
        _FABRIC_IMPORT_RE.search(java_code) or _NEOFORGE_IMPORT_RE.search(java_code)
    )
    is_test = _is_test_class(java_code)

    for pattern, severity, message in PATTERNS:
        # Skip Paper-specific rules for Velocity plugins
        if is_velocity and pattern in _PAPER_ONLY_PATTERNS:
            continue
        # Skip NMS rules for Fabric/NeoForge mods — they use net.minecraft.* legitimately
        if is_fabric_or_neo and pattern in _PAPER_ONLY_NMS_PATTERNS:
            continue
        # Skip test-framework import checks for JUnit test classes — MockBukkit/JUnit are
        # legitimate there and should not be reported as runtime errors.
        if is_test and pattern in _TEST_SKIP_PATTERNS:
            continue
        for i, line in enumerate(lines, start=1):
            if re.search(pattern, line):
                issues.append(
                    StaticIssue(severity=severity, pattern=pattern, message=message, line=i)
                )
                break  # One issue per pattern per file is enough for feedback

    # ── @EventHandler / @Subscribe missing on event-listener methods ─────── #
    # Bukkit/Paper uses @EventHandler; Velocity uses @Subscribe.
    # A method that accepts an XxxEvent parameter with neither annotation will
    # never be called by the server — the event silently fires with no effect.
    _event_method_re = re.compile(
        r"^\s*(?:public|protected)\s+void\s+\w+\s*\(\s*(\w*Event)\s+\w+\s*\)",
    )
    for i, line in enumerate(lines):
        m = _event_method_re.match(line)
        if not m:
            continue
        # Look back up to 5 lines for the appropriate annotation
        context = lines[max(0, i - 5):i]
        if is_velocity:
            has_annotation = any("@Subscribe" in l for l in context)
            if not has_annotation:
                issues.append(StaticIssue(
                    severity="error",
                    pattern="missing_subscribe_annotation",
                    message=(
                        f"Method accepting {m.group(1)} is missing @Subscribe — "
                        "the event will never fire on Velocity. "
                        "Add @Subscribe above the method (com.velocitypowered.api.event.Subscribe)."
                    ),
                    line=i + 1,
                ))
        else:
            has_annotation = any("@EventHandler" in l for l in context)
            if not has_annotation:
                issues.append(StaticIssue(
                    severity="error",
                    pattern="missing_event_handler_annotation",
                    message=(
                        f"Method accepting {m.group(1)} is missing @EventHandler — "
                        "the event will never fire. Add @Override @EventHandler above the method."
                    ),
                    line=i + 1,
                ))

    # ── getCommand() used but setExecutor never called (file-scope) ──────── #
    # Only applies to the main plugin class (extends JavaPlugin / has onEnable).
    # Exclude event.getCommand() / CommandEvent patterns — those are framework
    # calls on command event objects, not plugin command registration.
    _is_plugin_class = bool(
        re.search(r'\bextends\s+JavaPlugin\b', java_code)
        or re.search(r'\bvoid\s+onEnable\s*\(', java_code)
    )
    # Plugin-level getCommand: `this.getCommand(` or bare `getCommand(` on its own
    # but NOT `event.getCommand(` or `cmd.getCommand(` or `.getCommand(`
    _plugin_getcmd = bool(re.search(r'(?<![.\w])getCommand\s*\(', java_code))
    if _is_plugin_class and _plugin_getcmd and not re.search(r'setExecutor\s*\(', java_code):
        for i, line in enumerate(lines, start=1):
            if re.search(r'(?<![.\w])getCommand\s*\(', line):
                issues.append(StaticIssue(
                    severity="error",
                    pattern="getcommand_no_setexecutor",
                    message=(
                        "getCommand() is called but setExecutor() is never called — "
                        "commands will silently do nothing. "
                        "Call getCommand(\"name\").setExecutor(this) in onEnable()."
                    ),
                    line=i,
                ))
                break

    # ── implements Listener but registerEvents never called (file-scope) ─── #
    # Only fire on the MAIN plugin class (has onEnable / extends JavaPlugin).
    # Separate Listener classes should NOT call registerEvents themselves —
    # the main class registers them.  Checking standalone listener blocks causes
    # false positives that confuse the heal model into adding onEnable() to them.
    _is_main_class = bool(
        re.search(r'\bextends\s+JavaPlugin\b', java_code)
        or re.search(r'\bvoid\s+onEnable\s*\(', java_code)
    )
    if _is_main_class and re.search(r'\bimplements\b[^{]*\bListener\b', java_code) and \
            not re.search(r'registerEvents\s*\(', java_code):
        for i, line in enumerate(lines, start=1):
            if re.search(r'\bimplements\b[^{]*\bListener\b', line):
                issues.append(StaticIssue(
                    severity="error",
                    pattern="listener_not_registered",
                    message=(
                        "Class implements Listener but registerEvents() is never called — "
                        "events will silently not fire. "
                        "Call getServer().getPluginManager().registerEvents(this, this) in onEnable()."
                    ),
                    line=i,
                ))
                break

    # ── No class declaration (imports-only truncation) ──────────────────── #
    # If the file has import statements but no class/interface/enum/record
    # declaration at all, the model was cut off before writing the class body.
    # Flag this immediately so the healer loop knows it needs a full regen.
    has_imports = bool(re.search(r'^\s*import\s+[\w.]+', java_code, re.MULTILINE))
    has_class_decl = bool(re.search(
        r'\b(?:class|interface|enum|record)\s+\w+', java_code
    ))
    if has_imports and not has_class_decl and not is_test:
        issues.append(StaticIssue(
            severity="error",
            pattern="no_class_declaration",
            message=(
                "No class declaration found — the file contains only package/import "
                "statements. The entire class body is missing. "
                "Output a complete 'public class [Name] extends JavaPlugin' with "
                "onEnable(), onDisable(), and all required methods in a SINGLE ```java block."
            ),
            line=1,
        ))

    return issues

def _check_import_wall(java_code: str, declared: set[str], project_prefix: str | None) -> list[StaticIssue]:
    """
    Detect import-wall anti-pattern: the model pre-plans a large number of
    sub-package classes via import statements before writing them, then runs
    out of tokens before defining any of them.  If >=6 project-local imports
    exist and <40% have a corresponding declared class, flag it.
    """
    if not project_prefix:
        return []
    imports = re.findall(r"^\s*import\s+([\w.]+)\s*;", java_code, re.MULTILINE)
    project_imports = [
        fqn for fqn in imports
        if fqn.startswith(project_prefix) and not fqn.endswith("*")
    ]
    if len(project_imports) < 6:
        return []
    undeclared = [fqn for fqn in project_imports if fqn.split(".")[-1] not in declared]
    ratio_missing = len(undeclared) / len(project_imports)
    if ratio_missing < 0.6:
        return []
    sample = ", ".join(fqn.split(".")[-1] for fqn in undeclared[:5])
    return [StaticIssue(
        severity="error",
        pattern="import_wall",
        message=(
            f"{len(undeclared)} project-local classes are imported but never defined "
            f"({sample}{'...' if len(undeclared) > 5 else ''}). "
            "This is the import-wall anti-pattern: you pre-planned sub-classes via imports "
            "before writing them and ran out of tokens. "
            "FIX: remove ALL these imports and convert every sub-class to a "
            "private static nested class inside the main plugin class. "
            "Do NOT re-add the imports unless the class body is present in the output."
        ),
    )]


def check_plugin_yml(yml_text: str) -> list[StaticIssue]:
    """Run YAML-specific pattern checks."""
    issues: list[StaticIssue] = []
    for pattern, severity, message in PATTERNS:
        if re.search(pattern, yml_text):
            issues.append(StaticIssue(severity=severity, pattern=pattern, message=message))
    return issues


def check_response(response: str) -> list[StaticIssue]:
    """Check all code blocks in a model response."""
    issues: list[StaticIssue] = []

    # Extract Java blocks
    java_blocks = re.findall(r"```java\n(.*?)```", response, re.DOTALL)
    for block in java_blocks:
        issues.extend(check_java(block))

    # Extract YAML blocks
    yml_blocks = re.findall(r"```yaml\n(.*?)```", response, re.DOTALL)
    for block in yml_blocks:
        issues.extend(check_plugin_yml(block))

    # ── Cross-block check: detect project-local imports without a matching class ──
    # If the main class imports com.example.foo.Bar, there must be a java block that
    # declares `class Bar` (or `interface Bar`, `enum Bar`) in the output.
    if java_blocks:
        # Collect all class/interface/enum names declared across all java blocks
        declared: set[str] = set()
        for block in java_blocks:
            for m in re.finditer(
                r"^\s*(?:public\s+|private\s+|protected\s+|final\s+|abstract\s+)*"
                r"(?:class|interface|enum)\s+(\w+)",
                block, re.MULTILINE,
            ):
                declared.add(m.group(1))

        # Well-known external / framework packages — never flag these.
        # Kept as a fast short-circuit for the most common prefixes.
        _STDLIB_PREFIXES = (
            "java.", "javax.", "org.bukkit.", "io.papermc.", "net.kyori.",
            "net.minecraft.", "org.bukkit.craftbukkit.", "com.google.",
            "org.yaml.", "be.seeseemelk.", "org.junit.", "org.mockito.",
            "com.mojang.", "it.unimi.", "org.java_websocket.", "redis.clients.",
            "com.zaxxer.", "org.apache.", "io.netty.", "com.rabbitmq.",
            "org.mongodb.", "me.clip.", "com.destroystokyo.", "net.milkbowl.",
            "org.spongepowered.", "net.luckperms.", "com.sk89q.", "net.md_5.",
        )

        # Gather all imports from main (first) java block
        main_block = java_blocks[0]

        # Derive the plugin's own package prefix (first 2 segments, e.g.
        # "com.spirits" from "com.spirits.SpiritsPlugin").  Only imports that
        # START with this prefix are considered project-local — everything else
        # is a third-party library and should never be flagged as missing.
        pkg_match = re.search(r"^\s*package\s+([\w.]+)\s*;", main_block, re.MULTILINE)
        if pkg_match:
            parts = pkg_match.group(1).split(".")
            # Use 2 segments when available (com.myproject), else 1 (myproject)
            project_prefix = ".".join(parts[:2]) + "."
        else:
            project_prefix = None   # can't determine — fall back to blocklist only

        for imp_match in re.finditer(
            r"^\s*import\s+([\w.]+)\s*;", main_block, re.MULTILINE
        ):
            fqn = imp_match.group(1)
            simple_name = fqn.split(".")[-1]
            if simple_name == "*":
                continue
            # Skip well-known external packages (fast path)
            if any(fqn.startswith(p) for p in _STDLIB_PREFIXES):
                continue
            # Skip anything that doesn't belong to the plugin's own package tree.
            # e.g.  plugin package "com.spirits" → only flag "com.spirits.*"
            #        org.java_websocket.* → different root → skip
            if project_prefix and not fqn.startswith(project_prefix):
                continue
            # If we can't find a declaration for this class name in any block, flag it
            if simple_name not in declared:
                issues.append(StaticIssue(
                    severity="error",
                    pattern="missing_helper_class",
                    message=(
                        f"Class '{simple_name}' is imported from '{fqn}' but never "
                        f"defined in the output. Either add a ```java block for it or "
                        f"use a private inner class instead."
                    ),
                ))

        # Check for import-wall anti-pattern
        issues.extend(_check_import_wall(main_block, declared, project_prefix))

    return issues


def get_error_messages(response: str) -> list[str]:
    """Return only error-level issues as strings (for feedback loop)."""
    return [str(i) for i in check_response(response) if i.severity == "error"]


def get_all_messages(response: str) -> list[str]:
    return [str(i) for i in check_response(response)]
