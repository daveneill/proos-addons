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
UNAVAIL_SECS = 30 * 60
FROZEN_SECS = 10 * 60
INFRA_SECS = 2 * 60      # sustained window before naming dead network gear
_INFRA_TTL = 10 * 60     # registry re-harvest interval for the gear list
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
_PREP_TTL = 30 * 60


def _prep_posture(slug, rec, snapall):
    """Failing prepare checks for one committed room, 30-min cached.
    Fail-open at every layer: no prepare module, no entry fetcher, or an
    audit error mean the LAST known answer (or nothing) — never a fault."""
    if _prepare is None:
        return []
    now = time.time()
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
_infra_cache = {"ts": 0.0, "gear": {}}


def _infra_gear(snapall, now):
    """{tracker_eid: gear display name} — cached; fail-open to last known."""
    if _infra_cache["gear"] and now - _infra_cache["ts"] < _INFRA_TTL:
        return _infra_cache["gear"]
    try:
        from . import netmap as _nm
        _entries, devices, entities = _nm.load_registries(client=CLIENT)
        dev_name = {d.get("id"): (d.get("name_by_user") or d.get("name"))
                    for d in devices}
        has_update = {e.get("device_id") for e in entities
                      if str(e.get("entity_id", "")).startswith("update.")
                      and e.get("device_id")}
        gear = {}
        for e in entities:
            eid = str(e.get("entity_id", ""))
            did = e.get("device_id")
            if (not eid.startswith("device_tracker.") or e.get("disabled_by")
                    or did not in has_update):
                continue
            attrs = ((snapall.get(eid) or {}).get("attributes") or {})
            if attrs.get("source_type") != "router":
                continue
            if "oui" in attrs or "host_name" in attrs:
                continue                      # client fingerprint: not gear
            gear[eid] = dev_name.get(did) or eid
        _infra_cache["ts"] = now
        _infra_cache["gear"] = gear
    except Exception:                                            # noqa: BLE001
        pass                                  # keep last known (fail open)
    return _infra_cache["gear"]


def _ts(rec):
    """Epoch seconds from a snapshot record's last_changed; None if unknown."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(
            str(rec.get("last_changed", "")).replace("Z", "+00:00")).timestamp()
    except Exception:                                            # noqa: BLE001
        return None


def _co_dropped(snapall, gear_eid):
    """Names of network CLIENTS that vanished within _CO_DROP_SECS of the gear
    going down — the evidence that ties the room's dead devices to the switch."""
    t0 = _ts(snapall.get(gear_eid) or {})
    if t0 is None:
        return []
    names = []
    for eid, rec in snapall.items():
        if not eid.startswith("device_tracker.") or eid == gear_eid:
            continue
        attrs = (rec or {}).get("attributes") or {}
        if attrs.get("source_type") != "router":
            continue
        if "oui" not in attrs and "host_name" not in attrs:
            continue                          # other gear handles itself
        if (rec or {}).get("state") != "not_home":
            continue
        t1 = _ts(rec)
        if t1 is not None and abs(t1 - t0) <= _CO_DROP_SECS:
            names.append(attrs.get("name") or attrs.get("friendly_name") or eid)
    return sorted(names)


def _infra_check(now, snapall, seen):
    for eid, name in (_infra_gear(snapall, now) or {}).items():
        if (snapall.get(eid) or {}).get("state") != "not_home":
            continue
        cid = _iid("infra_down", "network", eid)
        seen.add(cid)
        first = _first_bad.setdefault(cid, now)
        if now - first < INFRA_SECS:
            continue                          # a reboot/firmware blip clears
        took = _co_dropped(snapall, eid)
        t0 = _ts(snapall.get(eid) or {})
        mins = int((now - t0) / 60) if t0 else int((now - first) / 60)
        took_txt = ""
        if took:
            shown = ", ".join(took[:6]) + (" …" if len(took) > 6 else "")
            took_txt = (" It took %d device%s off the network with it: %s."
                        % (len(took), "s" if len(took) != 1 else "", shown))
        _ensure(cid, {
            "kind": "infra_down", "room": "Network", "slug": "network",
            "severity": "critical",
            "title": "Network gear offline — %s" % name,
            "cause": ("%s — the network switch/access point itself, not a "
                      "device on it — has been off the network for %d "
                      "minutes.%s Everything wired through it has no network "
                      "path. Check its power, PoE port or uplink cable. A "
                      "deliberate reboot or firmware update clears itself in "
                      "a few minutes." % (name, mins, took_txt)),
            "subject": eid,
            "actions": []})


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
                if se not in committed_eids:
                    committed_eids.append(se)
            if getattr(a, "provisional", False):
                prov_keys.append(a.key)

        # 1 · committed entity unavailable / missing
        for eid in committed_eids:
            st = (snapall.get(eid) or {}).get("state")
            bad = st in (None, "unavailable", "unknown")
            cid = _iid("committed_unavailable", slug, eid)
            if bad:
                seen.add(cid)
                first = _first_bad.setdefault(cid, now)
                if now - first >= UNAVAIL_SECS:
                    _ensure(cid, {
                        "kind": "committed_unavailable", "room": room,
                        "slug": slug, "severity": "critical",
                        "title": "%s — committed device unreachable" % room,
                        "cause": "%s has been %s for %d minutes. If it was "
                                 "re-added to HA it may have a NEW entity id "
                                 "while the room still drives this one."
                                 % (eid, st or "missing",
                                    int((now - first) / 60)),
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
        for se in src_eids:
            st = (snapall.get(se) or {}).get("state")
            if st not in ("unavailable", "unknown", "off", None):
                continue
            w = (witnesses or {}).get(se)
            if not w:
                continue
            rate = 0.0
            for s in (w.get("sensors") or []):
                try:
                    rate += float((snapall.get(s) or {}).get("state") or 0)
                except (TypeError, ValueError):
                    pass
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

        # 5 · witness coverage gap (info) — provider present, source unbound
        if witnesses is not None and len(witnesses) > 0:
            for se in src_eids:
                if se in witnesses:
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

    _sweep_clears(seen)


def _ensure(cid, inc, quiet=False):
    with _lock:
        _load()
        if cid in _open:
            cur = _open[cid]
            cur["last_seen"] = time.time()
            # Keep content FRESH: wording and actions ship with builds, and
            # an open card was keeping its pre-update text (2 Aug). `since`
            # and identity survive; the message tracks the current build.
            for k in ("title", "cause", "actions", "severity", "room",
                      "slug", "subject", "kind"):
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
    """Clear one RESOLVED incident immediately — a definitive fix already
    removed its condition, so there's no reason to wait for the next scan.
    (Uncertain fixes like a reload do NOT call this: recovery isn't guaranteed,
    so their card stays until a scan confirms the device is actually back.)"""
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
        journal.emit(inc.get("slug", "site"), "repair",
                     {"incident": iid, "action": "reload",
                      "entity": act["entity"]})
        return {"ok": True, "did": "reload", "entity": act["entity"]}
    except Exception as e:
        return {"error": "reload failed: %s" % e}
