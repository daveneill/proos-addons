"""
Health & drift monitor — turns the silent failure modes of 28-29 Jul 2026
into plain-language incidents with one-tap repairs.

Every check is evidence-based against COMMITTED facts (immutable ids from the
project record) and the live state snapshot the ctlbridge sweep already
fetched — no extra HA traffic, no engine involvement. Runs throttled off the
sweep (about once a minute).

The failure classes it detects are exactly the ones that burned the
reference home:
  1. committed_unavailable — a committed entity gone/unavailable ≥30 min
     (identity churn: delete/re-add minted new ids, records rot silently).
     This is the ID-based detector for the Cast-reclaimed-id trap that
     poisoned Family/Living. (The old name-keyed `duplicate_names` check was
     RETIRED 5 Aug 2026 — identity keys off ids, never display names — see the
     tombstone at check #2. It was redundant with this one.)
  3. provisional_room     — room says committed but activities still carry
     provisional flags: the commit never fully reached the runtime.
  4. frozen_session       — witness traffic proves the device is alive on the
     network while its integration entity reports dead ≥10 min (the 11-hour
     samsungtv freeze, caught in minutes).
  5. missing_witness      — a certified network-evidence provider exists but
     a committed source has no witness binding (coverage gap, info-level).
"""
import datetime as _dt
import hashlib
import json
import os
import threading
import time
import re

from . import journal

try:
    from . import sessmon as _sessmon
except Exception:                                            # noqa: BLE001
    _sessmon = None

# Session-stability memory + heal wiring (set by server at boot):
# AUTO_HEAL mirrors the add-on's auto_heal option; CLIENT is the HA client
# used ONLY for the cooled-down reload heal.
_sess: dict = {}
AUTO_HEAL = False
CLIENT = None
NET_CLIENT = None            # set by server: certified UniFi Network client (optional)

try:
    from . import prepare as _prepare
except Exception:                                                # noqa: BLE001
    _prepare = None

STATE_PATH = "/data/health_state.json"
# STAGE 3 BUILD 1 (16 Aug 2026): the 30-minute blindfold comes off. HA
# announces `unavailable` within seconds — its most native fact — and this
# constant made its only committed-device consumer wait HALF AN HOUR before
# saying anything (the whole-product audit's "day's failure in miniature";
# Claims Matrix row #1, RED since 9 Aug, now benched). 180 s is PATIENCE:
# long enough to ride out an HA restart's transients, seconds-scale like
# everything else here. The card states its own window, so the debounce
# lives on the glass, not hidden in a constant. Benched by
# committed_unavailable_bench.py — a 30-minute regression goes red.
UNAVAIL_SECS = 3 * 60
FROZEN_SECS = 10 * 60
INFRA_SECS = 2 * 60      # sustained window before naming dead network gear
_CO_DROP_SECS = 5 * 60   # clients that vanished within this window of the gear

# ── preparation posture (2 Aug 2026) ────────────────────────────────────────
# The Family Room Frame's unpaired IP control sat visible in Pro's Prepare
# TAB while the Health page said "All Systems Normal" — the audit was pull,
# not push, and Dave found out from overnight behaviour instead of from Pro
# ("why was this not flagged as an issue in Pro?"). A committed room whose
# display FAILS its preparation audit is now an OPEN WARNING on the Health
# page. The audit is cached per room (below) so the sweep never gains a
# per-pass HA call; the cache also keeps incidents alive between refreshes
# so they clear only when the audit really passes.
prepare_entry_fn = None      # set by server: display entity -> {data,options}


def _ip_from_sensor(eid):
    """Pull a dotted IPv4 out of a reachability sensor's object id, e.g.
    binary_sensor.192_168_1_110 -> '192.168.1.110'. None if it isn't IP-named."""
    if not eid:
        return None
    obj = str(eid).split(".", 1)[-1]
    m = re.search(r"(\d{1,3})_(\d{1,3})_(\d{1,3})_(\d{1,3})", obj)
    return ".".join(m.groups()) if m else None
_prep_cache = {}             # slug -> {"ts": float, "fails": [(id,label,fix)]}
_prep_avail = {}             # slug -> was the display readable last pass?
_PREP_TTL = 30 * 60


def _prep_posture(slug, rec, snapall):
    """Failing prepare checks for one committed room, 30-min cached.
    Fail-open at every layer: no prepare module, no entry fetcher, or an
    audit error mean the LAST known answer (or nothing) — never a fault.

    STAGE 5 BUILD 5 (16 Aug 2026, Dave's ruling: report within minutes):
    a display COMING BACK from unavailable is exactly what a factory
    reset/reboot looks like from the outside — so that comeback, and only
    that, drops this room's cache and the next sweep re-audits at once. A
    wiped setting cards within ~one scan instead of hiding inside the
    30-minute cache. A display that never blinks keeps the cache (the
    no-extra-traffic contract); the outage itself does not re-audit — an
    off panel reads unknowns, the comeback is when truth is readable
    (frame_reset_reaudit_bench.py)."""
    if _prepare is None:
        return []
    now = time.time()
    _dst = ((snapall or {}).get((rec or {}).get("display")) or {}).get("state")
    _avail = _dst not in (None, "", "unavailable", "unknown")
    _prev = _prep_avail.get(slug)
    _prep_avail[slug] = _avail
    if _prev is False and _avail:
        _prep_cache.pop(slug, None)          # the comeback: re-audit NOW
    c = _prep_cache.get(slug)
    if c and now - c["ts"] < _PREP_TTL:
        return c["fails"]
    fails = (c or {}).get("fails") or []
    try:
        disp = (rec or {}).get("display")
        entry = prepare_entry_fn(disp) if (prepare_entry_fn and disp) else None
        audit = _prepare.audit_room(rec, snapall, entry)
        fails = [(k["id"], k.get("label") or k["id"], k.get("fix") or "")
                 for k in (audit.get("checks") or []) if k.get("ok") is False]
    except Exception:                                            # noqa: BLE001
        pass
    _prep_cache[slug] = {"ts": now, "fails": fails}
    return fails

_lock = threading.Lock()
_first_bad = {}        # check-id -> first-seen-bad ts (pending, not yet open)
_open = {}             # incident-id -> incident dict
_loaded = False

# ── FAULT CLASSES (Dave's ruling, 16 Aug 2026) ──────────────────────────────
# *"I think we are going to have classes of faults, as this would be something
# that would flag in Pro but not on the Dashboard for the homeowner."*
#
# He is right, and today it is only accidentally true: the dashboard happens
# not to fetch /incidents, so nothing reaches a homeowner — by omission, not by
# decision. An accident is not a contract. One new card wired to the wrong
# endpoint and the homeowner is reading "UniFi Network has stopped reporting".
#
# So every incident now DECLARES who it is for:
#
#   installer  Pro only. Commissioning posture, integration plumbing, witness
#              coverage, credentials, provider sign-ins — the trade's work. A
#              homeowner can do nothing with these and should never see them.
#   home       may ALSO reach the homeowner's dashboard: the plain, physical
#              facts a resident can act on or genuinely needs to know.
#
# THE DEFAULT IS `installer`, and it is deliberately the closed one. A new
# check that forgets to declare an audience stays inside Pro; reaching the
# homeowner has to be an act, never an oversight. That is the same law as the
# rest of this file — a claim must be EARNED — pointed at the audience instead
# of the evidence.
AUD_INSTALLER = "installer"
AUD_HOME = "home"


def for_audience(incs, audience):
    """The incidents a given surface may show. Pro passes 'installer' and sees
    everything (the installer is entitled to the homeowner's view as well —
    the mirror rule: he must read exactly what the homeowner reads). The
    dashboard passes 'home' and sees only what was explicitly released."""
    if audience == AUD_INSTALLER:
        return list(incs or [])
    return [i for i in (incs or [])
            if i.get("audience", AUD_INSTALLER) == AUD_HOME]


def _iid(kind, room, subject):
    h = hashlib.sha1(("%s|%s|%s" % (kind, room, subject)).encode()).hexdigest()[:10]
    return "hm-%s" % h


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        _first_bad.update(d.get("first_bad") or {})
        _open.update(d.get("open") or {})
    except Exception:
        pass


def _save():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"first_bad": _first_bad, "open": _open}, f)
    except Exception:
        pass


def incidents():
    with _lock:
        _load()
        return sorted(_open.values(), key=lambda i: i.get("since", 0), reverse=True)


def get(iid):
    with _lock:
        _load()
        return _open.get(iid)


def dismiss(iid):
    """Manual clear (Dave, 2 Aug): the installer closes an incident by hand.
    Honest by construction — nothing is suppressed: if the condition still
    exists, the very next scan re-detects and re-opens it. Journaled, so the
    dismissal itself is on the record."""
    with _lock:
        _load()
        inc = _open.pop(iid, None)
        if inc is None:
            return None
        _first_bad.pop(iid, None)
        _save()
    journal.emit(inc.get("slug", "site"), "incident_dismissed",
                 {"id": iid, "kind": inc.get("kind"),
                  "title": inc.get("title")})
    journal.broadcast("incidents", {"count": len(_open)})
    return inc


# ── 6 · infrastructure down: the network gear ITSELF (Dave, 9 Aug 2026) ─────
# THE SECOND SWITCH TEST: Dave pulled the Bedroom switch again. Every client
# witness worked (TV, Shield, Apple TV trackers all went not_home) — but the
# Apple TV hopped to WiFi, so the room was never "ALL devices gone" and
# room_offline correctly stayed quiet. Meanwhile HA held the DIRECT evidence
# the whole time: device_tracker.bedroom_tv — the UniFi entry for the
# "Bedroom - TV" switch itself — read not_home, and the product never looked.
# Dave: "we have integration with Unifi network... my unifi shows the switch
# is dead including all ports but all I can see in Pro is shield offline."
# RULE: infrastructure is a first-class witness. Gear down is ITS OWN finding,
# named as the switch/AP it is, listing the clients it took with it.
#
# GEAR DETECTION (evidence, no name tokens): a router-sourced device_tracker
# whose attributes carry NO client fingerprint (clients have oui/host_name;
# gear does not) AND whose HA device also owns an update.* entity (the
# network integration publishes firmware updates for its own gear, never for
# clients). Verified live 9 Aug against all 9 UniFi switches/APs/gateway and
# dozens of clients. Integration-agnostic: any network integration that
# models its gear this way is picked up, none is named.
# STAGE 3 BUILD 4 (16 Aug 2026): the controller states what its gear IS and
# whether it is online. Injected by the server: () -> {mac: {"name", "model",
# "type", "online"}} from the controller's own device list (the same 60 s
# cache the port readings ride), or None when no controller can be asked.
# This DELETED the `_infra_gear` fingerprint — "router tracker, no oui, owns
# an update entity" — which guessed what gear looks like and misclassified a
# registry-merged NAS as infrastructure and invisible gear as furniture.
# Carries no claim: PLUMBING.
GEAR_FN = None

