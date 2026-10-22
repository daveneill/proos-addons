"""ProOS Core — Apple Home accessories for the devices Apple pairs on their own. Register 970p.

Dave, 22 Oct 2026, 17:38: "some devices like TVs and cameras need to be setup as a separate accessory
and when I add a TV say Bedroom tv i turn it on but it never shows to add in Apple Home".

THE PLATFORM'S RULE, read in its own code (homekit/util.py `state_needs_accessory_mode`, homekit/__init__.py
`add_bridge_accessory`): cameras, locks, TV / receiver / projector media players and activity remotes must be
paired in ACCESSORY MODE — one HomeKit entry each, with its own pairing code. A bridge made through the platform's
setup flow carries `exclude_accessory_mode` and silently skips them. Pro put them on its one bridge, so they
never reached Apple Home.

THE PLATFORM'S MACHINERY (18 Sep law): an accessory-mode entry is made with the platform's own setup flow
(user → pairing → created) and turned into an accessory with its own Configure flow (init mode=accessory →
accessory → created). ProOS walks exactly those steps. The one thing those steps cannot do is name the entry
after the device ("HASS Bridge 4F" otherwise reaches the Home app); that is the same store write and one restart
`_homekit_brand` already does for the bridge.

ProOS owns only the entries it made — titled "ProOS · <device>". An accessory entry made in the platform's own
pages is respected (never duplicated, never removed).
"""
from __future__ import annotations

import json
import os
import re
import shutil

PREFIX = "ProOS · "
OWN_MEDIA = ("tv", "receiver", "projector")
REMOTE_ACTIVITY = 4                    # RemoteEntityFeature.ACTIVITY
SEED_DOMAIN = "light"                  # the setup flow needs a domain; light never needs accessory mode, so the
                                       # flow spawns no extra accessory entries (it does for every domain it names)
GROUP_PREFIX = "ProOS Alerts "          # the platform group helper that combines a camera's chosen detections
FLOW = "/api/config/config_entries/flow"
OFLOW = "/api/config/config_entries/options/flow"


def needs_own(entity_id: str, attrs: dict | None) -> bool:
    """The platform's `state_needs_accessory_mode`, mirrored: must this entity be its own accessory?"""
    dom = str(entity_id).split(".", 1)[0]
    at = attrs or {}
    if dom in ("camera", "lock"):
        return True
    if dom == "media_player":
        return at.get("device_class") in OWN_MEDIA
    if dom == "remote":
        try:
            return bool(int(at.get("supported_features") or 0) & REMOTE_ACTIVITY)
        except (TypeError, ValueError):
            return False
    return False


def _mode_entity(e: dict):
    """(mode, entity) of a HomeKit entry. Options win over data, as in the platform's own setup."""
    o, d = e.get("options") or {}, e.get("data") or {}
    mode = o.get("mode") or d.get("mode") or "bridge"
    flt = o.get("filter") or d.get("filter") or {}
    inc = flt.get("include_entities") or []
    return mode, (inc[0] if mode == "accessory" and len(inc) == 1 else None)


def accessories(entries: list) -> dict:
    """{entity_id: {entry_id, title, ours, name}} for every accessory-mode HomeKit entry."""
    out = {}
    for e in entries or []:
        if e.get("domain") not in (None, "homekit"):
            continue
        mode, eid = _mode_entity(e)
        if mode != "accessory" or not eid:
            continue
        title = str(e.get("title") or "")
        ec = ((e.get("options") or {}).get("entity_config") or (e.get("data") or {}).get("entity_config") or {})
        out[eid] = {"entry_id": e.get("entry_id"), "title": title, "ours": title.startswith(PREFIX),
                    "name": (e.get("data") or {}).get("name"),
                    "linked_motion_sensor": (ec.get(eid) or {}).get("linked_motion_sensor")}
    return out


# ── camera alerts (plan §3, Dave 18:26: "they can still trigger doorbell and person vehicle notification in Apple
# Home and Apple TV"). The platform links a camera's own motion event and its doorbell ring by itself
# (`_async_configure_linked_sensors`, setdefault); an explicit `linked_motion_sensor` in the accessory's
# entity_config wins. One detection is linked directly; several are combined with the platform's own group
# helper (a binary_sensor group), which ProOS names GROUP_PREFIX + camera and owns. Nothing = Any Motion.

def alert_groups(group_entries: list) -> dict:
    """{camera entity: {entry_id, entities}} for ProOS's own alert groups."""
    out = {}
    for e in group_entries or []:
        t = str(e.get("title") or "")
        if t.startswith(GROUP_PREFIX):
            out[t[len(GROUP_PREFIX):]] = {"entry_id": e.get("entry_id"),
                                          "entities": sorted((e.get("options") or {}).get("entities") or [])}
    return out


