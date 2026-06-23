// FEATURE: config_manager
// config.yml handler with reload support.
// config.yml is created from src/main/resources/config.yml on first run.
//
// In onEnable():
//   saveDefaultConfig();
//   loadSettings();
//
// In your /reload handler:
//   reloadConfig();
//   loadSettings();

// Cache frequently-used values to avoid repeated getString() calls
private String welcomeMessage;
private int    maxCount;
private boolean featureEnabled;

private void loadSettings() {
    var cfg = getConfig();
    welcomeMessage  = cfg.getString("messages.welcome", "<green>Welcome!</green>");
    maxCount        = cfg.getInt("settings.max-count",  10);
    featureEnabled  = cfg.getBoolean("settings.enabled", true);
    // TODO: implement — cache any additional config keys your plugin needs
}

// Reading pattern (use cached fields above instead of calling getConfig() repeatedly):
//   player.sendMessage(MiniMessage.miniMessage().deserialize(welcomeMessage));

// Writing pattern (persists to disk immediately):
private void saveValue(String path, Object value) {
    getConfig().set(path, value);
    saveConfig();
}