# Gear currently OFF the network, by display name — the live answer, rewritten
# every scan. The watcher reads this (A-8, Dave's switch test 16 Aug): while a
# switch or access point is down, ProOS may no longer assume a panel that has
# left the network is merely resting.
_gear_down: list = []


def gear_down():
    """Names of network gear ProOS currently believes is offline. Empty when
    the network is healthy, when nothing has been harvested yet, or when the
    registry could not be read — every one of which means 'I have no evidence
    the network is broken', which is the safe answer for the caller."""
    return list(_gear_down)


def _ts(rec):
    """Epoch seconds from a snapshot record's last_changed; None if unknown."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(
            str(rec.get("last_changed", "")).replace("Z", "+00:00")).timestamp()
    except Exception:                                            # noqa: BLE001
        return None


# ── A-9 · CONFIRM, DON'T ASSUME — APPLIED TO OUR OWN CAUSATION CLAIM ────────
# Dave, 16 Aug 2026, reading the infra card during the switch test:
#
#   "This incident also advises devices that this data switch took offline and
#    not all are correct. It also contradicts our rule of confirm don't assume
#    — it's assuming these are offline, not confirming, when it actually can."
#
# He is right on both counts. The old `_co_dropped` matched on TIME ALONE: any
# router-sourced client that went `not_home` within five minutes of the gear.
# That is correlation wearing causation's clothes, and on his box it named
# "Gra's PC - Wireless", "LG TV - Gra's Place" and "Watch Watch" as casualties
# of a switch in his bedroom. **A wireless client cannot be taken down by a
# switch it was never plugged into.** Shape 3: a claim with no mechanism.
#
# And it CAN confirm. UniFi's own client table carries `sw_mac`, `sw_port` and
# `is_wired` per client — ProOS already reads it for PoE and VLAN evidence.
# That is the controller stating which switch a device is physically behind:
# an observation, not an inference.
#
# THE ONE WRINKLE, AND WHY THIS IS A CACHE. When a switch dies its clients
# leave the ACTIVE client table, so asking at the moment of the outage returns
# nothing. So ProOS records the topology continuously while the network is
# healthy, and the claim it makes is the honest one: *when these were last
# seen, the controller had them behind this switch.*
#
# WITHOUT the certified UniFi client configured, ProOS cannot confirm anything
# here — and then it NAMES NOTHING. A shorter card that is true beats a longer
# one that is invented.
_topo = {"ts": 0.0, "sw_by_mac": {}, "wired": {}}
_TOPO_TTL = 5 * 60


def _learn_topology():
    """Refresh {client_mac: sw_mac} + {client_mac: is_wired} from the
    controller while the network is healthy. Silent and best-effort: no
    controller, no credentials, or any failure simply leaves the last good map
    (and an empty map means 'I cannot confirm', never 'nothing is connected')."""
    now = time.time()
    if now - _topo["ts"] < _TOPO_TTL:
        return
    if NET_CLIENT is None:
        return
    try:
        if not NET_CLIENT.configured():
            return
        rows = NET_CLIENT.clients() or []
    except Exception:                                            # noqa: BLE001
        return                       # keep the last good map; claim nothing new
    sw, wired = {}, {}
    for c in rows:
        mac = str(c.get("mac") or "").lower()
        if not mac:
            continue
        if c.get("sw_mac"):
            sw[mac] = str(c["sw_mac"]).lower()
        wired[mac] = bool(c.get("is_wired"))
    if sw or wired:
        _topo.update({"ts": now, "sw_by_mac": sw, "wired": wired})


def _downstream_of(snapall, gmac, t0=None):
    """(names, confirmed). Clients the CONTROLLER placed behind this gear that
    are now off the network. Keyed on the GEAR'S OWN MAC (Stage 3 build 4 —
    the controller's device list is the identity now, not a tracker entity);
    `t0` is when ProOS first saw the gear offline.

    `confirmed` is False when ProOS has no topology to judge by — no certified
    UniFi client, no credentials, or nothing learned yet. In that case the
    caller must claim nothing: absence of topology is not evidence of absence
    of devices, and guessing from timing is exactly what this replaced."""
    _learn_topology()
    gmac = str(gmac or "").lower()
    sw_by_mac = _topo["sw_by_mac"]
    if not gmac or not sw_by_mac:
        return [], False
    names = []
    for eid, rec in snapall.items():
        if not eid.startswith("device_tracker."):
            continue
        attrs = (rec or {}).get("attributes") or {}
        if attrs.get("source_type") != "router":
            continue
        if (rec or {}).get("state") != "not_home":
            continue
        mac = str(attrs.get("mac") or "").lower()
        if not mac or sw_by_mac.get(mac) != gmac:
            continue                 # the controller never put it behind this gear
        if _topo["wired"].get(mac) is False:
            continue                 # a wireless client is not a switch's casualty
        # Timing is still required, but it is now a FILTER on confirmed
        # membership rather than the evidence itself: a device behind this
        # switch that went offline last Tuesday is not this outage.
        t1 = _ts(rec)
        if t0 is None or (t1 is not None and abs(t1 - t0) <= _CO_DROP_SECS):
            names.append(attrs.get("name") or attrs.get("friendly_name") or eid)
    return sorted(names), True


def _infra_check(now, snapall, seen):
    """STAGE 3 BUILD 4 (16 Aug 2026): the gear question is ANSWERED, not
    guessed. GEAR_FN serves the controller's own device list — what its gear
    IS and whether each piece is online, first-hand from the thing the cables
    plug into. The tracker fingerprint that stood here, and its not_home
    inference on top, are DELETED. `online` None is UNKNOWN and claims
    nothing. With no controller to ask, this check reports nothing at all —
    consciously uncovered (register 181), never guessed: the fingerprint's
    misfires (a NAS accused as gear; invisible gear never flagged) were the
    exact class Dave's law forbids."""
    global _gear_down
    _down = []
    fn = globals().get("GEAR_FN")
    gear = None
    if fn:
        try:
            gear = fn()
        except Exception:                                        # noqa: BLE001
            gear = None
    if not gear:
        _gear_down = []
        return
    for mac, g in sorted(gear.items()):
        if g.get("online") is not False:
            continue              # online, or UNKNOWN — neither is a claim
        name = g.get("name") or mac
        # A-8: recorded the moment it is SEEN down, before the sustain gate.
        # The watcher's own debounce (45s for a panel) is the patience here —
        # adding INFRA_SECS on top would leave the TV reading "normal" for two
        # minutes after ProOS already knew the switch feeding it was dead,
        # which is the contradiction Dave photographed.
        _down.append(name)
        cid = _iid("infra_down", "network", "gear_" + mac)
        seen.add(cid)
        first = _first_bad.setdefault(cid, now)
        if now - first < INFRA_SECS:
            continue                          # a reboot/firmware blip clears
        took, confirmed = _downstream_of(snapall, mac, first)
        mins = int((now - first) / 60)
        # A-9: the sentence now matches the strength of the evidence behind it.
        # Confirmed membership is stated as fact and says who confirmed it; no
        # topology means ProOS says it cannot tell, and names nobody. It never
        # again lists devices that merely dropped at a similar moment.
        took_txt = ""
        if not confirmed:
            took_txt = (" ProOS cannot say which devices were behind it: that "
                        "needs the network controller's own client list, which "
                        "is not available here. Nothing is listed rather than "
                        "guessed from timing.")
        elif took:
            shown = ", ".join(took[:6]) + (" …" if len(took) > 6 else "")
            took_txt = (" The controller had %d wired device%s behind it, and "
                        "%s now off the network: %s."
                        % (len(took), "s" if len(took) != 1 else "",
                           "they are" if len(took) != 1 else "it is", shown))
        else:
            took_txt = (" No device the controller placed behind it has gone "
                        "off the network, so nothing else appears affected.")
        _ensure(cid, {
            "kind": "infra_down", "room": "Network", "slug": "network",
            "severity": "critical",
            "title": "Network gear offline — %s" % name,
            "cause": ("%s — the network switch/access point itself, not a "
                      "device on it — has been offline to the network "
                      "controller for %d minutes.%s Everything wired through "
                      "it has no network path. Check its power, PoE port or "
                      "uplink cable. A deliberate reboot or firmware update "
                      "clears itself in a few minutes." % (name, mins, took_txt)),
            "subject": "gear_" + mac,
            "actions": []})
    _gear_down = _down


# ── COMMITTED-MEMBER AREA DRIFT (Stage 5 build 3, 16 Aug 2026) ──────────────
# The membership question has two answers BY DESIGN, both Dave's rulings:
# membership.area_of (5 Aug) answers "what room is this entity in" for
# monitoring and room views; the committed record (4 Aug, the quarantine
# retirement) is the only truth about what a committed room's ACTIVITIES
# are built from. What was missing was the RECONCILER: when a committed
# member's HA area drifts away from its committed room, nothing noticed —
# activities kept firing while every room view filed the device somewhere
# else. The literal mechanism of "we go back in and it's no longer
# assigned", silent for weeks at a time.
#
# This check is a READING plus a REPORT, never a rewrite. The retired
# auto-janitor stays retired (tenet 12 — ProOS never fights HA): the card
# names the device, its committed room and where HA now places it, and the
# repair belongs to the installer in Pro. Benched red-first by
# membership_drift_bench.py.
DRIFT_SECS = 2 * 60     # an installer mid-move never sees a flap


