"""Patch watchdog.py: initialize 'configured' from env vars at module load time
so all Gunicorn workers (not just the watchdog-lock winner) start with the
correct configured state."""
import os

path = '/opt/stacknest/inference/watchdog.py'
with open(path) as f:
    c = f.read()

replacements = [
    (
        '    "gemini": {\n        "configured":     False,\n        "healthy":        True,      # assumed healthy until proven otherwise',
        '    "gemini": {\n        "configured":     bool(os.getenv("GEMINI_API_KEY", "")),\n        "healthy":        True,      # assumed healthy until proven otherwise',
    ),
    (
        '    "claude": {\n        "configured":     False,\n        "healthy":        True,',
        '    "claude": {\n        "configured":     bool(os.getenv("CLAUDE_API_KEY", "")),\n        "healthy":        True,',
    ),
    (
        '    "kimi": {\n        "configured":     False,\n        "healthy":        True,',
        '    "kimi": {\n        "configured":     bool(os.getenv("KIMI_API_KEY", "")),\n        "healthy":        True,',
    ),
]

count = 0
for old, new in replacements:
    if old in c:
        c = c.replace(old, new, 1)
        count += 1
    else:
        print(f'MISS: {old[:40]!r}')

if count == 3:
    with open(path, 'w') as f:
        f.write(c)
    print('SUCCESS: patched all 3 backends')
else:
    print(f'PARTIAL: only patched {count}/3')
