"""
ProOS Core -- what an INSTALLER may see of the home (register 338).

Dave's model, 5 Sep 2026: the Developer mirrors everything the platform
holds; the installer sees only what is published to them — certified
integrations always, plus whatever Tech has published (Core's catalog +
the published list). Register 336 applied that rule on the Integrations
page. This module applies THE SAME RULE to the room record, so a room page
mirrors the Integrations page for every tier:

  * veil(project)      — an installer's view of the commissioning project:
                         members, candidates, inputs, meta and folds of any
                         integration they cannot see are withheld, and each
                         room says how many were withheld (never silently
                         missing — the honesty rule).
  * unveil(posted, stored) — an installer's SAVE: everything withheld from
                         their view is put back from the stored record before
                         the save is validated, so an installer can never drop
                         a device they were not shown.
  * veil_devices(list) — the same gate for a list of devices carrying an
                         `integration` / `platform` field (the unassigned tray,
                         the room's non-AV devices).

Tech and the owner are never veiled. Pure functions; the gate is passed in.
"""
from __future__ import annotations
import copy

# The lists in a room record that hold entity ids.
_MEMBER_LISTS = ("sources", "audio", "speakers")


def gate_from_catalog(catalog_doc: dict, published) -> "callable":
    """Build is_visible(domain) from Core's catalog and the published list —
    the same rule Pro's _catVisible applies on the Integrations page:
    certified → always; hidden → never; anything else → only if published."""
    tiers = ((catalog_doc or {}).get("integrations") or {})
    pub = set(published or [])

    def is_visible(dom) -> bool:
        if not dom:
            return False
        t = ((tiers.get(dom) or {}).get("tier") or "").lower()
        if t == "certified":
            return True
        if t == "hidden":
            return False
        return dom in pub
    return is_visible


def _platform(rec: dict, entity: str, platform_of=None) -> str:
    m = ((rec or {}).get("meta") or {}).get(entity) or {}
    p = m.get("integration") or ""
    if not p and platform_of:
        try:
            p = platform_of(entity) or ""
        except Exception:
            p = ""
    return p


def _visible(rec: dict, entity: str, is_visible, platform_of=None) -> bool:
    return bool(entity) and is_visible(_platform(rec, entity, platform_of))


def veil_record(rec: dict, is_visible, platform_of=None) -> dict:
    """One room, as an installer sees it. Returns a new record."""
    if not isinstance(rec, dict):
        return rec
    out = copy.deepcopy(rec)
    hidden = set()

    def keep(e):
        ok = _visible(rec, e, is_visible, platform_of)
        if not ok and e:
            hidden.add(e)
        return ok

    if out.get("display") and not keep(out["display"]):
        out["display"] = None
    for k in _MEMBER_LISTS:
        if isinstance(out.get(k), list):
            out[k] = [e for e in out[k] if keep(e if isinstance(e, str) else (e or {}).get("entity"))]
    if out.get("tvaudio") and not keep(out["tvaudio"]):
        out["tvaudio"] = None
    sw = out.get("avswitch")
    if isinstance(sw, dict):
        if sw.get("entity") and not keep(sw["entity"]):
            out["avswitch"] = None
        elif isinstance(sw.get("inputs"), dict):
            sw["inputs"] = {e: v for e, v in sw["inputs"].items() if keep(e)}
    if isinstance(out.get("inputs"), dict):
        out["inputs"] = {e: v for e, v in out["inputs"].items() if keep(e)}
    cand = out.get("candidates")
    if isinstance(cand, dict):
        if cand.get("display") and not keep(cand["display"]):
            cand["display"] = None
        for k in ("sources", "audio"):
            if isinstance(cand.get(k), list):
                cand[k] = [e for e in cand[k] if keep(e)]
    if isinstance(out.get("folded"), dict):
        out["folded"] = {e: v for e, v in out["folded"].items() if keep(e) and keep(v)}
    if isinstance(out.get("meta"), dict):
        out["meta"] = {e: v for e, v in out["meta"].items() if e not in hidden and keep(e)}
    if isinstance(out.get("slots"), list):
        out["slots"] = [s for s in out["slots"] if not (isinstance(s, dict) and s.get("entity") in hidden)]
    out["withheld"] = len(hidden)
    return out


def veil(project: dict, is_visible, platform_of=None) -> dict:
    """The whole project, as an installer sees it. Returns a new dict."""
    if not isinstance(project, dict):
        return project
    out = copy.deepcopy(project)
    areas = out.get("areas")
    if isinstance(areas, dict):
        for k, rec in list(areas.items()):
            areas[k] = veil_record(rec, is_visible, platform_of)
    return out


