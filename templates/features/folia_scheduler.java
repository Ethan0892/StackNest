// FEATURE: folia_scheduler
// Folia-compatible scheduler using GlobalRegionScheduler for non-entity tasks.
// Also works on non-Folia Paper servers via try/catch fallback.
//
// Required imports: none extra (GlobalRegionScheduler is in Paper API)
//
// Add to main class (Object to avoid hard compile dependency on Folia type):
//   private Object foliaTask;
//
// Call startGlobalTask() from onEnable(), stopGlobalTask() from onDisable().
//
// NOTE: For entity/block-specific work use RegionScheduler or EntityScheduler.
//       GlobalRegionScheduler is only for tasks with no region affinity
//       (e.g. broadcasts, global leaderboard updates, connection pooling).

private Object foliaTask;

private void startGlobalTask(long initialDelayTicks, long periodTicks) {
    try {
        // Folia path — GlobalRegionScheduler
        foliaTask = getServer().getGlobalRegionScheduler().runAtFixedRate(
            this,
            scheduledTask -> {
                // TODO: implement — periodic global task body
            },
            initialDelayTicks,
            periodTicks
        );
    } catch (NoSuchMethodError | UnsupportedOperationException ignored) {
        // Non-Folia Paper fallback
        foliaTask = getServer().getScheduler().runTaskTimer(this, () -> {
            // TODO: implement — same logic as Folia path above
        }, initialDelayTicks, periodTicks);
    }
}

private void stopGlobalTask() {
    if (foliaTask == null) return;
    if (foliaTask instanceof io.papermc.paper.threadedregions.scheduler.ScheduledTask st) {
        st.cancel();
    } else if (foliaTask instanceof org.bukkit.scheduler.BukkitTask bt) {
        bt.cancel();
    }
    foliaTask = null;
}

/**
 * Schedule a one-off task on the region that owns the given location.
 * Falls back to runTask() on non-Folia servers.
 */
private void runOnRegion(org.bukkit.Location location, Runnable task) {
    try {
        getServer().getRegionScheduler().run(this, location, st -> task.run());
    } catch (NoSuchMethodError | UnsupportedOperationException ignored) {
        getServer().getScheduler().runTask(this, task);
    }
}
