// FEATURE: sqlite_manager
// Full SQLite database manager using JDBC (bundled in Paper's JVM classpath).
// No external driver dependency required.
//
// Add to main class:
//   private SqliteManager db;
//
// In onEnable():
//   db = new SqliteManager(getDataFolder());
//   try { db.init(); } catch (java.sql.SQLException e) {
//       getLogger().severe("Database init failed: " + e.getMessage()); setEnabled(false); return;
//   }
//
// In onDisable():
//   if (db != null) { try { db.close(); } catch (java.sql.SQLException ignored) {} }

private static final class SqliteManager {

    private final java.io.File dbFile;
    private java.sql.Connection conn;

    SqliteManager(java.io.File dataFolder) {
        dataFolder.mkdirs();
        this.dbFile = new java.io.File(dataFolder, "data.db");
    }

    void init() throws java.sql.SQLException {
        conn = java.sql.DriverManager.getConnection(
            "jdbc:sqlite:" + dbFile.getAbsolutePath()
        );
        // Enable WAL mode for better concurrent read performance
        try (var stmt = conn.createStatement()) {
            stmt.execute("PRAGMA journal_mode=WAL;");
            // TODO: implement — CREATE TABLE IF NOT EXISTS statements
            // Example:
            // stmt.execute("""
            //     CREATE TABLE IF NOT EXISTS player_data (
            //         uuid  TEXT PRIMARY KEY,
            //         coins INTEGER NOT NULL DEFAULT 0
            //     )""");
        }
    }

    void close() throws java.sql.SQLException {
        if (conn != null && !conn.isClosed()) conn.close();
    }

    // TODO: implement — add typed query/update methods for your tables.
    // Always use PreparedStatement to prevent SQL injection.
    //
    // Example pattern:
    // long getCoins(java.util.UUID uuid) throws java.sql.SQLException {
    //     try (var ps = conn.prepareStatement(
    //             "SELECT coins FROM player_data WHERE uuid = ?")) {
    //         ps.setString(1, uuid.toString());
    //         var rs = ps.executeQuery();
    //         return rs.next() ? rs.getLong("coins") : 0L;
    //     }
    // }
    //
    // void setCoins(java.util.UUID uuid, long coins) throws java.sql.SQLException {
    //     try (var ps = conn.prepareStatement(
    //             "INSERT INTO player_data(uuid, coins) VALUES(?,?) "
    //             + "ON CONFLICT(uuid) DO UPDATE SET coins=excluded.coins")) {
    //         ps.setString(1, uuid.toString());
    //         ps.setLong(2, coins);
    //         ps.executeUpdate();
    //     }
    // }
}
