package com.example.${PLUGIN_NAME_LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitRunnable;
import org.bukkit.scheduler.BukkitTask;

/**
 * ${PLUGIN_NAME} — Paper 1.21 scheduler plugin skeleton.
 *
 * Paper 1.21 scheduler rules:
 * - Use BukkitRunnable or Bukkit.getScheduler() — NOT Folia APIs unless plugin.yml declares folia-supported: true.
 * - BukkitRunnable.runTaskTimer(plugin, delayTicks, periodTicks)
 *   - 1 second = 20 ticks  |  1 minute = 1200 ticks
 * - Always cancel the task in onDisable() to prevent memory leaks.
 * - Async tasks (runTaskTimerAsynchronously) must NOT touch Bukkit API — use sync tasks to act on results.
 * - Never call Bukkit.broadcastMessage(String) — use Component broadcasting.
 */
public final class ${PLUGIN_NAME}Plugin extends JavaPlugin {

    // Keep a reference so we can cancel in onDisable
    private BukkitTask repeatingTask;

    @Override
    public void onEnable() {
        saveDefaultConfig();

        long intervalTicks = getConfig().getLong("interval-ticks", 6000L); // default 5 min

        // Inline BukkitRunnable — runs on the main server thread
        repeatingTask = new BukkitRunnable() {
            @Override
            public void run() {
                broadcastAnnouncement();
            }
        }.runTaskTimer(
            this,
            intervalTicks,   // initial delay before first run
            intervalTicks    // period between subsequent runs
        );

        getLogger().info(getName() + " enabled. Interval: " + intervalTicks + " ticks.");
    }

    @Override
    public void onDisable() {
        // Cancel all tasks registered by this plugin
        if (repeatingTask != null && !repeatingTask.isCancelled()) {
            repeatingTask.cancel();
        }
        getLogger().info(getName() + " disabled.");
    }

    /**
     * Run a one-shot delayed task (fires once after delayTicks ticks).
     * Demonstrates the alternative to BukkitRunnable.
     */
    private void scheduleDelayedAction(long delayTicks) {
        getServer().getScheduler().runTaskLater(this, () -> {
            getLogger().info("Delayed task fired after " + delayTicks + " ticks.");
        }, delayTicks);
    }

    /**
     * Broadcast an announcement to all online players using Adventure API.
     * Never use Bukkit.broadcastMessage(String).
     */
    private void broadcastAnnouncement() {
        String msg = getConfig().getString("message", "Server announcement!");
        Component component = Component.text("[Announce] ", NamedTextColor.GOLD)
            .append(Component.text(msg, NamedTextColor.WHITE));

        getServer().getOnlinePlayers().forEach(player -> player.sendMessage(component));
    }
}
