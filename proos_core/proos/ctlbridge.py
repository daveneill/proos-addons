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
        testif = [a for a in alive if route_matches(a, disp_src) or _awm(a)]
        pick = (testif[0] if len(testif) == 1
                else (alive[0] if len(alive) == 1 else None))
        if pick is not None:
            active_key, active, verified = pick.key, pick, False
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
        self._conv: dict = {}          # area_slug -> last convergence ts
        self._roommem: dict = {}       # area_slug -> decide() memory
        self.converge = True           # intent convergence (option-gated in server)

    @property
    def witnesses(self) -> dict:
        w = self._witnesses
        try:
            return w() if callable(w) else w
        except Exception:
            return {}

    # -- publishing ----------------------------------------------------------
    def _publish(self, eid: str, state: str, attrs: dict) -> None:
        if self._last.get(eid) == state:
            return
        try:
            self.client._req("POST", "/api/states/%s" % eid,
                             {"state": state, "attributes": attrs})
            self._last[eid] = state
            print("  [ctlbridge] %s -> %s" % (eid, state), flush=True)
        except Exception as e:                                   # noqa: BLE001
            print("  [ctlbridge] publish %s failed: %s" % (eid, e), flush=True)

    # -- room activity verdicts ---------------------------------------------
    def _mem(self, area_slug: str) -> dict:
        m = self._roommem.setdefault(area_slug, {})
        m["last"] = self._last.get("sensor.proos_activity_%s" % area_slug)
        return m

    def _sweep_rooms(self, snapall: dict) -> None:
        proj = self.project.load() or {}
        for key, rec in (proj.get("areas") or {}).items():
            if not (rec and rec.get("committed")):
                continue
            area_name = rec.get("name") or key
            area_slug = rec.get("area_id") or _slug(key)
            try:
                ctrl = self.get_controller(area_name)
                acts = ctrl.activities
                if not acts:
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
                self._publish("sensor.proos_activity_%s" % area_slug,
                              d["state"], attrs)
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

    def _maybe_converge(self, area_slug, ctrl, src_acts, bcast, snap,
                        active, verified, disp_src, on_tuner) -> None:
        import time as _t
        # A fully verified activity means the room is DONE -- nothing to finish.
        if active is not None and verified:
            return
        if on_tuner:
            return                      # display says tuner; broadcast needs no help
        now = _t.time()
        if now - self._conv.get(area_slug, 0) < 120:
            return                      # one attempt per episode
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
        self._sweep_rooms(snapall)
        # host probe: 3 ports x 3s timeouts can eat 9s -- probe every 6th sweep
        self._hostn = getattr(self, "_hostn", 0) + 1
        if self._hostn >= 6:
            self._hostn = 0
            self._sweep_host()

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
