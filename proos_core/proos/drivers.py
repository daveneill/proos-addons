"""OUR CERTIFIED DRIVERS RIDE INSIDE CORE (20 Oct 2026, register 969a; Dave's ruling the same day: "Ship inside Core").

Dave, 12:55: "There seems to be an issue with missing integrations that we built our versions of when we built the
image for a shipped unit — I'm missing the Samsung TV, Shelly, Coolink". The ProOS versions of the platform's drivers
lived only in the old box's <config>/custom_components (and copies in Downloads on the Mac); nothing put them on a new
box, so the G-Flip image shipped without any of them and Pro's certified tiles said "isn't installed".

Core now carries them in /app/drivers/<domain>/ and converges <config>/custom_components/<domain>/ to them on every boot
— the same law as the pages (register 273) and the remote door (351): install a file when it differs, leave it alone
when identical, touch nothing else, never block boot. A file Core installs reaches the platform only when the platform
next starts, so the state says so ("restart_needed") until it has — Pro offers the one restart.

The 12 drivers were copied off the old box on 20 Oct (all running there on the platform 2026.10.1); the remote door's
own `proos` component is not among them — door.py writes that one.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time

HA_CONFIG = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"
SRC = os.environ.get("PROOS_DRIVERS_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "drivers")
_SKIP = {"__pycache__", ".DS_Store"}

_last: dict = {}


def shipped(src: str | None = None) -> dict:
    """{domain: {"version": str, "files": {relpath: bytes}}} — what this Core carries."""
    import json
    root = src or SRC
    out: dict = {}
    if not os.path.isdir(root):
        return out
    for dom in sorted(os.listdir(root)):
        d = os.path.join(root, dom)
        if dom in _SKIP or not os.path.isdir(d):
            continue
        files: dict = {}
        for base, dirs, names in os.walk(d):
            dirs[:] = [x for x in dirs if x not in _SKIP]
            for n in names:
                if n in _SKIP or n.endswith(".pyc"):
                    continue
                p = os.path.join(base, n)
                with open(p, "rb") as fh:
                    files[os.path.relpath(p, d)] = fh.read()
        ver = ""
        try:
            ver = str(json.loads(files.get("manifest.json", b"{}").decode("utf-8")).get("version") or "")
        except Exception:  # noqa: BLE001
            pass
        out[dom] = {"version": ver, "files": files}
    return out


def install(config_dir: str | None = None, src: str | None = None) -> dict:
    """Converge each shipped driver into <config>/custom_components/<domain>/."""
    cc = os.path.join(config_dir or HA_CONFIG, "custom_components")
    result = {"dest": cc, "installed": [], "current": [], "errors": [], "versions": {}, "at": time.time()}
    for dom, drv in shipped(src).items():
        result["versions"][dom] = drv["version"]
        changed = False
        for rel, want in drv["files"].items():
            dst = os.path.join(cc, dom, rel)
            try:
                have = None
                if os.path.exists(dst):
                    with open(dst, "rb") as fh:
                        have = fh.read()
                if have == want:
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix="." + os.path.basename(rel) + ".")
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(want)
                    os.replace(tmp, dst)
                except Exception:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
                changed = True
            except Exception as e:  # noqa: BLE001
                result["errors"].append("%s/%s: %s" % (dom, rel, e))
        (result["installed"] if changed else result["current"]).append(dom)
    _last.clear()
    _last.update(result)
    return result


def fingerprint(src: str | None = None) -> str:
    """One hash of everything shipped — what the platform must have loaded."""
    h = hashlib.sha256()
    for dom, drv in shipped(src).items():
        for rel in sorted(drv["files"]):
            h.update(dom.encode() + b"/" + rel.encode() + b"\0" + drv["files"][rel])
    return h.hexdigest()[:16]


_cache: dict = {"at": 0.0, "state": None}


def state(ws_call=None, force: bool = False) -> dict:
    """What /health says: the drivers Core carries and, for each, whether the platform is RUNNING that version —
    asked of the platform itself (its loaded manifest, `manifest/get`), never assumed from the copy. A driver whose
    files are in place but whose version the platform does not report is waiting for the one restart."""
    now = time.time()
    if not force and _cache["state"] and now - _cache["at"] < 60:
        return _cache["state"]
    vers = (dict(_last).get("versions") if _last else None) or {d: v["version"] for d, v in shipped().items()}
    loaded, waiting, unknown = [], [], []
    for dom, want in sorted(vers.items()):
        if ws_call is None:
            unknown.append(dom)
            continue
        try:
            r = ws_call("manifest/get", integration=dom) or {}
            got = str((r if isinstance(r, dict) else {}).get("version") or "")
            (loaded if got == want else waiting).append(dom)
        except (ConnectionError, OSError, TimeoutError):
            unknown.append(dom)            # the platform did not answer — say so, never claim
        except Exception:  # noqa: BLE001
            waiting.append(dom)            # the platform answered: it has no such integration loaded yet
    words = ("ProOS drivers loaded (%d)" % len(loaded) if not waiting and not unknown else
             ("%d ProOS driver%s installed — restart the home engine once to load %s" % (len(waiting), "" if len(waiting) == 1 else "s", "it" if len(waiting) == 1 else "them")
              if waiting else "Can't tell which ProOS drivers the home engine has loaded"))
    st = {"versions": vers, "loaded": loaded, "waiting": waiting, "unknown": unknown,
          "installed_this_boot": (dict(_last).get("installed") if _last else []) or [],
          "errors": (dict(_last).get("errors") if _last else []) or [],
          "restart_needed": bool(waiting), "words": words}
    _cache.update(at=now, state=st)
    return st
