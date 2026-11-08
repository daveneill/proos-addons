"""CORE KEEPS ASSIST'S PLATFORM TOOLS — register 1253.
Run: python3 tests/assist_platform_tools_kept_bench.py

Dave, 8 Nov 2026: "Why … was the platform tools removed its the … main tool for the whole communication to the
platform"; "yes please" to Core keeping it and Health saying so. HOW IT WAS LOST (read): the Home Assistant MCP Server
add-on was hand-installed on the old house box in August; the ProOS image (942, 18 Oct) and Core's set-up never
installed it, so the move to the G-Flip left Assist without it, and nothing said so.

PINNED (driven — the real assisttools.ensure over a stand-in Supervisor; the real healthmon.mirror_check):
 1. a box without it: the repository is added to the platform's store, the add-on installed, set to start on boot,
    watchdog on, started — done
 2. a box where it is all in place gets NO writes (converge, don't clobber)
 3. the store refusing the repository is said, not passed over
 4. Health: Assist without the platform's tools for 15 minutes is a critical card with the reason and what ProOS is
    doing; not inside the grace (a fresh box installing it); cleared the moment the tools answer
 5. (SOURCE) Core runs the keeper on EVERY box (not only a panel), finds the server at once when it is in place, and the
    module ships in the zip
"""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_tools_kept_")

from proos import assisttools as T                               # noqa: E402
from proos import healthmon as H                                 # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "\n        wanted %r\n        got    %r" % (want, got))


class Sup:
    def __init__(self, repo=False, installed=False, boot="manual", state="stopped", accept_repo=True):
        self.repos = [{"slug": "a1b2c3d4", "source": T.REPO}] if repo else []
        self.installed, self.boot, self.state, self.watchdog = installed, boot, state, False if not installed else True
        self.accept, self.writes = accept_repo, []

    def __call__(self, method, path, payload=None, timeout=None):
        if method == "POST":
            self.writes.append(path)
        if path == "/store/repositories":
            if method == "POST" and self.accept:
                self.repos = [{"slug": "a1b2c3d4", "source": T.REPO}]
            return self.repos
        if path == "/store/addons/a1b2c3d4_ha_mcp/install":
            self.installed = True
            return {}
        if path == "/addons/a1b2c3d4_ha_mcp/info":
            if not self.installed:
                raise RuntimeError("400 addon not installed")
            return {"version": "9.1.1", "boot": self.boot, "state": self.state, "watchdog": self.watchdog, "options": {}}
        if path == "/addons/a1b2c3d4_ha_mcp/options":
            if "boot" in (payload or {}):
                self.boot = payload["boot"]
            if "watchdog" in (payload or {}):
                self.watchdog = payload["watchdog"]
            return {}
        if path == "/addons/a1b2c3d4_ha_mcp/start":
            self.state = "started"
            return {}
        return {}


s = Sup()
r = T.ensure(s)
check("1 · a bare box: repository added, installed, on boot, watchdog, started — done",
      (r.get("done"), s.boot, s.watchdog, s.state, r.get("addon")), (True, "auto", True, "started", "a1b2c3d4_ha_mcp"))
check("1 · in that order: store, install, boot, start",
      [w for w in s.writes if w != "/addons/a1b2c3d4_ha_mcp/options"][:3],
      ["/store/repositories", "/store/addons/a1b2c3d4_ha_mcp/install", "/addons/a1b2c3d4_ha_mcp/start"])

s2 = Sup(repo=True, installed=True, boot="auto", state="started")
r2 = T.ensure(s2)
check("2 · all in place: done, and NO writes", (r2.get("done"), s2.writes), (True, []))

s3 = Sup(accept_repo=False)
r3 = T.ensure(s3)
check("3 · the store refusing the repository is said", (r3.get("done"), "did not take the repository" in str(r3.get("error"))), (False, True))

# 4 · Health
opened = {}
H._ensure = lambda cid, inc, quiet=False: opened.__setitem__(cid, inc)
now = 1_000_000.0
H.MIRROR_STATE_FN = lambda: {"connected": False, "why": "no MCP server answered the handshake — no candidates found",
                             "down_since": now - 60, "keeper": {}}
seen = set(); H.mirror_check(seen, now)
check("4 · inside the grace (a fresh box installing it): no card", (len(seen), len(opened)), (0, 0))
H.MIRROR_STATE_FN = lambda: {"connected": False, "why": "no MCP server answered the handshake — no candidates found",
                             "down_since": now - 1200, "keeper": {}}
seen = set(); H.mirror_check(seen, now)
inc = next(iter(opened.values()), {})
check("4 · 20 minutes without: a critical card with the reason and what ProOS is doing",
      (len(seen), inc.get("severity"), inc.get("title"), "no candidates found" in inc.get("cause", ""),
       "installing it and keeps it running" in inc.get("cause", "")),
      (1, "critical", "Assist Has Lost the Platform's Tools", True, True))
H.MIRROR_STATE_FN = lambda: {"connected": True, "why": "", "down_since": None}
seen = set(); H.mirror_check(seen, now)
check("4 · the tools answer: nothing seen, so the card clears", len(seen), 0)

src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
zl = open(os.path.join(HERE, "..", "zipfiles.txt"), encoding="utf-8").read().split()
import re as _re
pi = _re.search(r"\n( *)if bool\(_opt\(\"panel\", False\)\):", src)
ki = _re.search(r"\n( *)try:\n *from proos import assisttools as _assisttools", src)
check("5 · the keeper runs on every box (beside the panel-only block, not inside it), finds the server at once, and ships",
      bool(pi and ki) and len(pi.group(1)) == len(ki.group(1)) and "_assisttools.ensure(_sv)" in src
      and "_ha_mcp_at = 0.0                 # found now" in src and "proos/assisttools.py" in zl
      and "_healthmon_mod.MIRROR_STATE_FN" in src, True)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
