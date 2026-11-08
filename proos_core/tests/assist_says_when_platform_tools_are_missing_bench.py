"""ASSIST SAYS WHEN THE PLATFORM'S TOOLS ARE MISSING — register 1252.
Run: python3 tests/assist_says_when_platform_tools_are_missing_bench.py

Dave, 8 Nov 2026, after "assist fix the bedroom": "it is far from being able to fix an issue … I was even telling it
the area to look and it wanted to send to installer".

MEASURED on his box: both fix jobs asked more_tools("platform") 3–4 times and never once called the integration, log
or service tools; Assist itself, asked to load the set, listed NOT ONE ha_ tool — the platform's MCP server was not
connected. more_tools still answered "loaded", so Assist reported the tools "not exposed, despite the tool loader
reporting them loaded" and flagged the installer. Why it was not connected lived only in a print to Core's log.

PINNED (driven — the real ToolRunner.t_more_tools):
 1. with no platform tools, asking for "platform" is NOT "loaded": ok False, an error that says the platform's tools are
    not available AND the reason Core recorded (MIRROR_STATE.why), and tells Assist to say so plainly
 2. with the platform's tools there, it loads exactly as before ("loaded", ha_ tools in tools_now)
 3. another set ("diagnostic") is untouched by the mirror's absence
 4. (SOURCE) Core keeps the reason (_ha_mcp_state), hands it to Assist (MIRROR_STATE), and serves it to the installer at
    /assist/mirror (installer only; ?retry=1 skips the five-minute wait), with every add-on whose name says MCP and its state
"""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_mirror_says_")

from proos import assist as A                                    # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "\n        wanted %r\n        got    %r" % (want, got))


def rig(mirror):
    class _R(A.ToolRunner):
        def __init__(self):
            self.user = {"id": "proos-engine", "name": "ProOS", "is_owner": True, "tech": True, "engine": True}
            self.ma = True
            self.actions, self.trace = [], []
            self.opened_tools = set()
            self.on_act = None
            self._m = [{"name": "ha_get_logs", "description": "the platform's log", "input_schema": {"type": "object"},
                        "read_only": True},
                       {"name": "ha_get_integration", "description": "an integration", "input_schema": {"type": "object"},
                        "read_only": True}] if mirror else []

        def home_domains(self):
            return {"light", "media_player", "remote", "switch"}

        def _mcp_list(self):
            return self._m
    return _R()


A.MIRROR_STATE = {"connected": False, "why": "no MCP server answered the handshake — no candidates found"}
none = rig(False).t_more_tools({"set": "platform"})
check("1 · no platform tools: 'platform' is not reported loaded", none.get("ok"), False)
err = str(none.get("error") or "")
check("1 · it says the platform's tools are NOT available, with the reason Core recorded",
      "NOT available" in err and "no candidates found" in err and "Say so plainly" in err, True)

has = rig(True).t_more_tools({"set": "platform"})
check("2 · with the platform's tools: loaded, as before", (has.get("ok"), "loaded" in str(has.get("note"))), (True, True))
check("2 · the platform's tools are in the list", {"ha_get_logs", "ha_get_integration"} <= set(has.get("tools_now") or []), True)

diag = rig(False).t_more_tools({"set": "diagnostic"}) if "diagnostic" in A._TOOL_GROUPS else {"ok": True}
check("3 · another set is untouched by the mirror's absence", diag.get("ok"), True)

src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
check("4 · Core keeps the reason, hands it to Assist and serves it to the installer",
      all(x in src for x in ['_ha_mcp_state.update(connected=False, at=now, tools=0, read_only=0, why=str(e)[:400])',
                             '_assist.MIRROR_STATE = dict(_ha_mcp_state)',
                             'if parts == ["assist", "mirror"]:',
                             'return self._send(403, {"error": "installer access required"})',
                             'out["mcp_addons"] = [{"slug": a.get("slug"), "name": a.get("name"), "state": a.get("state"),']), True)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
