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

    Returns {state, label, confirmed, commanded_key, external[, missed]}.
    `missed` = {key, label, fired_at}: a watch command whose display never
    came on after it fired, once the boot grace has passed.
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
    mem["cmd_last"] = latest                   # REGISTER 1078: device_verdict reads when the command was sent

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
    out = {"state": "off", "label": "Off", "confirmed": confirmed,
           "commanded_key": (latest[0] if latest else None),
           "external": False}
    # THE COMMANDED ACTIVITY NEVER LANDED (register 513, reserved 19 Sep; the
    # 2 Aug clause "healmon's business"; plan item 3). A watch command past its
    # boot grace with the display never having come on SINCE it fired is a
    # fact — what was asked, when, and that power never answered. The room
    # still reads off (that is true); the miss rides alongside for Health.
    # No new window: BOOT_GRACE_S is the same patience the verdict already uses.
    if latest and latest[0] != "tv_off":
        on_since = mem.get("cmd_on_since")
        if on_since is None or on_since < latest[2]:
            out["missed"] = {"key": latest[0], "label": latest[1],
                             "fired_at": latest[2]}
    return out


# ── REGISTER 925: THE TV'S OWN INPUT DECIDES "VERIFIED" ──────────────────────
# Dave, 17 Oct 2026 (approving the change to his 2 Aug "commanded + power" ruling): "Yes both" — verified only when the
# TV is on that activity's input where the TV reports one; otherwise unconfirmed with the reason. And the standing rule:
# "this system is all about confirmation not assuming … otherwise its not actual state based awareness its virtual".
# Read on the box (923a): after the TV was switched to its own Foxtel app with its remote, the Bedroom kept publishing
# "watch_apple_tv — verified: commanded, and the display confirms power" while the Samsung reported source "Foxtel".
def input_verdict(cs, source_of, inputs, disp_row):
    """Check a commanded, power-confirmed activity against the input the TV reports.

    cs        — decide_cmd's answer.
    source_of — {activity_key: source entity or None} (None = the TV's own activity, Watch TV).
    inputs    — the room record's committed {entity: input name}.
    disp_row  — the display's own state row.
    Returns cs, extended with input_check: "agrees" | "unreported" | "contradicted" (+ the readings). A contradicted
    activity is NOT claimed: the room reads on (the TV is on), unconfirmed, and says what the TV reports — the same
    shape decide_cmd already publishes for a room lit by other means."""
    key = cs.get("commanded_key")
    if not (cs.get("confirmed") and key and key != "tv_off" and cs.get("state") not in ("on", "off")):
        return cs
    src = str(((disp_row or {}).get("attributes") or {}).get("source") or "").strip()
    if not src:
        return dict(cs, input_check="unreported")
    box_inputs = {str(v).strip() for e, v in (inputs or {}).items() if v}
    se = (source_of or {}).get(key)
    want = str((inputs or {}).get(se) or "").strip() if se else ""
    if se and want:
        ok = (src == want)
    elif se:
        return dict(cs, input_check="unreported", tv_reports=src)   # no committed input for that box — can't check
    else:
        own = str((inputs or {}).get((disp_row or {}).get("entity_id")) or "").strip()
        ok = src not in (box_inputs - ({own} if own else set()))    # the TV's own: on none of the boxes' inputs
    if ok:
        return dict(cs, input_check="agrees", tv_reports=src)
    return {"state": "on", "label": "On", "confirmed": False, "commanded_key": key, "external": True,
            "input_check": "contradicted", "tv_reports": src, "expected": want or None,
            "asked": cs.get("label") or key}


# ── REGISTER 926 ITEM 2: "NEVER CAME ON" IS READ FROM THE DISPLAY'S OWN HISTORY ─────────────────────────────────────
# Dave, 17 Oct 2026: "I am all for incidents being reported but not when they are fake … any incident reported needs to
# be actual and not virtual". Read on the box: the Bedroom TV was on from 12:47; Watch Apple TV fired at 12:52:31 with the
# TV on; the TV was switched off outside ProOS at 12:52:55 — and Health said "Bedroom TV never came on". decide_cmd's miss
# compares the CURRENT on-session's start with the fire time, so a display that was already on when asked, then turned
# off, reads as never having come on; and that memory is lost on every Core restart, so the same false card returned.
# The measurement is the platform's own recorder: did the display report power at any moment from the fire onwards?
def display_came_on(rows):
    """rows — the display's recorder rows from (just before) the fire until now, as [(ts, state), ...]; the first row is
    the state it was in at that moment. True: it reported power at some point (on/playing/paused — the same power
    signals the room's verdict uses). False: it never did. None: no rows — nothing was read, so nothing is claimed."""
    if not rows:
        return None
    return any(str(s or "").lower() in ("on", "playing", "paused") for _ts, s in rows)


