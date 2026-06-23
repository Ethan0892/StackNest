"""
api/presets.py
==============
Static plugin preset catalog.

Each preset is a complete, ready-to-compile Paper 26.1 plugin.
No AI is used — the Java source and plugin.yml are fully written here.

Placeholders used in source / yml:
  {NAME}   — PascalCase plugin class name   (e.g. "HealPlugin" from "Heal")
  {LOWER}  — lowercase package / permission prefix (e.g. "heal")

Public API
----------
PRESETS         : list[dict]  — ordered catalog (metadata + source)
get_preset(id)  : dict | None — look up by id
build_preset(id, plugin_name, paper_profile) : bytes — compile and return JAR
"""

from __future__ import annotations
import re
from typing import Optional
from validation.compile_check import build_jar, DEFAULT_PAPER_PROFILE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(text: str, name: str, lower: str) -> str:
    return text.replace("{NAME}", name).replace("{LOWER}", lower)


def _to_pascal(s: str) -> str:
    """'my plugin' / 'my-plugin' / 'MyPlugin' → 'MyPlugin'"""
    cleaned = re.sub(r"[^a-zA-Z0-9 ]+", " ", s).strip()
    return "".join(w.capitalize() for w in cleaned.split()) or "MyPlugin"


def _to_lower(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower()) or "myplugin"


# ---------------------------------------------------------------------------
# Preset sources
# ---------------------------------------------------------------------------

_HEAL_JAVA = r"""
package com.example.{LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.List;

public final class {NAME}Plugin extends JavaPlugin implements CommandExecutor, TabCompleter {

    @Override
    public void onEnable() {
        var cmd = getCommand("heal");
        if (cmd != null) { cmd.setExecutor(this); cmd.setTabCompleter(this); }
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public boolean onCommand(CommandSender s, Command c, String l, String[] a) {
        if (!s.hasPermission("{LOWER}.use")) {
            s.sendMessage(Component.text("No permission.", NamedTextColor.RED));
            return true;
        }
        Player target;
        if (a.length >= 1) {
            if (!s.hasPermission("{LOWER}.others")) {
                s.sendMessage(Component.text("You can't heal others.", NamedTextColor.RED));
                return true;
            }
            target = getServer().getPlayer(a[0]);
            if (target == null) {
                s.sendMessage(Component.text("Player not found.", NamedTextColor.RED));
                return true;
            }
        } else if (s instanceof Player p) {
            target = p;
        } else {
            s.sendMessage(Component.text("Specify a player: /heal <player>", NamedTextColor.RED));
            return true;
        }
        target.setHealth(target.getMaxHealth());
        target.setFoodLevel(20);
        target.setSaturation(20f);
        target.setFireTicks(0);
        target.sendMessage(Component.text("You have been healed!", NamedTextColor.GREEN));
        if (!s.getName().equals(target.getName()))
            s.sendMessage(Component.text("Healed " + target.getName() + ".", NamedTextColor.GREEN));
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender s, Command c, String al, String[] args) {
        if (args.length == 1 && s.hasPermission("{LOWER}.others"))
            return getServer().getOnlinePlayers().stream()
                    .map(Player::getName)
                    .filter(n -> n.toLowerCase().startsWith(args[0].toLowerCase()))
                    .toList();
        return List.of();
    }
}
""".strip()

_HEAL_YML = """
name: {NAME}
version: '1.0'
main: com.example.{LOWER}.{NAME}Plugin
api-version: '1.21'
description: Heal yourself or another player to full health.
commands:
  heal:
    description: Heal a player.
    usage: /heal [player]
    permission: {LOWER}.use
permissions:
  {LOWER}.use:
    description: Use /heal on yourself.
    default: op
  {LOWER}.others:
    description: Heal other players.
    default: op
""".strip()

# ---------------------------------------------------------------------------

