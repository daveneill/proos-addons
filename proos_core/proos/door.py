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
DOOR_BUILD = "7"
DOMAIN = "proos"
# REGISTER 928 (door build 3): conversation.py — "ProOS Assist", the platform's
# own way of plugging an assistant into its voice pipeline, so a room's own
# microphone (the G-Flip panel) reaches Core's Assist with its room.
# REGISTER 1259 (door build 6): presence.py — the box announces itself on the network as ProOS, through the
# platform's own announcer, and (only when Core says so) stops the platform announcing itself.
DOOR_FILES = ("manifest.json", "__init__.py", "config_flow.py", "conversation.py", "presence.py")
QUIET_FILE = "proos_quiet"            # in the platform's config folder: present = the platform stays unannounced

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
    "dependencies": ["http", "conversation", "zeroconf"],
    "after_dependencies": ["ssdp"],
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


def _keep(name):
    # Core's own cross-origin headers (needed on its bare port) never pass
    # the door: the platform's server adds its own, and REFUSES a reply that
    # already carries them (door build 2 — every POST through build 1 died
    # on that refusal).
    n = name.lower()
    return n not in _NOT_COPIED and not n.startswith("access-control-")


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    if not hass.data.get(DOMAIN):
        hass.http.register_view(ProOSDoor(hass))
        hass.data[DOMAIN] = True
    # Door build 3 (register 928): "ProOS Assist" — a room's own microphone
    # reaches Core's Assist through the platform's voice pipeline.
    await hass.config_entries.async_forward_entry_setups(entry, ["conversation"])
    # Door build 6 (register 1259): this box is found on the network as ProOS.
    try:
        from . import presence
        await presence.async_start(hass, entry)
    except Exception:  # noqa: BLE001 — never stop the door over the announcement
        pass
    return True


async def async_unload_entry(hass, entry):
    # A registered route cannot be taken back until the next start; the
    # door simply stays. The assistant is unloaded with the entry.
    try:
        from . import presence
        await presence.async_stop(hass)
    except Exception:  # noqa: BLE001
        pass
    return await hass.config_entries.async_unload_platforms(entry, ["conversation"])


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
        headers = {k: v for k, v in request.headers.items() if _keep(k)}
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
            headers={k: v for k, v in up.headers.items() if _keep(k)})
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


