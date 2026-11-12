"""The last five Assist tools are benched — every tool now answers to the gate.
Run: python3 tests/assist_last_tools_bench.py

STAGE 6 BUILD 7 (16 Aug 2026). The audit counted 10 unbenched tools;
device_recover + scene_create (201), locks/apply/delete (202), and these
five close the list: scene_dashboard, scene_photo, device_powerlog,
music_search, music_playlist_create.

RAILS DRIVEN — the real tools with fakes:
 scene_dashboard:
  1. only Assist-created scenes may be pinned
  2. no label channel -> the honest sentence, never a silent no-op
  3. show adds the dashboard_scene label PRESERVING existing labels;
     hide removes exactly it; audited
  4. a registry failure is an error, never a false ok
 scene_photo:
  5. only Assist-created scenes; photo_query required
  6. the curated-fallback is SAID (photo_note names the missing key)
 device_powerlog:
  7. entity_id required; hours clamped to 240; errors pass through
 music_search:
  8. music not linked -> the honest sentence; query required
  9. results are slimmed and the note routes to music_play
 music_playlist_create:
 10. created-but-couldn't-add is a WARNING with added:0 — never a
     silent full success; the clean path audits the real track count
"""
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_lt_")
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import assist, powerlog                               # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


AUDITS = []


class WS:
    def __init__(self):
        self.calls = []
        self.labels = ["other_label"]

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd == "config/label_registry/list":
            return [{"label_id": l} for l in self.labels]
        return {}


class Client:
    def __init__(self):
        self.registry = [{"entity_id": "scene.proos_assist_dinner",
                          "labels": ["mood"]}]

    def entity_registry(self):
        return self.registry

    def _req(self, method, path, data=None):
        return {"attributes": {"friendly_name": "Dinner"}}


def runner(ma=None, ws=None):
    r = object.__new__(assist.ToolRunner)
    r.client = Client()
    r.user = {"id": "t1", "tech": True}
    r.ws_call = ws
    r.project = None
    r.ma = ma
    r.actions = []
    r._audit = lambda tool, **kw: AUDITS.append((tool, kw))
    r._scene_cfg_id = lambda eid: eid.split("scene.", 1)[-1]
    r._owns_scene = lambda eid: str(eid).startswith("scene.proos_assist_")
    return r


# ── scene_dashboard ────────────────────────────────────────────────────────
# RE-PINNED, register 1279, with cause (Dave 10 Nov 08:33/08:47: Assist "said it added it to my dashboard but didnt show"; "1. yes"): any scene may be put on the dashboard — the platform's tools make every
# scene Assist builds now, and Pro's own switch always could. Something that is not a scene is still refused.
ws0 = WS()
out = runner(ws=ws0).t_scene_dashboard({"scene_entity_id": "scene.hand_made", "show": True})
check("any scene may be pinned — one the platform made too (1279)", "error" not in out)
out = runner().t_scene_dashboard({"scene_entity_id": "light.office", "show": True})
check("  …but something that is not a scene is refused", "not a scene" in str(out.get("error", "")))
out = runner(ws=None).t_scene_dashboard(
    {"scene_entity_id": "scene.proos_assist_dinner"})
check("no label channel: the honest sentence, never a silent no-op",
      "label update unavailable" in str(out.get("error", "")))
ws = WS()
AUDITS.clear()
out = runner(ws=ws).t_scene_dashboard(
    {"scene_entity_id": "scene.proos_assist_dinner", "show": True})
upd = [kw for c, kw in ws.calls if c == "config/entity_registry/update"]
check("show adds the dashboard label PRESERVING existing labels",
      upd and sorted(upd[0].get("labels", [])) == ["dashboard_scene", "mood"])
check("  …the label is created when missing",
      any(c == "config/label_registry/create" for c, _ in ws.calls))
check("  …audited", AUDITS[-1][0], "scene_dashboard")
ws2 = WS()
ws2.labels = ["dashboard_scene"]
out = runner(ws=ws2).t_scene_dashboard(
    {"scene_entity_id": "scene.proos_assist_dinner", "show": False})
upd = [kw for c, kw in ws2.calls if c == "config/entity_registry/update"]
check("hide removes exactly the dashboard label",
      upd and upd[0].get("labels") == ["mood"])


class BoomWS(WS):
    def __call__(self, cmd, **kw):
        raise RuntimeError("registry down")


out = runner(ws=BoomWS()).t_scene_dashboard(
    {"scene_entity_id": "scene.proos_assist_dinner"})
check("a registry failure is an error, never a false ok",
      "registry down" in str(out.get("error", "")) and out.get("ok") is None)

# ── scene_photo ────────────────────────────────────────────────────────────
out = runner().t_scene_photo({"scene_entity_id": "scene.hand_made",
                              "photo_query": "warm dinner"})
check("only Assist-created scenes may be re-photoed",
      "can only re-photo" in str(out.get("error", "")))
