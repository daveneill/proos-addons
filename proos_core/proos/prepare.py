"""
ProOS Core — preparation audit (matrix #13, first shippable slice).

WHY THIS EXISTS (measured, 31 Jul – 1 Aug 2026)
-----------------------------------------------
A factory reset silently dropped three required settings, and each cost hours
of log archaeology instead of seconds at commissioning:

  * the Frame's `app_list` — the panel never answers ed.installedApp.get
    (13+ requests, zero replies), so with no committed list the room has no
    apps and nothing says why;
  * `ip_control_art_mode` — absent means False in the fork, art readback fell
    to the websocket client which misreports on this panel, and the room read
    "Watch Apple TV" all night while showing artwork;
  * UniFi `allow_bandwidth_sensors` — absent means False, zero data-rate
    sensors existed, and every committed source raised "no network witness".

The audit walks every committed room and names what is missing and how to fix
it. It ADVISES — it never blocks a commit and never changes what green means.

DESIGN RULES (same contract as netevidence.py)
----------------------------------------------
* Facts, not code: per-integration requirements live in PREPARE_FACTS.
  Certifying a new brand is a table entry, never an engine change. No check
  asks "is this a Samsung?" — it asks the table for the device's integration.
* Observation first; options only where observation cannot tell. App
  enumeration is judged from the published source_list. Art readback health
  is invisible in entity state, so the entry's options are consulted —
  supplied by the caller (the server reads them over HA's diagnostics REST
  endpoint); this module does no I/O and benches offline.
* Three verdicts per check: ok=True (proven), ok=False (proven missing, with
  a `fix`), ok=None (cannot tell — an off panel or unreadable entry is
  UNKNOWN, never failed; matrix #14 taught that lesson for source_list).
* A device is never faulted for a capability it doesn't claim: a non-Frame
  has no art checks, a compatible display gets no samsungtv facts, a music
  room audits nothing (its preparation surface is the network provider's,
  covered by netevidence.inspect).
"""
from __future__ import annotations

import re

# Mirrors appctl._INPUT_RE deliberately (one vocabulary for "input-shaped").
_INPUT_RE = re.compile(
    r"^(hdmi|av|input|source|component|composite|video|vga|dvi|scart|usb|pc|rgb|"
    r"cable|antenna|tuner|aux|line|dtv|atv|tv|live tv|screen ?mirroring|airplay)\b",
    re.I)


def _has_apps(source_list) -> bool:
    return any(isinstance(s, str) and s.strip() and not _INPUT_RE.match(s.strip())
               for s in (source_list or []))


