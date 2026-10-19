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
import time

_LOG = logging.getLogger("proos.layouts")
STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "layouts.json")
KINDS = ("phone", "tablet", "panel", "wall")
# register 962: wall panels (the G-Flip, ProOS's own PoE screens) are their own kind, edited from the app or Pro; a page
# no wall layout holds yet falls back to the iPad's — the layout the installer set up for a tablet-sized screen (937).
FALLBACK = {"wall": "tablet"}
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


def screen(sid: str) -> dict:
    """One screen's record as the box holds it, or {}."""
    return load()["screens"].get(str(sid or "")[:120]) or {}


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
                                               "pages": _pages(s.get("pages")),
                                               "shot": s.get("shot") if s.get("shot") in SHOTS else "",
                                               "settings": _settings(s.get("settings"))}
    return out


# register 964: a wall panel's own settings, set in Pro (Dave's mocks SS-W2, 19 Oct). Each is what a person chose; an
# absent key means "not set" and the panel keeps its own behaviour.
#   home_room  the area the panel belongs to (its mic's room is set to the same area in the platform's device registry)
#   mic        the platform's assist_satellite entity the panel uses
#   rooms      "all" (every room, opening on the home room) or "home" (the home room only)
#   rest       seconds before the resting screen; 0 = never
REST_CHOICES = (0, 30, 60, 120, 300)
_AREA = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
_SAT = re.compile(r"^assist_satellite\.[a-z0-9_]{1,120}$")


def _settings(v) -> dict:
    out = {}
    if not isinstance(v, dict):
        return out
    if isinstance(v.get("home_room"), str) and _AREA.match(v["home_room"]):
        out["home_room"] = v["home_room"]
    if isinstance(v.get("mic"), str) and _SAT.match(v["mic"]):
        out["mic"] = v["mic"]
    if v.get("rooms") in ("all", "home"):
        out["rooms"] = v["rooms"]
    if isinstance(v.get("rest"), int) and not isinstance(v.get("rest"), bool) and v["rest"] in REST_CHOICES:
        out["rest"] = v["rest"]
    return out


def set_settings(screen: str, settings) -> dict:
    """Merge a person's choices into one screen's settings. A key given as null clears it (back to the panel's own)."""
    try:
        screen = str(screen or "")[:120]
        doc = load()
        s = doc["screens"].get(screen)
        if not s:
            return {"error": "unknown screen"}
        cur = dict(s.get("settings") or {})
        for k, val in (settings or {}).items() if isinstance(settings, dict) else []:
            if val is None:
                cur.pop(k, None)
            elif k in _settings({k: val}):      # a choice Pro does not offer is refused; the old choice stays
                cur[k] = val
        s["settings"] = _settings(cur)
        _write(doc)
        return resolve(s.get("kind", ""), screen)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def forget(screen: str) -> dict:
    """Drop one screen's record - a panel taken off the wall, or an old copy of one (Pro, the installer's choice)."""
    try:
        screen = str(screen or "")[:120]
        doc = load()
        gone = doc["screens"].pop(screen, None) is not None
        if gone:
            _write(doc)
        _SEEN.pop(screen, None)
        return {"forgotten": gone}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# when each screen last asked for its layout (it asks on load and every minute) - kept in memory only, never written:
# "online" in Pro is this, read, not a guess. Gone on a Core restart until the screen next asks (within a minute).
_SEEN = {}


def seen(screen: str):
    if screen:
        _SEEN[str(screen)[:120]] = time.time()


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
    for p, ws in doc["default"].get(FALLBACK.get(kind, ""), {}).items():
        pages[p], scopes[p] = ws, "fallback"
    for p, ws in dflt.items():
        pages[p], scopes[p] = ws, "default"
    for p, ws in ((s or {}).get("pages") or {}).items():
        pages[p], scopes[p] = ws, "screen"
    return {"kind": kind, "screen": screen, "name": (s or {}).get("name", ""), "pages": pages, "scopes": scopes,
            "default": dflt, "settings": (s or {}).get("settings") or {}}


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


# register 963: how Core can take a real picture of a screen, said by the screen itself. "kiosk" = the box's own kiosk
# screen app (the G-Flip), reached on the box's internal network; ProOS's own PoE screens will add theirs. A screen that
# says none has no picture, and Pro says so.
SHOTS = ("kiosk",)


def register(kind: str, screen: str, name: str = "", shot: str = "") -> dict:
    """A screen says what it is (a wall panel names itself once), so the app and Pro can list it to edit. Its pages are
    untouched; its kind is what it says; a name given by a person is never replaced by a screen's own default."""
    try:
        kind, screen = _kind(kind), str(screen or "")[:120]
        if not kind or not screen:
            return {"error": "kind and screen required"}
        doc = load()
        s = doc["screens"].setdefault(screen, {"kind": kind, "name": "", "pages": {}})
        s["kind"] = kind
        if name and not s.get("name"):
            s["name"] = str(name)[:60]
        s["shot"] = shot if shot in SHOTS else ""
        _write(doc)
        return resolve(kind, screen)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("layout register failed: %s", exc)
        return {"error": str(exc)}


def rename(screen: str, name: str) -> dict:
    try:
        screen = str(screen or "")[:120]
        doc = load()
        s = doc["screens"].get(screen)
        if not s:
            return {"error": "unknown screen"}
        s["name"] = str(name or "")[:60]
        _write(doc)
        return resolve(s.get("kind", ""), screen)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def summary() -> dict:
    """For Pro and for the reading on the box: how many pages each level holds."""
    doc = load()
    return {"default": {k: sorted(v.keys()) for k, v in doc["default"].items()},
            "screens": {sid: {"kind": s["kind"], "name": s["name"], "pages": sorted(s["pages"].keys()), "shot": s.get("shot", ""),
                              "settings": s.get("settings") or {},
                              "seen_ago": (round(time.time() - _SEEN[sid]) if sid in _SEEN else None)}
                        for sid, s in doc["screens"].items()}}
