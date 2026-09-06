"""THE PROOS DOOR ON THE BOX'S OWN FRONT DOOR (7 Sep 2026, register 351;
Dave's ruling A on the three-device audit).

Both pages and the app reach Core by taking the address the page came
from and swapping the port to Core's. At home that host is the box, so
the port answers. Away from home the host is the remote address, which
forwards only the platform's own web port — nothing is behind Core's port
there, and every ProOS call waits on nothing ("Discovering devices…",
register 350). Nothing in a page or the app can route around that: the
BOX has to offer a door.

This module is that door, installed by Core itself: a small integration
written into the platform's config folder (Core already writes www/
there — provision.deploy_dashboards, register 273) that registers ONE
view on the platform's own web server,

    /api/proos/<anything>   →   ProOS Core inside the box,
                                same method, headers, body; streamed

so Core is reachable through whatever address the platform is reachable
through — the LAN, the platform's remote service, a dealer's https proxy.
Auth stays Core's: the door passes the request through untouched and
Core keeps deciding, exactly as it does on the LAN today. The integration
names nothing of the platform's anywhere a person reads.

THE ONE RESTART. The platform only sees a new integration after it
restarts once — Core never restarts the platform (doctrine 323), so the
deploy restart covers it and the door's state SAYS SO until then. After
that restart Core opens the door itself (one config entry, made through
the platform's own flow API) — nothing for anyone to click.

The rule for the files is register 273's: install when different, leave
alone when identical, touch nothing else, never block boot. The door's
own build (DOOR_BUILD) is stamped in the files and answered by the door,
so "installed but not yet the one that is running" is a served fact.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import time

# Bumped ONLY when the integration's own code below changes — a Core
# update that leaves the door alone must not ask for a restart.
DOOR_BUILD = "1"
DOMAIN = "proos"
DOOR_FILES = ("manifest.json", "__init__.py", "config_flow.py")

# How long a non-live answer is trusted before the door is asked again
# (PATIENCE, ledger): after Dave's one restart, /health flips to "open"
# within this window without a Core restart.
DOOR_RECHECK_S = 60

HA_CONFIG = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"

try:
    from proos import journal as _journal
except Exception:  # noqa: BLE001 — never block boot
    _journal = None


# ── where Core is, from inside the box ─────────────────────────────────────
def core_address(port: int | None = None) -> str:
    """The address the platform's container reaches THIS Core at: the
    add-on's own hostname on the box's internal network, Core's port."""
    host = os.environ.get("PROOS_DOOR_HOST") or socket.gethostname() or "127.0.0.1"
    p = port or int(os.environ.get("PROOS_PORT") or 8770)
    return "http://%s:%d" % (host, p)


# ── the integration, as text ───────────────────────────────────────────────
_MANIFEST = {
    "domain": DOMAIN,
    "name": "ProOS",
    "version": "1.0." + DOOR_BUILD,
    "codeowners": ["@protechsystems"],
    "config_flow": True,
    "dependencies": ["http"],
    "documentation": "https://protechsystems.com.au",
    "integration_type": "service",
    "iot_class": "local_push",
    "requirements": [],
    "single_config_entry": True,
}

_INIT = '''"""ProOS — the door to ProOS Core on this box's own front door.

Written by ProOS Core (proos/door.py). Everything under /api/proos/ is
handed to Core inside the box — same method, headers and body, the reply
streamed back — so ProOS is reachable through any address this box is.
"""
from __future__ import annotations

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

DOMAIN = "proos"
DOOR_BUILD = "%(build)s"
CORE = "%(core)s"

# Hop-by-hop and framing headers are the door's own business, never copied.
_NOT_COPIED = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "upgrade", "proxy-authorization", "proxy-connection", "te", "trailer",
    "content-encoding",
}


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    if not hass.data.get(DOMAIN):
        hass.http.register_view(ProOSDoor(hass))
        hass.data[DOMAIN] = True
    return True


async def async_unload_entry(hass, entry):
    # A registered route cannot be taken back until the next start; the
    # door simply stays. Nothing else was set up.
    return True


class ProOSDoor(HomeAssistantView):
    url = "/api/proos/{tail:.*}"
    name = "api:proos"
    requires_auth = False      # Core is the gate, exactly as on the LAN
    cors_allowed = True

    def __init__(self, hass):
        self._hass = hass

    async def get(self, request, tail):
        return await self._through(request, tail)

    async def post(self, request, tail):
        return await self._through(request, tail)

    async def put(self, request, tail):
        return await self._through(request, tail)

    async def patch(self, request, tail):
        return await self._through(request, tail)

    async def delete(self, request, tail):
        return await self._through(request, tail)

    async def _through(self, request, tail):
        if tail == "":
            # The door itself answers: which build of it is running.
            return self.json({"proos_door": DOOR_BUILD})
        session = async_get_clientsession(self._hass)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _NOT_COPIED}
        body = await request.read()
        url = CORE + "/" + tail
        if request.query_string:
            url += "?" + request.query_string
        try:
            up = await session.request(
                request.method, url, headers=headers, data=body or None,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=None))
        except Exception:  # noqa: BLE001 — the reason is Core's silence, said in words
            return self.json({"error": "ProOS Core is not answering"}, status_code=502)
        out = web.StreamResponse(
            status=up.status,
            headers={k: v for k, v in up.headers.items() if k.lower() not in _NOT_COPIED})
        await out.prepare(request)
        try:
            async for chunk in up.content.iter_any():
                await out.write(chunk)
        finally:
            up.release()
        await out.write_eof()
        return out
'''