def confirm_miss(miss, mem, read_rows):
    """decide_cmd's `missed` stands only when the display's own history shows it never came on.

    miss      — {key, label, fired_at} or None.
    mem       — the room's memory; the answer is kept per fire (key miss_read), so the recorder is asked once per command.
    read_rows — fired_at -> rows for display_came_on, or None when the recorder could not be read.
    Returns the miss when measured as never-on; None when it came on, or when the history could not be read (an
    unmeasured fault is not raised — it is asked again on the next sweep)."""
    if not miss:
        return None
    fired = miss.get("fired_at")
    kept = mem.get("miss_read")
    if not (isinstance(kept, (list, tuple)) and len(kept) == 2 and kept[0] == fired):
        try:
            came = display_came_on(read_rows(fired))
        except Exception:                                        # noqa: BLE001
            came = None
        if came is None:
            return None
        mem["miss_read"] = (fired, came)
        kept = mem["miss_read"]
    return None if kept[1] else miss


# ── REGISTER 1078: THE ROOM'S STATE IS WHAT ITS DEVICES REPORT ─────────────────────────────────────────────────────────
# Dave, 30 Oct 2026 06:43–06:56, ruling on the Device Truth Review: "ProOS would report state for everything that has
# integration and assigned to a room meaning actual state not I sent an on so it should be on thats assumption … a room
# can then be commissioned with activities which in theory is just a scene"; "1. yes … 4. if a device can not confirm then
# it can only send". This replaces the 2 Aug "commanded + power" ruling for the published room state (decide_cmd and
# input_verdict above stay as the record of what was SENT; they no longer decide what the room IS):
#   - an activity is named only when the devices themselves show it: the TV reports power AND the activity's input
#     (through the AV switch where the room has one: the TV on the switch's output and the switch on the box's input),
#     AND the box reports it is awake. Who started it — ProOS, the TV's own remote, CEC — does not matter and is not
#     claimed (external is never set).
#   - a TV that does not report its input cannot confirm any activity: the room is On (power is a reading), and what was
#     sent rides along as sent, never as confirmed.
#   - a just-sent activity reads "Starting <name>" while its devices come up (BOOT_GRACE_S, the patience decide_cmd
#     already uses); a just-sent Off on a TV still reporting power reads "Turning Off" (OFF_GRACE_S). Both say they are
#     waiting on a command; neither is confirmed.
#   - a display that reports nothing (unavailable/unknown) is "No Reading", never Off.
_AWAKE = ("on", "playing", "paused", "idle")


def _input_shaped(src):
    """Is this source string an INPUT (HDMI 3, AV, Line…) rather than content (an app)? ONE vocabulary: ctlbridge's
    _INPUT_SHAPED (which mirrors appctl's)."""
    try:
        from .ctlbridge import _INPUT_SHAPED
        return bool(_INPUT_SHAPED.match(str(src).strip()))
    except Exception:                                    # noqa: BLE001
        return True                                      # can't tell -> never claim it is the TV's own app


