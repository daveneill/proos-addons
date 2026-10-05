"""
Independent reachability — the second signal, read from the PLATFORM.

A device's own integration can sit on a stale session for minutes after a
clean network cut (no TCP reset; apple_tv waits on its keepalive). The second
signal is a witness the platform already holds for the device: a router /
UniFi device_tracker (home / not_home) or a `ping` binary_sensor (on / off).
The platform does the ICMP or holds the controller's client table; Core reads
it. This module's own docstring called that "the production path" while a
TCP probe from Core stood beside it as the other source — a connect on a
fixed per-brand port where a REFUSED connection counted as alive and a
camera's liveness was "the NVR answers on 443". REGISTER 448 (13 Sep 2026,
Build F of the mirror programme): the probe is retired. A spec that carries
only an address is an ADDRESS (kept for the port reading and the tracker
join), not a witness; a device with no platform witness is single-signal and
says so — never "presumed dead" on a guess.

resolve(spec, client) -> True / False / None(unknown or no witness).
"""
from __future__ import annotations

def is_witness(spec) -> bool:
    """Only a platform sensor is a witness. An ip alone is an address."""
    return bool(spec and isinstance(spec, dict) and spec.get("sensor"))


def resolve(spec: dict, client=None) -> bool | None:
    """Resolve a reachability spec to True/False/None(unknown)."""
    if not is_witness(spec) or client is None:
        return None
    try:
        snap = client.snapshot([spec["sensor"]])
        st = snap.get(spec["sensor"], {}).get("state")
        if st in ("on", "home"):
            return True
        # A-6, APPLIED EVERYWHERE (Dave's ruling 16 Aug, register 155;
        # extended in Stage 2 build 1 of the rescue). Only a witness making
        # a POSITIVE statement convicts: off/not_home means "it left the
        # network" and resolves False. `unavailable`/`unknown` mean the
        # witness itself has not testified — usually the network
        # integration reloading — and resolve None, never False. Before this
        # line, one UniFi reload turned all 111 trackers `unavailable` and
        # every witnessed device in every room was accused of "not
        # responding" in the same instant. Silence is not evidence.
        if st in ("off", "not_home"):
            return False
    except Exception:
        return None
    return None
