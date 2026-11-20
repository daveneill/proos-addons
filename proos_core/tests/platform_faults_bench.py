"""The platform's own fault lists reach Health — DRIVEN (register 1014; audit 1013 step 1).
Run: python3 tests/platform_faults_bench.py

Read on Dave's box, 26 Oct 2026: the platform said the LIFX "Bec's Lamp" integration could not connect
(setup_retry · cannot_connect) and the box had no current backup (Supervisor no_current_backup) — and Pro said
"All Systems Normal". The fakes below are those readings, shape for shape.

 1. a failing integration becomes one card, in the platform's own words (its title, domain, state and reason)
 2. a Supervisor issue becomes one card naming the Supervisor's own type
 3. an open Repairs issue becomes one card with the platform's own title; an ignored one does not
 4. a loaded, a disabled, and a plain setting-up integration make no card
 5. the platform clears it -> the card is gone on the next read (no ProOS timer keeps it)
 6. a retry in progress right after a failure keeps its card, saying it is retrying
 7. a reader that fails keeps the last reading and says why — an unanswered question is never an all-clear
 8. none of these reach a phone (warning / installer — not on the critical list, 1013b)
 9. acknowledge keeps the card, marked; when the platform clears it, the acknowledgement goes with it
10. the sweep already cards Backups -> no second card; the sweep card carries the platform's agreement
11. healthmon.incidents() includes the platform cards (the wiring Pro reads)
12. server wiring: /health/fix and /health/dismiss route pf- ids; the loop is started; Pro shows the buttons
"""
import os
import sys
import tempfile

_DATA = tempfile.mkdtemp(prefix="proos_pf_")
os.environ["PROOS_DATA_DIR"] = _DATA
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import platformfaults as PF                           # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


LIFX = {"entry_id": "e_lifx", "domain": "lifx", "title": "Bec's Lamp", "state": "setup_retry",
        "reason": "cannot_connect", "disabled_by": None}


class Box:
    def __init__(self):
        self.ents = [LIFX.copy(),
                     {"entry_id": "e_sonos", "domain": "sonos", "title": "Sonos", "state": "loaded"},
                     {"entry_id": "e_off", "domain": "hue", "title": "Old Hue", "state": "not_loaded", "disabled_by": "user"},
                     {"entry_id": "e_new", "domain": "cast", "title": "Cast", "state": "setup_in_progress"},
                     # 1426, read on Dave's box 20 Nov: an IGNORED discovery — the platform's own "don't ask again"
                     {"entry_id": "e_ign", "domain": "dlna_dms", "title": "HDHomeRun DMS 1252EB4B", "state": "not_loaded",
                      "source": "ignore", "disabled_by": None}]
        self.reps = [{"domain": "homeassistant", "issue_id": "deprecated_yaml", "title": "The YAML setup is deprecated",
                      "severity": "warning", "ignored": False},
                     {"domain": "hacs", "issue_id": "old", "title": "Ignored one", "ignored": True}]
        self.res = {"issues": [{"type": "no_current_backup", "context": "system", "reference": None}],
                    "unhealthy": [], "unsupported": []}
        self.broken = set()

    def entries(self):
        if "entries" in self.broken:
            raise RuntimeError("platform not answering")
        return [dict(e) for e in self.ents]

    def repairs(self):
        return [dict(r) for r in self.reps]

    def resolution(self):
        return dict(self.res)


def by_subject(cs):
    return {c["subject"]: c for c in cs}


LOGGED = []
PF.LOG = LOGGED.append
b = Box()
PF.refresh(b, now=1000)
cs = by_subject(PF.cards())
lifx = cs.get("entry:e_lifx")
check("1 · the failing LIFX integration is one card", lifx is not None)
check("1 · its title is the platform's own name", (lifx or {}).get("title"), "Bec's Lamp — Not Connecting")
check("1 · its cause carries the platform's state words and reason",
      all(w in (lifx or {}).get("cause", "") for w in ("Bec's Lamp", "lifx", "can't connect", "cannot_connect")))
check("1 · it offers the platform's Reload and Check Again",
      [a["kind"] for a in (lifx or {}).get("actions", [])], ["pf_reload", "pf_recheck"])