def create_group(camera: str, members: list, req) -> str:
    """One binary_sensor group through the platform's own helper flow. Returns its entry id."""
    s = req("POST", FLOW, {"handler": "group", "show_advanced_options": False}) or {}
    url = "%s/%s" % (FLOW, s.get("flow_id"))
    try:
        _step(s, "user", kind="menu")
        _step(req("POST", url, {"next_step_id": "binary_sensor"}), "binary_sensor")
        s = _step(req("POST", url, {"name": GROUP_PREFIX + camera, "entities": list(members),
                                    "hide_members": False, "all": False}), kind="create_entry")
    except Exception:
        try:
            req("DELETE", url, None)
        except Exception:  # noqa: BLE001
            pass
        raise
    entry_id = (s.get("result") or {}).get("entry_id") if isinstance(s.get("result"), dict) else None
    if not entry_id:
        raise RuntimeError("the platform made the alert group but did not say which")
    return entry_id


def resolve_alerts(alerts: dict, group_entries: list, req, entity_of_entry, sleep) -> tuple:
    """({camera: link entity or None}, created_groups, removed_groups, errors). `alerts` is {camera: [sensor
    entities]} for every camera in play; a camera with [] gets Any Motion (no link: the platform's own)."""
    have = alert_groups(group_entries)
    links, made, gone, errors = {}, [], [], {}
    for cam, chosen in (alerts or {}).items():
        chosen = sorted(dict.fromkeys(c for c in (chosen or []) if isinstance(c, str) and "." in c))
        g = have.get(cam)
        try:
            if len(chosen) > 1 and any(not c.startswith("binary_sensor.") for c in chosen):
                raise RuntimeError("packages cannot be combined with other alerts")
            if len(chosen) > 1:
                if g and g["entities"] == chosen:
                    gid = g["entry_id"]
                else:
                    if g:
                        remove(g["entry_id"], req)
                        gone.append(g["entry_id"])
                    gid = create_group(cam, chosen, req)
                    made.append(gid)
                ent = None
                for _ in range(10):        # the platform registers the group's entity a moment after the entry
                    ent = entity_of_entry(gid)
                    if ent:
                        break
                    sleep(0.5)
                if not ent:
                    raise RuntimeError("the alert group has no entity yet")
                links[cam] = ent
            else:
                if g:
                    remove(g["entry_id"], req)
                    gone.append(g["entry_id"])
                links[cam] = chosen[0] if chosen else None
        except Exception as e:  # noqa: BLE001 — one camera's refusal never stops the others
            errors[cam] = str(e)[:200]
    for cam, g in have.items():          # a camera no longer in play keeps no group
        if cam not in (alerts or {}) and g["entry_id"] not in gone:
            try:
                remove(g["entry_id"], req)
                gone.append(g["entry_id"])
            except Exception as e:  # noqa: BLE001
                errors[cam] = str(e)[:200]
    return links, made, gone, errors


def is_accessory_entry(e: dict) -> bool:
    return _mode_entity(e)[0] == "accessory"


def plan(wanted, entries: list):
    """(to_create, to_remove): entities needing a new accessory, and OUR entries no longer wanted."""
    want = [w for w in dict.fromkeys(wanted or []) if isinstance(w, str) and "." in w]
    have = accessories(entries)
    create = [w for w in want if w not in have]
    remove = [v["entry_id"] for k, v in have.items() if v["ours"] and k not in want]
    return create, remove


def mdns_name(name: str) -> str:
    """The name rule the platform's own flow applies (`_async_available_name`): letters, digits, spaces."""
    n = re.sub(r"[^A-Za-z0-9 ]+", " ", str(name or ""))
    n = re.sub(r"\s+", " ", n).strip()[:60].strip()
    return n or "ProOS Device"


def unique_name(want: str, taken) -> str:
    taken = {str(t) for t in (taken or []) if t}
    if want not in taken:
        return want
    i = 2
    while "%s %d" % (want, i) in taken:
        i += 1
    return "%s %d" % (want, i)


def _step(s, step_id=None, kind="form"):
    s = s or {}
    if s.get("type") != kind or (step_id and s.get("step_id") != step_id):
        raise RuntimeError("expected %s %s, got type=%r step=%r errors=%r reason=%r"
                           % (kind, step_id or "", s.get("type"), s.get("step_id"), s.get("errors"), s.get("reason")))
    return s


def create(entity_id: str, req) -> str:
    """Make one accessory-mode entry for `entity_id` through the platform's own flows. Returns its entry id.
    A half-made entry is removed again; a half-open flow is closed."""
    dom = entity_id.split(".", 1)[0]
    s = req("POST", FLOW, {"handler": "homekit", "show_advanced_options": False}) or {}
    url = "%s/%s" % (FLOW, s.get("flow_id"))
    try:
        _step(s, "user")
        s = _step(req("POST", url, {"include_domains": [SEED_DOMAIN]}), "pairing")
        s = _step(req("POST", url, {}), kind="create_entry")
    except Exception:
        try:
            req("DELETE", url, None)
        except Exception:  # noqa: BLE001
            pass
        raise
    entry_id = ((s.get("result") or {}).get("entry_id")) if isinstance(s.get("result"), dict) else None
    if not entry_id:
        raise RuntimeError("the platform made the entry but did not say which")
    o = req("POST", OFLOW, {"handler": entry_id}) or {}
    ourl = "%s/%s" % (OFLOW, o.get("flow_id"))
    try:
        _step(o, "init")
        o = _step(req("POST", ourl, {"mode": "accessory", "include_exclude_mode": "include", "domains": [dom]}),
                  "accessory")
        o = req("POST", ourl, {"entities": entity_id}) or {}
        if o.get("type") == "form" and o.get("step_id") == "cameras":
            # the platform's own accessory flow copies a camera's video untouched; sound is switched on so a
            # doorbell's visitor is heard (plan §3, Dave 18:29)
            o = req("POST", ourl, {"camera_copy": [entity_id], "camera_audio": [entity_id]}) or {}
        _step(o, kind="create_entry")
    except Exception:
        for path in (ourl, "/api/config/config_entries/entry/%s" % entry_id):
            try:
                req("DELETE", path, None)
            except Exception:  # noqa: BLE001
                pass
        raise
    return entry_id


