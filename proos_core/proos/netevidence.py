"""
ProOS Core — network evidence providers (templated, certification-driven).

The traffic-witness rung of the room verdict needs per-client network facts
(presence, throughput). Those come from whatever network stack the site runs —
UniFi today, TP-Link Omada or others later. This module is the CONTRACT:

  * PROVIDERS holds per-integration FACTS supplied by certification — how a
    certified network integration exposes client presence and client traffic,
    and which of its options must be enabled. Adding Omada later is a new
    PROVIDERS entry + its certification checklist; no engine change.
  * Readiness is judged by OBSERVATION (which entities actually exist), never
    by assuming an options flag — confirm, don't assume. If no provider is
    present, awareness degrades gracefully: the witness rung simply never
    fires and verdicts rest on integration state + verdict memory.
  * The witness map (source entity -> rate sensors + threshold) is
    INSTALLER-COMMITTED through Pro and stored in /data/net_witnesses.json.
    The add-on option `traffic_witnesses` remains as a bootstrap/legacy path;
    the committed file wins.

Identity rules: everything is keyed by entity_id / integration domain.
Suggestions for mapping ARE name-token matched — but they are only ever
SUGGESTIONS surfaced to the installer, who commits the binding. Runtime never
name-matches.
"""
from __future__ import annotations

import json
import os
import re
import threading

_DATA = "/data/net_witnesses.json"
_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Certification facts, one entry per certified network integration.
# ---------------------------------------------------------------------------
PROVIDERS = {
    "unifi": {
        "label": "UniFi Network",
        # evidence classes this provider contributes
        "capabilities": ["client_presence", "client_traffic"],
        # how this integration's entities look (observation patterns)
        "traffic_sensor_pattern": r"^sensor\..*data_rate(_\d+)?$",
        "presence_pattern": r"^device_tracker\.",
        "traffic_unit": "MB/s",
        "default_min_rate": 0.25,   # MB/s — above idle trickle, below any video
        # certification checklist: options that MUST be on for full awareness
        "required_options": [
            {"option": "allow_bandwidth_sensors", "why": "traffic witnesses"},
            {"option": "track_clients", "why": "presence witnesses"},
            {"option": "track_wired_clients", "why": "wired sources (ATV/Shield)"},
        ],
    },
    "unifiprotect": {
        "label": "UniFi Protect",
        "capabilities": ["camera_state", "smart_detection"],
        "traffic_sensor_pattern": None,
        "presence_pattern": None,
        "traffic_unit": None,
        "default_min_rate": None,
        "required_options": [
            {"option": "smart detections enabled per camera",
             "why": "person/vehicle/animal states for awareness + Savant mirror"},
        ],
    },
    # Future certified providers plug in here — same shape, no engine change:
    "omada": {
        "label": "TP-Link Omada (planned)",
        "capabilities": ["client_presence", "client_traffic"],
        "traffic_sensor_pattern": None,   # filled at certification
        "presence_pattern": None,
        "traffic_unit": None,
        "default_min_rate": None,
        "required_options": [],
        "planned": True,
    },
}


# ---------------------------------------------------------------------------
# Witness store (installer-committed, survives restarts, wins over the option)
# ---------------------------------------------------------------------------
def load_witnesses(option_raw: str = "") -> dict:
    """Merged witness map: committed file over bootstrap option."""
    from .ctlbridge import ActivityPublisher
    merged = ActivityPublisher.parse_witnesses(option_raw or "")
    try:
        with _LOCK:
            if os.path.exists(_DATA):
                data = json.load(open(_DATA))
                for src, rec in (data or {}).items():
                    if rec is None:
                        merged.pop(src, None)        # explicit removal
                    elif rec.get("sensors"):
                        merged[src] = {"sensors": list(rec["sensors"]),
                                       "min": float(rec.get("min", 0.25))}
    except Exception:
        pass
    return merged


