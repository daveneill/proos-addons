"""
ProOS Core -- the commissioning PROJECT (the AV orchestration model).

The declared source of truth for what each area's AV system IS: which media_players
are committed to the room, what role each plays (display / source / tvaudio / speaker),
and the audio path. It is the ProOS equivalent of a Control4 project -- except the same
declared model feeds BOTH planes:

  - control:   the generator builds one-touch activities from the declared roles.
  - awareness: the watcher takes the committed set as its expected-state baseline.
               (You can only be *aware* a device is missing/wedged once you've declared
               it should be there -- auto-discovery alone can't tell "gone" from
               "never here". The committed project IS that baseline.)

Where it lives:
  - Persisted at /data/proos_project.json (same pattern as recovery_config.json etc.).
  - Mirrored to HA ENTITY LABELS for every COMMITTED area -- proos_av (membership) plus
    proos_<role> -- so membership is visible and hand-editable in HA, and the dashboard
    (which already reads these labels) filters each room to its committed AV set. The
    project file stays authoritative for anything relational; labels are the mirror.

How it's built:
  - suggest(client) wraps discover_av() per area. discover_av is integration-filtered
    (it only maps known AV integrations -> roles), so Macs, phones and intercom speakers
    never enter the suggestion. Suggestions start uncommitted; nothing touches HA until
    an installer commits an area (create-if-absent: committing never clobbers prior
    committed edits).

Stdlib-only, like the rest of Core. HA writes go through ha_ws.ws_command (the same WS
path area/device/entity registry reads already use).
"""
from __future__ import annotations

import json
import os
import re
import datetime

from . import sync
from . import discovery
from .discovery import discover_av
from .ha_ws import ws_command

DEFAULT_PATH = os.environ.get("PROOS_PROJECT_PATH", "/data/proos_project.json")
SCHEMA_VERSION = 1

# Entity-label vocabulary this module owns. The mirror only ever adds/removes THESE
# labels on an entity; any other labels (proos_pause_*, proos_tv_combined, dashboard_*)
# are left untouched. Keep in lockstep with dashboard.html's LABEL_AV / LABEL_ROLE_*.
LABEL_AV = "proos_av"
LABEL_DISPLAY = "proos_display"
LABEL_SOURCE = "proos_source"
LABEL_TVAUDIO = "proos_tvaudio"
LABEL_SPEAKER = "proos_speaker"
# The room's committed AV switch (any-brand AVR/matrix doing video+audio
# switching). The dashboard uses it for two-hop active-source resolution:
# display input -> switch (via the switch's proos_input_<output>), then the
# SWITCH's current source -> the streamer (via proos_input_<avr input> on it).
LABEL_AVSWITCH = "proos_avswitch"
_ROLE_LABELS = {LABEL_AV, LABEL_DISPLAY, LABEL_SOURCE, LABEL_TVAUDIO, LABEL_SPEAKER,
                LABEL_AVSWITCH}
# Per-source input assignment is mirrored as a dynamic label proos_input_<slug> on the
# source entity (e.g. Apple TV on 'HDMI 2' -> proos_input_hdmi_2). The dashboard matches
# the display's reported input to find the active source even when the source lies about
# its power. These are 'managed' like the role labels (added/removed by the mirror).
_INPUT_PREFIX = "proos_input_"


