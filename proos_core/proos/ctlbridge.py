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
    def __init__(self, client, project_mod, get_controller,
                 enabled=lambda: True, savant_host: str = ""):
        self.client = client
        self.project = project_mod
        self.get_controller = get_controller
        self.enabled = enabled
        self.savant_host = (savant_host or "").strip()
        self._last: dict = {}          # entity_id -> last published state

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
                snap = ctrl.client.snapshot(
                    list({e for a in acts.values() for e in a.entity_ids()}))
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
                active_key, active, verified = None, None, True
                for a in src_acts:                       # rung 1: verified
                    try:
                        if getattr(a.summary(snap), "ok", False):
                            active_key, active = a.key, a
                            break
                    except Exception:
                        continue
                if active_key is None:                   # rung 2: alive source
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
                state = active_key or "off"
                attrs = {"friendly_name": "ProOS Activity — %s" % area_name,
                         "area": area_name,
                         "label": (active.label if active else "Off"),
                         "verified": verified if active else True,
                         "icon": "mdi:television-play" if active else "mdi:television-off"}
                if active is not None and getattr(active, "source_eid", None):
                    attrs["source"] = active.source_eid
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
