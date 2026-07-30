"""
Verdict-ladder bench — every scenario the live house produced on 28 Jul 2026,
replayed offline against the pure decide(). Run:  python3 tests/verdict_bench.py
A change to the ladder ships ONLY when this file is green.
"""
import sys, os, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from proos.ctlbridge import decide

AREA = "bedroom"
DISP = "media_player.bedroom_tv"
ATV, SHIELD = "media_player.bedroom_apple_tv", "media_player.bedroom_shield"
AVR = "media_player.avr"


def act(key, source, route=None, aw=None, summary=False, targets_eid=DISP, prov=False):
    a = types.SimpleNamespace()
    a.key, a.source_eid, a.route, a.audio_witness = key, source, route, aw
    a.provisional = prov
    a.targets = [types.SimpleNamespace(entity_id=targets_eid)]
    a.summary = (lambda snap: types.SimpleNamespace(ok=summary(snap))) if callable(summary) \
        else (lambda snap: types.SimpleNamespace(ok=summary))
    a.label = key
    return a


def bc(summary=True, tuner="TV", prov=False):
    b = act("watch_tv", None, route={"select_source": tuner}, summary=summary, prov=prov)
    b.source_eid = None
    return b


def S(disp="on", disp_attrs=None, **entities):
    snap = {DISP: {"state": disp, "attributes": disp_attrs or {}}}
    for eid, val in entities.items():
        pass
    return snap


def ent(snap, eid, state, attrs=None):
    snap[eid] = {"state": state, "attributes": attrs or {}}
    return snap


PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")


def run(name, sweeps, acts, bcast, wit=None, want=None, want_attrs=None):
    """sweeps: list of snap dicts; verdict checked after the LAST sweep."""
    mem = {}
    out = None
    published = None
    for snap in sweeps:
        mem["last"] = published
        out = decide(AREA, snap, acts, bcast, wit or {}, mem,
                     art_check=lambda s, e: str(((s.get(e) or {}).get("attributes") or {})
                                                .get("art_mode_status", "")).lower() == "on")
        if not out["defer"]:
            published = out["state"]
    check(name, published, want)
    if want_attrs:
        for k, v in want_attrs.items():
            check(f"{name}.{k}", out.get(k), v)
    return out


atv = lambda **kw: act("watch_bedroom_apple_tv", ATV, **kw)
shd = lambda **kw: act("watch_bedroom_shield", SHIELD, **kw)

# ── 1. verified activity wins and cleans its name ──
snap = ent(ent(S("on"), ATV, "playing"), SHIELD, "off")
run("verified apple", [snap], [atv(summary=True), shd()], bc(),
    want="watch_apple_tv", want_attrs={"verified": True})

# ── 2. return to tuner: display testimony beats idle shield ──
snap = ent(ent(S("on", {"source": "TV"}), ATV, "off"), SHIELD, "idle")
run("tuner beats idle shield", [snap], [atv(), shd()], bc(True),
    want="watch_tv")

# ── 3. TV off, shield still idle -> off after confirm (2 dark sweeps) ──
s_on = ent(ent(S("on", {"source": "HDMI 2"}), ATV, "off"), SHIELD, "idle")
s_off = ent(ent(S("off"), ATV, "off"), SHIELD, "idle")
run("tv off kills idle shield", [s_on, s_off, s_off, s_off],
    [atv(), shd(route={"hdmi_code": "KEY_HDMI2"})], bc(), want="off")

# ── 4. Art Mode = dark (art_mode_status attr) ──
s_art = ent(ent(S("on", {"art_mode_status": "on"}), ATV, "off"), SHIELD, "idle")
run("art mode is off", [s_on, s_art, s_art, s_art],
    [atv(), shd(route={"hdmi_code": "KEY_HDMI2"})], bc(), want="off")

# ── 5. session drop mid-watch: hold while display up ──
s_watch = ent(ent(S("on", {"source": "HDMI 1"}), ATV, "playing"), SHIELD, "off")
s_drop = ent(ent(S("on", {"source": "HDMI 1"}), ATV, "off"), SHIELD, "off")
# (display testimony now answers this directly -- memory no longer needed
#  when the panel's committed input names the source; held stays False)
run("hold through session drop",
    [s_watch, s_drop, s_drop],
    [atv(summary=lambda s: s[ATV]["state"] == "playing",
         route={"hdmi_code": "KEY_HDMI1"}), shd()],
    bc(), want="watch_apple_tv", want_attrs={"held_now": False, "evidence": "display"})

# ── 6. traffic witness revives a lying integration ──
s_tr = ent(ent(ent(S("on", {"source": "HDMI 1"}), ATV, "off"), SHIELD, "off"),
           "sensor.atv_rx", "2.4")
run("traffic witness confirms",
    [s_tr], [atv(), shd()], bc(),
    wit={ATV: {"sensors": ["sensor.atv_rx"], "min": 0.25}},
    want="watch_apple_tv", want_attrs={"evidence": "traffic"})