def _restore_list(posted_list, stored_list, hidden_ids):
    """Put the hidden entries back, in their stored order where possible."""
    posted = list(posted_list or [])
    ids = {(x if isinstance(x, str) else (x or {}).get("entity")) for x in posted}
    for x in (stored_list or []):
        e = x if isinstance(x, str) else (x or {}).get("entity")
        if e in hidden_ids and e not in ids:
            posted.append(x)
            ids.add(e)
    return posted


def unveil_record(posted: dict, stored: dict, is_visible, platform_of=None) -> dict:
    """An installer's save of one room: everything the veil withheld from
    the stored record is put back. Returns a new record."""
    if not isinstance(stored, dict):
        return posted
    if not isinstance(posted, dict):
        return copy.deepcopy(stored)
    out = copy.deepcopy(posted)
    hidden = set()

    def is_hidden(e):
        return bool(e) and not _visible(stored, e, is_visible, platform_of)

    for e in [stored.get("display"), stored.get("tvaudio"), ((stored.get("avswitch") or {}).get("entity"))]:
        if is_hidden(e):
            hidden.add(e)
    for k in _MEMBER_LISTS:
        for x in (stored.get(k) or []):
            e = x if isinstance(x, str) else (x or {}).get("entity")
            if is_hidden(e):
                hidden.add(e)
    for e in list((stored.get("inputs") or {}).keys()) + list(((stored.get("avswitch") or {}).get("inputs") or {}).keys()):
        if is_hidden(e):
            hidden.add(e)
    for e, v in (stored.get("folded") or {}).items():     # a fold's halves are members too
        for x in (e, v):
            if is_hidden(x):
                hidden.add(x)
    if not hidden:
        out.pop("withheld", None)
        return out
    # the display and the switch: the installer never saw them, so an empty
    # slot in the post means "unchanged", not "removed"
    if stored.get("display") in hidden and not out.get("display"):
        out["display"] = stored["display"]
    if stored.get("tvaudio") in hidden and not out.get("tvaudio"):
        out["tvaudio"] = stored["tvaudio"]
    ssw = stored.get("avswitch") or {}
    if ssw.get("entity") in hidden and not (out.get("avswitch") or {}).get("entity"):
        out["avswitch"] = copy.deepcopy(ssw)
    for k in _MEMBER_LISTS:
        out[k] = _restore_list(out.get(k), stored.get(k), hidden)
    inputs = dict(out.get("inputs") or {})
    for e, v in (stored.get("inputs") or {}).items():
        if e in hidden:
            inputs[e] = v
    out["inputs"] = inputs
    if isinstance(out.get("avswitch"), dict):
        sin = dict((out["avswitch"].get("inputs") or {}))
        for e, v in (ssw.get("inputs") or {}).items():
            if e in hidden:
                sin[e] = v
        out["avswitch"]["inputs"] = sin
    meta = dict(out.get("meta") or {})
    for e, v in (stored.get("meta") or {}).items():
        if e in hidden and e not in meta:
            meta[e] = v
    out["meta"] = meta
    folded = dict(out.get("folded") or {})
    for e, v in (stored.get("folded") or {}).items():
        if e in hidden or v in hidden:
            folded[e] = v
    if folded:
        out["folded"] = folded
    out.pop("withheld", None)
    return out


def unveil(posted: dict, stored: dict, is_visible, platform_of=None) -> dict:
    """An installer's save of the whole project. A room the installer's post
    does not carry at all is kept as stored — they cannot delete what they
    did not see."""
    posted = posted if isinstance(posted, dict) else {}
    stored = stored if isinstance(stored, dict) else {}
    out = copy.deepcopy(posted)
    out.setdefault("areas", {})
    p_areas = out["areas"] if isinstance(out["areas"], dict) else {}
    for k, srec in (stored.get("areas") or {}).items():
        if k in p_areas:
            p_areas[k] = unveil_record(p_areas[k], srec, is_visible, platform_of)
        else:
            p_areas[k] = copy.deepcopy(srec)
    for k, prec in list(p_areas.items()):
        if isinstance(prec, dict):
            prec.pop("withheld", None)
    out["areas"] = p_areas
    return out


def veil_devices(devices: list, is_visible, key: str = "integration") -> list:
    """A device list (each row carrying its integration under `key`), as an
    installer sees it."""
    out = []
    for d in (devices or []):
        if not isinstance(d, dict):
            continue
        dom = d.get(key) or d.get("platform") or ""
        if is_visible(dom):
            out.append(d)
    return out
