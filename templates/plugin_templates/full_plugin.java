package com.example.${PLUGIN_NAME_LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.configuration.file.FileConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.plugin.java.JavaPlugin;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * ${PLUGIN_NAME} — Full Paper 1.21 plugin skeleton.
 */
public final class ${PLUGIN_NAME}Plugin extends JavaPlugin implements CommandExecutor, Listener {

    // ConcurrentHashMap for thread-safe reads; writes still happen on main thread
    private final ConcurrentHashMap<UUID, String> dataMap = new ConcurrentHashMap<>();

    @Override
    public void onEnable() {
        saveDefaultConfig();
        var cmd = getCommand("myplugin");
        if (cmd != null) cmd.setExecutor(this);
        getServer().getPluginManager().registerEvents(this, this);
        getLogger().info(getName() + " v" + getDescription().getVersion() + " enabled.");
    }

    @Override
    public void onDisable() {
        getLogger().info(getName() + " disabled.");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage(Component.text("Player-only command.", NamedTextColor.RED));
            return true;
        }
        if (!player.hasPermission("myplugin.use")) {
            player.sendMessage(Component.text("No permission.", NamedTextColor.RED));
            return true;
        }

        // ── Safe async pattern ────────────────────────────────────────────────
        // Snapshot any Bukkit values needed inside the async block BEFORE going async.
        // Never call player.anything() or any Bukkit API from inside runTaskAsynchronously.
        final UUID playerId = player.getUniqueId();
        final String playerName = player.getName();

        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            // ✓ Safe: pure Java / file I/O / DB only — no Bukkit API here
            String result = expensiveFileRead(playerName);

            // ✓ Dispatch ALL Bukkit API calls back to the main thread
            getServer().getScheduler().runTask(this, () -> {
                Player p = Bukkit.getPlayer(playerId);
                if (p != null && p.isOnline()) {
                    p.sendMessage(Component.text("Result: " + result, NamedTextColor.GREEN));
                }
            });
        });

        return true;
    }

    // ── Race-safe map operations ──────────────────────────────────────────────
    // Use putIfAbsent instead of containsKey+put to avoid time-of-check/time-of-use races.
    private boolean registerPlayer(UUID id, String name) {
        return dataMap.putIfAbsent(id, name) == null; // returns true only if actually inserted
    }

    private String expensiveFileRead(String key) {
        // Simulate file I/O — safe to call from async thread
        FileConfiguration config = getConfig();
        return config.getString("message", "Hello from StackNest!");
    }
}
