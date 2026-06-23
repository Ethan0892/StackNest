// FEATURE: repeating_task
// BukkitScheduler repeating task — runs on the main thread every N ticks.
// Safe for all Bukkit API calls. NOT safe for blocking I/O (use runTaskAsynchronously for that).
//
// Required imports: none beyond org.bukkit.scheduler.BukkitTask (included in Paper)
//
// Add to main class:
//   private org.bukkit.scheduler.BukkitTask repeatingTask;
//
// Call startRepeatingTask() from onEnable().
// Call stopRepeatingTask() from onDisable().

private org.bukkit.scheduler.BukkitTask repeatingTask;

/**
 * Starts the repeating task.
 * @param delayTicks  ticks before first run (20 ticks = 1 second)
 * @param periodTicks ticks between each run
 */
private void startRepeatingTask(long delayTicks, long periodTicks) {
    repeatingTask = getServer().getScheduler().runTaskTimer(this, () -> {
        // TODO: implement — code here runs on main thread every periodTicks
        // Safe to call: player.sendMessage(), Bukkit.broadcast(), inventory ops, etc.
        // NOT safe: Thread.sleep(), blocking file/DB reads (use runTaskAsynchronously).
    }, delayTicks, periodTicks);
}

private void stopRepeatingTask() {
    if (repeatingTask != null) {
        repeatingTask.cancel();
        repeatingTask = null;
    }
}

/**
 * Run a one-off async task (e.g. database write) then dispatch the result
 * back to the main thread for Bukkit API calls.
 * Pattern: snapshot Bukkit values → go async → dispatch back to main thread.
 */
private void runAsync(Runnable asyncWork, Runnable mainThreadCallback) {
    getServer().getScheduler().runTaskAsynchronously(this, () -> {
        asyncWork.run();
        getServer().getScheduler().runTask(this, mainThreadCallback);
    });
}
