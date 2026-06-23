path = '/opt/stacknest/validation/compile_check.py'
with open(path) as f:
    c = f.read()

old = (
    'PAPER_API_URL = (\n'
    '    "https://repo.papermc.io/repository/maven-public/"\n'
    '    "io/papermc/paper/paper-api/1.21-R0.1-SNAPSHOT/"\n'
    '    "paper-api-1.21-R0.1-SNAPSHOT.jar"\n'
    ')'
)
new = (
    'PAPER_API_URL = (\n'
    '    "https://repo.papermc.io/repository/maven-public/"\n'
    '    "io/papermc/paper/paper-api/1.21-R0.1-SNAPSHOT/"\n'
    '    "paper-api-1.21-R0.1-20240810.100446-132.jar"\n'
    ')'
)

if old in c:
    c = c.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(c)
    print('SUCCESS: URL updated')
else:
    print('FAIL: pattern not found')
    # Show what's actually there
    idx = c.find('PAPER_API_URL')
    print(repr(c[idx:idx+200]))
