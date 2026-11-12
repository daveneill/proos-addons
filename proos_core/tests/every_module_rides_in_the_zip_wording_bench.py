"""EVERY MODULE RIDES IN THE ZIP — register 1326 (12 Nov 2026). Found shipping 1.0.1431: proos/repairspush.py was new
and not in zipfiles.txt — the box would have had the loop and not its module (the thread dies on import). The gate could
not see it: release_complete is the one bench the ship expects to fail before the zip exists.
Run: python3 tests/every_module_rides_in_the_zip_wording_bench.py
(Named _wording_ by register 101: it reads files, it drives no code.)
Every proos/*.py Core imports, and every file of every driver folder, is listed in zipfiles.txt.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
listed = {l.strip() for l in open(os.path.join(CORE, "zipfiles.txt"), encoding="utf-8") if l.strip()}
missing = []
for f in sorted(os.listdir(os.path.join(CORE, "proos"))):
    if f.endswith(".py") and "proos/" + f not in listed:
        missing.append("proos/" + f)
droot = os.path.join(CORE, "drivers")
for base, dirs, files in os.walk(droot):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if f.endswith(".pyc"):
            continue
        rel = os.path.relpath(os.path.join(base, f), CORE)
        if rel not in listed:
            missing.append(rel)
print(("PASS  every Core module and driver file is in zipfiles.txt" if not missing else
       "FAIL  not in zipfiles.txt (the box would not get them): " + ", ".join(missing[:20])))
print("\n%d passed, %d failed" % (0 if missing else 1, 1 if missing else 0))
sys.exit(1 if missing else 0)