def membership_drift_check(now, seen, project_mod):
    if project_mod is None or CLIENT is None:
        return                                # blind is not broken
    try:
        from . import netmap, membership
        proj = project_mod.load()
        committed = {k: r for k, r in ((proj or {}).get("areas") or {}).items()
                     if r.get("committed")}
        if not committed:
            return
        _, devices, entities = netmap.load_registries(client=CLIENT)
        # (An unreadable/empty registry says nothing by construction: every
        # member misses its row below and is skipped — benched. No separate
        # guard, because mutation testing proved one here could never fail.)
        dev_area = {d.get("id"): d.get("area_id") for d in devices}
        ent_by_id = {e.get("entity_id"): e for e in entities}
        try:
            area_names = netmap.load_areas(client=CLIENT) or {}
        except Exception:                                        # noqa: BLE001
            area_names = {}
    except Exception:                                            # noqa: BLE001
        return
    for key, rec in sorted(committed.items()):
        room_area = rec.get("area_id") or key
        room_name = rec.get("name") or area_names.get(room_area) or room_area
        for eid in project_mod._area_members(rec):
            row = ent_by_id.get(eid)
            if row is None:
                continue      # a vanished entity is a different fault
            cur = membership.area_of(row, dev_area)
            if cur == room_area:
                continue
            cid = _iid("membership_drift", room_area, eid)
            seen.add(cid)
            first = _first_bad.setdefault(cid, now)
            if now - first < DRIFT_SECS:
                continue
            name = row.get("name") or row.get("original_name") or eid
            where = ("in %s" % area_names.get(cur, cur)) if cur else "in no room"
            _ensure(cid, {
                "kind": "membership_drift", "room": room_name,
                "slug": room_area, "severity": "warning",
                "title": "%s has drifted out of %s" % (name, room_name),
                "cause": ("%s is part of %s's committed setup, but Home "
                          "Assistant now files it %s. The room's activities "
                          "still run exactly as committed; room views and "
                          "monitoring group by the HA area, so this device "
                          "shows in the wrong place. ProOS never moves it "
                          "back on its own — re-assign it in Pro (Rooms), or "
                          "re-commit the room if the device genuinely moved."
                          % (name, room_name, where)),
                "subject": eid,
                "actions": []})


# ── 7 · COMMAND-TIME failures: integrations that refuse, not just die ───────
# (Dave, 9 Aug 2026, after the 4-day invisible "Admin access required" that
# killed every music play: "needs to monitor all our integrations — if they
# stop working, identify and fix themselves.") State-watching (checks 1-6)
# sees integrations that DIE; this channel sees integrations that REFUSE —
# a command fails at call time while every entity looks healthy. Dashboards
# report failures (POST /journal/note, type command_failed); each becomes an
# OPEN incident immediately, honest about its class:
#   AUTH-class (admin/permission/unauthorized in the error): a reload can
#   NEVER fix it — the incident says exactly that and points at the account/
#   token, and auto-heal is FORBIDDEN (healing what cannot heal is noise).
#   Other classes: with auto_heal on, ONE cooled-down integration reload
#   (the same safe tier + shared sessmon cooldown as every other heal).
# Incidents live CMD_TTL beyond the last failure, then age out via the sweep.
CMD_TTL = 30 * 60
_cmd_open = {}     # cid -> last-failure ts (keeps the sweep from clearing)


def _auth_class(err):
    e = str(err or "").lower()
    return any(k in e for k in ("admin", "permission", "unauthorized",
                                "auth", "forbidden", "401", "403"))


# ── the PROVIDER-AUTH class (register 125 → 126, 13 Aug 2026) ───────────────
# Spotify's streaming credential died server-side: the engine's Web-API half
# still logged in (browse, search and the queue all worked) while librespot
# refused EVERY track with INVALID_CREDENTIALS — so play surfaced as the
# engine's "No playable item found to start playback", and this channel
# blamed the PLAYER ("an integration fault, not a network one") and offered
# a reload that cannot resurrect a revoked credential. The class is
# INFORMATION, not a Spotify scenario: any provider whose streaming half
# loses auth shows the same split, and the provider is NAMED from the
# engine's own log line, never hardcoded. Both halves of evidence are
# required — the command's "no playable item" refusal (the queue FILLED,
# so browsing works; nothing would stream) AND a recent provider-tagged
# auth refusal in the engine's log (read via the same MUSIC_LOG hook as
# check #8; blind is not evidence, so no log means no classification).
# Branded provider tags are Capitalised ([music_assistant.Spotify]); the
# engine's own modules are lowercase python paths ([music_assistant.
# player_queues]) — the tag itself tells the layers apart.
_NOPLAY_RE = re.compile(r"no playable item", re.I)
_PROV_TAG_RE = re.compile(r"\[music_assistant\.([A-Z][A-Za-z0-9 _-]*)\]")
_ANY_TAG_RE = re.compile(r"\[music_assistant\.([A-Za-z0-9 _-]+)\]")
_AUTH_MARK_RE = re.compile(
    r"invalid[_ ]credentials|unauthori[sz]ed|not authori[sz]ed|"
    r"token expired|login failed|credential", re.I)
