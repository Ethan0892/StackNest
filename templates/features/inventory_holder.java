// FEATURE: inventory_holder
// InventoryHolder inner class for safe GUI management.
// Using a typed holder prevents handling clicks in unrelated inventories.
//
// Required imports:
//   import net.kyori.adventure.text.Component;
//   import org.bukkit.Bukkit;
//   import org.bukkit.inventory.Inventory;
//   import org.bukkit.inventory.InventoryHolder;

/** Typed holder for all menus created by this plugin. */
private static final class MenuHolder implements InventoryHolder {

    private Inventory inventory;
    /** Identifies which menu this is (e.g. "main", "confirm", "shop"). */
    private final String menuId;

    MenuHolder(String menuId) {
        this.menuId = menuId;
    }

    @Override
    public Inventory getInventory() {
        return inventory;
    }

    void setInventory(Inventory inventory) {
        this.inventory = inventory;
    }

    String getMenuId() {
        return menuId;
    }
}

/**
 * Opens a named inventory menu for the player.
 * @param player  the player to open the menu for
 * @param menuId  identifier used in click handling (e.g. "main", "shop")
 * @param title   Component title displayed at the top of the inventory
 * @param rows    number of rows (1-6); size = rows * 9
 */
private Inventory openMenu(org.bukkit.entity.Player player,
                            String menuId,
                            net.kyori.adventure.text.Component title,
                            int rows) {
    MenuHolder holder = new MenuHolder(menuId);
    Inventory inv = Bukkit.createInventory(holder, rows * 9, title);
    // TODO: implement — fill inv with items before opening
    holder.setInventory(inv);
    player.openInventory(inv);
    return inv;
}
