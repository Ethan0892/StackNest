// FEATURE: vault_hook
// Integrates Vault economy API so the plugin can read and modify player balances.
//
// Required imports:
//   import net.milkbowl.vault.economy.Economy;
//   import org.bukkit.plugin.RegisteredServiceProvider;
//
// plugin.yml:  softdepend: [Vault]
//
// Add field to main class:
//   private Economy econ;
//
// Call hookVault() from onEnable() and abort if it returns false.

private boolean hookVault() {
    if (getServer().getPluginManager().getPlugin("Vault") == null) {
        getLogger().warning("Vault not found — economy features disabled.");
        return false;
    }
    RegisteredServiceProvider<Economy> rsp =
        getServer().getServicesManager().getRegistration(Economy.class);
    if (rsp == null) {
        getLogger().warning("No economy provider registered with Vault.");
        return false;
    }
    econ = rsp.getProvider();
    return econ != null;
}

// Convenience wrappers — use these instead of calling econ directly:

private double getBalance(org.bukkit.OfflinePlayer player) {
    return econ.getBalance(player);
}

/** Returns true if the player had enough balance and it was deducted. */
private boolean charge(org.bukkit.OfflinePlayer player, double amount) {
    if (!econ.has(player, amount)) return false;
    econ.withdrawPlayer(player, amount);
    return true;
}

private void pay(org.bukkit.OfflinePlayer player, double amount) {
    econ.depositPlayer(player, amount);
}
