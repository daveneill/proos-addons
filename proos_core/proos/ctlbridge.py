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

_INTERVAL = 5          # seconds between sweeps; publishes only on change
_PROBE_TIMEOUT = 3
_PROBE_PORTS = (22, 80, 443)     # Savant hosts answer SSH; 80/443 as fallbacks


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "x"


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
        self._held: dict = {}          # area_slug -> (state, key) verdict memory
        self._offpend: dict = {}       # area_slug -> consecutive off sweeps

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
    def _sweep_rooms(self) -> None:
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
                _witmap = self.witnesses
                _wit_ids = [s for a in acts.values()
                            for s in (_witmap.get(
                                getattr(a, "source_eid", None) or "") or {}
                                ).get("sensors", [])]
                snap = ctrl.client.snapshot(
                    list({e for a in acts.values() for e in a.entity_ids()}
                         | set(_wit_ids)))
                # Verdict ladder -- most specific evidence wins, and "off" is only
                # ever the conclusion when the room is actually dark:
                #   1. a source activity whose FULL verification passes
                #   2. a source that is plainly alive (on/playing/paused/idle)
                #      even if strict verification fails (witness gap, menu idle,
                #      Samsung power blip) -- published with verified: false
                #   3. broadcast watch_tv, only when NO source is alive
                #   4. off
                # tv_off / display_on never leak into the verdict vocabulary.
                from .activities import _st as _ast
                _SRC_ALIVE = ("on", "playing", "paused", "idle")
                src_acts = [a for a in acts.values()
                            if a.key not in ("display_on", "tv_off", "watch_tv")
                            and getattr(a, "source_eid", None)]
                bcast = acts.get("watch_tv")
                # Display's own testimony: which input is it on? The display is
                # the one witness that settles "source awake in the background"
                # vs "room actually on the tuner".
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
                active_key, active, verified = None, None, True
                for a in src_acts:                       # rung 1: verified
                    try:
                        if getattr(a.summary(snap), "ok", False):
                            active_key, active = a.key, a
                            break
                    except Exception:
                        continue
                evidence, wrate = None, None
                if active_key is None:          # rung 1.5: traffic witness --
                    # the network cannot lie about sustained throughput, so a
                    # streaming source is confirmed even when its integration
                    # session (pyatv etc.) has dropped.
                    for a in src_acts:
                        w = _witmap.get(a.source_eid)
                        if not w:
                            continue
                        rate = 0.0
                        for s_ in w["sensors"]:
                            try:
                                rate += float(_ast(snap, s_))
                            except (TypeError, ValueError):
                                pass
                        if rate >= w["min"]:
                            active_key, active = a.key, a
                            evidence, wrate = "traffic", round(rate, 3)
                            break
                if active_key is None and not on_tuner:  # rung 2: alive source
                    # (skipped when the display itself reports the tuner input --
                    # an awake-but-unselected source is background, not watching)
                    for a in src_acts:
                        if _ast(snap, a.source_eid) in _SRC_ALIVE:
                            active_key, active, verified = a.key, a, False
                            break
                if active_key is None and bcast is not None:   # rung 3: broadcast
                    try:
                        if getattr(bcast.summary(snap), "ok", False):
                            active_key, active = "watch_tv", bcast
                    except Exception:
                        pass
                # Cosmetic vocabulary: the sensor is already room-scoped, so
                # strip the area prefix from the source object_id
                # (watch_bedroom_bedroom_apple_tv -> watch_apple_tv). Pure string
                # transform of the FROZEN object_id -- stability identical to the
                # raw key. If two sources in the room would clean to the same
                # name, both keep their raw keys so values stay unique.
                def _clean(k):
                    if not k.startswith("watch_") or k == "watch_tv":
                        return k
                    toks = [t for t in k[6:].split("_") if t]
                    at = [t for t in str(area_slug).split("_") if t]
                    while at and len(toks) > len(at) and toks[:len(at)] == at:
                        toks = toks[len(at):]
                    return "watch_" + "_".join(toks) if toks else k
                cleaned = {}
                for a in src_acts:
                    cleaned[a.key] = _clean(a.key)
                vals = list(cleaned.values())
                for k, v in list(cleaned.items()):
                    if vals.count(v) > 1:
                        cleaned[k] = k
                state = cleaned.get(active_key, active_key) or "off"
                # -- verdict memory: absence of evidence is not evidence of off.
                # Flaky integration sessions (Apple TV Companion drops, Samsung
                # power blips) make sources vanish for minutes while the room is
                # visibly unchanged. Once a SPECIFIC source verdict is confirmed,
                # hold it while the display stays up and nothing contradicts it;
                # a downgrade requires positive evidence: the room actually dark,
                # or a different source coming alive. "off" additionally needs
                # two consecutive sweeps (10s) so a single display blip can't
                # kill a live room.
                held = self._held.get(area_slug)
                held_now = False
                if active is not None and getattr(active, "source_eid", None):
                    self._held[area_slug] = (state, active_key)      # fresh proof
                elif active is not None and on_tuner:
                    # the display CONFIRMS the tuner: positive evidence, replaces
                    # any remembered source verdict instead of being overridden
                    self._held[area_slug] = (state, active_key)
                elif held and disp not in ("off", "standby"):
                    state, active_key, held_now = held[0], held[1], True
                elif disp in ("off", "standby"):
                    self._held.pop(area_slug, None)
                if state == "off" and self._last.get(
                        "sensor.proos_activity_%s" % area_slug) not in (None, "off"):
                    n = self._offpend.get(area_slug, 0) + 1
                    self._offpend[area_slug] = n
                    if n < 2:
                        continue                        # confirm off next sweep
                self._offpend.pop(area_slug, None)
                attrs = {"friendly_name": "ProOS Activity — %s" % area_name,
                         "activity_key": active_key or "off",
                         "area": area_name,
                         "label": (active.label if active else "Off"),
                         "verified": (verified if active else True) and not held_now,
                         "held": held_now,
                         "icon": "mdi:television-play" if state != "off" else "mdi:television-off"}
                if active is not None and getattr(active, "source_eid", None):
                    attrs["source"] = active.source_eid
                if evidence:
                    attrs["evidence"] = evidence
                    attrs["witness_rate"] = wrate
                self._publish("sensor.proos_activity_%s" % area_slug, state, attrs)
            except Exception as e:                               # noqa: BLE001
                print("  [ctlbridge] %s sweep failed: %s" % (area_name, e), flush=True)

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
        self._sweep_rooms()
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