_HOME_JAVA = r"""
package com.example.{LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.List;

public final class {NAME}Plugin extends JavaPlugin implements CommandExecutor, TabCompleter {

    @Override
    public void onEnable() {
        saveDefaultConfig();
        for (var cmd : List.of("sethome", "home", "delhome")) {
            var c = getCommand(cmd);
            if (c != null) { c.setExecutor(this); c.setTabCompleter(this); }
        }
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public boolean onCommand(CommandSender s, Command c, String l, String[] a) {
        if (!(s instanceof Player p)) {
            s.sendMessage(Component.text("Player only.", NamedTextColor.RED));
            return true;
        }
        if (!p.hasPermission("{LOWER}.use")) {
            p.sendMessage(Component.text("No permission.", NamedTextColor.RED));
            return true;
        }
        String key = "homes." + p.getUniqueId();
        return switch (l.toLowerCase()) {
            case "sethome" -> {
                Location loc = p.getLocation();
                getConfig().set(key + ".world",  loc.getWorld().getName());
                getConfig().set(key + ".x",      loc.getX());
                getConfig().set(key + ".y",      loc.getY());
                getConfig().set(key + ".z",      loc.getZ());
                getConfig().set(key + ".yaw",    (double) loc.getYaw());
                getConfig().set(key + ".pitch",  (double) loc.getPitch());
                saveConfig();
                p.sendMessage(Component.text("Home set!", NamedTextColor.GREEN));
                yield true;
            }
            case "home" -> {
                if (!getConfig().contains(key)) {
                    p.sendMessage(Component.text("No home set. Use /sethome first.", NamedTextColor.YELLOW));
                    yield true;
                }
                World world = getServer().getWorld(getConfig().getString(key + ".world", "world"));
                if (world == null) {
                    p.sendMessage(Component.text("Your home world no longer exists.", NamedTextColor.RED));
                    yield true;
                }
                p.teleport(new Location(world,
                        getConfig().getDouble(key + ".x"),
                        getConfig().getDouble(key + ".y"),
                        getConfig().getDouble(key + ".z"),
                        (float) getConfig().getDouble(key + ".yaw"),
                        (float) getConfig().getDouble(key + ".pitch")));
                p.sendMessage(Component.text("Teleported home!", NamedTextColor.GREEN));
                yield true;
            }
            case "delhome" -> {
                if (!getConfig().contains(key)) {
                    p.sendMessage(Component.text("You have no home set.", NamedTextColor.YELLOW));
                    yield true;
                }
                getConfig().set(key, null);
                saveConfig();
                p.sendMessage(Component.text("Home deleted.", NamedTextColor.GREEN));
                yield true;
            }
            default -> false;
        };
    }

    @Override
    public List<String> onTabComplete(CommandSender s, Command c, String a, String[] args) {
        return List.of();
    }
}
""".strip()

_HOME_YML = """
name: {NAME}
version: '1.0'
main: com.example.{LOWER}.{NAME}Plugin
api-version: '1.21'
description: Set, teleport to, and delete your personal home point.
commands:
  sethome:
    description: Save current location as home.
    usage: /sethome
    permission: {LOWER}.use
  home:
    description: Teleport to your home.
    usage: /home
    permission: {LOWER}.use
  delhome:
    description: Delete your home point.
    usage: /delhome
    permission: {LOWER}.use
permissions:
  {LOWER}.use:
    description: Use /sethome, /home, and /delhome.
    default: true
""".strip()

# ---------------------------------------------------------------------------

