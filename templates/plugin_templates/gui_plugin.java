package com.example.${PLUGIN_NAME_LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Bukkit;
import org.bukkit.Material;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.event.inventory.InventoryCloseEvent;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.InventoryHolder;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.List;

/**
 * ${PLUGIN_NAME} — Paper 1.21 GUI / inventory menu plugin skeleton.
 *
 * Paper 1.21 GUI rules:
 * - Implement InventoryHolder in a separate class for each distinct GUI.
 * - Check getInventory().getHolder() instanceof YourHolder in InventoryClickEvent
 *   to ensure you only handle clicks in YOUR inventory.
 * - Always event.setCancelled(true) in InventoryClickEvent to prevent item removal.
 * - Never call Inventory methods from an async thread.
 * - Use Component (Adventure) for all inventory titles — not ChatColor strings.
 */
public final class ${PLUGIN_NAME}Plugin extends JavaPlugin implements CommandExecutor, Listener {

    @Override
    public void onEnable() {
        var cmd = getCommand("openmenu");
        if (cmd != null) cmd.setExecutor(this);
        getServer().getPluginManager().registerEvents(this, this);
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public void onDisable() {
        getLogger().info(getName() + " disabled.");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage(Component.text("Players only.", NamedTextColor.RED));
            return true;
        }
        openMainMenu(player);
        return true;
    }

    /**
     * Opens the main GUI for the player.
     */
    private void openMainMenu(Player player) {
        // Inventory size must be a multiple of 9 (9, 18, 27, 36, 45, 54)
        MainMenuHolder holder = new MainMenuHolder();
        Inventory inv = Bukkit.createInventory(
            holder,
            27,
            Component.text("${PLUGIN_NAME} Menu", NamedTextColor.DARK_PURPLE)
        );

        // Slot 13 — centre of 3-row chest
        inv.setItem(13, makeItem(Material.NETHER_STAR, "Click me!", List.of("Does something cool")));

        // Fill empty slots with grey glass panes (standard GUI border pattern)
        ItemStack filler = makeItem(Material.GRAY_STAINED_GLASS_PANE, " ", List.of());
        for (int i = 0; i < inv.getSize(); i++) {
            if (inv.getItem(i) == null) inv.setItem(i, filler);
        }

        holder.setInventory(inv);
        player.openInventory(inv);
    }

    /** Handles all inventory click events — filter by InventoryHolder type. */
    @EventHandler
    public void onInventoryClick(InventoryClickEvent event) {
        if (!(event.getInventory().getHolder() instanceof MainMenuHolder)) return;

        // Always cancel to stop item removal
        event.setCancelled(true);

        if (!(event.getWhoClicked() instanceof Player player)) return;
        if (event.getCurrentItem() == null) return;

        // React to the specific slot clicked
        if (event.getSlot() == 13) {
            player.sendMessage(Component.text("You clicked the star!", NamedTextColor.GOLD));
            player.closeInventory();
        }
    }

    /** Optional: handle inventory close (save state, etc.) */
    @EventHandler
    public void onInventoryClose(InventoryCloseEvent event) {
        if (!(event.getInventory().getHolder() instanceof MainMenuHolder)) return;
        // e.g. save any changes made in the GUI
    }

    // ── Helper ──────────────────────────────────────────────────────────────

    private ItemStack makeItem(Material material, String name, List<String> loreLines) {
        ItemStack item = new ItemStack(material);
        ItemMeta meta = item.getItemMeta();
        if (meta != null) {
            meta.displayName(Component.text(name, NamedTextColor.WHITE));
            meta.lore(loreLines.stream()
                .map(l -> Component.text(l, NamedTextColor.GRAY))
                .toList());
            item.setItemMeta(meta);
        }
        return item;
    }

    // ── InventoryHolder implementation ──────────────────────────────────────

    /**
     * Identifies this plugin's GUI so InventoryClickEvent can filter correctly.
     * One holder class per distinct GUI screen.
     */
    public static class MainMenuHolder implements InventoryHolder {
        private Inventory inventory;

        @Override
        public Inventory getInventory() { return inventory; }

        public void setInventory(Inventory inventory) { this.inventory = inventory; }
    }
}