# ── REGISTER 928: ProOS Assist — a room's own microphone reaches Core ──────
# The platform's mechanism for plugging an assistant into its voice pipeline
# is a conversation agent. This one adds nothing Core does not already do:
# it hands the words to Core's /assist/chat with the asking device's own
# room (a microphone knows its room — register 103) and lets Core answer,
# act, and speak through that room's speaker exactly as for the apps.
_CONV = '''"""ProOS Assist — what is said to a room's own microphone goes to ProOS Core.

Written by ProOS Core (proos/door.py, register 928). The words, the room the
asking device is in, and a short-lived sign-in for the panel go to Core's
Assist inside the box; Core answers and acts as it does for the apps, and
speaks the answer through the room's own speaker. When it did, nothing is
said here a second time.
"""
from __future__ import annotations

import asyncio

import aiohttp
from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

CORE = "%(core)s"
PANEL_NAME = "ProOS Panel"
PANEL_GROUP = "system-users"          # not an administrator, not the owner
NOT_ANSWERING = "ProOS isn't answering right now."
TURN_S = 90                           # Core's slowest turn (an AI plan) fits
# A QUESTION KEEPS THE MICROPHONE OPEN (register 936). Dave, 18 Oct: "it would make sense to have mic open after
# a question … All needs to be done so go". The platform's own rule for its assistants: a reply that ends in a
# question continues the conversation, and the room's microphone listens again without the wake word. When Core
# spoke the question through the room's speaker, the platform cannot hear when that speaker finishes, so the
# turn is held for about as long as the question takes to say -- otherwise the microphone would open while the
# speaker is still asking and hear the question as the answer. TIMINGS (plumbing, not facts about the house):
SAY_START_S = 1.5                     # the speaker starting to talk
SAY_WORD_S = 0.4                      # one spoken word, at the local voice's pace
SAY_MAX_S = 15                        # never hold a turn longer than this


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ProOSAssist(hass, entry)])


class ProOSAssist(conversation.ConversationEntity):
    _attr_has_entity_name = False
    _attr_name = "ProOS Assist"
    _attr_unique_id = "proos_assist"

    def __init__(self, hass, entry):
        self.hass = hass
        self._entry = entry

    @property
    def supported_languages(self):
        return MATCH_ALL

    async def _panel_token(self):
        """A short-lived sign-in for the panel's own platform user. The user is
        a system user: it cannot sign in anywhere by itself; it is made once
        and remembered in this entry."""
        auth = self.hass.auth
        uid = self._entry.data.get("panel_user")
        user = await auth.async_get_user(uid) if uid else None
        if user is None:
            user = await auth.async_create_system_user(PANEL_NAME, group_ids=[PANEL_GROUP])
            self.hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, "panel_user": user.id})
        rt = next(iter(user.refresh_tokens.values()), None)
        if rt is None:
            rt = await auth.async_create_refresh_token(user)
        return auth.async_create_access_token(rt)

    def _room(self, device_id):
        """The asking device's own room in the platform's device registry."""
        if not device_id:
            return None
        dev = dr.async_get(self.hass).async_get(device_id)
        return (dev.area_id or None) if dev else None

    async def async_process(self, user_input):
        resp = intent.IntentResponse(language=user_input.language)
        body = {"text": user_input.text,
                "session": "panel-" + (user_input.device_id or user_input.conversation_id or "voice")}
        room = self._room(user_input.device_id)
        if room:
            body["area_id"] = room
            body["area_confidence"] = "certain"
        res = None
        try:
            token = await self._panel_token()
            session = async_get_clientsession(self.hass)
            async with session.post(
                    CORE + "/assist/chat", json=body,
                    headers={"Authorization": "Bearer " + token},
                    timeout=aiohttp.ClientTimeout(total=TURN_S)) as r:
                if r.status == 200:
                    res = await r.json(content_type=None)
        except Exception:  # noqa: BLE001 — Core's silence, said in words
            res = None
        reply = str(res.get("reply") or "") if isinstance(res, dict) else ""
        asks = reply.rstrip().endswith("?")
        if not isinstance(res, dict):
            resp.async_set_speech(NOT_ANSWERING)
        elif (res.get("spoke") or {}).get("sent"):
            resp.async_set_speech("")     # said once, through the room's speaker
        else:
            # Core's reply — or, when it answered with a problem instead
            # (register 928, read on the G-Flip: {"error": "Pro Assist AI is
            # not configured …"}), Core's own words for it. Never silence.
            resp.async_set_speech(res.get("reply") or res.get("error") or NOT_ANSWERING)
        if asks and isinstance(res, dict) and (res.get("spoke") or {}).get("sent"):
            # Door build 5 (register 970ww): Core measures the question's own audio and says how long it takes
            # (spoke.say_s); the words-per-second estimate is kept only for when it could not.
            say = (res.get("spoke") or {}).get("say_s")
            if not isinstance(say, (int, float)) or isinstance(say, bool) or say <= 0:
                say = SAY_START_S + SAY_WORD_S * len(reply.split())
            await asyncio.sleep(min(SAY_MAX_S, say))
        return conversation.ConversationResult(
            response=resp, conversation_id=user_input.conversation_id,
            continue_conversation=asks)
'''


