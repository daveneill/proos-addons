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