sup = cs.get("supervisor:no_current_backup")
check("2 · the Supervisor's no_current_backup is one card", sup is not None and sup["title"] == "No Current Backup")
# RE-PINNED 1342 (Dave 14 Nov 09:56: "If it cant be fixed from the warning then it just needs to be in the log"): a
# Repairs issue has no button ProOS can press — it is written to the Master Log once, in words, and is NOT a card
check("3 · the open Repairs issue is NOT a card — it goes to the log once, in the platform's words",
      ("repair:homeassistant/deprecated_yaml" in cs, [w for w in LOGGED if "YAML" in w].__len__()), (False, 1))
check("3 · an ignored Repairs issue is not", "repair:hacs/old" in cs, False)
check("4 · loaded, disabled and plain setting-up integrations make no card",
      [s for s in cs if s in ("entry:e_sonos", "entry:e_off", "entry:e_new")], [])
check("1426 · an ignored discovery (source ignore, not_loaded) is no card — the platform lists it under Ignored, never as a fault",
      "entry:e_ign" in cs, False)
check("1426 · …and is not counted as failing", "e_ign" in PF.snapshot().get("failing", []), False)
check("4 · exactly two cards (re-pinned 1342: the Repairs one is in the log)", len(cs), 2)
check("8 · none reaches a phone (warning, installer)",
      all(c["severity"] == "warning" and c["audience"] == "installer" for c in cs.values()), True)
check("all ids are pf- and stable across reads",
      [c["id"] for c in PF.cards()] == [c["id"] for c in (PF.refresh(b, now=1060) and PF.cards())]
      and all(c["id"].startswith("pf-") for c in cs.values()), True)

# 9 acknowledge
ack = PF.acknowledge(lifx["id"])
check("9 · acknowledge returns the card id", ack, {"id": lifx["id"]})
check("9 · the card stays, marked acknowledged", bool(by_subject(PF.cards())["entry:e_lifx"].get("acknowledged")), True)
check("9 · acknowledging a card that is not open is refused", PF.acknowledge("pf-nothing"), None)

# 6 retry in progress after a failure
b.ents[0]["state"] = "setup_in_progress"
b.ents[0]["reason"] = None
PF.refresh(b, now=1120)
r = by_subject(PF.cards()).get("entry:e_lifx")
check("6 · a retry right after a failure keeps its card", r is not None and "retrying" in r["cause"], True)

# 7 a reader that fails
b.broken.add("entries")
snap = PF.refresh(b, now=1180)
check("7 · a failed read says why", "integrations" in (snap.get("error") or ""), True)
check("7 · and keeps the last reading (the card stands)", "entry:e_lifx" in by_subject(PF.cards()), True)
b.broken.clear()

# 5 the platform clears it
b.ents[0]["state"] = "loaded"
b.res = {"issues": []}
PF.refresh(b, now=1240)
PF.prune_acks()
cs = by_subject(PF.cards())
check("5 · the platform says loaded -> the LIFX card is gone", "entry:e_lifx" in cs, False)
check("5 · the Supervisor clears -> its card is gone", "supervisor:no_current_backup" in cs, False)
check("9 · the acknowledgement went with it", lifx["id"] in PF._load_acks(), False)
b.ents[0]["state"] = "setup_retry"
PF.refresh(b, now=1300)
check("9 · the same fault coming back is new (not acknowledged)",
      bool(by_subject(PF.cards())["entry:e_lifx"].get("acknowledged")), False)

# 10 one problem, one card
b.res = {"issues": [{"type": "no_current_backup", "context": "system"}]}
PF.refresh(b, now=1360)
sweep_backup = {"subject": "sweep:8:Backups", "cause": "automatic backups are off.", "id": "sw-1"}
merged = PF.merge_into([sweep_backup], PF.cards())
check("10 · with the sweep's Backups card open, no second backup card",
      any(c["subject"] == "supervisor:no_current_backup" for c in merged), False)
check("10 · the sweep card carries the platform's own words", "supervisor:no_current_backup" in sweep_backup["cause"], True)
check("10 · without it, the platform card stands",
      any(c["subject"] == "supervisor:no_current_backup" for c in PF.merge_into([], PF.cards())), True)

