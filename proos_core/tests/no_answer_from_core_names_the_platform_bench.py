"""NO ANSWER FROM CORE NAMES THE PLATFORM — register 1479 (26 Nov 2026).
Run: python3 tests/no_answer_from_core_names_the_platform_bench.py

The sweep (1478): three narrow filters stood between the platform's words and the glass — the log filter (1475), Assist's
(one exact spelling only) and the page's (one exact spelling, twelve places) — and 506 answers left Core unfiltered. Dave:
"needs to be right can't have this leak"; "Go with leak fix".

ONE RULE, in platformlog.scrub_text, used at ONE DOOR (every JSON answer Core sends, Developer excepted) and by Assist:
  * in a sentence, the platform's name in any spelling reads ProOS
  * an IDENTITY is never touched — an entity id, a web address, a file path, a dotted name, a longer joined name — or
    controls, links and sign-ins would stop working
  * a lone lower-case word is an identity (a domain the pages and Assist act on); a lone Capitalised one is a name
DRIVEN: the real proos/platformlog.py and proos/assist.py.  WIRING (SPELLING): server._send.
"""
import os
import re
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_door_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import platformlog as PL  # noqa: E402
from proos import assist as A  # noqa: E402

SRV = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
H, S = "home", "assistant"                  # never written whole (platform_words)
T = H.title() + " " + S.title()             # the usual spelling
passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok    " + m)
    else:
        failed += 1
        print("  FAIL  " + m)


def eq(got, want, m):
    ok(got == want, "%s — got %r" % (m, got))


sc = PL.scrub_text
# 1 · every spelling, in a sentence
for theirs in (T, T.lower(), T.upper(), H.title() + S, H + S, H.title() + S.title(), H + "-" + S, H + "_" + S, "HASS", "hass", "Hassio"):
    eq(sc("Restart %s to finish" % theirs), "Restart ProOS to finish", "1 · in a sentence %r reads ProOS" % theirs)
eq(sc("%s Core Update" % T), "ProOS Core Update", "1 · the update item's name")
eq(sc("UniFi Network %s" % T), "UniFi Network ProOS", "1 · at the end of a name")
eq(sc("Updated by %s." % T), "Updated by ProOS.", "1 · before a full stop")
eq(sc("%s's backup failed" % (H.title() + S)), "ProOS's backup failed", "1 · with an apostrophe")
eq(sc("%s keeps warning" % (H + S)), "ProOS keeps warning", "1 · yesterday's leak, as it was stored")
# 2 · identities are never touched
for ident in ("update.%s_%s_core_update" % (H, S), "http://%s%s.local:8123" % (H, S), "%s%s.local" % (H, S),
              "/api/hassio_ingress/CPhHlmM2Bja0", "%s%s.components.unifiprotect" % (H, S), "%s_%s_core_update" % (H, S),
              H + S, "hassio", "hass", "sensor.%s_%s_data_rate" % (H, S), "eyJhbGciOi.9hassQ.Xk", "9hassQXk"):
    eq(sc(ident), ident, "2 · %r is left exactly as it is" % ident)
eq(sc("Open http://%s%s.local:8123 on the %s box" % (H, S, T)), "Open http://%s%s.local:8123 on the ProOS box" % (H, S),
   "2 · an address inside a sentence is kept; the name beside it is not")
eq(sc("entity update.%s_%s_core_update is on" % (H, S)), "entity update.%s_%s_core_update is on" % (H, S), "2 · an entity id inside a sentence is kept")
eq(sc("the chassis fan and Hassan's lamp"), "the chassis fan and Hassan's lamp", "2 · words that merely contain the letters are not touched")
# 3 · a lone capitalised word is the name
for nm in (H.title() + S, H.title() + S.title(), "HASS"):
    eq(sc(nm), "ProOS", "3 · alone, %r is the name" % nm)
eq(sc(""), "", "3 · nothing is nothing")
ok(sc(None) is None and sc(7) == 7, "3 · what is not text is returned as it is")
# 4 · a whole answer
ans = {"ok": True, "devices": [{"name": "%s RX" % T, "entity_id": "sensor.%s_%s_data_rate" % (H, S), "domain": H + S,
                                "url": "http://%s%s.local:8123/x" % (H, S), "note": "seen by %s" % (H + S)}],
       "title": H.title() + S, "count": 3}
keep = repr(ans)
out = PL.for_glass(ans, developer=False)
d = out["devices"][0]
ok(d["name"] == "ProOS RX" and d["note"] == "seen by ProOS" and out["title"] == "ProOS", "4 · names and sentences in an answer read ProOS")
ok(d["entity_id"] == "sensor.%s_%s_data_rate" % (H, S) and d["domain"] == H + S and d["url"].startswith("http://" + H + S), "4 · its identities are untouched")
ok(repr(ans) == keep and out["count"] == 3, "4 · the answer given is not altered; numbers pass")
ok(PL.for_glass(ans, developer=True) is ans, "4 · the Developer gets it word for word")
ok(not PL._PLATFORM_NAME.search(d["name"] + d["note"] + out["title"]), "4 · nothing shown names the platform")
# 5 · Assist: what it says, and what its tools hand it
eq(A.product_words("playing through Music Assistant"), "playing through ProOS Music", "5 · the music engine keeps its own ProOS name")
for theirs in (T, H.title() + S, T.lower(), "HASS"):
    eq(A.product_words("I restarted %s for you" % theirs), "I restarted ProOS for you", "5 · Assist never says %r" % theirs)
eq(A.product_words("platform=music_assistant engine=tts.%s_%s_cloud" % (H, S)), "platform=music_assistant engine=tts.%s_%s_cloud" % (H, S), "5 · ids in a reply are untouched")
tp = A._tool_payload({"results": [{"name": "%s Supervisor Update" % T, "entity_id": "update.%s_%s_supervisor_update" % (H, S), "domain": H + S,
                                   "why": "%s is restarting" % (H.title() + S)}]})
ok("ProOS Supervisor Update" in tp and "ProOS is restarting" in tp, "5 · a tool's answer reaches Assist with the name already gone, any spelling")
ok("update.%s_%s_supervisor_update" % (H, S) in tp and '"domain": "%s"' % (H + S) in tp, "5 · …and the ids and the domain it must act on are intact")
# 6 · the door
i = SRV.find("    def _send(self, code: int, payload: dict):")
door = SRV[i:i + 1500]
ok("for_glass(" in door and "is_owner" in door, "6 · every JSON answer passes the filter at _send, the Developer excepted (SPELLING)")
ok("_PLATFORM_NAME.search(" in door, "6 · …and the work is done only when the name is in the answer (SPELLING)")

print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