# ---------------------------------------------------------------------------
# Certification facts: what a prepared display of each integration looks like.
# `when` gates a fact on entry data (e.g. Frame-only checks). `opt` facts read
# the entry's options (absent == integration default); `obs` facts observe.
# ---------------------------------------------------------------------------
PREPARE_FACTS = {
    "samsungtv_smart": [
        # AUDITED AGAINST THE LIVE BOX, 1 Aug 2026 — twice. First render
        # claimed 8 issues on a freshly repaired house; the corrections:
        #  * ip_control is decided by TOKEN PRESENCE in entry data (the fork's
        #    own rule, switch.py), not by an enable_ip_control option.
        #  * options are compared as strings ("1" vs 1 false-alarmed WOL).
        #  * ABSENT means UNSET, and unset means the integration DEFAULT
        #    applies. The fork's diagnostics export the full persisted
        #    options (entry.as_dict()); the earlier "not visible" theory was
        #    wrong — the misleading view was the options-flow FORM defaults,
        #    not a truncated export. So each `absent` below encodes the
        #    fork's real default: power_on_method defaults to WOL (fine);
        #    ip_control_art_mode defaults to OFF (a real failure on a Frame).
        {"id": "ip_control", "kind": "obs_data", "field": "ip_control_token",
         "label": "IP Control paired",
         "why": "power off, input, Art Mode and local verification all run on "
                "the 1516/1515 channel; the fork enables it on token presence",
         "fix": "In Pro: open the TV's device sheet → Reconnect / Fix setup → IP Control → Pair now, then accept the prompt on the panel"},
        {"id": "art_readback", "kind": "opt", "option": "ip_control_art_mode",
         "want": True, "when": lambda d: bool(d.get("is_frame_tv")),
         "absent": False,
         "absent_note": "unset — the integration default is OFF",
         "label": "Art Mode readback over IP Control",
         "why": "the websocket art client misreports on this panel family; "
                "without ip_control_art_mode the room misreads artwork as "
                "watching (measured: TV Off flipped back in 136 ms)",
         "fix": "In Pro: open the TV's device sheet → Reconnect / Fix setup → IP Control → turn ON Art Mode readback"},
        {"id": "power_on_wol", "kind": "opt", "option": "power_on_method",
         "want": "1", "absent": True,
         "absent_note": "unset — the integration default is WOL",
         "label": "Power-on method is WOL",
         "why": "nothing on a Samsung listens while it is fully off; any other "
                "method adds a doomed attempt before WOL runs",
         "fix": "In Pro: open the TV's device sheet → Configure → set Wake method to WOL Packet"},
        {"id": "ping_port", "kind": "opt", "option": "ping_port", "want": "set",
         "label": "Ping port committed",
         "why": "presence detection needs the discovered open port (9197); "
                "unset falls back to ICMP, blocked in the HA container",
         "fix": "In Pro: open the TV's device sheet → Reconnect / Fix setup → Connection → set the discovered open port"},
        {"id": "art_switch", "kind": "obs_art_switch",
         "when": lambda d: bool(d.get("is_frame_tv")),
         "label": "Art Mode switch entity present",
         "why": "the room's off-state is Art Mode; without the switch the "
                "generated TV Off cannot rest the panel",
         "fix": "In Pro: Systems › Samsung TV › ⋮ → Reload; if still absent, "
                "re-pair with the panel on a live input"},
        {"id": "app_enumeration", "kind": "obs_apps",
         "label": "Built-in apps available",
         "why": "some panels never answer ed.installedApp.get (the 2020 Frame, "
                "measured); without apps the room breaks the 'keep all "
                "built-in TV apps' rule",
         "fix": "In Pro: tap Copy app list on this alarm, tap the alarm to open "
                "the room, open the TV's device sheet → Configure → tick "
                "App & source setup → Submit → Configure Applications → "
                "paste → Save Configuration"},
    ],
}



# ── the Samsung app_list Core hands the installer ────────────────────────────
# For panels that never answer ed.installedApp.get (2020 Frame, measured):
# the documented route is a manually committed app_list. The product SERVES
# it — a Copy button on the alarm, this table as the source (curated 31 Jul
# from the live panel; all 29 kept per START_HERE §5 "keep all built-in TV
# apps" — prune from evidence, not taste). Tizen app ids are platform-wide,
# not per-panel, so one table serves every Samsung.
SAMSUNG_APP_LIST = {
    "Internet": "org.tizen.browser",
    "YouTube": "111299001912",
    "Prime Video": "3201512006785",
    "Disney+": "3201901017640",
    "Netflix": "3201907018807",
    "Stan": "3201606009798",
    "Foxtel": "3201910019449",
    "Apple TV": "3201807016597",
    "Universal Guide": "3201710015067",
    "Google Play Movies": "3201601007250",
    "7plus": "3201803015934",
    "10": "3201704012147",
    "SmartThings": "3201710015016",
    "Kayo Sports": "3201910019354",
    "Gallery": "3201710015037",
    "9Now": "3201607010031",
    "ABC iview": "3201812017479",
    "DocPlay": "3201901017758",
    "HBO Max": "3202301029760",
    "e-Manual": "20192100003",
    "YouTube Kids": "3201611010983",
    "AnimeLab": "3201808016819",
    "Telstra TV Box Office": "11101000407",
    "Garage Movies": "3201904018182",
    "Privacy Choices": "3201909019271",
    "Jillian Michaels Fitness App": "3202002020229",
    "Calm": "3201909019241",
    "Spotify - Music and Podcasts": "3201606009684",
    "Tubi - Free Movies ＆ TV": "3201504001965",
}


# ── source-device facts ──────────────────────────────────────────────────────
# CEC-wake advisory WITHDRAWN same day (Dave, 2 Aug): "we are not advising
# that a feature needs to be disabled — it's a feature. A Shield or any media
# player turning on and starting the TV is no different from using the native
# remote, so it's not an issue and should not be reported as one." The
# correct handling is the external_control JOURNAL line (information in the
# logs), which ships in ctlbridge — nothing in Prepare. The mechanism below
# stays (empty) for future GENUINE source facts; CEC behaviour never returns
# to this table.
SOURCE_FACTS = {}