_SPAWN_JAVA = r"""
package com.example.{LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.List;

public final class {NAME}Plugin extends JavaPlugin implements CommandExecutor, TabCompleter {

    @Override
    public void onEnable() {
        saveDefaultConfig();
        for (var cmd : List.of("setspawn", "spawn")) {
            var c = getCommand(cmd);
            if (c != null) { c.setExecutor(this); c.setTabCompleter(this); }
        }
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public boolean onCommand(CommandSender s, Command c, String l, String[] a) {
        return switch (l.toLowerCase()) {
            case "setspawn" -> {
                if (!s.hasPermission("{LOWER}.setspawn")) {
                    s.sendMessage(Component.text("No permission.", NamedTextColor.RED));
                    yield true;
                }
                if (!(s instanceof Player p)) {
                    s.sendMessage(Component.text("Player only.", NamedTextColor.RED));
                    yield true;
                }
                Location loc = p.getLocation();
                getConfig().set("spawn.world",  loc.getWorld().getName());
                getConfig().set("spawn.x",      loc.getX());
                getConfig().set("spawn.y",      loc.getY());
                getConfig().set("spawn.z",      loc.getZ());
                getConfig().set("spawn.yaw",    (double) loc.getYaw());
                getConfig().set("spawn.pitch",  (double) loc.getPitch());
                saveConfig();
                s.sendMessage(Component.text("Spawn point set!", NamedTextColor.GREEN));
                yield true;
            }
            case "spawn" -> {
                if (!s.hasPermission("{LOWER}.spawn")) {
                    s.sendMessage(Component.text("No permission.", NamedTextColor.RED));
                    yield true;
                }
                Player target;
                if (a.length >= 1 && s.hasPermission("{LOWER}.others")) {
                    target = getServer().getPlayer(a[0]);
                    if (target == null) {
                        s.sendMessage(Component.text("Player not found.", NamedTextColor.RED));
                        yield true;
                    }
                } else if (s instanceof Player p) {
                    target = p;
                } else {
                    s.sendMessage(Component.text("Specify a player: /spawn <player>", NamedTextColor.RED));
                    yield true;
                }
                if (!getConfig().contains("spawn.world")) {
                    s.sendMessage(Component.text("Spawn not set yet. Use /setspawn first.", NamedTextColor.YELLOW));
                    yield true;
                }
                World world = getServer().getWorld(getConfig().getString("spawn.world", "world"));
                if (world == null) {
                    s.sendMessage(Component.text("Spawn world not found.", NamedTextColor.RED));
                    yield true;
                }
                target.teleport(new Location(world,
                        getConfig().getDouble("spawn.x"),
                        getConfig().getDouble("spawn.y"),
                        getConfig().getDouble("spawn.z"),
                        (float) getConfig().getDouble("spawn.yaw"),
                        (float) getConfig().getDouble("spawn.pitch")));
                target.sendMessage(Component.text("Teleported to spawn!", NamedTextColor.GREEN));
                if (!s.getName().equals(target.getName()))
                    s.sendMessage(Component.text("Teleported " + target.getName() + " to spawn.", NamedTextColor.GREEN));
                yield true;
            }
            default -> false;
        };
    }

    @Override
    public List<String> onTabComplete(CommandSender s, Command c, String a, String[] args) {
        if (a.equalsIgnoreCase("spawn") && args.length == 1 && s.hasPermission("{LOWER}.others"))
            return getServer().getOnlinePlayers().stream()
                    .map(Player::getName)
                    .filter(n -> n.toLowerCase().startsWith(args[0].toLowerCase()))
                    .toList();
        return List.of();
    }
}
""".strip()

_SPAWN_YML = """
name: {NAME}
version: '1.0'
main: com.example.{LOWER}.{NAME}Plugin
api-version: '1.21'
description: Set the server spawn and teleport to it.
commands:
  setspawn:
    description: Set the server spawn point.
    usage: /setspawn
    permission: {LOWER}.setspawn
  spawn:
    description: Teleport to the server spawn.
    usage: /spawn [player]
    permission: {LOWER}.spawn
permissions:
  {LOWER}.setspawn:
    description: Set the spawn point.
    default: op
  {LOWER}.spawn:
    description: Teleport to spawn.
    default: true
  {LOWER}.others:
    description: Teleport other players to spawn.
    default: op
""".strip()

# ---------------------------------------------------------------------------

