"""
Health & drift monitor — turns the silent failure modes of 28-29 Jul 2026
into plain-language incidents with one-tap repairs.

Every check is evidence-based against COMMITTED facts (immutable ids from the
project record) and the live state snapshot the ctlbridge sweep already
fetched — no extra HA traffic, no engine involvement. Runs throttled off the
sweep (about once a minute).

The five failure classes it detects are exactly the ones that burned the
reference home:
  1. committed_unavailable — a committed entity gone/unavailable ≥30 min
     (identity churn: delete/re-add minted new ids, records rot silently).
  2. duplicate_names      — two media players in one area sharing a friendly
     name (the Cast-reclaimed-id trap that poisoned Family/Living).
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

from . import journal

try:
    from . import prepare as _prepare
except Exception:                                                # noqa: BLE001
    _prepare = None

STATE_PATH = "/data/health_state.json"
UNAVAIL_SECS = 30 * 60
FROZEN_SECS = 10 * 60

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
                    "actions": [{"kind": "room", "room": slug,
                                 "label": "Open %s" % room}],
                })
        except Exception:                                        # noqa: BLE001
            pass
        try:
            ctrl = get_controller(room)
            acts = ctrl.activities or {}
        except Exception:
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

        # 2 · duplicate friendly names among the room's media players
        names = {}
        for eid in src_eids + committed_eids:
            if not eid.startswith("media_player."):
                continue
            fn = ((snapall.get(eid) or {}).get("attributes") or {}) \
                .get("friendly_name")
            if fn:
                names.setdefault(fn.strip().lower(), set()).add(eid)
        for fn, eids in names.items():
            if len(eids) > 1:
                cid = _iid("duplicate_names", slug, fn)
                seen.add(cid)
                _ensure(cid, {
                    "kind": "duplicate_names", "room": room, "slug": slug,
                    "severity": "warning",
                    "title": "%s — two devices share one name" % room,
                    "cause": "\"%s\" belongs to %s. A re-added integration "
                             "reclaims names; committed sources can then "
                             "point at the wrong id." % (fn, ", ".join(sorted(eids))),
                    "subject": fn,
                    "actions": [{"kind": "recommit", "room": slug,
                                 "label": "Recommit room"}]})

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
            _open[cid]["last_seen"] = time.time()
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
