"""
ProOS Core -- control-system bridge publisher.

Publishes ProOS's room-activity VERDICTS as plain HA sensor entities, one per
committed room:

    sensor.proos_activity_<area>   state: watch_apple_tv | watch_shield |
                                          watch_tv | ... | off

Why: an overlaid control system (Savant today, Control4's full build next)
reacts to ProOS through the state bridge -- and without this, its programmer
must re-assemble "which activity is this room in" from raw device variables
(TV power + input + source state), duplicating in Blueprint the evidence-
weighing ProOS already does. Publishing the CONCLUSION means the overlay wires
ONE trigger per room, and every consumer -- Savant, Control4, an HA automation,
a wall keypad -- reads the same verdict the dashboards show. Truth is computed
once, in ProOS, from actual device state; everything else mirrors it.

The sensors ride HA's normal state stream, so the Savant AV bridge profile
(proos_savant_av.xml) forwards them automatically as
CurrentState_sensor.proos_activity_<area> -- no XML change, no extra wiring.

Also (optional): a liveness sensor for the overlay controller host itself --
binary_sensor.proos_savant_host -- from a direct TCP probe of the host's IP.
Savant is a closed box; whether its HOST is alive is the one signal it can't
refuse to give us.

State is published via POST /api/states (a bare state entity, not a registry
entity) -- correct for a mirror: it carries no controls, costs nothing when
unused, and vanishes if Core stops publishing.
"""
from __future__ import annotations

import re
import socket
import threading
import time

try:                                    # additive: event journal (write-only,
    from . import journal as _jrnl     # never read back into behavior)
except Exception:                       # noqa: BLE001
    _jrnl = None

try:                                    # additive: music-room status producer.
    from . import musicstat as _music   # fills the SAME sensor for kind='music'
except Exception:                       # noqa: BLE001
    _music = None

_INTERVAL = 2          # seconds between sweeps; publishes only on change
_PROBE_TIMEOUT = 3
_PROBE_PORTS = (22, 80, 443)     # Savant hosts answer SSH; 80/443 as fallbacks


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "x"



def _norm_txt(x) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(x or "").lower())


def route_matches(a, disp_src) -> bool:
    """Does the display's reported input correspond to this activity's
    commissioned route? Normalised containment either way."""
    if not disp_src:
        return False
    r = getattr(a, "route", None) or {}
    cand = (r.get("display_input") or r.get("hdmi_code")
            or r.get("c4_source") or r.get("select_source"))
    if not cand:
        return False
    n1, n2 = _norm_txt(cand).replace("key", ""), _norm_txt(disp_src)
    return bool(n1 and n2 and (n1 in n2 or n2 in n1))