def device_verdict(cs, acts, rec, snap, disp_eid, disp_on, mem, now=None):
    """The room's published state, from its devices' own reports.

    cs      — decide_cmd's answer (what was sent; used only for Starting / Turning Off / the sent note).
    acts    — [(key, label, source_eid or None)] for every activity except tv_off; None (or the display itself) is the
              TV's own activity (Watch TV).
    rec     — the room record: inputs {entity: display input}, avswitch {entity, output, inputs {entity: switch input}}.
    snap    — {entity_id: {state, attributes}}.
    Returns {state, label, confirmed, commanded_key, external, basis, unconfirmed, source, starting}."""
    if now is None:
        import time as _t
        now = _t.time()
    row = lambda e: (snap.get(e) or {}) if e else {}
    st = lambda e: str(row(e).get("state") or "").lower()
    at = lambda e, k: (row(e).get("attributes") or {}).get(k)
    last = mem.get("cmd_last") or None
    sent_key, sent_label, sent_at = (last if last else (None, None, None))
    labels = {k: l for k, l, _s in acts}
    name = lambda k: labels.get(k) or (str(k or "").replace("watch_", "Watch ").replace("_", " ").title())
    _where = lambda n: n[6:] if str(n).startswith("Watch ") else n   # "Watch Apple TV" -> "Apple TV", "Watch TV" -> "TV"
    fresh = bool(sent_at is not None and now - sent_at <= BOOT_GRACE_S)
    base = {"commanded_key": sent_key, "external": False, "unconfirmed": None, "source": None, "starting": False}

    dstate = st(disp_eid)
    if dstate in ("unavailable", "unknown", ""):
        return dict(base, state="unknown", label="No Reading", confirmed=False, basis=None,
                    unconfirmed="the TV isn't reporting anything")

    if not disp_on:
        if sent_key and sent_key != "tv_off" and fresh:
            return dict(base, state=sent_key, label="Starting " + name(sent_key).replace("Watch ", ""),
                        confirmed=False, basis=None, starting=True,
                        unconfirmed="%s was sent; the TV hasn't reported power yet" % name(sent_key))
        # a TV that reports power but is not counted on is resting on its artwork (Art Mode is off — Dave's ruling;
        # the room says so rather than "the TV reports on" under an Off)
        _why = ("the TV is resting on Art Mode" if dstate in ("on", "playing", "paused")
                else "the TV reports %s" % (dstate or "off"))
        out = dict(base, state="off", label="Off", confirmed=True, basis=_why)
        if cs.get("missed"):
            out["missed"] = cs["missed"]
        return out

    # the TV reports power
    if sent_key == "tv_off" and sent_at is not None and now - sent_at <= OFF_GRACE_S:
        return dict(base, state="on", label="Turning Off", confirmed=False, basis="the TV reports power",
                    unconfirmed="Off was sent; the TV still reports power")
    src = str(at(disp_eid, "source") or "").strip()
    inputs = (rec or {}).get("inputs") or {}
    sw = (rec or {}).get("avswitch") or {}
    sw_e, sw_out, sw_in = sw.get("entity"), str(sw.get("output") or "").strip(), (sw.get("inputs") or {})
    sw_src = str(at(sw_e, "source") or "").strip() if sw_e else ""
    box_inputs = {str(v).strip() for e, v in inputs.items() if v and e != disp_eid}
    if sw_out:
        box_inputs.add(sw_out)
    own = str(inputs.get(disp_eid) or "").strip()

    def why_not(key, se):
        """None when the devices show this activity; else the reason they don't."""
        if se is None or se == disp_eid:
            if not src:
                return "the TV doesn't report its input"
            if src == own:
                return None
            if src in box_inputs or _input_shaped(src):
                return "the TV reports %s" % src        # an input — a box's, or one no activity uses (a console)
            return None                                  # content, not an input: the TV's own app
        want = str(inputs.get(se) or "").strip()
        if want:
            if not src:
                return "the TV doesn't report its input"
            if src != want:
                return "the TV reports %s, not %s" % (src, want)
        elif sw_e and sw_in.get(se):
            if not src:
                return "the TV doesn't report its input"
            if sw_out and src != sw_out:
                return "the TV reports %s, not %s" % (src, sw_out)
            if not sw_src:
                return "the AV switch doesn't report its input"
            if sw_src != str(sw_in.get(se)).strip():
                return "the AV switch reports %s, not %s" % (sw_src, sw_in.get(se))
        else:
            return "no input is set for it in Pro"
        if st(se) not in _AWAKE:
            return "the %s reports %s" % (name(key).replace("Watch ", ""), st(se) or "nothing")
        return None

    shown = [(k, se) for k, _l, se in acts if why_not(k, se) is None]
    if shown:
        shown.sort(key=lambda x: 0 if x[0] == sent_key else 1)
        k, se = shown[0]
        where = ("%s through the AV switch on %s" % (src, sw_src)) if (se and se != disp_eid and not inputs.get(se)) else src
        basis = "the TV reports power on %s" % where
        if se and se != disp_eid:
            basis += ", and the %s reports %s" % (name(k).replace("Watch ", ""), st(se))
        # Dave 30 Oct 07:33: once confirmed the room says WHERE it is ("Apple TV"), not the action ("Watch Apple TV")
        return dict(base, state=k, label=_where(name(k)), confirmed=True, basis=basis,
                    source=(se if se and se != disp_eid else None))
    out = dict(base, state="on", label="On", confirmed=True,
               basis="the TV reports power" + ((" on %s" % src) if src else ""))
    if sent_key and sent_key != "tv_off":
        se = next((s for k, _l, s in acts if k == sent_key), None)
        if fresh:
            out.update(state=sent_key, label="Starting " + name(sent_key).replace("Watch ", ""), confirmed=False,
                       starting=True)
        if cs.get("state") == sent_key or fresh:      # sent during THIS on-session
            out["unconfirmed"] = "%s was sent, but it can't be confirmed: %s" % (name(sent_key), why_not(sent_key, se))
    return out