_JOINMSG_JAVA = r"""
package com.example.{LOWER};

import net.kyori.adventure.text.minimessage.MiniMessage;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;

public final class {NAME}Plugin extends JavaPlugin implements Listener {

    private final MiniMessage mm = MiniMessage.miniMessage();

    @Override
    public void onEnable() {
        saveDefaultConfig();
        getServer().getPluginManager().registerEvents(this, this);
        getLogger().info(getName() + " enabled.");
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent e) {
        String template = getConfig().getString(
                "join-message", "<green><bold>+</bold></green> <yellow><player></yellow> <gray>joined the server.</gray>");
        e.joinMessage(mm.deserialize(template,
                Placeholder.unparsed("player", e.getPlayer().getName())));
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent e) {
        String template = getConfig().getString(
                "quit-message", "<red><bold>-</bold></red> <yellow><player></yellow> <gray>left the server.</gray>");
        e.quitMessage(mm.deserialize(template,
                Placeholder.unparsed("player", e.getPlayer().getName())));
    }
}
""".strip()

_JOINMSG_YML = """
name: {NAME}
version: '1.0'
main: com.example.{LOWER}.{NAME}Plugin
api-version: '1.21'
description: Custom join and quit messages using MiniMessage formatting.
""".strip()

# ---------------------------------------------------------------------------

_ANNOUNCER_JAVA = r"""
package com.example.{LOWER};

import net.kyori.adventure.text.minimessage.MiniMessage;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

import java.util.List;

public final class {NAME}Plugin extends JavaPlugin {

    private final MiniMessage mm = MiniMessage.miniMessage();
    private BukkitTask task;
    private int index = 0;

    @Override
    public void onEnable() {
        getConfig().addDefault("interval-seconds", 60);
        getConfig().addDefault("prefix", "<gray>[<aqua>Announcement</aqua>]</gray> ");
        getConfig().addDefault("messages", List.of(
                "Welcome to the server! Type /help for commands.",
                "Join our Discord for updates and giveaways!",
                "Remember to follow the server rules."
        ));
        getConfig().options().copyDefaults(true);
        saveConfig();
        scheduleTask();
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public void onDisable() {
        if (task != null) task.cancel();
    }

    private void scheduleTask() {
        long ticks = getConfig().getLong("interval-seconds", 60) * 20L;
        List<String> messages = getConfig().getStringList("messages");
        String prefix = getConfig().getString("prefix", "");
        if (messages.isEmpty()) return;

        task = getServer().getScheduler().runTaskTimer(this, () -> {
            String raw = prefix + messages.get(index % messages.size());
            getServer().broadcast(mm.deserialize(raw));
            index++;
        }, ticks, ticks);
    }
}
""".strip()

_ANNOUNCER_YML = """
name: {NAME}
version: '1.0'
main: com.example.{LOWER}.{NAME}Plugin
api-version: '1.21'
description: Broadcasts timed announcements. Edit messages in config.yml.
""".strip()

# ---------------------------------------------------------------------------

