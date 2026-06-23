// FEATURE: player_command
// Standard player-only command with permission check.
// Register in plugin.yml and call setExecutor(this) from onEnable().
//
// plugin.yml entry:
//   commands:
//     <commandname>:
//       description: Player command
//       permission: <pluginname>.use
//       usage: /<command> [args...]

@Override
public boolean onCommand(org.bukkit.command.CommandSender sender,
                         org.bukkit.command.Command command,
                         String label,
                         String[] args) {
    // Restrict to players (not console)
    if (!(sender instanceof org.bukkit.entity.Player player)) {
        sender.sendMessage(
            net.kyori.adventure.text.Component.text(
                "This command can only be used by players.",
                net.kyori.adventure.text.format.NamedTextColor.RED
            )
        );
        return true;
    }

    // Permission check — defined in plugin.yml
    if (!player.hasPermission(getName().toLowerCase() + ".use")) {
        player.sendMessage(
            net.kyori.adventure.text.Component.text(
                "You don't have permission to use this command.",
                net.kyori.adventure.text.format.NamedTextColor.RED
            )
        );
        return true;
    }

    // TODO: implement — main command logic goes here
    // Use args[] for sub-commands or arguments.
    // Return true always (return false only to print plugin.yml usage string).

    return true;
}