_FLOW = '''"""ProOS — one entry, made by ProOS Core itself; nothing to fill in."""
from __future__ import annotations

from homeassistant import config_entries

DOMAIN = "proos"


class ProOSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="ProOS", data={})
'''


def files(core_url: str | None = None) -> dict:
    """The three files, as bytes, for THIS box (Core's own address inside)."""
    core = core_url or core_address()
    return {
        "manifest.json": (json.dumps(_MANIFEST, indent=2) + "\n").encode("utf-8"),
        "__init__.py": (_INIT % {"build": DOOR_BUILD, "core": core}).encode("utf-8"),
        "config_flow.py": _FLOW.encode("utf-8"),
    }


def install(config_dir: str | None = None, core_url: str | None = None) -> dict:
    """Converge <config>/custom_components/proos to the door's files —
    install when different, leave alone when identical, touch nothing
    else, never block boot (register 273's law, the same code shape)."""
    dest_dir = os.path.join(config_dir or HA_CONFIG, "custom_components", DOMAIN)
    result = {"dest": dest_dir, "installed": [], "current": [], "errors": []}
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        result["errors"].append("mkdir %s failed: %s" % (dest_dir, e))
        return result
    for name, want in files(core_url).items():
        dst = os.path.join(dest_dir, name)
        try:
            have = None
            if os.path.exists(dst):
                with open(dst, "rb") as fh:
                    have = fh.read()
            if have == want:
                result["current"].append(name)
                continue
            fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix="." + name + ".")
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
            result["installed"].append(name)
        except Exception as e:  # noqa: BLE001
            result["errors"].append("%s: %s" % (name, e))
    if result["installed"] and _journal is not None:
        try:
            _journal.emit("site", "door_install",
                          {"installed": result["installed"], "build": DOOR_BUILD,
                           "dest": dest_dir})
        except Exception:  # noqa: BLE001
            pass
    return result


# ── is the door open? ──────────────────────────────────────────────────────
_WORDS = {
    "open": "Remote door open — ProOS reaches this box through its front door, at home and away.",
    "restart": "Remote door installed — restart the box once to open it.",
    "opening": "Remote door installed — opening it now.",
    "stale": "Remote door updated — restart the box once to run the new one.",
    "closed": "Remote door not installed.",
    "unreachable": "Remote door — can't tell yet; the box isn't answering.",
}


def probe(client) -> dict:
    """What the door itself says: {"answered": bool, "build": str|None}.
    A door that is not there gives the platform's 404 — 'answered' False."""
    try:
        got = client._req("GET", "/api/%s/" % DOMAIN)
    except RuntimeError as e:
        if "HTTP 404" in str(e) or "HTTP 405" in str(e):
            return {"answered": False, "build": None}
        raise
    build = (got or {}).get("proos_door") if isinstance(got, dict) else None
    return {"answered": build is not None, "build": build}


def _entries(client) -> list:
    entries = client._req("GET", "/api/config/config_entries/entry") or []
    return [e for e in entries if isinstance(e, dict) and e.get("domain") == DOMAIN]


def ensure_open(client, installed: dict | None = None) -> dict:
    """One evaluation, in words. Returns {"state", "words", "build",
    "running"}. States: open · opening · restart · stale · closed ·
    unreachable · error."""
    out = {"state": "unreachable", "build": DOOR_BUILD, "running": None}
    try:
        p = probe(client)
        if p["answered"] and p["build"] == DOOR_BUILD:
            out["state"], out["running"] = "open", p["build"]
        elif p["answered"]:
            out["state"], out["running"] = "stale", p["build"]
        elif installed is not None and installed.get("errors") and not (
                installed.get("installed") or installed.get("current")):
            out["state"] = "closed"
            out["reason"] = "; ".join(installed["errors"])
        elif _entries(client):
            # the entry exists but the door does not answer: the files are
            # newer than what the platform loaded — the restart it needs
            out["state"] = "restart"
        else:
            # no entry yet: open it through the platform's own flow. Refused
            # with 400/404 = the platform has not loaded the files yet.
            try:
                r = client._req("POST", "/api/config/config_entries/flow",
                                {"handler": DOMAIN, "show_advanced_options": False}) or {}
            except RuntimeError as e:
                if "HTTP 400" in str(e) or "HTTP 404" in str(e):
                    out["state"] = "restart"
                    return _worded(out)
                raise
            if r.get("type") in ("create_entry", "abort"):
                p2 = probe(client)
                if p2["answered"] and p2["build"] == DOOR_BUILD:
                    out["state"], out["running"] = "open", p2["build"]
                else:
                    out["state"] = "opening"
            else:
                out["state"] = "error"
                out["reason"] = "the platform's flow answered %r" % r.get("type")
    except Exception as e:  # noqa: BLE001
        out["state"] = "unreachable"
        out["reason"] = str(e)[:160]
    return _worded(out)


def _worded(out: dict) -> dict:
    if out["state"] == "error":
        out["words"] = "Remote door — could not be opened: %s." % out.get("reason", "unknown")
    elif out["state"] == "closed":
        out["words"] = "Remote door not installed — %s." % out.get("reason", "unknown")
    else:
        out["words"] = _WORDS[out["state"]]
    return out


_cache = {"at": 0.0, "state": None}


def state(client, installed: dict | None = None, force: bool = False) -> dict:
    """The door's state, re-asked at most every DOOR_RECHECK_S — /health
    reads this on every Pro load."""
    now = time.time()
    if not force and _cache["state"] and now - _cache["at"] < DOOR_RECHECK_S:
        return _cache["state"]
    s = ensure_open(client, installed)
    _cache.update(at=now, state=s)
    return s