out = runner().t_scene_photo({"scene_entity_id": "scene.proos_assist_dinner"})
check("photo_query is required", "photo_query required" in str(out.get("error", "")))
_real_rp = assist.resolve_scene_photo
from proos import scenephotos as _sp                             # noqa: E402
_real_set = _sp.set_photo
try:
    assist.resolve_scene_photo = lambda nm, q, sid: ("/p/x.jpg", "curated_no_key")
    _sp.set_photo = lambda eid, photo=None: {"record": {"photo": photo}}
    out = runner().t_scene_photo({"scene_entity_id": "scene.proos_assist_dinner",
                                  "photo_query": "warm dinner"})
    check("the curated fallback is SAID — the note names the missing key",
          out.get("ok") is True and "key" in str(out.get("photo_note", "")))
finally:
    assist.resolve_scene_photo = _real_rp
    _sp.set_photo = _real_set

# ── device_powerlog ────────────────────────────────────────────────────────
# register 1324: a read tool's line to the platform refuses anything but reads
_rr = runner(); _rr.ws_call = lambda t, **k: ["sent", t]
_refused = False
try:
    _rr.ws_read("call_service", domain="light", service="turn_on")
except PermissionError:
    _refused = True
check("ws_read: the account list passes; a command that acts on the home is refused",
      (_rr.ws_read("config/auth/list"), _refused), (["sent", "config/auth/list"], True))
out = runner().t_device_powerlog({})
check("powerlog: entity_id required",
      "entity_id required" in str(out.get("error", "")))
class Proj:
    @staticmethod
    def load():
        return {}


_real_fetch = powerlog.fetch_log
try:
    seen = {}
    # RE-PINNED with cause, register 1324: fetch_log now also takes the platform's account list (ws_read=, a READ-only
    # line) to name who did it; the rule pinned — delegate, hours clamped to 240, errors pass through — is unchanged.
    powerlog.fetch_log = (lambda client, load, eid, hours, ws_read=None:
                          seen.update(eid=eid, hours=hours, ws=ws_read) or {"ok": True})
    r = runner()
    r.project = Proj
    out = r.t_device_powerlog({"entity_id": "media_player.tv",
                               "hours": 999})
    check("powerlog delegates with hours clamped to 240",
          (out.get("ok"), seen.get("hours")), (True, 240.0))

    def _boom(client, load, eid, hours, ws_read=None):
        raise RuntimeError("recorder offline")

    powerlog.fetch_log = _boom
    r = runner()
    r.project = Proj
    out = r.t_device_powerlog({"entity_id": "media_player.tv"})
    check("  …and a recorder failure passes through as an error",
          "recorder offline" in str(out.get("error", "")))
finally:
    powerlog.fetch_log = _real_fetch

# ── music_search ───────────────────────────────────────────────────────────
out = runner(ma=None).t_music_search({"query": "jazz"})
# PIN REWRITTEN (16 Aug 2026, register 217 — Dave: "go look for an
# alternative, you're writing rules again"): the refusal now names the
# engine as OPTIONAL and carries the generic path as information. The
# RULE — no engine never fakes a result — is unchanged.
check("music not linked: the honest sentence",
      "isn't linked" in str(out.get("error", ""))
      and "optional" in str(out.get("error", "")).lower())


class MA:
    def __init__(self):
        self.searched = None
        self.created = []
        self.added = []
        self.add_fails = False

    def search(self, q, media_types=None, limit=6):
        self.searched = (q, limit)
        return {"tracks": [{"uri": "library://track/1"}]}

    def create_playlist(self, name):
        self.created.append(name)
        return {"item_id": "pl1"}

    def playlist_add(self, pid, uris):
        if self.add_fails:
            raise RuntimeError("engine busy")
        self.added.append((pid, uris))


ma = MA()
r = runner(ma=ma)
r._slim_search = lambda res: {"slimmed": True}
out = r.t_music_search({"query": ""})
check("music_search: query required", "query required" in str(out.get("error", "")))
out = r.t_music_search({"query": "jazz"})
check("results are slimmed and the note routes to music_play",
      out.get("results") == {"slimmed": True}
      and "music_play" in str(out.get("note", ""))
      and ma.searched == ("jazz", 6))

# ── music_playlist_create ──────────────────────────────────────────────────
out = runner(ma=None).t_music_playlist_create({"name": "Sunday"})
# PIN REWRITTEN with 217 — same reason as music_search above.
check("playlist: music not linked is the honest sentence",
      "isn't linked" in str(out.get("error", ""))
      and "optional" in str(out.get("error", "")).lower())
ma = MA()
AUDITS.clear()
out = runner(ma=ma).t_music_playlist_create(
    {"name": "Sunday", "track_uris": ["u1", "u2"]})
check("the clean path creates, adds, and audits the REAL track count",
      (out.get("added"), AUDITS[-1][1].get("tracks")), (2, 2))
ma = MA()
ma.add_fails = True
out = runner(ma=ma).t_music_playlist_create(
    {"name": "Sunday", "track_uris": ["u1"]})
check("created-but-couldn't-add is a WARNING with added:0 — never a "
      "silent full success",
      out.get("added") == 0 and "couldn't add tracks" in str(out.get("warning", "")))

print("\n%d checks passed, %d failed%s"
      % (PASS, len(FAIL), ("  " + str(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
