"""
ProOS Core -- Android TV / Shield app harvester (network ADB).

The Shield's own phone app shows every installed app with perfect artwork
because it runs ON the box and reads the package manager. This module does the
same from Core over network ADB: connect once (the TV shows an "Allow USB
debugging?" prompt — tick Always allow), then harvest the box's TRUE installed
leanback app list. Each app's label + real icon come from its public Google
Play listing; icons are stored locally in /config/www/proos_apps/sq/ so every
dashboard shows the genuine artwork, offline, forever.

Store: /data/shield_apps.json  { entity_id: {host, ts, apps:[{name,package,icon}]} }
appctl consumes it: a harvested box lists its real apps (launched by package id)
even while asleep. Cleared by factory reset.

Setup on the box (once): Settings → Device Preferences → About → click Build 7x
→ Developer options → Network debugging ON.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import time
import urllib.request

_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "shield_apps.json")
_ADB_TIMEOUT = 20

# Launcher/system packages that aren't user apps — never surface these.
_IGNORE = re.compile(
    r"^(com\.android\.|com\.google\.android\.(tvlauncher|leanbacklauncher|tungsten|"
    r"tvrecommendations|katniss|backdrop|gsf|tts|inputmethod|providers)|"
    r"com\.nvidia\.(stats|diagtools|shieldtech|ota|beta)|android$)")


def _www_sq_dir() -> str:
    base = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"
    d = os.path.join(base, "www", "proos_apps", "sq")
    os.makedirs(d, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_") or "app"


# ── store ─────────────────────────────────────────────────────────────────────

def load() -> dict:
    try:
        with open(_STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, _STORE)


def apps_for(entity_id: str) -> list:
    """Harvested [{name, package, icon}] for an entity, [] if never harvested."""
    rec = load().get(entity_id) or {}
    return rec.get("apps") or []


def clear() -> None:
    try:
        if os.path.exists(_STORE):
            os.remove(_STORE)
    except Exception:
        pass


# ── adb ───────────────────────────────────────────────────────────────────────

def _run(args, timeout=_ADB_TIMEOUT):
    return subprocess.run(["adb"] + args, capture_output=True, text=True, timeout=timeout)


def _connect(host: str):
    """Connect (host[:5555]). Returns (serial, None) or (None, error)."""
    host = host.strip()
    if not host:
        return None, "host required"
    serial = host if ":" in host else host + ":5555"
    try:
        _run(["start-server"], timeout=15)
        r = _run(["connect", serial], timeout=15)
        txt = (r.stdout or "") + (r.stderr or "")
        if "connected" not in txt.lower():
            return None, "couldn't reach %s — is Network debugging on? (%s)" % (serial, txt.strip()[:120])
        # authorization check
        d = _run(["devices"], timeout=10).stdout or ""
        for line in d.splitlines():
            if line.startswith(serial):
                if "unauthorized" in line:
                    return None, ("the TV is asking to allow debugging — accept the prompt on "
                                  "screen (tick 'Always allow'), then harvest again")
                if "device" in line:
                    return serial, None
        return None, "adb sees no device at %s" % serial
    except FileNotFoundError:
        return None, "adb isn't installed in Core (update the add-on)"
    except subprocess.TimeoutExpired:
        return None, "adb timed out talking to %s" % serial
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _leanback_packages(serial: str) -> list:
    """Packages with a LEANBACK launcher activity — exactly the tiles the box's
    own home screen shows."""
    r = _run(["-s", serial, "shell", "cmd", "package", "query-activities", "--brief",
              "-a", "android.intent.action.MAIN",
              "-c", "android.intent.category.LEANBACK_LAUNCHER"], timeout=25)
    pkgs = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        m = re.match(r"^([a-zA-Z0-9_.]+)/", line)
        if m:
            p = m.group(1)
            if not _IGNORE.match(p) and p not in pkgs:
                pkgs.append(p)
    return pkgs


# ── Google Play metadata (label + real icon) ─────────────────────────────────

def _play_meta(pkg: str):
    """(label, icon_url) from the app's public Play listing; (None, None) for
    sideloaded/unlisted apps."""
    url = "https://play.google.com/store/apps/details?id=%s&hl=en&gl=AU" % pkg
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read(400000).decode("utf-8", "replace")
        name = None
        m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if m:
            name = re.sub(r"\s*[-–]\s*(Apps|Android Apps) on Google Play\s*$", "", m.group(1)).strip()
        icon = None
        m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        if m:
            icon = re.sub(r"=w\d+.*$", "", m.group(1)) + "=w512"
        return name, icon
    except Exception:
        return None, None


def _label_from_pkg(pkg: str) -> str:
    tail = pkg.split(".")[-1]
    return re.sub(r"[_\-]+", " ", tail).title()


def _save_icon(name: str, icon_url: str):
    """Download the Play icon once into /local/proos_apps/sq/<slug>.png so every
    dashboard renders the genuine artwork offline."""
    try:
        p = os.path.join(_www_sq_dir(), _slug(name) + ".png")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return True
        req = urllib.request.Request(icon_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(2_000_000)
        with open(p, "wb") as fh:
            fh.write(data)
        return True
    except Exception:
        return False


# ── the harvest ───────────────────────────────────────────────────────────────

def harvest(entity_id: str, host: str) -> dict:
    """Connect → true leanback app list → Play labels + icons → store. The
    harvested list makes the box a first-class app device even while asleep."""
    entity_id = (entity_id or "").strip()
    if not entity_id:
        return {"error": "entity_id required"}
    serial, err = _connect(host)
    if err:
        return {"error": err}
    pkgs = _leanback_packages(serial)
    if not pkgs:
        return {"error": "no TV apps found — is the box awake? (adb connected fine)"}
    apps, icons = [], 0
    for pkg in pkgs[:60]:
        name, icon_url = _play_meta(pkg)
        if not name:
            name = _label_from_pkg(pkg)
        icon = None
        if icon_url and _save_icon(name, icon_url):
            icon = "/local/proos_apps/sq/%s.png" % _slug(name)
            icons += 1
        apps.append({"name": name, "package": pkg, "icon": icon})
    apps.sort(key=lambda a: a["name"].lower())
    d = load()
    d[entity_id] = {"host": host, "ts": int(time.time()), "apps": apps}
    _write(d)
    return {"ok": True, "entity_id": entity_id, "count": len(apps), "icons": icons,
            "apps": apps}