def decide(area_slug, snap, src_acts, bcast, witmap, mem, art_check=None):
    """The verdict ladder as a PURE function -- everything it knows arrives in
    its arguments, everything it concludes leaves in its return value, and the
    only state it touches is the mem dict handed to it. This is the bench
    surface: every scenario the house has ever produced replays here offline.

    mem keys (mutated): held, darkn, offpend, last
    returns dict: state, active, active_key, verified, held_now, evidence,
                  wrate, disp_eid, disp, disp_src, on_tuner, confirmed_dark,
                  defer (True = publish nothing this sweep; off pending)
    """
    from .activities import _st as _ast
    from .activities import _art_on as _default_art
    art_check = art_check or _default_art
    _SRC_ALIVE = ("on", "playing", "paused", "idle")

    disp_eid = None
    for _a in (src_acts + ([bcast] if bcast else [])):
        if getattr(_a, "targets", None):
            disp_eid = _a.targets[0].entity_id
            break
    _drec = snap.get(disp_eid) or {}
    disp = _drec.get("state", "unavailable")
    disp_src = (_drec.get("attributes") or {}).get("source")
    tuner = ((getattr(bcast, "route", None) or {}).get("select_source")
             if bcast else None)
    on_tuner = bool(disp_src and tuner and
                    str(disp_src).strip().lower() == str(tuner).strip().lower())

    alive = [a for a in src_acts if _ast(snap, a.source_eid) in _SRC_ALIVE]
    # Dark = confirmed nobody-is-watching. off/standby and Art Mode count;
    # so does a display sitting 'unavailable' with NO source alive (room
    # powered off at the wall must not hold a verdict forever).
    _dark_now = (disp in ("off", "standby") or art_check(snap, disp_eid)
                 or (disp in ("unavailable", "unknown") and not alive))
    mem["darkn"] = mem.get("darkn", 0) + 1 if _dark_now else 0
    confirmed_dark = mem["darkn"] >= 2

    active_key, active, verified = None, None, True
    evidence, wrate = None, None
    for a in src_acts:                                   # rung 1: verified
        try:
            if getattr(a.summary(snap), "ok", False):
                active_key, active = a.key, a
                break
        except Exception:
            continue
    if active_key is None and not confirmed_dark:        # rung 1.5: traffic
        for a in src_acts:
            w = witmap.get(a.source_eid)
            if not w:
                continue
            rate = 0.0
            for s_ in w.get("sensors", []):
                try:
                    rate += float(_ast(snap, s_))
                except (TypeError, ValueError):
                    pass
            if rate >= w.get("min", 0.25):
                active_key, active = a.key, a
                evidence, wrate = "traffic", round(rate, 3)
                break
    if active_key is None and not on_tuner and not confirmed_dark:  # rung 2
        def _awm(a):
            aw = getattr(a, "audio_witness", None) or {}
            ent, want = aw.get("entity"), aw.get("source")
            if not (ent and want):
                return False
            cur = (snap.get(ent) or {}).get("attributes", {}).get("source")
            return bool(cur) and str(cur).strip().lower() == str(want).strip().lower()
        # Display testimony OUTRANKS source liveliness: a panel actively on a
        # source's COMMITTED input names that source even when its integration
        # session plays dead (Companion/Tizen drop) -- the room is on that
        # input, full stop. Unique match required; published unverified with
        # evidence=display. AVR rooms (no per-source display_input) fall to
        # the receiver-input witness, then to sole-alive -- and a sole alive
        # source whose committed route CONTRADICTS the panel may not claim.
        route_all = [a for a in src_acts if route_matches(a, disp_src)]
        awm_alive = [a for a in alive if _awm(a)]
        pick, ev = None, None
        if len(route_all) == 1:
            pick = route_all[0]
            if pick not in alive:
                ev = "display"
        elif len(awm_alive) == 1:
            pick = awm_alive[0]
        elif len(alive) == 1:
            a0 = alive[0]
            r0 = getattr(a0, "route", None) or {}
            contradicted = bool(disp_src and (r0.get("display_input")
                                or r0.get("hdmi_code") or r0.get("c4_source"))
                                and not route_matches(a0, disp_src))
            if not contradicted:
                pick = a0
        if pick is not None:
            active_key, active, verified = pick.key, pick, False
            if ev:
                evidence = ev
    if active_key is None and bcast is not None:         # rung 3: broadcast
        _off_tuner = bool(disp_src and tuner and not on_tuner)
        if not _off_tuner and not confirmed_dark:
            try:
                if getattr(bcast.summary(snap), "ok", False):
                    active_key, active = "watch_tv", bcast
            except Exception:
                pass

    # vocabulary: strip area prefix from source keys; collisions keep raw
    def _clean(k):
        if not k or not k.startswith("watch_") or k == "watch_tv":
            return k
        toks = [t for t in k[6:].split("_") if t]
        at = [t for t in str(area_slug).split("_") if t]
        while at and len(toks) > len(at) and toks[:len(at)] == at:
            toks = toks[len(at):]
        return "watch_" + "_".join(toks) if toks else k
    cleaned = {a.key: _clean(a.key) for a in src_acts}
    vals = list(cleaned.values())
    for k, v in list(cleaned.items()):
        if vals.count(v) > 1:
            cleaned[k] = k
    state = cleaned.get(active_key, active_key) or "off"

    # verdict memory
    held = mem.get("held")
    held_now = False
    if active is not None and getattr(active, "source_eid", None):
        mem["held"] = (state, active_key)
    elif active is not None and on_tuner:
        mem["held"] = (state, active_key)
    elif held and not confirmed_dark and disp not in ("off", "standby"):
        state, active_key, held_now = held[0], held[1], True
    if confirmed_dark:
        mem.pop("held", None)
        state, active_key, active, held_now = "off", None, None, False

    # off must confirm across two sweeps unless darkness already did
    defer = False
    if state == "off" and not confirmed_dark and mem.get("last") not in (None, "off"):
        n = mem.get("offpend", 0) + 1
        mem["offpend"] = n
        if n < 2:
            defer = True
    if not defer:
        mem.pop("offpend", None)

    return {"state": state, "active": active, "active_key": active_key,
            "verified": verified, "held_now": held_now, "evidence": evidence,
            "wrate": wrate, "disp_eid": disp_eid, "disp": disp,
            "disp_src": disp_src, "on_tuner": on_tuner,
            "confirmed_dark": confirmed_dark, "defer": defer}


