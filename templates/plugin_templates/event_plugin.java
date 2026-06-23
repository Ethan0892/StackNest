package com.example.${PLUGIN_NAME_LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * ${PLUGIN_NAME} — Paper 1.21 event listener plugin skeleton.
 *
 * Key rules for Paper 1.21 event listeners:
 * - Always use Adventure API (Component) for messages — never legacy ChatColor strings.
 * - Use @EventHandler with explicit EventPriority when order matters.
 * - Implement Listener in a dedicated class OR the main plugin class.
 * - Register with: getServer().getPluginManager().registerEvents(listenerInstance, plugin);
 * - Cancellable events: check event.isCancelled() before acting at NORMAL priority.
 */
public final class ${PLUGIN_NAME}Plugin extends JavaPlugin implements Listener {

    @Override
    public void onEnable() {
        // Register THIS class as an event listener
        getServer().getPluginManager().registerEvents(this, this);
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public void onDisable() {
        // Bukkit automatically unregisters listeners on disable — no manual cleanup needed
        getLogger().info(getName() + " disabled.");
    }

    /**
     * Fires when a player joins the server.
     * EventPriority.MONITOR = last to execute, read-only observation.
     */
    @EventHandler(priority = EventPriority.NORMAL)
    public void onPlayerJoin(PlayerJoinEvent event) {
        var player = event.getPlayer();
        // Use Component for the join message — Adventure API
        event.joinMessage(
            Component.text(player.getName() + " joined the server!", NamedTextColor.GREEN)
        );
    }

    /**
     * Fires when a player leaves the server.
     */
    @EventHandler(priority = EventPriority.NORMAL)
    public void onPlayerQuit(PlayerQuitEvent event) {
        var player = event.getPlayer();
        event.quitMessage(
            Component.text(player.getName() + " left the server.", NamedTextColor.GRAY)
        );
    }

    /**
     * Fires when a player dies.
     * ignoreCancelled = true means this handler is skipped if a higher-priority
     * handler already cancelled the event.
     */
    @EventHandler(priority = EventPriority.NORMAL, ignoreCancelled = true)
    public void onPlayerDeath(PlayerDeathEvent event) {
        var player = event.getEntity();
        // Example: broadcast death coordinates
        player.getWorld().getPlayers().forEach(p ->
            p.sendMessage(
                Component.text(player.getName() + " died at ")
                    .append(Component.text(
                        "(" + player.getBlockX() + ", " + player.getBlockY() + ", " + player.getBlockZ() + ")",
                        NamedTextColor.YELLOW
                    ))
            )
        );
    }
}
