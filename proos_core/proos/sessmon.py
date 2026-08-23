"""Session-stability watching — the device-reliability layer (2 Aug 2026).

Dave: "We need to now focus on the actual devices and their reliability —
Apple TV dropping info etc and it being able to reload the integration."
The recorder's evidence: the Family Room Apple TV collapsed idle->off in
22 SECONDS of hands-off; the Shield flapped in 3-second blips. A device
whose reporting link flaps is a device whose truth cannot be trusted —
and that is an INCIDENT with a mechanical repair (reload the integration),
never something an installer should discover by debugging card visibility.

Pure and mem-driven. healthmon owns the incidents and the reload action;
this module only counts.
"""

ALIVE = ("on", "playing", "paused", "idle")
# STAGE 3 BUILD 2 (16 Aug 2026): a person turning their TV off is NOT a
# "drop". The old DEAD set included off/standby — deliberate human power
# states — so three power-cycles in an evening of telly read as "link
# unstable" and earned an unrequested integration reload (census X1). A
# drop is now losing the REPORTING LINK: transitions to states no human
# causes with a remote. HA natively distinguishes becoming-unavailable
# from user-off; so does this counter now. The 2 Aug phantom-off case
# (Apple TV session loss disguised as "off") belongs to the frozen-session
# check, where the witness's TRAFFIC is the evidence — a phantom off with
# no traffic cannot be told from a person's button press, and a claim
# that cannot be distinguished is not made. Both sets are FACT class:
# translations of HA's own state vocabulary.
LINK_LOST = ("unavailable", "unknown", None, "")
POWERED_OFF = ("off", "standby")

# Rolling window and threshold: >= THRESHOLD alive->link-lost collapses
# inside WINDOW_S = the reporting link is unstable.
WINDOW_S = 1800
THRESHOLD = 3
# A heal (integration reload) fires at most once per entity per cooldown —
# a flapping device must never trigger reload storms.
RELOAD_COOLDOWN_S = 3600


def _rec(mem, eid):
    return mem.setdefault(eid, {"last": None, "drops": []})


def observe(mem, eid, state, now):
    """Feed one sweep's state. Returns 'drop', 'recover' or None."""
    r = _rec(mem, eid)
    prev, r["last"] = r["last"], state
    if prev is None:
        return None
    was_alive = prev in ALIVE
    is_alive = state in ALIVE
    if was_alive and state in LINK_LOST:
        r["drops"].append(now)
        # prune as we go so mem never grows unbounded
        r["drops"] = [t for t in r["drops"] if now - t <= WINDOW_S]
        return "drop"
    if not was_alive and is_alive:
        return "recover"
    return None


def drop_count(mem, eid, now):
    r = _rec(mem, eid)
    r["drops"] = [t for t in r["drops"] if now - t <= WINDOW_S]
    return len(r["drops"])


def unstable(mem, eid, now):
    return drop_count(mem, eid, now) >= THRESHOLD


def heal_due(mem, eid, now):
    """True when a reload heal may fire for this entity (per-entity
    cooldown). Marks the attempt — call only when actually healing."""
    r = _rec(mem, eid)
    last = r.get("healed_at")
    if last is not None and now - last < RELOAD_COOLDOWN_S:
        return False
    r["healed_at"] = now
    return True