def _source_checks(record, snap):
    """Manual installer steps for the room's committed SOURCES (ok=None +
    note). Runs for every committed room regardless of display facts."""
    out = []
    for item in ((record or {}).get("sources") or []):
        eid = item.get("entity") if isinstance(item, dict) else item
        if not isinstance(eid, str) or not eid:
            continue
        integ = (((record or {}).get("meta") or {}).get(eid) or {}) \
            .get("integration") or ""
        nm = ((((snap or {}).get(eid) or {}).get("attributes") or {})
              .get("friendly_name")) or eid
        for fact in SOURCE_FACTS.get(integ, []):
            out.append({"id": "%s:%s" % (fact["id"], eid),
                        "label": "%s \u2014 %s" % (nm, fact["label"]),
                        "ok": None, "why": fact["why"],
                        "note": fact["note"]})
    return out


def _check(fact, ok, extra=None):
    out = {"id": fact["id"], "label": fact["label"], "ok": ok,
           "why": fact["why"]}
    if ok is False:
        out["fix"] = fact["fix"]
    if extra:
        out.update(extra)
    return out


def audit_room(record, snap, entry) -> dict:
    """Audit one committed room's display. Pure; benches offline.

    record -- the committed area record (project.load() shape)
    snap   -- {entity_id: {'state':..., 'attributes': {...}}}
    entry  -- {'data': {...}, 'options': {...}} for the display's config
              entry (server reads it via HA diagnostics), or None when
              unreadable — option checks then report unknown, never failure.
    """
    out = {"area": (record or {}).get("name") or "",
           "display": (record or {}).get("display"), "checks": []}
    if not record or not record.get("committed"):
        return out
    disp = record.get("display")
    if not disp:
        out["checks"].extend(_source_checks(record, snap))
        return out                       # music rooms: sources only (if any)

    integ = ((record.get("meta") or {}).get(disp) or {}).get("integration") or ""
    facts = PREPARE_FACTS.get(integ) or []
    if not facts:
        # No display facts claimed => the display is not faulted — but the
        # room's SOURCES still get their manual steps (CEC wake is a class
        # issue; a compatible-tier display does not exempt the Shield).
        out["checks"].extend(_source_checks(record, snap))
        return out

    data = (entry or {}).get("data") or {}
    options = (entry or {}).get("options") or {}
    st = (snap or {}).get(disp) or {}
    attrs = st.get("attributes") or {}

    for fact in facts:
        when = fact.get("when")
        if when and not when(data):
            continue                     # capability not claimed: no check

        if fact["kind"] == "opt":
            if entry is None:
                out["checks"].append(_check(fact, None))
                continue
            val = options.get(fact["option"])
            if val is None and "absent" in fact:
                # The fork's diagnostics don't export this option: unknown,
                # with the reason. Never a failure (confirm, don't assume).
                out["checks"].append(_check(fact, fact["absent"],
                                            {"note": fact.get("absent_note")}))
                continue
            if fact["want"] == "set":
                ok = val not in (None, "", 0)
            else:
                # Options round-trip as strings or natives depending on the
                # flow that wrote them ("1" vs 1 vs True): compare as strings.
                ok = str(val) == str(fact["want"])
            out["checks"].append(_check(fact, bool(ok)))

        elif fact["kind"] == "obs_data":
            if entry is None:
                out["checks"].append(_check(fact, None))
                continue
            out["checks"].append(_check(fact, bool(data.get(fact["field"]))))

        elif fact["kind"] == "obs_art_switch":
            found = any(eid.startswith("switch.") and eid.endswith("_art_mode")
                        for eid in (snap or {}))
            out["checks"].append(_check(fact, found))

        elif fact["kind"] == "obs_apps":
            # A committed app_list satisfies the requirement outright — that
            # IS the documented route for a panel that will not enumerate.
            if options.get("app_list"):
                out["checks"].append(_check(fact, True,
                                            {"via": "committed app_list"}))
                continue
            sl = attrs.get("source_list")
            state = (st.get("state") or "").lower()
            if sl is None or state in ("off", "unavailable", "unknown", ""):
                # matrix #14: an off panel returns nothing — that is UNKNOWN,
                # not failure, or every sleeping TV would be faulted nightly.
                out["checks"].append(_check(fact, None,
                                            {"note": "panel off — read with "
                                                     "the TV on"}))
            else:
                out["checks"].append(_check(fact, _has_apps(sl)))

    out["checks"].extend(_source_checks(record, snap))
    return out
