"""
Fix two issues:
1. extract_java_blocks regex in compile_check.py and yml_check.py is too strict —
   only matches ```java\n exactly, fails on ```java (test)\n, ```Java\n, etc.
2. SYSTEM_PROMPT in router.py uses ```java (test) marker which teaches the model
   to output that label, breaking the regex.
"""

# ─── 1. Fix extract_java_blocks regex in compile_check.py ────────────────────
path = '/opt/stacknest/validation/compile_check.py'
with open(path) as f:
    c = f.read()

old = r'pattern = re.compile(r"```java\n(?://\s*([\w./\-]+\.java)\n)?(.*?)```", re.DOTALL)'
new = r'pattern = re.compile(r"```[Jj]ava[^\n]*\n(?://\s*([\w./\-]+\.java)\n)?(.*?)```", re.DOTALL)'

if old in c:
    c = c.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(c)
    print('OK: compile_check.py regex fixed')
else:
    print('SKIP/FAIL: compile_check.py regex anchor not found')
    idx = c.find('```java')
    print('Found at:', repr(c[idx:idx+100]))

# ─── 2. Fix extract_java_blocks regex in yml_check.py ────────────────────────
path2 = '/opt/stacknest/validation/yml_check.py'
with open(path2) as f:
    c2 = f.read()

old2 = 'return re.findall(r"```java\\n(.*?)```", response, re.DOTALL)'
new2 = 'return re.findall(r"```[Jj]ava[^\\n]*\\n(.*?)```", response, re.DOTALL)'
if old2 in c2:
    c2 = c2.replace(old2, new2, 1)
    with open(path2, 'w') as f:
        f.write(c2)
    print('OK: yml_check.py regex fixed')
else:
    print('SKIP: yml_check.py regex anchor not found')
    idx = c2.find('java')
    print(repr(c2[max(0,idx-20):idx+80]))

# ─── 3. Fix SYSTEM_PROMPT in router.py — remove (test) label from code fence ─
path3 = '/opt/stacknest/inference/router.py'
with open(path3) as f:
    c3 = f.read()

# Replace the test block instruction to not use the (test) fence annotation
old_test = '"4. ```java (test) — JUnit 5 + MockBukkit test class. Package matches plugin. "'
new_test = '"4. ```java — JUnit 5 + MockBukkit test class (add a comment // test on the first line). Package matches plugin. "'
if old_test in c3:
    c3 = c3.replace(old_test, new_test, 1)
    with open(path3, 'w') as f:
        f.write(c3)
    print('OK: router.py SYSTEM_PROMPT test fence annotation removed')
else:
    print('SKIP: router.py test annotation not found')

print('\nDone.')
