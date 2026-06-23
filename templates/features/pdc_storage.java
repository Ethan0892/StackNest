// FEATURE: pdc_storage
// Type-safe PersistentDataContainer helpers for storing data on Players, ItemStacks,
// Entities, Block states, or any other PersistentDataHolder.
// Data survives server restarts without a database.
//
// Required imports:
//   import org.bukkit.NamespacedKey;
//   import org.bukkit.persistence.PersistentDataHolder;
//   import org.bukkit.persistence.PersistentDataType;
//
// Declare keys as fields and initialise in onEnable():
//   private NamespacedKey levelKey;
//   private NamespacedKey tagKey;
//   ...
//   levelKey = new NamespacedKey(this, "level");
//   tagKey   = new NamespacedKey(this, "tag");

private <T, Z> Z getPdc(PersistentDataHolder holder,
                         NamespacedKey key,
                         PersistentDataType<T, Z> type,
                         Z defaultValue) {
    return holder.getPersistentDataContainer().getOrDefault(key, type, defaultValue);
}

private <T, Z> void setPdc(PersistentDataHolder holder,
                            NamespacedKey key,
                            PersistentDataType<T, Z> type,
                            Z value) {
    holder.getPersistentDataContainer().set(key, type, value);
}

private void removePdc(PersistentDataHolder holder, NamespacedKey key) {
    holder.getPersistentDataContainer().remove(key);
}

// TODO: implement — add domain-specific typed accessors, for example:
//
// int  getLevel(Player p)          { return getPdc(p, levelKey, PersistentDataType.INTEGER, 0); }
// void setLevel(Player p, int lvl) { setPdc(p, levelKey, PersistentDataType.INTEGER, lvl);    }
//
// boolean isTagged(Entity e)       { return getPdc(e, tagKey, PersistentDataType.BYTE, (byte) 0) == 1; }
// void    setTagged(Entity e, boolean v) { setPdc(e, tagKey, PersistentDataType.BYTE, v ? (byte) 1 : (byte) 0); }
