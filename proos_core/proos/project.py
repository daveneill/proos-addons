"""
ProOS Core -- the commissioning PROJECT (the AV orchestration model).

The declared source of truth for what each area's AV system IS: which media_players
are committed to the room, what role each plays (display / source / tvaudio / speaker),
and the audio path. It is the ProOS equivalent of a Control4 project -- except the same
declared model feeds BOTH planes:

  - control:   the generator builds one-touch activities from the declared roles.
  - awareness: the watcher takes the committed set as its expected-state baseline.
               (You can only be *aware* a device is missing/wedged once you've declared
               it should be there -- auto-discovery alone can't tell "gone" from
               "never here". The committed project IS that baseline.)

Where it lives:
  - Persisted at /data/proos_project.json (same pattern as recovery_config.json etc.).
  - Mirrored to HA ENTITY LABELS for every COMMITTED area -- proos_av (membership) plus
    proos_<role> -- so membership is visible and hand-editable in HA, and the dashboard
    (which already reads these labels) filters each room to its committed AV set. The
    project file stays authoritative for anything relational; labels are the mirror.

How it's built:
  - suggest(client) wraps discover_av() per area. discover_av is integration-filtered
    (it only maps known AV integrations -> roles), so Macs, phones and intercom speakers
    never enter the suggestion. Suggestions start uncommitted; nothing touches HA until
    an installer commits an area (create-if-absent: committing never clobbers prior
    committed edits).

Stdlib-only, like the rest of Core. HA writes go through ha_ws.ws_command (the same WS
path area/device/entity registry reads already use).
"""
from __future__ import annotations

import json
import os
import re
import datetime

from . import sync
from . import discovery
from .discovery import discover_av
from .ha_ws import ws_command

DEFAULT_PATH = os.environ.get("PROOS_PROJECT_PATH", "/data/proos_project.json")
SCHEMA_VERSION = 1

# Entity-label vocabulary this module owns. The mirror only ever adds/removes THESE
# labels on an entity; any other labels (proos_pause_*, proos_tv_combined, dashboard_*)
# are left untouched. Keep in lockstep with dashboard.html's LABEL_AV / LABEL_ROLE_*.
LABEL_AV = "proos_av"
LABEL_DISPLAY = "proos_display"
LABEL_SOURCE = "proos_source"
LABEL_TVAUDIO = "proos_tvaudio"
LABEL_SPEAKER = "proos_speaker"
_ROLE_LABELS = {LABEL_AV, LABEL_DISPLAY, LABEL_SOURCE, LABEL_TVAUDIO, LABEL_SPEAKER}
# Per-source input assignment is mirrored as a dynamic label proos_input_<slug> on the
# source entity (e.g. Apple TV on 'HDMI 2' -> proos_input_hdmi_2). The dashboard matches
# the display's reported input to find the active source even when the source lies about
# its power. These are 'managed' like the role labels (added/removed by the mirror).
_INPUT_PREFIX = "proos_input_"


