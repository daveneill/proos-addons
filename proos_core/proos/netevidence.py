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
# Self-heal — ProOS turns its own evidence back on (register 147)
# ---------------------------------------------------------------------------
# Dave, 14 Aug 2026: "I said the UniFi is supposed to be auto enabled for Allow
# bandwidth sensors." The certification table below has always known the option
# is REQUIRED, prepare.apply_recommended has been able to write it since 5 Aug
# (it was the option that writer was first proven on), and prepare.py's own
# docstring lists it as one of the three settings a factory reset drops. Three
# parts of the product knew, and none of them acted — the installer got a card
# telling him to go and do it by hand instead.
def ensure_traffic_sensors(client, prepare_mod) -> dict:
    """Enable the certified provider's traffic sensors if they are missing.

    OBSERVATION FIRST: if the rate sensors already exist, nothing is touched — a
    working home is never "repaired". With no certified provider configured,
    ProOS does nothing and says so rather than inventing a failure.
    """
    try:
        states = client._req("GET", "/api/states") or []
        ids = [s.get("entity_id", "") for s in states]
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "did": "unreadable", "why": str(e)}
    if rate_sensor_ids(ids):
        return {"ok": True, "did": "already_on"}
    for dom, facts in PROVIDERS.items():
        if facts.get("planned") or not facts.get("traffic_sensor_pattern"):
            continue
        want = {o["option"]: True for o in facts.get("required_options", [])
                if o.get("why") == "traffic witnesses"}
        if not want:
            continue
        try:
            entries = client.config_entries(dom) or []
        except Exception:                                        # noqa: BLE001
            entries = []
        for entry in entries:
            eid = entry.get("entry_id")
            if not eid:
                continue
            try:
                prepare_mod.apply_recommended(client, eid, want)
            except Exception as e:                               # noqa: BLE001
                # Reported, never swallowed: the honest card stays up.
                return {"ok": False, "did": "apply_failed", "why": str(e),
                        "provider": dom}
            try:
                client.reload_integration(eid)
            except Exception:                                    # noqa: BLE001
                pass
            return {"ok": True, "did": "enabled", "provider": dom,
                    "options": sorted(want)}
    return {"ok": False, "did": "no_provider"}


def autobind(client, project_mod, option_raw: str = "") -> dict:
    """Bind every committed source whose witness is PROVEN BY IDENTITY.

    STAGE 3 BUILD 5 (16 Aug 2026, register 182): the register-147 amendment
    that let name-token matches be APPLIED here is REVERSED. One shared word
    ("bedroom") could wire a TV to a different client's traffic sensor, and
    three verdict engines then built "evidence-backed" conclusions on the
    wrong device's data (census N3). The product's own doctrine had the
    answer all along: witness binding is JOINED BY IP (Witness Binding
    Reference, 9 Aug).

    A rate sensor is auto-bound only when its HA device's own tracker
    carries the SAME network address the source resolves to — the
    controller's identity, not a word. Name-token matches are returned as
    `suggested` for the installer's one-tap commit in Systems › Network,
    and the source stays UNCOVERED until a human or an identity binds it —
    the coverage-gap card keeps telling the truth meanwhile. An unreadable
    registry or an unknown address never guesses: suggestion-only.
    """
    try:
        states = client._req("GET", "/api/states") or []
        ids = [s.get("entity_id", "") for s in states]
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": str(e), "bound": {}, "uncovered": []}
    rates = rate_sensor_ids(ids)
    # ── the identity join: source ip ⇄ tracker ip ⇄ rate sensor's device ──
    try:
        from . import netmap as _nm
        _ipmap = _nm.harvest(client=client) or {}
    except Exception:                                            # noqa: BLE001
        _ipmap = {}
    _dev_of, _ip_of_dev = {}, {}
    try:
        for e in (client.entity_registry() or []):
            if e.get("entity_id") and e.get("device_id"):
                _dev_of[e["entity_id"]] = e["device_id"]
        _attrs = {s.get("entity_id"): (s.get("attributes") or {})
                  for s in states}
        for eid, did in _dev_of.items():
            if eid.startswith("device_tracker."):
                ip = _attrs.get(eid, {}).get("ip")
                if ip:
                    _ip_of_dev[did] = str(ip)
    except Exception:                                            # noqa: BLE001
        _dev_of, _ip_of_dev = {}, {}

    def _identity_picks(src):
        ip = str((_ipmap.get(src) or {}).get("ip") or "")
        if not ip:
            return []
        return [r for r in rates
                if _ip_of_dev.get(_dev_of.get(r)) == ip][:2]

    sources = [s["entity"] for s in _committed_sources(project_mod)]
    cl = classify(load_witnesses(option_raw), ids, sources)
    real = cl["real"]
    # CLEAN UP OUR OWN CONFIG (register 150). A binding whose key is not a
    # source in any committed room can never testify — on Dave's box these were
    # the Google Cast twins of three Shields, left in the bootstrap option. A
    # card that names them every minute forever is nagging about something ProOS
    # can fix: tombstone them (which kills the option entry too) and say so.
    cleared = []
    for src, rec in (cl["broken"] or {}).items():
        if "not_a_source" in (rec.get("reasons") or []):
            save_witness(src, None, None)
            cleared.append(src)
    bound, uncovered, suggested = {}, [], {}
    for src in sources:
        if src in real:
            continue
        picks = _identity_picks(src)
        if picks:
            save_witness(src, picks, None)   # identity: evidence, bound
            bound[src] = picks
            continue
        toks = suggest_sensors(src, rates)
        if toks:
            suggested[src] = toks            # a word is an offer, never a bind
        uncovered.append(src)                # honest until a human commits
    return {"ok": True, "bound": bound, "uncovered": uncovered,
            "suggested": suggested, "cleared": cleared}


