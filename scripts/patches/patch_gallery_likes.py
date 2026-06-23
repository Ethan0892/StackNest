"""
Patch db.py and app.py on the server:
  1. Add gallery_likes table + update like_gallery() for 1-like-per-IP
  2. Update gallery_like route to use voter key
  3. Add /gallery/<id> route serving gallery_entry.html
"""
import re

# ── db.py ────────────────────────────────────────────────────────────────────
with open('/opt/stacknest/api/db.py', 'r') as f:
    db = f.read()

# 1a. Add gallery_likes table to schema (after the gallery table)
OLD_GALLERY_IDX = "CREATE INDEX IF NOT EXISTS idx_gallery_public ON gallery(public, ts DESC);"
NEW_GALLERY_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_gallery_public ON gallery(public, ts DESC);\n\n"
    "CREATE TABLE IF NOT EXISTS gallery_likes (\n"
    "    entry_id   INTEGER NOT NULL,\n"
    "    voter_key  TEXT    NOT NULL,\n"
    "    ts         REAL    NOT NULL,\n"
    "    PRIMARY KEY (entry_id, voter_key)\n"
    ");\n"
    "CREATE INDEX IF NOT EXISTS idx_gallery_likes_entry ON gallery_likes(entry_id);"
)
if OLD_GALLERY_IDX in db and 'gallery_likes' not in db:
    db = db.replace(OLD_GALLERY_IDX, NEW_GALLERY_IDX)
    print("OK: added gallery_likes table to schema")
elif 'gallery_likes' in db:
    print("SKIP: gallery_likes table already in schema")
else:
    print("FAIL: could not find gallery index anchor in db.py")

# 1b. Replace like_gallery function
OLD_LIKE = (
    "def like_gallery(entry_id: int) -> int:\n"
    "    \"\"\"Increment likes for a gallery entry. Returns new like count.\"\"\"\n"
    "    with _lock, _conn() as con:\n"
    "        con.execute(\"UPDATE gallery SET likes = likes + 1 WHERE id = ?\", (entry_id,))\n"
    "        row = con.execute(\"SELECT likes FROM gallery WHERE id = ?\", (entry_id,)).fetchone()\n"
    "        return row[0] if row else 0"
)
NEW_LIKE = (
    "def like_gallery(entry_id: int, voter_key: str):\n"
    "    \"\"\"Like a gallery entry. One like per voter_key (IP hash). Returns (new_count, did_like).\"\"\"\n"
    "    with _lock, _conn() as con:\n"
    "        con.execute(\"\"\"\n"
    "            CREATE TABLE IF NOT EXISTS gallery_likes (\n"
    "                entry_id  INTEGER NOT NULL,\n"
    "                voter_key TEXT    NOT NULL,\n"
    "                ts        REAL    NOT NULL,\n"
    "                PRIMARY KEY (entry_id, voter_key)\n"
    "            )\"\"\")\n"
    "        try:\n"
    "            con.execute(\n"
    "                \"INSERT INTO gallery_likes (entry_id, voter_key, ts) VALUES (?, ?, ?)\",\n"
    "                (entry_id, voter_key, time.time())\n"
    "            )\n"
    "        except sqlite3.IntegrityError:\n"
    "            # Already liked — return current count without incrementing\n"
    "            row = con.execute(\"SELECT likes FROM gallery WHERE id = ?\", (entry_id,)).fetchone()\n"
    "            return (row[0] if row else 0), False\n"
    "        con.execute(\"UPDATE gallery SET likes = likes + 1 WHERE id = ?\", (entry_id,))\n"
    "        row = con.execute(\"SELECT likes FROM gallery WHERE id = ?\", (entry_id,)).fetchone()\n"
    "        return (row[0] if row else 0), True"
)

# Normalise line endings for matching
db_normalised = db
if OLD_LIKE in db_normalised:
    db = db.replace(OLD_LIKE, NEW_LIKE)
    print("OK: updated like_gallery function")
else:
    print("FAIL: could not find old like_gallery body — checking partial match...")
    if 'def like_gallery' in db:
        print("  (function exists but body differs — manual edit may be needed)")
    else:
        print("  (function not found at all)")

with open('/opt/stacknest/api/db.py', 'w') as f:
    f.write(db)
print("db.py written.")

# ── app.py ───────────────────────────────────────────────────────────────────
with open('/opt/stacknest/api/app.py', 'r') as f:
    app_py = f.read()

# 2a. Update gallery_like route
OLD_ROUTE = (
    "@app.route(\"/api/gallery/<int:entry_id>/like\", methods=[\"POST\"])\n"
    "def gallery_like(entry_id: int):\n"
    "    \"\"\"Increment likes for a gallery entry. Returns { 'likes': N }.\"\"\"\n"
    "    entry = get_gallery_entry(entry_id)\n"
    "    if not entry:\n"
    "        return jsonify({\"error\": \"Not found\"}), 404\n"
    "    new_count = like_gallery(entry_id)\n"
    "    return jsonify({\"likes\": new_count})"
)
NEW_ROUTE = (
    "@app.route(\"/api/gallery/<int:entry_id>/like\", methods=[\"POST\"])\n"
    "def gallery_like(entry_id: int):\n"
    "    \"\"\"Like a gallery entry (1 per IP). Returns { 'likes': N, 'already_liked': bool }.\"\"\"\n"
    "    entry = get_gallery_entry(entry_id)\n"
    "    if not entry:\n"
    "        return jsonify({\"error\": \"Not found\"}), 404\n"
    "    ip = get_remote_address()\n"
    "    voter_key = hashlib.sha256(ip.encode()).hexdigest()[:32]\n"
    "    new_count, did_like = like_gallery(entry_id, voter_key)\n"
    "    return jsonify({\"likes\": new_count, \"already_liked\": not did_like})"
)
if OLD_ROUTE in app_py:
    app_py = app_py.replace(OLD_ROUTE, NEW_ROUTE)
    print("OK: updated gallery_like route")
else:
    print("FAIL: could not find old gallery_like route body")

# 2b. Add /gallery/<int:entry_id> page route
# Find the existing /gallery route and add the entry sub-route right after it
OLD_GALLERY_ROUTE = (
    "@app.route(\"/gallery\")\n"
    "def gallery_page():\n"
    "    return send_from_directory(app.static_folder, \"gallery.html\")"
)
NEW_GALLERY_ROUTE = (
    "@app.route(\"/gallery\")\n"
    "def gallery_page():\n"
    "    return send_from_directory(app.static_folder, \"gallery.html\")\n\n"
    "@app.route(\"/gallery/<int:entry_id>\")\n"
    "def gallery_entry_page(entry_id: int):\n"
    "    return send_from_directory(app.static_folder, \"gallery_entry.html\")"
)
if OLD_GALLERY_ROUTE in app_py and '/gallery/<int:entry_id>' not in app_py:
    app_py = app_py.replace(OLD_GALLERY_ROUTE, NEW_GALLERY_ROUTE)
    print("OK: added /gallery/<int:entry_id> route")
elif '/gallery/<int:entry_id>' in app_py:
    print("SKIP: gallery entry route already exists")
else:
    print("FAIL: could not find /gallery route anchor")

with open('/opt/stacknest/api/app.py', 'w') as f:
    f.write(app_py)
print("app.py written.")
print("All patches applied.")
