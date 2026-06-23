package com.example.${PLUGIN_NAME_LOWER};

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.List;

/**
 * ${PLUGIN_NAME} — Paper 1.21 command plugin skeleton.
 *
 * plugin.yml must declare:
 *   commands:
 *     mycommand:
 *       description: Does something
 *       permission: ${PLUGIN_NAME_LOWER}.use
 *       usage: /<command> [args]
 */
public final class ${PLUGIN_NAME}Plugin extends JavaPlugin implements CommandExecutor, TabCompleter {

    @Override
    public void onEnable() {
        var cmd = getCommand("mycommand");
        if (cmd != null) {
            cmd.setExecutor(this);
            cmd.setTabCompleter(this);
        }
        getLogger().info(getName() + " enabled.");
    }

    @Override
    public void onDisable() {
        getLogger().info(getName() + " disabled.");
    }

    /**
     * Called when /mycommand is executed.
     * Always return true — return false only to print the usage string from plugin.yml.
     */
    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        // Restrict to players
        if (!(sender instanceof Player player)) {
            sender.sendMessage(Component.text("Only players can use this command.", NamedTextColor.RED));
            return true;
        }

        // Permission check — node declared in plugin.yml
        if (!player.hasPermission("${PLUGIN_NAME_LOWER}.use")) {
            player.sendMessage(Component.text("You don't have permission to use this.", NamedTextColor.RED));
            return true;
        }

        // Handle sub-commands via args[0] if needed
        if (args.length == 0) {
            // No arguments — perform default action
            player.sendMessage(Component.text("Command executed!", NamedTextColor.GREEN));
            return true;
        }

        switch (args[0].toLowerCase()) {
            case "help" -> player.sendMessage(Component.text("Help text here.", NamedTextColor.YELLOW));
            case "reload" -> {
                if (!player.hasPermission("${PLUGIN_NAME_LOWER}.admin")) {
                    player.sendMessage(Component.text("No permission.", NamedTextColor.RED));
                    return true;
                }
                reloadConfig();
                player.sendMessage(Component.text("Config reloaded.", NamedTextColor.GREEN));
            }
            default -> player.sendMessage(Component.text("Unknown argument: " + args[0], NamedTextColor.RED));
        }

        return true;
    }

    /**
     * Tab-completion for /mycommand.
     * Return null to let Bukkit use default player-name completion.
     */
    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String label, String[] args) {
        if (args.length == 1) {
            return List.of("help", "reload");
        }
        return List.of();
    }
}
