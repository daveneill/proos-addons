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

# ── certified sync: write the harvested list INTO the integration ───────────
# androidtv_remote keeps its per-device app list in the config entry's
# "Applications List" options. Writing the harvested (true installed) apps in
# there makes HA itself expose source_list — every surface then works through
# the plain capability path, launch-safe, no special-casing anywhere.
# The options flow is driven via HA's REST flow API, parsing each step's
# returned schema ADAPTIVELY (field names/sentinels discovered, not assumed),
# so an upstream rename degrades to a clear error instead of corrupting.

def _fields(step: dict) -> dict:
    return {f.get("name"): f for f in (step.get("data_schema") or []) if isinstance(f, dict)}


def _select_info(f: dict):
    """(is_select, option_values) tolerant of both schema serialisations."""
    sel = ((f.get("selector") or {}).get("select")) or {}
    opts = sel.get("options")
    if opts is None and f.get("type") == "select":
        opts = f.get("options")
    if opts is None:
        return False, []
    vals = [o.get("value") if isinstance(o, dict) else (o[0] if isinstance(o, (list, tuple)) else o)
            for o in opts]
    return True, [str(v) for v in vals if v is not None]


def _is_bool(f: dict) -> bool:
    return f.get("type") == "boolean" or (f.get("selector") or {}).get("boolean") is not None


def write_applications(client, entry_id: str, apps: list) -> dict:
    """Drive the integration's options flow to add each {name, package}."""
    try:
        step = client._req("POST", "/api/config/config_entries/options/flow",
                           {"handler": entry_id}) or {}
    except Exception as e:  # noqa: BLE001
        return {"error": "couldn't open the integration's options: %s" % e}
    flow_id = step.get("flow_id")
    if not flow_id:
        return {"error": "integration options flow unavailable"}

    def _post(body):
        return client._req("POST", "/api/config/config_entries/options/flow/%s" % flow_id, body) or {}

    written = 0
    for app in apps:
        if step.get("type") == "create_entry":
            break
        fields = _fields(step)
        sel_name = new_val = None
        extras = {}
        for name, f in fields.items():
            is_sel, vals = _select_info(f)
            if is_sel:
                for v in vals:
                    if "new" in v.lower():
                        sel_name, new_val = name, v
                        break
            elif _is_bool(f) and "default" in f:
                extras[name] = f.get("default")
        if not (sel_name and new_val):
            return {"error": "the integration's apps flow looks different in this HA version — "
                             "sync aborted safely (nothing written)", "written": written}
        step = _post({sel_name: new_val, **extras})
        f2 = _fields(step)
        body2 = {}
        for name in f2:
            low = name.lower()
            if "app" in low and "id" in low:
                body2[name] = app["package"]
            elif "app" in low and "name" in low:
                body2[name] = app["name"]
        if not body2:
            return {"error": "unrecognised app-entry step — sync aborted", "written": written}
        step = _post(body2)
        if step.get("errors"):
            return {"error": "integration rejected %s: %s" % (app["name"], step["errors"]),
                    "written": written}
        written += 1
    # Finish: submit the menu WITHOUT selecting an app → options saved, entry reloads.
    if step.get("type") != "create_entry":
        fields = _fields(step)
        body = {n: f.get("default") for n, f in fields.items() if _is_bool(f) and "default" in f}
        step = _post(body)
    if step.get("type") != "create_entry":
        return {"error": "options didn't save (flow ended in '%s')" % step.get("type"),
                "written": written}
    return {"ok": True, "written": written}


def sync(client, entity_id: str, host: str, entry_id: str) -> dict:
    """The certified one-tap: harvest the box's true installed apps, then write
    them into the integration's Applications List. HA then exposes source_list
    natively — dashboards, assistant and shortcuts light up with no other step."""
    res = harvest(entity_id, host)
    if res.get("error"):
        return res
    apps = res.get("apps") or []
    w = write_applications(client, entry_id, apps)
    if w.get("error"):
        w["harvested"] = len(apps)
        return w
    return {"ok": True, "count": len(apps), "written": w.get("written", 0),
            "apps": apps}


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
    apps = []
    for pkg in pkgs[:60]:
        name, icon_url = _play_meta(pkg)
        if not name:
            name = _label_from_pkg(pkg)
        apps.append({"name": name, "package": pkg})
    apps.sort(key=lambda a: a["name"].lower())
    d = load()
    d[entity_id] = {"host": host, "ts": int(time.time()), "apps": apps}
    _write(d)
    return {"ok": True, "entity_id": entity_id, "count": len(apps), "apps": apps}
