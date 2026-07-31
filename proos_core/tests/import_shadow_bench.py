"""
Import-shadow bench — run: python3 tests/import_shadow_bench.py

WHY THIS EXISTS (live house, 30 Jul 2026)
-----------------------------------------
server.py imports `parse_qs, urlparse` at module level. Two handler branches
deep inside do_GET ALSO did `from urllib.parse import parse_qs, urlparse`.

Python decides a name is local for the WHOLE function body at compile time, so
those two lines made `parse_qs` and `urlparse` local to all ~680 lines of
do_GET. Every route that used them WITHOUT first running one of those two
branches raised:

    UnboundLocalError: cannot access local variable 'parse_qs'
                       where it is not associated with a value

Thirteen GET routes were dead, silently, returning that string as a JSON error:
app tile artwork, the /events SSE stream dashboards register on, room art
styles, commissioning options, fire plans, device commands, room order, the
Android TV app list, and more. Each surfaced as its own unrelated-looking bug
and got chased separately. The import line was the whole cause.

The rule: a function must never re-import a name the module already imports.
It is never necessary, and when it's wrong it is invisible — the code reads
correctly and fails only on the paths that skip the import.
"""
import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")

# Known-latent and deliberately left alone: in both of these the local import is
# the FIRST statement, so every use comes after it and it cannot raise. Named
# here rather than silently ignored -- they are still traps for a future edit
# that adds a use above the import.
ALLOW = {("controller.py", "_off_config", "generator"),
         ("users.py", "_post_form", "urllib")}
LATENT = []
FAIL = []
CHECKED = 0


def module_level_names(tree):
    out = set()
    for node in tree.body:                       # top level only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
    return out


def scan(path):
    global CHECKED
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        FAIL.append(f"{os.path.basename(path)}: will not parse — {e}")
        return
    CHECKED += 1
    mod = module_level_names(tree)
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for sub in ast.walk(fn):
            if not isinstance(sub, (ast.Import, ast.ImportFrom)):
                continue
            for a in sub.names:
                name = a.asname or a.name.split(".")[0]
                if name in mod:
                    if (os.path.basename(path), fn.name, name) in ALLOW:
                        LATENT.append(f"{os.path.basename(path)}:{sub.lineno} {fn.name}() re-imports '{name}' (import is first statement; cannot raise)")
                        continue
                    FAIL.append(
                        f"{os.path.basename(path)}:{sub.lineno} in {fn.name}() — "
                        f"re-imports '{name}', which is already imported at module "
                        f"level. This shadows it for the ENTIRE function; every "
                        f"other use of '{name}' in {fn.name}() raises "
                        f"UnboundLocalError.")


targets = [os.path.join(ROOT, "server.py")]
pkg = os.path.join(ROOT, "proos")
targets += [os.path.join(pkg, f) for f in sorted(os.listdir(pkg))
            if f.endswith(".py")]

for t in targets:
    if os.path.isfile(t):
        scan(t)

print(f"\n{CHECKED} files scanned, {len(FAIL)} shadowed imports that can raise, {len(LATENT)} known-latent")
for l in LATENT:
    print("  latent", l)
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
