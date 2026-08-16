"""
ProOS Core - dashboard bottom-nav layout (per-site, server-side).

Stored here (not the input_text command channel) so it has no size limit and
can hold a per-AREA layout for large homes. Shape:

  { "home":  ["lights", "climate", ...],
    "areas": { "Lounge":  ["lights", "media"],
               "Theatre": ["media", "lights"] } }

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


def load_caps() -> dict:
    try:
        with open(CAPS_STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_caps(caps: dict) -> dict:
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
                clean["rooms"] = {str(k): [str(x) for x in v]
                                  for k, v in rooms.items() if isinstance(v, list)}
        os.makedirs(os.path.dirname(CAPS_STORE), exist_ok=True)
        with open(CAPS_STORE, "w", encoding="utf-8") as fh:
            json.dump(clean, fh)
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


def load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            return _clean(json.load(fh))
    except Exception:
        return {}


def save(cfg: dict) -> dict:
    try:
        clean = _clean(cfg)
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        with open(STORE, "w", encoding="utf-8") as fh:
            json.dump(clean, fh)
        _LOG.info("navconfig saved (home=%d, areas=%d)",
                  len(clean.get("home", [])), len(clean.get("areas", {})))
        return clean
    except Exception as exc:
        _LOG.warning("navconfig save failed: %s", exc)
        return {"error": str(exc)}