# ── REGISTER 1259: ProOS on the network ───────────────────────────────────
# Dave, 8 Nov 2026: "my app is discovered by my app no other app will see it at all". Every app built for the platform
# finds a box by the platform's own network announcement (_home-assistant._tcp, and its SSDP self-announcement), and
# ProOS's own apps found it the same way. This announces the box under ProOS's own service type, with the same facts
# the platform announces (its addresses, its address for the apps, its name) — through the platform's own announcer,
# so nothing of ProOS listens on the network itself. While the config folder holds QUIET_FILE (written by Core only
# once ProOS's own apps look for ProOS — step 3), the platform's own announcements are withdrawn; the platform still
# LISTENS for devices (Sonos, Apple TV …) exactly as before — only its announcing of itself stops.
_PRESENCE = '''"""ProOS on the network — written by ProOS Core (proos/door.py, register 1259)."""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from zeroconf.asyncio import AsyncServiceInfo

from homeassistant.components import zeroconf as zc
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers.event import async_track_time_interval

PROOS_TYPE = "_proos._tcp.local."
PLATFORM_TYPE = "_home-assistant._tcp.local."
QUIET_FILE = "%(quiet)s"
CHECK = timedelta(seconds=60)
KEY = "proos_presence"
_LOG = logging.getLogger(__name__)


async def _platform_info(hass):
    """The platform's own announcement, built by the platform's own code."""
    from homeassistant.components.zeroconf import _async_get_local_service_info
    return await _async_get_local_service_info(hass)


async def _apply(hass, *_):
    st = hass.data.setdefault(KEY, {"proos": None, "quiet": False})
    try:
        aio = await zc.async_get_async_instance(hass)
        base = await _platform_info(hass)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("ProOS: the network announcer is not available: %%s", e)
        return
    if st["proos"] is None:
        props = dict(base.properties or {})
        props[b"proos"] = b"1"
        info = AsyncServiceInfo(PROOS_TYPE, name=base.name.replace(PLATFORM_TYPE, PROOS_TYPE), server=base.server,
                                addresses=list(base.addresses), port=base.port, properties=props)
        await aio.async_register_service(info, allow_name_change=True)
        st["proos"] = info
    quiet = os.path.exists(hass.config.path(QUIET_FILE))
    ssdp = (hass.data.get("ssdp") or {}).get("server") if isinstance(hass.data.get("ssdp"), dict) else None
    if quiet and not st["quiet"]:
        # withdraw what the platform ACTUALLY announced (looked up by its type — the platform may have been renamed on
        # the network when it announced), not a copy rebuilt by name
        try:
            for info in list(aio.zeroconf.registry.async_get_infos_type(PLATFORM_TYPE)):
                await aio.async_unregister_service(info)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("ProOS: the platform's announcement could not be withdrawn: %%s", e)
        if ssdp is not None:
            try:
                await ssdp.async_stop()
            except Exception as e:  # noqa: BLE001
                _LOG.warning("ProOS: the platform's SSDP announcement could not be stopped: %%s", e)
        st["quiet"] = True
    elif st["quiet"] and not quiet:
        try:
            if not aio.zeroconf.registry.async_get_infos_type(PLATFORM_TYPE):
                await aio.async_register_service(base, allow_name_change=True)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("ProOS: the platform's announcement could not be put back: %%s", e)
        if ssdp is not None:
            try:
                await ssdp._async_start_upnp_servers(None)
            except Exception:  # noqa: BLE001
                pass
        st["quiet"] = False


async def async_start(hass, entry):
    # Register 1262: the platform runs a PLAIN function given to its timer or its bus in a worker thread, and a worker
    # thread may not start a task (measured on the box 9 Nov: "calls hass.async_create_task from a thread other than
    # the event loop" — the minute check never ran again). Coroutine functions run on the platform's own loop.
    async def _tick(now):
        await _apply(hass)

    async def _go(*_):
        await _apply(hass)
        hass.data.setdefault(KEY, {})["unsub"] = async_track_time_interval(hass, _tick, CHECK)
    if hass.state is CoreState.running:
        await _go()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _go)


async def async_stop(hass):
    st = hass.data.get(KEY) or {}
    if st.get("unsub"):
        st["unsub"]()
    if st.get("proos") is not None:
        try:
            aio = await zc.async_get_async_instance(hass)
            await aio.async_unregister_service(st["proos"])
        except Exception:  # noqa: BLE001
            pass
    hass.data.pop(KEY, None)
'''


def files(core_url: str | None = None) -> dict:
    """The door's files, as bytes, for THIS box (Core's own address inside)."""
    core = core_url or core_address()
    return {
        "manifest.json": (json.dumps(_MANIFEST, indent=2) + "\n").encode("utf-8"),
        "__init__.py": (_INIT % {"build": DOOR_BUILD, "core": core}).encode("utf-8"),
        "config_flow.py": _FLOW.encode("utf-8"),
        "conversation.py": (_CONV % {"core": core}).encode("utf-8"),
        "presence.py": (_PRESENCE % {"quiet": QUIET_FILE}).encode("utf-8"),
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
    # PLAN H1 interim (3 Oct 2026; L-23): the door answering HERE is measured; "and away" was
    # not — on Dave's box away-from-home was never set up, and this line claimed it worked.
    "open": "Remote door open — ProOS answers through the box's own front door. Reaching it from "
            "away needs remote access, which has not been proven on this box.",
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
    # REGISTER 448: the WebSocket call the rest of Core uses. The REST index
    # answers a Supervisor-token caller with an EMPTY LIST (live_ha.py,
    # register 148), so this door could not tell "needs restart" from "no
    # entry" — it read every state as closed.
    entries = client.config_entries(DOMAIN) or []
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
