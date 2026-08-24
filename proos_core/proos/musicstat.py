"""
Music-room status producer.

ProOS publishes ONE status per committed room at sensor.proos_activity_<area>,
with one attribute shape, whatever the room is. Consumers — Pro, the dashboard,
Assist — read that one thing and never branch on room type.

Two producers fill it:

    kind 'tv'     -> ctlbridge.decide(), the six-rung verdict ladder
    kind 'music'  -> this module

This module deliberately does NOT run the ladder. The ladder exists because a
TV room has to be INFERRED: no single device will tell you what the screen is
showing, so the engine convenes witnesses and reasons about them. A music room
has no such problem — the speaker states plainly that it is playing, what it is
playing, and who it is grouped with. Direct evidence needs no inference, and
reusing a frozen, bench-gated engine to restate a fact would be a change to
that engine for no gain.

Pure function, no I/O, no client — same contract as decide(), so it benches
offline (tests/music_status_bench.py).

Motivating failure, measured on the live house 31 Jul 2026 after a clean
factory reset and re-commission: Ryan's Room, Office and Study are all
committed and all publish nothing, because ctlbridge._sweep_rooms() bails at
`if not acts: continue` and a music room has no AV activities.
"""

# A speaker is mains-powered and effectively never off. So these states do not
# mean "the device is powered down" -- they mean the room has nothing to say,
# and it should be CLEARED from the live surface. `off` here is a presentation
# state, not a power state (ProOS_Room_Status_Spec §2). Reading power would be
# wrong on every brand.
_CLEARED = ("off", "standby", "unavailable", "unknown", "", None)

# Paused and stopped are the same thing to a room: something is set up but
# nothing is coming out of the speakers.
_IDLE = ("paused", "idle", "stopped", "buffering")

# Installer's per-speaker "auto-clear when paused" (spec, 1 Aug 2026). ONE
# vocabulary, three readers: Pro writes the entity label proos_pause_<key>,
# the homeowner dashboard clears the CARD after this long, and this producer
# clears the room's LIVE STATUS on the same clock — card and status can never
# disagree because they share the label. No label = Disabled = idle stays,
# exactly as before this table existed.
PAUSE_LABEL_SECONDS = {
    "proos_pause_10s": 10, "proos_pause_15s": 15, "proos_pause_30s": 30,
    "proos_pause_45s": 45, "proos_pause_1m": 60, "proos_pause_2m": 120,
    "proos_pause_5m": 300, "proos_pause_10m": 600, "proos_pause_15m": 900,
    "proos_pause_30m": 1800,
}


def _paused_for(snap, eid, now):
    """Seconds this entity has sat in its current state, or None if unknowable.

    last_changed arrives as an ISO string from live HA and as an epoch float
    from mock_ha; both parse. Anything else -> None, and None NEVER clears —
    a broken clock must not blank a room (fail-open, like everything here).
    """
    if now is None:
        return None
    lc = (snap.get(eid) or {}).get("last_changed")
    if lc is None:
        return None
    try:
        if isinstance(lc, (int, float)):
            return max(0.0, float(now) - float(lc))
        from datetime import datetime
        dt = datetime.fromisoformat(str(lc).replace("Z", "+00:00"))
        return max(0.0, float(now) - dt.timestamp())
    except Exception:                                            # noqa: BLE001
        return None


def _eff_state(snap, eid, pause_s, now):
    """The speaker's state AS PRESENTED: idle past its installer-set
    auto-clear window reads as off (presentation, never power)."""
    s = _st(snap, eid)
    if s in _IDLE and pause_s:
        try:
            secs = int(pause_s.get(eid) or 0)
        except Exception:                                        # noqa: BLE001
            secs = 0
        if secs > 0:
            pf = _paused_for(snap, eid, now)
            if pf is not None and pf >= secs:
                return "off"
    return s


def _members(rec) -> list:
    """Committed audio members of the room, as entity ids.

    Accepts either bare entity-id strings or the dict form, because the project
    record has carried both shapes over time and a status producer is the wrong
    place to be strict about it.
    """
    out = []
    for bucket in ("speakers", "audio"):
        for item in (rec.get(bucket) or []):
            eid = item
            if isinstance(item, dict):
                eid = item.get("entity") or item.get("entity_id")
            if isinstance(eid, str) and eid and eid not in out:
                out.append(eid)
    return out


def _st(snap, eid) -> str:
    rec = snap.get(eid) or {}
    return (rec.get("state") or "").strip().lower()


def _attrs(snap, eid) -> dict:
    return (snap.get(eid) or {}).get("attributes") or {}


