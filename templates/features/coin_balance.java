// FEATURE: coin_balance
// Custom in-plugin currency stored in PersistentDataContainer (no Vault required).
// Data survives server restarts and is attached to the player entity.
//
// Required imports:
//   import org.bukkit.NamespacedKey;
//   import org.bukkit.persistence.PersistentDataType;
//
// Add field to main class:
//   private NamespacedKey coinKey;
//
// In onEnable():
//   coinKey = new NamespacedKey(this, "coins");

private long getCoins(org.bukkit.entity.Player player) {
    return player.getPersistentDataContainer()
                 .getOrDefault(coinKey, PersistentDataType.LONG, 0L);
}

private void setCoins(org.bukkit.entity.Player player, long amount) {
    player.getPersistentDataContainer()
          .set(coinKey, PersistentDataType.LONG, Math.max(0L, amount));
}

private void addCoins(org.bukkit.entity.Player player, long amount) {
    setCoins(player, getCoins(player) + amount);
}

/**
 * Deducts {@code cost} coins from the player's balance.
 * Returns true if the player had enough — false if insufficient funds.
 */
private boolean deductCoins(org.bukkit.entity.Player player, long cost) {
    long balance = getCoins(player);
    if (balance < cost) return false;
    setCoins(player, balance - cost);
    return true;
}