# ── 7. AVR testimony picks among two alive ──
s_avr = ent(ent(ent(S("on", {"source": "HDMI 4"}), ATV, "idle"), SHIELD, "idle"),
            AVR, "on", {"source": "SHIELD"})
run("avr testimony picks shield", [s_avr],
    [atv(aw={"entity": AVR, "source": "Apple TV"}),
     shd(aw={"entity": AVR, "source": "SHIELD"})],
    bc(), want="watch_shield")

# ── 8. two alive, no testimony -> abstain, memory holds prior truth ──
s_first = ent(ent(S("on"), ATV, "playing"), SHIELD, "off")
s_ambig = ent(ent(S("on"), ATV, "idle"), SHIELD, "idle")
run("ambiguous abstains to held", [s_first, s_ambig],
    [atv(summary=lambda s: s[ATV]["state"] == "playing"), shd()],
    bc(False), want="watch_apple_tv", want_attrs={"held_now": True})

# ── 9. broadcast may not claim a non-tuner input ──
s_h2 = ent(ent(S("on", {"source": "HDMI 2"}), ATV, "off"), SHIELD, "off")
out = run("broadcast blocked off-tuner", [s_h2, s_h2], [atv(), shd()], bc(True),
          want="off")

# ── 10. single display blip does not kill the room ──
s_blip = ent(ent(S("unavailable"), ATV, "playing"), SHIELD, "off")
run("blip survives via live source", [s_watch, s_blip],
    [atv(summary=lambda s: s.get(ATV, {}).get("state") == "playing",
         route={"hdmi_code": "KEY_HDMI1"}), shd()],
    bc(), want="watch_apple_tv")

# ── 11. whole room unplugged: unavailable + nothing alive -> off, hold dies ──
s_dead = ent(ent(S("unavailable"), ATV, "unavailable"), SHIELD, "unavailable")
run("unplugged room goes off", [s_watch, s_dead, s_dead, s_dead],
    [atv(summary=lambda s: s.get(ATV, {}).get("state") == "playing"), shd()],
    bc(False), want="off")

# ── 12. held replay keeps a sane label (not 'Off') ──
out = run("held label sane", [s_watch, s_drop, s_drop],
          [atv(summary=lambda s: s[ATV]["state"] == "playing",
               route={"hdmi_code": "KEY_HDMI1"}), shd()], bc(),
          want="watch_apple_tv")

# ── 13. off->on same sweep responds immediately (no debounce on the way UP) ──
run("instant on", [s_off, s_watch],
    [atv(summary=lambda s: s.get(ATV, {}).get("state") == "playing"), shd()],
    bc(False), want="watch_apple_tv")

# ── 14. area-prefix cleaning + collision guard ──
a1 = act("watch_bedroom_apple_tv", ATV)
a2 = act("watch_apple_tv", "media_player.other_atv")
snap = ent(ent(ent(S("on"), ATV, "playing"), "media_player.other_atv", "off"), SHIELD, "off")
o = decide(AREA, snap, [a1, a2], bc(False), {}, {})
check("collision keeps raw key", o["state"], "watch_bedroom_apple_tv")

# ── 15. committed display_input route drives rung-2 testimony ──
s15 = ent(ent(S("on", {"source": "HDMI 2"}), ATV, "idle"), SHIELD, "idle")
run("display_input route picks apple", [s15],
    [atv(route={"display_input": "HDMI 2"}), shd(route={"display_input": "HDMI 3"})],
    bc(False), want="watch_apple_tv")

# ── 16. walk-in with DEAD session: verdict stays honest (off), matcher
#        still identifies the intended activity for the converger ──
from proos.ctlbridge import route_matches
s16 = ent(ent(S("on", {"source": "HDMI 2"}), ATV, "off"), SHIELD, "off")
o16 = decide(AREA, s16, [atv(route={"display_input": "HDMI 2"}),
                         shd(route={"display_input": "HDMI 3"})], bc(True), {}, {"last": "off"})
check("dead-session walk-in names the input's source", o16["state"], "watch_apple_tv")
check("dead-session claim carries display evidence", o16["evidence"], "display")

# ── 17. idle shield may NOT claim a panel on apple's committed input ──
s17 = ent(ent(S("on", {"source": "HDMI 2"}), ATV, "off"), SHIELD, "idle")
o17 = decide(AREA, s17, [atv(route={"display_input": "HDMI 2"}),
                         shd(route={"display_input": "HDMI 3"})], bc(False), {}, {})
check("panel input beats idle shield", o17["state"], "watch_apple_tv")

# ── 18. sole-alive with contradicting route abstains ──
s18 = ent(ent(S("on", {"source": "HDMI 2"}), ATV, "unknown"), SHIELD, "idle")
o18 = decide(AREA, s18, [act("watch_bedroom_apple_tv", ATV),
                         shd(route={"display_input": "HDMI 3"})], bc(False), {}, {"last": "off"})
check("contradicted sole-alive abstains", o18["state"], "off")
check("matcher IDs the walk-in intent",
      route_matches(atv(route={"display_input": "HDMI 2"}), "HDMI 2"), True)

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
