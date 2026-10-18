"""
ProOS Core - dashboard layouts (register 960).

Dave, 19 Oct 2026: the wall panel is to be "the same as the app dashboards with support for adding devices and updating
dashboards", LOCKED on the wall and edited from the app or Pro; and "I did plan on having our own POE touchscreens as
opposed to apps so would this set the basis to support these also?". Until now a dashboard layout lived only in the
browser storage of the device that made it, so nothing could set another screen's layout. The layout now lives on the
box, in the same two levels the room order already uses (roomorder.py):

  { "default": { "<kind>": { "<page>": [widget, ...] } },
    "screens": { "<screen id>": { "kind": "<kind>", "name": "<optional>",
                                   "pages": { "<page>": [widget, ...] } } } }

  kind  - the kind of screen, the dashboard's own form-factor word: phone / tablet / panel
  page  - "home" or "room_<area_id>", the dashboard's own page key
  screen - the dashboard's own opaque device id (the one room order uses)

A screen resolves each page to its own layout if it has one, else its kind's home default, else nothing. A widget is
the dashboard's own record ({uid, id, ...}); Core does not interpret it - it keeps it whole, bounded in size. Never
raises to the caller.
"""
import json
import logging
import os
import re

_LOG = logging.getLogger("proos.layouts")
STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "layouts.json")
KINDS = ("phone", "tablet", "panel")
MAX_WIDGETS = 48          # the dashboard caps a page at 24; twice that is room to grow, not a hole
MAX_WIDGET_BYTES = 4096   # one widget's record; a camera or device widget is well under 1 KB
_PAGE = re.compile(r"^(home|room_[A-Za-z0-9_\-]{1,80})$")


def _kind(k) -> str:
    k = str(k or "")
    return k if k in KINDS else ""


def _page(p) -> str:
    p = str(p or "")
    return p if _PAGE.match(p) else ""


def _widgets(v) -> list:
    """A clean list of widget records: dicts only, each JSON-sized, at most MAX_WIDGETS."""
    if not isinstance(v, list):
        return []
    out = []
    for w in v:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        try:
            if len(json.dumps(w)) > MAX_WIDGET_BYTES:
                continue
        except Exception:  # noqa: BLE001 - not serialisable
            continue
        out.append(w)
        if len(out) >= MAX_WIDGETS:
            break
    return out


def _pages(v) -> dict:
    out = {}
    if isinstance(v, dict):
        for p, ws in v.items():
            pk = _page(p)
            if pk and isinstance(ws, list):
                out[pk] = _widgets(ws)
    return out


def load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:  # noqa: BLE001
        d = {}
    out = {"default": {}, "screens": {}}
    for k, pages in (d.get("default") or {}).items() if isinstance(d.get("default"), dict) else []:
        if _kind(k):
            out["default"][k] = _pages(pages)
    for sid, s in (d.get("screens") or {}).items() if isinstance(d.get("screens"), dict) else []:
        if isinstance(s, dict) and str(sid):
            out["screens"][str(sid)[:120]] = {"kind": _kind(s.get("kind")), "name": str(s.get("name") or "")[:60],
                                               "pages": _pages(s.get("pages"))}
    return out


def _write(doc: dict) -> dict:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, STORE)
    return doc


def resolve(kind: str = "", screen: str = "") -> dict:
    """What one screen shows, page by page: its own layout, else its kind's default. Every page either level knows."""
    doc = load()
    kind, screen = _kind(kind), str(screen or "")[:120]
    s = doc["screens"].get(screen) if screen else None
    if s and not kind:
        kind = s.get("kind") or ""
    dflt = doc["default"].get(kind, {}) if kind else {}
    pages, scopes = {}, {}
    for p, ws in dflt.items():
        pages[p], scopes[p] = ws, "default"
    for p, ws in ((s or {}).get("pages") or {}).items():
        pages[p], scopes[p] = ws, "screen"
    return {"kind": kind, "screen": screen, "name": (s or {}).get("name", ""), "pages": pages, "scopes": scopes,
            "default": dflt}


def save(kind: str, screen: str, page: str, widgets, scope: str = "screen", name: str = "") -> dict:
    """Write one page: this screen's own layout (scope "screen"), or its kind's home default (scope "default")."""
    try:
        kind, page, screen = _kind(kind), _page(page), str(screen or "")[:120]
        if not kind or not page:
            return {"error": "kind and page required"}
        doc = load()
        if scope == "default":
            doc["default"].setdefault(kind, {})[page] = _widgets(widgets)
        else:
            if not screen:
                return {"error": "screen required"}
            s = doc["screens"].setdefault(screen, {"kind": kind, "name": "", "pages": {}})
            s["kind"] = kind
            if name:
                s["name"] = str(name)[:60]
            s["pages"][page] = _widgets(widgets)
        _write(doc)
        return resolve(kind, screen)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("layout save failed: %s", exc)
        return {"error": str(exc)}


def clear(screen: str, page: str = "") -> dict:
    """Drop one screen's own layout for a page (or every page), so it follows its kind's default again."""
    try:
        screen = str(screen or "")[:120]
        doc = load()
        s = doc["screens"].get(screen)
        kind = (s or {}).get("kind", "")
        if s:
            if page:
                s["pages"].pop(_page(page), None)
            else:
                s["pages"] = {}
            _write(doc)
        return resolve(kind, screen)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("layout clear failed: %s", exc)
        return {"error": str(exc)}


def adopt(kind: str, screen: str, pages, name: str = "") -> dict:
    """COPY UP, NEVER OVERWRITE. A device's layouts from its own storage: each page becomes this screen's own layout
    where the box has none for it yet, and its kind's home default where that has none yet. Nothing already on the box
    is replaced, so adopting twice (or from two devices) can never lose a layout."""
    try:
        kind, screen = _kind(kind), str(screen or "")[:120]
        if not kind or not screen:
            return {"error": "kind and screen required"}
        doc = load()
        incoming = _pages(pages)
        s = doc["screens"].setdefault(screen, {"kind": kind, "name": "", "pages": {}})
        s["kind"] = s.get("kind") or kind
        if name and not s.get("name"):
            s["name"] = str(name)[:60]
        d = doc["default"].setdefault(kind, {})
        took = {"screen": [], "default": []}
        for p, ws in incoming.items():
            if p not in s["pages"]:
                s["pages"][p] = ws
                took["screen"].append(p)
            if p not in d:
                d[p] = ws
                took["default"].append(p)
        _write(doc)
        out = resolve(kind, screen)
        out["adopted"] = took
        return out
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("layout adopt failed: %s", exc)
        return {"error": str(exc)}


def summary() -> dict:
    """For Pro and for the reading on the box: how many pages each level holds."""
    doc = load()
    return {"default": {k: sorted(v.keys()) for k, v in doc["default"].items()},
            "screens": {sid: {"kind": s["kind"], "name": s["name"], "pages": sorted(s["pages"].keys())}
                        for sid, s in doc["screens"].items()}}
