"""THE BOX MIRRORS PROOS CONNECT'S APP TILE LIBRARY — register 1350 (15 Nov 2026).

Dave 15 Nov 08:22: "Only from Connect as it keeps the dashboard our premium look not cheap logos thrown on buttons by
an installer that look bad." 08:23 "yes" to the plan.

ProOS Connect holds the ONE library (Core's own tile module, run there). Once Dave switches it on for every box
("live"), this box makes its stored tiles and its app records exactly Connect's: every tile Connect has, fetched when
its fingerprint differs; every tile Connect hasn't, removed; the brand store, hidden tiles, tile origins and retired apps
copied whole. Until then the box keeps what it has. The box keeps its own copy, so dashboards never wait on the internet,
and the ProOS pack in the Core image stays the first-boot set.

The box also tells Connect which of this home's apps still show a wordmark (wanted), and — once, for the move — can
offer its own uploaded tiles, which wait on Connect until Dave adds or discards them. Nothing is added to the library
from a box.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import threading
import urllib.request
import zipfile

from . import appart, connectkeys

STATE_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "tiles_sync.json")
_lock = threading.Lock()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _get_json(path):
    return connectkeys._get(path)


def _get_bytes(path):
    req = urllib.request.Request(connectkeys._url() + path, method="GET",
                                 headers=dict(connectkeys._creds(), **{"User-Agent": "proos-core"}))
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def _post(path, body):
    req = urllib.request.Request(connectkeys._url() + path, method="POST", data=json.dumps(body).encode(),
                                 headers=dict(connectkeys._creds(), **{"Content-Type": "application/json",
                                                                       "User-Agent": "proos-core"}))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def _write(path, data: bytes):
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _write_json(path, obj):
    _write(path, json.dumps(obj, indent=1, sort_keys=True).encode())


def _state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def sync(get_json=None, get_bytes=None):
    """Make this box's tiles and app records Connect's. Returns what changed (slugs), or why nothing did."""
    get_json, get_bytes = get_json or _get_json, get_bytes or _get_bytes
    with _lock:
        m = get_json("/v1/tiles/manifest") or {}
        if not m.get("live"):
            return {"live": False}
        want = {s: h for s, h in (m.get("tiles") or {}).items() if appart.slug(s) == s}
        up = appart._UPLOADED
        os.makedirs(up, exist_ok=True)
        have = {}
        for s in appart._ls(up):
            try:
                with open(os.path.join(up, s + ".png"), "rb") as fh:
                    have[s] = _sha(fh.read())
            except OSError:
                pass
        fetched, failed, removed = [], [], []
        for s, h in sorted(want.items()):
            if have.get(s) == h:
                continue
            try:
                data = get_bytes("/v1/tiles/file/%s.png" % s)
            except Exception:                                    # noqa: BLE001
                failed.append(s)
                continue
            if _sha(data) != h:                                  # what arrived is not what Connect listed
                failed.append(s)
                continue
            _write(os.path.join(up, s + ".png"), data)
            fetched.append(s)
        for s in sorted(set(have) - set(want)):
            try:
                os.remove(os.path.join(up, s + ".png"))
                removed.append(s)
            except OSError:
                pass
        _write_json(appart._HIDDEN, sorted(m.get("hidden") or []))
        _write_json(appart._ORIGIN, m.get("origins") or {})
        _write_json(appart._BRANDS, m.get("brands") or {})
        _write_json(appart._REMOVED, sorted(m.get("removed") or []))
        appart._CANON_INDEX = None                               # the app names follow Connect's brand store at once
        st = _state()
        st.update(live=True, tiles=len(want), last_fetched=fetched, last_removed=removed, failed=failed)
        _write_json(STATE_PATH, st)
        return {"live": True, "fetched": fetched, "removed": removed, "failed": failed, "tiles": len(want)}


def report_wanted(app_names, post=None):
    """Tell Connect which of this home's apps still show a wordmark — only when that list changed."""
    miss = [{"name": r["name"]} for r in (appart.audit(app_names).get("missing") or [])]
    key = _sha(json.dumps(sorted(r["name"] for r in miss)).encode())
    st = _state()
    if not miss or st.get("wanted_key") == key:
        return {"sent": 0}
    (post or _post)("/v1/tiles/wanted", {"apps": miss[:50]})
    st["wanted_key"] = key
    _write_json(STATE_PATH, st)
    return {"sent": len(miss[:50])}


def ask_for_app(name, domain="", ids=None, alias="", post=None):
    """From a room page: an installer met an app ProOS doesn't know. It goes to Connect's list, with what they typed."""
    app = {"name": (name or "").strip()[:80], "domain": (domain or "").strip()[:120],
           "ids": [str(i)[:80] for i in (ids or [])][:10], "alias": (alias or "").strip()[:80]}
    if not app["name"]:
        return {"error": "Give it a name."}
    return (post or _post)("/v1/tiles/wanted", {"apps": [app]})


def offer(post=None):
    """For the move, once: this box's own uploaded tiles (and the apps it was taught) go to Connect, to wait there."""
    names = sorted(appart._ls(appart._UPLOADED))
    if not names:
        return {"error": "no tiles of this box's own to send"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s in names:
            z.write(os.path.join(appart._UPLOADED, s + ".png"), s + ".png")
    res = (post or _post)("/v1/tiles/offer", {"zip_b64": base64.b64encode(buf.getvalue()).decode(),
                                              "brands": appart.brands()})
    return dict(res, names=names)


def status():
    return _state()