_TPA_JAVA = r"""
package com.example.{LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

public final class {NAME}Plugin extends JavaPlugin implements CommandExecutor, TabCompleter {

    // requester UUID → (target UUID, expiry task)
    private final Map<UUID, UUID>       pending = new HashMap<>();
    private final Map<UUID, BukkitTask> timers  = new HashMap<>();

    private static final long TIMEOUT_TICKS = 30 * 20L; // 30 seconds

    @Override
    public void onEnable() {
        for (var cmd : List.of("tpa", "tpaccept", "tpdeny")) {
            var c = getCommand(cmd);
            if (c != null) { c.setExecutor(this); c.setTabCompleter(this); }
        }
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public boolean onCommand(CommandSender s, Command c, String l, String[] a) {
        if (!(s instanceof Player p)) {
            s.sendMessage(Component.text("Player only.", NamedTextColor.RED));
            return true;
        }
        if (!p.hasPermission("{LOWER}.use")) {
            p.sendMessage(Component.text("No permission.", NamedTextColor.RED));
            return true;
        }

        return switch (l.toLowerCase()) {
            case "tpa" -> {
                if (a.length < 1) { p.sendMessage(Component.text("Usage: /tpa <player>", NamedTextColor.RED)); yield true; }
                Player target = getServer().getPlayer(a[0]);
                if (target == null || target.equals(p)) {
                    p.sendMessage(Component.text("Player not found or invalid target.", NamedTextColor.RED));
                    yield true;
                }
                // Cancel any existing request from this player
                cancelRequest(p.getUniqueId());

                pending.put(p.getUniqueId(), target.getUniqueId());
                BukkitTask t = getServer().getScheduler().runTaskLater(this, () -> {
                    if (pending.remove(p.getUniqueId()) != null)
                        p.sendMessage(Component.text("Your teleport request to " + target.getName() + " expired.", NamedTextColor.GRAY));
                    timers.remove(p.getUniqueId());
                }, TIMEOUT_TICKS);
                timers.put(p.getUniqueId(), t);

                p.sendMessage(Component.text("Teleport request sent to " + target.getName() + ". Expires in 30s.", NamedTextColor.YELLOW));
                target.sendMessage(Component.text(p.getName() + " wants to teleport to you. ", NamedTextColor.YELLOW)
                        .append(Component.text("[Accept]", NamedTextColor.GREEN)
                                .clickEvent(ClickEvent.runCommand("/tpaccept")))
                        .append(Component.text(" "))
                        .append(Component.text("[Deny]", NamedTextColor.RED)
                                .clickEvent(ClickEvent.runCommand("/tpdeny"))));
                yield true;
            }
            case "tpaccept" -> {
                UUID requesterUUID = findRequester(p.getUniqueId());
                if (requesterUUID == null) {
                    p.sendMessage(Component.text("No pending teleport request.", NamedTextColor.YELLOW));
                    yield true;
                }
                Player requester = getServer().getPlayer(requesterUUID);
                cancelRequest(requesterUUID);
                if (requester == null) {
                    p.sendMessage(Component.text("That player is no longer online.", NamedTextColor.RED));
                    yield true;
                }
                requester.teleport(p.getLocation());
                requester.sendMessage(Component.text("Teleport accepted by " + p.getName() + "!", NamedTextColor.GREEN));
                p.sendMessage(Component.text("Accepted teleport request from " + requester.getName() + ".", NamedTextColor.GREEN));
                yield true;
            }
            case "tpdeny" -> {
                UUID requesterUUID = findRequester(p.getUniqueId());
                if (requesterUUID == null) {
                    p.sendMessage(Component.text("No pending teleport request.", NamedTextColor.YELLOW));
                    yield true;
                }
                Player requester = getServer().getPlayer(requesterUUID);
                cancelRequest(requesterUUID);
                p.sendMessage(Component.text("Teleport request denied.", NamedTextColor.RED));
                if (requester != null)
                    requester.sendMessage(Component.text(p.getName() + " denied your teleport request.", NamedTextColor.RED));
                yield true;
            }
            default -> false;
        };
    }

    private UUID findRequester(UUID targetId) {
        return pending.entrySet().stream()
                .filter(e -> e.getValue().equals(targetId))
                .map(Map.Entry::getKey)
                .findFirst().orElse(null);
    }

    private void cancelRequest(UUID requesterId) {
        pending.remove(requesterId);
        BukkitTask t = timers.remove(requesterId);
        if (t != null) t.cancel();
    }

    @Override
    public List<String> onTabComplete(CommandSender s, Command c, String a, String[] args) {
        if (a.equalsIgnoreCase("tpa") && args.length == 1)
            return getServer().getOnlinePlayers().stream()
                    .map(Player::getName)
                    .filter(n -> n.toLowerCase().startsWith(args[0].toLowerCase()))
                    .toList();
        return List.of();
    }
}
""".strip()

