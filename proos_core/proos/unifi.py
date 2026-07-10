"""
proos/unifi.py — Certified UniFi Protect integration (ProCore proxy).

The dashboard talks to ProCore; ProCore holds the local UniFi API key and proxies
the UniFi console so (a) the browser never handles the key, (b) there's no CORS,
and (c) Home Assistant stays invisible. This is the foundation for the full Protect
app-in-dashboard, and the same shape is reused for Access / Network / Alarm.

Two upstream surfaces on the console (both local, one key):
  • Official integration API  → https://{host}/proxy/protect/integration/v1/...
      Cameras (+PTZ, talkback, snapshot, RTSPS), live views, sensors, lights,
      sirens, chimes, speakers, relays, viewers, fobs, bridges, alarm-hubs,
      arm-profiles, meta/info, and /subscribe/{events,devices} realtime streams.
  • Private API               → https://{host}/proxy/protect/api/...
      Historical event SEARCH, event THUMBNAILS, and VIDEO EXPORT (clips) — not
      exposed by the official v1 API yet, so uiprotect/this module fall back here.

Auth: header  X-API-KEY: <local key>  (Protect → Settings → Control Plane →
Integrations → create API key). Console cert is self-signed → TLS unverified.

Credential storage (CredentialStore, service "unifi_protect"):
  token = API key ; meta = {"host": "<console ip/host>", "verify_ssl": false}
"""
from __future__ import annotations
import json
import os
import ssl
import urllib.request
import urllib.parse
import urllib.error
import re
import threading
import time

SERVICE = "unifi_protect"
INTEGRATION = "/proxy/protect/integration"
PRIVATE = "/proxy/protect/api"

# Integration GET endpoints safe to expose read-only as generic passthroughs.
_GET_COLLECTIONS = {
    "cameras", "sensors", "lights", "sirens", "chimes", "speakers", "relays",
    "viewers", "fobs", "bridges", "alarm-hubs", "arm-profiles", "liveviews",
    "nvrs", "users", "ulp-users", "link-stations",
}

_ssl_unverified = ssl.create_default_context()
_ssl_unverified.check_hostname = False
_ssl_unverified.verify_mode = ssl.CERT_NONE


