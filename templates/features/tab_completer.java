// FEATURE: tab_completer
// TabCompleter for commands registered with setTabCompleter(this) in onEnable().
//
// Required imports:
//   import java.util.List;
//   import java.util.stream.Stream;
//
// Main class must implement TabCompleter.

@Override
public java.util.List<String> onTabComplete(org.bukkit.command.CommandSender sender,
                                             org.bukkit.command.Command command,
                                             String label,
                                             String[] args) {
    if (args.length == 1) {
        // Top-level sub-commands — filter by what the player has typed so far
        return java.util.List.of("help", "info", "reload")
            .stream()
            .filter(s -> s.startsWith(args[0].toLowerCase()))
            .toList();
    }

    // TODO: implement — return context-aware completions for deeper argument positions
    // Example for args.length == 2 when args[0] == "give":
    // if ("give".equalsIgnoreCase(args[0])) {
    //     return getServer().getOnlinePlayers().stream()
    //         .map(org.bukkit.entity.Player::getName)
    //         .filter(n -> n.toLowerCase().startsWith(args[1].toLowerCase()))
    //         .toList();
    // }

    return java.util.List.of();
}