_TPA_YML = """
name: {NAME}
version: '1.0'
main: com.example.{LOWER}.{NAME}Plugin
api-version: '1.21'
description: Player teleport requests with /tpa, /tpaccept, and /tpdeny.
commands:
  tpa:
    description: Send a teleport request to a player.
    usage: /tpa <player>
    permission: {LOWER}.use
  tpaccept:
    description: Accept an incoming teleport request.
    usage: /tpaccept
    permission: {LOWER}.use
  tpdeny:
    description: Deny an incoming teleport request.
    usage: /tpdeny
    permission: {LOWER}.use
permissions:
  {LOWER}.use:
    description: Use TPA commands.
    default: true
""".strip()

# ---------------------------------------------------------------------------

_GOD_JAVA = r"""
package com.example.{LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.GameMode;
import org.bukkit.attribute.Attribute;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

public final class {NAME}Plugin extends JavaPlugin implements CommandExecutor, TabCompleter, Listener {

    private final Set<UUID> godPlayers = new HashSet<>();

    @Override
    public void onEnable() {
        var cmd = getCommand("god");
        if (cmd != null) { cmd.setExecutor(this); cmd.setTabCompleter(this); }
        getServer().getPluginManager().registerEvents(this, this);
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public boolean onCommand(CommandSender s, Command c, String l, String[] a) {
        if (!s.hasPermission("{LOWER}.use")) {
            s.sendMessage(Component.text("No permission.", NamedTextColor.RED));
            return true;
        }
        Player target;
        if (a.length >= 1) {
            if (!s.hasPermission("{LOWER}.others")) {
                s.sendMessage(Component.text("You can't toggle god mode for others.", NamedTextColor.RED));
                return true;
            }
            target = getServer().getPlayer(a[0]);
            if (target == null) {
                s.sendMessage(Component.text("Player not found.", NamedTextColor.RED));
                return true;
            }
        } else if (s instanceof Player p) {
            target = p;
        } else {
            s.sendMessage(Component.text("Specify a player: /god <player>", NamedTextColor.RED));
            return true;
        }

        if (godPlayers.remove(target.getUniqueId())) {
            target.sendMessage(Component.text("God mode disabled.", NamedTextColor.RED));
            if (!s.getName().equals(target.getName()))
                s.sendMessage(Component.text("Disabled god mode for " + target.getName() + ".", NamedTextColor.RED));
        } else {
            godPlayers.add(target.getUniqueId());
            target.sendMessage(Component.text("God mode enabled.", NamedTextColor.GOLD));
            if (!s.getName().equals(target.getName()))
                s.sendMessage(Component.text("Enabled god mode for " + target.getName() + ".", NamedTextColor.GOLD));
        }
        return true;
    }

    @EventHandler
    public void onDamage(EntityDamageEvent e) {
        if (e.getEntity() instanceof Player p && godPlayers.contains(p.getUniqueId()))
            e.setCancelled(true);
    }

    @Override
    public List<String> onTabComplete(CommandSender s, Command c, String a, String[] args) {
        if (args.length == 1 && s.hasPermission("{LOWER}.others"))
            return getServer().getOnlinePlayers().stream()
                    .map(Player::getName)
                    .filter(n -> n.toLowerCase().startsWith(args[0].toLowerCase()))
                    .toList();
        return List.of();
    }
}
""".strip()

_GOD_YML = """
name: {NAME}
version: '1.0'
main: com.example.{LOWER}.{NAME}Plugin
api-version: '1.21'
description: Toggle invincibility (god mode) for yourself or other players.
commands:
  god:
    description: Toggle god mode.
    usage: /god [player]
    permission: {LOWER}.use
permissions:
  {LOWER}.use:
    description: Toggle your own god mode.
    default: op
  {LOWER}.others:
    description: Toggle god mode for other players.
    default: op
""".strip()

# ---------------------------------------------------------------------------
# Preset catalog
# ---------------------------------------------------------------------------

