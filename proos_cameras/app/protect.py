"""ProOS Cameras — UniFi Protect, through Ubiquiti's official Integration API only (register 699).

The console's address and key are the platform's UniFi Protect integration's own (register 439 / 695): read from
its config entry where they live, on every call, never kept. The same reading ProOS Core makes.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

STORE = os.environ.get("PROOS_PLATFORM_ENTRIES",
                       ("/homeassistant" if os.path.isdir("/homeassistant/.storage") else "/config")
                       + "/.storage/core.config_entries")
BASE = "/proxy/protect/integration"

_unverified = ssl.create_default_context()
_unverified.check_hostname = False
_unverified.verify_mode = ssl.CERT_NONE


class ProtectError(Exception):
    pass


def entry_data(store: str = STORE) -> dict:
    try:
        with open(store, encoding="utf-8") as fh:
            d = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}
    for e in (d.get("data") or {}).get("entries") or []:
        if e.get("domain") == "unifiprotect" and not e.get("disabled_by"):
            return dict(e.get("data") or {})
    return {}


def conf(store: str = STORE):
    """(key, host, verify) from the platform's own unifiprotect entry."""
    d = entry_data(store)
    key = str(d.get("api_key") or "").strip()
    host = str(d.get("host") or "").strip().rstrip("/").replace("https://", "").replace("http://", "")
    port = d.get("port")
    if host and port and int(port) not in (443, 0) and ":" not in host:
        host = "%s:%s" % (host, int(port))
    return key, host, bool(d.get("verify_ssl"))


def req(method: str, path: str, body=None, raw: bool = False, query: dict | None = None, timeout: int = 10,
        store: str = STORE):
    key, host, verify = conf(store)
    if not (key and host):
        raise ProtectError("UniFi Protect is not set up in Home Assistant (no address or key in its entry)")
    url = "https://%s%s%s" % (host, BASE, path)
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "X-API-KEY": key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=None if verify else _unverified) as resp:
            payload = resp.read()
            if raw:
                return payload
            return json.loads(payload.decode()) if payload else {}
    except urllib.error.HTTPError as e:
        raise ProtectError("Protect answered %s on %s" % (e.code, path))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProtectError("cannot reach Protect: %s" % (getattr(e, "reason", e),))


def norm_mac(m) -> str:
    return "".join(ch for ch in str(m or "").upper() if ch in "0123456789ABCDEF")


def cameras(store: str = STORE) -> list:
    return req("GET", "/v1/cameras", store=store) or []


def camera_id_for_mac(mac: str, store: str = STORE) -> str | None:
    want = norm_mac(mac)
    for c in cameras(store):
        if norm_mac(c.get("mac")) == want:
            return c.get("id")
    return None


def snapshot(cid: str, store: str = STORE) -> bytes:
    return req("GET", "/v1/cameras/%s/snapshot" % cid, raw=True, query={"highQuality": "true"}, store=store)


def stream_url(cid: str, quality: str = "high", store: str = STORE) -> str:
    """The camera's RTSPS address for a quality, made through the API if it has none yet."""
    got = req("GET", "/v1/cameras/%s/rtsps-stream" % cid, store=store) or {}
    url = got.get(quality)
    if not url:
        got = req("POST", "/v1/cameras/%s/rtsps-stream" % cid, body={"qualities": [quality]}, store=store) or {}
        url = got.get(quality)
    if not url:
        raise ProtectError("Protect gave no %s stream for this camera" % quality)
    return url


def talkback(cid: str, store: str = STORE) -> dict:
    """Ubiquiti's official talk session: {url: rtp://<camera>:7004, codec, samplingRate, bitsPerSample}."""
    return req("POST", "/v1/cameras/%s/talkback-session" % cid, store=store) or {}