class ProtectError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ProtectClient:
    """Thin authenticated proxy to a local UniFi Protect console."""

    def __init__(self, creds):
        self._creds = creds
        self._session = None  # {"token","csrf"} for the private API (UniFi OS login)
        self._login_lock = threading.Lock()
        self._blocked_until = 0.0  # cooldown after a 429 so we don't hammer the console

    def _ensure_session(self):
        # Reuse the cached session; only log in once (guarded so concurrent
        # requests don't each trigger a login — that's what tripped the 429).
        if self._session:
            return self._session
        if time.time() < self._blocked_until:
            raise ProtectError(429, "login rate-limited by console — retry shortly")
        with self._login_lock:
            if self._session:
                return self._session
            if time.time() < self._blocked_until:
                raise ProtectError(429, "login rate-limited by console — retry shortly")
            return self._session_login()

    # ---- config ----
    def _conf(self):
        key = self._creds.get(SERVICE)
        meta = self._creds.meta(SERVICE) or {}
        return (key, meta.get("host"), bool(meta.get("verify_ssl")),
                meta.get("username"), meta.get("password"))

    def configured(self) -> bool:
        key, host, _, _, _ = self._conf()
        return bool(key and host)

    def status(self) -> dict:
        key, host, verify, user, pw = self._conf()
        out = {"configured": bool(key and host), "host": host, "verify_ssl": verify,
               "has_login": bool(user and pw), "username": user}
        if key and host:
            try:
                info = self._req("GET", INTEGRATION, "/v1/meta/info")
                out["reachable"] = True
                out["application_version"] = (info or {}).get("applicationVersion")
            except ProtectError as e:
                out["reachable"] = False
                out["error"] = e.message
            # private-API (event search / thumbnails / clips) needs the login session
            if user and pw:
                try:
                    self._ensure_session()
                    out["private_ok"] = True
                except ProtectError as e:
                    out["private_ok"] = False
                    out["private_error"] = e.message
        return out

    def set_config(self, host, api_key, verify_ssl=False, username=None, password=None) -> dict:
        meta = self._creds.meta(SERVICE) or {}
        host = (host or meta.get("host") or "").strip().rstrip("/").replace("https://", "").replace("http://", "")
        api_key = (api_key or self._creds.get(SERVICE) or "").strip()
        if not host or not api_key:
            raise ProtectError(400, "host and api_key are required")
        extra = {"verify_ssl": bool(verify_ssl)}
        # preserve existing local-user creds unless new ones are provided
        extra["username"] = username if username is not None else meta.get("username")
        extra["password"] = password if password is not None else meta.get("password")
        self._creds.put(SERVICE, api_key, name="UniFi Protect", host=host, extra=extra)
        self._session = None
        return self.status()

    # ---- private API session (UniFi OS local login) ----
    def _session_login(self):
        key, host, verify, user, pw = self._conf()
        if not (host and user and pw):
            raise ProtectError(409, "private API needs a local username/password")
        url = f"https://{host}/api/auth/login"
        data = json.dumps({"username": user, "password": pw, "rememberMe": True}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        ctx = None if verify else _ssl_unverified
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                token = None
                for c in (r.headers.get_all("Set-Cookie") or []):
                    m = re.search(r"(?:^|\s)TOKEN=([^;]+)", c)
                    if m:
                        token = m.group(1)
                csrf = r.headers.get("X-CSRF-Token") or r.headers.get("X-Updated-CSRF-Token")
                if not token:
                    raise ProtectError(401, "login returned no session token (use a LOCAL user, not SSO)")
                self._session = {"token": token, "csrf": csrf}
                return self._session
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self._blocked_until = time.time() + 60
                raise ProtectError(429, "console rate-limited the login — waiting 60s before retry")
            raise ProtectError(e.code, f"login failed: {e.code} (local user + password, not SSO)")
        except urllib.error.URLError as e:
            raise ProtectError(502, f"cannot reach console for login: {e.reason}")

    # ---- transport ----
    def _req(self, method: str, base: str, path: str, *, body=None,
             query: dict | None = None, raw: bool = False, timeout: int = 20, _retry: bool = True):
        key, host, verify, user, pw = self._conf()
        if not (key and host):
            raise ProtectError(409, "UniFi Protect is not configured")
        url = f"https://{host}{base}{path}"
        if query:
            q = urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")}, doseq=True)
            if q:
                url += "?" + q
        data = None
        headers = {"Accept": "application/json"}
        private = base == PRIVATE
        if private:
            # private /proxy/protect/api/* uses the UniFi OS login session, not the API key
            self._ensure_session()
            headers["Cookie"] = f"TOKEN={self._session['token']}"
            if self._session.get("csrf"):
                headers["X-CSRF-Token"] = self._session["csrf"]
        else:
            headers["X-API-KEY"] = key
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        ctx = None if verify else _ssl_unverified
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                payload = r.read()
                if raw:
                    return payload, r.headers.get("Content-Type", "application/octet-stream")
                if not payload:
                    return {}
                return json.loads(payload.decode())
        except urllib.error.HTTPError as e:
            if private and e.code in (401, 403) and _retry:
                # session likely expired — re-login once and retry
                self._session = None
                self._ensure_session()
                return self._req(method, base, path, body=body, query=query,
                                 raw=raw, timeout=timeout, _retry=False)
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            raise ProtectError(e.code, f"upstream {e.code}: {detail or e.reason}")
        except urllib.error.URLError as e:
            raise ProtectError(502, f"cannot reach console: {e.reason}")
        except ssl.SSLError as e:
            raise ProtectError(502, f"TLS error: {e}")

    # ---- cameras ----
    def cameras(self):
        return self._req("GET", INTEGRATION, "/v1/cameras")

    def camera(self, cid):
        return self._req("GET", INTEGRATION, f"/v1/cameras/{cid}")

    def snapshot(self, cid, high=True):
        return self._req("GET", INTEGRATION, f"/v1/cameras/{cid}/snapshot",
                         query={"highQuality": "true" if high else "false"}, raw=True)

    def rtsps_get(self, cid):
        return self._req("GET", INTEGRATION, f"/v1/cameras/{cid}/rtsps-stream")

    def rtsps_create(self, cid, qualities=None):
        return self._req("POST", INTEGRATION, f"/v1/cameras/{cid}/rtsps-stream",
                         body={"qualities": qualities or ["high"]})

    def ptz_goto(self, cid, slot):
        return self._req("POST", INTEGRATION, f"/v1/cameras/{cid}/ptz/goto/{slot}")

    def ptz_patrol_start(self, cid, slot):
        return self._req("POST", INTEGRATION, f"/v1/cameras/{cid}/ptz/patrol/start/{slot}")

    def ptz_patrol_stop(self, cid):
        return self._req("POST", INTEGRATION, f"/v1/cameras/{cid}/ptz/patrol/stop")

    def talkback(self, cid):
        return self._req("POST", INTEGRATION, f"/v1/cameras/{cid}/talkback-session")

    # ---- alarm / arm profiles ----
    def arm_profiles(self):
        return self._req("GET", INTEGRATION, "/v1/arm-profiles")

    def arm_enable(self):
        return self._req("POST", INTEGRATION, "/v1/arm-profiles/enable")

    def arm_disable(self):
        return self._req("POST", INTEGRATION, "/v1/arm-profiles/disable")

    def set_arm_profile(self, profile_id):
        return self._req("PATCH", INTEGRATION, "/v1/arm-profiles/settings",
                         body={"armProfileId": profile_id})

    # ---- sirens ----
    def siren_play(self, sid):
        return self._req("POST", INTEGRATION, f"/v1/sirens/{sid}/play")

    def siren_stop(self, sid):
        return self._req("POST", INTEGRATION, f"/v1/sirens/{sid}/stop")

    # ---- generic read-only collection passthrough ----
    def collection(self, name):
        if name not in _GET_COLLECTIONS:
            raise ProtectError(404, f"unknown collection {name}")
        return self._req("GET", INTEGRATION, f"/v1/{name}")

    def meta_info(self):
        return self._req("GET", INTEGRATION, "/v1/meta/info")

    # ---- private API: historical events, thumbnails, clips ----
    def events(self, start=None, end=None, types=None, cameras=None, limit=None):
        return self._req("GET", PRIVATE, "/events",
                         query={"start": start, "end": end, "types": types,
                                "cameras": cameras, "limit": limit})

    def event_thumbnail(self, event_id):
        return self._req("GET", PRIVATE, f"/events/{event_id}/thumbnail", raw=True)

    def video_export(self, camera, start, end, kind=None, fps=None):
        # private API: GET /proxy/protect/api/video/export?camera=&start=&end= → MP4 bytes
        return self._req("GET", PRIVATE, "/video/export", raw=True, timeout=120,
                         query={"camera": camera, "start": start, "end": end,
                                "type": kind, "fps": fps})


# ---------------------------------------------------------------------------
# HTTP dispatch — called from server.py do_GET / do_POST when parts[0]=="unifi".
# Returns (status:int, content_type:str, payload: bytes|dict).
# `parts` is the path after "unifi" (e.g. ["protect","cameras"]); qs is a dict of
# already-parsed query params (lists); body is the parsed JSON dict (or None).
# ---------------------------------------------------------------------------
# One reused client (and thus one reused login session) across requests — creating a
# fresh client per request logs in every time and trips the console's 429 rate limit.
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _get_client(creds):
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = ProtectClient(creds)
    return _CLIENT


def handle(method, parts, qs, body, creds):
    if not parts or parts[0] != "protect":
        return 404, "application/json", {"errorMessage": "not found"}
    p = parts[1:]
    client = _get_client(creds)

    def q1(k, default=None):
        v = qs.get(k)
        return v[0] if isinstance(v, list) and v else (v if v not in (None, []) else default)

    try:
        # config / status
        if p == ["status"] and method == "GET":
            return 200, "application/json", client.status()
        if p == ["config"] and method == "POST":
            b = body or {}
            return 200, "application/json", client.set_config(
                b.get("host"), b.get("api_key") or b.get("apiKey"), b.get("verify_ssl", False),
                username=b.get("username"), password=b.get("password"))

        if not client.configured():
            return 409, "application/json", {"errorMessage": "UniFi Protect not configured"}

        # meta
        if p == ["info"] and method == "GET":
            return 200, "application/json", client.meta_info()

        # generic collections
        if len(p) == 1 and method == "GET" and p[0] in _GET_COLLECTIONS:
            return 200, "application/json", client.collection(p[0])

        # cameras
        if p and p[0] == "cameras":
            if len(p) == 1 and method == "GET":
                return 200, "application/json", client.cameras()
            if len(p) == 2 and method == "GET":
                return 200, "application/json", client.camera(p[1])
            if len(p) == 3 and p[2] == "snapshot" and method == "GET":
                data, ctype = client.snapshot(p[1], high=(q1("q", "high") != "low"))
                return 200, ctype, data
            if len(p) == 3 and p[2] == "rtsps" and method == "GET":
                return 200, "application/json", client.rtsps_get(p[1])
            if len(p) == 3 and p[2] == "rtsps" and method == "POST":
                return 200, "application/json", client.rtsps_create(p[1], (body or {}).get("qualities"))
            if len(p) == 3 and p[2] == "talkback" and method == "POST":
                return 200, "application/json", client.talkback(p[1])
            if len(p) >= 4 and p[2] == "ptz" and method == "POST":
                if p[3] == "goto" and len(p) == 5:
                    return 200, "application/json", client.ptz_goto(p[1], p[4])
                if p[3] == "patrol" and len(p) >= 5 and p[4] == "start" and len(p) == 6:
                    return 200, "application/json", client.ptz_patrol_start(p[1], p[5])
                if p[3] == "patrol" and len(p) == 5 and p[4] == "stop":
                    return 200, "application/json", client.ptz_patrol_stop(p[1])

        # arm profiles
        if p and p[0] == "arm-profiles":
            if len(p) == 1 and method == "GET":
                return 200, "application/json", client.arm_profiles()
            if p == ["arm-profiles", "enable"] and method == "POST":
                return 200, "application/json", client.arm_enable()
            if p == ["arm-profiles", "disable"] and method == "POST":
                return 200, "application/json", client.arm_disable()
            if p == ["arm-profiles", "settings"] and method in ("PATCH", "POST"):
                return 200, "application/json", client.set_arm_profile((body or {}).get("armProfileId"))

        # sirens
        if len(p) == 3 and p[0] == "sirens" and method == "POST":
            if p[2] == "play":
                return 200, "application/json", client.siren_play(p[1])
            if p[2] == "stop":
                return 200, "application/json", client.siren_stop(p[1])

        # events (private) + thumbnail + clip export
        if p == ["events"] and method == "GET":
            return 200, "application/json", client.events(
                start=q1("start"), end=q1("end"), types=q1("types"),
                cameras=q1("cameras"), limit=q1("limit"))
        if len(p) == 3 and p[0] == "events" and p[2] == "thumbnail" and method == "GET":
            data, ctype = client.event_thumbnail(p[1])
            return 200, ctype, data
        if p == ["video", "export"] and method == "POST":
            b = body or {}
            data, ctype = client.video_export(b.get("camera"), b.get("start"), b.get("end"),
                                              kind=b.get("type"), fps=b.get("fps"))
            return 200, ctype, data

        return 404, "application/json", {"errorMessage": f"unknown route: {method} /unifi/{'/'.join(parts)}"}
    except ProtectError as e:
        return e.status, "application/json", {"errorMessage": e.message}
    except Exception as e:  # noqa: BLE001 — never 500 the whole server
        return 500, "application/json", {"errorMessage": f"unifi proxy error: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# UnifiLayer — ProCore owns the *native* HA UniFi Protect integration
# ─────────────────────────────────────────────────────────────────────────────
# The native `unifiprotect` integration is what gives the dashboard its camera
# entities + go2rtc WebRTC. Its config entry is raised by HA **integration
# discovery** (HA finds the console on the LAN over SSDP), exactly like the Music
# Assistant add-on — so provisioning it mirrors proos.music.MusicLayer: detect the
# entry (REST), else find the discovery flow (WebSocket), and complete it (REST).
#
# The one difference from Music: MA's discovery flow carries its own token and is
# confirm-only; the UniFi discovery flow asks for the local API key. We read the
# flow's declared fields (get_flow) and submit only what it asks for, from the
# same key ProCore already holds for the private-API proxy — so the installer
# enters the console once, in ProCore, and both halves come up together.

UNIFI_DOMAIN = "unifiprotect"
CERTIFIED_TITLE = "UniFi Protect — ProOS Certified"

OK = "ok"
PENDING = "pending"
FAULT = "fault"
_CONFIRMABLE = ("discovery_confirm", "confirm", "hassio_confirm")


class UnifiLayer:
    """Provision + report on the native unifiprotect integration from ProCore."""

    def __init__(self, client):
        self.client = client  # RestHAClient (proos.live_ha)

    # ── Ownership ───────────────────────────────────────────────────────────
    def ensure_integration(self, api_key: str = "", username: str = "",
                           password: str = "") -> dict:
        """Bring the native unifiprotect integration up, idempotently.

        Returns a small dict for logs + POST /unifi/setup:
          action: noop | created | waiting | need_input | abort | form | error
          loaded: bool
        """
        try:
            entries = self._entries()
        except Exception as e:  # noqa: BLE001
            return {"action": "error", "loaded": False, "error": f"entries: {e}"}
        if entries:
            state = entries[0].get("state")
            self._stamp_title(entries[0].get("entry_id"),
                              entries[0].get("title"))
            return {"action": "noop", "loaded": state == "loaded",
                    "state": state, "entry_id": entries[0].get("entry_id")}

        # Not configured yet → find the discovery flow HA raised for the console.
        try:
            flows = self.client.flow_progress()
        except Exception as e:  # noqa: BLE001
            return {"action": "error", "loaded": False, "error": f"flow progress: {e}"}
        flow = next((f for f in flows if f.get("handler") == UNIFI_DOMAIN), None)
        if not flow:
            return {"action": "waiting", "loaded": False,
                    "detail": "No UniFi Protect discovery flow yet — is the console "
                              "reachable on the LAN so HA can discover it?"}

        flow_id = flow["flow_id"]
        # Read the current step so we submit exactly the fields it declares.
        try:
            step = self.client.get_flow(flow_id) or flow
        except Exception:
            step = flow
        payload = self._payload_for(step, api_key, username, password)

        # A confirm-only step (no fields) is completed with {}.
        try:
            res = self.client.configure_flow(flow_id, payload)
        except Exception as e:  # noqa: BLE001
            return {"action": "error", "loaded": False, "error": f"configure: {e}"}
        return self._interpret(res)

    def _payload_for(self, step: dict, api_key: str, username: str,
                     password: str) -> dict:
        """Map the creds ProCore holds onto whatever the flow step asks for.

        HA serialises a step's data_schema as a list of {name, type, required,...}.
        We only send keys the step declares — sending extras trips schema
        validation and bounces the form back.
        """
        names = set()
        for f in (step.get("data_schema") or []):
            n = f.get("name")
            if n:
                names.add(n)
        payload: dict = {}
        if api_key and ("api_key" in names or "api-key" in names):
            payload["api_key"] = api_key
        if username and "username" in names:
            payload["username"] = username
        if password and "password" in names:
            payload["password"] = password
        if "verify_ssl" in names:
            payload["verify_ssl"] = False
        # Some builds surface host/port on the discovery step pre-filled; leave
        # them to HA's discovery defaults unless the step demands them empty.
        return payload

    def _interpret(self, res: dict) -> dict:
        rtype = res.get("type")
        if rtype == "create_entry":
            for e in self._entries_safe():
                if e.get("domain") == UNIFI_DOMAIN:
                    self._stamp_title(e.get("entry_id"), e.get("title"))
                    break
            return {"action": "created", "loaded": True, "title": res.get("title")}
        if rtype == "abort":
            reason = res.get("reason")
            # already_configured is success (someone/HA finished it first).
            return {"action": "abort", "reason": reason,
                    "loaded": bool(self._entries_safe()),
                    "created": reason == "already_configured"}
        if rtype == "form":
            return {"action": "form", "loaded": False, "step": res.get("step_id"),
                    "errors": res.get("errors"),
                    "detail": "Discovery flow needs input we didn't supply — check the "
                              "API key / local user."}
        return {"action": rtype or "unknown", "loaded": False}

    # ── Awareness ───────────────────────────────────────────────────────────
    def report(self) -> dict:
        """Native-side health: configured? loaded? how many cameras?"""
        try:
            entries = self._entries()
        except Exception as e:  # noqa: BLE001
            return {"status": FAULT, "summary": "Cannot reach Home Assistant",
                    "loaded": False, "cameras": 0, "configured": False, "error": str(e)}
        if not entries:
            return {"status": FAULT, "summary": "Protect integration not set up",
                    "loaded": False, "cameras": 0, "configured": False}
        state = entries[0].get("state")
        if state != "loaded":
            return {"status": PENDING, "summary": f"Protect integration {state}",
                    "loaded": False, "cameras": 0, "configured": True, "state": state}
        try:
            cams = [e for e in self.client.integration_entities(UNIFI_DOMAIN)
                    if e.startswith("camera.")]
        except Exception:
            cams = []
        return {"status": OK, "summary": f"{len(cams)} camera(s)",
                "loaded": True, "cameras": len(cams), "configured": True,
                "entry_id": entries[0].get("entry_id")}

    # ── internals ───────────────────────────────────────────────────────────
    def _stamp_title(self, entry_id, current_title=None):
        """Rename the native entry to the certified title, once (idempotent)."""
        if not entry_id or current_title == CERTIFIED_TITLE:
            return
        try:
            self.client.set_entry_title(entry_id, CERTIFIED_TITLE)
        except Exception:
            pass

    def _entries(self) -> list:
        return self.client.config_entries(UNIFI_DOMAIN)

    def _entries_safe(self) -> list:
        try:
            return self._entries()
        except Exception:
            return []


def unified_status(protect_client, unifi_layer) -> dict:
    """One certified-integration status merging the two halves.

    native  → UnifiLayer.report()   (cameras, integration loaded, go2rtc-backed)
    private → ProtectClient.status() (API key valid, event search / clip export)

    status is ok only when BOTH sides are healthy — the same all-green rule the
    Apple Home certified card uses.
    """
    try:
        native = unifi_layer.report()
    except Exception as e:  # noqa: BLE001
        native = {"status": FAULT, "error": str(e), "loaded": False, "cameras": 0}
    try:
        private = protect_client.status()
    except Exception as e:  # noqa: BLE001
        private = {"ok": False, "error": str(e)}

    native_ok = native.get("status") == OK
    # ProtectClient.status() reports its health as `private_ok` (reachable + key
    # valid), not `ok` — read the flag it actually emits.
    private_ok = bool(private.get("private_ok") or private.get("ok")
                      or private.get("reachable"))
    if native_ok and private_ok:
        status, summary = OK, f"{native.get('cameras', 0)} cameras · events + clips ready"
    elif native.get("configured") or private_ok:
        status, summary = PENDING, "UniFi Protect partially configured"
    else:
        status, summary = FAULT, "UniFi Protect not configured"
    return {
        "service": "unifi_protect",
        "brand": "UniFi Protect — ProOS Certified",
        "status": status,
        "summary": summary,
        "native": native,
        "private": private,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Curation — Apple-Home-style exposure layer for the certified integration
# ─────────────────────────────────────────────────────────────────────────────
# The console exposes every physical camera; the installer curates what the
# homeowner actually sees + how each camera behaves. Mirrors the Apple Home
# exposure model: an explicit include set (never "expose everything" by
# accident) plus per-camera roles. Persisted as a plain /data JSON like the
# other ProCore stores (consent, credentials).
#
#   exposed   : [camId, ...]   cameras surfaced to the homeowner
#   doorbell  : camId | None   the doorbell (drives the doorbell widget/PiP)
#   talkback  : [camId, ...]   cameras with two-way audio enabled
# Capability flags (canTalk / hasPackage / smart) are read live from the
# console so the UI can default sensibly and never offer talk on a camera
# with no speaker.

CURATION_STORE = os.path.join(
    os.environ.get("PROOS_DATA_DIR", "/data"), "unifi_curation.json")
_SMART = ("person", "vehicle", "animal", "package", "face", "licensePlate")


def curation_load() -> dict:
    try:
        with open(CURATION_STORE) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def curation_save(data: dict) -> dict:
    clean = {
        "configured": True,
        "exposed": [str(x) for x in (data.get("exposed") or [])],
        "doorbell": (str(data["doorbell"]) if data.get("doorbell") else None),
        "talkback": [str(x) for x in (data.get("talkback") or [])],
    }
    try:
        os.makedirs(os.path.dirname(CURATION_STORE) or "/data", exist_ok=True)
        with open(CURATION_STORE, "w") as f:
            json.dump(clean, f, indent=2)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "curation": clean}


def curation_view(client) -> dict:
    """Live camera list merged with saved curation, for the installer UI + dashboard.

    Until an installer saves once (configured=False), everything defaults to a
    sensible auto-config: all cameras exposed, talk on every speaker-equipped
    camera, the package-camera-bearing camera treated as the doorbell.
    """
    cur = curation_load()
    configured = bool(cur.get("configured"))
    exposed = set(cur.get("exposed") or [])
    talk = set(cur.get("talkback") or [])
    try:
        raw = client.cameras()
    except Exception as e:  # noqa: BLE001
        return {"cameras": [], "curation": cur, "configured": configured,
                "error": str(e)}
    if isinstance(raw, dict):
        raw = raw.get("result") or raw.get("cameras") or []
    if not isinstance(raw, list):
        raw = []

    cams = []
    auto_doorbell = None
    for c in raw:
        cid = str(c.get("id"))
        ff = c.get("featureFlags") or {}
        has_speaker = bool(ff.get("hasSpeaker"))
        has_package = bool(c.get("hasPackageCamera"))
        smart = [s for s in (ff.get("smartDetectTypes") or []) if s in _SMART]
        if has_package and auto_doorbell is None:
            auto_doorbell = cid
        cams.append({
            "id": cid,
            "name": c.get("name"),
            "canTalk": has_speaker,
            "hasPackage": has_package,
            "smart": smart,
            # curation state (auto-defaults until configured)
            "exposed": (cid in exposed) if configured else True,
            "talkback": (cid in talk) if configured else has_speaker,
            "_has_package": has_package,
        })
    doorbell = cur.get("doorbell") if configured else auto_doorbell
    for c in cams:
        c["doorbell"] = (c["id"] == doorbell)
        c.pop("_has_package", None)
    return {"cameras": cams, "curation": cur, "configured": configured,
            "doorbell": doorbell}