def decide_music(rec, snap, area_of=None, pause_s=None, now=None,
                 area_names=None):
    """Return the room's status, or None when this producer has nothing to say.

    rec      -- the committed area record
    snap     -- {entity_id: {'state':..., 'attributes': {...}}}
    area_of  -- callable(entity_id) -> room name, for resolving a group
                coordinator that lives in another room. Optional.
    pause_s  -- {entity_id: auto-clear seconds} from the installer's
                proos_pause_* labels. Optional; absent = nothing auto-clears.
    now      -- epoch seconds, for the auto-clear clock. Optional.

    None means "not mine, or nothing known" — never a fabricated verdict. A
    device ProOS has never heard from gets no status, exactly as the tile and
    app-list paths refuse to invent for an unknown device.
    """
    if not rec or not rec.get("committed"):
        return None
    if (rec.get("kind") or "").lower() != "music":
        return None                      # a tv room belongs to the ladder

    members = _members(rec)
    if not members:
        return None

    # Never invent: if not one committed speaker appears in the snapshot, we
    # know nothing about this room and say nothing about it.
    known = [e for e in members if e in snap]
    if not known:
        return None

    # STAGE 2a (23 Aug 2026, register 256): the LIVE name first — the
    # record's copy speaks only when no live reading is in hand.
    area_name = ((area_names or {}).get(rec.get("area_id") or "")
                 or rec.get("name") or rec.get("area_id") or "")

    # The room is playing if ANY of its speakers is. Prefer that one as the
    # subject; otherwise fall back to the first speaker we actually know, so
    # idle and cleared rooms still name an entity for consumers to hang on to.
    playing = [e for e in known if _st(snap, e) == "playing"]
    subject = playing[0] if playing else known[0]
    sub_attrs = _attrs(snap, subject)

    # ── grouping ────────────────────────────────────────────────────────────
    # HA lists the coordinator FIRST in group_members. A group of one is not a
    # group. A group whose coordinator is a speaker of THIS room is not a
    # cross-room group either -- the room is still the origin.
    group = [g for g in (sub_attrs.get("group_members") or [])
             if isinstance(g, str)]
    coordinator = group[0] if len(group) > 1 else None
    grouped_to = None
    if coordinator and coordinator not in members:
        grouped_to = (area_of(coordinator) if callable(area_of) else None) \
            or None

    # Now-playing comes from the coordinator when the room is only along for
    # the ride: the joined speaker mirrors audio but not always metadata.
    meta_from = coordinator if (grouped_to and coordinator in snap) else subject
    meta = _attrs(snap, meta_from)

    # ── state ───────────────────────────────────────────────────────────────
    # Effective states apply the installer's auto-clear window: idle past the
    # per-speaker timeout PRESENTS as off. Playing is judged on raw state and
    # always wins — a playing speaker can never be cleared by a clock.
    auto_cleared = False
    eff = {}
    for e in known:
        eff[e] = _eff_state(snap, e, pause_s, now)
        if eff[e] == "off" and _st(snap, e) in _IDLE:
            auto_cleared = True
    sub_state = eff[subject]
    if playing:
        state = "playing"
    elif any(eff[e] in _IDLE for e in known):
        state = "idle"
    elif sub_state in _CLEARED or all(eff[e] in _CLEARED for e in known):
        state = "off"
    else:
        # An unfamiliar state is not evidence of silence. Treat it as idle
        # rather than clearing a room that may well be doing something.
        state = "idle"

    if state == "playing":
        label = "Playing (grouped to %s)" % grouped_to if grouped_to else "Playing"
    elif state == "idle":
        label = "Idle"
    else:
        label = "Off"

    out = {
        "state": state,
        "label": label,
        # Parity with the AV producer: the keys a consumer can always rely on.
        "activity_key": "off" if state == "off" else "listen",
        "area": area_name,
        # Direct evidence, so there is nothing to hold or half-believe. The
        # ladder's `verified`/`held` exist for inference; here they are simply
        # true and false, and they are present so consumers need no branch.
        "verified": True,
        "held": False,
        "icon": "mdi:music" if state != "off" else "mdi:music-off",
        "source": subject,
        "audio_entity": subject,
        "kind": "music",
        "grouped_to": grouped_to,
        "coordinator": coordinator if grouped_to else None,
        "members": known,
    }
    if state == "off" and auto_cleared:
        out["auto_cleared"] = True       # diagnosis: cleared by the timer,
                                         # not by the speakers themselves

    for key in ("media_title", "media_artist", "media_album_name", "source"):
        val = meta.get(key)
        if val:
            out["media_source" if key == "source" else key] = val

    return out