def _input_label(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return _INPUT_PREFIX + (slug or "x")


def _is_managed(label: str) -> bool:
    return label in _ROLE_LABELS or label.startswith(_INPUT_PREFIX)


# ── PERSISTENCE ──────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "updated": None, "areas": {}}


def load(path: str = DEFAULT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "areas" not in data:
            return _empty()
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("areas", {})
        return data
    except FileNotFoundError:
        return _empty()
    except Exception:
        # A corrupt project must never take the box down; treat as empty and let the
        # next save overwrite it.
        return _empty()


def save(project: dict, path: str = DEFAULT_PATH) -> dict:
    project = dict(project or {})
    project["version"] = SCHEMA_VERSION
    project["updated"] = datetime.datetime.utcnow().isoformat() + "Z"
    project.setdefault("areas", {})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(project, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return project


# ── SUGGESTION (discover_av per area) ────────────────────────────────────────

def _tv_capable(client, entities):
    """Which of these audio entities expose a 'TV' source (one render for the set)."""
    if not entities:
        return {}
    tmpl = (
        "{{ {"
        + ", ".join(
            "%s: ('TV' in (state_attr(%s,'source_list') or []))"
            % (json.dumps(e), json.dumps(e))
            for e in entities
        )
        + "} | to_json }}"
    )
    try:
        return json.loads(client.render_template(tmpl)) or {}
    except Exception:
        return {}


def _discrete_inputs(client, display_eid):
    """The display's selectable discrete HDMI inputs (['HDMI 1', ...]) or []. Present =
    the driver can route by explicit input (deterministic one-touch); absent = the room
    falls back to HDMI-CEC one-touch (best-effort). Certified drivers guarantee these."""
    if not display_eid:
        return []
    try:
        sl = json.loads(client.render_template(
            "{{ state_attr('%s','source_list') | to_json }}" % display_eid)) or []
    except Exception:
        return []
    return [s for s in sl if isinstance(s, str) and s.strip().lower().startswith("hdmi")]


def _area_record(client, cluster) -> dict:
    display = cluster.display.entity if cluster.display else None
    sources = [d.entity for d in cluster.sources]
    audio = [d.entity for d in cluster.audio]
    # The TV-audio path: prefer an audio device that exposes a 'TV' source (soundbar /
    # Sonos / AVR on its TV input); otherwise the first audio device, or none.
    tvcap = _tv_capable(client, audio)
    tvaudio = next((e for e in audio if tvcap.get(e)), (audio[0] if audio else None))
    # Per-device metadata: integration + certification tier (certified drivers add the
    # discrete commands + awareness that native integrations omit). For the display we
    # also record its discrete HDMI inputs so the UI can promise deterministic one-touch
    # (present) or flag best-effort CEC (absent). meta is side-channel: membership/label
    # logic still runs off the flat entity lists above.
    meta = {}
    for d in (([cluster.display] if cluster.display else [])
              + list(cluster.sources) + list(cluster.audio)):
        meta[d.entity] = {"integration": d.integration,
                          "tier": getattr(d, "tier", "compatible"),
                          "capabilities": discovery.capabilities(d.integration)}
    if display:
        di = _discrete_inputs(client, display)
        meta[display]["discrete_inputs"] = di
        # Earn the badge, don't assume it: a display claiming certified discrete_input must
        # actually expose HDMI inputs on THIS box. If it doesn't, note it so the UI can
        # show "certified driver, discrete inputs not detected" rather than over-promise.
        caps = set(meta[display]["capabilities"])
        if "discrete_input" in caps:
            meta[display]["discrete_input_verified"] = bool(di)
    # Room kind + warnings so the UI never nags a valid music zone but always flags a TV
    # room whose screen wasn't assigned. sources-without-display = a TV room missing its
    # display (warn, offer the Unassigned tray). audio-only = a music zone (clean, no warn).
    kind = "tv" if (display or sources) else "music"
    warnings = []
    if sources and not display:
        warnings.append("missing_display")
    return {
        "display": display,
        "sources": sources,
        "audio": audio,
        "tvaudio": tvaudio,
        "speakers": list(audio),
        "inputs": {},   # source_entity -> display input name (e.g. 'HDMI 2'); set at review
        "meta": meta,
        "kind": kind,
        "warnings": warnings,
        "committed": False,
    }


def unassigned(client) -> list:
    """AV‑integration media_players HA discovered but that aren't in any area yet.

    These never reach discover_av (it works per area), so a TV that wasn't assigned to a
    room silently goes missing. The installer pulls one into a room from the UI, which also
    sets its HA area. Integration‑filtered, so only real AV devices appear here."""
    known = json.dumps(discovery._KNOWN)
    tmpl = (
        "{% set known = " + known + " %}"
        "{% set ns = namespace(rows=[]) %}"
        "{% for k in known %}"
        "{% for e in integration_entities(k) if e is match('media_player\\.') and not area_name(e) %}"
        "{% set ns.rows = ns.rows + [{'entity': e, 'name': state_attr(e,'friendly_name'), 'integration': k}] %}"
        "{% endfor %}"
        "{% endfor %}"
        "{{ ns.rows | to_json }}"
    )
    try:
        rows = json.loads(client.render_template(tmpl)) or []
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        e = r.get("entity")
        if not e or e in seen:
            continue
        seen.add(e)
        integ = r.get("integration", "unknown")
        out.append({"entity": e, "name": r.get("name") or e, "integration": integ,
                    "tier": discovery.tier(integ),
                    "suggested_role": discovery.ROLE_BY_INTEGRATION.get(integ),
                    "capabilities": discovery.capabilities(integ)})
    return out


def verify(client, project: dict) -> dict:
    """Stage‑4 proof: for every COMMITTED area, is each member reachable right now?

    Honest green/red — reachable = a real state (not unavailable/unknown/absent). This is
    what turns the 'now watching' promise into something the installer can see, not assume.
    Script‑presence and watcher‑coverage checks layer on top later."""
    out = {}
    for area, rec in (project or {}).get("areas", {}).items():
        if not rec.get("committed"):
            continue
        members = _area_members(rec)
        if not members:
            out[area] = {"reachable": [], "unreachable": [], "ok": True}
            continue
        tmpl = ("{{ {" + ", ".join(
            "%s: states(%s)" % (json.dumps(e), json.dumps(e)) for e in members)
            + "} | to_json }}")
        try:
            states = json.loads(client.render_template(tmpl)) or {}
        except Exception:
            states = {}
        reach, unreach = [], []
        for e in members:
            s = states.get(e)
            (unreach if s in (None, "unavailable", "unknown") else reach).append(e)
        out[area] = {"reachable": reach, "unreachable": unreach, "ok": not unreach}
    return {"areas": out, "ok": all(a["ok"] for a in out.values()) if out else True}


def suggest(client) -> dict:
    """Fresh suggestion for every area, from discover_av (integration-filtered)."""
    areas = {}
    for area in sync.list_areas(client):
        try:
            cluster = discover_av(client, area)
        except Exception:
            continue
        rec = _area_record(client, cluster)
        if rec["display"] or rec["sources"] or rec["audio"]:
            areas[area] = rec
    return {"version": SCHEMA_VERSION, "updated": None, "areas": areas}


def merge(existing: dict, suggestion: dict) -> dict:
    """Overlay a fresh suggestion on the stored project WITHOUT clobbering committed
    areas (create-if-absent, applied to rooms). Committed areas are kept verbatim;
    uncommitted areas are refreshed from the suggestion. Committed areas that no longer
    appear in the suggestion are preserved (a device may just be temporarily offline)."""
    ex = (existing or {}).get("areas", {})
    out = {"version": SCHEMA_VERSION, "updated": (existing or {}).get("updated"), "areas": {}}
    for area, rec in suggestion.get("areas", {}).items():
        prior = ex.get(area)
        out["areas"][area] = prior if (prior and prior.get("committed")) else rec
    for area, rec in ex.items():
        if area not in out["areas"] and rec.get("committed"):
            out["areas"][area] = rec
    return out


# ── MEMBERSHIP / ROLE LABEL PLAN ─────────────────────────────────────────────

def _area_members(rec: dict):
    seen, out = set(), []
    for e in ([rec.get("display")] + list(rec.get("sources") or [])
              + list(rec.get("audio") or [])):
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def label_plan(project: dict) -> dict:
    """Desired proos_* label set per entity, for COMMITTED areas only.

    Returns {entity_id: set(label_id)}. Uncommitted areas contribute nothing, so a
    suggestion never changes HA until an installer commits it.
    """
    plan = {}
    for area, rec in (project or {}).get("areas", {}).items():
        if not rec.get("committed"):
            continue
        for e in _area_members(rec):
            plan.setdefault(e, set()).add(LABEL_AV)
        if rec.get("display"):
            plan.setdefault(rec["display"], set()).add(LABEL_DISPLAY)
        for e in (rec.get("sources") or []):
            plan.setdefault(e, set()).add(LABEL_SOURCE)
        for e in (rec.get("audio") or []):
            plan.setdefault(e, set()).add(LABEL_SPEAKER)
        if rec.get("tvaudio"):
            plan.setdefault(rec["tvaudio"], set()).add(LABEL_TVAUDIO)
        # Per-source input assignment -> proos_input_<slug> on the source entity.
        for src_e, inp in (rec.get("inputs") or {}).items():
            if src_e and inp:
                plan.setdefault(src_e, set()).add(_input_label(inp))
    return plan


# ── HA LABEL MIRROR (committed areas only) ───────────────────────────────────

def _existing_label_ids(client) -> set:
    try:
        rows = ws_command(client.base_url, client._token,
                          "config/label_registry/list")
        return {r.get("label_id") for r in (rows or []) if r.get("label_id")}
    except Exception:
        return set()


def _ensure_labels(client, label_ids) -> None:
    have = _existing_label_ids(client)
    for lid in sorted(label_ids):
        if lid in have:
            continue
        try:
            # HA slugs the name into the label_id; our ids are already valid slugs.
            ws_command(client.base_url, client._token,
                       "config/label_registry/create", name=lid)
        except Exception:
            pass


def mirror(client, project: dict) -> dict:
    """Reconcile HA entity labels to match the committed project.

    For every entity that either SHOULD carry a proos_* membership/role label or
    currently DOES, set its label set to (its non-proos labels) + (the desired proos
    labels). This adds labels to freshly-committed members and removes them from
    de-committed ones, while never touching unrelated labels (proos_pause_*, dashboard_*).
    """
    plan = label_plan(project)
    _ensure_labels(client, _ROLE_LABELS | {l for s in plan.values() for l in s})
    try:
        reg = client.entity_registry()
    except Exception as e:
        return {"ok": False, "error": "entity_registry unavailable: %s" % e, "changed": []}

    changed = []
    for ent in reg or []:
        eid = ent.get("entity_id")
        if not eid:
            continue
        cur = set(ent.get("labels") or [])
        want = plan.get(eid, set())
        # Skip entities that neither want nor currently carry any managed proos_* label.
        if not want and not any(_is_managed(l) for l in cur):
            continue
        desired = {l for l in cur if not _is_managed(l)} | want
        if desired != cur:
            try:
                ws_command(client.base_url, client._token,
                           "config/entity_registry/update",
                           entity_id=eid, labels=sorted(desired))
                changed.append(eid)
            except Exception:
                pass
    committed = [a for a, r in (project or {}).get("areas", {}).items()
                 if r.get("committed")]
    return {"ok": True, "changed": changed, "committed_areas": committed,
            "members": sorted(plan.keys())}
