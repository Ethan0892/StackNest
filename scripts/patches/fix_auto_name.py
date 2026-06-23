path = '/opt/stacknest/api/app.py'
with open(path) as f:
    c = f.read()

if '_auto_plugin_name' in c and 'raw_plugin_name' not in c:
    print('Injecting _auto_plugin_name derivation...')
    old = (
        '    skip_compile = bool(data.get("skip_compile", False))\n'
        '    folia = bool(data.get("folia_compatible", False))\n'
        '    save_project = bool(data.get("save_project", True))\n'
        '    tier = get_tier()'
    )
    new = (
        '    skip_compile = bool(data.get("skip_compile", False))\n'
        '    folia = bool(data.get("folia_compatible", False))\n'
        '    save_project = bool(data.get("save_project", True))\n'
        '    _raw_plugin_name = str(data.get("project_name") or data.get("plugin_name") or "").strip()\n'
        '    _auto_plugin_name = _raw_plugin_name or _derive_plugin_name(data.get("instruction", ""))\n'
        '    tier = get_tier()'
    )
    if old in c:
        c = c.replace(old, new, 1)
        with open(path, 'w') as f:
            f.write(c)
        print('SUCCESS: _auto_plugin_name injected')
    else:
        print('FAIL: anchor not matched')
        # show context
        idx = c.find('skip_compile = bool(data.get("skip_compile"')
        print(repr(c[idx:idx+300]))
elif '_auto_plugin_name' not in c:
    print('FAIL: _auto_plugin_name not referenced anywhere')
else:
    print('SKIP: already patched')