# 11 healthmon wiring
from proos import healthmon as HM                               # noqa: E402
subs = {i.get("subject") for i in HM.incidents()}
check("11 · healthmon.incidents() carries the platform cards", {"entry:e_lifx", "supervisor:no_current_backup"} <= subs, True)
from proos.proactive import incident_reaches_phone              # noqa: E402
# RE-PINNED (register 1022, Dave's critical list 26 Oct 16:06: "backup or storage failure"): the box's own
# no_current_backup now reaches the phone; a failing integration (LIFX) still does not.
_ph = [i.get("subject") for i in HM.incidents() if str(i.get("id", "")).startswith("pf-") and incident_reaches_phone(i)]
check("8 · the real phone rule: the box's no-backup warning reaches the phone (critical: backup failure); the LIFX card does not",
      ("supervisor:no_current_backup" in _ph, "entry:e_lifx" in _ph), (True, False))

# 12 wiring, read from source (a spelling check of the routes, named as one)
SRV = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
PRO = open(os.path.join(HERE, "..", "apps", "pro.html"), encoding="utf-8", errors="replace").read()
check("12 · (wiring) /health/fix routes pf- cards", '_iid_.startswith("pf-")' in SRV and "_platform_fault_fix(_iid_" in SRV, True)
check("12 · (wiring) /health/dismiss routes pf- cards", '_did_.startswith("pf-")' in SRV, True)
check("12 · (wiring) the read loop is started", "target=_platform_faults_loop" in SRV, True)
check("12 · (wiring) Pro shows Reload and Check Again", "'pf_reload','pf_recheck'" in PRO and "did==='platform_fix'" in PRO, True)


# ── 1342: the box's own fix is the button; no button → the log; never the platform's names ──
PF.cards()
check("1342 · read again: the same Repairs issue is NOT logged twice", len([w for w in LOGGED if "YAML" in w]), 1)
b.reps = []
b.res = {"issues": [{"type": "boot_fail", "context": "addon", "reference": "b333b432_proos_cameras", "uuid": "i1"},
                    {"type": "dns_server_failed", "context": "dns_server", "reference": None, "uuid": "i2"}],
         "suggestions": [{"type": "execute_start", "context": "addon", "reference": "b333b432_proos_cameras", "uuid": "s1"},
                         {"type": "disable_boot", "context": "addon", "reference": "b333b432_proos_cameras", "uuid": "s2"}],
         "names": {"b333b432_proos_cameras": "ProOS Cameras"}, "unhealthy": ["docker"], "unsupported": []}
b.reps = [{"domain": "hassio", "issue_id": "boot_fail", "title": "App failed to start at boot", "severity": "warning",
           "ignored": False}]
PF.refresh(b, now=2000)
cs2 = PF.cards()
cam = next((c for c in cs2 if c["subject"] == "supervisor:boot_fail"), None)
check("1342 · an app that didn't start: one card, by its NAME, with the box's own fix as the button (Start, not 'disable')",
      (cam or {}).get("title"), "ProOS Cameras Didn't Start")
check("  …its button is the box's own suggestion", [(a["kind"], a.get("label"), a.get("uuid")) for a in (cam or {}).get("actions", [])
                                                   if a["kind"] != "pf_recheck"], [("pf_suggest", "Start ProOS Cameras", "s1")])
check("  …a box issue with no fix offered is NOT a card (logged instead)", any(c["subject"] == "supervisor:dns_server_failed" for c in cs2), False)
check("  …nor is 'unhealthy' (logged)", any("unhealthy" in c["subject"] for c in cs2), False)
txt = " ".join("%s %s" % (c["title"], c["cause"]) for c in cs2) + " " + " ".join(LOGGED)
check("  …'hassio', the app's slug and the platform's context words are never in the words",
      [w for w in ("hassio", "b333b432", "proos_cameras", "(addon", "dns_server,") if w in txt], [])
b.res = {"issues": [], "suggestions": [], "names": {}, "unhealthy": [], "unsupported": []}
b.reps = []
PF.refresh(b, now=3000); PF.cards()
b.reps = [{"domain": "hassio", "issue_id": "boot_fail", "title": "App failed to start at boot", "severity": "warning", "ignored": False}]
PF.refresh(b, now=4000); PF.cards()
check("  …cleared and back again: logged again", len([w for w in LOGGED if "App failed to start" in w]), 2)
SRV = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
fx = SRV[SRV.index("def _platform_fault_fix("):SRV.index("class _SweepReaders")]
check("1342 · WIRING: Start runs the box's own suggestion; the log writer is the Master Log (SPELLING)",
      ('_supervisor_req("POST", "/resolution/suggestion/%s" % act["uuid"], None)' in fx
       and '_pf.LOG = lambda words: _journal_mod.emit("site", "platform_notice", {"words": words})' in SRV), True)

print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