def remove(entry_id: str, req) -> None:
    req("DELETE", "/api/config/config_entries/entry/%s" % entry_id, None)


def name_entries(store_path: str, names: dict, links: dict | None = None) -> int:
    """Give each new accessory entry its device's name ({entry_id: friendly name}), in the config-entry store,
    as `_homekit_brand` does for the bridge. The caller restarts the platform so the names load (the platform's
    API cannot change a HomeKit entry's name). Backs up, validates, swaps atomically. Returns entries named."""
    with open(store_path) as f:
        d = json.load(f)
    entries = d["data"]["entries"]
    total = len(entries)
    taken = {(e.get("data") or {}).get("name") for e in entries
             if e.get("domain") == "homekit" and e.get("entry_id") not in names}
    links = links or {}
    done = 0
    for e in entries:
        eid = e.get("entry_id")
        if e.get("domain") != "homekit" or (eid not in names and eid not in links):
            continue
        if eid in names:
            nm = unique_name(mdns_name(names[eid]), taken)
            taken.add(nm)
            e.setdefault("data", {})["name"] = nm
            e["title"] = PREFIX + str(names[eid])
        if eid in links:                  # (camera entity, link or None) — the camera's Alert On
            cam, link = links[eid]
            ec = e.setdefault("options", {}).setdefault("entity_config", {}).setdefault(cam, {})
            if link:
                ec["linked_motion_sensor"] = link
            else:
                ec.pop("linked_motion_sensor", None)
        done += 1
    if done:
        try:
            shutil.copy(store_path, store_path + ".proosbak")
        except Exception:  # noqa: BLE001
            pass
        tmp = store_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        with open(tmp) as f:
            json.load(f)
        assert len(d["data"]["entries"]) == total
        os.replace(tmp, store_path)
    return done


def reconcile(wanted, entries: list, req, attrs_of, sleep, store_path: str, alerts: dict | None = None,
              group_entries: list | None = None, entity_of_entry=None, names_for: dict | None = None) -> dict:
    """Make the accessory entries match `wanted` (entities the installer switched on that need their own
    accessory). Entities that do not need one are refused here — they belong on the bridge."""
    ok, refused = [], []
    for w in dict.fromkeys(wanted or []):
        (ok if needs_own(w, attrs_of(w)) else refused).append(w)
    to_create, to_remove = plan(ok, entries)
    created, removed, errors, names, made_entry = [], [], {}, {}, {}
    for eid in to_create:
        try:
            entry_id = create(eid, req)
            created.append(eid)
            made_entry[eid] = entry_id
            # 970r: the name Pro shows (a camera's own name, not its stream's) — else the platform's friendly name
            names[entry_id] = (names_for or {}).get(eid) or (attrs_of(eid) or {}).get("friendly_name") or eid
        except Exception as e:  # noqa: BLE001 — one device's refusal never stops the others
            errors[eid] = str(e)[:200]
    for entry_id in to_remove:
        try:
            remove(entry_id, req)
            removed.append(entry_id)
        except Exception as e:  # noqa: BLE001
            errors[entry_id] = str(e)[:200]
    # camera alerts: only cameras that are (or now become) their own accessory
    links_by_entry, groups_made, groups_gone = {}, [], []
    if alerts is not None:      # {} still matters: a camera switched off keeps no alert group
        cams = {c: v for c, v in alerts.items() if c in ok and c.startswith("camera.")}
        links, groups_made, groups_gone, aerr = resolve_alerts(cams, group_entries or [], req,
                                                                entity_of_entry or (lambda _i: None), sleep)
        errors.update(aerr)
        now = accessories(entries)
        for cam, link in links.items():
            entry_id = (now.get(cam) or {}).get("entry_id") or made_entry.get(cam)
            if not entry_id:
                continue
            if (now.get(cam) or {}).get("linked_motion_sensor") != link or cam in made_entry:
                links_by_entry[entry_id] = (cam, link)
    named = 0
    if names or links_by_entry:
        sleep(3)       # 968w: the platform's own pending save of the new entries lands first
        named = name_entries(store_path, names, links_by_entry)
    return {"created": created, "removed": removed, "refused": refused, "errors": errors,
            "named": named, "alerts_set": len(links_by_entry), "groups_made": groups_made,
            "groups_removed": groups_gone, "restart": bool(named)}
