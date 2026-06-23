"""Add /gallery/<int:entry_id> frontend route to app.py"""
with open('/opt/stacknest/api/app.py', 'r') as f:
    content = f.read()

target = (
    '@app.route("/gallery")\n'
    'def gallery_page():\n'
    '    return send_from_directory(app.static_folder, "gallery.html")\n'
)

replacement = (
    '@app.route("/gallery")\n'
    'def gallery_page():\n'
    '    return send_from_directory(app.static_folder, "gallery.html")\n'
    '\n'
    '@app.route("/gallery/<int:entry_id>")\n'
    'def gallery_entry_page(entry_id: int):\n'
    '    return send_from_directory(app.static_folder, "gallery_entry.html")\n'
)

if '@app.route("/gallery/<int:entry_id>")\ndef gallery_entry_page' in content:
    print("SKIP: frontend route already present")
elif target in content:
    content = content.replace(target, replacement, 1)
    with open('/opt/stacknest/api/app.py', 'w') as f:
        f.write(content)
    print("SUCCESS: added /gallery/<int:entry_id> frontend route")
else:
    print("FAIL: anchor not found")
