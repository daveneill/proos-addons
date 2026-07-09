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

    # ---- config ----
    def _conf(self):
        key = self._creds.get(SERVICE)
        meta = self._creds.meta(SERVICE) or {}
        host = meta.get("host")
        return key, host, bool(meta.get("verify_ssl"))

    def configured(self) -> bool:
        key, host, _ = self._conf()
        return bool(key and host)

    def status(self) -> dict:
        key, host, verify = self._conf()
        out = {"configured": bool(key and host), "host": host, "verify_ssl": verify}
        if key and host:
            try:
                info = self._req("GET", INTEGRATION, "/v1/meta/info")
                out["reachable"] = True
                out["application_version"] = (info or {}).get("applicationVersion")
            except ProtectError as e:
                out["reachable"] = False
                out["error"] = e.message
        return out

    def set_config(self, host: str, api_key: str, verify_ssl: bool = False) -> dict:
        host = (host or "").strip().rstrip("/").replace("https://", "").replace("http://", "")
        if not host or not api_key:
            raise ProtectError(400, "host and api_key are required")
        self._creds.put(SERVICE, api_key.strip(), name="UniFi Protect",
                        host=host, extra={"verify_ssl": bool(verify_ssl)})
        return self.status()

    # ---- transport ----
    def _req(self, method: str, base: str, path: str, *, body=None,
             query: dict | None = None, raw: bool = False, timeout: int = 20):
        key, host, verify = self._conf()
        if not (key and host):
            raise ProtectError(409, "UniFi Protect is not configured")
        url = f"https://{host}{base}{path}"
        if query:
            q = urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")}, doseq=True)
            if q:
                url += "?" + q
        data = None
        headers = {"X-API-KEY": key, "Accept": "application/json"}
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
        body = {"camera": camera, "start": start, "end": end}
        if kind:
            body["type"] = kind
        if fps:
            body["fps"] = fps
        return self._req("POST", PRIVATE, "/video/export", body=body, raw=True, timeout=120)


# ---------------------------------------------------------------------------
# HTTP dispatch — called from server.py do_GET / do_POST when parts[0]=="unifi".
# Returns (status:int, content_type:str, payload: bytes|dict).
# `parts` is the path after "unifi" (e.g. ["protect","cameras"]); qs is a dict of
# already-parsed query params (lists); body is the parsed JSON dict (or None).
# ---------------------------------------------------------------------------
def handle(method, parts, qs, body, creds):
    if not parts or parts[0] != "protect":
        return 404, "application/json", {"errorMessage": "not found"}
    p = parts[1:]
    client = ProtectClient(creds)

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
                b.get("host"), b.get("api_key") or b.get("apiKey"), b.get("verify_ssl", False))

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
