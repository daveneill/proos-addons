"""THE BOX KNOWS WHERE IN THE WORLD IT IS — time zone, country, currency (register 1002).

Dave, 25 Oct 2026, 21:20 (Jarvis answered "10:15 AM" at 9:15 pm Melbourne): "Yes I haven't set location in pro yet as
weather it also asked where I was" … 21:22 "Yes please".

READ ON HIS BOX: the platform's time zone UTC, no country, no location. ProOS's own headless onboarding (provision.py)
sends the platform's "core_config" step with nothing in it, so every box ProOS onboards — the image's first boot
included — came out on UTC with no country; Pro's Site page sets the map pin only.

THE PLATFORM'S OWN MECHANISM, named: the platform's onboarding asks its own location service from the box's internet
connection (the websocket command config/core/detect — read on his box: Australia/Melbourne, AU, AUD) and writes the
answer with config/core/update. ProOS does exactly that, once, on a box that has never been told: time zone still UTC
AND no country. A box whose country is set has been told by a person (or the platform's own onboarding) and is never
touched, whatever its time zone. The installer sees and can change both in Pro's Site page.
"""
from __future__ import annotations

UNSET_ZONES = ("", "UTC", "Etc/UTC")


def never_told(cfg) -> bool:
    """True for a box whose time zone and country were never set: UTC (or none) and no country."""
    cfg = cfg or {}
    return str(cfg.get("time_zone") or "") in UNSET_ZONES and not cfg.get("country")


def ensure(ws) -> dict:
    """One pass. Returns {"done": bool, "changed": {...}, "error": str}. done = nothing left to do."""
    out = {"done": False, "changed": {}}
    try:
        cfg = ws("get_config") or {}
    except Exception as e:  # noqa: BLE001
        out["error"] = "get_config: %s" % str(e)[:160]
        return out
    if not never_told(cfg):
        out["done"] = True
        return out
    try:
        found = ws("config/core/detect") or {}
    except Exception as e:  # noqa: BLE001 — no internet yet, or an older platform: try again later
        out["error"] = "detect: %s" % str(e)[:160]
        return out
    upd = {}
    if found.get("time_zone"):
        upd["time_zone"] = str(found["time_zone"])
    if found.get("country"):
        upd["country"] = str(found["country"])
    if found.get("currency"):
        upd["currency"] = str(found["currency"])
    if not upd.get("time_zone"):
        out["error"] = "the platform's location service gave no time zone"
        return out
    ws("config/core/update", **upd)
    out["changed"] = upd
    out["done"] = True
    return out