class ActivityPublisher:
    @staticmethod
    def parse_witnesses(raw: str) -> dict:
        """Installer-committed traffic witnesses:
        'source_eid|sensor1,sensor2|min;source_eid|...'  ->
        {source_eid: {"sensors": [...], "min": float}}"""
        out = {}
        for entry in (raw or "").split(";"):
            parts = [p.strip() for p in entry.split("|")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                try:
                    mn = float(parts[2]) if len(parts) > 2 and parts[2] else 1.0
                except ValueError:
                    mn = 1.0
                out[parts[0]] = {"sensors": [s for s in parts[1].split(",") if s],
                                 "min": mn}
        return out

    def __init__(self, client, project_mod, get_controller,
                 enabled=lambda: True, savant_host: str = "",
                 witnesses: dict | None = None):
        self.client = client
        self.project = project_mod
        self.get_controller = get_controller
        self.enabled = enabled
        self.savant_host = (savant_host or "").strip()
        self._witnesses = witnesses or {}
        self._last: dict = {}          # entity_id -> last published state
        self._lastattrs: dict = {}     # entity_id -> last MEANINGFUL attrs
        self._conv: dict = {}          # area_slug -> last convergence ts
        self._roommem: dict = {}       # area_slug -> decide() memory
        self.converge = True           # intent convergence (option-gated in server)
        self.healthcheck = None        # optional: healthmon hook(snapall), set by server
        self._healthn = 0              # sweep counter for throttled health scans

    @property
    def witnesses(self) -> dict:
        w = self._witnesses
        try:
            return w() if callable(w) else w
        except Exception:
            return {}

    # -- publishing ----------------------------------------------------------
    def _reconcile_published(self, snapall: dict) -> None:
        """Drop cache entries HA disagrees with, so they republish.

        Matrix #19: `_last` is in-memory and the verdict sensors are bare
        POSTed states — HA does not restore them across a restart — so after
        every HA restart the sensors were simply gone while this cache still
        said "off". Nothing republished until the room's activity actually
        changed, and the documented workaround was "restart ProOS Core after
        any HA restart". Every home, every restart, silently.

        The sweep already fetches EVERY state in one call, including these
        sensors, so the snapshot IS the reconciliation source: an entry HA is
        missing (restart) or holds differently ('unknown', manual edit) is
        dropped here, and _publish's ordinary publish-on-change re-POSTs it
        this same sweep. Zero extra HA traffic, no restart detection.
        An agreeing entry is untouched, so steady state publishes nothing —
        exactly as before.
        """
        for eid in list(self._last):
            ha = (snapall.get(eid) or {}).get("state")
            if ha != self._last[eid]:
                del self._last[eid]
                self._lastattrs.pop(eid, None)

    # Attributes that can move every 2-second sweep (witness evidence and its
    # rate). They ride along on every publish but must never CAUSE one: a
    # re-POST per room per sweep is a recorder row per room per sweep, forever.
    # Publish-on-change exists precisely to prevent that.
    _VOLATILE_ATTRS = frozenset({"evidence", "witness_rate"})

    def _publish(self, eid: str, state: str, attrs: dict) -> None:
        # Meaning lives in the state AND the meaningful attributes. A music
        # room playing track after track is 'playing' -> 'playing' forever;
        # skipping on state alone left the first song on the dashboard all
        # evening (found 1 Aug 2026 writing the #19 bench). So: publish when
        # the state OR the non-volatile attributes change.
        sig = {k: v for k, v in (attrs or {}).items()
               if k not in self._VOLATILE_ATTRS}
        if self._last.get(eid) == state and self._lastattrs.get(eid) == sig:
            return
        try:
            self.client._req("POST", "/api/states/%s" % eid,
                             {"state": state, "attributes": attrs})
            self._last[eid] = state
            self._lastattrs[eid] = sig
            print("  [ctlbridge] %s -> %s" % (eid, state), flush=True)
        except Exception as e:                                   # noqa: BLE001
            print("  [ctlbridge] publish %s failed: %s" % (eid, e), flush=True)

    # -- room activity verdicts ---------------------------------------------
    def _mem(self, area_slug: str) -> dict:
        m = self._roommem.setdefault(area_slug, {})
        m["last"] = self._last.get("sensor.proos_activity_%s" % area_slug)
        return m

    @staticmethod
    def _area_index(proj: dict) -> dict:
        """entity_id -> room NAME, across every committed room.

        Only needed to answer one question: when a speaker is joined to a group,
        which room is the coordinator in. Built once per sweep rather than per
        room so a grouped house doesn't re-walk the project for every zone.
        """
        idx: dict = {}
        for key, r in (proj.get("areas") or {}).items():
            if not r:
                continue
            nm = r.get("name") or key
            members = list(r.get("speakers") or []) + list(r.get("audio") or []) \
                + list(r.get("sources") or []) + [r.get("display")]
            for item in members:
                eid = item.get("entity") if isinstance(item, dict) else item
                if isinstance(eid, str) and eid:
                    idx.setdefault(eid, nm)
        return idx

    def _publish_music(self, area_name: str, area_slug: str,
                       rec: dict, snapall: dict, area_idx: dict) -> None:
        """Status for an audio-only room. Additive: never touches the ladder.

        Committed means committed -- a music zone that can be committed must be
        able to report. Same sensor, same attribute shape as an AV room, so Pro,
        the dashboard and Assist read one thing and never branch on room type.
        Fails closed: any error here leaves the room silent exactly as it is
        today, and can never disturb the AV rooms in the same sweep.
        """
        if _music is None:
            return
        try:
            d = _music.decide_music(rec, snapall, area_idx.get)
        except Exception as e:                                   # noqa: BLE001
            print("  [ctlbridge] music status %s failed: %s" % (area_slug, e),
                  flush=True)
            return
        if not d:
            return
        attrs = {"friendly_name": "ProOS Activity — %s" % area_name,
                 "activity_key": d["activity_key"],
                 "provisional": False,
                 "area": d["area"] or area_name,
                 "label": d["label"],
                 "verified": d["verified"],
                 "held": d["held"],
                 "icon": d["icon"],
                 "source": d["source"],
                 "audio_entity": d["audio_entity"],
                 "kind": "music"}
        for k in ("grouped_to", "coordinator", "media_title", "media_artist",
                  "media_album_name", "media_source", "members"):
            if d.get(k):
                attrs[k] = d[k]
        _prev = self._last.get("sensor.proos_activity_%s" % area_slug)
        self._publish("sensor.proos_activity_%s" % area_slug, d["state"], attrs)
        try:
            if _jrnl is not None and _prev != d["state"]:
                _jrnl.emit(area_slug, "verdict", {
                    "from": _prev, "to": d["state"], "verified": True,
                    "held": False, "kind": "music",
                    "grouped_to": d.get("grouped_to"),
                    "source": d.get("source")})
        except Exception:                                        # noqa: BLE001
            pass

    def _sweep_rooms(self, snapall: dict) -> None:
        proj = self.project.load() or {}
        area_idx = self._area_index(proj)
        for key, rec in (proj.get("areas") or {}).items():
            if not (rec and rec.get("committed")):
                continue
            area_name = rec.get("name") or key
            area_slug = rec.get("area_id") or _slug(key)
            # A committed MUSIC room is answered from its record, before the AV
            # path is consulted at all.
            #
            # Why the record and not "does it have activities": controller
            # _committed_cluster() only builds from the record when the record
            # has a DISPLAY, so every display-less room falls back to live
            # discover_av(). Discovery roles purely by integration and ignores
            # committed roles -- so a music zone containing an apple_tv-platform
            # speaker (a HomePod) has activities INVENTED for it, takes the AV
            # path, and a deferred verdict then exits without publishing. The
            # room's status freezes at whatever it last said. Measured on the
            # live Office, 31 Jul 2026: two Sonos/HomePod speakers both paused,
            # sensor stuck reporting 'playing', no publish line in the log,
            # while single-speaker Study and Ryan's Room updated correctly.
            #
            # The committed record is the source of truth; discovery is the
            # un-commissioned fallback. A committed room must never be driven
            # by discovery's guess about what it is.
            if (rec.get("kind") or "").lower() == "music":
                self._publish_music(area_name, area_slug, rec, snapall,
                                    area_idx)
                continue
            try:
                ctrl = self.get_controller(area_name)
                acts = ctrl.activities
                if not acts:
                    # No AV activities and not a music room: a TV room with
                    # nothing added yet. decide_music() refuses anything that
                    # isn't kind='music', so this stays silent exactly as it
                    # did before.
                    self._publish_music(area_name, area_slug, rec, snapall,
                                        area_idx)
                    continue
                src_acts = [a for a in acts.values()
                            if a.key not in ("display_on", "tv_off", "watch_tv")
                            and getattr(a, "source_eid", None)]
                bcast = acts.get("watch_tv")
                d = decide(area_slug, snapall, src_acts, bcast,
                           self.witnesses, self._mem(area_slug))
                if d["defer"]:
                    continue
                active = d["active"]
                _prov = any(getattr(a, "provisional", False)
                            for a in src_acts) or bool(
                            getattr(bcast, "provisional", False))
                _label = (active.label if active
                          else (d["state"].replace("watch_", "Watch ").replace("_", " ").title()
                                if d["state"] != "off" else "Off"))
                attrs = {"friendly_name": "ProOS Activity — %s" % area_name,
                         "activity_key": d["active_key"] or "off",
                         "provisional": _prov,
                         "area": area_name,
                         "label": _label,
                         "verified": (d["verified"] if active else True) and not d["held_now"],
                         "held": d["held_now"],
                         "icon": ("mdi:television-play" if d["state"] != "off"
                                  else "mdi:television-off")}
                if active is not None and getattr(active, "source_eid", None):
                    attrs["source"] = active.source_eid
                if active is not None:
                    aw = getattr(active, "audio_witness", None) or {}
                    attrs["audio_entity"] = aw.get("entity") or d["disp_eid"]
                attrs["prov_detail"] = {a.key: bool(getattr(a, "provisional", False))
                                        for a in src_acts}
                if d["evidence"]:
                    attrs["evidence"] = d["evidence"]
                    attrs["witness_rate"] = d["wrate"]
                _prev = self._last.get("sensor.proos_activity_%s" % area_slug)
                self._publish("sensor.proos_activity_%s" % area_slug,
                              d["state"], attrs)
                # additive: journal the verdict CHANGE with its evidence —
                # write-only; a journal failure can never touch the sweep
                try:
                    if _jrnl is not None and _prev != d["state"]:
                        _jrnl.emit(area_slug, "verdict", {
                            "from": _prev, "to": d["state"],
                            "verified": attrs.get("verified"),
                            "held": d["held_now"],
                            "evidence": d.get("evidence"),
                            "display": d.get("disp"),
                            "display_input": d.get("disp_src"),
                            "source": attrs.get("source"),
                            "provisional": _prov, "label": _label})
                except Exception:                            # noqa: BLE001
                    pass
                try:
                    if self.converge and d["disp"] not in ("off", "standby",
                                                           "unavailable", ""):
                        self._maybe_converge(area_slug, ctrl, src_acts, bcast,
                                             snapall, active, d["verified"],
                                             d["disp_src"], d["on_tuner"])
                except Exception as e:                       # noqa: BLE001
                    print("  [ctlbridge] converge check failed: %s" % e, flush=True)
            except Exception as e:                               # noqa: BLE001
                print("  [ctlbridge] %s sweep failed: %s" % (area_name, e), flush=True)

    @staticmethod
    def _norm(x) -> str:
        import re as _re
        return _re.sub(r"[^a-z0-9]+", "", str(x or "").lower())

    def _route_matches(self, a, disp_src) -> bool:
        return route_matches(a, disp_src)

    # The intent window: for this long after a ProOS activity script fires,
    # that script IS the room's intent, and the converger pulls the room back
    # to it — CEC coexistence (Dave's standing rule, 1 Aug 2026): CEC ships on
    # and every reset turns it back on, so ProOS doesn't fight it — it is
    # state-based, sees the yank within a sweep, and re-asserts the committed
    # state. Beyond the window, reality wins: a human's later hands-on choice
    # is respected. ProOS converges to its OWN commands, never against people.
    _INTENT_WINDOW_S = 180

    def _intent_script(self, ctrl, src_acts, bcast, snap):
        """(activity, script_eid, seconds_ago) of the most recently fired
        ProOS activity for this room within the intent window, tv_off
        included; (None, None, None) when no recent intent exists."""
        from datetime import datetime, timezone
        cands = list(src_acts or [])
        if bcast is not None:
            cands.append(bcast)
        off = (getattr(ctrl, "activities", None) or {}).get("tv_off")
        if off is not None:
            cands.append(off)
        best = (None, None, None)
        for a in cands:
            try:
                eid = ctrl._script_entity_for(a)
            except Exception:
                eid = None
            if not eid:
                continue
            lt = ((snap.get(eid) or {}).get("attributes") or {}) \
                .get("last_triggered")
            if not lt:
                continue
            try:
                dt = datetime.fromisoformat(str(lt).replace("Z", "+00:00"))
                ago = (datetime.now(timezone.utc) - dt).total_seconds()
            except Exception:
                continue
            if 0 <= ago <= self._INTENT_WINDOW_S and \
                    (best[2] is None or ago < best[2]):
                best = (a, eid, ago)
        return best

    def _maybe_converge(self, area_slug, ctrl, src_acts, bcast, snap,
                        active, verified, disp_src, on_tuner) -> None:
        import time as _t
        now = _t.time()
        if now - self._conv.get(area_slug, 0) < 120:
            return                      # one attempt per episode

        # ── Intent window first: the fired script beats every other signal,
        # including a VERIFIED verdict for a different source — verified means
        # the evidence is real (the AVR truly sat on Blu-ray when CEC yanked
        # it, recorder 1 Aug 09:01:57), not that it is what the user asked
        # for. tv_off is an intent too: a room popping back on inside the
        # window is pulled back off (the Bedroom case).
        intent, intent_eid, _ago = self._intent_script(ctrl, src_acts, bcast,
                                                       snap)
        if intent is not None:
            wanted = getattr(intent, "key", None)
            got = getattr(active, "key", None) if active is not None else \
                ("tv_off" if wanted == "tv_off" else None)
            if wanted == "tv_off":
                satisfied = active is None        # room reads off
            else:
                satisfied = (got == wanted and verified)
            if satisfied:
                return
            if ((snap.get(intent_eid) or {}).get("state") or "") == "on":
                return                  # still running: hands off
            self._conv[area_slug] = now
            print("  [ctlbridge] intent-converge %s -> %s (room drifted from "
                  "fired intent inside %ds window, re-running %s)"
                  % (area_slug, wanted, self._INTENT_WINDOW_S, intent_eid),
                  flush=True)
            try:
                if _jrnl is not None:
                    _jrnl.emit(area_slug, "intent_converge",
                               {"intent": wanted, "was": got,
                                "script": intent_eid})
            except Exception:                                # noqa: BLE001
                pass
            try:
                ctrl.client.call_service("script", "turn_on", intent_eid)
            except Exception as e:                           # noqa: BLE001
                print("  [ctlbridge] converge %s failed: %s"
                      % (area_slug, e), flush=True)
            return

        # ── No recent intent: the original evidence-based path, unchanged.
        # A fully verified activity means the room is DONE -- nothing to finish.
        if active is not None and verified:
            return
        if on_tuner:
            return                      # display says tuner; broadcast needs no help
        # Resolve the INTENDED activity from evidence:
        target = None
        matches = [a for a in src_acts if self._route_matches(a, disp_src)]
        if len(matches) == 1:
            target = matches[0]
        elif not disp_src and len(src_acts) == 1:
            target = src_acts[0]        # unambiguous single-source room
        if target is None:
            return
        try:
            if getattr(target.summary(snap), "ok", False):
                return                  # already satisfied
        except Exception:
            pass
        script_eid = None
        try:
            script_eid = ctrl._script_entity_for(target)
        except Exception:
            script_eid = None
        if not script_eid:
            return
        # Is someone already driving? (a recent run of this script = hands off)
        try:
            srec = snap.get(script_eid) or {}
            lt = (srec.get("attributes") or {}).get("last_triggered")
            if lt:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(str(lt).replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - dt).total_seconds() < 120:
                    return
        except Exception:
            pass
        self._conv[area_slug] = now
        print("  [ctlbridge] intent-converge %s -> %s (external control detected, "
              "running %s)" % (area_slug, target.key, script_eid), flush=True)
        try:
            ctrl.client.call_service("script", "turn_on", script_eid)
        except Exception as e:                                   # noqa: BLE001
            print("  [ctlbridge] converge %s failed: %s" % (area_slug, e), flush=True)

    # -- overlay host liveness ----------------------------------------------
    def _sweep_host(self) -> None:
        if not self.savant_host:
            return
        alive = False
        for port in _PROBE_PORTS:
            try:
                with socket.create_connection((self.savant_host, port),
                                              timeout=_PROBE_TIMEOUT):
                    alive = True
                    break
            except OSError:
                continue
        self._publish("binary_sensor.proos_savant_host",
                      "on" if alive else "off",
                      {"friendly_name": "Savant Host",
                       "ip": self.savant_host,
                       "device_class": "connectivity",
                       "icon": "mdi:server-network"})

    # -- lifecycle -----------------------------------------------------------
    def sweep(self) -> None:
        if not self.enabled():
            return
        # ONE /api/states fetch per sweep, shared by every room, the converger
        # and anything else that needs a reading — per-room full fetches had
        # stretched the "5-second" sweep to ~30-60s on a large home.
        try:
            raw = self.client._req("GET", "/api/states") or []
        except Exception:
            raw = []
        snapall = {r.get("entity_id"): {"state": r.get("state", "unavailable"),
                                        "attributes": r.get("attributes") or {},
                                        "last_changed": r.get("last_changed")}
                   for r in raw if r.get("entity_id")}
        # Learn each device's published source_list off the SAME snapshot the
        # verdict ladder is about to use -- no extra HA calls. It has to be
        # continuous: the attribute is present on only some updates, so anything
        # request-driven simply misses it (measured across the whole house,
        # 30 Jul 2026).
        try:
            from . import appctl as _appctl_mod
            _n = _appctl_mod.observe_snapshot(snapall)
            if _n:
                print("  [appctl] learned source_list for %d device(s)" % _n, flush=True)
        except Exception as _e:                                  # noqa: BLE001
            print("  [appctl] learn failed: %s" % _e, flush=True)
        # #19: reconcile the publish cache against the same snapshot the rooms
        # are about to be judged from — an HA restart wiped the sensors, and
        # without this they stayed gone until the room's activity changed.
        self._reconcile_published(snapall)
        self._sweep_rooms(snapall)
        # host probe: 3 ports x 3s timeouts can eat 9s -- probe every 6th sweep
        self._hostn = getattr(self, "_hostn", 0) + 1
        if self._hostn >= 6:
            self._hostn = 0
            self._sweep_host()
        # additive: throttled health scan (~every 60s) on the SAME snapshot —
        # zero extra HA traffic; a scan failure can never touch the sweep
        try:
            if self.healthcheck is not None:
                self._healthn += 1
                if self._healthn >= 30:
                    self._healthn = 0
                    self.healthcheck(snapall)
        except Exception as e:                                   # noqa: BLE001
            print("  [ctlbridge] health scan failed: %s" % e, flush=True)

    def loop(self):
        time.sleep(30)                 # let controllers/discovery settle
        while True:
            try:
                self.sweep()
            except Exception as e:                               # noqa: BLE001
                print("  [ctlbridge] sweep error: %s" % e, flush=True)
            time.sleep(_INTERVAL)

    def start(self):
        t = threading.Thread(target=self.loop, daemon=True, name="proos-ctlbridge")
        t.start()
        print("  ctlbridge · publishing room activity verdicts every %ds%s"
              % (_INTERVAL, (" + Savant host %s" % self.savant_host)
                 if self.savant_host else ""), flush=True)
        return t
