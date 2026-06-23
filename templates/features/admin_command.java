// FEATURE: admin_command
// Admin sub-command handler with permission gate.
// Register this command in plugin.yml and call setExecutor(this) in onEnable().
//
// plugin.yml entry:
//   commands:
//     <pluginname>admin:
//       description: Admin command
//       permission: <pluginname>.admin
//       usage: /<command> <sub-command> [args...]

private boolean handleAdminCommand(org.bukkit.command.CommandSender sender, String[] args) {
    if (!sender.hasPermission(getName().toLowerCase() + ".admin")) {
        sender.sendMessage(
            net.kyori.adventure.text.Component.text(
                "You don't have permission to use this command.",
                net.kyori.adventure.text.format.NamedTextColor.RED
            )
        );
        return true;
    }

    if (args.length == 0) {
        sender.sendMessage(
            net.kyori.adventure.text.Component.text(
                "Usage: /" + getName().toLowerCase() + "admin <sub-command> [args...]",
                net.kyori.adventure.text.format.NamedTextColor.YELLOW
            )
        );
        return true;
    }

    // TODO: implement — switch on args[0] for each admin sub-command
    // Example:
    // switch (args[0].toLowerCase()) {
    //     case "reload" -> { reloadConfig(); sender.sendMessage(...); }
    //     case "give"   -> handleGive(sender, args);
    //     default       -> sender.sendMessage(Component.text("Unknown sub-command: " + args[0]));
    // }

    return true;
}
