"""
ProOS Core — house-wide app-shortcut favourites (spec 1 Aug 2026, review §3c).

A homeowner's press-and-hold favourite used to live in ONE panel's
localStorage: the iPad knew about the shortcut, the wall panel didn't, and
every screen had to be taught by hand. Favourites now publish HERE, and every
dashboard converges on this list — one gesture on any screen, the whole home
agrees. That convergence is the point ("a system like no other").

Shape, deliberately minimal and raw: {area_id: [{app, appid, device}]}.
`app` is the DEVICE'S OWN source string (launching needs it verbatim);
canonical display naming stays a render-time concern (appart.canonical).
`device` pins the shortcut to the entity it was favourited from, exactly as
the widget itself is pinned. Wiped by factory reset with the rest of the
home's commissioning.
"""
from __future__ import annotations

import json
import os

_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"),
                      "appfavs.json")


def _load() -> dict:
    try:
        with open(_STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def _write(d: dict) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, _STORE)


def list_for(area_id: str) -> list:
    if not area_id:
        return []
    out = []
    for f in (_load().get(area_id) or []):
        if isinstance(f, dict) and f.get("app"):
            out.append({"app": f["app"], "appid": f.get("appid") or None,
                        "device": f.get("device") or None})
    return out


def set_fav(area_id: str, app: str, appid=None, device=None,
            remove: bool = False) -> dict:
    """Add or remove one favourite. Identity is (app, device) — the same pair
    the widget dedupes on. Idempotent both ways."""
    area_id = (area_id or "").strip()
    app = (app or "").strip()
    if not area_id or not app:
        return {"error": "area_id and app required"}
    d = _load()
    cur = [f for f in (d.get(area_id) or []) if isinstance(f, dict)]
    dev = (device or None)
    keep = [f for f in cur
            if not (f.get("app") == app and (f.get("device") or None) == dev)]
    if not remove:
        keep.append({"app": app, "appid": appid or None, "device": dev})
    if keep:
        d[area_id] = keep
    else:
        d.pop(area_id, None)
    _write(d)
    return {"ok": True, "area_id": area_id, "favourites": keep}


def clear() -> None:
    try:
        if os.path.exists(_STORE):
            os.remove(_STORE)
    except Exception:                                            # noqa: BLE001
        pass
