"""Commanded room state — the product reset of 2 Aug 2026.

Dave, after three inference builds in one afternoon each found a new way
the panel lies: "We are never going to reliably say what the TV is doing
and what mode it is in... this has to be based on the actual device being
on or off... what we said the device was going to do when we control it —
then we can confirm if it switched."

So the room's activity is what ProOS COMMANDED, confirmed against the one
thing hardware reports reliably: power. No command = no activity claim —
a lit room nobody commanded is "on", a device fact, never a guess at what
is on screen. The inference ladder (decide) survives as DIAGNOSTICS for
Pro and the journal; it no longer speaks to the homeowner.

Pure function, mem-driven, benched by tests/cmdstate_bench.py.
"""
from datetime import datetime, timezone

# A watch command may precede the panel actually lighting (WOL boot); inside
# this grace the room reads the commanded activity, unconfirmed ("Starting").
BOOT_GRACE_S = 90
# tv_off may take a moment to land; beyond this, a still-lit room makes the
# off UNCONFIRMED — a fact for incidents, never a fight (observe only).
OFF_GRACE_S = 90
# A command can claim an on-session that began up to this long after it
# fired (script fires -> panel takes seconds to boot and report).
CMD_LEADS_ON_S = 120


def _fired_at(snap, eid):
    lt = ((snap.get(eid) or {}).get("attributes") or {}).get("last_triggered")
    if not lt:
        return None
    try:
        if isinstance(lt, (int, float)):
            return float(lt)
        return datetime.fromisoformat(
            str(lt).replace("Z", "+00:00")).timestamp()
    except Exception:                                        # noqa: BLE001
        return None


def decide_cmd(area_slug, snap, commands, disp_on, mem, now=None):
    """The commanded-state machine for one room, one sweep.

    commands: [(activity_key, script_eid, label), ...] — every generated
              activity script for the room including tv_off.
    disp_on:  the room's REAL power truth, supplied by the caller from
              settled power signals only (display power / audio playing —
              a session state must never reach this flag).
    mem:      per-room dict (shared with the sweep's other memory; keys
              used here: cmd_on, cmd_on_since, cmd_off_since).

    Returns {state, label, confirmed, commanded_key, external}.
    """
    if now is None:
        import time as _t
        now = _t.time()

    # on-session tracking: when did the room last transition on / off
    if disp_on:
        if not mem.get("cmd_on"):
            mem["cmd_on"] = True
            mem["cmd_on_since"] = now
    else:
        if mem.get("cmd_on") or "cmd_off_since" not in mem:
            mem["cmd_on"] = False
            mem["cmd_off_since"] = now
    on_since = mem.get("cmd_on_since")
    off_since = mem.get("cmd_off_since")

    # newest fired command
    latest = None                              # (key, label, fired_at)
    for key, eid, label in commands:
        ts = _fired_at(snap, eid)
        if ts is not None and ts <= now + 5 and \
                (latest is None or ts > latest[2]):
            latest = (key, label, ts)

    if disp_on:
        if latest and latest[0] != "tv_off":
            # a command claims THIS on-session only: it must not predate
            # the session by more than the boot lead, and no off-settle may
            # sit between the command and now.
            key, label, ts = latest
            session_ok = (on_since is None
                          or ts >= on_since - CMD_LEADS_ON_S)
            not_broken = (off_since is None or ts >= off_since
                          or (on_since is not None and on_since <= ts))
            if session_ok and not_broken:
                return {"state": key, "label": label, "confirmed": True,
                        "commanded_key": key, "external": False}
        if latest and latest[0] == "tv_off":
            # we said off and the room is lit
            if now - latest[2] <= OFF_GRACE_S:
                # still landing — report off, unconfirmed, hands off
                return {"state": "off", "label": "Off", "confirmed": False,
                        "commanded_key": "tv_off", "external": False}
            # off long ago and the room is lit: reality wins — the room
            # reads on — but the unconfirmed off rides along as a fact
            # (healmon's business, never a fight).
            return {"state": "on", "label": "On", "confirmed": False,
                    "commanded_key": "tv_off", "external": True}
        return {"state": "on", "label": "On", "confirmed": True,
                "commanded_key": None, "external": True}

    # room dark
    if latest and latest[0] != "tv_off" and now - latest[2] <= BOOT_GRACE_S:
        # fresh watch command, panel still booting: Starting
        return {"state": latest[0], "label": latest[1], "confirmed": False,
                "commanded_key": latest[0], "external": False}
    confirmed = True
    if latest and latest[0] == "tv_off":
        confirmed = True                       # asked off, is off
    return {"state": "off", "label": "Off", "confirmed": confirmed,
            "commanded_key": (latest[0] if latest else None),
            "external": False}
