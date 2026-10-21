"""ProOS Core — weather is standard, at the home's location (register 970a, 21 Oct 2026).

Dave, 16:28: "yes weather needs to be standard based on location set in location in Pro". Read on the G-Flip the same
afternoon: no weather service at all (no weather entity, no weather integration) — so weather never opened there, and
Pro's location page said "Weather and sun now use the new location" when nothing used it.

THE PLATFORM'S OWN MACHINERY: its default forecast service (Met.no, the one the platform's own first-run sets up for
the home), added through its own setup flow at the coordinates the platform holds for the home — the coordinates Pro's
Location page sets. When the home moves, the service is moved through its own options flow; if that fails nothing is
removed — the reading says it could not be moved. What only ProOS keeps: where the service was put, so a move is
noticed (/data/weather_std.json).
"""
from __future__ import annotations

import json
import os

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
STORE = os.path.join(DATA_DIR, "weather_std.json")
DOMAIN = "met"


def _load():
    try:
        with open(STORE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, STORE)


def _where(cfg):
    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if lat is None or lon is None:
        return None
    return {"latitude": round(float(lat), 5), "longitude": round(float(lon), 5),
            "elevation": int(round(float(cfg.get("elevation") or 0)))}


def _same(a, b):
    return bool(a and b) and abs(a["latitude"] - b["latitude"]) < 0.001 and abs(a["longitude"] - b["longitude"]) < 0.001


def _form(where, name):
    return {"name": name or "Home", "latitude": where["latitude"], "longitude": where["longitude"],
            "elevation": where["elevation"]}


def ensure(ws, req) -> dict:
    """One pass: the forecast service exists, at the home's location. A right box gets no writes. Never raises."""
    out = {}
    try:
        cfg = (ws("get_config") or {})
        cfg = cfg.get("result", cfg) if isinstance(cfg, dict) else {}
        where = _where(cfg)
        if not where or (where["latitude"] == 0 and where["longitude"] == 0):
            return {"skipped": "the home has no location set yet (Pro › Location)"}
        entries = ws("config_entries/get", domain=DOMAIN) or []
        entries = entries.get("result", entries) if isinstance(entries, dict) else entries
        st = _load()
        mine = next((e for e in entries if e.get("entry_id") == st.get("entry_id")), None) if st.get("entry_id") else None
        if not entries:
            r = req("POST", "/api/config/config_entries/flow", {"handler": DOMAIN, "show_advanced_options": False}) or {}
            if r.get("type") == "form" and r.get("flow_id"):
                r = req("POST", "/api/config/config_entries/flow/%s" % r["flow_id"], _form(where, "Home")) or {}
            if r.get("type") != "create_entry":
                return {"error": "the platform's weather setup said %s" % (r.get("errors") or r.get("reason") or r.get("type"))}
            _save({"entry_id": (r.get("result") or {}).get("entry_id"), "at": where})
            return {"created": True, "at": where}
        if mine is None:
            # a forecast service set up some other way (by hand, or by the platform's first run) — ProOS does not
            # move what it did not put there; it says so
            return {"current": "a weather service is already set up on this box", "entries": len(entries)}
        if _same(st.get("at"), where):
            return {"current": True, "at": where}
        # the home moved: move the service through its own options, else set it up afresh at the new place
        moved = False
        try:
            r = req("POST", "/api/config/config_entries/options/flow", {"handler": mine["entry_id"]}) or {}
            if r.get("type") == "form" and r.get("flow_id"):
                r = req("POST", "/api/config/config_entries/options/flow/%s" % r["flow_id"], _form(where, mine.get("title"))) or {}
            moved = r.get("type") == "create_entry"
        except Exception:  # noqa: BLE001
            moved = False
        if not moved:
            # nothing is removed to force it: the service stays where it was and the reading says so
            return {"error": "the weather service could not be moved to the new location", "at": st.get("at")}
        _save({"entry_id": mine["entry_id"], "at": where})
        out = {"moved": True, "at": where}
    except Exception as e:  # noqa: BLE001
        out = {"error": str(e)[:160]}
    return out