PRESETS: list[dict] = [
    {
        "id":       "heal",
        "name":     "Heal",
        "desc":     "Restores a player to full health, hunger, and saturation. Supports healing others with a separate permission.",
        "icon":     "💊",
        "tags":     ["commands", "admin", "utility"],
        "commands": ["/heal [player]"],
        "java":     _HEAL_JAVA,
        "yml":      _HEAL_YML,
    },
    {
        "id":       "home",
        "name":     "Home",
        "desc":     "Per-player home point. Set your home, teleport back to it, or delete it. Stored in config.yml.",
        "icon":     "🏠",
        "tags":     ["commands", "teleport", "utility"],
        "commands": ["/sethome", "/home", "/delhome"],
        "java":     _HOME_JAVA,
        "yml":      _HOME_YML,
    },
    {
        "id":       "spawn",
        "name":     "Spawn",
        "desc":     "Set a global server spawn point and teleport players to it. Admins can teleport other players.",
        "icon":     "🌐",
        "tags":     ["commands", "teleport", "admin"],
        "commands": ["/setspawn", "/spawn [player]"],
        "java":     _SPAWN_JAVA,
        "yml":      _SPAWN_YML,
    },
    {
        "id":       "joinmsg",
        "name":     "Join Messages",
        "desc":     "Replaces default join/quit messages with custom MiniMessage-formatted text. Supports <player> placeholder.",
        "icon":     "💬",
        "tags":     ["events", "chat", "cosmetic"],
        "commands": [],
        "java":     _JOINMSG_JAVA,
        "yml":      _JOINMSG_YML,
    },
    {
        "id":       "announcer",
        "name":     "Announcer",
        "desc":     "Broadcasts a rotating list of server announcements on a configurable timer. Edit messages in config.yml.",
        "icon":     "📢",
        "tags":     ["events", "admin", "utility"],
        "commands": [],
        "java":     _ANNOUNCER_JAVA,
        "yml":      _ANNOUNCER_YML,
    },
    {
        "id":       "tpa",
        "name":     "TPA",
        "desc":     "Teleport request system. Send a request, accept or deny it. Requests expire after 30 seconds automatically.",
        "icon":     "✈️",
        "tags":     ["commands", "teleport", "social"],
        "commands": ["/tpa <player>", "/tpaccept", "/tpdeny"],
        "java":     _TPA_JAVA,
        "yml":      _TPA_YML,
    },
    {
        "id":       "god",
        "name":     "God Mode",
        "desc":     "Toggle invincibility for yourself or another player. God mode cancels all incoming damage events.",
        "icon":     "⭐",
        "tags":     ["commands", "admin", "utility"],
        "commands": ["/god [player]"],
        "java":     _GOD_JAVA,
        "yml":      _GOD_YML,
    },
]

_PRESET_BY_ID: dict[str, dict] = {p["id"]: p for p in PRESETS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_preset(preset_id: str) -> Optional[dict]:
    """Return preset dict for the given id, or None if not found."""
    return _PRESET_BY_ID.get(preset_id)


def preset_catalog() -> list[dict]:
    """Return the preset catalog with source fields stripped (for API responses)."""
    return [
        {k: v for k, v in p.items() if k not in ("java", "yml")}
        for p in PRESETS
    ]


def build_preset(
    preset_id: str,
    plugin_name: str,
    paper_profile: str = DEFAULT_PAPER_PROFILE,
) -> bytes:
    """
    Compile a preset and return the JAR as bytes.

    plugin_name : raw string from the user — will be converted to PascalCase
                  for the class name and lowercase for the package.
    Raises ValueError if the preset_id is unknown.
    Raises RuntimeError (from build_jar) on compilation failure.
    """
    preset = get_preset(preset_id)
    if preset is None:
        raise ValueError(f"Unknown preset id: {preset_id!r}")

    pascal = _to_pascal(plugin_name)
    lower  = _to_lower(plugin_name)

    java_src = _fmt(preset["java"], pascal, lower)
    yml_src  = _fmt(preset["yml"],  pascal, lower)

    # Wrap in markdown fences so build_jar can extract them
    response_text = f"```java\n{java_src}\n```\n\n```yaml\n{yml_src}\n```"

    return build_jar(response_text, plugin_name=pascal, paper_profile=paper_profile)
