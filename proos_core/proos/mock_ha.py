"""
A deterministic, in-memory Home Assistant stand-in.

It exists so we can prove the engine -- including failure handling -- without
touching real hardware, and run it in CI. It is faithful where faithfulness
matters:

  * The Samsung TV accepts select_source but NEVER exposes source on readback
    (source stays None), exactly like the real device. So the engine is forced
    to validate by sibling inference; it cannot cheat by reading TV input.

  * turn_on / turn_off mutate state the way the real integrations do.

Failure injection: pass `broken={entity_id}` to simulate a device that ignores
turn_on (e.g. an Apple TV that won't wake). This drives the DEGRADED path --
the money-shot demo.
"""
from __future__ import annotations
from .ha_client import Snapshot


class MockHA:
    def __init__(self, home_id: str, initial: dict[str, str],
                 broken: set[str] | None = None):
        self.home_id = home_id
        self.broken = broken or set()
        import time as _t
        self._now = _t.time
        # internal truth: state + attributes + last_changed per entity
        self._state: dict[str, dict] = {
            eid: {"state": s, "attributes": {}, "last_changed": self._now()}
            for eid, s in initial.items()
        }
        self._tv_input_internal: str | None = None

    def set_state(self, eid: str, state: str, minutes_ago: float = 0.0):
        """Test helper: force a state, optionally backdated (for fault timing)."""
        self._state.setdefault(eid, {"attributes": {}})
        self._state[eid]["state"] = state
        self._state[eid]["last_changed"] = self._now() - minutes_ago * 60

    def resolve_config_entry(self, entity_id: str) -> str:
        """Fake but stable per-entity entry id."""
        return "entry_" + entity_id.replace(".", "_")

    def reload_integration(self, entry_id: str) -> None:
        """Simulate a reload fixing a stale control channel: clear 'broken'."""
        # entry id maps back to an entity; clear any broken device under it.
        for eid in list(self.broken):
            if self.resolve_config_entry(eid) == entry_id:
                self.broken.discard(eid)

    def snapshot(self, entity_ids: list[str]) -> Snapshot:
        out: Snapshot = {}
        for eid in entity_ids:
            rec = self._state.get(eid, {"state": "unavailable", "attributes": {}, "last_changed": None})
            out[eid] = {"state": rec["state"], "attributes": dict(rec.get("attributes", {})),
                        "last_changed": rec.get("last_changed")}
        return out

    def call_service(self, domain: str, service: str, entity_id: str,
                     data: dict | None = None) -> None:
        if entity_id not in self._state:
            self._state[entity_id] = {"state": "unavailable", "attributes": {}}
        rec = self._state[entity_id]

        # A broken device silently ignores wake commands -- just like reality.
        if entity_id in self.broken and service in ("turn_on",):
            return

        if service == "turn_on":
            # Apple TV / Shield come up "playing"; a TV comes up "on".
            rec["state"] = "on" if entity_id.endswith("_tv") else "playing"
            rec["last_changed"] = self._now()
        elif service == "turn_off":
            rec["state"] = "off"
            rec["last_changed"] = self._now()
        elif service == "select_source":
            # Samsung quirk: we record the input internally but NEVER expose it.
            self._tv_input_internal = (data or {}).get("source")
            # rec["attributes"]["source"] stays absent on purpose.