def _input_label(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return _INPUT_PREFIX + (slug or "x")


def _is_managed(label: str) -> bool:
    return label in _ROLE_LABELS or label.startswith(_INPUT_PREFIX)


# ── PERSISTENCE ──────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "updated": None, "areas": {}}


def _normalize_slots(rec: dict) -> None:
    """Endpoint model Stage 3 (ProOS_Endpoint_Model_Spec §4): slots and the
    legacy fields are two views of one truth, normalised HERE, both
    directions, committed rooms only.

    * A LEGACY record (no `slots` key — everything Pro writes today) gains
      `slots` as an in-memory derived view: video from `display`, audio from
      `speakers`∪`audio`, video_audio from `tvaudio`, switch from
      `avswitch.entity`. Sources are NOT slots — they stay `sources`.
    * A SLOT-BEARING record (a future Pro writes slots as truth) gets its
      legacy fields DERIVED from the slots, so every consumer — six Core
      modules, both PWAs' ~480 label refs, the label mirror — keeps reading
      exactly what it reads today (§4.5 rule 1). The `avswitch` dict (edges:
      inputs/output/broadcast) is signal-graph data and stays stored, not
      derived.
    * Slots are keyed by BOUND ENTITY, never position (settled 31 Jul):
      lists of {"entity": eid}, order = priority, primary first. Removing an
      endpoint can never renumber another.
    * The disk file is never rewritten by a read; this runs on the loaded
      dict only.
    """
    if not rec.get("committed"):
        return

    def _ents(lst):
        out = []
        for it in (lst or []):
            eid = it.get("entity") if isinstance(it, dict) else it
            if isinstance(eid, str) and eid and eid not in out:
                out.append(eid)
        return out

    slots = rec.get("slots")
    if isinstance(slots, dict):
        # slots are the stored truth -> derive the legacy views
        video = _ents(slots.get("video"))
        audio = _ents(slots.get("audio"))
        va = _ents(slots.get("video_audio"))
        rec["display"] = video[0] if video else None
        # A4 (3 Aug): a SECOND video endpoint was silently dropped from
        # every derived view — committed but invisible to awareness.
        # displays[] carries them all; display stays the primary.
        rec["displays"] = list(video)
        rec["speakers"] = list(audio)
        rec["audio"] = list(audio)
        rec["tvaudio"] = va[0] if va else None
    else:
        # legacy record -> derive slots as the in-memory view
        audio = _ents(list(rec.get("speakers") or [])
                      + list(rec.get("audio") or []))
        sw = (rec.get("avswitch") or {}).get("entity")
        rec["displays"] = [rec["display"]] if rec.get("display") else []
        rec["slots"] = {
            "video": [{"entity": rec["display"]}] if rec.get("display") else [],
            "audio": [{"entity": e} for e in audio],
            "video_audio": ([{"entity": rec["tvaudio"]}]
                            if rec.get("tvaudio") else []),
            "switch": {"entity": sw} if sw else None,
        }


def _derive_kind(rec: dict) -> None:
    """kind follows what was COMMITTED, not what was discovered.

    Stage 2 of the endpoint model (ProOS_Endpoint_Model_Spec_2026-07-31.md).
    The stored flag is computed at suggest time from CANDIDATES
    (`kind = "tv" if (disp_c or src_c) else "music"`), so an apple_tv-platform
    HomePod — a source *candidate* whatever the installer commits it as —
    poisoned the Office into a stored 'tv': a tv room with no display, which
    publishes nothing at all. Turning Source off in Pro couldn't fix it,
    because membership never fed the flag. Measured live, 1 Aug 2026.

    The rule: a COMMITTED room's kind is derived from its committed display —
    the one thing a watch activity cannot exist without. No display ⇒ music.
    Committed sources without a display do not make a tv room. This is the
    same fact the endpoint model will later read off the slots ("no video
    endpoint ⇒ music"); this stage moves the truth source, Stage 3 moves the
    storage.

    UNCOMMITTED rooms keep the stored suggest-time hint: it describes the
    candidates so the UI can label a TV room before anything is added, and
    deriving from empty membership would flip every suggestion to music.

    Derived HERE, at load, in one place — nine modules read rec["kind"]
    (assist, credentials, ctlbridge, healthmon, journal, musicstat, project,
    watcher, server) and none of them derives it, so none of them can drift
    (spec §4.5 rule 2). The file on disk is never rewritten by a read.
    """
    if rec.get("committed"):
        rec["kind"] = "tv" if rec.get("display") else "music"


def load(path: str = DEFAULT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "areas" not in data:
            return _empty()
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("areas", {})
        for rec in data["areas"].values():
            if isinstance(rec, dict):
                _normalize_slots(rec)     # Stage 3: slots <-> legacy, one place
                _derive_kind(rec)         # Stage 2: kind follows the display,
                                          # which now itself follows the slots —
                                          # "no video slot => music", one fact
        return data
    except FileNotFoundError:
        return _empty()
    except Exception:
        # A corrupt project must never take the box down; treat as empty and let the
        # next save overwrite it.
        return _empty()


def save(project: dict, path: str = DEFAULT_PATH) -> dict:
    project = dict(project or {})
    project["version"] = SCHEMA_VERSION
    project["updated"] = datetime.datetime.utcnow().isoformat() + "Z"
    project.setdefault("areas", {})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(project, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return project


def clear(path: str = DEFAULT_PATH) -> bool:
    """Delete the persisted commissioning project so it starts blank after a home/factory
    reset. The project lives in the add-on's /data, which the HA baseline restore and the
    factory .storage wipe DON'T touch — so without this it would survive a reset stale,
    pointing at rooms/entities that were wiped."""
    ok = True
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        ok = False
    return ok


# ── SUGGESTION (discover_av per area) ────────────────────────────────────────

def _tv_capable(client, entities):
    """Which of these audio entities expose a 'TV' source (one render for the set)."""
    if not entities:
        return {}
    tmpl = (
        "{{ {"
        + ", ".join(
            "%s: ('TV' in (state_attr(%s,'source_list') or []))"
            % (json.dumps(e), json.dumps(e))
            for e in entities
        )
        + "} | to_json }}"
    )
    try:
        return json.loads(client.render_template(tmpl)) or {}
    except Exception:
        return {}


def _discrete_inputs(client, display_eid):
    """The display's selectable discrete HDMI inputs (['HDMI 1', ...]) or []. Present =
    the driver can route by explicit input (deterministic one-touch); absent = the room
    falls back to HDMI-CEC one-touch (best-effort). Certified drivers guarantee these."""
    if not display_eid:
        return []
    try:
        sl = json.loads(client.render_template(
            "{{ state_attr('%s','source_list') | to_json }}" % display_eid)) or []
    except Exception:
        return []
    return [s for s in sl if isinstance(s, str) and s.strip().lower().startswith("hdmi")]


def _area_record(client, cluster) -> dict:
    # STRICT OPT-IN (v1.0.74): discovery NEVER makes a device a room member. Everything HA
    # placed in the area is offered as a CANDIDATE the installer must add on purpose. This
    # is the fix for "Sonos / Google Cast auto-add to rooms": HA auto-assigns areas on
    # onboarding, and ProOS used to treat area membership AS room membership, so a single
    # Commit labelled the whole area. Now nothing is a member until explicitly added, and
    # only members are ever labelled/committed. Membership != HA area.
    disp_c = cluster.display.entity if cluster.display else None
    src_c = [d.entity for d in cluster.sources]
    aud_c = [d.entity for d in cluster.audio]
    # Per-device metadata: integration + certification tier (certified drivers add the
    # discrete commands + awareness that native integrations omit). For the display we
    # also record its discrete HDMI inputs so the UI can promise deterministic one-touch
    # (present) or flag best-effort CEC (absent). display_ok gates the display role so a
    # non-display integration (Cast/Sonos/…) can never occupy the screen slot, whatever
    # the device happens to be named. meta covers every CANDIDATE, so the add list can
    # show tier/inputs before a device is committed.
    meta = {}
    for d in (([cluster.display] if cluster.display else [])
              + list(cluster.sources) + list(cluster.audio)):
        meta[d.entity] = {"integration": d.integration,
                          "tier": getattr(d, "tier", "compatible"),
                          "capabilities": discovery.capabilities(d.integration),
                          "version": discovery.driver_version(d.integration),
                          "display_ok": discovery.ROLE_BY_INTEGRATION.get(d.integration) == "display"}
    if disp_c:
        di = _discrete_inputs(client, disp_c)
        meta[disp_c]["discrete_inputs"] = di
        # Earn the badge, don't assume it: a display claiming certified discrete_input must
        # actually expose HDMI inputs on THIS box. If it doesn't, note it so the UI can
        # show "certified driver, discrete inputs not detected" rather than over-promise.
        caps = set(meta[disp_c]["capabilities"])
        if "discrete_input" in caps:
            meta[disp_c]["discrete_input_verified"] = bool(di)
    # kind reflects what's AVAILABLE (candidates), so the UI can still label a TV room vs a
    # music zone before anything is added. No warnings at suggest time — warnings are about
    # the CHOSEN set (e.g. sources added but no display) and are computed as the installer
    # builds the room, not pre-emptively.
    kind = "tv" if (disp_c or src_c) else "music"
    return {
        # Immutable HA area_id — what generated script ids key on — plus the human name for
        # display only. Never generate anything from the name.
        "area_id": getattr(cluster, "area_id", "") or "",
        "name": cluster.area,
        # Members start EMPTY — the installer opts each device in.
        "display": None,
        "sources": [],
        "audio": [],
        "tvaudio": None,
        "speakers": [],
        "inputs": {},   # source_entity -> display input name (e.g. 'HDMI 2'); set at review
        # Explicit AV-switch plan when an any-brand AVR/matrix does the room's
        # video+audio switching: {entity, inputs:{source: avr_input}, output,
        # broadcast}. None = display-direct routing (the default).
        "avswitch": None,
        "off_state": "full",  # 'full' = real power off | 'art' = rest a Frame TV in Art Mode
        "meta": meta,
        # Candidates = discovered-in-area AV devices, grouped by their natural role, offered
        # for explicit add. Being here is NOT membership.
        "candidates": {"display": disp_c, "sources": src_c, "audio": aud_c},
        "kind": kind,
        "warnings": [],
        "committed": False,
    }


def unassigned(client) -> list:
    """AV‑integration media_players HA discovered but that aren't in any area yet.

    These never reach discover_av (it works per area), so a TV that wasn't assigned to a
    room silently goes missing. The installer pulls one into a room from the UI, which also
    sets its HA area. Integration‑filtered, so only real AV devices appear here."""
    known = json.dumps(discovery._KNOWN)
    tmpl = (
        "{% set known = " + known + " %}"
        "{% set ns = namespace(rows=[]) %}"
        "{% for k in known %}"
        "{% for e in integration_entities(k) if e is match('media_player\\.') and not area_name(e) %}"
        "{% set ns.rows = ns.rows + [{'entity': e, 'name': state_attr(e,'friendly_name'), 'integration': k}] %}"
        "{% endfor %}"
        "{% endfor %}"
        "{{ ns.rows | to_json }}"
    )
    try:
        rows = json.loads(client.render_template(tmpl)) or []
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        e = r.get("entity")
        if not e or e in seen:
            continue
        seen.add(e)
        integ = r.get("integration", "unknown")
        out.append({"entity": e, "name": r.get("name") or e, "integration": integ,
                    "tier": discovery.tier(integ),
                    "suggested_role": discovery.ROLE_BY_INTEGRATION.get(integ),
                    "capabilities": discovery.capabilities(integ)})
    return out


def verify(client, project: dict) -> dict:
    """Stage‑4 proof: for every COMMITTED area, is each member reachable right now?

    Honest green/red — reachable = a real state (not unavailable/unknown/absent). This is
    what turns the 'now watching' promise into something the installer can see, not assume.
    Script‑presence and watcher‑coverage checks layer on top later."""
    out = {}
    for area, rec in (project or {}).get("areas", {}).items():
        if not rec.get("committed"):
            continue
        members = _area_members(rec)
        if not members:
            out[area] = {"reachable": [], "unreachable": [], "ok": True}
            continue
        tmpl = ("{{ {" + ", ".join(
            "%s: states(%s)" % (json.dumps(e), json.dumps(e)) for e in members)
            + "} | to_json }}")
        try:
            states = json.loads(client.render_template(tmpl)) or {}
        except Exception:
            states = {}
        reach, unreach = [], []
        for e in members:
            s = states.get(e)
            (unreach if s in (None, "unavailable", "unknown") else reach).append(e)
        out[area] = {"reachable": reach, "unreachable": unreach, "ok": not unreach}
    # Alias each entry by the room's display name too, so a reader keyed by either the
    # area_id (canonical) or the name works across the migration.
    for k in list(out.keys()):
        nm = ((project or {}).get("areas", {}).get(k) or {}).get("name")
        if nm and nm not in out:
            out[nm] = out[k]
    return {"areas": out, "ok": all(a["ok"] for a in out.values()) if out else True}


# ── Identity anchors + reconcile (matrix #12) ───────────────────────────────
# Delete-and-re-add an integration and HA can mint new entity ids (the _2
# twins). Committed records then rot silently — scripts drive corpses,
# verdicts read corpses ("this wrecked Family and Living Rooms for most of
# today and nobody was told", What It Cannot Do §3). healthmon ALARMS on it;
# these two functions let a rescan REPAIR it. Identity is the registry's
# (platform, unique_id) — the one key HA holds stable across re-pairs. Never
# a name, never a derived id (the identity standard, register D7).

def _member_eids(rec) -> list:
    out = []
    for item in ([rec.get("display")] + list(rec.get("sources") or [])
                 + list(rec.get("speakers") or []) + list(rec.get("audio") or [])
                 + [rec.get("tvaudio")]
                 + [((rec.get("avswitch") or {}).get("entity"))]):
        eid = item.get("entity") if isinstance(item, dict) else item
        if isinstance(eid, str) and eid and eid not in out:
            out.append(eid)
    return out


def capture_anchors(rec, registry) -> dict:
    """Anchor every committed member to its registry identity. Pure.

    Registry truth only: an entity the registry doesn't know gets NO anchor —
    inventing one would anchor to a guess. Called at save time (server), so a
    commit refreshes anchors for the ids the installer just chose."""
    if not (rec and rec.get("committed")) or not registry:
        return {}
    by_eid = {r.get("entity_id"): r for r in registry
              if isinstance(r, dict) and r.get("entity_id")}
    out = {}
    for eid in _member_eids(rec):
        entry = by_eid.get(eid)
        if entry and entry.get("unique_id") is not None:
            out[eid] = {"platform": entry.get("platform"),
                        "unique_id": entry.get("unique_id"),
                        "device_id": entry.get("device_id")}
    return out


def reconcile_identities(rec, registry):
    """Repair a committed room whose anchored entities were renamed. Pure.

    Returns (record, renames). Fail-open on every edge: no anchors, registry
    unreadable, unique_id vanished (the ALARM's job, not ours), or the old id
    still live -> untouched, {} renames. The rename is applied to EVERY field
    that names entities — a half-renamed record is worse than a stale one —
    and reported back for journalling: silent repair is how trust dies."""
    if not (rec and rec.get("committed")) or not registry:
        return rec, {}
    anchors = rec.get("anchors") or {}
    if not anchors:
        return rec, {}
    live = {r.get("entity_id") for r in registry
            if isinstance(r, dict) and r.get("entity_id")}
    by_key = {}
    for r in registry:
        if isinstance(r, dict) and r.get("unique_id") is not None:
            by_key[(r.get("platform"), r.get("unique_id"))] = r.get("entity_id")

    renames = {}
    for old, a in anchors.items():
        if old in live:
            continue                                  # nothing actually renamed
        new = by_key.get(((a or {}).get("platform"), (a or {}).get("unique_id")))
        if new and new != old:
            renames[old] = new
    if not renames:
        return rec, {}

    def _sub(eid):
        return renames.get(eid, eid)

    def _sublist(lst):
        out = []
        for item in (lst or []):
            eid = _sub(item) if isinstance(item, str) else item
            if eid not in out:                        # a rename never duplicates
                out.append(eid)
        return out

    rec = dict(rec)
    if isinstance(rec.get("display"), str):
        rec["display"] = _sub(rec["display"])
    rec["sources"] = _sublist(rec.get("sources"))
    rec["speakers"] = _sublist(rec.get("speakers"))
    rec["audio"] = _sublist(rec.get("audio"))
    if isinstance(rec.get("tvaudio"), str):
        rec["tvaudio"] = _sub(rec["tvaudio"])
    rec["inputs"] = {_sub(k): v for k, v in (rec.get("inputs") or {}).items()}
    sw = rec.get("avswitch")
    if isinstance(sw, dict) and sw.get("entity"):
        sw = dict(sw)
        sw["entity"] = _sub(sw["entity"])
        sw["inputs"] = {_sub(k): v for k, v in (sw.get("inputs") or {}).items()}
        rec["avswitch"] = sw
    rec["anchors"] = {_sub(k): v for k, v in anchors.items()}
    # Stage 3: slot bindings are entities, so they rename with everything else
    # (a half-renamed record is worse than a stale one).
    slots = rec.get("slots")
    if isinstance(slots, dict):
        slots = dict(slots)
        for key in ("video", "audio", "video_audio"):
            slots[key] = [dict(x, entity=_sub(x.get("entity")))
                          if isinstance(x, dict) else x
                          for x in (slots.get(key) or [])]
        sw2 = slots.get("switch")
        if isinstance(sw2, dict) and sw2.get("entity"):
            slots["switch"] = dict(sw2, entity=_sub(sw2["entity"]))
        rec["slots"] = slots
    return rec, renames


def suggest(client) -> dict:
    """Fresh suggestion for every area, from discover_av (integration-filtered)."""
    areas = {}
    for area in sync.list_areas(client):
        try:
            cluster = discover_av(client, area)
        except Exception:
            continue
        rec = _area_record(client, cluster)
        cand = rec.get("candidates") or {}
        if cand.get("display") or cand.get("sources") or cand.get("audio"):
            areas[rec.get("area_id") or area] = rec   # key by the IMMUTABLE area_id, never the name
    return {"version": SCHEMA_VERSION, "updated": None, "areas": areas}


def merge(existing: dict, suggestion: dict) -> dict:
    """Overlay a fresh suggestion on the stored project.

    Membership is the INSTALLER's, never discovery's: an area that already exists in the
    stored project keeps its member selections (display/sources/audio/tvaudio/inputs/
    off_state) and its committed flag VERBATIM. Only the CANDIDATE list and per-device
    meta are refreshed from the fresh scan — so a newly-onboarded device shows up as
    something to add, and a device that vanished drops out of the candidates, without ever
    silently joining or leaving a room. A brand-new area starts with empty members (strict
    opt-in). Committed areas whose devices all went offline (absent from the scan) are
    preserved as-is."""
    ex = (existing or {}).get("areas", {})
    out = {"version": SCHEMA_VERSION, "updated": (existing or {}).get("updated"), "areas": {}}
    # Suggestion is keyed by the immutable area_id. The stored project may be keyed by the
    # old room NAME (pre-migration) OR the area_id — so match the prior record by either.
    for area, rec in suggestion.get("areas", {}).items():   # 'area' is the area_id
        prior = ex.get(area) or ex.get(rec.get("name"))
        if prior:
            merged = dict(prior)
            merged["candidates"] = rec.get("candidates", {})
            # Fresh meta wins for rediscovered devices (keeps discrete_inputs current);
            # prior meta is retained for a committed member that wasn't rediscovered.
            merged["meta"] = {**(prior.get("meta") or {}), **(rec.get("meta") or {})}
            merged["kind"] = rec.get("kind", prior.get("kind", "music"))
            merged["area_id"] = area                         # stamp the immutable key onto the record
            merged.setdefault("name", rec.get("name"))
            out["areas"][area] = merged
        else:
            out["areas"][area] = rec
    # Preserve committed rooms not rediscovered this scan (devices offline), re-keyed to their
    # area_id; skip any already present (matched by area_id or name) so a migrated room can't
    # appear twice.
    present_ids = set(out["areas"].keys())
    present_names = {r.get("name") for r in out["areas"].values() if r.get("name")}
    for k, rec in ex.items():
        if not rec.get("committed"):
            continue
        aid = rec.get("area_id") or k
        if aid in present_ids or (rec.get("name") and rec.get("name") in present_names):
            continue
        out["areas"][aid] = rec
    return out


# ── NO‑AUTO‑ROOM QUARANTINE (AV‑scoped) ──────────────────────────────────────
# "Nothing shows in a room until the installer places it." HA stamps a device's area at
# pairing when its name matches a room (a HomePod called 'Office' -> Office), and a room
# shows that device on the dashboard — so an auto‑guess appears uninvited. Opt‑in
# membership stops ProOS COMMITTING it, but the auto DEVICE area still makes it show. So
# ProOS clears the auto‑stamped DEVICE area off any AV media device the installer hasn't
# placed, dropping it into Unassigned.
#
# AV‑SCOPED on purpose: only media devices on AV integrations are touched. Lights, sensors,
# switches keep their rooms (clearing those would empty the dashboard rooms and there's no
# ProOS flow to re‑place them). A device is LEFT ALONE when it's a committed member
# (proos_av), an in‑progress member of the saved project, or the installer pinned it with
# an entity‑level area override (how every ProOS "Add"/assign records an explicit
# placement — set_entity_area, which survives a device‑area clear). No baseline and no
# per‑device memory: it simply reasserts "unplaced AV ⇒ no room" every scan, so a
# delete+re‑add (same device_id, new pairing) is caught exactly like a first add.

def _all_member_entities(project: dict) -> set:
    out = set()
    for rec in (project or {}).get("areas", {}).values():
        for e in _area_members(rec):
            out.add(e)
    return out


# ── NO-AUTO-ROOM QUARANTINE — RETIRED 4 Aug 2026 ─────────────────────────────
# quarantine_auto_rooms() cleared HA's auto-stamped DEVICE area off any AV
# media device ProOS hadn't placed, on a 45-second loop, forever.
#
# Dave: "we only had this because on onboarding devices would auto add and
# create rooms and felt this was not required, so you built a way it would
# unassign after being assigned — that sounds like it is going against HA,
# which is what's leading to some of these issues."
#
# He was right, and the diagnosis generalises: ProOS was deriving room
# membership from HA's areas, then fighting HA to keep that derivation
# clean. Every symptom followed — a room assigned in HA silently reverted
# (the janitor cleared it, with no incident and no journal); commissioning
# had to write an invisible entity-area "pin" to protect devices from our
# OWN janitor; and Pro ended up saying "in this room" and "Not in a room"
# about one device because two levels of HA's model now carried meaning.
#
# THE CORRECTION: the committed record is the only truth about what is in a
# room. HA is a device and protocol layer; its areas are an input to
# SUGGESTIONS and a courtesy for HA's own UI — never ProOS truth, and never
# something ProOS fights. With that, there is nothing to quarantine.
#
# A DELIBERATE placement still writes HA's area (POST /project/assign) —
# that is ProOS working WITH HA, not against it. What is gone is the
# automatic un-placement.
#
# Withdrawn ideas keep their pins (tenet 12): tests/quarantine_retired_bench.py
# asserts this function, its loop and its option stay gone.
def _area_members(rec: dict):
    seen, out = set(), []
    for e in ([rec.get("display")] + list(rec.get("sources") or [])
              + list(rec.get("audio") or [])):
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def label_plan(project: dict) -> dict:
    """Desired proos_* label set per entity, for COMMITTED areas only.

    Returns {entity_id: set(label_id)}. Uncommitted areas contribute nothing, so a
    suggestion never changes HA until an installer commits it.
    """
    plan = {}
    for area, rec in (project or {}).get("areas", {}).items():
        if not rec.get("committed"):
            continue
        for e in _area_members(rec):
            plan.setdefault(e, set()).add(LABEL_AV)
        # Backstop the display-role guard server-side too: a non-display integration
        # (Cast/Sonos/…) never gets the proos_display label even if a stale/hand-edited
        # record names it the display. display_ok defaults True for back-compat (older
        # records carry no meta), and is set False only when discovery is certain.
        disp = rec.get("display")
        if disp:
            dm = (rec.get("meta") or {}).get(disp) or {}
            if dm.get("display_ok", True):
                plan.setdefault(disp, set()).add(LABEL_DISPLAY)
        for e in (rec.get("sources") or []):
            plan.setdefault(e, set()).add(LABEL_SOURCE)
        for e in (rec.get("audio") or []):
            plan.setdefault(e, set()).add(LABEL_SPEAKER)
        if rec.get("tvaudio"):
            plan.setdefault(rec["tvaudio"], set()).add(LABEL_TVAUDIO)
        # Per-source input assignment -> proos_input_<slug> on the source entity.
        for src_e, inp in (rec.get("inputs") or {}).items():
            if src_e and inp:
                plan.setdefault(src_e, set()).add(_input_label(inp))
        # AV-switch plan -> labels for the dashboard's two-hop resolution:
        #   switch entity: proos_avswitch + proos_input_<output> (so the display
        #     sitting on the AVR's output HDMI maps to the switch, suppressing
        #     the Live TV card), and each switched source: proos_input_<its AVR
        #     input> (so the SWITCH's current source maps to the streamer).
        sw = rec.get("avswitch") or {}
        if sw.get("entity"):
            plan.setdefault(sw["entity"], set()).add(LABEL_AVSWITCH)
            out_inp = (sw.get("output") or "").strip()
            if out_inp:
                plan.setdefault(sw["entity"], set()).add(_input_label(out_inp))
            for src_e, inp in (sw.get("inputs") or {}).items():
                if src_e and inp:
                    plan.setdefault(src_e, set()).add(_input_label(inp))
    return plan


# ── HA LABEL MIRROR (committed areas only) ───────────────────────────────────

def _existing_label_ids(client) -> set:
    try:
        rows = ws_command(client.base_url, client._token,
                          "config/label_registry/list")
        return {r.get("label_id") for r in (rows or []) if r.get("label_id")}
    except Exception:
        return set()


def _ensure_labels(client, label_ids) -> None:
    have = _existing_label_ids(client)
    for lid in sorted(label_ids):
        if lid in have:
            continue
        try:
            # HA slugs the name into the label_id; our ids are already valid slugs.
            ws_command(client.base_url, client._token,
                       "config/label_registry/create", name=lid)
        except Exception:
            pass


def mirror(client, project: dict) -> dict:
    """Reconcile HA entity labels to match the committed project.

    For every entity that either SHOULD carry a proos_* membership/role label or
    currently DOES, set its label set to (its non-proos labels) + (the desired proos
    labels). This adds labels to freshly-committed members and removes them from
    de-committed ones, while never touching unrelated labels (proos_pause_*, dashboard_*).
    """
    plan = label_plan(project)
    _ensure_labels(client, _ROLE_LABELS | {l for s in plan.values() for l in s})
    try:
        reg = client.entity_registry()
    except Exception as e:
        return {"ok": False, "error": "entity_registry unavailable: %s" % e, "changed": []}

    changed = []
    for ent in reg or []:
        eid = ent.get("entity_id")
        if not eid:
            continue
        cur = set(ent.get("labels") or [])
        want = plan.get(eid, set())
        # Skip entities that neither want nor currently carry any managed proos_* label.
        if not want and not any(_is_managed(l) for l in cur):
            continue
        desired = {l for l in cur if not _is_managed(l)} | want
        if desired != cur:
            try:
                ws_command(client.base_url, client._token,
                           "config/entity_registry/update",
                           entity_id=eid, labels=sorted(desired))
                changed.append(eid)
            except Exception:
                pass
    committed = [a for a, r in (project or {}).get("areas", {}).items()
                 if r.get("committed")]
    out = {"ok": True, "changed": changed, "committed_areas": committed,
           "members": sorted(plan.keys())}
    # Voice follows the same truth: every mirror pass (commit, explicit
    # re-mirror, boot) also reconciles Assist exposure to committed membership.
    try:
        out["assist_exposure"] = expose_assist(client, project)
    except Exception as exc:  # noqa: BLE001 - exposure is additive, never fatal
        out["assist_exposure"] = {"ok": False, "error": str(exc)}
    return out


def expose_assist(client, project: dict) -> dict:
    """Reconcile ASSIST (voice) exposure to committed membership — one truth:
    what the dashboard shows is what voice can address.

    Scope is deliberately narrow: ONLY media_player entities on AV platforms
    are managed, because they are the noise source — streaming twins (the HEOS
    player of a committed Denon/Marantz), unclaimed zone entities, casting
    laptops/phones. HA's defaults expose ALL of them, so "turn off the TV"
    could land on gear no installer ever committed. After this pass:
      - a member of a COMMITTED room        -> exposed to Assist
      - every other AV-platform media_player -> hidden from Assist
    Lights, covers, climate and sensors keep HA's own defaults untouched —
    the stock per-area intents already handle them correctly, and managing
    them here would surprise installers who curate exposure by hand.
    Idempotent: only entities whose exposure actually differs are written."""
    try:
        entities = client.entity_registry() or []
    except Exception as e:
        return {"ok": False, "error": "entity_registry unavailable: %s" % e}
    av_platforms = set(discovery._KNOWN)
    members = set()
    for rec in (project or {}).get("areas", {}).values():
        if rec and rec.get("committed"):
            for e in _area_members(rec):
                members.add(e)
    expose, hide = [], []
    for ent in entities:
        eid = ent.get("entity_id") or ""
        if not eid.startswith("media_player."):
            continue
        if ent.get("platform") not in av_platforms:
            continue
        # Current exposure lives in the entity's options under each assistant
        # key; absent means "HA default" (exposed), so treat absent as exposed.
        opts = (ent.get("options") or {}).get("conversation") or {}
        cur = opts.get("should_expose", True)
        want = eid in members
        if want and not cur:
            expose.append(eid)
        elif not want and cur:
            hide.append(eid)
    out = {"ok": True, "exposed": expose, "hidden": hide}
    try:
        if expose:
            ws_command(client.base_url, client._token, "homeassistant/expose_entity",
                       assistants=["conversation"], entity_ids=expose, should_expose=True)
        if hide:
            ws_command(client.base_url, client._token, "homeassistant/expose_entity",
                       assistants=["conversation"], entity_ids=hide, should_expose=False)
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": str(exc), "exposed": expose, "hidden": hide}
    return out


# ── ACTIVITY GENERATION (committed rooms → HA scripts) ───────────────────────

def _friendly_names(client, entities) -> dict:
    if not entities:
        return {}
    tmpl = ("{{ {" + ", ".join(
        "%s: state_attr(%s,'friendly_name')" % (json.dumps(e), json.dumps(e))
        for e in entities) + "} | to_json }}")
    try:
        return json.loads(client.render_template(tmpl)) or {}
    except Exception:
        return {}


def _cluster_from_record(client, area, rec):
    """Build an AVCluster from a COMMITTED project record so the generator runs off the
    installer's declared roles/inputs, not a re-discovery. The display's own live-TV is
    left to the generator's built-in 'Watch TV' (it exposes a TV source), so the display
    is excluded from the external-source list to avoid a duplicate activity."""
    display_e = rec.get("display")
    src_list = rec.get("sources") or []
    src_es = [e for e in src_list if e != display_e]
    aud_es = list(rec.get("audio") or [])
    # The display is its OWN source only when the installer ticked "Also a source". That,
    # not merely having a 'TV' tuner in the source_list, is what earns the room a 'Watch TV'
    # activity -- a display-only TV must not generate one. Its committed input (e.g. 'TV')
    # drives that activity.
    display_is_source = bool(display_e and display_e in src_list)
    display_input = (rec.get("inputs") or {}).get(display_e) if display_is_source else None
    names = _friendly_names(client, ([display_e] if display_e else []) + src_es + aud_es)
    meta = rec.get("meta") or {}
    def dev(e):
        m = meta.get(e) or {}
        return discovery.Device(e, names.get(e) or e, m.get("integration", "unknown"),
                                None, m.get("tier", "compatible"))
    # Immutable area_id for generation (stored on the record; resolved from the area for
    # legacy records that predate this field). The name is display only.
    aid = (rec.get("area_id") or "").strip()
    if not aid:
        try:
            aid = (client.render_template("{{ area_id(%s) or '' }}" % json.dumps(area, ensure_ascii=False)) or "").strip()
        except Exception:
            aid = ""
    return discovery.AVCluster(
        area=(rec.get("name") or area),
        area_id=aid,
        display=dev(display_e) if display_e else None,
        sources=[dev(e) for e in src_es],
        audio=[dev(e) for e in aud_es],
        display_is_source=display_is_source,
        display_input=display_input,
    )


def generate_committed(client, project, overwrite=False) -> dict:
    """For every COMMITTED room with a display, generate its one-touch activities from the
    declared record. The per-source input map feeds the generator's deterministic display
    routing (routes[source]={'input': 'HDMI 2'}). create-if-absent + hash edit-protection
    are the generator's own — an installer-edited activity is never clobbered."""
    from . import generator
    out = []
    for area, rec in (project or {}).get("areas", {}).items():
        if not rec.get("committed") or not rec.get("display"):
            continue
        try:
            cluster = _cluster_from_record(client, area, rec)
            res = generator.generate(client, cluster, _commissioning_from_record(rec), overwrite=overwrite)
            out.append({"area": area, "created": res.get("created", []),
                        "kept": res.get("kept", []), "refreshed": res.get("refreshed", []),
                        "object_ids": res.get("object_ids", [])})
        except Exception as e:
            out.append({"area": area, "error": str(e)})
    return {"rooms": out}


def _routes_for(rec):
    return {e: {"input": inp} for e, inp in (rec.get("inputs") or {}).items()
            if e != rec.get("display") and inp}


def _commissioning_from_record(rec):
    """The COMPLETE generator input for a committed room -- routes AND off_state (plus any
    future per-room config). EVERY generation entry point (commit, the Activities list,
    Reset-to-generated, boot) must build from THIS one function so a script is byte-for-byte
    identical no matter which path produced it. Passing routes-only from some paths was the
    bug: the Activities-list build (used for edit-detection) and Reset-to-generated both
    dropped off_state, so they rebuilt tv_off as full power-off -- flipping Art Mode back
    off, and falsely flagging the (art) live script as 'edited' so a later commit refused to
    refresh it. Single source of truth = no drift."""
    comm = {"routes": _routes_for(rec)}
    if rec.get("off_state"):
        comm["off_state"] = rec["off_state"]
    # Explicit AV-switch plan (any-brand AVR / matrix doing the room's video AND
    # audio switching). rec['avswitch'] = {entity, inputs:{source: avr_input},
    # output: <display input the AVR feeds>, broadcast: <avr input for TV
    # audio>} — every value picked by the installer in Pro from the device's
    # OWN source_list / the display's own input list. Nothing is name-guessed.
    #   audio  -> the generator's existing explicit-override path: power the
    #             switch on and select the committed input per activity
    #             (broadcast input for Watch TV), power off in TV Off.
    #   routes -> every switched source's DISPLAY input becomes the constant
    #             AVR-output HDMI: video goes source -> AVR -> display, so the
    #             display is always told to look at the AVR's output while the
    #             AVR selects what plays. The TV-as-its-own-source input and
    #             non-switched sources keep their own routes untouched.
    sw = rec.get("avswitch") or {}
    if sw.get("entity"):
        comm["audio"] = {"mode": "avr", "entity": sw["entity"],
                         "inputs": dict(sw.get("inputs") or {}),
                         "broadcast": (sw.get("broadcast") or "").strip() or None,
                         "power": True}
        out_inp = (sw.get("output") or "").strip()
        if out_inp:
            for e in (sw.get("inputs") or {}):
                if e and e != rec.get("display"):
                    comm["routes"][e] = {"input": out_inp}
            # ...and so does the SWITCH ITSELF when it is also committed as a
            # source (its own tuner / HEOS / streaming). It is not plugged into
            # itself, so it never appeared in sw['inputs'] and silently got no
            # display hop at all: 'Watch <AVR>' powered the room on and left
            # the screen on whatever input it happened to be showing (matrix
            # #7, "one-touch lands on a black input" -- measured live on the
            # Living Room, 31 Jul 2026).
            #
            # Same rule, one more node: anything downstream of the switch looks
            # at the switch's committed output. The switch simply has no input
            # to select for itself, which the generator already handles because
            # it only emits an AVR-input step for sources in sw['inputs'].
            #
            # setdefault, not assignment: an explicitly committed per-source
            # route for the switch is the installer's word and always wins.
            sw_ent = sw.get("entity")
            if sw_ent and sw_ent != rec.get("display"):
                comm["routes"].setdefault(sw_ent, {"input": out_inp})
    return comm


def _resolve_rec(project, key):
    """Find a room record by area_id OR name. The project is keyed by the immutable area_id,
    but callers (and older stored data) may present either — so match on the key, then on any
    record whose area_id or name equals it. Keeps reads working across the migration."""
    areas = (project or {}).get("areas", {})
    if key in areas:
        return areas[key]
    for r in areas.values():
        if r and (r.get("area_id") == key or r.get("name") == key):
            return r
    return None


def record_from_labels(entities, area_id, area_name, source_lists=None):
    """Rebuild a committed record from the HA LABELS ProOS wrote at commit.

    THE LABELS ARE THE COMMISSION (4 Aug 2026). mirror() writes the
    installer's decisions onto the entities — proos_display, proos_source,
    proos_speaker, proos_tvaudio, proos_input_<slug> — so when the record
    is lost or unreadable the facts are still on the box and the room can
    heal itself instead of demanding a manual recommit.

    `entities` is the entity-registry rows FOR THIS AREA (caller resolves
    membership). `source_lists` maps the display entity to its live
    source_list, used to recover an input's exact spelling: the label is a
    slug ("proos_input_hdmi_2") and `select_source` needs the display's own
    wording ("HDMI 2"). No source_list -> the slug is de-slugified as a
    best effort and the input is still recorded, never invented.

    Pure. Returns a record dict, or None when the labels describe no
    display (nothing to build). Benched: record_rebuild_bench.
    """
    display, sources, speakers, tvaudio = None, [], [], None
    inputs, input_slugs = {}, {}
    for e in (entities or []):
        eid = (e or {}).get("entity_id")
        labels = set((e or {}).get("labels") or [])
        if not eid or LABEL_AV not in labels:
            continue
        if LABEL_DISPLAY in labels and not display:
            display = eid
        if LABEL_SOURCE in labels and eid not in sources:
            sources.append(eid)
        if LABEL_SPEAKER in labels and eid not in speakers:
            speakers.append(eid)
        if LABEL_TVAUDIO in labels and not tvaudio:
            tvaudio = eid
        for lb in labels:
            if lb.startswith("proos_input_"):
                input_slugs[eid] = lb[len("proos_input_"):]
    if not display:
        return None
    # recover each input's exact spelling from the display's own source_list
    _srcs = ((source_lists or {}).get(display)) or []
    _by_slug = {_input_label(s): s for s in _srcs if s}
    for eid, slug in input_slugs.items():
        inputs[eid] = (_by_slug.get(_INPUT_PREFIX + slug)
                       or slug.replace("_", " ").upper())
    rec = {
        "area_id": area_id, "name": area_name, "committed": True,
        "display": display, "displays": [display],
        "sources": sources, "audio": list(speakers), "speakers": speakers,
        "tvaudio": tvaudio, "inputs": inputs,
        "kind": "tv", "healed": True,
    }
    return rec


def is_committed(project, key) -> bool:
    """Has this room been committed? Accepts an area_id OR a name — the
    project is keyed by area_id, but controllers and the add-on config
    speak names. Never raises."""
    try:
        rec = _resolve_rec(project, key)
        return bool(rec and rec.get("committed"))
    except Exception:                                        # noqa: BLE001
        return False


def committed_record(project, key):
    """THE RECORD IS THE TRUTH (4 Aug 2026). The room's committed record if
    it is committed AND buildable (has a display), else None.

    This exists because the controller looked the project up with a raw
    `areas.get(name)` while the project is keyed by area_id — so it found
    nothing for every room, always, and every committed room silently ran
    on DISCOVERY instead of its record. One resolver, used everywhere, so
    that class of drift cannot come back. Benched: committed_record_bench.
    """
    try:
        rec = _resolve_rec(project, key)
        if rec and rec.get("committed") and rec.get("display"):
            return rec
    except Exception:                                        # noqa: BLE001
        pass
    return None


def activities_status(client, project, area) -> dict:
    """List a room's activities EXACTLY as stored on the box -- read straight from the HA
    scripts (script.proos_<area>_*), never a live re-generation. Every device that asks Core
    gets the SAME answer: the stored alias, the stored kind, and an EDITED flag from the
    script's own stamped proos_hash vs its current content. No dependency on live device
    state or on any one client's cache, so two devices (or two moments) can't disagree."""
    from . import generator
    rec = _resolve_rec(project, area)
    if not rec or not rec.get("committed") or not rec.get("display"):
        return {"activities": []}
    try:
        cluster = _cluster_from_record(client, area, rec)
        area_slug = cluster.area_id or generator._slug(cluster.area)
    except Exception:
        area_slug = generator._slug(area)
    prefix = "script.%s_%s_" % (generator.PROOS_PREFIX, area_slug)
    # Enumerate the room's stored ProOS scripts on the box (authoritative, device-neutral).
    tmpl = ("{% set ns = namespace(x=[]) %}"
            "{% for s in states.script %}"
            "{% if s.entity_id.startswith(" + json.dumps(prefix) + ") %}"
            "{% set ns.x = ns.x + [s.entity_id] %}{% endif %}"
            "{% endfor %}{{ ns.x | to_json }}")
    try:
        eids = json.loads(client.render_template(tmpl) or "[]")
    except Exception as e:
        return {"activities": [], "error": str(e)}
    out = []
    for eid in sorted(eids):
        oid = eid.split(".", 1)[1]
        try:
            ex = client.get_script(oid)
        except Exception:
            ex = None
        ecfg = generator._as_cfg(ex) if ex is not None else None
        if not isinstance(ecfg, dict):
            continue
        v = ecfg.get("variables") or {}
        stored = v.get("proos_hash")
        cur = generator._content_hash(ecfg)
        key = oid[len(area_slug) + len(generator.PROOS_PREFIX) + 2:] if oid.startswith("%s_%s_" % (generator.PROOS_PREFIX, area_slug)) else oid
        label = (ecfg.get("alias") or oid).rsplit("\u00b7", 1)[-1].strip()
        out.append({"object_id": oid, "entity_id": eid, "script": eid,
                    "key": key, "label": label,
                    "alias": ecfg.get("alias") or oid,
                    "kind": v.get("proos_kind"),
                    "source_eid": v.get("proos_source"),
                    "edited": bool(stored) and stored != cur,
                    "exists": True})
    return {"activities": out}


def room_fire_plan(client, project, area, target):
    """The canonical fire sequence built from the room's STORED activity scripts -- the SAME
    list Pro and the dashboard show. Stop every other real activity (never the Display On
    helper), then turn on the target. Returns None if the room has no stored activities yet
    (caller falls back to the controller's provisional plan)."""
    acts = activities_status(client, project, area).get("activities") or []
    if not acts:
        return None
    steps, seen = [], set()
    for a in acts:
        eid = a.get("entity_id")
        if not eid or eid == target or eid in seen:
            continue
        if a.get("kind") == "display_on":   # a building block Watch runs -- never cancel it
            continue
        seen.add(eid)
        steps.append({"domain": "script", "service": "turn_off", "entity_id": eid})
    steps.append({"domain": "script", "service": "turn_on", "entity_id": target})
    return steps


def regenerate_activity(client, project, area, object_id) -> dict:
    """Force-regenerate ONE activity from the room's declared config, overwriting any
    installer edits (the popup's 'Reset to generated', guarded by a warning in the UI)."""
    from . import generator
    rec = _resolve_rec(project, area)
    if not rec:
        return {"ok": False, "error": "unknown area"}
    try:
        cluster = _cluster_from_record(client, area, rec)
        fresh = generator.build_room_scripts(client, cluster, _commissioning_from_record(rec))
        cfg = fresh.get(object_id)
        if not cfg:
            return {"ok": False, "error": "unknown activity"}
        client.upsert_script(object_id, cfg)
        return {"ok": True, "object_id": object_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def duplicate_members(proj: dict):
    """A12 (3 Aug): one entity committed to TWO rooms poisons attribution —
    devices{}, verdicts, watchers and Assist all assume membership is
    exclusive. Returns [{"entity", "rooms"}] for every entity committed in
    more than one room (duplicates WITHIN a room are fine — first role
    wins, A1). Pure; benched by tests/dup_commit_bench.py."""
    seen = {}
    for key, rec in (proj or {}).get("areas", {}).items():
        if not (isinstance(rec, dict) and rec.get("committed")):
            continue
        name = rec.get("name") or key
        ents = set()
        for e in ([rec.get("display"), rec.get("tvaudio")]
                  + list(rec.get("displays") or [])
                  + list(rec.get("sources") or [])
                  + list(rec.get("audio") or [])
                  + list(rec.get("speakers") or [])):
            if isinstance(e, dict):
                e = e.get("entity")
            if isinstance(e, str) and e:
                ents.add(e)
        for e in ents:
            seen.setdefault(e, []).append(name)
    return [{"entity": e, "rooms": rs}
            for e, rs in sorted(seen.items()) if len(rs) > 1]
