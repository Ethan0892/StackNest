// FEATURE: click_handler
// Handles inventory click and close events for menus created with MenuHolder.
// Must be used together with the inventory_holder feature block.
//
// Required imports:
//   import org.bukkit.entity.Player;
//   import org.bukkit.event.EventHandler;
//   import org.bukkit.event.inventory.InventoryClickEvent;
//   import org.bukkit.event.inventory.InventoryCloseEvent;
//
// Main class must implement Listener and call:
//   getServer().getPluginManager().registerEvents(this, this);

@EventHandler
public void onInventoryClick(InventoryClickEvent event) {
    // Only handle clicks in OUR inventories
    if (!(event.getInventory().getHolder() instanceof MenuHolder holder)) return;

    // Always cancel to prevent item removal / duplication
    event.setCancelled(true);

    if (!(event.getWhoClicked() instanceof org.bukkit.entity.Player player)) return;

    int slot = event.getRawSlot();
    // rawSlot < inventory.size() means the click is in the top inventory (not the player's hotbar)
    if (slot < 0 || slot >= event.getInventory().getSize()) return;

    // TODO: implement — dispatch on holder.getMenuId() and slot
    // Example:
    // switch (holder.getMenuId()) {
    //     case "main" -> handleMainMenuClick(player, slot);
    //     case "confirm" -> handleConfirmClick(player, slot);
    // }
}

@EventHandler
public void onInventoryClose(InventoryCloseEvent event) {
    if (!(event.getInventory().getHolder() instanceof MenuHolder holder)) return;
    // TODO: implement — cleanup on close if needed (e.g. cancel pending tasks)
}