# ---------------------------------------------------------------------------
# Binding integrity — a witness that cannot testify is not a witness
# ---------------------------------------------------------------------------
def classify(witnesses: dict, known_ids=None, source_eids=None) -> dict:
    """Split a witness map into the bindings that can ACTUALLY testify and the
    ones that cannot, with the reason recorded.

    Register 146 (Dave, 14 Aug 2026). His box carried six bindings and not one
    could testify: three named `sensor.*_data_rate_*` entities that no longer
    exist (the network integration's bandwidth sensors were switched off), and
    three named the Google Cast twin of a Shield rather than the Android TV
    entity the room actually commits. Three rooms said "no witness" honestly;
    the other three were counted as COVERED and said nothing — a surface
    claiming a power it does not have, in its silent form.

    A binding is real only when
      * at least one of its sensors exists (one of two surviving still measures
        traffic — the rate is a sum), and
      * its key is a source ProOS actually watches, when that list is known.

    BLIND IS NOT BROKEN: with no snapshot (`known_ids` empty) nothing is
    accused, and with no source list (`source_eids` None) sourcehood is not
    judged. ProOS does not manufacture a failure out of its own ignorance.
    """
    real, broken = {}, {}
    known = set(known_ids or ())
    # A-5 (audit, 15 Aug): an EMPTY commission means UNKNOWN, not "nothing is a
    # source". The blind-is-not-broken guard was applied to `known_ids` and not
    # to this one — so on the first boot after a factory reset, when no room is
    # committed yet, every binding was judged not_a_source and autobind()
    # tombstoned them all. Same shape as register 148: an empty answer read as a
    # real answer. The guard belongs on both arguments or neither.
    srcs = set(source_eids) if source_eids else None
    for src, rec in (witnesses or {}).items():
        if not rec:
            continue                       # tombstone: already not a binding
        sensors = [s for s in (rec.get("sensors") or []) if s]
        missing = [s for s in sensors if s not in known] if known else []
        reasons = []
        if not sensors:
            reasons.append("no_sensors")
        elif known and len(missing) == len(sensors):
            reasons.append("sensors_missing")
        if srcs is not None and src not in srcs:
            reasons.append("not_a_source")
        out = {"sensors": sensors, "min": float(rec.get("min", 0.25)),
               "missing": missing}
        if reasons:
            out["reasons"] = reasons
            broken[src] = out
        else:
            real[src] = out
    return {"real": real, "broken": broken}


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
    # Register 146: judge every binding against what is actually in HA before
    # reporting coverage. A binding whose sensors are gone is not coverage, and
    # Pro is told it is BROKEN — not that it was never made, which would send
    # the installer to bind a witness he already bound.
    _cl = classify(witnesses, ids, [s["entity"] for s in sources])
    live = _cl["real"]
    for src in sources:
        w = live.get(src["entity"])
        src["witness"] = w or None
        src["broken"] = _cl["broken"].get(src["entity"]) or None
        src["suggested"] = (suggest_sensors(src["entity"], unifi_sensors)
                            if (not w and unifi_sensors) else [])

    covered = len([s for s in sources if s["witness"]])
    any_traffic = any(p.get("traffic_ready") for p in providers.values())
    return {
        "providers": providers,
        "sources": sources,
        "broken": _cl["broken"],
        "coverage": {"covered": covered, "total": len(sources)},
        "degraded": (not any_traffic),
        "degraded_note": ("No network evidence provider detected — verdicts rest "
                          "on integration state + verdict memory. Certified "
                          "providers: " + ", ".join(
                              p["label"] for p in PROVIDERS.values())
                          ) if not any_traffic else "",
    }
