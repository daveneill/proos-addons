"""
ProOS Core - dashboard bottom-nav layout (per-site, server-side).

Stored here (not the input_text command channel) so it has no size limit and
can hold a per-AREA layout for large homes. Shape:

  { "home":  ["lights", "climate", ...],
    "areas": { "lounge":  ["lights", "media"],
               "theatre": ["media", "lights"] } }

REGISTER 440: "areas" (and navcaps "rooms") are keyed by the platform's
AREA ID, never by a room's name. They used to be keyed by name — rename a
room in the platform and its layout was orphaned. On every load and save the
keys are checked against the platform's live area list: a key that is a
room's NAME is re-keyed to its id (an older store, migrated once and saved
back); a key that is neither a known id nor a known name is a room that no
longer exists and is dropped. Names are never written here.

Any area not present in "areas" falls back to the dashboard's built-in area
default. The installer writes this from the Pro console (POST, admin-gated);
the dashboard reads it on load (GET). Homeowner tweaks, if any, layer on top
client-side. Never raises to the caller.
"""
import json
import logging
import os

_LOG = logging.getLogger("proos.navconfig")
STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "navconfig.json")
CAPS_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "navcaps.json")


def rekey(rooms: dict, areas) -> tuple:
    """(rekeyed_map, changed). `areas` is the platform's live area list
    [{area_id, name}]; None means "unknown" and the map is returned as it is
    (nothing is decided without the platform). A name key becomes its id; an
    id key stays; anything else is a room that no longer exists."""
    if not isinstance(rooms, dict):
        return {}, False
    if areas is None:
        return dict(rooms), False
    ids = {str(a.get("area_id")) for a in areas if a.get("area_id")}
    by_name = {str(a.get("name") or "").strip().lower(): str(a.get("area_id"))
               for a in areas if a.get("area_id") and a.get("name")}
    out, changed = {}, False
    for k, v in rooms.items():          # id keys first: they always win
        if str(k) in ids:
            out[str(k)] = v
    for k, v in rooms.items():
        k = str(k)
        if k in ids:
            continue
        changed = True                  # a name key, or a room that is gone
        aid = by_name.get(k.strip().lower())
        if aid:
            out.setdefault(aid, v)      # an id key already present wins
    return out, changed


def load_caps(areas=None) -> dict:
    try:
        with open(CAPS_STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            return {}
        rooms, changed = rekey(d.get("rooms") or {}, areas)
        if changed:
            d["rooms"] = rooms
            _write(CAPS_STORE, d)
            _LOG.info("navcaps re-keyed by area id (register 440)")
        elif isinstance(d.get("rooms"), dict):
            d["rooms"] = rooms
        return d
    except Exception:
        return {}


def _write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def save_caps(caps: dict, areas=None) -> dict:
    """Store the dashboard's own per-room capability map (which pages each room
    supports). The builder reads this so it offers exactly what the dashboard
    will show. Written by the dashboard on load."""
    try:
        clean = {}
        if isinstance(caps, dict):
            if isinstance(caps.get("home"), list):
                clean["home"] = [str(x) for x in caps["home"]]
            rooms = caps.get("rooms")
            if isinstance(rooms, dict):
                rooms = {str(k): [str(x) for x in v]
                         for k, v in rooms.items() if isinstance(v, list)}
                clean["rooms"], _ = rekey(rooms, areas)
        _write(CAPS_STORE, clean)
        return clean
    except Exception as exc:
        _LOG.warning("navcaps save failed: %s", exc)
        return {"error": str(exc)}


def _clean(cfg: dict) -> dict:
    out = {}
    if isinstance(cfg, dict):
        if isinstance(cfg.get("home"), list):
            out["home"] = [str(x) for x in cfg["home"]]
        areas = cfg.get("areas")
        if isinstance(areas, dict):
            out["areas"] = {str(k): [str(x) for x in v]
                            for k, v in areas.items() if isinstance(v, list)}
        if isinstance(cfg.get("offered"), list):
            # What the Pro nav editor COULD offer at save time (Dave, 9 Aug
            # 2026: new Bedroom lights + the Home alarm never appeared — the
            # saved layout predated them and gated them off forever). A page
            # absent from this list was never a CHOICE, so the dashboard
            # defaults it to visible when its capability later appears.
            # "A choice nobody makes leaves the room broken forever."
            out["offered"] = [str(x) for x in cfg["offered"]]
    return out


def load(areas=None) -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            clean = _clean(json.load(fh))
        if "areas" in clean:
            clean["areas"], changed = rekey(clean["areas"], areas)
            if changed:
                _write(STORE, clean)
                _LOG.info("navconfig re-keyed by area id (register 440)")
        return clean
    except Exception:
        return {}


def save(cfg: dict, areas=None) -> dict:
    try:
        clean = _clean(cfg)
        if "areas" in clean:
            clean["areas"], _ = rekey(clean["areas"], areas)
        _write(STORE, clean)
        _LOG.info("navconfig saved (home=%d, areas=%d)",
                  len(clean.get("home", [])), len(clean.get("areas", {})))
        return clean
    except Exception as exc:
        _LOG.warning("navconfig save failed: %s", exc)
        return {"error": str(exc)}