# The engine's LATEST word about a service wins: a sign-in it logs AFTER the
# refusal means the credential was replaced, so the fault is over. Live shape
# (13 Aug): "[music_assistant.spotify] Successfully logged in to Spotify as
# Dave" — note the LOWERCASE tag on the success and the Capitalised tag on the
# stream refusal, both about the same service. Tags are therefore matched
# case-insensitively, while a service is only ever NAMED from a Capitalised
# tag (so `[music_assistant.player_queues]` can never become a service).
_AUTH_OK_RE = re.compile(
    r"successfully logged in|login successful|successfully authenticated|"
    r"token refreshed", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PROVAUTH_WINDOW = 15 * 60    # evidence must be current, like WEDGE_WINDOW


def _at_words(ts, newest):
    """The refusal's own clock time in words — "11:05 today", or "11:05 on
    12 Aug" when it is not from the log's latest day. Read from the LOG's
    stamps, never from time.time(): the same rule as _wedge_age."""
    if ts is None:
        return None
    if newest is not None and ts.date() == newest.date():
        return "%02d:%02d today" % (ts.hour, ts.minute)
    return "%02d:%02d on %d %s" % (ts.hour, ts.minute, ts.day,
                                   ts.strftime("%b"))


def provider_stream_faults(text):
    """Every music service the ENGINE'S OWN LOG says is refusing to STREAM:
    {"Spotify": {"ts", "line", "at", "behind"}}. THE one detector — the
    command-time classifier below, the standing Health card (check #9) and
    the Music Services row all read this, so the product cannot hold two
    opinions about one service (register 127).

    Evidence, and nothing else: the service is NAMED from the log's own
    branded tag (never hardcoded — Tidal, Qobuz or anything else that loses
    its streaming half is read the same way), the time is the log's own
    stamp, and a service whose newest word from the engine is a successful
    sign-in is not reported at all. Blind is not evidence: no text, no
    claim."""
    newest = None
    hits, oks = {}, {}
    for raw in str(text or "").splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        ts = _logstamp(line)
        if ts is not None and (newest is None or ts > newest):
            newest = ts
        m = _ANY_TAG_RE.search(line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        if _AUTH_OK_RE.search(line):
            # A sign-in with no readable stamp cannot be proved to be the
            # LATER word, so it never clears a timestamped refusal.
            if ts is not None and (key not in oks or ts >= oks[key]):
                oks[key] = ts
            continue
        if not _AUTH_MARK_RE.search(line):
            continue
        brand = _PROV_TAG_RE.search(line)
        if not brand:
            continue          # the engine's own lowercase modules: plumbing
        cur = hits.get(key)
        if cur is None or cur["ts"] is None or ts is None or ts >= cur["ts"]:
            # The evidence is quoted on the card, so a cut line SAYS it was
            # cut — the reason lives at the end of a librespot line and a
            # silent truncation would drop it (bench A1).
            hits[key] = {"name": brand.group(1).strip(), "ts": ts,
                         "line": (line if len(line) <= 300
                                  else line[:300] + " …")}
    out = {}
    for key, h in hits.items():
        ok = oks.get(key)
        if ok is not None and h["ts"] is not None and ok > h["ts"]:
            continue          # the engine's latest word is a good sign-in
        behind = (None if (h["ts"] is None or newest is None)
                  else (newest - h["ts"]).total_seconds())
        out[h["name"]] = {"ts": h["ts"], "line": h["line"], "behind": behind,
                          "at": _at_words(h["ts"], newest)}
    return out


def provider_fault_words(prov, at=None):
    """The ONE set of words for a service whose streaming half is refused —
    spoken by the Health card and by the Music Services row alike, so the two
    surfaces cannot say different things about one fault.

    `fix` is for a reader somewhere else (the Health card names the whole
    path); `fix_here` is for a reader already standing on the row, where
    Configure and Remove are the two buttons in front of them. Both are
    honest about what Configure can and cannot do: it renders the MUSIC
    ENGINE'S OWN setup form, so a sign-in step exists only where the service
    offers one — and MA masks a stored secret on read, so a save that is
    refused leaves remove-and-add as the fix that always works."""
    when = (" — last at %s" % at) if at else ""
    return {
        "headline": "Can browse but not play — the streaming sign-in "
                    "was revoked",
        # the headline as a full sentence, for a surface that does not carry
        # the service's name beside it (a Health card, not a row)
        "lead": "%s can browse but not play — its streaming sign-in was "
                "revoked by the service." % prov,
        "note": "%s's library, search and queues still answer, so nothing "
                "else looks wrong; the engine was refused on every track it "
                "tried to stream%s." % (prov, when),
        "detail": "This is not the speaker and not the network. Only a fresh "
                  "sign-in restores playback — restarting the music engine "
                  "or reloading the player cannot, because the credential "
                  "was revoked at the service's end.",
        "fix": "Systems → ProOS Music → Music Services → %s → Configure, "
               "then sign in again and Save. Configure opens the music "
               "engine's own setup form, so the sign-in step is there only "
               "where the service offers one — if it is missing, or the save "
               "is refused, Remove %s and add it back: that wipes the dead "
               "credential and signs in fresh." % (prov, prov),
        "fix_here": "Tap Configure and sign in again, then Save. Configure "
                    "opens the music engine's own setup form, so the sign-in "
                    "step is there only where the service offers one — if it "
                    "is missing, or the save is refused, Remove %s and add "
                    "it back: that wipes the dead credential and signs in "
                    "fresh." % prov,
    }


def _provider_auth(error):
    """The provider whose streaming auth is refusing THIS command, or None.
    Both halves of evidence are still required — the engine's own "no
    playable item" refusal (which is itself the tell that browsing works:
    the queue filled, nothing would stream) AND a CURRENT provider-tagged
    auth refusal in its log. The evidence comes from the one detector above;
    what belongs here is only the command-time gate and the staleness window.
    Timestamps compare log-to-log (the box's own clock), never
    log-to-time.time() — the same rule as _wedge_age."""
    if not _NOPLAY_RE.search(str(error or "")):
        return None
    if MUSIC_LOG is None:
        return None
    try:
        faults = provider_stream_faults(MUSIC_LOG())
    except Exception:                                            # noqa: BLE001
        return None
    best = None
    for name, f in faults.items():
        if (best is None or f["ts"] is None
                or (best[1]["ts"] is not None and f["ts"] >= best[1]["ts"])):
            best = (name, f)
    if best is None:
        return None
    behind = best[1]["behind"]
    if behind is not None and behind >= PROVAUTH_WINDOW:
        return None                    # stale: not THIS failure's evidence
    return best[0]


def note_command_failure(entity, domain, service, error, room="site"):
    # Called by the server when a dashboard reports a failed command.
    now = time.time()
    cid = _iid("command_failed", room, "%s|%s.%s" % (entity, domain, service))
    _cmd_open[cid] = now
    auth = _auth_class(error)
    prov = None if auth else _provider_auth(error)
    n = sum(1 for t in _cmd_open.values() if now - t < CMD_TTL)
    cause = ("%s refused '%s.%s': %s. " % (entity, domain, service,
             str(error)[:180]))
    if auth:
        cause += ("This is an ACCESS failure — reloading cannot fix it. The "
                  "integration's account/token lacks the right permission; "
                  "fix the credential (for ProOS Music: the server's "
                  "homeassistant_system user must be Admin), then retry.")
    elif prov:
        # The wrong layer got named once (register 125): this is NOT the
        # player's fault, and a reload cannot address it — so none is
        # offered for this class (behaviour change, residual in the
        # register: the honest fix is the provider's sign-in). The words
        # are the SHARED ones (register 127) so this card and the Music
        # Services row cannot describe one fault two ways.
        _w = provider_fault_words(prov)
        cause += ("That is not the player and not the network: %s The queue "
                  "filled (browsing and search still work) and %s refused to "
                  "stream every item, which is what 'No playable item' "
                  "means. %s Fix: %s"
                  % (_w["lead"], prov, _w["detail"], _w["fix"]))
    else:
        cause += ("The device looked healthy and refused anyway — an "
                  "integration fault, not a network one.")
    _ensure(cid, {
        "kind": "provider_auth" if prov else "command_failed",
        "room": room, "slug": room,
        "severity": "critical" if (auth or prov or n >= 3) else "warning",
        "title": ("%s — %s can browse but not play" % (room, prov)) if prov
                 else "%s — a command was refused" % room,
        "cause": cause, "subject": entity,
        "actions": ([] if (auth or prov)
                    else [{"kind": "reload", "entity": entity,
                           "label": "Reload integration"}])})
    # identify AND fix themselves — but only the classes a reload can fix,
    # under the same guardrails as every other heal (auto_heal + cooldown).
    # provider_auth never heals: a reload cannot resurrect a revoked
    # credential (healing what cannot heal is noise — same law as auth).
    if (not auth and not prov and AUTO_HEAL and CLIENT is not None
            and _sessmon is not None
            and _sessmon.heal_due(_sess, entity, now)):
        try:
            CLIENT._req("POST",
                        "/api/services/homeassistant/reload_config_entry",
                        {"entity_id": entity})
            journal.emit(room, "auto_heal", {
                "action": "reload_integration", "entity": entity,
                "reason": "command_failed"})
            print("  [healthmon] auto-heal: reloaded %s after refused command"
                  % entity, flush=True)
        except Exception as _e:                                  # noqa: BLE001
            print("  [healthmon] command-fail heal failed for %s: %s"
                  % (entity, _e), flush=True)


# ── 8 · the music engine WEDGES (Dave, 9 Aug 2026) ──────────────────────────
# "I just had it start after nearly 5 mins… pause doesn't even work." A
# playback task hung on a dead Apple Music stream and never released the
# Office Sonos's player lock. The engine's own guard gives up after 30s and
# proceeds anyway — so nothing crashed, nothing errored, and EVERY command
# (play, pause, stop) simply cost 30 seconds. Seven in a row before a hand
# restart cleared it. ProOS never noticed: the speaker was reachable, the
# add-on was "started", every witness said healthy.
#
# This is the frozen-session class (Claims Matrix row 4) turned on our own
# music engine: provably alive, provably not working. The evidence is the
# engine's own warning in the add-on log; the repair is a restart.
#
# Both hooks are injected by the server at boot (same idiom as CLIENT /
# prepare_entry_fn) so this module keeps its no-extra-traffic contract and
# stays importable — and testable — with no Supervisor at all.
MUSIC_LOG = None         # -> str: recent ProOS Music add-on log text
MUSIC_RESTART = None     # -> restarts the ProOS Music add-on
WEDGE_WINDOW = 15 * 60   # how recent a stuck-command warning must be to count
_WEDGE_RE = re.compile(r"acquiring playback lock", re.I)
_LOGTS_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)")


def _logstamp(line):
    """The datetime on a log line, or None. Uses `datetime`, never the `time`
    module: log stamps are the BOX's local clock and time.time() is epoch UTC
    — comparing the two would be a timezone bug waiting for a DST change."""
    m = _LOGTS_RE.search(line or "")
    if not m:
        return None
    try:
        return _dt.datetime(*(int(g) for g in m.groups()))
    except Exception:                                            # noqa: BLE001
        return None


def _wedge_age(text):
    """Seconds between the engine's LAST stuck-command warning and the newest
    line in the same log — both on the same clock, so no timezone maths. None
    when there is no warning (or nothing parsable) — which is not a fault."""
    newest = last_warn = None
    for line in str(text or "").splitlines():
        ts = _logstamp(line)
        if ts is None:
            continue
        if newest is None or ts > newest:
            newest = ts
        if _WEDGE_RE.search(line) and (last_warn is None or ts > last_warn):
            last_warn = ts
    if last_warn is None or newest is None:
        return None
    return (newest - last_warn).total_seconds()


def music_wedge_check(now, seen, _first_ts=None, text=None):
    """Open/keep/clear the wedged-engine incident. `_first_ts` overrides the
    parsed evidence age (the bench pins staleness without faking clocks).
    `text` lets the sweep hand over the log it has ALREADY read, so checks #8
    and #9 share one fetch instead of pulling the add-on log twice a minute."""
    cid = _iid("music_wedged", "site", "proos_music")
    if text is None:
        if MUSIC_LOG is None:
            return
        try:
            text = MUSIC_LOG()
        except Exception:                                        # noqa: BLE001
            return                   # blind is not broken — say nothing
    try:
        age = _wedge_age(text)
    except Exception:                                            # noqa: BLE001
        return
    if _first_ts is not None:
        age = (now - _first_ts) if age is not None else None
    if age is None or age >= WEDGE_WINDOW:
        return                       # nothing live: the sweep clears it
    seen.add(cid)
    _ensure(cid, {
        "kind": "music_wedged", "room": "site", "slug": "site",
        "severity": "warning",
        # THE ONE CARD RELEASED TO THE HOMEOWNER (Dave, 16 Aug). It earns it on
        # all three tests the others fail: the symptom is one a resident is
        # experiencing RIGHT NOW (they pressed play and waited 30 seconds), the
        # words carry no trade language, and the repair is a single button they
        # can press themselves. Every other check on this page is commissioning
        # posture, integration plumbing or witness coverage — a homeowner can
        # do nothing with those, so they stay in Pro.
        "audience": AUD_HOME,
        "title": "Music is not responding",
        "cause": "ProOS Music has stopped responding to commands. A track "
                 "that failed mid-stream left the speaker's playback stuck, "
                 "so every play, pause and stop is taking about 30 seconds "
                 "before anything happens. The speakers are fine and the "
                 "network is fine — the music service needs restarting.",
        "subject": "proos_music",
        "actions": [{"kind": "restart_music",
                     "label": "Restart ProOS Music"}]})
    # Identify AND fix themselves — under Dave's existing guardrail. With
    # auto_heal OFF the card and its button are the whole answer: named
    # immediately, repaired in one tap, never restarted behind his back.
    if (AUTO_HEAL and MUSIC_RESTART is not None and _sessmon is not None
            and _sessmon.heal_due(_sess, "proos_music", now)):
        try:
            MUSIC_RESTART()
            journal.emit("site", "auto_heal", {
                "action": "restart_music", "entity": "proos_music",
                "reason": "music_wedged"})
            print("  [healthmon] auto-heal: restarted ProOS Music (wedged)",
                  flush=True)
        except Exception as _e:                                  # noqa: BLE001
            print("  [healthmon] music restart failed: %s" % _e, flush=True)


# ── 9 · a music service that can browse but not PLAY (Dave, 13 Aug 2026) ────
# "Just says reload sent and still nothing even if I go into Music services
# there is no errors !!!!" Register 125 recorded this residual and it was
# flagged-not-built: a revoked STREAMING credential leaves the Web-API half
# answering, so MA reports no `last_error`, Pro's Music Services row looked
# perfectly healthy, and the only surface that could ever name the fault was
# a Health card someone had to press PLAY to earn.
#
# The evidence was there the whole time — the engine's own log — and the
# sweep already reads it once a minute for check #8. So this costs no extra
# traffic at all: the same text, read through the same detector, opens the
# card without anybody trying to play anything.
#
# ONE ANSWER PER SERVICE. When a play command has already opened a
# provider_auth card naming this service, THAT card is the answer — it knows
# the room the installer just used. This check then stands down (and by
# leaving its own id out of `seen`, lets the sweep clear a standing card the
# command has superseded). Two answers to one question is the defect this
# codebase names as the cause of most of its bugs.
def provider_stream_check(now, seen, text):
    """Open/keep the standing 'can browse but not play' card per service,
    straight from the engine's log. `text` is the log the sweep already
    read — None means blind, and blind says nothing."""
    if text is None:
        return
    try:
        faults = provider_stream_faults(text)
    except Exception:                                            # noqa: BLE001
        return
    if not faults:
        return
    others = [i for i in incidents() if i.get("kind") == "provider_auth"]
    for prov, f in faults.items():
        cid = _iid("provider_auth", "site", prov)
        if any(i.get("id") != cid and prov in str(i.get("title") or "")
               for i in others):
            continue             # the command's own card already says it
        w = provider_fault_words(prov, f.get("at"))
        seen.add(cid)
        _ensure(cid, {
            "kind": "provider_auth", "room": "site", "slug": "site",
            "severity": "critical",
            "title": "%s can browse but not play" % prov,
            "cause": "%s %s %s Fix: %s The music engine's own words: \"%s\"."
                     % (w["lead"], w["note"], w["detail"], w["fix"],
                        f.get("line") or ""),
            "subject": prov,
            # No repair button: the fix is a sign-in only the installer
            # holds, and healing what cannot heal is noise (the same law
            # as the AUTH class, register 126).
            "actions": []})


# ── 11 · THE WITNESS LAYER GOES QUIET (A-6 · Dave's ruling, 16 Aug 2026) ────
# Read on Dave's own box, 16 Aug: **all 111 device trackers come from ONE UniFi
# config entry**, and ProOS deliberately prefers that controller's view over a
# TCP probe for every watched device ("the controller's presence beats a blind
# probe"). One config entry is therefore the single point of failure for the
# entire second-signal layer.
#
# Until today, `unavailable` on a tracker was read as "the device is gone". So a
# UniFi reboot, a firmware update or a failed reauth would have raised a red
# OFFLINE fault on every television in the house at the same instant, each one
# telling the installer to go and check a power cable that was never unplugged.
# The van roll the product exists to prevent, caused by the product.
#
# `watcher._REACH_MUTE` fixed the misreading. This check supplies the other
# half of Dave's ruling — **name the real fault**. Forty devices losing their
# witness in the same sweep is not forty faults. It is one.
#
# THE EVIDENCE, AND ONLY THE EVIDENCE:
#   * the witnesses must have been ASKED and said nothing (watcher publishes
#     the mute list; an ip-probe timeout is a fact about one device and is
#     deliberately excluded)
#   * there must be MANY of them — one mute tracker is one device's problem
#   * they must share ONE integration, resolved from the SENSOR's config entry
#     through the registry. No name tokens, no brand strings: a blackout is
#     only ever named after the entry that actually owns the entities.
#   * and it must SUSTAIN past INFRA_SECS, so a controller reboot clears itself
#     exactly like a switch reboot does.
# Blind is not broken: no registry, no naming, no card.
BLACKOUT_MIN = 3          # fewer than this is not a fleet, it is a device
BLACKOUT_SHARE = 0.5      # ...and it must be at least half of what is bound
WATCHERS = None           # -> watcher.report(); injected by server at boot


def _integration_of(sensor_eids):
    """(entry_title, domain, n) for the ONE config entry that owns these
    entities, or None. None when the registry can't be read, when they are
    split across entries, or when the entry can't be identified — every one of
    those is 'I cannot name it', and an unnamed blackout is not reported."""
    if CLIENT is None:
        return None
    try:
        from . import netmap as _nm
        entries, _devices, entities = _nm.load_registries(client=CLIENT)
    except Exception:                                            # noqa: BLE001
        return None
    ent_entry, dev_entry = {}, {}
    for e in entities:
        eid = e.get("entity_id")
        if not eid:
            continue
        if e.get("config_entry_id"):
            ent_entry[eid] = e["config_entry_id"]
        elif e.get("device_id"):
            dev_entry[eid] = e["device_id"]
    ids = {ent_entry.get(s) for s in sensor_eids}
    ids.discard(None)
    if len(ids) != 1:
        return None                  # split across integrations, or unknown
    entry_id = ids.pop()
    for en in entries:
        if en.get("entry_id") == entry_id:
            return (en.get("title") or en.get("domain") or "the network "
                    "integration", en.get("domain") or "", len(sensor_eids))
    return None


def witness_blackout_check(now, seen, report=None):
    """Open/keep the ONE card for a witness layer that has gone dark."""
    if report is None:
        if WATCHERS is None:
            return
        try:
            report = WATCHERS() or {}
        except Exception:                                        # noqa: BLE001
            return                   # blind is not broken — say nothing
    mute = report.get("witness_mute") or []
    bound = int(report.get("witness_bound") or 0)
    if len(mute) < BLACKOUT_MIN or not bound:
        return
    if len(mute) < bound * BLACKOUT_SHARE:
        return                       # a few quiet witnesses, not a blackout
    who = _integration_of([m.get("sensor") for m in mute if m.get("sensor")])
    if not who:
        return                       # cannot NAME it -> does not claim it
    title, _domain, _n = who
    cid = _iid("witness_blackout", "network", title)
    seen.add(cid)
    first = _first_bad.setdefault(cid, now)
    if now - first < INFRA_SECS:
        return                       # a controller reboot clears itself
    names = _name_list([m.get("name") or m.get("entity") for m in mute][:8])
    _ensure(cid, {
        "kind": "witness_blackout", "room": "Network", "slug": "network",
        "severity": "critical",
        # INSTALLER CLASS, explicitly. Nothing here is a homeowner's problem —
        # every device in the house is still working; what stopped is ProOS's
        # ability to independently CHECK them.
        "audience": AUD_INSTALLER,
        "title": "%s has stopped reporting — %d devices lost their second "
                 "signal" % (title, len(mute)),
        "cause": "%d of the %d watched devices with a second signal lost it in "
                 "the same sweep, and every one of those signals comes from %s. "
                 "That is ONE integration that has stopped answering, not %d "
                 "faulty devices — the devices themselves are almost certainly "
                 "fine, and ProOS is NOT raising a fault against any of them. "
                 "What it has lost is its independent way to CHECK them: until "
                 "%s answers again, those devices are watched on their own "
                 "integration's word alone. Affected: %s. Check the controller "
                 "is up and that its sign-in has not expired — a reboot or a "
                 "firmware update clears itself within a few minutes."
                 % (len(mute), bound, title, len(mute), title, names),
        "subject": title,
        "actions": [{"kind": "reload", "entity": (mute[0].get("sensor") or ""),
                     "label": "Reload %s" % title}]})


# ── scan ────────────────────────────────────────────────────────────────────
def scan(snapall, project_mod, get_controller, witnesses=None):
    """One pass over every committed room. Mutates the incident set; journals
    and broadcasts opens/clears. Never raises."""
    try:
        _scan(snapall, project_mod, get_controller, witnesses or {})
    except Exception as e:
        print("  [healthmon] scan failed: %s" % e, flush=True)


def _scan(snapall, project_mod, get_controller, witnesses):
    now = time.time()
    seen = set()       # condition-ids observed bad THIS pass
    _uni_clients = None   # certified UniFi Network clients, fetched at most once

    # 6 · infrastructure first: a dead switch/AP explains everything below it.
    _infra_check(now, snapall, seen)

    # 12 · committed members whose HA area drifted from their committed room
    #      (Stage 5 build 3) — report, never rewrite.
    try:
        membership_drift_check(now, seen, project_mod)
    except Exception as _e:                                      # noqa: BLE001
        print("  [healthmon] membership drift check failed: %s" % _e,
              flush=True)

    # 11 · and the witness layer going dark explains everything below THAT:
    #      one integration that stopped answering is not forty dead devices.
    try:
        witness_blackout_check(now, seen)
    except Exception as _e:                                      # noqa: BLE001
        print("  [healthmon] witness blackout check failed: %s" % _e,
              flush=True)

    # The engine's log, read ONCE for checks #8 and #9 (no extra traffic).
    _mlog = None
    if MUSIC_LOG is not None:
        try:
            _mlog = MUSIC_LOG()
        except Exception:                                        # noqa: BLE001
            _mlog = None             # blind is not broken — say nothing

    # 8 · a wedged music engine: alive, "started", and answering nothing
    try:
        music_wedge_check(now, seen, text=_mlog)
    except Exception as _e:                                      # noqa: BLE001
        print("  [healthmon] music wedge check failed: %s" % _e, flush=True)

    # 9 · a music service whose STREAMING half is refused — visible without
    #     waiting for someone to press play (register 127)
    try:
        provider_stream_check(now, seen, _mlog)
    except Exception as _e:                                      # noqa: BLE001
        print("  [healthmon] provider stream check failed: %s" % _e,
              flush=True)

    # 7 · command-time failures stay open CMD_TTL past the last refusal
    for _cid, _ts in list(_cmd_open.items()):
        if now - _ts < CMD_TTL:
            seen.add(_cid)
        else:
            _cmd_open.pop(_cid, None)

    # A WITNESS THAT CANNOT TESTIFY IS NOT A WITNESS (register 146). Judge the
    # bindings against the live snapshot ONCE, here, so every check below asks
    # "who can actually testify" rather than "who is listed". Sourcehood is
    # judged after the loop, when every committed source is known.
    _wreal, _all_src = dict(witnesses or {}), set()
    try:
        from . import netevidence as _netev
        _wreal = _netev.classify(witnesses or {}, snapall.keys())["real"]
    except Exception:                                            # noqa: BLE001
        pass                         # blind is not broken: keep the raw map

    proj = project_mod.load() or {}
    for key, rec in (proj.get("areas") or {}).items():
        if not (rec and rec.get("committed")):
            continue
        room = rec.get("name") or key
        slug = rec.get("area_id") or key
        # Preparation posture BEFORE the controller fetch: a room too broken
        # to build a controller is exactly a room whose posture must show.
        try:
            for _fid, _flabel, _ffix in _prep_posture(slug, rec, snapall):
                cid = _iid("prepare", slug, _fid)
                seen.add(cid)
                _ensure(cid, {
                    "kind": "prepare", "room": room, "slug": slug,
                    "severity": "warning",
                    "title": "%s — display not fully prepared: %s"
                             % (room, _flabel),
                    "cause": ((_ffix + ". ") if _ffix else "")
                             + "Found by the preparation audit (Pro › room › "
                               "Prepare). Control may work, but awareness "
                               "will not be reliable until this is done.",
                    "subject": rec.get("display"),
                    # A card whose only option is a chat button is a
                    # diagnosis withheld (Dave, 2 Aug): the incident links
                    # STRAIGHT to where the fix lives.
                    "actions": ([{"kind": "room", "room": slug,
                                  "label": "Open %s" % room}]
                                + ([{"kind": "apply_settings", "room": slug,
                                     "label": "Apply recommended settings"}]
                                   if _fid in ("art_readback", "power_on_wol")
                                   else [])),
                })
        except Exception:                                        # noqa: BLE001
            pass
        try:
            ctrl = get_controller(room)
            acts = ctrl.activities or {}
        except Exception:
            continue

        # RECORD FAULT (4 Aug 2026) — the room is committed but its record
        # can't be built, so the controller refused to guess from discovery.
        # This is its OWN incident: "recommit" was the advice for the old
        # silent-fallback symptom and it could never work, because the
        # phantom came from a device the record doesn't contain.
        _rf = getattr(ctrl, "_record_fault", None)
        if _rf:
            cid = _iid("record_fault", slug, "rec")
            seen.add(cid)
            _ensure(cid, {
                "kind": "record_fault", "room": room, "slug": slug,
                "severity": "fault",
                "title": "%s — the room's record can't be read" % room,
                "cause": "%s is %s. Its devices and roles are still in Home "
                         "Assistant, so ProOS can rebuild the record from "
                         "them — or open the room and commit it again."
                         % (room, _rf),
                "subject": "rec",
                "actions": [{"kind": "heal_record", "room": slug,
                             "label": "Rebuild record"},
                            {"kind": "recommit", "room": slug,
                             "label": "Open %s" % room}]})
            continue

        committed_eids, prov_keys, src_eids = [], [], []
        for a in acts.values():
            for t in (getattr(a, "targets", None) or []):
                eid = getattr(t, "entity_id", None)
                if eid and eid not in committed_eids:
                    committed_eids.append(eid)
            se = getattr(a, "source_eid", None)
            if se:
                src_eids.append(se)
                _all_src.add(se)
                if se not in committed_eids:
                    committed_eids.append(se)
            if getattr(a, "provisional", False):
                prov_keys.append(a.key)

        # 1 · committed entity unavailable / missing
        for eid in committed_eids:
            st = (snapall.get(eid) or {}).get("state")
            bad = _dead_state(st)   # shared with verify_after_reload
            cid = _iid("committed_unavailable", slug, eid)
            if bad:
                seen.add(cid)
                first = _first_bad.setdefault(cid, now)
                if now - first >= UNAVAIL_SECS:
                    _ensure(cid, {
                        "kind": "committed_unavailable", "room": room,
                        "slug": slug, "severity": "critical",
                        "title": "%s — committed device unreachable" % room,
                        "cause": "%s has been %s for %s, confirmed over a "
                                 "%d-second window. If it was re-added to HA "
                                 "it may have a NEW entity id while the room "
                                 "still drives this one."
                                 % (eid, st or "missing",
                                    ("%d minutes" % int((now - first) / 60)
                                     if (now - first) >= 120 else
                                     "%d seconds" % int(now - first)),
                                    int(UNAVAIL_SECS)),
                        "subject": eid,
                        "actions": [
                            {"kind": "reload", "entity": eid,
                             "label": "Reload integration"},
                            {"kind": "recommit", "room": slug,
                             "label": "Recommit room"}]})

        # 1b · reporting link unstable (sessmon, 2 Aug): a device whose
        # session flaps is a device whose truth cannot be trusted — an
        # incident with a count, a timeline and a mechanical repair. The
        # Family Room Apple TV collapsed idle->off in 22 seconds of
        # hands-off; nobody should discover that by debugging a missing
        # card. Auto-heal (integration reload) fires ONLY when the add-on's
        # auto_heal option is on, cooled down per entity.
        if _sessmon is not None:
            for eid in committed_eids:
                _st2 = (snapall.get(eid) or {}).get("state")
                _sessmon.observe(_sess, eid, _st2, now)
                cid = _iid("link_unstable", slug, eid)
                if _sessmon.unstable(_sess, eid, now):
                    seen.add(cid)
                    _n = _sessmon.drop_count(_sess, eid, now)
                    _cause = ("%s dropped its reporting link %d times in "
                              "the last %d minutes. Its on/off and "
                              "now-playing info cannot be trusted until "
                              "the link holds. Reloading the integration "
                              "usually restores it; if it keeps dropping, "
                              "check the network path between the box and "
                              "this device (mDNS/multicast on separated "
                              "VLANs is the usual culprit)."
                              % (eid, _n, int(_sessmon.WINDOW_S / 60)))
                    # cure #2 (5 Aug) — OPTIONAL UniFi VLAN evidence. Runs only
                    # when the certified UniFi Network integration is configured
                    # AND this source has a commissioned reachability witness
                    # (binary_sensor.<ip>). Nothing depends on UniFi: no UniFi /
                    # no witness / not isolated adds nothing; failures swallowed.
                    try:
                        if NET_CLIENT is not None and NET_CLIENT.configured():
                            _rs = None
                            for _a in acts.values():
                                if getattr(_a, "source_eid", None) == eid:
                                    _rs = getattr(_a, "reachability_sensor",
                                                  None)
                                    break
                            _ip = _ip_from_sensor(_rs)
                            if _ip:
                                if _uni_clients is None:
                                    _uni_clients = NET_CLIENT.clients() or []
                                from . import unifinet as _uni
                                _iso = _uni.vlan_isolation(_uni_clients, ip=_ip)
                                if _iso:
                                    _cause += (
                                        " Evidence (UniFi): this device is on "
                                        "network '%s' (VLAN %s) while the home "
                                        "is on '%s' (VLAN %s) — mDNS/Bonjour "
                                        "does not cross VLANs, so enable "
                                        "multicast/mDNS reflection between them "
                                        "or move it to the main network."
                                        % (_iso["network"], _iso["vlan"],
                                           _iso["main_network"],
                                           _iso["main_vlan"]))
                    except Exception:                            # noqa: BLE001
                        pass
                    _ensure(cid, {
                        "kind": "link_unstable", "room": room,
                        "slug": slug, "severity": "warning",
                        "title": "%s — device reporting link unstable" % room,
                        "cause": _cause,
                        "subject": eid,
                        "actions": [{"kind": "reload", "entity": eid,
                                     "label": "Reload integration"}]})
                    if (AUTO_HEAL and CLIENT is not None
                            and _sessmon.heal_due(_sess, eid, now)):
                        try:
                            CLIENT._req(
                                "POST",
                                "/api/services/homeassistant/"
                                "reload_config_entry",
                                {"entity_id": eid})
                            journal.emit(slug, "auto_heal", {
                                "action": "reload_integration",
                                "entity": eid, "drops": _n})
                            print("  [healthmon] auto-heal: reloaded "
                                  "integration for %s (%d drops)"
                                  % (eid, _n), flush=True)
                        except Exception as _e:              # noqa: BLE001
                            print("  [healthmon] auto-heal reload failed "
                                  "for %s: %s" % (eid, _e), flush=True)

        # 2 · duplicate_names — RETIRED 5 Aug 2026 (Dave's ruling: identity keys
        #     off immutable ids, never display names). This grouped a room's
        #     media players by their display name and warned on a collision — a
        #     name-keyed proxy for the re-added-integration trap. Check #1
        #     (committed_unavailable) already catches that trap by the honest,
        #     id-based signal: the committed id goes unavailable (its cause text
        #     names the "NEW entity id" case). Keying on the display name only
        #     added false positives — two live devices that legitimately share a
        #     name — and broke on rename / remove-and-re-add. One mechanism per
        #     question: #1 owns this.

        # 3 · provisional flags on a committed room
        if prov_keys:
            cid = _iid("provisional_room", slug, "prov")
            seen.add(cid)
            _ensure(cid, {
                "kind": "provisional_room", "room": room, "slug": slug,
                "severity": "warning",
                "title": "%s — commit didn't fully apply" % room,
                "cause": "Activities still provisional: %s. The runtime is "
                         "guessing where it should be reading committed facts."
                         % ", ".join(sorted(prov_keys)),
                "subject": "prov",
                "actions": [{"kind": "recommit", "room": slug,
                             "label": "Recommit room"}]})

        # 4 · frozen integration session: network says alive, entity says dead
        # — and (Stage 3 build 3) the SPLIT: an answered "off" is a live
        # session, so off+traffic is a readings-disagree note, never a
        # frozen-session claim and never an automatic reload.
        for se in src_eids:
            st = (snapall.get(se) or {}).get("state")
            _link_dead = _frozen_state(st)   # shared with verify_after_reload
            if not _link_dead and st != "off":
                continue
            w = _wreal.get(se)
            if not w:
                continue
            rate = 0.0
            for s in (w.get("sensors") or []):
                try:
                    rate += float((snapall.get(s) or {}).get("state") or 0)
                except (TypeError, ValueError):
                    pass
            if rate >= float(w.get("min", 0.25)) and not _link_dead:
                # "off" + traffic: two honest readings that disagree. Either
                # a background update in standby, or a session lost mid-use
                # reporting a phantom off (the 2 Aug Apple TV case). The
                # network alone cannot tell them apart, and the card says so
                # instead of choosing. Reload is a BUTTON, never automatic —
                # the integration just answered, so nothing here may restart
                # it behind the installer's back.
                cid = _iid("off_with_traffic", slug, se)
                seen.add(cid)
                first = _first_bad.setdefault(cid, now)
                if now - first >= FROZEN_SECS:
                    _ensure(cid, {
                        "kind": "off_with_traffic", "room": room,
                        "slug": slug, "severity": "warning",
                        "title": "%s — readings disagree" % room,
                        "cause": "%s reports off, but its network witness "
                                 "shows %.2f MB/s of traffic (witness "
                                 "threshold %.2f). That is either a "
                                 "background update in standby, or a session "
                                 "lost mid-use reporting a phantom off — "
                                 "ProOS cannot tell the two apart from the "
                                 "network alone. If the room was in use, "
                                 "reload the integration."
                                 % (se, rate, float(w.get("min", 0.25))),
                        "subject": se,
                        "actions": [{"kind": "reload", "entity": se,
                                     "label": "Reload integration"}]})
                continue
            if rate >= float(w.get("min", 0.25)):
                cid = _iid("frozen_session", slug, se)
                seen.add(cid)
                first = _first_bad.setdefault(cid, now)
                if now - first >= FROZEN_SECS:
                    _ensure(cid, {
                        "kind": "frozen_session", "room": room, "slug": slug,
                        "severity": "critical",
                        "title": "%s — integration session frozen" % room,
                        "cause": "%s reports %s but its network witness shows "
                                 "%.2f MB/s of traffic. The device is alive; "
                                 "the integration's session is dead."
                                 % (se, st or "missing", rate),
                        "subject": se,
                        "actions": [{"kind": "reload", "entity": se,
                                     "label": "Reload integration"}]})
                    # Auto-heal (7 Aug): a frozen session is the SUSTAINED twin
                    # of link_unstable — the entity is stuck dead while its
                    # witness proves real playback (the Bedroom Apple TV case,
                    # where a single wedge never reaches the flap threshold).
                    # Reload the integration when auto_heal is on, sharing
                    # sessmon's per-entity reload cooldown so the two detectors
                    # can never storm one device between them.
                    if (AUTO_HEAL and CLIENT is not None and _sessmon is not None
                            and _sessmon.heal_due(_sess, se, now)):
                        try:
                            CLIENT._req(
                                "POST",
                                "/api/services/homeassistant/"
                                "reload_config_entry",
                                {"entity_id": se})
                            journal.emit(slug, "auto_heal", {
                                "action": "reload_integration",
                                "entity": se, "reason": "frozen_session",
                                "rate": round(rate, 2)})
                            print("  [healthmon] auto-heal: reloaded frozen "
                                  "session for %s (%.2f MB/s)"
                                  % (se, rate), flush=True)
                        except Exception as _e:              # noqa: BLE001
                            print("  [healthmon] auto-heal frozen reload "
                                  "failed for %s: %s" % (se, _e), flush=True)

        # 5 · witness coverage gap (info) — a REAL witness exists somewhere in
        #     the home, and this source is not one of them. Keyed on the
        #     bindings that can testify (register 146): when nothing can, the
        #     home has one honest fault, not one nag per source.
        if len(_wreal) > 0:
            for se in src_eids:
                if se in _wreal:
                    continue
                cid = _iid("missing_witness", slug, se)
                seen.add(cid)
                _ensure(cid, {
                    "kind": "missing_witness", "room": room, "slug": slug,
                    "severity": "info",
                    "title": "%s — source has no network witness" % room,
                    "cause": "%s has no traffic witness bound. When its "
                             "integration lies, ProOS has one less way to "
                             "catch it." % se,
                    "subject": se,
                    "actions": [{"kind": "witness", "room": slug,
                                 "label": "Bind witness"}]},
                    quiet=True)

    # 10 · bindings that cannot testify — the SILENT half (register 146)
    #
    # ONE DEFINITION OF "SOURCE" (register 150). This used to judge a binding
    # against the ACTIVITY set — the source_eids the controller happens to be
    # running. That set is a runtime derivative: the controller CLEARS
    # activities (an AVR-routed room clears its per-source ones), so a device
    # the installer commissioned as a source could be missing from it. Meanwhile
    # netevidence binds from the RECORD. Two lists, one question, disagreeing —
    # so ProOS bound seven witnesses and then accused itself of binding them
    # wrongly. The record is the commission (entities labelled proos_source);
    # the record wins, and both sides now read it from the same function.
    _src_truth = _all_src
    try:
        from . import netevidence as _netev2
        _src_truth = {s["entity"] for s in
                      _netev2._committed_sources(project_mod)} or _all_src
    except Exception:                                            # noqa: BLE001
        pass                       # unreadable record: fall back, accuse less
    _witness_integrity(seen, snapall, witnesses or {}, _src_truth)

    _sweep_clears(seen)


def _friendly(snapall, eid):
    """A device's own name, never its entity id. Rule 13 (register 123): every
    surface speaks the home's words — and an entity id on this card is exactly
    what made the 14 Aug incident unreadable."""
    nm = (((snapall.get(eid) or {}).get("attributes") or {})
          .get("friendly_name") or "").strip()
    return nm or "a device ProOS can no longer see"


def _name_list(names):
    names = sorted(set(names))
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return "%s and %s" % (names[0], names[1])
    return "%s and %s" % (", ".join(names[:-1]), names[-1])


def _witness_integrity(seen, snapall, witnesses, src_eids):
    """One card for every traffic-witness binding that cannot testify.

    Dave, 14 Aug 2026, reading three "no network witness" cards after a factory
    reset: "I thought we sorted this with the shield showing separate google
    cast." The three cards were honest. The fault was the three rooms that said
    NOTHING — bound to sensors that had ceased to exist, and counted as covered
    the whole time. A failure must be as earned as a success (doctrine 6), and
    a PASS must be earned just as hard.
    """
    if not witnesses:
        return
    try:
        from . import netevidence as _netev
        broken = _netev.classify(witnesses, snapall.keys(), src_eids)["broken"]
    except Exception:                                            # noqa: BLE001
        return                       # cannot judge: accuse nothing
    if not broken:
        return
    gone = [_friendly(snapall, s) for s, r in broken.items()
            if "sensors_missing" in r["reasons"] or "no_sensors" in r["reasons"]]
    wrong = [_friendly(snapall, s) for s, r in broken.items()
             if "not_a_source" in r["reasons"]]
    parts = []
    if gone:
        parts.append("%s: the network traffic sensors they were bound to no "
                     "longer exist." % _name_list(gone))
    if wrong:
        parts.append("%s: bound to a device that is not a source in any "
                     "committed room, so it can never testify."
                     % _name_list(wrong))
    cid = _iid("witness_broken", "site", "witness")
    seen.add(cid)
    _ensure(cid, {
        "kind": "witness_broken", "room": "site", "slug": "site",
        "severity": "warning",
        "title": "Traffic witnesses are not working",
        # No instruction to a control that does not exist (register 147): the
        # card states the fault and stops. ProOS turns the provider's traffic
        # sensors on ITSELF at commissioning; if this card is showing, that
        # self-repair could not run, and saying "go and do it" would send the
        # installer to a page with no such control.
        # A-1 (audit, 15 Aug). This sentence used to end "...no longer counted
        # as confirmed two ways" — borrowing the LIVENESS phrase for a
        # THROUGHPUT fault, on the very screen whose two contradictory lines
        # started the finding. One phrase cannot do two jobs:
        #   "confirmed two ways" = is the device ALIVE on the network (watcher)
        #   "witnessed"          = is it actually STREAMING (traffic witness)
        # This card is the second one. It says so.
        "cause": " ".join(parts) + " Until they can testify, ProOS has only "
                 "each integration's own word for what those devices are "
                 "doing — so they are no longer counted as witnessed.",
        "subject": "witness",
        # No repair button: the sensors live in another integration's options,
        # and ProOS does not reach into someone else's settings. Naming the
        # fault precisely IS the repair path (the AUTH law, register 126).
        "actions": []})


def _ensure(cid, inc, quiet=False):
    # FAULT CLASS, defaulted CLOSED (Dave, 16 Aug). A check that does not say
    # who its card is for is an installer's card. Reaching the homeowner is an
    # act; it can never be an omission.
    inc.setdefault("audience", AUD_INSTALLER)
    with _lock:
        _load()
        if cid in _open:
            cur = _open[cid]
            cur["last_seen"] = time.time()
            # Keep content FRESH: wording and actions ship with builds, and
            # an open card was keeping its pre-update text (2 Aug). `since`
            # and identity survive; the message tracks the current build.
            for k in ("title", "cause", "actions", "severity", "room",
                      "slug", "subject", "kind", "audience"):
                if k in inc:
                    cur[k] = inc[k]
            _save()
            return
        inc["id"] = cid
        inc["since"] = time.time()
        inc["last_seen"] = inc["since"]
        _open[cid] = inc
        _save()
    if not quiet:
        print("  [healthmon] OPEN %s: %s" % (inc["kind"], inc["title"]), flush=True)
    journal.emit(inc.get("slug", "site"), "incident_open",
                 {"id": cid, "kind": inc["kind"], "title": inc["title"],
                  "severity": inc["severity"], "subject": inc.get("subject")})
    journal.broadcast("incidents", {"count": len(_open)})


def _sweep_clears(seen):
    cleared = []
    with _lock:
        _load()
        for cid in [c for c in _first_bad if c not in seen]:
            _first_bad.pop(cid, None)
        for cid, inc in list(_open.items()):
            if cid not in seen:
                cleared.append(_open.pop(cid))
        if cleared:
            _save()
    for inc in cleared:
        print("  [healthmon] CLEAR %s: %s" % (inc["kind"], inc["title"]), flush=True)
        journal.emit(inc.get("slug", "site"), "incident_cleared",
                     {"id": inc["id"], "kind": inc["kind"], "title": inc["title"]})
    if cleared:
        journal.broadcast("incidents", {"count": len(_open)})


def _clear(iid):
    """Clear one RESOLVED incident immediately — its condition has been
    OBSERVED gone (a definitive fix like a witness bind, or a reload whose
    outcome verify_after_reload confirmed against the incident's own clear
    predicate), so there's no reason to wait for the next scan. An
    unverified fix never calls this: a card clears on observation or not
    at all (register 126 — confirm, don't assume)."""
    with _lock:
        _load()
        inc = _open.pop(iid, None)
        _first_bad.pop(iid, None)
        if inc:
            _save()
    if inc:
        journal.emit(inc.get("slug", "site"), "incident_cleared",
                     {"id": iid, "kind": inc.get("kind"),
                      "title": inc.get("title")})
        journal.broadcast("incidents", {"count": len(_open)})
    return inc


# ── verify after repair (Dave, 13 Aug 2026, register 126) ───────────────────
# "Watch it clear" was an unconfirmed claim: Fix Now sent a reload and the
# glass promised a recovery nobody had observed — the exact opposite of
# confirm-don't-assume. The doctrine is diagnose → consent → fix → VERIFY →
# report, and it binds Pro's buttons exactly as it binds Assist. After the
# reload, the fix path re-reads the incident's OWN clear condition — the SAME
# predicate the sweep clears by (shared below, one mechanism) — with the
# patience t_verify (assist.py) gives slow-reporting domains: an integration
# may only confirm on its next poll, up to ~15s, re-checked every 2s.
VERIFY_WAIT = 15
VERIFY_STEP = 2


def _dead_state(st):
    """committed_unavailable's bad predicate — shared by the sweep and the
    post-reload verifier, so 'Cleared' means exactly what the sweep means."""
    return st in (None, "unavailable", "unknown")


def _frozen_state(st):
    """frozen_session's bad predicate — shared the same way. STAGE 3 BUILD 3
    (16 Aug 2026): 'off' is no longer dead here. An integration that ANSWERS
    "off" has a live session by definition — calling it frozen was false,
    and a standby device pulling an overnight update earned a critical card
    plus an automatic reload of a healthy integration (census H16). An
    answered off with traffic is now the off_with_traffic readings-disagree
    card: both facts stated, nothing auto-fired, nothing claimed that the
    network alone cannot distinguish."""
    return st in (None, "unavailable", "unknown")


def _live_state(client, eid):
    """One entity's live state, or None when it cannot be read — a missing
    entity and an unreachable HA answer the same: nothing confirmable."""
    try:
        return ((client._req("GET", "/api/states/%s" % eid) or {})
                .get("state"))
    except Exception:                                            # noqa: BLE001
        return None


def verify_after_reload(client, inc):
    """{cleared, note}: what the reload ACTUALLY achieved, checked against
    the incident's own clear condition. Clears the card only on observation;
    never claims, never says 'watch it clear'."""
    kind = inc.get("kind")
    eid = inc.get("subject")
    if kind == "command_failed":
        # This class clears by TIME (CMD_TTL without another refusal) — no
        # state read can confirm a refusal is gone. The honest next step:
        return {"cleared": False, "note": (
            "Didn't clear — a reload can't be confirmed from here: the only "
            "proof is the command itself. Try the command again; this card "
            "clears after %d minutes without another refusal."
            % (CMD_TTL // 60))}
    if kind == "link_unstable":
        # Its clear condition is sessmon's drop window — the SAME memory the
        # sweep reads. A reload cannot rewind history; usually only time can.
        if _sessmon is not None and not _sessmon.unstable(_sess, eid,
                                                          time.time()):
            _clear(inc.get("id"))
            return {"cleared": True, "note": (
                "Cleared — the link's drop count is back under the "
                "threshold, the same test the monitor clears by.")}
        return {"cleared": False, "note": (
            "Didn't clear — the reload was sent, but a stable link is only "
            "proven by time: this clears once the link holds for the rest "
            "of its %d-minute window."
            % ((_sessmon.WINDOW_S // 60) if _sessmon is not None else 30))}
    pred = {"committed_unavailable": _dead_state,
            "frozen_session": _frozen_state,
            # off_with_traffic: still "off" after a reload means either a
            # genuine standby (the card stays, honestly) or the reload did
            # not restore the session — verified only by the state changing.
            "off_with_traffic": (lambda st: st == "off")}.get(kind)
    if pred is None:
        return {"cleared": False, "note": (
            "Reload sent. This card has no live state to confirm against "
            "from here — it clears when the next scan finds the condition "
            "gone.")}
    deadline = time.time() + VERIFY_WAIT
    st = _live_state(client, eid)
    while pred(st) and time.time() < deadline:
        time.sleep(VERIFY_STEP)
        st = _live_state(client, eid)
    if not pred(st):
        _clear(inc.get("id"))
        return {"cleared": True, "note": (
            "Cleared — the device is reporting again (its state read back "
            "healthy after the reload, the same test the monitor clears "
            "by)." if kind == "committed_unavailable" else
            "Cleared — the integration session is alive again (its state "
            "read back after the reload, the same test the monitor clears "
            "by).")}
    return {"cleared": False, "note": (
        "Didn't clear — the device is still not reporting %d seconds after "
        "the reload. Reloading didn't bring it back: check its power and "
        "network path, or re-commit the room if it was re-added with a new "
        "id." % VERIFY_WAIT if kind == "committed_unavailable" else
        "Didn't clear — the session still reads dead %d seconds after the "
        "reload while the network witness says the device is alive. Check "
        "the integration's connection to it, or power-cycle the device."
        % VERIFY_WAIT)}


def resolve_action(client, iid, action_kind=None):
    """Execute an incident's mechanical repair. Only 'reload' acts on HA
    (homeassistant.reload_config_entry on the entity). recommit/witness are
    navigation actions the UI handles itself."""
    inc = get(iid)
    if not inc:
        return {"error": "unknown incident"}
    act = None
    for a in inc.get("actions", []):
        if action_kind in (None, a.get("kind")):
            act = a
            break
    if not act:
        return {"error": "no such action"}
    if act["kind"] == "restart_music":
        # The wedged-engine repair (check #8). NOT definitive: the restart is
        # asked for, but "back to normal" is only true when the stuck-command
        # warnings stop — so the card stays until a scan sees that, exactly
        # like the reload path. Never claim recovery we haven't observed.
        if MUSIC_RESTART is None:
            return {"error": "ProOS Music is not managed on this box"}
        try:
            MUSIC_RESTART()
        except Exception as e:                                   # noqa: BLE001
            return {"error": "restart failed: %s" % e}
        journal.emit(inc.get("slug", "site"), "repair",
                     {"incident": iid, "action": "restart_music",
                      "entity": "proos_music"})
        return {"ok": True, "did": "restart_music", "cleared": False,
                "note": "ProOS Music is restarting — it takes about a minute "
                        "to come back."}
    if act["kind"] == "witness":
        # One-tap bind (Stage 9b): commit the suggested traffic sensors for this
        # source — the installer's tap IS the commit (doctrine: name-tokens only
        # at the suggestion boundary, never a runtime match). If no certified
        # rate sensor matches, say so — never invent one.
        try:
            from . import netevidence as _ne
            states = client._req("GET", "/api/states") or []
            ids = [s.get("entity_id", "") for s in states]
            src = inc.get("subject")
            sensors = _ne.suggest_sensors(src, _ne.rate_sensor_ids(ids))
            if not sensors:
                return {"error": "no matching traffic sensor to bind — check the "
                                 "UniFi Network integration is on and "
                                 "allow_bandwidth_sensors is enabled"}
            _ne.save_witness(src, sensors, None)
            journal.emit(inc.get("slug", "site"), "repair",
                         {"incident": iid, "action": "witness_bound",
                          "entity": src, "sensors": sensors})
            # Definitive fix (Dave, 5 Aug): the witness IS bound, so the
            # condition is gone — clear the card NOW, don't make the installer
            # wait for the next sweep to see their tap take effect.
            _clear(iid)
            return {"ok": True, "did": "witness_bound", "cleared": True,
                    "entity": src, "sensors": sensors}
        except Exception as e:                                   # noqa: BLE001
            return {"error": "bind failed: %s" % e}
    if act["kind"] == "apply_settings":
        # One-tap (Stage 11): the certification already knows the right OPTION
        # values, so write them onto the display's integration via HA's options
        # flow, then reload. Definitive — the room's applyable posture incidents
        # clear immediately. Only fixed-value options are applied here; the
        # discovered ping port and physical pairing stay their own steps.
        try:
            from . import prepare as _prep
            disp = inc.get("subject")
            entry_id = (client.resolve_config_entry(disp)
                        if disp and hasattr(client, "resolve_config_entry")
                        else None)
            if not entry_id:
                return {"error": "could not find the display's integration entry"}
            data = ((prepare_entry_fn(disp) if callable(prepare_entry_fn)
                     else None) or {}).get("data") or {}
            want = {"power_on_method": "1"}
            if data.get("is_frame_tv"):
                want["ip_control_art_mode"] = True
            committed = _prep.apply_recommended(client, entry_id, want)
            try:
                client._req("POST",
                            "/api/services/homeassistant/reload_config_entry",
                            {"entity_id": disp})
            except Exception:                                    # noqa: BLE001
                pass
            # Only clear a posture incident whose option ACTUALLY committed.
            # Samsung hides some options ("show advanced options") behind a
            # multi-step form this single-step apply can't reach, so never
            # falsely clear something we didn't really fix (Dave, 5 Aug).
            _applied = {
                "power_on_wol": str(committed.get("power_on_method")) == "1",
                "art_readback": committed.get("ip_control_art_mode") is True,
            }
            done = [f for f, ok in _applied.items()
                    if ok and _clear(_iid("prepare", inc.get("slug"), f))]
            journal.emit(inc.get("slug", "site"), "repair",
                         {"incident": iid, "action": "settings_applied",
                          "entity": disp, "options": committed, "cleared": done})
            if not any(_applied.values()):
                return {"error": "those settings are behind the TV's Advanced "
                                 "options and could not be applied automatically "
                                 "yet — set them in the device sheet for now"}
            return {"ok": True, "did": "settings_applied",
                    "cleared": bool(done), "entity": disp}
        except Exception as e:                                   # noqa: BLE001
            return {"error": "apply failed: %s" % e}
    if act["kind"] != "reload":
        return {"ok": True, "navigate": act}
    try:
        client._req("POST", "/api/services/homeassistant/reload_config_entry",
                    {"entity_id": act["entity"]})
    except Exception as e:
        return {"error": "reload failed: %s" % e}
    # VERIFY, then report (register 126): the reload is only the attempt —
    # the answer is the incident's own clear condition, re-read. The repair
    # journal event records the verdict, not just the try.
    v = verify_after_reload(client, inc)
    journal.emit(inc.get("slug", "site"), "repair",
                 {"incident": iid, "action": "reload",
                  "entity": act["entity"],
                  "verified": bool(v.get("cleared"))})
    return {"ok": True, "did": "reload", "entity": act["entity"],
            "cleared": bool(v.get("cleared")), "note": v.get("note")}
