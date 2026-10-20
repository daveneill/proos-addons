"""
ProOS Core - dashboard room order (per-home default + optional per-device override).

Server-side so a homeowner's dashboard arrangement is durable: it survives a
browser cache clear, a new device, or an iPad re-image, and can be shared across
every screen in the home. Shape:

  { "default": ["area_id", ...],
    "devices": { "<client_id>": ["area_id", ...] } }

A screen resolves to its own device list if it has one, else the home default,
else nothing (the dashboard keeps HA's natural creation order). Dragging on a
screen saves THAT screen's override; "set for all screens" writes the default.
If a device's opaque client id is ever wiped, that screen simply falls back to
the home default — a graceful outcome, not a broken one. Never raises to the
caller.
"""
import json
import logging
import os

_LOG = logging.getLogger("proos.roomorder")
STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "room_order.json")


def _ids(v) -> list:
    """Coerce to a clean list of string area ids (drops non-list / non-scalar)."""
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, (str, int)):
            s = str(x)
            if s and s not in out:  # de-dupe, preserve first position
                out.append(s)
    return out


def load() -> dict:
    """Full stored document, normalised. Missing/corrupt file -> empty shape."""
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        d = {}
    out = {"default": _ids(d.get("default")), "devices": {}}
    dev = d.get("devices")
    if isinstance(dev, dict):
        out["devices"] = {
            str(k): _ids(v) for k, v in dev.items() if isinstance(v, list)
        }
    return out


def _write(doc: dict) -> dict:
    """Atomic write (tmp + replace) so a crash mid-write can't corrupt the store."""
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, STORE)
    return doc


def resolve(device: str = "") -> dict:
    """What a given screen should use: its device order, else the home default,
    else nothing. Always returns the home default too, so the UI can offer
    'set for all screens' / 'reset to default' without a second call."""
    doc = load()
    device = str(device or "")
    if device and doc["devices"].get(device):
        return {"order": doc["devices"][device], "scope": "device",
                "default": doc["default"]}
    if doc["default"]:
        return {"order": doc["default"], "scope": "default",
                "default": doc["default"]}
    return {"order": [], "scope": "none", "default": doc["default"]}


def save_default(order) -> dict:
    """Set the home-wide default order (applies to every screen without its own
    override)."""
    try:
        doc = load()
        doc["default"] = _ids(order)
        _write(doc)
        _LOG.info("room_order default saved (%d rooms)", len(doc["default"]))
        return resolve()
    except Exception as exc:
        _LOG.warning("room_order default save failed: %s", exc)
        return {"error": str(exc)}


def save_device(device: str, order) -> dict:
    """Set one screen's override order."""
    try:
        device = str(device or "")
        if not device:
            return {"error": "device required"}
        doc = load()
        doc["devices"][device] = _ids(order)
        _write(doc)
        return resolve(device)
    except Exception as exc:
        _LOG.warning("room_order device save failed: %s", exc)
        return {"error": str(exc)}


def clear_device(device: str) -> dict:
    """Drop one screen's override so it follows the home default again."""
    try:
        device = str(device or "")
        doc = load()
        if device in doc["devices"]:
            del doc["devices"][device]
            _write(doc)
        return resolve(device)
    except Exception as exc:
        _LOG.warning("room_order device clear failed: %s", exc)
        return {"error": str(exc)}