def save_witness(source: str, sensors: list | None, min_rate: float | None) -> dict:
    """Commit (or clear, sensors=None) one source's witness binding."""
    with _LOCK:
        data = {}
        try:
            if os.path.exists(_DATA):
                data = json.load(open(_DATA)) or {}
        except Exception:
            data = {}
        if sensors:
            data[source] = {"sensors": list(sensors),
                            "min": float(min_rate if min_rate is not None else 0.25)}
        else:
            data[source] = None                      # tombstone: kill option entry too
        tmp = _DATA + ".tmp"
        json.dump(data, open(tmp, "w"), indent=1)
        os.replace(tmp, _DATA)
    return data


# ---------------------------------------------------------------------------
# Evidence-based readiness inspection
# ---------------------------------------------------------------------------
def _committed_sources(project_mod) -> list:
    """Every watch-source entity in committed rooms, with its area."""
    out = []
    try:
        proj = project_mod.load() or {}
        for key, rec in (proj.get("areas") or {}).items():
            if not (rec and rec.get("committed")):
                continue
            for e in (rec.get("sources") or []):
                eid = e.get("entity") if isinstance(e, dict) else e
                if isinstance(eid, str) and eid:
                    out.append({"area": rec.get("name") or key, "entity": eid})
    except Exception:
        pass
    return out


def _tokens(s: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2}


def suggest_sensors(source_eid: str, rate_sensors: list, limit: int = 2) -> list:
    """The top token-matched traffic sensors for a source — the SUGGESTION the
    installer commits with ONE TAP. This is a suggestion, never a runtime match:
    it fires only when a human triggers the bind (the doctrine keeps name-tokens
    at the suggestion boundary; the runtime verdict never name-matches)."""
    stoks = _tokens(source_eid)
    scored = [(len(stoks & _tokens(s)), s) for s in (rate_sensors or [])]
    scored = [(o, s) for o, s in scored if o]
    scored.sort(reverse=True)
    return [s for _, s in scored[:limit]]


def rate_sensor_ids(all_ids: list) -> list:
    """The certified UniFi traffic (data-rate) sensor ids among `all_ids`."""
    pat = PROVIDERS["unifi"].get("traffic_sensor_pattern")
    return [i for i in (all_ids or []) if pat and re.match(pat, i)]


def inspect(client, project_mod, option_raw: str = "") -> dict:
    """Full awareness report for Pro. Observation only — no options assumed."""
    try:
        states = client._req("GET", "/api/states") or []
    except Exception:
        states = []
    ids = [s.get("entity_id", "") for s in states]

    providers = {}
    for dom, facts in PROVIDERS.items():
        rec = {"label": facts["label"],
               "capabilities": facts["capabilities"],
               "planned": bool(facts.get("planned")),
               "required_options": facts["required_options"]}
        tp = facts.get("traffic_sensor_pattern")
        pp = facts.get("presence_pattern")
        rec["traffic_sensors"] = sorted(
            [i for i in ids if tp and re.match(tp, i)]) if tp else []
        rec["presence_entities_count"] = (
            len([i for i in ids if pp and re.match(pp, i)]) if pp else 0)
        if dom == "unifiprotect":
            rec["present"] = any(i.startswith("camera.") for i in ids)
        else:
            rec["present"] = bool(rec["traffic_sensors"]) or rec["presence_entities_count"] > 0
        rec["traffic_ready"] = bool(rec["traffic_sensors"])
        providers[dom] = rec

    witnesses = load_witnesses(option_raw)
    unifi_sensors = providers.get("unifi", {}).get("traffic_sensors", [])
    sources = _committed_sources(project_mod)
    for src in sources:
        w = witnesses.get(src["entity"])
        src["witness"] = w or None
        src["suggested"] = (suggest_sensors(src["entity"], unifi_sensors)
                            if (not w and unifi_sensors) else [])

    covered = len([s for s in sources if s["witness"]])
    any_traffic = any(p.get("traffic_ready") for p in providers.values())
    return {
        "providers": providers,
        "sources": sources,
        "coverage": {"covered": covered, "total": len(sources)},
        "degraded": (not any_traffic),
        "degraded_note": ("No network evidence provider detected — verdicts rest "
                          "on integration state + verdict memory. Certified "
                          "providers: " + ", ".join(
                              p["label"] for p in PROVIDERS.values())
                          ) if not any_traffic else "",
    }
