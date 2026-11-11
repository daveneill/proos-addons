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

# HANDED BACK TO THE PLATFORM (register 1298, 11 Nov 2026 — the design's item 4; Dave 19:53 "Just fix it all please").
# A ProOS copy keeps the platform's own name, so it REPLACES the platform's integration and the platform's updates for it
# stop arriving. These copies only added a "<device> Connection" sensor, which Health no longer needs (1297: the UniFi
# network witness). On every boot Core MOVES a retired copy out of custom_components — to <config>/proos_retired_drivers,
# never deleted — so the platform's own integration loads at its next restart. Only a ProOS copy is moved: a folder
# whose manifest version carries "+proos".
RETIRED = ("wiz", "lifx", "shelly", "heos", "denonavr", "sonos", "unifi", "elkm1")   # 1298 WiZ; 1299 the other seven
RETIRED_DIR = "proos_retired_drivers"


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
    result["retired"] = retire(config_dir)
    _last.clear()
    _last.update(result)
    return result


def _is_proos_copy(folder: str) -> bool:
    import json
    try:
        with open(os.path.join(folder, "manifest.json"), "rb") as fh:
            return "+proos" in str(json.loads(fh.read().decode("utf-8")).get("version") or "")
    except Exception:  # noqa: BLE001
        return False


def retire(config_dir: str | None = None, domains=None) -> list:
    """Move each retired ProOS copy out of custom_components (register 1298). Returns what was moved, and where."""
    base = config_dir or HA_CONFIG
    cc = os.path.join(base, "custom_components")
    moved = []
    for dom in (RETIRED if domains is None else domains):
        src = os.path.join(cc, dom)
        if not os.path.isdir(src) or not _is_proos_copy(src):
            continue
        dst = os.path.join(base, RETIRED_DIR, "%s-%s" % (dom, time.strftime("%Y%m%d-%H%M%S")))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            moved.append({"domain": dom, "to": dst})
        except Exception as e:  # noqa: BLE001
            moved.append({"domain": dom, "error": str(e)})
    return moved


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
    # 1298: a retired copy is "back on the platform's own" once the platform reports its own integration (no "+proos")
    handed_back, still_copy = [], []
    for dom in RETIRED:
        if ws_call is None:
            continue
        try:
            r = ws_call("manifest/get", integration=dom) or {}
            got = str((r if isinstance(r, dict) else {}).get("version") or "")
            (still_copy if "+proos" in got else handed_back).append(dom)
        except Exception:  # noqa: BLE001
            pass
    waiting = waiting + still_copy
    words = ("ProOS drivers loaded (%d)" % len(loaded) if not waiting and not unknown else
             ("%d ProOS driver%s installed — restart the home engine once to load %s" % (len(waiting), "" if len(waiting) == 1 else "s", "it" if len(waiting) == 1 else "them")
              if waiting else "Can't tell which ProOS drivers the home engine has loaded"))
    st = {"versions": vers, "loaded": loaded, "waiting": waiting, "unknown": unknown,
          "installed_this_boot": (dict(_last).get("installed") if _last else []) or [],
          "errors": (dict(_last).get("errors") if _last else []) or [],
          "restart_needed": bool(waiting), "words": words,
          "handed_back": handed_back, "retired_waiting": still_copy,
          "retired_this_boot": (dict(_last).get("retired") if _last else []) or []}
    _cache.update(at=now, state=st)
    return st
